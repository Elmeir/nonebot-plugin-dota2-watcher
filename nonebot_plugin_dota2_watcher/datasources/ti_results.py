"""
抓取 The International 2026 (TI15) 最新比赛胜负结果，并按赛程阶段生成战报图片。

数据来源：Valve 官方赛事 API
  GET https://www.dota2.com/webapi/IDOTA2League/GetLeagueData/v001?league_id=19719&delay_seconds=0

说明：
  - TI 2026 联赛 ID 为 19719。
  - 数据嵌套结构：node_groups[].node_groups[].nodes[]
      · nodes[] 中的每一项代表一场 BO 系列赛（series）
      · team_1_wins / team_2_wins 为该系列当前比分
      · matches[] 里每局含 winning_team_id 与 match_id
      · team_standings[] 提供 team_id -> 队伍名称/tag 的映射
  - 运行依赖：抓取赛果仅用标准库；生成战报图片需额外安装 Pillow 与 Playwright。
"""

import asyncio
import base64
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

# 兼容两种运行方式：作为插件包被导入，或作为独立脚本直接运行。
# 所有 URL / 目录 / 赛事配置统一从 config.py 读取。
if __package__:
    from .. import config as _cfg
    from ..generators import shared_browser
    from ..utils import (
        cache_with_fallback,
        download_bytes,
        dumpjson,
        image_to_data_uri,
        load_cache,
    )
else:
    import config as _cfg
    import shared_browser
    from utils import cache_with_fallback, download_bytes, dumpjson, image_to_data_uri, load_cache

# TI 2026 联赛 ID（可由 dota2.com/esports/ti15/schedule 页面 URL 中的 ti15 参数确认）
LEAGUE_ID = _cfg.TI_LEAGUE_ID

# 官方 API 端点
API_URL = _cfg.DOTA2_API_URL.format(league_id=LEAGUE_ID)

# 本地时区（北京时间 UTC+8）
LOCAL_TZ = timezone(timedelta(hours=8))

# 请求头（模拟浏览器，避免被部分边缘节点拒绝）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": _cfg.TI_REFERER,
}

# ==================== Steam Web API（实时单局详情，需要 key） ====================
# 可通过 https://steamcommunity.com/dev/apikey 申请，或用环境变量 STEAM_API_KEY 指定。
# 优先使用独立配置 D2W_TI_STEAM_API_KEY，其次环境变量 STEAM_API_KEY，最后回退 D2W_STEAM_API_KEY。
STEAM_API_KEY = (
    _cfg.config.d2w_ti_steam_api_key
    or os.environ.get("STEAM_API_KEY")
    or _cfg.config.d2w_steam_api_key
)
STEAM_API_BASE = _cfg.STEAM_API_BASE
# 当前直播中的联赛比赛（含实时比分板/阵容/英雄）
LIVE_GAMES_URL = _cfg.STEAM_LIVE_GAMES_URL.format(key=STEAM_API_KEY)
# 英雄 ID -> 英雄名 映射（官方站点自带接口，无需 key）
HEROES_URL = _cfg.DOTA2_HEROES_URL


def _urlopen_with_retry(req, timeout, retries=3, delay=1.0):
    """带重试的 urlopen：短超时 + 多重重试，提升瞬时网络抖动下的成功率。"""
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception:  # 网络异常统一重试，最后一次直接抛出
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def fetch_league_data(url=API_URL, headers=HEADERS):
    """请求官方 API，返回解析后的 JSON 字典。

    不做网络重试：失败直接抛出异常，由调用方决定是否回退/重试。
    """
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        # 官方 API 偶发返回 null/空，需显式报错供调用方回退/重试
        raise ValueError(f"官方 API 返回非字典数据: {type(data).__name__}")
    return data


def _walk_node_groups(node_groups, on_group):
    """DFS 前序遍历 node_groups 树，对每个分组 dict 依次调用 on_group(group)。

    供 build_team_map / collect_series / build_full_team_info 复用，
    统一「递归遍历 node_groups -> node_groups/nodes」的分组树遍历逻辑。
    """
    stack = list(node_groups or [])
    while stack:
        g = stack.pop()
        if not isinstance(g, dict):
            continue
        on_group(g)
        stack.extend(g.get("node_groups") or [])


def build_team_map(node_groups):
    """遍历所有外/内层分组，建立 team_id -> 官方完整队名 的映射。

    优先使用 team_name（缺失时回退到 team_abbreviation / team_tag）。
    """
    team_map = {}

    def collect_group(group):
        """收集单个分组的 team_standings 与 node 队伍 id，写入 team_map。"""
        for ts in group.get("team_standings", []) or []:
            if not isinstance(ts, dict):
                continue
            tid = ts.get("team_id")
            if tid:
                team_map[tid] = (
                    ts.get("team_name")
                    or ts.get("team_abbreviation")
                    or ts.get("team_tag")
                    or str(tid)
                )
        for node in group.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            for tid in (node.get("team_id_1"), node.get("team_id_2")):
                if tid:
                    team_map.setdefault(tid, str(tid))

    _walk_node_groups(node_groups, collect_group)
    return team_map


def collect_series(node_groups):
    """遍历所有分组，收集所有比赛系列(nodes)。"""
    series = []

    def _collect(group):
        for node in group.get("nodes", []) or []:
            if isinstance(node, dict):
                series.append(node)

    _walk_node_groups(node_groups, _collect)
    return series


def _score_line(t1, w1, w2, t2):
    """格式化比赛比分行：'{战队A} {比分A} : {比分B} {战队B}'（冒号两侧带空格）。"""
    return f"{t1} {w1} : {w2} {t2}"


def _series_score(node):
    """返回系列赛比分 (w1, w2)，优先使用官方聚合字段 team_1_wins/team_2_wins。

    用 matches 单局 winning_team_id 统计交叉校验：若统计胜场更多（聚合字段尚未
    同步刚结束的单局，存在瞬时滞后），则采用 matches 统计，避免比分少显示一局。
    """
    w1 = int(node.get("team_1_wins") or 0)
    w2 = int(node.get("team_2_wins") or 0)
    try:
        t1, t2 = int(node.get("team_id_1")), int(node.get("team_id_2"))
    except (TypeError, ValueError):
        return w1, w2
    wins = {t1: 0, t2: 0}
    for m in node.get("matches", []) or []:
        if isinstance(m, dict):
            try:
                wid = int(m.get("winning_team_id"))
            except (TypeError, ValueError):
                continue
            if wid in wins:
                wins[wid] += 1
    c1, c2 = wins[t1], wins[t2]
    return (c1, c2) if c1 + c2 > w1 + w2 else (w1, w2)


def collect_finished_games(data, team_map):
    """汇总所有已结束的单局。

    返回 dict: match_id -> {'series_id','t1','t2','winner','w1','w2','name'}
    其中 t1/t2 为对战双方队名，winner 为本局胜者，w1/w2 为系列比分。
    """
    result = {}
    for node in collect_series(data.get("node_groups", [])):
        t1 = team_map.get(node.get("team_id_1"), "TBD")
        t2 = team_map.get(node.get("team_id_2"), "TBD")
        w1, w2 = _series_score(node)
        for m in node.get("matches", []) or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("match_id")
            if not mid:
                continue
            win_id = m.get("winning_team_id")
            if not win_id:  # 未结束的单局不收集
                continue
            winner = team_map.get(win_id, "?")
            result[mid] = {
                "series_id": node.get("series_id"),
                "name": node.get("name"),
                "t1": t1,
                "t2": t2,
                "winner": winner,
                "w1": w1,
                "w2": w2,
            }
    return result


def watch_finished(interval):
    """每 interval 秒轮询官方 API，检测并打印最新结束的比赛结果，直到 Ctrl+C。

    输出格式统一为 '[TI] {战队A} {比分A}:{比分B} {战队B}'（该局结束后的系列比分）。
    首次轮询仅建立基线（不输出历史结果），之后每次只报告新结束的对局。
    """
    print(f"[*] 开始监听最新结束的比赛（每 {interval}s 检查一次，Ctrl+C 停止）...")
    seen = set()  # 已报告过的 match_id
    first_pass = True  # 首次仅建立基线
    while True:
        stamp = time.strftime("%H:%M:%S")
        try:
            # 轮询监控不做网络重试：失败即走外层 sleep(interval) 兜底
            data = fetch_league_data()
            team_map = build_team_map(data.get("node_groups", []))
            games = collect_finished_games(data, team_map)
        except Exception as exc:
            print(f"[{stamp}] [!] 获取失败: {exc}", file=sys.stderr)
            time.sleep(interval)
            continue

        if first_pass:
            first_pass = False
            for mid in games:
                seen.add(mid)
            print(f"[{stamp}] [基线] 已记录 {len(games)} 个已结束单局，开始监听新结果...")
            time.sleep(interval)
            continue

        # 报告自上次以来新结束的单局（附该局结束后的系列比分）
        for mid, g in games.items():
            if mid in seen:
                continue
            seen.add(mid)
            print(f"[{stamp}] [TI] {_score_line(g['t1'], g['w1'], g['w2'], g['t2'])}")

        time.sleep(interval)


# watch 状态（模块级，供 watch_latest_result 多次调用去重）
_watch_seen_series = set()
_watch_baseline_done = False
_watch_seen_games = set()
_watch_game_baseline_done = False
_watch_next_check_ts = 0.0  # 下次允许真正拉取数据的时间戳（时间门控）

# 根据赛程时间动态调整轮询间隔（秒）
WATCH_INTERVAL_LIVE = 10  # 存在进行中比赛
WATCH_INTERVAL_SOON = 30  # 最近一场将在 SOON 窗口内开赛
WATCH_INTERVAL_NEAR = 60  # 最近一场将在 NEAR 窗口内开赛
WATCH_INTERVAL_IDLE = 300  # 空闲 / 无赛程 / 距离开赛较远
WATCH_INTERVAL_RETRY = 30  # 获取数据失败后的重试间隔（避免误判为空闲导致错过比赛）
WATCH_SOON_WINDOW = 600  # 10 分钟内
WATCH_NEAR_WINDOW = 1800  # 30 分钟内
WATCH_GATE_TOLERANCE = 1.0  # 门控容差（秒）：避免与外部调用间隔相等时的边界抖动
_watch_interval = WATCH_INTERVAL_IDLE  # 当前生效的动态轮询间隔（失败时沿用）


def recommend_poll_interval(node_groups, now=None):
    """根据当前赛程动态计算建议的下次轮询间隔（秒）。

    优先级：
      - 存在进行中的系列赛 -> WATCH_INTERVAL_LIVE
      - 最近一场未开始系列赛将在 WATCH_SOON_WINDOW 内开赛 -> WATCH_INTERVAL_SOON
      - 将在 WATCH_NEAR_WINDOW 内开赛 -> WATCH_INTERVAL_NEAR
      - 其它（空闲/无赛程/距离较远） -> WATCH_INTERVAL_IDLE
    """
    if now is None:
        now = time.time()

    live = False
    nearest = None
    for node in collect_series(node_groups or []):
        if node.get("is_completed"):
            continue
        if node.get("has_started"):
            live = True
            continue
        # 未开始：只要 scheduled_time 是有效未来时间就纳入“最近开赛”判断。
        # 瑞士轮下一轮对阵未公布时 node 的 team_id 为 0，但仍携带轮次开赛时间，
        # 若跳过这些占位节点，将无法提前识别“即将开赛”，导致间歇期误判为空闲。
        st = node.get("scheduled_time") or 0
        if st > now and (nearest is None or st < nearest):
            nearest = st

    if live:
        return WATCH_INTERVAL_LIVE
    if nearest is None:
        return WATCH_INTERVAL_IDLE
    diff = nearest - now
    if diff <= WATCH_SOON_WINDOW:
        return WATCH_INTERVAL_SOON
    if diff <= WATCH_NEAR_WINDOW:
        return WATCH_INTERVAL_NEAR
    return WATCH_INTERVAL_IDLE


