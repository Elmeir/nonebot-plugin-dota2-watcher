"""业务逻辑层：命令处理与定时任务共用的纯逻辑。

本模块不依赖 NoneBot matcher，只通过独立函数暴露可复用业务，
供命令层（commands）与定时任务层（scheduler）调用，保持两者职责单一。
"""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger

from ..config import DATA_DIR, config
from ..datasources import d2pt, pro_peers, ti_results
from ..datasources.hero_pool import HeroPoolError
from ..datasources.pro_peers import ProPeersError
from ..datasources.request_match import (
    request_match_history,
    request_match_info_opendota,
    request_news,
)
from ..generators import core_build, hero_pool, match_builder
from ..utils import load_cache, run_single_flight
from . import store
from .player import Player

# Steam GetMatchHistory 接口存在速率限制，并发过高易触发 429/503 导致请求失败，
# 因此用信号量限制同批并发拉取比赛历史的数量（并发上限见 config.d2w_history_concurrency）。
_history_semaphore = asyncio.Semaphore(config.d2w_history_concurrency)


@dataclass
class NewMatch:
    """一场待处理的新比赛：所属群 + 比赛 ID + 订阅玩家 + 比赛详情。

    match_info 在阶段二拉取后填充，供战报文本与图片生成复用，避免重复请求数据源。
    """

    gid: str
    match_id: int
    players: list[Player] = field(default_factory=list)
    match_info: dict | None = None

    def has_valid_info(self) -> bool:
        """比赛详情是否有效：已成功拉取且含 radiant_win 等必要字段。"""
        info = self.match_info
        return bool(info) and not isinstance(info, Exception) and "radiant_win" in info


# ---------------------------------------------------------------
# 命令业务
# ---------------------------------------------------------------
async def add_player(group_id, nickname: str, steam_id) -> str:
    """订阅玩家；返回提示文案。"""
    if not str(steam_id).isdigit():
        return "steam id 必须是数字"
    if nickname == config.d2w_all_nickname:
        return f"{config.d2w_all_nickname}不是一个合法的昵称"
    reply = store.upsert_player(str(group_id), nickname, int(steam_id))
    store.save()
    return reply


def list_players(group_id) -> str:
    """返回本群玩家列表文案。"""
    players = store.get_all().get(str(group_id), [])
    if not players:
        return "当前群组没有添加任何玩家"
    lines = [f"{p.nickname}（{p.short_steamID}）" for p in players]
    return "本群玩家列表：\n" + "\n".join(lines)


def delete_player(group_id, player_name: str) -> str:
    """删除本群指定玩家；返回提示文案。"""
    reply = store.delete_player(str(group_id), player_name)
    store.save()
    return reply


def toggle_broadcast(group_id, player_name: str, display: bool) -> str:
    """开启/关闭某玩家（或全体）的播报；返回提示文案（空串表示无需回复）。"""
    reply = store.set_display(str(group_id), player_name, display)
    store.save()
    return reply or ""


_ON, _OFF = "开", "关"

# 订阅项：key -> (显示名, 本群存储字段, 全局总开关当前值)
_SUBSCRIPTIONS = {
    "news": ("官方新闻", "subscribe_news", config.d2w_news_enabled),
    "ti": ("TI 赛事", "subscribe_ti", config.d2w_ti_enabled),
}


def _state(v: bool) -> str:
    """布尔开关值 -> '开'/'关'。"""
    return _ON if v else _OFF


def toggle_subscription(group_id, key: str) -> str:
    """切换某类订阅开关，并返回含全部订阅状态的提示文案。"""
    name = _SUBSCRIPTIONS[key][0]
    enabled = store.toggle_subscription(str(group_id), key)
    store.save()
    _sync_news_baseline(key)
    return f"已{'开启' if enabled else '关闭'}{name}订阅\n" + subscription_status(group_id)


def set_subscription(group_id, key: str, enabled: bool) -> str:
    """将某类订阅设为指定开关状态，并返回含全部订阅状态的提示文案。"""
    name = _SUBSCRIPTIONS[key][0]
    enabled = store.set_subscription(str(group_id), key, bool(enabled))
    store.save()
    _sync_news_baseline(key)
    return f"{name}订阅已{_state(enabled)}\n" + subscription_status(group_id)


