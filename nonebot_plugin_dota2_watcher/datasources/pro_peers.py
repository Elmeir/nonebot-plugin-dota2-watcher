"""Stratz 职业选手对战记录数据源：单次 GraphQL 聚合查询玩家与职业选手的队友/对手记录。

接口：https://api.stratz.com/graphql（GraphQL 端点），使用 Bearer Token 鉴权。
Token 配置：config.json 的 d2w_stratz_token，或环境变量 D2W_STRATZ_TOKEN / STRATZ_TOKEN。

查询走 stratz.page.player.peers（STRATZ 网站专用聚合字段，API Token 亦可访问）：
一次请求同时返回队友（peers）与对手（peersAgainst）两组完整列表，
每条含 matchCount / lastMatchDateTime / steamAccount.proSteamAccount（职业身份与当前战队），
服务端已完成全量历史聚合，无需本地翻页拉取比赛。

职业校验：STRATZ 的 proSteamAccount 标记较宽泛（社区比赛选手也会被标记为 PRO），
因此聚合结果还需经 Liquipedia Dota2 wiki 交叉校验 —— 批量查询 MediaWiki API，
页面存在选手页（Infobox player）且 Steam ID 与选手账号一致才确认；
校验通过的缓存不限时，未通过的缓存 1 个月后重查（选手日后可能被收录）。
选手名与战队名均由 STRATZ 直接提供（team.name 优先，其次 team.tag）。

双源互补：OpenDota 的 /players/{id}/pros 端点提供另一份职业选手对战聚合
（含队友/对手场次与上次同场时间，名单与 STRATZ 各有覆盖），两份结果按
proSteamAccount.id（= OpenDota account_id，选手实际账号 ID）归并互补。

每次调用先尝试抓取 API；抓取成功按 steam 账号缓存到 data/pro_peers/，
抓取失败（网络/限流）才回退本地缓存（缓存不设时间限制）。

上次共同对局的比赛 ID 单独缓存（last_match_ids_{id}.json，按查询账号一份）：
选手的上次同局时间没变即视为 ID 仍有效，直接读缓存不再向 STRATZ 发查询。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from nonebot.log import logger

from ..config import DATA_DIR
from ..utils import cache_with_fallback, get_json, load_cache
from .hero_pool import HeroPoolError, _graphql_post, _RateLimited, _token

# 与 stratz.com peers 页面一致的聚合请求：一次 POST 同时取队友与对手两组全量列表
QUERY = """
query GetPeers($steamId: Long!, $teammatesPeersRequest: PlayerTeammatesGroupByRequestType!, $teammatesPeersAgainstRequest: PlayerTeammatesGroupByRequestType!, $take: Int) {
  player(steamAccountId: $steamId) {
    steamAccount { name }
  }
  stratz {
    page {
      player(steamAccountId: $steamId) {
        peers: peers(request: $teammatesPeersRequest, take: $take) {
          matchCount
          winCount
          lastMatchDateTime
          steamAccount { id name proSteamAccount { id name team { name tag } } }
        }
        peersAgainst: peers(request: $teammatesPeersAgainstRequest, take: $take) {
          matchCount
          winCount
          lastMatchDateTime
          steamAccount { id name proSteamAccount { id name team { name tag } } }
        }
      }
    }
  }
}
"""

_PEERS_VARS_BASE = {
    "playerTeammateSort": "WITH",  # AGAINST 由对手组覆盖
    "matchGroupOrderBy": "MATCH_COUNT",
    "orderBy": "DESC",
    "matchLimitMin": 1,
    "skip": 0,
    "take": 10000,  # 与 stratz.com 一致：单次拉全量
}

# 抓取结果缓存：按 steam 账号各存一份到 data/pro_peers/ 目录，缓存不设时间限制
CACHE_DIR = DATA_DIR / "pro_peers"
CACHE_VERSION = 6  # 缓存结构版本（stats 含玩家昵称）；升级后旧缓存自动失效
OUTPUT_LIMIT = 10  # 报告最多展示的职业选手条数

# Liquipedia 选手页校验：批量 MediaWiki API + 按选手名缓存 1 个月
LIQUIPEDIA_API = "https://liquipedia.net/dota2/api.php"
_LIQUIPEDIA_UA = "nonebot-plugin-dota2-watcher/0.1 (https://github.com/Elmeir/dota2-watcher-nonebot)"
_LIQUI_BATCH_SIZE = 50  # MediaWiki API 单次最多 50 个标题
_LIQUI_BATCH_INTERVAL = 1.0  # 批间间隔（秒），遵守 Liquipedia 限速（约 2 req/s）
_LIQUI_CACHE_FILE = CACHE_DIR / "liquipedia_players.json"
# 未通过校验的缓存时长（1 个月）；通过校验（确认职业选手）的缓存不限时
_LIQUI_TTL = 30 * 86400

# OpenDota 职业选手对战聚合（与 STRATZ 互补）
OPENDOTA_PROS_URL = "https://api.opendota.com/api/players/{account_id}/pros"
_OD_CACHE_VERSION = 1


class ProPeersError(Exception):
    """职业选手对战记录抓取失败（供上层转为用户提示）。"""


def _cache_path(steam_id) -> Path:
    """返回指定 steam 账号对应的缓存文件路径。"""
    return CACHE_DIR / f"pro_peers_{int(steam_id)}.json"


def _load_cache(cache_path: Path, steam_id):
    """读取缓存；命中（结构/账号一致）即返回 (player_name, stats)，否则 None。"""
    data = load_cache(cache_path)
    if data is None or data.get("cache_version") != CACHE_VERSION:
        return None
    if data.get("steam_id") != int(steam_id):
        return None
    return data.get("player_name") or "玩家", data.get("stats") or []


def _save_cache(cache_path: Path, steam_id, player_name: str, stats: list[dict]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "cache_version": CACHE_VERSION,
                "steam_id": int(steam_id),
                "fetched_at": time.time(),
                "player_name": player_name,
                "stats": stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_peer_entry(entry: dict) -> tuple[int, int, int, int] | None:
    """解析一条 peers 记录为 (steam_id, match_count, win_count, last_ts)；无效返回 None。"""
    account = entry.get("steamAccount") or {}
    steam_id = account.get("id")
    if not steam_id:
        return None
    return (
        int(steam_id),
        int(entry.get("matchCount") or 0),
        int(entry.get("winCount") or 0),
        int(entry.get("lastMatchDateTime") or 0),
    )


def _merge_peer_groups(payload: dict, steam_id: int) -> list[dict]:
    """合并队友/对手两组 peers 列表，聚合出职业选手的队友/对手场次统计。

    返回按（队友+对手）总场次降序的列表，每条
    {'name', 'team', 'with', 'with_win', 'against', 'against_win', 'last'}
    （*_win 为查询玩家视角的胜场：队友组取 winCount，对手组取 matchCount - winCount；
    last 为最近同局 Unix 时间戳）。
    """
    page_player = (((payload.get("data") or {}).get("stratz") or {}).get("page") or {}).get(
        "player"
    ) or {}

    # 先按 steam_id 归并两组记录：with/against 场次与最近同局时间取最大
    merged: dict[int, dict] = {}
    for key, group in (("with", "peers"), ("against", "peersAgainst")):
        for entry in page_player.get(group) or []:
            parsed = _parse_peer_entry(entry)
            if parsed is None:
                continue
            pid, count, win, last = parsed
            if pid == steam_id:
                continue
            account = entry.get("steamAccount") or {}
            info = merged.setdefault(
                pid,
                {"with": 0, "with_win": 0, "against": 0, "against_win": 0, "last": 0, "account": account},
            )
            info[key] = count
            # 队友同队胜负一致；对手的败场即查询玩家的胜场
            info[f"{key}_win"] = win if key == "with" else count - win
            info["last"] = max(info["last"], last)

    stats = []
    for info in merged.values():
        account = info.pop("account")
        pro = account.get("proSteamAccount") or {}
        if not pro:
            continue  # 仅保留 STRATZ 认定的职业选手
        stats.append(
            {
                "name": pro.get("name") or account.get("name") or "",
                # proSteamAccount.id（pro 库 ID）才是稳定标识；
                # steamAccount.id 会错绑同场普通玩家，不能用于比赛匹配
                "pro_id": pro.get("id") or "",
                # 玩家游戏内昵称（steamAccount.name），输出展示用
                "nickname": account.get("name") or "",
                "with": info["with"],
                "with_win": info["with_win"],
                "against": info["against"],
                "against_win": info["against_win"],
                "last": info["last"],
            }
        )
    stats.sort(key=lambda s: -(s["with"] + s["against"]))
    return stats


async def fetch_pro_peers(steam_id):
    """单次 GraphQL 聚合查询玩家与职业选手的队友/对手记录。

    每次调用先尝试抓取 API，抓取失败（网络/限流）才回退本地缓存（不设时间限制）。
    返回 (player_name, stats)：stats 按（队友+对手）总场次降序，每条
    {'name', 'nickname', 'with', 'with_win', 'against', 'against_win', 'last'}。
    """
    steam_id = int(steam_id)
    cache_path = _cache_path(steam_id)
    try:
        token = _token()
    except HeroPoolError as e:
        raise ProPeersError(str(e)) from e
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "stratz-pro-peers/0.1",
    }
    variables = {
        "steamId": steam_id,
        "take": _PEERS_VARS_BASE["take"],
        "teammatesPeersRequest": {**_PEERS_VARS_BASE},
        "teammatesPeersAgainstRequest": {**_PEERS_VARS_BASE, "playerTeammateSort": "AGAINST"},
    }

    async def _fetch():
        # 单次 POST 聚合查询；对限流(429/503)做退避重试
        for attempt in range(4):
            try:
                payload = await _graphql_post(QUERY, variables, headers)
                break
            except _RateLimited:
                if attempt < 3:
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                raise
        if payload.get("errors"):
            raise ProPeersError(f"Stratz GraphQL 返回错误：{payload['errors']}")
        player_name = (
            ((payload.get("data") or {}).get("player") or {}).get("steamAccount") or {}
        ).get("name") or "玩家"
        stats = _merge_peer_groups(payload, steam_id)
        try:
            _save_cache(cache_path, steam_id, player_name, stats)
        except Exception as e:
            logger.warning(f"职业选手对战记录缓存写入失败：{e}")
        return player_name, stats

    return await cache_with_fallback(
        cache_path,
        _fetch,
        max_age=None,
        force_update=True,
        loader=lambda p: _load_cache(p, steam_id),
        warn=lambda: logger.warning(f"Stratz 抓取失败，回退本地缓存 {cache_path.name}"),
    )


def _load_od_cache(cache_path: Path):
    """读取 OpenDota pros 缓存；结构不符返回 None。"""
    data = load_cache(cache_path)
    if data is None or data.get("cache_version") != _OD_CACHE_VERSION:
        return None
    return data.get("records") or []


async def fetch_opendota_pros(steam_id) -> list[dict]:
    """从 OpenDota 拉取职业选手对战聚合（/players/{id}/pros，与 STRATZ 名单互补）。

    缓存策略与 STRATZ 一致：每次先抓取，失败回退本地缓存（不设时间限制）。
    返回 stats 同构列表（last 为 OpenDota 的 last_played 时间戳，可能为 0）。
    """
    steam_id = int(steam_id)
    cache_path = CACHE_DIR / f"opendota_pros_{steam_id}.json"

    async def _fetch():
        data = await get_json(OPENDOTA_PROS_URL.format(account_id=steam_id))
        records = []
        for entry in data or []:
            pro_id = entry.get("account_id")
            if not pro_id:
                continue
            records.append(
                {
                    "name": entry.get("name") or "",
                    "nickname": entry.get("personaname") or "",
                    "pro_id": int(pro_id),
                    "with": entry.get("with_games") or 0,
                    "with_win": entry.get("with_win") or 0,
                    "against": entry.get("against_games") or 0,
                    "against_win": entry.get("against_win") or 0,
                    "last": entry.get("last_played") or 0,
                }
            )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"cache_version": _OD_CACHE_VERSION, "records": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return records

    return await cache_with_fallback(
        cache_path,
        _fetch,
        max_age=None,
        force_update=True,
        loader=_load_od_cache,
        warn=lambda: logger.warning(f"OpenDota 抓取失败，回退本地缓存 {cache_path.name}"),
    )


def merge_stats(stratz_stats: list[dict], od_stats: list[dict]) -> list[dict]:
    """按 pro_id 归并 STRATZ 与 OpenDota 两边的职业选手统计（互补，原列表不变）。

    两边都有的选手：队友/对手场次各取数据更多的一边（胜负随之），
    上次同局时间取较大值，名字/昵称优先 STRATZ，OpenDota 侧补充缺失项。
    """
    merged: dict[str, dict] = {}
    for st in stratz_stats:
        key = str(st.get("pro_id") or "")
        if key:
            merged[key] = dict(st)
    for od in od_stats:
        key = str(od.get("pro_id") or "")
        if not key:
            continue
        cur = merged.setdefault(key, dict(od))
        for field in ("with", "against"):
            if (od.get(field) or 0) > (cur.get(field) or 0):
                cur[field] = od[field]
                cur[f"{field}_win"] = od[f"{field}_win"]
        cur["last"] = max(cur.get("last") or 0, od.get("last") or 0)
        if not cur.get("name"):
            cur["name"] = od.get("name") or ""
        # 昵称以 OpenDota 的 personaname 为准（选手当前实际游戏昵称，比 STRATZ 更新及时）
        if od.get("nickname"):
            cur["nickname"] = od["nickname"]
    return sorted(merged.values(), key=lambda s: -(s["with"] + s["against"]))


def _fmt_games(win: int, total: int) -> str:
    """场次格式化：无同局记录（0/0）时只显示 0，否则显示 胜/总。"""
    return str(total) if total == 0 else f"{win}/{total}"


def build_report(player_name: str, stats: list[dict]) -> str:
    """把聚合统计格式化为群消息文本（按总场次降序，最多 OUTPUT_LIMIT 条）。"""
    if not stats:
        return f"{player_name}未在历史比赛中遇到过职业选手"
    head = f"{player_name}与职业选手的对战记录"
    if len(stats) > OUTPUT_LIMIT:
        head += f"(共{len(stats)}位, 仅展示前{OUTPUT_LIMIT}位)"
    else:
        head += f"(共{len(stats)}位)"
    lines = [f"{head}："]
    for st in stats[:OUTPUT_LIMIT]:
        # 括号内显示玩家游戏内昵称；与选手名相同或为空时省略
        nickname = st.get("nickname") or ""
        tag = f" ({nickname})" if nickname and nickname != st["name"] else ""
        last = (
            datetime.fromtimestamp(st["last"]).strftime("%Y/%m/%d") if st["last"] else "未知"
        )
        lines.append(
            f"{st['name']}{tag} 队友{_fmt_games(st['with_win'], st['with'])}"
            f" 对手{_fmt_games(st['against_win'], st['against'])} {last}"
        )
        if st.get("last_match_id"):
            lines[-1] += f" {st['last_match_id']}"
    return "\n".join(lines)


# ============================================================
# Liquipedia 选手页校验
# ============================================================
# Steam 64 位 ID = 76561197960265728 + 32 位账号 ID
_STEAM64_BASE = 76561197960265728
_STEAM64_RE = re.compile(r"7656119\d{10}")
_PLAYERID_RE = re.compile(r"\|\s*playerid\s*=\s*(\d+)")  # Liquipedia Infobox 的账号 ID 参数
_LIQUI_CACHE_VERSION = 2


def _load_liqui_cache() -> dict:
    """读取选手校验缓存：{'version': 2, 'entries': {...}}；缺失/损坏/旧版本返回空表。"""
    data = load_cache(_LIQUI_CACHE_FILE) or {}
    if data.get("version") != _LIQUI_CACHE_VERSION:
        return {"version": _LIQUI_CACHE_VERSION, "entries": {}}
    data.setdefault("entries", {})
    return data


def _save_liqui_cache(cache: dict) -> None:
    _LIQUI_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LIQUI_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _inspect_page(page: dict | None) -> dict:
    """解析 Liquipedia 页面：返回 {'page': player|other|missing, 'steam_ids': [...]}。

    steam_ids 为页面上能提取到的 Steam 32 位账号 ID 集合（字符串形式），
    来源：Infobox 的 |playerid= 参数与全文中的 Steam 64 位 ID；
    页面未提供任何 ID 时为空表。
    """
    if not page or "missing" in page:
        return {"page": "missing", "steam_ids": []}
    revs = page.get("revisions") or []
    text = ((revs[0].get("slots") or {}).get("main") or {}).get("*") if revs else ""
    text = text or ""
    if "infobox player" not in text.lower():
        return {"page": "other", "steam_ids": []}
    ids = {int(pid) for pid in _PLAYERID_RE.findall(text)}
    ids |= {int(s) - _STEAM64_BASE for s in _STEAM64_RE.findall(text) if int(s) > _STEAM64_BASE}
    return {"page": "player", "steam_ids": sorted(str(i) for i in ids)}


def _page_matches_pro(entry: dict, pro_id: int) -> bool:
    """判断缓存的页面信息能否证实「该 Liquipedia 页面属于此账号」。

    页面缺失/非选手页 → 不匹配；选手页未提供 Steam ID → 视为匹配（不误杀）；
    提供了 ID → 必须包含选手的账号 ID。
    """
    if entry.get("page") != "player":
        return False
    steam_ids = entry.get("steam_ids") or []
    if not steam_ids:
        return True
    return str(pro_id) in steam_ids


def _resolve_pages(query: dict, names: list[str]) -> dict[str, dict | None]:
    """把 MediaWiki 响应中的页面按原始请求名归位（处理大小写归一与重定向链）。"""
    norm_map = {r["from"]: r["to"] for r in query.get("normalized") or []}
    redirect_map = {r["from"]: r["to"] for r in query.get("redirects") or []}
    pages_by_title = {p.get("title"): p for p in (query.get("pages") or {}).values()}

    result: dict[str, dict | None] = {}
    for name in names:
        title = norm_map.get(name, name)
        seen: set[str] = set()
        while title in redirect_map and title not in seen:
            seen.add(title)
            title = redirect_map[title]
        result[name] = pages_by_title.get(title)
    return result


async def _query_liquipedia(names: list[str]) -> dict[str, dict]:
    """批量查询 Liquipedia 页面信息，返回 {选手名: 页面信息}（page + steam_ids）。

    单批最多 _LIQUI_BATCH_SIZE 个标题，批间按 Liquipedia 限速留间隔；
    任一批失败直接抛异常（由调用方决定回退行为）。
    """
    params = {
        "action": "query",
        "format": "json",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "redirects": 1,
    }
    headers = {"User-Agent": _LIQUIPEDIA_UA}
    inspected: dict[str, dict] = {}
    for index in range(0, len(names), _LIQUI_BATCH_SIZE):
        if index:
            await asyncio.sleep(_LIQUI_BATCH_INTERVAL)
        batch = names[index : index + _LIQUI_BATCH_SIZE]
        data = await get_json(LIQUIPEDIA_API, headers=headers, params={**params, "titles": "|".join(batch)})
        query = data.get("query") or {}
        if data.get("error"):
            raise ProPeersError(f"Liquipedia API 返回错误：{data['error']}")
        for name, page in _resolve_pages(query, batch).items():
            inspected[name] = _inspect_page(page)
    return inspected


async def filter_verified(stats: list[dict]) -> list[dict]:
    """用 Liquipedia 交叉验证并过滤非真正职业选手（STRATZ 的 PRO 标记含社区比赛选手）。

    双重验证：标题存在选手页 + 页面 Infobox 中的 Steam ID 与选手实际账号一致
    （防止同名/重定向页面误判）；页面未填 Steam ID 时仅按页面存在性放行。
    校验通过（确认职业选手）的缓存不限时；未通过的结果缓存 1 个月后重查
    （选手日后可能被 Liquipedia 收录）。查询失败时未缓存的选手保留不过滤
    （避免误杀），已缓存的判定正常生效。返回仅含通过校验选手的新列表（原列表不变）。
    """
    if not stats:
        return stats
    cache = _load_liqui_cache()
    entries = cache["entries"]
    now = time.time()

    def _needs_refresh(name: str, pro_id: int) -> bool:
        entry = entries.get(name)
        if entry is None:
            return True
        # 已确认职业选手（页面匹配通过）的缓存不限时
        if _page_matches_pro(entry, pro_id):
            return False
        return now - entry.get("fetched_at", 0) > _LIQUI_TTL

    stale = []
    for st in stats:
        name = st.get("name") or ""
        if not name or not _needs_refresh(name, int(st.get("pro_id") or 0)):
            continue
        if name not in stale:
            stale.append(name)

    failed = False
    if stale:
        try:
            inspected = await _query_liquipedia(stale)
        except Exception as e:
            logger.warning(f"Liquipedia 校验失败，未缓存选手本次不做过滤：{e}")
            inspected = {}
            failed = True
        for name, info in inspected.items():
            info["fetched_at"] = now
            entries[name] = info
        if inspected:
            try:
                _save_liqui_cache(cache)
            except Exception as e:
                logger.warning(f"Liquipedia 校验缓存写入失败：{e}")

    result = []
    for st in stats:
        entry = entries.get(st["name"])
        if entry is None:
            if failed:  # 校验不可用：保留，避免误杀
                result.append(st)
            continue
        if _page_matches_pro(entry, int(st.get("pro_id") or 0)):
            result.append(st)
    return result


# 比赛 ID 缓存：按查询账号存 data/pro_peers/last_match_ids_{id}.json，
# 记录 {str(pro_id): {"last": 上次同局时间, "match_id": 比赛 ID}}；
# 上次同局时间没变即视为比赛 ID 仍有效，直接读缓存不再请求 STRATZ
_IDS_CACHE_VERSION = 1


def _ids_cache_path(steam_id) -> Path:
    """返回指定 steam 账号的比赛 ID 缓存文件路径。"""
    return CACHE_DIR / f"last_match_ids_{int(steam_id)}.json"


def _load_ids_cache(cache_path: Path, steam_id) -> dict:
    """读取比赛 ID 缓存；命中（结构/账号一致）返回 entries 表，否则空表。"""
    data = load_cache(cache_path)
    if data is None or data.get("cache_version") != _IDS_CACHE_VERSION:
        return {}
    if data.get("steam_id") != int(steam_id):
        return {}
    return data.get("entries") or {}


def _save_ids_cache(cache_path: Path, steam_id, entries: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {"cache_version": _IDS_CACHE_VERSION, "steam_id": int(steam_id), "entries": entries},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def attach_last_match_ids(steam_id, stats: list[dict]) -> None:
    """为前 OUTPUT_LIMIT 位选手补充「上次共同对局」的比赛 ID（原地写入 last_match_id 键）。

    做法：以 peers 给出的 lastMatchDateTime 为中心开 ±3 小时时间窗口，
    每位选手一个别名字段查窗口内自己的比赛（含全部玩家 proSteamAccount），
    按 proSteamAccount.id 匹配出该选手同场的比赛，窗口内取最新一场。
    注意不能用 peers 的 steamAccount.id 去匹配（它会错绑同场普通玩家），
    也不能用 withFriend/withEnemySteamAccountIds 过滤（行为不可靠）。
    比赛 ID 按 (pro_id, last) 缓存：上次同局时间没变就直接复用缓存的 ID，
    仅对时间变化或无缓存的选手发起查询。
    抓取失败不抛异常（仅日志），报告退化为无比赛 ID。
    """
    targets = [st for st in stats if st.get("pro_id")][:OUTPUT_LIMIT]
    if not targets:
        return
    cache_path = _ids_cache_path(steam_id)
    cached = _load_ids_cache(cache_path, steam_id)

    # 上次同局时间没变的选手直接复用缓存，只有时间变化/无缓存的才需要查询
    pending = []
    for st in targets:
        last = st["last"] or 0
        entry = cached.get(str(st["pro_id"])) or {}
        if last and entry.get("last") == last and entry.get("match_id"):
            st["last_match_id"] = entry["match_id"]
        else:
            pending.append(st)
    if not pending:
        return
    try:
        token = _token()
    except HeroPoolError as e:
        logger.warning(f"未配置 Stratz Token，无法补充比赛 ID：{e}")
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "stratz-pro-peers/0.1",
    }
    # 每位选手一个窗口变量（变量式传参与 stratz.com 网站一致）
    window = 3 * 3600
    var_defs, aliases, variables = [], [], {"id": int(steam_id)}
    for i, st in enumerate(pending):
        last = st["last"] or 0
        if not last:
            continue
        var_defs.append(f"$w{i}: PlayerMatchesRequestType!")
        aliases.append(
            f"m{i}: matches(request: $w{i}) {{ id startDateTime "
            "players { steamAccount { proSteamAccount { id } } } }"
        )
        variables[f"w{i}"] = {
            "startDateTime": last - window,
            "endDateTime": last + window,
            "take": 20,
        }
    if not aliases:
        return
    query = (
        "query LastMatchIds($id: Long!, " + ", ".join(var_defs) + ") "
        "{ player(steamAccountId: $id) { " + " ".join(aliases) + " } }"
    )

    # 对限流(429/503)做退避重试；最终失败则放弃补充 ID
    for attempt in range(4):
        try:
            payload = await _graphql_post(query, variables, headers)
            break
        except _RateLimited:
            if attempt < 3:
                await asyncio.sleep(15 * (attempt + 1))
                continue
            logger.warning("Stratz 补充比赛 ID 被限流，本次报告不含比赛 ID")
            return
        except Exception as e:
            logger.warning(f"Stratz 补充比赛 ID 失败，本次报告不含比赛 ID：{e}")
            return
    if payload.get("errors"):
        logger.warning(f"Stratz 补充比赛 ID 返回错误：{payload['errors']}")
        return

    player_data = (payload.get("data") or {}).get("player") or {}
    updated = False
    for i, st in enumerate(pending):
        if f"w{i}" not in variables:
            continue
        pro_id = st["pro_id"]
        hits = []
        for m in player_data.get(f"m{i}") or []:
            for p in m.get("players") or []:
                pro = ((p.get("steamAccount") or {}).get("proSteamAccount")) or {}
                if pro.get("id") == pro_id:
                    hits.append(m)
                    break
        if hits:
            # 窗口内可能同场多场，取最新一场
            match = max(hits, key=lambda m: m.get("startDateTime") or 0)
            st["last_match_id"] = match["id"]
            cached[str(pro_id)] = {"last": st["last"] or 0, "match_id": match["id"]}
            updated = True
    if updated:
        try:
            _save_ids_cache(cache_path, steam_id, cached)
        except Exception as e:
            logger.warning(f"比赛 ID 缓存写入失败：{e}")