async def watch_latest_result(mode="series", debug=False):
    """监控 TI2026 最新比赛结果，返回自上次以来新结束的结果字符串（异步）。

    函数内部通过时间门控实现“根据赛程时间动态调整间隔”：每次真正拉取数据后，
    根据赛程计算下一次允许拉取的时间（recommend_poll_interval）。外部以固定小间隔
    （如 10 秒）调用时，未到下次拉取时间会直接返回空字符串，不发起 API 请求。

    参数 mode:
      - 'series'（默认）：只返回系列赛（BO3/BO5）终局结果。
        行格式: '{战队A} {比分A}:{比分B} {战队B}'，如 'TEAM VISION 2:1 Team Falcons'
      - 'game'：只返回单局比赛结果（附该局结束后的系列赛比分）。
        行格式: '{战队A} {比分A}:{比分B} {战队B}'，如 'TEAM VISION 1:0 Team Falcons'

    参数 debug:
      - 默认 False 关闭；设为 True 时打印动态轮询间隔 DEBUG 日志（当前时间 / 动态间隔 / 下次拉取时间）。

    返回:
      - 多个结果用换行连接，仅第一行带 [TI] 前缀，其余行不带。
      - 首次调用仅建立基线，返回空字符串 ''。
      - 未到动态间隔 / 无新结果 / 获取数据失败时返回 ''。

    用法:
        result = await watch_latest_result(mode='series')
        result = await watch_latest_result(mode='game', debug=True)
        if result:
            for line in result.split("\\n"):
                ...
    """
    global _watch_seen_series, _watch_baseline_done, _watch_seen_games, _watch_game_baseline_done
    global _watch_next_check_ts, _watch_interval

    now = time.time()
    if now < _watch_next_check_ts - WATCH_GATE_TOLERANCE:
        # 尚未到达动态间隔，直接跳过本次检查
        if debug:
            print(
                f"[DEBUG] {datetime.now(LOCAL_TZ).strftime('%H:%M:%S')} "
                f"门控跳过, 距下次拉取约 {_watch_next_check_ts - now:.0f}s",
                file=sys.stderr,
            )
        return ""

    try:
        # 轮询监控不做网络重试：失败即快速返回，由外层按重试间隔再试
        data = await asyncio.to_thread(fetch_league_data)
    except Exception as exc:
        print(f"[!] 获取比赛数据失败: {exc}", file=sys.stderr)
        if _watch_next_check_ts == 0.0:
            # 首次启动即失败：读取缓存文件仅用于估算动态间隔，无缓存才退避到重试间隔
            cache = _load_league_data_cache()
            if not cache:
                print("[*] 无本地缓存可用，退避到重试间隔", file=sys.stderr)
                _watch_next_check_ts = now + WATCH_INTERVAL_RETRY
                return ""
            _watch_interval = recommend_poll_interval(cache.get("node_groups", []))
            _watch_next_check_ts = now + _watch_interval
            print("[*] 网络失败，按缓存赛程沿用动态间隔", file=sys.stderr)
            return ""
        else:
            # 非首次失败：沿用上次成功时的动态间隔，避免误判为空闲/退出重试档位
            _watch_next_check_ts = now + _watch_interval
            return ""

    # 抓取成功，保存到本地缓存 data/dota2_ti.json（供战报图片函数失败时回退）
    await asyncio.to_thread(_save_league_data_cache, data)

    team_map = build_team_map(data.get("node_groups", []))
    next_interval = recommend_poll_interval(data.get("node_groups", []))
    _watch_interval = next_interval
    _watch_next_check_ts = now + next_interval
    if debug:
        print(
            f"[DEBUG] {datetime.now(LOCAL_TZ).strftime('%H:%M:%S')} "
            f"动态间隔={next_interval}s, "
            f"下次拉取={datetime.fromtimestamp(_watch_next_check_ts, LOCAL_TZ).strftime('%H:%M:%S')}",
            file=sys.stderr,
        )

    if mode == "game":
        # —— game 模式：返回新结束的单局（附该局结束后的系列赛比分）——
        finished = []
        for node in collect_series(data.get("node_groups", [])):
            t1 = team_map.get(node.get("team_id_1"), "TBD")
            t2 = team_map.get(node.get("team_id_2"), "TBD")
            t1_id = node.get("team_id_1")
            t2_id = node.get("team_id_2")
            w1 = w2 = 0
            for m in node.get("matches", []) or []:
                if not isinstance(m, dict):
                    continue
                mid = m.get("match_id")
                win_id = m.get("winning_team_id")
                if not mid or not win_id:
                    continue
                if win_id == t1_id:
                    w1 += 1
                elif win_id == t2_id:
                    w2 += 1
                else:
                    continue  # 胜者不在对战双方中，跳过
                finished.append((mid, _score_line(t1, w1, w2, t2)))
        finished.sort(key=lambda x: x[0])
        if not _watch_game_baseline_done:
            _watch_game_baseline_done = True
            _watch_seen_games = {mid for mid, _ in finished}
            pending = []
        else:
            pending = []
            for mid, text in finished:
                if mid not in _watch_seen_games:
                    _watch_seen_games.add(mid)
                    pending.append(text)
    else:
        # —— series 模式（默认）：只返回新结束的系列赛（BO3/BO5）——
        finished = []
        for node in collect_series(data.get("node_groups", [])):
            if not node.get("is_completed"):
                continue
            sid = node.get("series_id")
            if sid:
                finished.append(
                    (node.get("actual_time") or node.get("scheduled_time") or 0, node, sid)
                )
        finished.sort(key=lambda x: x[0], reverse=True)
        if not _watch_baseline_done:
            # 首次调用：仅建立基线，把当前所有已结束的系列赛记为已见，不返回任何结果
            _watch_baseline_done = True
            _watch_seen_series = {sid for _, _, sid in finished}
            pending = []
        else:
            # 后续调用：返回所有尚未报告过的（按时间从新到旧）
            pending = []
            for _, n, sid in finished:
                if sid not in _watch_seen_series:
                    _watch_seen_series.add(sid)
                    t1 = team_map.get(n.get("team_id_1"), "TBD")
                    t2 = team_map.get(n.get("team_id_2"), "TBD")
                    w1, w2 = _series_score(n)
                    pending.append(_score_line(t1, w1, w2, t2))

    if not pending:
        return ""
    text = "\n".join(f"[ti] {p}" if i == 0 else p for i, p in enumerate(pending))
    return text


def fmt_ts(ts):
    """Unix 时间戳 -> 'YYYY-MM-DD HH:MM' 北京时间。"""
    if not ts:
        return "--"
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def series_status(node):
    """根据 node 字段判断系列状态：已结束 / 进行中 / 未开始。"""
    if node.get("is_completed"):
        return "Finished"
    if node.get("has_started"):
        return "Live"
    return "Scheduled"


# ==================== Steam 实时单局详情 ====================


def fetch_steam_json(url):
    """请求 Steam Web API 并返回 JSON 字典。"""
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_hero_map():
    """获取 hero_id -> 英雄名(中文) 映射。失败时返回空字典。"""
    try:
        data = fetch_steam_json(HEROES_URL)
        heroes = data.get("result", {}).get("data", {}).get("heroes", [])
        return {h["id"]: h.get("name_loc") or h.get("name") for h in heroes}
    except Exception as exc:
        print(f"[!] 获取英雄列表失败: {exc}", file=sys.stderr)
        return {}


def fetch_live_games():
    """获取 TI 联赛当前直播中的比赛（含比分板）。未在直播时返回空列表。"""
    data = fetch_steam_json(LIVE_GAMES_URL)
    games = data.get("result", {}).get("games", [])
    return [g for g in games if g.get("league_id") == LEAGUE_ID]


def fmt_duration(seconds):
    """秒 -> 'MM:SS'。"""
    if not seconds:
        return "--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _team_line(side, hero_map):
    """格式化单侧英雄列表：'英雄名 (K/D/A, 等级, 经济)'。"""
    parts = []
    for p in sorted(side.get("players", []), key=lambda x: x.get("player_slot", 0)):
        if not p.get("hero_id"):
            parts.append("选择中")
            continue
        hero = hero_map.get(p.get("hero_id"), f"Hero{p.get('hero_id')}")
        k, d, a = p.get("kills", 0), p.get("death", 0), p.get("assists", 0)
        lvl = p.get("level", 0)
        nw = p.get("net_worth", 0)
        parts.append(f"{hero} ({k}/{d}/{a} Lv{lvl} {nw:,}G)")
    return "  ".join(parts)


def print_live_details(games, hero_map):
    """打印 Steam 实时单局详情（阵容/比分板/英雄）。"""
    print("\n" + "=" * 78)
    print(f"### Steam 实时单局详情 ({len(games)} 场直播中)")
    if not games:
        print("[i] 当前 TI 联赛暂无直播中的比赛（系列局间也可能短暂为空）。")
        return
    for g in games:
        sb = g.get("scoreboard") or {}
        radiant = sb.get("radiant") or {}
        dire = sb.get("dire") or {}
        rname = g.get("radiant_team", {}).get("team_name", "Radiant")
        dname = g.get("dire_team", {}).get("team_name", "Dire")
        rw, dw = g.get("radiant_series_wins", 0), g.get("dire_series_wins", 0)
        print(f"\n[{g.get('match_id')}] {rname} {rw}:{dw} {dname}")
        print(
            f"  比分板: {radiant.get('score', 0)} - {dire.get('score', 0)}  "
            f"时长 {fmt_duration(sb.get('duration'))}"
        )
        print(f"  天辉 {rname}: {_team_line(radiant, hero_map)}")
        print(f"  夜魇 {dname}: {_team_line(dire, hero_map)}")


# ==================== 瑞士轮积分战报 ====================


def find_swiss_group(node_groups):
    """在 node_groups 树里定位 name == 'Swiss' 的分组。"""
    return find_node_group(node_groups, r"^Swiss$")


def find_node_group(node_groups, pattern):
    """在 node_groups 树里按名称正则查找分组，返回第一个匹配的 dict；未命中返回 None。"""
    rx = re.compile(pattern, re.I)
    queue = deque(node_groups or [])
    while queue:
        g = queue.popleft()
        if not isinstance(g, dict):
            continue
        if rx.search(g.get("name") or ""):
            return g
        queue.extend(g.get("node_groups") or [])
    return None


def _group_has_active_series(group):
    """判断分组中是否存在已开赛/已结束/已排定对阵的节点。"""
    return any(
        n.get("has_started") or n.get("is_completed") or (n.get("team_id_1") and n.get("team_id_2"))
        for n in group.get("nodes") or []
        if isinstance(n, dict)
    )


def detect_league_stage(node_groups):
    """判断当前赛程阶段。

    返回 'main_event'（国际邀请赛正赛/淘汰赛）、'elimination_round'（瑞士轮加赛）
    或 'swiss'（小组赛）。

    规则：
      · 正赛分组（Playoff/Main Event）中任一节点已开赛/已结束/已排定对阵
        => main_event
      · Elimination Round（瑞士轮晋级赛）已开赛/已结束/已排定对阵或已确定参赛队
        => elimination_round
      · 数据中不存在小组赛分组（只有正赛）=> main_event
      · 其余 => swiss
    """
    playoff = find_node_group(node_groups, r"playoff|main.?event")
    if playoff and _group_has_active_series(playoff):
        return "main_event"

    elim = find_node_group(node_groups, r"elimination round")
    if elim:
        elim_seeded = any(ts.get("team_id") for ts in elim.get("team_standings") or [])
        if _group_has_active_series(elim) or elim_seeded:
            return "elimination_round"

    if not find_node_group(node_groups, r"swiss|group"):
        return "main_event" if playoff else "swiss"
    return "swiss"


def build_full_team_info(node_groups):
    """构建 team_id -> {name, abbr, logo, wins, losses, standing}。

    优先用 Swiss 分组里 team_standings 的官方缩写和完整信息；
    缺失时回退到其它分组的 team_standings / node 中出现过的 id。
    """
    info = {}

    def merge(tid, **fields):
        """把字段合并进 info[tid]：仅当值有效或该键尚不存在时写入。"""
        if not tid:
            return
        tid = int(tid)
        cur = info.setdefault(tid, {})
        for k, v in fields.items():
            if v not in (None, "", 0, "0") or k not in cur:
                cur[k] = v

    # 1) 遍历所有 group 的 team_standings
    def walk(group):
        """合并分组 team_standings，并为 nodes 出现的 team_id 建空记录。"""
        for ts in group.get("team_standings", []):
            tid = ts.get("team_id")
            if not tid or int(tid) == 0:
                continue
            merge(
                tid,
                name=ts.get("team_name"),
                abbr=ts.get("team_abbreviation") or ts.get("team_tag"),
                logo=ts.get("team_logo_url"),
                wins=int(ts.get("wins", 0) or 0),
                losses=int(ts.get("losses", 0) or 0),
                standing=int(ts.get("standing", 0) or 0),
                tiebreak_game_win_pct=ts.get("tiebreak_game_win_pct"),
                tiebreak_opponent_match_wins=ts.get("tiebreak_opponent_match_wins"),
                tiebreak_opponent_game_win_pct=ts.get("tiebreak_opponent_game_win_pct"),
                tiebreak_coinflip=ts.get("tiebreak_coinflip"),
                tiebreak_average_game_length=ts.get("tiebreak_average_game_length"),
                score=ts.get("score"),
            )
        for node in group.get("nodes", []):
            for tid in (node.get("team_id_1"), node.get("team_id_2")):
                merge(tid)  # 至少建一条空记录，避免缩写为 TBD

    _walk_node_groups(node_groups, walk)

    # 确保每个队伍至少有个可读的显示名/缩写
    for tid, meta in info.items():
        meta.setdefault("name", f"Team{tid}")
        meta.setdefault("abbr", meta["name"])
    return info


def compute_team_game_record(swiss_nodes, team_info):
    """给每支队伍计算 Games 胜负场（例如 3-1），以及每轮对手。

    这里的 Round 是"每支战队自身的第 N 场比赛"顺序（按 scheduled_time 升序编号），
    而不是全局时间段，因为瑞士轮 16 队会拆成两个子段开赛，战队 A 的 Round 1 与战队 B
    的 Round 1 可能不在同一个 scheduled_time。

    返回:
        team_games:   tid -> (gw, gl)
        team_rounds:  tid -> {round_idx: (opp_abbr, team_wins, opp_wins, completed, started)}
        total_rounds: int（轮次数，最大轮号）
    """
    team_games = {}
    team_rounds = {}

    def abbr(tid):
        return team_info.get(int(tid), {}).get("abbr") or f"T{tid}"

    # 1) 按战队收集各自的所有 matches（携带时间排序键，供步骤 2 排序）
    team_matches = {}
    for n in swiss_nodes:
        t1 = n.get("team_id_1")
        t2 = n.get("team_id_2")
        if not t1 or not t2:
            continue
        t1, t2 = int(t1), int(t2)
        key = (n.get("scheduled_time") or 0, n.get("actual_time") or 0, n.get("series_id") or 0)
        team_matches.setdefault(t1, []).append((key, n, "t1"))
        team_matches.setdefault(t2, []).append((key, n, "t2"))

    # 2) 对每支战队按时间序编号 Round 1/2/3/...
    max_round = 0
    for tid, lst in team_matches.items():
        lst.sort(key=lambda x: x[0])
        for idx, (_, n, side) in enumerate(lst, start=1):
            if idx > max_round:
                max_round = idx
            t1 = int(n["team_id_1"])
            t2 = int(n["team_id_2"])
            w1, w2 = _series_score(n)
            completed = bool(n.get("is_completed"))
            started = bool(n.get("has_started"))
            if side == "t1":
                tw, ow = w1, w2
                opp = abbr(t2)
            else:
                tw, ow = w2, w1
                opp = abbr(t1)
            gw, gl = team_games.setdefault(tid, (0, 0))
            if completed:
                team_games[tid] = (gw + tw, gl + ow)
            team_rounds.setdefault(tid, {})[idx] = (opp, tw, ow, completed, started)

    return team_games, team_rounds, max_round