def subscription_status(group_id) -> str:
    """查看全局总开关与本群订阅开关状态。"""
    settings = store.get_group_settings(str(group_id))
    lines = ["DOTA2 订阅状态："]
    lines.extend(
        f"{name}：{_state(global_on)} | 本群 {_state(settings.get(field, True))}"
        for name, field, global_on in _SUBSCRIPTIONS.values()
    )
    return "\n".join(lines)


async def d2pt_report(pos: str = "all") -> Message:
    """D2PT 位置数据（文本）。相同位置的并发查询共享同一次执行。"""

    async def _build() -> Message:
        try:
            # 默认走 1 小时缓存，避免每次触发都重新拉取
            posdata = await d2pt.load_data(force_update=False)
        except Exception:
            logger.exception("d2pt 数据加载失败")
            return Message("d2pt读取数据失败")
        if not posdata:
            return Message("d2pt读取数据失败")

        msg = d2pt.generate_message(posdata, pos)
        if not msg:
            return Message("d2pt读取数据失败")

        return Message(msg)

    return await run_single_flight(("d2pt", pos), _build)


async def report_image(match_id: str) -> str:
    """生成开黑战报图片，返回本地路径；失败返回空串。相同比赛的并发查询共享同一次执行。"""

    async def _build() -> str:
        result = await match_builder.generate_report_img(match_id, force=True)
        return result or ""

    return await run_single_flight(("report", match_id), _build)


async def build_image(hero: str, position=None, theme: str = "light") -> str:
    """生成核心出装图片，返回本地路径；失败返回空串。相同参数的并发查询共享同一次执行。"""

    async def _build() -> str:
        try:
            result = await core_build.generate_image(hero, position, theme=theme)
            return result or ""
        except Exception:
            logger.exception("出装图生成失败")
            return ""

    return await run_single_flight(("build", hero, position, theme), _build)


async def ti_image(stage: str = "auto") -> str:
    """生成 TI 赛事战报图片，返回本地路径；失败返回空串。

    stage 取值 auto/swiss/elimination_round/main_event，传 auto 表示自动判断当前（最新）阶段。
    相同阶段的并发查询共享同一次执行。
    """

    async def _build() -> str:
        try:
            result = await ti_results.generate_league_report_image(stage=stage)
            return result or ""
        except Exception:
            logger.exception("TI 战报生成失败")
            return ""

    return await run_single_flight(("ti", stage), _build)


# 英雄池比赛数量档位：关键字（中英） -> 拉取场次；非法关键字回落默认挡
_HERO_POOL_SIZES = {
    "min": 25,
    "小": 25,
    "mid": 50,
    "中": 50,
    "max": 100,
    "大": 100,
}


async def hero_pool_image(group_id, arg: str, size: str = "") -> str:
    """生成玩家英雄池环形图，返回本地图片路径。

    arg 可为 steam_id（纯数字）或本群已订阅玩家昵称；
    size 为比赛数量档位（min/mid/max 或 小/中/大，留空默认 25 场）。
    参数解析失败 / 未配置 Token 时抛出 ValueError（提示文案），生成失败返回空串。
    """
    arg = arg.strip()
    # 优先按昵称匹配，因为昵称可能是数字，会与 steam_id 混淆
    player = next(
        (p for p in store.get_group(str(group_id)) if p.nickname == arg),
        None,
    )
    if player is not None:
        steam_id = player.short_steamID
    elif arg.isdigit():
        steam_id = int(arg)
    else:
        raise ValueError(
            f"未找到昵称为「{arg}」的订阅玩家，请先用 /添加刀塔玩家 订阅，或直接输入 steam_id"
        )
    count = _HERO_POOL_SIZES.get(size.strip().lower(), 25)

    async def _build() -> str:
        try:
            return await hero_pool.generate_image(steam_id, count=count) or ""
        except HeroPoolError as e:
            raise ValueError(str(e))
        except Exception:
            logger.exception("英雄池生成失败")
            return ""

    # 相同账号 + 相同档位的并发查询共享同一次执行
    return await run_single_flight(("hero_pool", steam_id, count), _build)