def _initial_group_of(tid, swiss_nodes):
    """返回战队初始分组 'A' 或 'B'（依据其第 1 轮对阵节点的 name 后缀 .A/.B）。

    官方规则：第 1-3 轮限初始组内对阵，第 4 轮跨组对阵，因此每队所有已完赛
    节点的 name 后缀一致，取最早（第 1 轮）节点的后缀即可。
    """
    best_st = None
    grp = None
    for n in swiss_nodes:
        t1, t2 = n.get("team_id_1"), n.get("team_id_2")
        if not t1 or not t2:
            continue
        if int(t1) == int(tid) or int(t2) == int(tid):
            st = n.get("scheduled_time") or 0
            if best_st is None or st < best_st:
                best_st = st
                name = (n.get("name") or "").rstrip()
                grp = "B" if name.endswith(".B") else "A"
    return grp


def _pairings_certain(tids_sorted, rank_key):
    """相邻配对是否完全确定（不受第 7 级"掷硬币"影响）。

    tids_sorted 已按排名排序，rank_key(tid) 返回不含队名兜底的排名键。
    相邻配对 (0,1),(2,3),... 中某对确定，当且仅当：
      - 同组（排名键相等）：该 tie 组大小为 2 且从该对开头（奇数位）开始，两队被迫相碰；
      - 跨组边界：两侧 tie 组都只有 1 队（排名键唯一），两队位置固定。
    """
    n = len(tids_sorted)
    keys = [rank_key(t) for t in tids_sorted]
    # 找出连续相同排名键的 tie 组
    group_of = {}  # 位置 -> (组起始, 组大小)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and keys[j + 1] == keys[i]:
            j += 1
        size = j - i + 1
        for idx in range(i, j + 1):
            group_of[idx] = (i, size)
        i = j + 1
    for k in range(0, n - 1, 2):
        a, b = k, k + 1
        pa, sa = group_of[a]
        pb, sb = group_of[b]
        if keys[a] == keys[b]:
            # 组内配对：必须恰为 2 队且从奇数位（a 位置）开始
            if not (sa == 2 and pa == a):
                return False
        else:
            # 边界配对：两侧都必须无并列
            if not (sa == 1 and sb == 1):
                return False
    return True


def predict_swiss_next_round(swiss_nodes, team_info):
    """按官方瑞士轮规则推算下一轮 100% 确定的组内/跨组对阵。

    官方规则（dota2.com/esports/ti15/tirules）：
      - 通用瑞士轮：战绩相同的队伍对阵、尽量不重复对阵、尽可能缩小排名差距；
      - 第 2-3 轮仅初始组内对阵；第 4 轮仅跨组对阵。
    算法：用上一轮结束后的战绩分池 -> 池内按 7 级 tiebreak 排名排序 -> 相邻配对
    （相邻配对即"排名差距最小"）。仅当配对完全确定（不受第 7 级"掷硬币"影响）
    且满足轮次分组约束、不重复对阵时才返回，宁缺毋滥。

    第 5 轮起为淘汰/最大化排名差距规则，不做预测。

    返回 {tid: 对手abbr}（仅含确定配对，双向）；无法确定时返回 {}。
    """
    from collections import defaultdict

    def abbr(tid):
        return team_info.get(int(tid), {}).get("abbr") or f"T{tid}"

    # 1) 收集每队已完成的系列赛：(对手tid, 系列胜0/1, 地图胜, 地图负)，按时间序
    completed = defaultdict(list)
    for n in swiss_nodes:
        t1, t2 = n.get("team_id_1"), n.get("team_id_2")
        if t1 in (None, 0) or t2 in (None, 0) or not n.get("is_completed"):
            continue
        t1, t2 = int(t1), int(t2)
        w1, w2 = _series_score(n)
        key = (n.get("scheduled_time") or 0, n.get("actual_time") or 0, n.get("series_id") or 0)
        completed[t1].append((key, t2, 1 if w1 >= 2 else 0, w1, w2))
        completed[t2].append((key, t1, 1 if w2 >= 2 else 0, w2, w1))
    for tid in completed:
        completed[tid].sort(key=lambda x: x[0])
        completed[tid] = [(opp, sw, gw, gl) for _, opp, sw, gw, gl in completed[tid]]

    # 2) 所有队伍必须已完成相同轮数，下一轮才可推算
    counts = {len(rounds) for rounds in completed.values()}
    if len(counts) != 1:
        return {}
    played = counts.pop()
    if played < 1 or played > 3:
        return {}  # 目标轮须在 2-4；第 5 轮起规则不同不做预测
    next_round = played + 1

    def rec(tid):
        """前 5 级 tiebreak 排名键：系列胜/负、对手系列胜和、地图胜率、对手平均地图胜率。"""
        rounds = completed[tid]
        w = sum(1 for _, sw, _, _ in rounds if sw)
        loss = played - w
        omw = 0
        ogwps = []
        for opp, _, _, _ in rounds:
            or_ = completed.get(opp, [])
            ow = sum(1 for _, sw2, _, _ in or_ if sw2)
            omw += ow
            ogw = sum(ggw for _, _, ggw, _ in or_)
            ogl = sum(ggl for _, _, _, ggl in or_)
            ogwps.append(ogw / (ogw + ogl) if (ogw + ogl) else 0.0)
        gw = sum(ggw for _, _, ggw, _ in rounds)
        gl = sum(ggl for _, _, _, ggl in rounds)
        gwp = gw / (gw + gl) if (gw + gl) else 0.0
        ogwp = sum(ogwps) / len(ogwps) if ogwps else 0.0
        return (w, loss, omw, gwp, ogwp)

    # 3) 按战绩分池
    if next_round in (2, 3):
        # 组内对阵：按 (战绩, 初始组) 分池
        pools = defaultdict(list)
        for tid in completed:
            w, losses, _, _, _ = rec(tid)
            pools[(w, losses, _initial_group_of(tid, swiss_nodes))].append(tid)
    else:  # next_round == 4：跨组对阵，按战绩分池
        pools = defaultdict(list)
        for tid in completed:
            w, losses, _, _, _ = rec(tid)
            pools[(w, losses)].append(tid)

    result = {}
    for tids in pools.values():
        if len(tids) < 2:
            continue
        tids_sorted = sorted(tids, key=lambda t: rec(t) + (abbr(t),))
        # 相邻配对必须完全确定 + 满足分组约束 + 不重复对阵，全部通过才采用
        if not _pairings_certain(tids_sorted, rec):
            continue
        pairs = [(tids_sorted[i], tids_sorted[i + 1]) for i in range(0, len(tids_sorted) - 1, 2)]
        valid = True
        for a, b in pairs:
            if next_round == 4 and _initial_group_of(a, swiss_nodes) == _initial_group_of(
                b, swiss_nodes
            ):
                valid = False  # 相邻配对产生同组对，跨组规则下无法确定
                break
            if any(opp == b for opp, _, _, _ in completed[a]):
                valid = False  # 已对阵过，官方会重排，无法确定
                break
        if not valid:
            continue
        for a, b in pairs:
            result[a] = abbr(b)
            result[b] = abbr(a)
    return result


def build_swiss_standings(data):
    """从 GetLeagueData 响应中汇总瑞士轮积分排名的结构化数据。

    返回 dict:
        rows: list of dict — 每行代表一支战队，字段有 rank/tid/name/abbr/wins/losses/
              gw/gl/logo_url/rounds (list of round_info or None)
        display_rounds: int — 需要展示的轮次数（至少 5）
    """
    node_groups = data.get("node_groups", [])
    swiss = find_swiss_group(node_groups)
    if not swiss:
        return {"rows": [], "display_rounds": 5}

    team_info = build_full_team_info(node_groups)
    swiss_nodes = swiss.get("nodes", [])
    team_games, team_rounds, total_rounds = compute_team_game_record(swiss_nodes, team_info)

    # 官方 API 尚未公布下一轮对阵时，按官方规则推算完全确定的组内/跨组配对并填入。
    # 若官方已排定下一轮对阵（该队存在未开赛对局），下一轮对手已由官方确定，跳过预测。
    predicted = predict_swiss_next_round(swiss_nodes, team_info)
    if predicted:
        for tid, opp_abbr in predicted.items():
            rounds = team_rounds.setdefault(tid, {})
            # 该队已有官方排定的未开赛对局 => 下一轮对手已确定，跳过预测
            if any(not completed for (_, _, _, completed, _) in rounds.values()):
                continue
            # 填入该队自身的下一轮（而非全局 total_rounds+1，兼容 A/B 组分批公布）
            next_round = (max(rounds) if rounds else 0) + 1
            if next_round not in rounds:
                rounds[next_round] = (opp_abbr, 0, 0, False, False)

    def sort_key(item):
        """严格按 TI 官方小组赛排名规则排序（7 级 tiebreak）。

        1. 获胜的系列赛场数（降序）
        2. 失败的系列赛场数（升序）
        3. 对手获胜的系列赛场数（降序）
        4. 比赛的胜率（降序）
        5. 对手的平均胜率（降序）
        6. 平均比赛时长（升序，越短越好）
        7. 掷硬币（升序，用于最终区分）
        """
        tid, meta = item
        empty = 0 if (tid != 0 and meta.get("name") and meta.get("abbr")) else 1
        wins = int(meta.get("wins", 0) or 0)
        losses = int(meta.get("losses", 0) or 0)
        omw = int(meta.get("tiebreak_opponent_match_wins", 0) or 0)
        gwp = float(meta.get("tiebreak_game_win_pct", 0) or 0)
        ogwp = float(meta.get("tiebreak_opponent_game_win_pct", 0) or 0)
        agl = int(meta.get("tiebreak_average_game_length", 0) or 0)
        coin = int(meta.get("tiebreak_coinflip", 0) or 0)
        return (
            empty,
            -wins,  # 规则 1
            losses,  # 规则 2
            -omw,  # 规则 3
            -gwp,  # 规则 4
            -ogwp,  # 规则 5
            agl,  # 规则 6
            coin,  # 规则 7
            tid,  # 稳定兜底
        )

    sorted_items = sorted(team_info.items(), key=sort_key)
    filtered = [(tid, m) for tid, m in sorted_items if tid != 0 and m.get("name") and m.get("abbr")]

    # 按 W/L 同分并列赋 rank
    last_rank = 0
    last_ml = None
    display_rounds = max(total_rounds, 5)
    rows = []
    for idx, (tid, meta) in enumerate(filtered, start=1):
        wins = int(meta.get("wins", 0) or 0)
        losses = int(meta.get("losses", 0) or 0)
        ml = (wins, losses)
        if ml != last_ml:
            last_ml = ml
            last_rank = idx
        gw, gl = team_games.get(tid, (0, 0))
        rounds_list = []
        for r in range(1, display_rounds + 1):
            rd = team_rounds.get(tid, {}).get(r)
            if rd is None:
                rounds_list.append(None)
            else:
                opp, tw, ow, completed, started = rd
                rounds_list.append(
                    {
                        "opp": opp,
                        "tw": tw,
                        "ow": ow,
                        "completed": completed,
                        "started": started,
                    }
                )
        rows.append(
            {
                "rank": last_rank,
                "tid": tid,
                "name": meta.get("name", ""),
                "abbr": meta.get("abbr", ""),
                "wins": wins,
                "losses": losses,
                "gw": gw,
                "gl": gl,
                "logo_url": meta.get("logo"),
                "rounds": rounds_list,
            }
        )
    return {"rows": rows, "display_rounds": display_rounds}


# ==================== 瑞士轮积分战报图片生成 ====================


def _norm_team_name(name):
    """归一化战队名用于匹配：转小写、去掉非字母数字字符。"""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def fetch_liquipedia_logos():
    """从 Liquipedia 页面抓取战队名(归一化) -> logo URL 的映射。

    使用 Liquipedia 的 MediaWiki API 获取页面 HTML（避免浏览器人机验证），
    解析 Standings 卡片中每支战队（.block-team）的 logo：
      · 优先 allmode（深浅底通用）变体，缺失时回退 lightmode
      · 返回 {归一化队名: 完整 logo URL}；失败返回空 dict。
      · 结果缓存到 images/logo/liquipedia_mapping.json，避免每次请求网络。
    """
    cache_path = os.path.join(_logo_cache_dir(), "liquipedia_mapping.json")
    # 优先读本地缓存（TI 参赛队伍固定，映射相对稳定）
    try:
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass

    try:
        url = _cfg.LIQUIPEDIA_API_URL
        headers = dict(HEADERS)
        headers["Accept-Encoding"] = "gzip"
        req = urllib.request.Request(url, headers=headers)
        with _urlopen_with_retry(req, timeout=10) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            payload = json.loads(raw.decode("utf-8"))
        text = payload.get("parse", {}).get("text", {})
        if isinstance(text, dict):
            text = text.get("*", "")
        m = re.search(r'<div[^>]*class="[^"]*standings-swiss[^"]*".*?</table>', text, re.S)
        if not m:
            return {}
        card = m.group(0)
        logos = {}
        # MediaWiki 会把 CSS 类里的 "__" 转义成 "&#95;&#95;"
        for r in re.findall(
            r'<tr[^>]*class="[^"]*table2&#95;&#95;row--body[^"]*".*?</tr>', card, re.S
        ):
            nm = re.search(r'<span class="name hidden-xs"[^>]*><a[^>]*>([^<]+)</a>', r)
            if not nm:
                continue
            name = nm.group(1).strip()
            block = re.search(r'<div class="block-team">.*?</div>', r, re.S)
            if not block:
                continue
            img = (
                re.search(r'<img[^>]*src="([^"]*allmode[^"]*)"', block.group(0))
                or re.search(r'<img[^>]*src="([^"]*lightmode[^"]*)"', block.group(0))
                or re.search(r'<img[^>]*src="([^"]*\.png[^"]*)"', block.group(0))
            )
            if not img:
                continue
            logos[_norm_team_name(name)] = _cfg.LIQUIPEDIA_CDN + img.group(1)
        # 保存缓存，下次直接读，省去网络请求
        try:
            os.makedirs(_logo_cache_dir(), exist_ok=True)
            dumpjson(logos, cache_path)
        except Exception:
            pass
        return logos
    except Exception:
        return {}


def _logo_cache_dir():
    """战队 logo 本地缓存目录（images/logo）。"""
    return _cfg.IMAGES_DIR / "logo"


def _league_data_cache_path():
    """联赛数据本地缓存文件路径（data/dota2_ti.json）。"""
    return _cfg.DATA_DIR / "dota2_ti.json"


def _save_league_data_cache(data):
    """将联赛数据保存到本地缓存 data/dota2_ti.json。失败静默忽略。"""
    try:
        path = _league_data_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        dumpjson(data, path)
    except Exception:
        pass


def _load_league_data_cache():
    """从本地缓存 data/dota2_ti.json 读取联赛数据。失败返回 None。"""
    return load_cache(_league_data_cache_path())


async def _fetch_and_cache_league_data():
    """从官方 API 抓取联赛数据并写入本地缓存。"""
    data = await asyncio.to_thread(fetch_league_data)
    _save_league_data_cache(data)
    return data


async def _league_data_cached_or_fetch():
    """读取联赛数据缓存；无缓存时回退官方 API 抓取并保存；失败返回 None。"""
    try:
        return await cache_with_fallback(
            _league_data_cache_path(),
            _fetch_and_cache_league_data,
            max_age=None,  # TI 赛事情报为永久缓存，读到即用
            fallback=False,
        )
    except Exception as exc:
        print(f"[!] 无本地缓存且获取联赛数据失败: {exc}", file=sys.stderr)
        return None


# ==================== XXH64（纯 Python 实现，无第三方依赖） ====================
# 用于对联赛数据文件计算确定性摘要，作为战报图片文件名的后缀，
# 便于"同一份数据只生成一次图片"的幂等缓存。
_XXH64_P1 = 0x9E3779B185EBCA87
_XXH64_P2 = 0xC2B2AE3D27D4EB4F
_XXH64_P3 = 0x165667B19E3779F9
_XXH64_P4 = 0x85EBCA77C2B2AE63
_XXH64_P5 = 0x27D4EB2F165667C5
_XXH64_MASK = 0xFFFFFFFFFFFFFFFF


def _xxh64_rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & _XXH64_MASK


def _xxh64_round(acc, inp):
    acc = (acc + inp * _XXH64_P2) & _XXH64_MASK
    acc = _xxh64_rotl(acc, 31)
    acc = (acc * _XXH64_P1) & _XXH64_MASK
    return acc


def _xxh64_merge_round(acc, val):
    val = _xxh64_round(0, val)
    acc ^= val
    acc = (acc * _XXH64_P1 + _XXH64_P4) & _XXH64_MASK
    return acc


def xxh64(data, seed=0):
    """计算 bytes 数据的 XXH64 摘要，返回 64 位无符号整数。

    与 C 库 xxhash 的 xxh64() 算法一致（已通过权威实现交叉验证）。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    n = len(data)
    p = 0
    if n >= 32:
        v1 = (seed + _XXH64_P1 + _XXH64_P2) & _XXH64_MASK
        v2 = (seed + _XXH64_P2) & _XXH64_MASK
        v3 = seed & _XXH64_MASK
        v4 = (seed - _XXH64_P1) & _XXH64_MASK
        limit = n - 32
        while p <= limit:
            v1 = _xxh64_round(v1, int.from_bytes(data[p : p + 8], "little"))
            v2 = _xxh64_round(v2, int.from_bytes(data[p + 8 : p + 16], "little"))
            v3 = _xxh64_round(v3, int.from_bytes(data[p + 16 : p + 24], "little"))
            v4 = _xxh64_round(v4, int.from_bytes(data[p + 24 : p + 32], "little"))
            p += 32
        h = (
            _xxh64_rotl(v1, 1) + _xxh64_rotl(v2, 7) + _xxh64_rotl(v3, 12) + _xxh64_rotl(v4, 18)
        ) & _XXH64_MASK
        h = _xxh64_merge_round(h, v1)
        h = _xxh64_merge_round(h, v2)
        h = _xxh64_merge_round(h, v3)
        h = _xxh64_merge_round(h, v4)
    else:
        h = (seed + _XXH64_P5) & _XXH64_MASK
    h = (h + n) & _XXH64_MASK
    while p + 8 <= n:
        k1 = _xxh64_round(0, int.from_bytes(data[p : p + 8], "little"))
        h ^= k1
        h = (_xxh64_rotl(h, 27) * _XXH64_P1 + _XXH64_P4) & _XXH64_MASK
        p += 8
    if p + 4 <= n:
        h ^= (int.from_bytes(data[p : p + 4], "little") * _XXH64_P1) & _XXH64_MASK
        h = (_xxh64_rotl(h, 23) * _XXH64_P2 + _XXH64_P3) & _XXH64_MASK
        p += 4
    while p < n:
        h ^= (data[p] * _XXH64_P5) & _XXH64_MASK
        h = (_xxh64_rotl(h, 11) * _XXH64_P1) & _XXH64_MASK
        p += 1
    h ^= h >> 33
    h = (h * _XXH64_P2) & _XXH64_MASK
    h ^= h >> 29
    h = (h * _XXH64_P3) & _XXH64_MASK
    h ^= h >> 32
    return h & _XXH64_MASK


def _league_data_snapshot(data):
    """从联赛数据中提取影响战报渲染的关键字段，忽略易变/无关字段。

    保留的字段（对阵、比分、状态、排名、关键时间）：
      · 队伍：team_id / name / abbr / tag
      · 积分：wins / losses / standing / score / tiebreak_*
      · 系列：team_id_1/2、team_1/2_wins、has_started / is_completed、
              scheduled_time / actual_time、series_id、node 树结构与晋级关系
      · 单局：match_id、winning_team_id

    忽略的易变字段：info.most_recent_activity、prize_pool（奖金池实时增长）、
    vods / stream_ids / streams（录像与直播）、team_logo_url（CDN 链接）、
    admins / registered_players、series_infos 等。
    返回可被 json.dumps(sort_keys=True) 确定性序列化的结构。
    """

    def node_snap(n):
        return {
            "node_id": n.get("node_id"),
            "name": n.get("name"),
            "team_id_1": n.get("team_id_1"),
            "team_id_2": n.get("team_id_2"),
            "team_1_wins": n.get("team_1_wins"),
            "team_2_wins": n.get("team_2_wins"),
            "has_started": n.get("has_started"),
            "is_completed": n.get("is_completed"),
            "scheduled_time": n.get("scheduled_time"),
            "actual_time": n.get("actual_time"),
            "series_id": n.get("series_id"),
            "winning_node_id": n.get("winning_node_id"),
            "losing_node_id": n.get("losing_node_id"),
            "incoming_node_id_1": n.get("incoming_node_id_1"),
            "incoming_node_id_2": n.get("incoming_node_id_2"),
            "matches": [
                {"match_id": m.get("match_id"), "winning_team_id": m.get("winning_team_id")}
                for m in (n.get("matches") or [])
                if isinstance(m, dict)
            ],
        }

    def team_standing_snap(ts):
        return {
            "team_id": ts.get("team_id"),
            "team_name": ts.get("team_name"),
            "team_abbreviation": ts.get("team_abbreviation"),
            "team_tag": ts.get("team_tag"),
            "wins": ts.get("wins"),
            "losses": ts.get("losses"),
            "standing": ts.get("standing"),
            "score": ts.get("score"),
            "tiebreak_game_win_pct": ts.get("tiebreak_game_win_pct"),
            "tiebreak_opponent_match_wins": ts.get("tiebreak_opponent_match_wins"),
            "tiebreak_opponent_game_win_pct": ts.get("tiebreak_opponent_game_win_pct"),
            "tiebreak_coinflip": ts.get("tiebreak_coinflip"),
            "tiebreak_average_game_length": ts.get("tiebreak_average_game_length"),
        }

    def group_snap(g):
        return {
            "name": g.get("name"),
            "node_group_id": g.get("node_group_id"),
            "is_completed": g.get("is_completed"),
            "team_standings": [
                team_standing_snap(ts)
                for ts in (g.get("team_standings") or [])
                if isinstance(ts, dict)
            ],
            "nodes": [node_snap(n) for n in (g.get("nodes") or []) if isinstance(n, dict)],
            "node_groups": [
                group_snap(ng) for ng in (g.get("node_groups") or []) if isinstance(ng, dict)
            ],
        }

    return {
        "node_groups": [
            group_snap(g) for g in (data.get("node_groups") or []) if isinstance(g, dict)
        ],
    }


def _league_data_xxh64():
    """读取本地缓存 data/dota2_ti.json，对关键字段（对阵/比分/排名等）计算 XXH64 十六进制串。

    只对 _league_data_snapshot 提取的关键字段做哈希，忽略 most_recent_activity、
    奖金池、录像等易变字段，避免数据无实质变化时重复生成战报图片。
    文件不存在或读取失败时返回 None。
    """
    try:
        path = _league_data_cache_path()
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        snapshot = json.dumps(
            _league_data_snapshot(data), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        return format(xxh64(snapshot), "016X")
    except Exception:
        return None


def _img_to_data_uri(img):
    """PIL Image -> base64 PNG data URI。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _download_logo_image(url, max_h=50):
    """下载战队 logo，返回保留透明背景、等比缩放的 PIL Image。失败返回 None。"""
    if not url:
        return None
    try:
        from PIL import Image

        raw = download_bytes(
            url, timeout=5, headers={"User-Agent": HEADERS["User-Agent"]}, retries=3
        )
        src = Image.open(io.BytesIO(raw)).convert("RGBA")
        sw, sh = src.size
        ratio = max_h / sh
        new_w = max(1, int(round(sw * ratio)))
        return src.resize((new_w, max_h), Image.LANCZOS)
    except Exception:
        return None


def _logo_data_uri_cached(tid, url):
    """获取战队 logo 的 data URI：优先读本地缓存 images/logo/{tid}.png，缺失则下载并缓存。"""
    if not tid or not url:
        return None
    cache_dir = _logo_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{tid}.png")
    if os.path.exists(cache_path):
        return image_to_data_uri(cache_path)
    img = _download_logo_image(url)
    if img is None:
        return None
    img.save(cache_path, format="PNG")
    return _img_to_data_uri(img)


async def _prepare_report(data, output_path, prefix):
    """读取本地缓存数据与 Liquipedia logo 映射，并解析输出路径。

    统一三个战报图片生成器的前置逻辑（数据读取 + logo 映射 + 幂等命名）。
    优先读取本地缓存 data/dota2_ti.json；本地无缓存时才回退到官方 API 抓取。
    logo 映射优先读取本地缓存 images/logo/liquipedia_mapping.json。
    命名基于 _league_data_xxh64 对"关键字段（对阵/比分/排名）"的摘要，
    易变字段（活跃时间戳、奖金池、录像等）变化不会导致重新生成图片。
    返回 (data, lp_logos, output_path, cached)：
      · data 为 None 表示无可用缓存（已打印错误，调用方应直接返回 None）；
      · cached 为 True 表示该数据的图片已生成过，output_path 可直接复用返回。
    """
    auto_named = output_path is None

    if data is None:
        data = await _league_data_cached_or_fetch()
        if not data:
            return None, {}, None, False
        lp_logos = await asyncio.to_thread(fetch_liquipedia_logos)
    else:
        _save_league_data_cache(data)
        lp_logos = await asyncio.to_thread(fetch_liquipedia_logos)

    if auto_named:
        digest = _league_data_xxh64()
        out_dir = _cfg.OUTPUT_DIR
        os.makedirs(out_dir, exist_ok=True)
        if digest:
            output_path = os.path.join(out_dir, f"{prefix}_{digest}.png")
            if os.path.exists(output_path):
                print(f"[*] 该数据的图片已生成过，直接返回: {output_path}")
                return data, lp_logos, output_path, True
        else:
            stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M")
            output_path = os.path.join(out_dir, f"{prefix}_{stamp}.png")

    return data, lp_logos, output_path, False