async def pro_report(group_id, arg: str) -> str:
    """查询玩家与职业选手的对战记录，返回文本。

    arg 可为 steam_id（纯数字）或本群已订阅玩家昵称；
    参数解析失败 / 未配置 Token 时抛出 ValueError（提示文案），查询失败返回空串。
    相同账号的并发查询通过 single-flight 共享同一次执行（不重复抓取）。
    """
    arg = arg.strip()
    # 优先按昵称匹配，因为昵称可能是数字，会与 steam_id 混淆
    player = next(
        (p for p in store.get_group(str(group_id)) if p.nickname == arg),
        None,
    )
    if player is not None:
        steam_id = player.short_steamID
    elif arg.isdigit():
        steam_id = int(arg)
    else:
        raise ValueError(
            f"未找到昵称为「{arg}」的订阅玩家，请先用 /添加刀塔玩家 订阅，或直接输入 steam_id"
        )

    async def _build() -> str:
        player_name, stratz_stats = await pro_peers.fetch_pro_peers(steam_id)
        od_stats = await pro_peers.fetch_opendota_pros(steam_id)
        stats = pro_peers.merge_stats(stratz_stats, od_stats)
        stats = await pro_peers.filter_verified(stats)
        await pro_peers.attach_last_match_ids(steam_id, stats)
        return pro_peers.build_report(player_name, stats)

    try:
        return await run_single_flight(int(steam_id), _build)
    except ProPeersError as e:
        raise ValueError(str(e))
    except Exception:
        logger.exception("职业选手对战记录查询失败")
        return ""


# ---------------------------------------------------------------
# 群播报
# ---------------------------------------------------------------
async def _broadcast(text: str | None, filter_key: str | None = None) -> None:
    """向群广播一条文本消息。filter_key 为 "subscribe_news"/"subscribe_ti" 时按开关过滤。"""
    if not text:
        return
    bots = get_bots()
    if not bots:
        return
    all_groups = store.get_all_groups()
    msg = Message(f"[DOTA2]{text}")
    for gid, info in all_groups.items():
        if filter_key and not info.get(filter_key, True):
            continue
        for bot in bots.values():
            try:
                await bot.send_group_msg(group_id=int(gid), message=msg)
            except Exception:
                logger.exception(f"广播消息到群 {gid} 失败")


async def _fetch_history(player: Player) -> int | None:
    """获取玩家最近一场比赛 ID；失败时记日志并返回 None。"""
    try:
        async with _history_semaphore:
            return await request_match_history(player, config.d2w_steam_api_key)
    except Exception as e:
        logger.warning(f"获取 {player.nickname} 最近比赛失败: {e}")
        return None


async def _report_match(match: NewMatch) -> None:
    """生成并发送一场比赛的战报（图片 + 一句话播报）。"""
    try:
        text = match_builder.generate_message(match.match_info, match.players, ezmode=True)
    except Exception:
        logger.exception(f"生成战报文本失败: {match.match_id}")
        text = None

    pic = None
    try:
        # 复用阶段二已拉取的 match_info，避免图片生成时再次请求 OpenDota
        pic = await match_builder.generate_report_img(
            match.match_id, force=True, match_data=match.match_info
        )
    except Exception:
        logger.exception(f"生成战报图片失败: {match.match_id}")

    msg = Message()
    if pic:
        msg += MessageSegment.image(file=pic)
    if text:
        msg += Message(text)
    if not msg:
        return

    bots = get_bots()
    if not bots:
        return
    for bot in bots.values():
        try:
            await bot.send_group_msg(group_id=int(match.gid), message=msg)
        except Exception:
            logger.exception(f"发送战报到群 {match.gid} 失败")


# ---------------------------------------------------------------
# 定时任务逻辑
# ---------------------------------------------------------------
async def poll_ti_results() -> None:
    """拉取最新 TI 赛果并广播。"""
    if not store.any_group_subscribed("subscribe_ti"):
        return
    try:
        msg = await ti_results.watch_latest_result(mode="game")
    except Exception:
        logger.exception("TI 结果监听失败")
        return
    await _broadcast(msg, "subscribe_ti")


# 新闻去重状态：记录已见过的新闻 gid，持久化到 data/news_state.json。
# 停机期间发布的新闻在恢复后按 gid 补播，不会因重启丢基线而错过；
# 首次部署（无状态文件）仅建立基线，不播报历史新闻。
# 全部群退订新闻时删除基线文件：停订期间不积累状态，
# 重新订阅后首轮轮询按首次运行重建基线，不会补播停订期间的旧新闻。
_NEWS_STATE_FILE: Path = DATA_DIR / "news_state.json"
_NEWS_STATE_MAX = 50  # 状态文件最多保留的 gid 数（防无限增长）