def _build_logo_uris(tids, team_info, lp_logos):
    """并行下载并缓存战队 logo，返回 {tid: data URI}。

    优先 Liquipedia 映射，回退 Valve API 提供的 logo URL。
    """
    from concurrent.futures import ThreadPoolExecutor

    def logo_uri(tid):
        if not tid:
            return None
        tid = int(tid)
        meta = team_info.get(tid, {})
        url = None
        for key in (meta.get("name"), meta.get("abbr")):
            if key:
                url = lp_logos.get(_norm_team_name(key))
                if url:
                    break
        if not url:
            url = meta.get("logo")
        return _logo_data_uri_cached(tid, url)

    unique = list(dict.fromkeys(int(t) for t in tids if t))
    with ThreadPoolExecutor(max_workers=8) as pool:
        uris = pool.map(logo_uri, unique)
    return dict(zip(unique, uris))


async def _screenshot_html(html, output_path, width, height):
    """渲染 HTML 到浏览器并截图保存，返回 output_path。"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    browser = await shared_browser.get_browser()
    page = await browser.new_page(
        viewport={"width": width, "height": height},
        device_scale_factor=1,
    )
    await page.set_content(html, wait_until="domcontentloaded")
    await page.wait_for_timeout(100)
    card = await page.query_selector(".wrap")
    await card.screenshot(path=output_path)
    await page.close()
    return output_path


async def generate_swiss_standings_image(output_path=None, data=None):
    """独立生成瑞士轮积分战报图片 —— 严格模仿 Liquipedia 样式（异步）。

    内部自动获取 TI2026 联赛数据，无需外部传入 data（也可通过 data 参数复用已获取的数据）。
    output_path 可选，省略时自动保存到 output/ti2026_swiss_YYYYMMDD_HHMM.png。
    返回生成图片的绝对路径；失败返回 None。

    用法:
        path = await generate_swiss_standings_image()

    参考: https://liquipedia.net/dota2/The_International/2026/Group_Stage#Standings
    关键样式要素：
      · 卡片：近白底 #FDFCFF + 1px rgba(0,0,0,0.12) 边框 + 8px 圆角
      · 字体："Open Sans", sans-serif, 15px
      · 表头：rgba(0,0,0,0.04) 浅灰底，深灰加粗字，padding 8px 12px
      · 行高 65px，分隔线用 inset box-shadow 1px rgba(0,0,0,0.08)
      · 左侧 4px 状态色条：up 绿 rgb(0,138,0) / stay 黄褐 rgb(150,111,0) / down 红 rgb(208,38,38)
      · 排名：20x20 圆角小方块，rgba(0,0,0,0.04) 底，11px 加粗
      · 队伍列：透明背景 logo(高25px) + 蓝色队名 rgb(13,97,164)
      · Matches 列加粗
      · Round 单元格：垂直 flex（比分标签在上 + 对手 logo 在下）
        - 比分标签：grid 三段式，padding 4px，圆角 4px，font-size 11px
        - 胜：浅绿底 rgb(214,245,214) + 深绿字 rgb(20,82,20)，我方分加粗
        - 负：浅红底 rgb(253,235,235) + 红字 rgb(184,20,20)
        - 平/LIVE：浅灰底 rgba(0,0,0,0.08) + 灰字 rgb(112,113,118)
        - 未开赛：空单元格
    """
    data, lp_logos, output_path, cached = await _prepare_report(data, output_path, "ti2026_swiss")
    if not data:
        return None
    if cached:
        return output_path

    summary = build_swiss_standings(data)
    rows = summary["rows"]
    display_rounds = summary["display_rounds"]
    if not rows:
        print("[!] 暂无可绘制的瑞士轮数据。")
        return None

    # 预加载 logo：team_info 由已获取的数据构建，Liquipedia 映射已并行就绪
    abbr_to_tid = {row["abbr"]: row["tid"] for row in rows}
    team_info = build_full_team_info(data.get("node_groups", []) or [])
    if lp_logos:
        print(f"[*] 已从 Liquipedia 获取 {len(lp_logos)} 个战队 logo")

    tids = [row["tid"] for row in rows]
    for row in rows:
        for rd in row["rounds"]:
            if rd:
                opp_tid = abbr_to_tid.get(rd["opp"])
                if opp_tid:
                    tids.append(opp_tid)
    logo_uris = await asyncio.to_thread(_build_logo_uris, tids, team_info, lp_logos)

    def logo_tag(tid, cls, fallback_abbr):
        """生成 logo HTML：有图用 <img>（透明背景），无图回退缩写文字。"""
        uri = logo_uris.get(tid)
        if uri:
            return f'<img class="{cls}" src="{uri}" alt="">'
        # 回退：显示缩写文字
        short = (fallback_abbr or "?")[:4]
        return f'<span class="{cls} logo-text">{short}</span>'

    def status_cls(idx, total):
        """按排名分区：前三名 up(绿)、倒三名 down(红)、其余 stay(黄)。"""
        if idx <= 3:
            return "status-up"
        if idx > total - 3:
            return "status-down"
        return "status-stay"

    def _round_cell(rd):
        """渲染单个 Round 单元格：空 / 已结束 / LIVE / 未开赛。"""
        if rd is None:
            return '<td class="td-round"></td>'
        opp, tw, ow = rd["opp"], rd["tw"], rd["ow"]
        opp_logo = logo_tag(abbr_to_tid.get(opp), "opp-img", opp)
        if not rd["completed"] and not rd["started"]:
            # 尚未开赛但已确定对手：仅显示对手 logo（即将对阵）
            return (
                f'<td class="td-round"><div class="match-overview upcoming">{opp_logo}</div></td>'
            )
        if rd["completed"]:
            if tw > ow:
                label_cls = "result-win"
                score_html = f"<b>{tw}</b><span>:</span><span>{ow}</span>"
            else:
                label_cls = "result-loss"
                score_html = f"<span>{tw}</span><span>:</span><b>{ow}</b>"
        else:
            # LIVE：灰色标签 + 当前比分
            label_cls = "result-default"
            score_html = f"<span>{tw}</span><span>:</span><span>{ow}</span>"
        return (
            '<td class="td-round"><div class="match-overview">'
            f'<div class="score-label {label_cls}">{score_html}</div>{opp_logo}</div></td>'
        )

    # 构建行 HTML
    total_rows = len(rows)
    rows_html = []
    for row_idx, row in enumerate(rows, start=1):
        wins, losses = row["wins"], row["losses"]
        s_cls = status_cls(row_idx, total_rows)
        matches = f"{wins} - {losses}"
        if wins + losses == 0 and row["gw"] + row["gl"] == 0:
            games = "0 - 0"
        else:
            games = f"{row['gw']} - {row['gl']}"

        cells = [_round_cell(rd) for rd in row["rounds"]]

        team_logo = logo_tag(row["tid"], "team-img", row["abbr"])
        rows_html.append(
            f'<tr class="body-row {s_cls}">'
            f'<td class="td-rank"><div class="rank-badge">{row["rank"]}</div></td>'
            f'<td class="td-team"><div class="block-team">'
            f'<span class="team-logo-wrap">{team_logo}</span>'
            f'<span class="team-name">{row["name"]}</span></div></td>'
            f'<td class="td-matches">{matches}</td>'
            f'<td class="td-games">{games}</td>'
            f"{''.join(cells)}"
            f"</tr>"
        )

    round_headers = "".join(
        f'<th class="th-center">Round {i}</th>' for i in range(1, display_rounds + 1)
    )

    css = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #f5f5f5;
  font-family: "Open Sans", "Microsoft YaHei", sans-serif;
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
}
.wrap { display: inline-block; padding: 16px; background: #f5f5f5; }
.card {
  background: #fdfcff;
  border: 1px solid rgba(0,0,0,0.12);
  border-radius: 8px;
  overflow: hidden;
}
table {
  border-collapse: separate;
  border-spacing: 0;
  width: 100%;
}
/* 表头 */
.head-row th {
  background: rgba(0,0,0,0.04);
  color: rgb(64,64,64);
  font-weight: 700;
  font-size: 15px;
  padding: 8px 12px;
  text-align: left;
  white-space: nowrap;
  height: 38px;
}
.th-center { text-align: center !important; }
/* 数据行 */
.body-row { position: relative; height: 65px; }
.body-row td {
  padding: 8px 12px;
  color: rgb(40,40,40);
  font-size: 15px;
  vertical-align: middle;
  box-shadow: rgba(0,0,0,0.08) 0px -1px 0px 0px inset;
}
/* 左侧色条 */
.body-row::after {
  content: "";
  position: absolute;
  left: 0; top: 0;
  width: 4px; height: 100%;
}
.status-up::after { background: rgb(0,138,0); }
.status-stay::after { background: rgb(150,111,0); }
.status-down::after { background: rgb(208,38,38); }
/* 排名 */
.td-rank { width: 44px; text-align: center; }
.rank-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px; height: 20px;
  background: rgba(0,0,0,0.04);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
  color: rgb(64,64,64);
}
/* 队伍列 */
.td-team { min-width: 200px; }
.block-team {
  display: flex;
  align-items: center;   /* logo 与队名在 Y 轴垂直居中 */
  height: 49px;          /* 行高 65px - 上下 padding 16px = 49px，占满内容区保证垂直居中一致 */
}
.team-logo-wrap {
  width: 60px;           /* 固定宽度：logo 水平居中，队名从固定位置左对齐 */
  height: 25px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.team-img {
  height: 25px;          /* 固定高度，所有行 logo 垂直对齐 */
  width: auto;
  max-width: 56px;
  object-fit: contain;
  display: block;
}
.logo-text.team-img { font-size: 11px; font-weight: 700; color: rgb(64,64,64); line-height: 25px; }
.team-name {
  color: rgb(13,97,164);
  font-size: 15px;
  font-weight: 400;
  white-space: nowrap;
  line-height: 25px;     /* 与 logo 同高，保证 Y 轴居中 */
  display: block;
}
/* Matches / Games */
.td-matches { text-align: center; font-weight: 700; white-space: nowrap; }
.td-games { text-align: center; white-space: nowrap; }
/* Round 单元格 */
.td-round { text-align: center; min-width: 70px; }
.match-overview {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
}
.score-label {
  display: grid;
  grid-template-columns: auto auto auto;
  justify-content: center;
  align-items: center;
  padding: 4px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 16px;
  white-space: nowrap;
}
.result-win { background: rgb(214,245,214); color: rgb(20,82,20); }
.result-loss { background: rgb(253,235,235); color: rgb(184,20,20); }
.result-default { background: rgba(0,0,0,0.08); color: rgb(112,113,118); }
.opp-img {
  height: 25px;          /* 固定高度，保证各 Round 格内对手 logo 垂直对齐 */
  width: auto;
  object-fit: contain;
  display: block;
}
.logo-text.opp-img { font-size: 9px; font-weight: 700; color: rgb(112,113,118); line-height: 25px; }
"""

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><style>
{css}
</style></head>
<body>
<div class="wrap">
<div class="card">
<table>
<thead>
<tr class="head-row">
<th>#</th>
<th>Participant</th>
<th class="th-center">Matches</th>
<th class="th-center">Games</th>
{round_headers}
</tr>
</thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
</div>
</div>
</body></html>"""

    return await _screenshot_html(html, output_path, 1200, 900)


# ==================== 国际邀请赛正赛对阵图战报图片生成 ====================
# 布局严格参考 Liquipedia div 版对阵图（Brackets.less + 8U4L2DSL1D 模板）：
#   https://liquipedia.net/dota2/The_International/2026/Main_Event

# Liquipedia bracket CSS 变量（取自页面 --match-width 等）
_BRK_MATCH_W = 180  # --match-width：比赛盒宽度（含 22px 比分列）
_BRK_OPP_H = 24  # --opponent-height：单支队伍行高
_BRK_MATCH_H = _BRK_OPP_H * 2 + 2  # 比赛盒总高（50px，含上下边框）
_BRK_ROUND_GAP = 20  # --round-horizontal-margin：轮次列水平间距
# 同轮相邻比赛的纵向节距：盒子下方留 28px 空隙，容纳未开始比赛的开赛时间行(14px)并保留呼吸空间
_BRK_PITCH = _BRK_MATCH_H + 28
_BRK_HEADER_H = 25  # 轮次表头高度
# Liquipedia 流式布局实测（TI2025 页面 inline style + 权威渲染测量）：
#   round-header margin: 8px 0 2px；UB round-lower margin-top: 0；LB round-lower margin-top: 12px
#   config.matchMargin = opponentHeight/4 = 6px（每个比赛盒的上下外边距）
_BRK_HEADER_TOP = 8  # round-header 上边距
_BRK_HEADER_GAP = 2  # round-header 下边距
_BRK_MATCH_MARGIN = _BRK_OPP_H // 4  # 比赛盒上下外边距（matchMargin = 6）
_BRK_LB_LOWER_MARGIN = 12  # LB round-lower 顶部间距
# 胜者组比赛区顶部 = 8(表头) + 25(表头高) + 2(表头底边距) + 6(比赛上边距) = 41
_BRK_BODY_TOP = _BRK_HEADER_TOP + _BRK_HEADER_H + _BRK_HEADER_GAP + _BRK_MATCH_MARGIN
# 胜者组底 → 败者组比赛区顶 = 6 + 8 + 25 + 2 + 12 + 6 + 额外 10 = 69。
# 额外间距为胜者组底部未开始比赛的开赛时间行(14px)留足空隙，
# 避免其顶到下方败者组表头/第 1 轮比赛。
_BRK_REGION_EXTRA = 10
_BRK_REGION_GAP = (
    _BRK_MATCH_MARGIN
    + _BRK_HEADER_TOP
    + _BRK_HEADER_H
    + _BRK_HEADER_GAP
    + _BRK_LB_LOWER_MARGIN
    + _BRK_MATCH_MARGIN
    + _BRK_REGION_EXTRA
)


def _round_name(rounds_count, idx):
    """按 Liquipedia 惯例命名轮次（中文）：末轮决赛，倒数第二半决赛，倒数第三四分之一决赛。"""
    from_end = rounds_count - idx
    if from_end == 1:
        return "决赛"
    if from_end == 2:
        return "半决赛"
    if from_end == 3:
        return "四分之一决赛"
    return f"第{idx + 1}轮"


def build_bracket_model(node_groups):
    """从正赛分组节点图推导双败对阵图的几何布局。

    推导过程（不硬编码节点 ID，兼容任意规模的双败/单败结构）：
      1. 胜者组(UB)/败者组(LB)/决赛(GF) 划分：败者边可达的节点属于 LB，
         无出边的节点为 GF（决赛），其余为 UB。
      2. 列号：LB 节点列 = max(UB 馈送列, LB 馈送列+1)；UB 节点列按轮次递增，
         但 UB 末轮与 LB 末轮同列（败者组决赛上方）。
      3. 纵向：UB 首轮自上而下等距排布，后续轮取两个馈送者中心的均值；
         LB 在独立区域同法推导（仅用 LB 馈送者定位）；GF 垂直居中。

    返回 dict（matches/headers/lines/width/height），无正赛分组或结构不完整时返回 None。
    """
    playoff = find_node_group(node_groups, r"playoff|main.?event")
    if not playoff:
        return None
    nodes = {}
    for n in playoff.get("nodes") or []:
        if isinstance(n, dict) and n.get("node_id"):
            nodes[int(n["node_id"])] = n
    if len(nodes) < 2:
        return None

    win_t, lose_t = {}, {}
    for nid, n in nodes.items():
        win_id, lose_id = n.get("winning_node_id") or 0, n.get("losing_node_id") or 0
        if win_id in nodes:
            win_t[nid] = win_id
        if lose_id in nodes:
            lose_t[nid] = lose_id

    # LB 可达集：沿败者边出发、跟随所有边扩散；无出边的节点（决赛）不算 LB
    lb_ids = set()
    stack = list(lose_t.values())
    while stack:
        nid = stack.pop()
        if nid in lb_ids or nid not in nodes:
            continue
        if nid not in win_t and nid not in lose_t:
            continue
        lb_ids.add(nid)
        for t in (win_t.get(nid), lose_t.get(nid)):
            if t is not None:
                stack.append(t)

    gf_ids = {nid for nid in nodes if nid not in win_t and nid not in lose_t}
    ub_ids = set(nodes) - lb_ids - gf_ids

    incoming = {
        nid: [int(n.get("incoming_node_id_1") or 0), int(n.get("incoming_node_id_2") or 0)]
        for nid, n in nodes.items()
    }

    # ---- 列号推导（memo 化 DFS，防环）----
    col = {}

    def feed_col(f, depth=0):
        """馈送者的列号（递归）。"""
        if f in col:
            return col[f]
        if depth > 64:
            return 0
        c = _calc_col(f, depth + 1)
        return c

    def _calc_col(nid, depth):
        """按组别规则推导节点列号（UB 递增，LB/GF 取馈送者列加偏移）。"""
        if nid in col:
            return col[nid]
        col[nid] = 0  # 占位防环
        best = 0
        feeders = [f for f in incoming[nid] if f in nodes]
        if nid in ub_ids:
            # UB：胜者馈送 +1（仅统计 UB 内馈送者）
            for f in feeders:
                if f in ub_ids:
                    best = max(best, feed_col(f, depth) + 1)
        elif nid in gf_ids:
            # GF：所有馈送者（胜者边）+1
            for f in feeders:
                best = max(best, feed_col(f, depth) + 1)
        else:
            # LB：LB 馈送者 +1；UB 馈送者（败者落入）同列
            for f in feeders:
                shift = 1 if f in lb_ids else 0
                best = max(best, feed_col(f, depth) + shift)
        col[nid] = best
        return best

    for nid in list(nodes):
        _calc_col(nid, 0)

    # UB 末轮与 LB 末轮同列（UB Final 对齐 LB Final 上方）
    if ub_ids:
        ub_last_raw = max(col[n] for n in ub_ids)
        lb_last = max((col[n] for n in lb_ids), default=-1)
        shift = max(lb_last, ub_last_raw) - ub_last_raw
        if shift > 0:
            # 仅移动 UB 末轮（列号最大的 UB 轮），避免破坏其余对齐
            ub_last_ids = {n for n in ub_ids if col[n] == ub_last_raw}
            for n in ub_last_ids:
                col[n] += shift
            # 依赖 UB 末轮的下游（GF）同步顺延
            for n in gf_ids:
                feeders = [f for f in incoming[n] if f in nodes]
                if any(f in ub_last_ids for f in feeders):
                    col[n] = max(col[f] + (1 if f in lb_ids else 1) for f in feeders)

    n_cols = max(col.values()) + 1 if col else 1

    # ---- 纵向布局 ----
    top = {}

    def center(nid):
        return top[nid] + _BRK_MATCH_H // 2

    def ub_rounds():
        rounds = {}
        for n in ub_ids:
            rounds.setdefault(col[n], []).append(n)
        return rounds

    # 胜者组：按列（轮次）自浅入深
    for c in sorted(ub_rounds()):
        nids = sorted(ub_rounds()[c])
        for idx, nid in enumerate(nids):
            ub_feeders = [f for f in incoming[nid] if f in ub_ids and f in top]
            if c == 0 or not ub_feeders or any(f not in top for f in ub_feeders):
                top[nid] = idx * _BRK_PITCH
            else:
                m = sum(center(f) for f in ub_feeders) / len(ub_feeders)
                top[nid] = m - _BRK_MATCH_H / 2

    ub_bottom = max((top[n] + _BRK_MATCH_H for n in ub_ids), default=0)
    y_lb = ub_bottom + _BRK_REGION_GAP

    # 败者组：按列推导，仅用 LB 馈送者定位。
    # 移植 Liquipedia alignMatchWithLowerNodes：单 LB 馈送者且对手在中间槽时，
    # 目标槽中心与馈送者盒中心等高（连线退化为直线）；多个馈送者取中间对齐。
    lb_rounds = {}
    for n in lb_ids:
        lb_rounds.setdefault(col[n], []).append(n)
    for c in sorted(lb_rounds):
        nids = sorted(lb_rounds[c])
        for idx, nid in enumerate(nids):
            lb_feeds = [
                (slot, f) for slot, f in enumerate(incoming[nid]) if f in lb_ids and f in top
            ]
            if not lb_feeds:
                top[nid] = y_lb + idx * _BRK_PITCH
            elif len(lb_feeds) == 1:
                slot, f = lb_feeds[0]
                top[nid] = center(f) - (12 if slot == 0 else 37)
            else:
                m = sum(center(f) for _s, f in lb_feeds) / len(lb_feeds)
                top[nid] = m - _BRK_MATCH_H / 2

    body_h = max((top[n] + _BRK_MATCH_H for n in nodes if n in top), default=0)

    # 决赛（GF）：垂直取两个馈送者（UBF/LBF）中心的均值，与 Liquipedia 布局一致
    for idx, nid in enumerate(sorted(gf_ids)):
        feeders = [f for f in incoming[nid] if f in top]
        if feeders:
            m = sum(center(f) for f in feeders) / len(feeders)
            top[nid] = m - _BRK_MATCH_H / 2 + idx * _BRK_PITCH
        else:
            top[nid] = body_h / 2 - _BRK_MATCH_H / 2 + idx * _BRK_PITCH
    body_h = max((top[n] + _BRK_MATCH_H for n in nodes if n in top), default=0)

    # ---- 表头 ----
    def add_bracket_headers(prefix_cn, cols_list, y):
        """输出中文表头（单档变体，180px 宽度足够放下中文全称，无需宽度适配）。"""
        cnt = len(cols_list)
        for i, c in enumerate(cols_list):
            headers.append(
                {
                    "x": c * (_BRK_MATCH_W + _BRK_ROUND_GAP),
                    "y": y,
                    "options": [f"{prefix_cn}{_round_name(cnt, i)}"],
                }
            )

    headers = []
    ub_cols = sorted({col[n] for n in ub_ids})
    if ub_cols:
        add_bracket_headers("胜者组", ub_cols, _BRK_HEADER_TOP)
    lb_cols = sorted({col[n] for n in lb_ids})
    if lb_cols:
        # 败者组表头底部到败者组比赛区顶 = 表头底边距 2 + lower 间距 12 + 比赛上边距 6 = 20
        add_bracket_headers(
            "败者组",
            lb_cols,
            _BRK_BODY_TOP
            + y_lb
            - _BRK_HEADER_H
            - _BRK_HEADER_GAP
            - _BRK_LB_LOWER_MARGIN
            - _BRK_MATCH_MARGIN,
        )
    if gf_ids:
        gc = max(col[n] for n in gf_ids)
        headers.append(
            {"x": gc * (_BRK_MATCH_W + _BRK_ROUND_GAP), "y": _BRK_HEADER_TOP, "options": ["总决赛"]}
        )

    # ---- 连接线（移植 Liquipedia Bracket.lua NodeLowerConnectors/NodeConnector）----
    # 官方几何（实测 TI2025 渲染页 + NodeConnector 源码）：
    #   · 晋级线（树内边 / 进决赛）：三段肘形线，画在“馈送盒右缘 → 目标盒左缘”之间。
    #       左水平段：馈送盒右缘 → joint（固定 w=10）
    #       垂直段  ：x = 馈送盒右缘 + 8（官方 jointLeft=9，线宽 2）
    #       右水平段：joint → 目标盒左缘（相邻列为 12px；跨列时跨越整列，
    #                 如 SF(列1)→UBF(列3) 为 212px，绕过中间的 LBSF）
    #     两中心等高时退化为单段水平线。
    #   · 跨区边（UB 败者落入 LB）：仅画 10px 短线头（stub），官方不画跨区长线
    #     （实测：LB QF/LB Final 的外来槽位只有 stub）。
    #   · LB 第一轮（无树内馈送）：不画任何线（官方无 connectors 容器）。
    lines = []
    GAP = _BRK_ROUND_GAP  # 20px 间隙

    def slot_center(nid, slot):
        # 目标盒槽位中心 = 盒顶 + 12/36（单行高 24，中心 12；两行各 24px，无额外边框）
        return top[nid] + (12 if slot == 0 else 36)

    def box_center(nid):
        # 馈送盒实际中线 = 盒顶 + 24（两行各 24px，渲染总高 48）
        return top[nid] + _BRK_OPP_H

    for t in nodes:
        if t not in top:
            continue
        t_left = col[t] * (_BRK_MATCH_W + GAP)  # 目标盒左缘
        tree_edges, cross_edges = [], []
        for slot, f in enumerate(incoming[t]):
            if f not in nodes or f not in top:
                continue
            in_tree = (f in ub_ids and t in ub_ids) or (f in lb_ids and t in lb_ids) or t in gf_ids
            (tree_edges if in_tree else cross_edges).append((slot, f))
        for slot, f in tree_edges:
            f_right = col[f] * (_BRK_MATCH_W + GAP) + _BRK_MATCH_W  # 馈送盒右缘
            joint = f_right + 8  # 垂直段 x（官方 jointLeft=9，线宽 2 居中）
            left_top, right_top = box_center(f), slot_center(t, slot)
            if abs(left_top - right_top) < 1:
                lines.append((f_right, left_top - 1, t_left - f_right, 2))
            else:
                lines.append((f_right, left_top - 1, 10, 2))  # 左水平
                y1, y2 = sorted((left_top, right_top))
                lines.append((joint, y1 - 1, 2, y2 - y1 + 2))  # 垂直
                lines.append((joint, right_top - 1, t_left - joint, 2))  # 右水平（跨列）
        # 跨区边（UB→LB）：stub 短线头（仅当该比赛已有树内边，即存在 connectors 容器）
        if tree_edges:
            for slot, _f in cross_edges:
                rc = slot_center(t, slot)
                lines.append((t_left - 10, rc - 1, 10, 2))

    width = n_cols * (_BRK_MATCH_W + _BRK_ROUND_GAP) - _BRK_ROUND_GAP
    height = max(body_h, y_lb - 5)

    return {
        "nodes": nodes,
        "col": col,
        "top": top,
        "headers": headers,
        "lines": lines,
        "width": width,
        "height": height,
        "y_lb": y_lb,
        "incoming": incoming,
    }


_BRK_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #f8f9fa; font-family: "Open Sans", "Microsoft YaHei", sans-serif; }
.wrap { display: inline-block; padding: 12px; background: #f8f9fa; }
.brkts-bracket {
  position: relative;
  font-size: 13px;
  background: #f8f9fa;
}
.brkts-header {
  position: absolute;
  background: #cfcfcf;
  border: 1px solid #aaaaaa;
  border-radius: 2px;
  color: #373737;
  font-size: 13px;
  height: 25px;
  line-height: 15px;
  overflow: hidden;
  padding: 4px;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
  width: 180px;
}
.brkts-header-option { display: none; }
.brkts-match {
  position: absolute;
  width: 180px;
  background: #f2f2f2;
  border: 1px solid #aaaaaa;
  border-radius: 2px;
}
.brkts-opponent-entry {
  display: flex;
  align-items: stretch;
  height: 24px;
  border-top: 1px solid #aaaaaa;
  border-bottom: 1px solid #aaaaaa;
  font-size: 11px;
  line-height: 1.55;
  position: relative;
}
.brkts-opponent-entry:first-child { margin-top: -1px; }
.brkts-opponent-entry.brkts-opponent-entry-last { margin-bottom: -1px; }
.brkts-opponent-entry-left {
  display: flex;
  flex: 1 1;
  align-items: center;
  min-width: 0;
}
.brkts-opponent-entry-left.brkts-opponent-win { font-weight: bold; }
.block-team {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1 1;
  min-width: 0;
}
.team-template-image-icon {
  display: inline-flex;
  justify-content: center;
  height: 18px;
  flex: 0 0 auto;
  width: 44px;
}
.team-template-image-icon img { max-height: 18px; max-width: 44px; }
.name {
  white-space: pre;
  text-overflow: ellipsis;
  overflow: hidden;
  color: #373737;
}
.brkts-opponent-block-literal {
  font-style: italic;
  padding-left: 3px;
  color: #696969;
}
.brkts-opponent-score-outer {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  flex: 0 0 auto;
  background: #ebebeb;
  border-left: 1px solid #aaaaaa;
}
.brkts-opponent-score-inner { flex: 1 1; text-align: center; }
.brkts-line { position: absolute; background: #aaaaaa; }
.brkts-match-time {
  position: absolute;
  text-align: center;
  font-size: 12px;
  line-height: 14px;
  height: 14px;
  color: #696969;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
"""


_BRACKET_HEADER_JS = """
document.querySelectorAll('.brkts-header-div').forEach((element) => {
  const optionsDivs = Array.from(element.querySelectorAll('.brkts-header-option'));
  if (optionsDivs.length === 0) return;
  const options = optionsDivs.map((div) => div.textContent);
  Array.from(element.childNodes).forEach((child) => {
    if (!optionsDivs.includes(child)) element.removeChild(child);
  });
  for (let i = 0; i < options.length; i++) {
    const textNode = document.createTextNode(options[i]);
    element.insertBefore(textNode, element.firstChild);
    if (element.scrollWidth <= element.clientWidth || i === options.length - 1) break;
    element.removeChild(textNode);
  }
});
"""


def _logo_img_html(tid, logo_uris):
    """生成战队 logo 的 HTML 片段（无图返回空串）。"""
    uri = logo_uris.get(tid)
    if uri:
        return f'<span class="team-template-image-icon"><img src="{uri}" alt=""></span>'
    return ""


def _bracket_opponent_html(n, slot, is_last, team_info, logo_uris):
    """渲染对阵图单侧队伍行（队名 + logo + 比分）。"""
    tid = int(n.get("team_id_1") if slot == 0 else n.get("team_id_2") or 0)
    w1, w2 = _series_score(n)
    wins = w1 if slot == 0 else w2
    opp_wins = w2 if slot == 0 else w1
    started = bool(n.get("has_started")) or wins or opp_wins
    win_cls = " brkts-opponent-win" if n.get("is_completed") and wins > opp_wins else ""
    last_cls = " brkts-opponent-entry-last" if is_last else ""
    if tid and tid in team_info:
        name = team_info[tid].get("name") or ""
        left = (
            f'<div class="block-team">{_logo_img_html(tid, logo_uris)}'
            f'<span class="name">{name}</span></div>'
        )
    else:
        # 无队伍/未排定对阵：留空，不显示 TBD 占位
        left = '<span class="brkts-opponent-block-literal"></span>'
    if started:
        score_html = f"<b>{wins}</b>" if n.get("is_completed") and wins > opp_wins else str(wins)
    else:
        score_html = ""
    return (
        f'<div class="brkts-opponent-entry{last_cls}">'
        f'<div class="brkts-opponent-entry-left{win_cls}">{left}</div>'
        f'<div class="brkts-opponent-score-outer">'
        f'<div class="brkts-opponent-score-inner">{score_html}</div></div></div>'
    )