def _sync_news_baseline(key: str) -> None:
    """新闻订阅开关变化后同步基线：已无任何订阅群时删除基线文件。"""
    if key != "news" or store.any_group_subscribed("subscribe_news"):
        return
    try:
        _NEWS_STATE_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"新闻基线删除失败：{e}")


def _load_news_state() -> dict:
    """读取新闻去重状态 {'seen_gids': [...]}；文件缺失/损坏返回空表（视为首次运行）。"""
    data = load_cache(_NEWS_STATE_FILE)
    return data if isinstance(data, dict) else {}


def _save_news_state(seen: list[str]) -> None:
    _NEWS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _NEWS_STATE_FILE.write_text(
        json.dumps({"version": 1, "seen_gids": seen[-_NEWS_STATE_MAX:]}, ensure_ascii=False),
        encoding="utf-8",
    )


async def poll_news() -> None:
    """监听 DOTA2 官方新闻，出现新头条时广播。

    每次拉取最新 5 条，按 gid 与本地状态去重后把未播报过的补播出去
    （旧→新顺序，合并为一条消息）；停机期间发布的新闻会在恢复后补上。
    无群订阅时不发请求（基线已在退订时删除，重新订阅按首次运行重建）。
    """
    if not store.any_group_subscribed("subscribe_news"):
        return
    try:
        news = await request_news()
    except Exception:
        logger.exception("获取 DOTA2 新闻失败")
        return
    events = [
        e
        for e in ((news or {}).get("events") or [])
        if e.get("gid") and e.get("event_name")
    ]
    if not events:
        return

    state = _load_news_state()
    first_run = not state
    seen: list[str] = [str(g) for g in (state.get("seen_gids") or [])]
    seen_set = set(seen)

    # 未播报过的新闻（接口按时间倒序，reversed 后为旧→新）
    fresh = [e for e in reversed(events) if str(e["gid"]) not in seen_set]
    # 无论是否播报都先更新并持久化已见列表（播报失败不重发，避免刷屏）
    for e in events:
        gid = str(e["gid"])
        if gid not in seen_set:
            seen.append(gid)
            seen_set.add(gid)
    try:
        _save_news_state(seen)
    except Exception as e:
        logger.warning(f"新闻状态写入失败：{e}")

    if first_run or not fresh:
        return
    lines = [f"[news] {e['event_name']} www.dota2.com/newsentry/{e['gid']}" for e in fresh]
    await _broadcast("\n".join(lines), "subscribe_news")


async def poll_new_matches() -> None:
    """轮询订阅玩家的新比赛并生成战报播报。"""
    data = store.get_all()
    watched = [
        (gid, player)
        for gid, players in data.items()
        for player in players
        if player.display_recent_match
    ]
    if not watched:
        return

    # 按 steam_id 去重，同一账号只拉取一次比赛历史
    unique_players: dict[int, Player] = {}
    for _, player in watched:
        unique_players.setdefault(player.short_steamID, player)
    unique_list = list(unique_players.values())

    # 阶段一：并发获取去重后玩家的最近比赛 ID
    fetched = await asyncio.gather(
        *(_fetch_history(player) for player in unique_list),
        return_exceptions=True,
    )
    history = dict(zip((p.short_steamID for p in unique_list), fetched))

    # 汇总新比赛（同一群、同一场比赛的订阅玩家合并到同一个 NewMatch）
    new_matches: dict[tuple[str, int], NewMatch] = {}
    for gid, player in watched:
        result = history.get(player.short_steamID)
        if isinstance(result, Exception) or not result:
            continue
        if result == player.last_DOTA2_match_ID:
            continue
        match = new_matches.setdefault((gid, result), NewMatch(gid=gid, match_id=result))
        match.players.append(player)

    if not new_matches:
        return

    # 阶段二：并发获取每场新比赛的详情
    match_ids = {m.match_id for m in new_matches.values()}
    infos = await asyncio.gather(
        *(request_match_info_opendota(mid) for mid in match_ids),
        return_exceptions=True,
    )
    info_map = dict(zip(match_ids, infos))

    changed = False
    for match in new_matches.values():
        match.match_info = info_map.get(match.match_id)
        if not match.has_valid_info():
            continue
        for player in match.players:
            player.last_DOTA2_match_ID = match.match_id
        changed = True
        # 自定义/活动等模式不播报
        if match.match_info.get("game_mode") in config.d2w_game_mode:
            continue
        await _report_match(match)

    if changed:
        store.save()