def _render_bracket_html(model, team_info, logo_uris):
    """按 Liquipedia div 对阵图样式渲染完整 HTML。"""

    # 表头：直接挂在 .brkts-bracket 上（不被内容层偏移影响、不被比赛盒遮挡）。
    # 每个表头输出 Liquipedia 三档变体（.brkts-header-option），由内嵌 JS 按
    # 官方算法从最长到最短选取能放入宽度的变体。
    header_parts = []
    for h in model["headers"]:
        opts = h["options"]
        header_parts.append(
            f'<div class="brkts-header brkts-header-div" '
            f'style="left:{h["x"]}px;top:{h["y"]}px">'
            + opts[0]
            + "".join(f'<div class="brkts-header-option">{o}</div>' for o in opts)
            + "</div>"
        )
    # 比赛盒 + 连接线：放在表头行下方的内容层。
    # 未开始但已排定对阵的比赛，在其比赛盒下方 14px 空隙处显示开赛时间。
    body_parts = []
    for x, y, w, h in model["lines"]:
        body_parts.append(
            f'<div class="brkts-line" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"></div>'
        )
    has_times = False
    bold_date = _bold_date(model["nodes"].values())
    today_delay = _today_delay(model["nodes"].values())
    for nid, n in model["nodes"].items():
        x = model["col"][nid] * (_BRK_MATCH_W + _BRK_ROUND_GAP)
        y = model["top"][nid]
        body_parts.append(
            f'<div class="brkts-match" style="left:{x}px;top:{y}px">'
            + _bracket_opponent_html(n, 0, False, team_info, logo_uris)
            + _bracket_opponent_html(n, 1, True, team_info, logo_uris)
            + "</div>"
        )
        scheduled = _scheduled_time_text(n, bold_date, live_red=True, today_delay=today_delay)
        if scheduled:
            has_times = True
            body_parts.append(
                f'<div class="brkts-match-time" style="left:{x}px;top:{y + _BRK_MATCH_H + 2}px;width:{_BRK_MATCH_W}px">{scheduled}</div>'
            )

    # 表头变体选择 JS：移植自 Liquipedia Lua-Modules PR #7007（Bracket.js）
    header_js = _BRACKET_HEADER_JS

    bracket_h = _BRK_BODY_TOP + model["height"] + (8 + (14 if has_times else 0))

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><style>{_BRK_CSS}</style></head>
<body><div class="wrap">
<div class="brkts-bracket" style="width:{model["width"]}px;height:{bracket_h}px">
{"".join(header_parts)}
<div style="position:absolute;top:{_BRK_BODY_TOP}px;left:0;width:{model["width"]}px;height:{model["height"]}px">
{"".join(body_parts)}
</div>
</div>
<script>{header_js}</script>
</div></body></html>"""


async def generate_main_event_image(output_path=None, data=None):
    """生成国际邀请赛正赛（淘汰赛）对阵图战报图片 —— Liquipedia div 样式（异步）。

    内部自动获取 TI2026 联赛数据（也可通过 data 参数复用已获取的数据）。
    output_path 可选，省略时自动保存到 output/ti2026_main_event_YYYYMMDD_HHMM.png。
    返回生成图片的绝对路径；失败返回 None。

    参考: https://liquipedia.net/dota2/The_International/2026/Main_Event
    """
    data, lp_logos, output_path, cached = await _prepare_report(
        data, output_path, "ti2026_main_event"
    )
    if not data:
        return None
    if cached:
        return output_path

    node_groups = data.get("node_groups", []) or []
    model = build_bracket_model(node_groups)
    if not model:
        print("[!] 未找到可绘制的正赛对阵图数据。", file=sys.stderr)
        return None

    team_info = build_full_team_info(node_groups)

    tids = []
    for n in model["nodes"].values():
        for tid in (n.get("team_id_1"), n.get("team_id_2")):
            if tid:
                tids.append(tid)
    logo_uris = await asyncio.to_thread(_build_logo_uris, tids, team_info, lp_logos)

    html = _render_bracket_html(model, team_info, logo_uris)
    return await _screenshot_html(
        html,
        output_path,
        max(model["width"] + 40, 800),
        model["height"] + 120,
    )


# ==================== Elimination Round（瑞士轮晋级赛）对阵图 ====================
# 布局参考 Liquipedia Group Stage 页的 Elimination Round（div 版）：
#   https://liquipedia.net/dota2/The_International/2026/Group_Stage
# 实测几何（Liquipedia 官方 div 渲染）：两列表头 + 单列比赛 + 右侧晋级列，
#   每场比赛右缘有一条水平连接线通往对应晋级席。

# Liquipedia Elimination Round 几何常量（实测 _seg_out.txt）：
#   · 表头 y=10，高度 25
#   · 比赛列 x=2，宽 180(队名区)+22(比分区)=202，高 48（两支 24px 队伍）
#   · 晋级列 x=202，宽 180，高 24，垂直居中于比赛盒中心
#   · 连接线 x=182（队名区右缘），宽 20，高 2，位于比赛盒中线
#   · 比赛纵向节距 60px
_ELIM_PAD = 2  # bracket padding
_ELIM_HEADER_TOP = 10  # 表头顶部 y
_ELIM_HEADER_H = 25  # 表头高度
_ELIM_MATCH_X = 2  # 比赛列左缘
_ELIM_MATCH_W = 180  # 队名区宽度
_ELIM_SCORE_W = 22  # 比分区宽度
_ELIM_OPP_H = 24  # 单支队伍行高
_ELIM_MATCH_H = _ELIM_OPP_H * 2  # 比赛盒高 48
_ELIM_QUAL_W = 180  # 晋级席宽度
_ELIM_QUAL_H = 24  # 晋级席高度
_ELIM_QUAL_X = 202  # 晋级列左缘
_ELIM_PITCH = 60  # 比赛纵向节距


def build_elimination_round_model(node_groups):
    """从 Elimination Round 分组推导单列比赛 + 晋级列的两列几何布局。

    参考 Liquipedia Group Stage 页 Elimination Round（div 版）的流式布局：
      左列 = 表头 + N 场比赛；右列 = 表头 + N 个晋级席；
      每场比赛右缘有一条水平连接线通往对应晋级席（晋级席居中于比赛中线）。

    返回 dict（matches/headers/lines/qual_pos/width/height）；无分组或结构不完整返回 None。
    """
    elim = find_node_group(node_groups, r"elimination round")
    if not elim:
        return None
    matches = []
    for n in elim.get("nodes") or []:
        if isinstance(n, dict) and n.get("node_id"):
            matches.append(n)
    matches.sort(key=lambda n: int(n["node_id"]))
    if not matches:
        return None

    # 表头：比赛列 + 晋级列
    headers = [
        {"x": _ELIM_MATCH_X, "y": _ELIM_HEADER_TOP, "options": ["3-2 vs 2-3", "[3-2] vs [2-3]"]},
        {
            "x": _ELIM_QUAL_X,
            "y": _ELIM_HEADER_TOP,
            "options": ["晋级", "晋级季后赛", "To Playoffs"],
        },
    ]

    # 比赛盒 / 晋级席 / 连接线
    qual_pos = []
    lines = []
    first_top = _ELIM_HEADER_TOP + _ELIM_HEADER_H + 2 + 6  # 10+25+2+6 = 43
    for i, _n in enumerate(matches):
        top = first_top + i * _ELIM_PITCH
        center_y = top + _ELIM_MATCH_H // 2  # 比赛盒中线
        qual_pos.append(
            {
                "x": _ELIM_QUAL_X,
                "y": center_y - _ELIM_QUAL_H // 2,
                "w": _ELIM_QUAL_W,
                "h": _ELIM_QUAL_H,
            }
        )
        # 连接线：队名区右缘(182) → 晋级席左缘(202)
        lines.append(
            (
                _ELIM_MATCH_X + _ELIM_MATCH_W,
                center_y - 1,
                _ELIM_QUAL_X - (_ELIM_MATCH_X + _ELIM_MATCH_W),
                2,
            )
        )

    width = _ELIM_QUAL_X + _ELIM_QUAL_W + _ELIM_PAD  # 202+180+2 = 384
    height = first_top + len(matches) * _ELIM_PITCH - _ELIM_PITCH + _ELIM_MATCH_H + 6 + _ELIM_PAD

    return {
        "matches": matches,
        "headers": headers,
        "lines": lines,
        "qual_pos": qual_pos,
        "width": width,
        "height": height,
    }


def _elim_winner_tid(n):
    """已结束比赛的获胜方 team_id；未结束返回 None。"""
    if not n.get("is_completed"):
        return None
    w1, w2 = _series_score(n)
    if w1 > w2:
        return int(n.get("team_id_1") or 0) or None
    if w2 > w1:
        return int(n.get("team_id_2") or 0) or None
    return None


def _bold_date(nodes):
    """计算需要加粗的日期（本地时区）。

    今天的比赛未打完（存在今天且未结束的比赛）时返回今天；
    否则（今天的比赛已全部结束或今天无比赛）返回明天。
    """
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    for n in nodes:
        ts = n.get("scheduled_time")
        if not ts:
            continue
        if datetime.fromtimestamp(ts, LOCAL_TZ).date() != today:
            continue
        if not n.get("is_completed"):
            return today
    return today + timedelta(days=1)


def _today_delay(nodes):
    """今日已开赛比赛的顺延量（秒）= actual_time - scheduled_time 的最大值；无则 0。

    用于把今日尚未开始的比赛显示时间后移，避免赛程推迟后仍显示过时的开赛时间。
    """
    today = datetime.now(LOCAL_TZ).date()
    delay = 0
    for n in nodes:
        st = n.get("scheduled_time")
        at = n.get("actual_time")
        if not st or not at:
            continue
        if datetime.fromtimestamp(st, LOCAL_TZ).date() != today:
            continue
        delay = max(delay, at - st)
    return delay


def _scheduled_time_text(n, bold_date=None, live_red=False, today_delay=0):
    """返回系列赛的开赛时间文本（北京时间，如 '8/16 10:00'）。

    - 已结束（is_completed）返回 None；
    - 尚未排定对阵（team_id 为 0，如瑞士轮下一轮未公布）时，只要带时间戳
      仍会显示时间，便于观众知晓下一轮何时开赛；
    - 进行中（has_started 且未结束）：live_red=True 时返回加粗标红的 <b> 时间；
      时间取 actual_time，缺失时回退 scheduled_time；
    - 今日未开始的比赛：today_delay 大于 0 时，显示时间按顺延量后移
      （今日已有比赛 actual_time - scheduled_time 的最大值）；
    - bold_date 为需加粗的日期（date 对象），匹配时返回普通 <b> 包裹的 HTML。
    """
    ts = n.get("actual_time") or n.get("scheduled_time")
    if not ts or n.get("is_completed"):
        return None
    dt = datetime.fromtimestamp(ts, LOCAL_TZ)
    if n.get("has_started"):
        # 进行中：仅正赛开启 live_red 时显示并加粗标红，其它场景不显示时间
        if not live_red:
            return None
        return f'<b style="color:#FF4B59">{dt.month}/{dt.day} {dt:%H:%M}</b>'
    # 今日未开始的比赛：按今日已开赛比赛的顺延量后移显示时间（推测值，加 ? 标注）
    if today_delay and dt.date() == datetime.now(LOCAL_TZ).date():
        dt = datetime.fromtimestamp(ts + today_delay, LOCAL_TZ)
        text = f"{dt.month}/{dt.day} {dt:%H:%M} ?"
    else:
        text = f"{dt.month}/{dt.day} {dt:%H:%M}"
    if bold_date is not None and dt.date() == bold_date:
        return f"<b>{text}</b>"
    return text


def _render_elimination_round_html(model, team_info, logo_uris):
    """按 Liquipedia Group Stage Elimination Round（div 版）样式渲染完整 HTML。"""

    bold_date = _bold_date(model["matches"])

    def qualified_html(n, q):
        """晋级席：已结束则显示获胜队（加粗）；未开始但已排定对阵显示开赛时间；否则占位空。"""
        tid = _elim_winner_tid(n)
        if tid and tid in team_info:
            name = team_info[tid].get("name") or "TBD"
            inner = (
                f'<div class="brkts-opponent-entry brkts-opponent-entry-last" '
                f'style="height:{_ELIM_QUAL_H}px">'
                f'<div class="brkts-opponent-entry-left brkts-opponent-win">'
                f'<div class="block-team">{_logo_img_html(tid, logo_uris)}'
                f'<span class="name">{name}</span></div></div></div>'
            )
        else:
            scheduled = _scheduled_time_text(n, bold_date)
            if scheduled:
                text = f'<span class="brkts-qualified-time">{scheduled}</span>'
            else:
                text = '<span class="brkts-opponent-block-literal">&ZeroWidthSpace;</span>'
            inner = (
                f'<div class="brkts-opponent-entry brkts-opponent-entry-last" '
                f'style="height:{_ELIM_QUAL_H}px">'
                f'<div class="brkts-opponent-entry-left">{text}</div></div>'
            )
        return f'<div class="brkts-qualified" style="left:{q["x"]}px;top:{q["y"]}px;width:{q["w"]}px;height:{q["h"]}px">{inner}</div>'

    # 表头（含 Liquipedia 三档变体，由内嵌 JS 选择可放入的变体）
    header_parts = []
    for h in model["headers"]:
        opts = h["options"]
        header_parts.append(
            f'<div class="brkts-header brkts-header-div" '
            f'style="left:{h["x"]}px;top:{h["y"]}px;width:{_ELIM_MATCH_W}px">'
            + opts[0]
            + "".join(f'<div class="brkts-header-option">{o}</div>' for o in opts)
            + "</div>"
        )

    body_parts = []
    for x, y, w, h in model["lines"]:
        body_parts.append(
            f'<div class="brkts-line" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"></div>'
        )
    first_top = _ELIM_HEADER_TOP + _ELIM_HEADER_H + 2 + 6  # 43
    for i, (n, q) in enumerate(zip(model["matches"], model["qual_pos"])):
        top = first_top + i * _ELIM_PITCH
        body_parts.append(
            f'<div class="brkts-match" style="left:{_ELIM_MATCH_X}px;top:{top}px;'
            f'width:{_ELIM_MATCH_W}px">'
            + _bracket_opponent_html(n, 0, False, team_info, logo_uris)
            + _bracket_opponent_html(n, 1, True, team_info, logo_uris)
            + "</div>"
        )
        body_parts.append(qualified_html(n, q))

    header_js = _BRACKET_HEADER_JS

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><style>{_BRK_CSS}
.brkts-qualified {{
  position: absolute;
  background: #f2f2f2;
  border: 1px solid #aaaaaa;
  border-radius: 2px;
  font-weight: bold;
}}
.brkts-qualified-time {{
  width: 100%;
  text-align: center;
  font-style: normal;
  font-weight: normal;
  font-size: 12px;
  color: #696969;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 4px;
}}
</style></head>
<body><div class="wrap">
<div class="brkts-bracket" style="width:{model["width"]}px;height:{model["height"]}px">
{"".join(header_parts)}
<div style="position:absolute;top:0px;left:0;width:{model["width"]}px;height:{model["height"]}px">
{"".join(body_parts)}
</div>
</div>
<script>{header_js}</script>
</div></body></html>"""


async def generate_elimination_round_image(output_path=None, data=None):
    """生成 Elimination Round（瑞士轮晋级赛）对阵图战报图片 —— Liquipedia div 样式（异步）。

    内部自动获取 TI2026 联赛数据（也可通过 data 参数复用已获取的数据）。
    output_path 可选，省略时自动保存到 output/ti2026_elimination_round_YYYYMMDD_HHMM.png。
    返回生成图片的绝对路径；失败返回 None。

    参考: https://liquipedia.net/dota2/The_International/2026/Group_Stage（Elimination Round）
    """
    data, lp_logos, output_path, cached = await _prepare_report(
        data, output_path, "ti2026_elimination_round"
    )
    if not data:
        return None
    if cached:
        return output_path

    node_groups = data.get("node_groups", []) or []
    model = build_elimination_round_model(node_groups)
    if not model:
        print("[!] 未找到可绘制的 Elimination Round 数据。", file=sys.stderr)
        return None

    team_info = build_full_team_info(node_groups)

    tids = []
    for n in model["matches"]:
        for tid in (n.get("team_id_1"), n.get("team_id_2")):
            if tid:
                tids.append(tid)
    logo_uris = await asyncio.to_thread(_build_logo_uris, tids, team_info, lp_logos)

    html = _render_elimination_round_html(model, team_info, logo_uris)
    return await _screenshot_html(
        html,
        output_path,
        max(model["width"] + 40, 800),
        model["height"] + 120,
    )


# 阶段名 -> (生成器函数, 阶段中文名)；用于指定阶段与自动判断的统一分派
_STAGE_GENERATORS = {
    "swiss": (generate_swiss_standings_image, "小组赛（瑞士轮）"),
    "elimination_round": (generate_elimination_round_image, "瑞士轮晋级赛（Elimination Round）"),
    "main_event": (generate_main_event_image, "国际邀请赛正赛"),
}


async def generate_league_report_image(output_path=None, stage=None):
    """战报图片统一入口：按指定阶段生成对应图片；未指定时自动判断当前赛程阶段（异步）。

      · stage=None/'auto'  自动判断当前阶段（最新阶段）
      · stage='swiss'      小组赛（瑞士轮）
      · stage='elimination_round'  瑞士轮晋级赛（Elimination Round）
      · stage='main_event' 国际邀请赛正赛

    数据优先从本地缓存 data/dota2_ti.json 读取；本地无缓存时才回退到官方 API 抓取。
    返回生成图片的绝对路径；失败返回 None。
    """
    data = await _league_data_cached_or_fetch()
    if not data:
        return None

    explicit = stage in _STAGE_GENERATORS
    resolved = stage if explicit else detect_league_stage(data.get("node_groups") or [])
    generator, label = _STAGE_GENERATORS[resolved]
    print(f"[*] {'指定阶段' if explicit else '当前赛程'}：{label}，生成战报")
    return await generator(output_path, data=data)


def main():
    """抓取官方数据并按结束/进行中/未开始分类打印全部赛果（含 Steam 实时单局详情）。"""
    print(f"[*] 正在从官方 API 抓取 TI2026 联赛数据 (league_id={LEAGUE_ID}) ...")
    try:
        data = fetch_league_data()
    except Exception as exc:
        print(f"[!] 请求失败: {exc}", file=sys.stderr)
        sys.exit(1)

    info = data.get("info", {})
    prize = data.get("prize_pool", {}).get("total_prize_pool", 0) or 0
    print(f"[*] 联赛: {info.get('name')}  |  奖金池: ${prize:,}")

    print("=" * 78)

    team_map = build_team_map(data.get("node_groups", []))
    series_list = collect_series(data.get("node_groups", []))

    if not series_list:
        print("[!] 未找到任何比赛数据。")
        return

    # 按实际开始时间排序，未开始/无时间的排最后
    def sort_key(node):
        return node.get("actual_time") or node.get("scheduled_time") or 0

    series_list.sort(key=sort_key)

    def has_teams(node):
        """是否已确定对阵双方（Swiss 阶段未填位的空节点无队伍）。"""
        return bool(node.get("team_id_1") and node.get("team_id_2"))

    finished = [n for n in series_list if n.get("is_completed")]
    live = [n for n in series_list if n.get("has_started") and not n.get("is_completed")]
    upcoming = [n for n in series_list if not n.get("has_started") and has_teams(n)]

    def print_series(node):
        t1 = team_map.get(node.get("team_id_1"), "TBD")
        t2 = team_map.get(node.get("team_id_2"), "TBD")
        w1, w2 = _series_score(node)
        status = series_status(node)
        time_str = fmt_ts(node.get("actual_time") or node.get("scheduled_time"))
        # 每局比分
        games = "  ".join(
            f"G{g['match_id']}->{team_map.get(g['winning_team_id'], '?')}"
            for g in node.get("matches", [])
        )
        print(f"{time_str}  [{status}]  {_score_line(t1, w1, w2, t2)}")
        if games:
            print(f"          局次: {games}")

    print(f"\n### 已结束的比赛 ({len(finished)})")
    for n in finished:
        print_series(n)

    print(f"\n### 进行中的比赛 ({len(live)})")
    for n in live:
        print_series(n)

    print(f"\n### 未开始的比赛 ({len(upcoming)})")
    for n in upcoming:
        print_series(n)

    print("\n" + "=" * 78)
    print(
        f"[*] 共 {len(series_list)} 场系列赛（结束 {len(finished)} / 进行中 {len(live)} / 未开始 {len(upcoming)}）"
    )

    # Steam 实时单局详情（需要 key；失败不影响上面的结果）
    try:
        hero_map = fetch_hero_map()
        live_games = fetch_live_games()
        print_live_details(live_games, hero_map)
    except Exception as exc:
        print(f"\n[!] 获取 Steam 实时单局详情失败: {exc}", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="抓取 TI2026 比赛结果 / 生成战报图片（自动判断小组赛/国际邀请赛正赛）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python ti_results.py                       # 输出全部赛果 + 实时单局详情\n"
            "  python ti_results.py --standings-img       # 生成战报 PNG（瑞士轮积分/正赛对阵图自动判断）\n"
            "  python ti_results.py --standings-img output/ti_report.png\n"
            "  python ti_results.py --watch               # 每10s监听最新结束的比赛结果\n"
        ),
    )
    parser.add_argument(
        "--standings-img",
        nargs="?",
        metavar="输出路径",
        const="DEFAULT",
        help="生成战报 PNG 图片（自动判断阶段：小组赛积分表 / 国际邀请赛正赛对阵图）；"
        "默认保存到 output/ti2026_swiss_YYYYMMDD_HHMM.png 或 ti2026_main_event_YYYYMMDD_HHMM.png",
    )
    parser.add_argument(
        "--watch", action="store_true", help="持续监听最新结束的比赛单局/系列赛结果"
    )
    parser.add_argument("--interval", type=int, default=10, help="轮询间隔秒数（默认 10）")
    args = parser.parse_args()

    # —— 图片战报分支 ——
    if args.standings_img:
        output_path = None if args.standings_img == "DEFAULT" else args.standings_img

        async def _run_with_cleanup():
            try:
                return await generate_league_report_image(output_path)
            finally:
                await shared_browser.close_browser()

        try:
            saved = asyncio.run(_run_with_cleanup())
        except ImportError as e:
            print(f"[!] 生成图片需要 Pillow 库：{e}", file=sys.stderr)
            sys.exit(2)
        if saved:
            print(f"[√] 战报图片已生成: {saved}")
        else:
            print("[!] 图片生成失败（可能暂无数据）。", file=sys.stderr)
            sys.exit(1)
    elif args.watch:
        try:
            watch_finished(args.interval)
        except KeyboardInterrupt:
            print("\n[*] 已停止监听。")
    else:
        main()
