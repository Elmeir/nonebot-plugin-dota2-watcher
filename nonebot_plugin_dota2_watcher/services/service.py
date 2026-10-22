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

from ..config import DATA_DIR, config, is_group_allowed, normalize_image_theme
from ..datasources import d2pt, pro_names, team_roster, ti_results
from ..datasources.hero_pool import HeroPoolError
from ..datasources.playmates import PlaymatesError
from ..datasources.pro_peers import ProPeersError
from ..datasources.request_match import (
    SOURCE_STEAM,
    SOURCE_XIAOHEIHE,
    request_match_info,
    request_news,
)
from ..datasources.xiaoheihe_overview import OverviewError
from ..generators import core_build, hero_pool, match_builder
from ..generators import playmates as playmates_gen
from ..generators import pro_peers as pro_peers_gen
from ..generators import xiaoheihe_overview as xiaoheihe_gen
from ..utils import load_cache, player_team, run_single_flight
from . import store
from .match_history import fetch_recent_match, latest_known_match_id
from .player import Player


@dataclass
class NewMatch:
    """一场待处理的新比赛：所属群 + 比赛 ID + 订阅玩家 + 比赛详情。

    match_info 在阶段二拉取后填充，供战报文本与图片生成复用，避免重复请求数据源。
    source 记录比赛 ID 的来源，决定阶段二该走哪个详情接口（见 request_match_info）。
    """

    gid: str
    match_id: int
    players: list[Player] = field(default_factory=list)
    match_info: dict | None = None
    source: str = SOURCE_STEAM

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


async def build_image(hero: str, position=None, theme: str | None = None) -> str:
    """生成核心出装图片，返回本地路径；失败返回空串。相同参数的并发查询共享同一次执行。

    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    """
    theme = normalize_image_theme(theme)

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


def _resolve_steam_id(group_id, arg: str) -> int:
    """把「steam_id 或本群订阅玩家昵称」解析成 steam_id。

    优先按昵称匹配，因为昵称可能是数字，会与 steam_id 混淆；
    都不是时抛 ValueError（提示文案）。
    """
    arg = (arg or "").strip()
    player = next(
        (p for p in store.get_group(str(group_id)) if p.nickname == arg),
        None,
    )
    if player is not None:
        return int(player.short_steamID)
    if arg.isdigit():
        return int(arg)
    raise ValueError(
        f"未找到昵称为「{arg}」的订阅玩家，请先用 /添加刀塔玩家 订阅，或直接输入 steam_id"
    )


async def hero_pool_image(group_id, arg: str, size: str = "", theme: str | None = None) -> str:
    """生成玩家英雄池环形图，返回本地图片路径。

    arg 可为 steam_id（纯数字）或本群已订阅玩家昵称；
    size 为比赛数量档位（min/mid/max 或 小/中/大，留空默认 25 场）；
    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    参数解析失败 / 未配置 Token 时抛出 ValueError（提示文案），生成失败返回空串。
    """
    steam_id = _resolve_steam_id(group_id, arg)
    count = _HERO_POOL_SIZES.get(size.strip().lower(), 25)
    theme = normalize_image_theme(theme)

    async def _build() -> str:
        try:
            return await hero_pool.generate_image(steam_id, count=count, theme=theme) or ""
        except HeroPoolError as e:
            raise ValueError(str(e))
        except Exception:
            logger.exception("英雄池生成失败")
            return ""

    # 相同账号 + 相同档位 + 相同风格的并发查询共享同一次执行
    return await run_single_flight(("hero_pool", steam_id, count, theme), _build)


async def pro_image(group_id, arg: str, theme: str | None = None) -> str:
    """生成玩家与职业选手的对战记录图片（按队友+对手总场次降序取前 10）。

    arg 可为 steam_id（纯数字）或本群已订阅玩家昵称；
    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    参数解析失败 / 未配置 Token 时抛出 ValueError（提示文案），生成失败返回空串。
    相同账号 + 相同风格的并发查询通过 single-flight 共享同一次执行（不重复抓取）。
    """
    theme = normalize_image_theme(theme)
    steam_id = _resolve_steam_id(group_id, arg)

    async def _build() -> str:
        try:
            return await pro_peers_gen.generate_image(steam_id, theme=theme) or ""
        except ProPeersError as e:
            raise ValueError(str(e))
        except Exception:
            logger.exception("职业选手对战记录生成失败")
            return ""

    return await run_single_flight(("pro_peers", steam_id, theme), _build)


async def playmates_image(group_id, arg: str, theme: str | None = None) -> str:
    """生成玩家开黑记录图片（最常一起开黑的队友，按共同场次降序取前 20）。

    arg 可为 steam_id（纯数字）或本群已订阅玩家昵称；
    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    参数解析失败 / 未配置 Token 时抛出 ValueError（提示文案），生成失败返回空串。
    相同账号的并发查询通过 single-flight 共享同一次执行（不重复抓取）。
    """
    theme = normalize_image_theme(theme)
    steam_id = _resolve_steam_id(group_id, arg)

    async def _build() -> str:
        try:
            return await playmates_gen.generate_image(steam_id, theme=theme) or ""
        except PlaymatesError as e:
            raise ValueError(str(e))
        except Exception:
            logger.exception("开黑记录生成失败")
            return ""

    return await run_single_flight(("playmates", steam_id, theme), _build)


async def xiaoheihe_overview_image(group_id, arg: str, theme: str | None = None) -> str:
    """生成小黑盒「最近战绩」图片（玩家卡 / 近20场 / 比赛列表 / 常用英雄 / 生涯纪录）。

    arg 可为 steam_id（纯数字）或本群已订阅玩家昵称；
    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    数据与生成图均按 d2w_xiaoheihe_cache_seconds（默认 1 小时）缓存。
    参数解析失败 / 查不到玩家时抛出 ValueError（提示文案），生成失败返回空串。
    相同账号 + 相同风格的并发查询通过 single-flight 共享同一次执行。
    """
    theme = normalize_image_theme(theme)
    steam_id = _resolve_steam_id(group_id, arg)

    async def _build() -> str:
        try:
            return await xiaoheihe_gen.generate_image(steam_id, theme=theme) or ""
        except OverviewError as e:
            raise ValueError(str(e))
        except Exception:
            logger.exception("小黑盒战绩图生成失败")
            return ""

    return await run_single_flight(("xiaoheihe_overview", steam_id, theme), _build)


# ---------------------------------------------------------------
# 战队名单订阅（成员加入 / 离开播报）
# ---------------------------------------------------------------
async def add_team(group_id, query: str) -> str:
    """订阅战队名单变动；返回提示文案。

    query 可为队名/缩写（如 XG、Team Liquid）、team_id，或 CN / china / 中国
    （一键订阅全部中国战队）。
    """
    if team_roster.is_cn_query(query):
        return await add_cn_teams(group_id)
    team_id = team_roster.resolve_team(query)
    if team_id is None:
        return f"未找到战队「{query}」，可输入队名（如 XG、Team Liquid）或 team_id"
    name = team_roster.team_name(team_id)
    if not store.add_team(str(group_id), team_id):
        return f"本群已订阅 {name}"
    store.save()
    return f"已订阅 {name}（team_id={team_id}）\n名单变动时将播报成员加入/离开\n" + list_teams(
        group_id
    )


def list_teams(group_id) -> str:
    """返回本群订阅战队列表文案。"""
    teams = store.get_teams(str(group_id))
    if not teams:
        return "当前群组没有订阅任何战队"
    lines = [f"{team_roster.team_name(t)}（{t}）" for t in teams]
    return "本群订阅战队：\n" + "\n".join(lines)


async def add_cn_teams(group_id) -> str:
    """一键订阅全部中国战队（CN_TEAMS）；返回提示文案。"""
    added = [team_roster.team_name(t) for t in team_roster.CN_TEAMS if store.add_team(str(group_id), t)]
    if added:
        store.save()
        return (
            f"已订阅 {len(added)} 支中国战队：{'、'.join(added)}\n"
            "名单变动时将播报成员加入/离开\n" + list_teams(group_id)
        )
    return "本群已订阅全部中国战队\n" + list_teams(group_id)


def delete_team(group_id, query: str) -> str:
    """取消订阅战队；返回提示文案。

    query 可为队名/缩写（如 XG、Team Liquid）、team_id，或 CN / china / 中国
    （一键取消订阅全部中国战队）。
    """
    if team_roster.is_cn_query(query):
        return delete_cn_teams(group_id)
    team_id = team_roster.resolve_team(query)
    if team_id is None:
        return f"未找到战队「{query}」"
    name = team_roster.team_name(team_id)
    if not store.remove_team(str(group_id), team_id):
        return f"本群未订阅 {name}"
    store.save()
    return f"已取消订阅 {name}"


def delete_cn_teams(group_id) -> str:
    """一键取消订阅全部中国战队（CN_TEAMS）；返回提示文案。"""
    removed = [
        team_roster.team_name(t)
        for t in team_roster.CN_TEAMS
        if store.remove_team(str(group_id), t)
    ]
    if removed:
        store.save()
        return f"已取消订阅 {len(removed)} 支中国战队：{'、'.join(removed)}\n" + list_teams(
            group_id
        )
    return "本群未订阅任何中国战队\n" + list_teams(group_id)


async def roster_report(query: str) -> str:
    """查询战队当前登记名单（文本）。参数解析失败时抛出 ValueError。

    query 为 CN / china / 中国 时返回全部中国战队名单（每队一行、不带选手 ID）。
    """
    query = (query or "").strip()
    key = team_roster.steam_key()
    if not key:
        raise ValueError("未配置 Steam Web API Key，无法查询战队名单")

    if team_roster.is_cn_query(query):
        return await _cn_rosters_report(key)

    team_id = team_roster.resolve_team(query)
    if team_id is None:
        raise ValueError(f"未找到战队「{query}」，可输入队名（如 XG）或 team_id")
    name, members = await team_roster.members(team_id, key)
    if not members:
        return f"{name} 暂未在 DOTA 客户端登记阵容"
    lines = [f"{name} 登记名单（{len(members)}人）："]
    lines.extend(f"{n}（{aid}）" for aid, n in members)
    return "\n".join(lines)


async def _cn_rosters_report(key: str) -> str:
    """全部中国战队的名单：每队一行，成员间用顿号连接（不带选手 ID）。"""
    lines = ["中国战队名单："]
    for team_id in team_roster.CN_TEAMS:
        name, members = await team_roster.members(team_id, key)
        if not members:
            lines.append(f"{name}：暂未登记阵容")
            continue
        joined = "、".join(n for _, n in members)
        lines.append(f"{name}（{len(members)}人）：{joined}")
    return "\n".join(lines)


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
        # 白名单/黑名单模式外的群不播报
        if not is_group_allowed(gid):
            continue
        if filter_key and not info.get(filter_key, True):
            continue
        for bot in bots.values():
            try:
                await bot.send_group_msg(group_id=int(gid), message=msg)
            except Exception:
                logger.exception(f"广播消息到群 {gid} 失败")


async def _broadcast_groups(text: str | None, gids) -> None:
    """向指定的若干群广播一条文本消息（用于按战队订阅关系定向播报）。"""
    if not text:
        return
    bots = get_bots()
    if not bots:
        return
    msg = Message(f"[DOTA2]{text}")
    for gid in gids:
        if not is_group_allowed(gid):
            continue
        for bot in bots.values():
            try:
                await bot.send_group_msg(group_id=int(gid), message=msg)
            except Exception:
                logger.exception(f"广播战队名单变动到群 {gid} 失败")


async def _fetch_history(player: Player):
    """获取玩家最近一场比赛（Steam 优先，战绩被隐藏时按门控回退小黑盒）。

    返回 RecentMatch；网络错误等异常在本层记日志后返回 None，
    由调用方按「本轮没有可播报的比赛」处理。
    """
    try:
        return await fetch_recent_match(player, config.d2w_steam_api_key)
    except Exception as e:
        logger.warning(f"获取 {player.nickname} 最近比赛失败: {e}")
        return None


def update_streak(player: Player, win: bool | None, match_id: int = 0) -> tuple[int, int]:
    """按本局胜负就地累加玩家的连胜 / 连败纪录，返回 (连胜, 连败)。

    连胜与连败二者必有一个为 0；胜负未知（详情缺字段）时不改动纪录。
    纪录随战报逐场累加，不额外请求数据源（见 _report_match）。

    同一玩家可能被多个群订阅，同一场比赛会被处理多次（各群各自播报），
    因此用 match_id 去重：同一场只累加一次，避免连胜/连败翻倍。
    match_id 为 0（详情缺 ID）时不做去重，仍按胜负累加。
    """
    if win is None:
        return player.win_streak, player.lose_streak
    if match_id and player.streak_match_id == int(match_id):
        return player.win_streak, player.lose_streak
    if win:
        player.win_streak += 1
        player.lose_streak = 0
    else:
        player.lose_streak += 1
        player.win_streak = 0
    if match_id:
        player.streak_match_id = int(match_id)
    return player.win_streak, player.lose_streak


def _player_win(match_info: dict, account_id: int) -> bool | None:
    """从比赛详情判断某玩家是否获胜；找不到该玩家或缺少结果字段时返回 None。"""
    radiant_win = match_info.get("radiant_win")
    if radiant_win is None:
        return None
    for info in match_info.get("players") or []:
        if info.get("account_id", 0) == account_id:
            team = player_team(info)
            if team is None:
                return None
            return bool(radiant_win) if team == 0 else not bool(radiant_win)
    return None


def _report_streaks(player_list: list, match_info: dict) -> dict[int, tuple[int, int]]:
    """按本局胜负累加各玩家的连胜 / 连败纪录，返回 {steam_id: (连胜, 连败)}。

    复用战报已拉取的比赛详情，不额外请求数据源；纪录就地写回 Player，
    由轮询末尾的 store.save() 一并持久化。
    """
    streaks: dict[int, tuple[int, int]] = {}
    for player in player_list:
        win = _player_win(match_info, player.short_steamID)
        streaks[player.short_steamID] = update_streak(player, win, match_info.get("match_id") or 0)
    return streaks


async def _report_match(match: NewMatch) -> None:
    """生成并发送一场比赛的战报（图片 + 一句话锐评）。"""
    # 连胜/连败是锐评的一个维度：按本局结果就地累加纪录（不额外抓取数据）
    try:
        streaks = _report_streaks(match.players, match.match_info)
    except Exception:
        logger.exception(f"连胜/连败纪录更新失败: {match.match_id}")
        streaks = {}

    try:
        text = match_builder.generate_message(match.match_info, match.players, streaks)
    except Exception:
        logger.exception(f"生成战报文本失败: {match.match_id}")
        text = None

    pic = None
    try:
        # 复用阶段二已拉取的 match_info，避免图片生成时再次请求 OpenDota；
        # 定时播报不接入小黑盒补充匿名玩家数据，避免额外请求拖慢播报
        pic = await match_builder.generate_report_img(
            match.match_id, force=True, match_data=match.match_info, supplement_anonymous=False
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
        # 白名单/黑名单模式外的群不轮询、不播报
        if player.display_recent_match and is_group_allowed(gid)
    ]
    if not watched:
        return

    # 按 steam_id 去重，同一账号只拉取一次比赛历史
    unique_players: dict[int, Player] = {}
    for _, player in watched:
        unique_players.setdefault(player.short_steamID, player)
    unique_list = list(unique_players.values())

    # 阶段一会就地改动「战绩隐藏次数」（累加 / 归零），先记录初值，
    # 便于本轮结束时判断是否需要落盘（见末尾 store.save）。
    # 按对象身份去重：同一账号在多个群各有实例，两处都会被改动。
    hidden_before = [(p, p.hidden_fail_count) for p in {id(p): p for _, p in watched}.values()]

    # 阶段一：并发获取去重后玩家的最近比赛 ID
    # （Steam 优先；战绩被隐藏且累计到阈值时回退小黑盒 overview，见 match_history）
    fetched = await asyncio.gather(
        *(_fetch_history(player) for player in unique_list),
        return_exceptions=True,
    )
    history = dict(zip((p.short_steamID for p in unique_list), fetched))

    # 同一账号被多个群订阅时，Player 是各群独立的实例，而阶段一只更新了参与抓取的
    # 那个实例；这里把门控状态同步给同账号的其它实例，避免各群计数不一致
    # （否则代表实例被删掉后计数会「凭空归零」，或各群落盘的值互相矛盾）。
    # 小黑盒 ID 取各实例的较大者：它只应单调前进，不该因某群曾关闭播报而落后。
    for _, player in watched:
        source = unique_players.get(player.short_steamID)
        if source is None or source is player:
            continue
        player.hidden_fail_count = source.hidden_fail_count
        player.last_xiaoheihe_match_id = max(
            player.last_xiaoheihe_match_id, source.last_xiaoheihe_match_id
        )

    # 汇总新比赛（同一群、同一场比赛的订阅玩家合并到同一个 NewMatch）
    new_matches: dict[tuple[str, int], NewMatch] = {}
    for gid, player in watched:
        result = history.get(player.short_steamID)
        # 只播报「比已知最新更新」的比赛：两个数据源的最近一场并不同步，
        # 小黑盒可能滞后返回已播报过的旧比赛，靠 ID 比较挡掉（见 is_newer_match）
        if isinstance(result, Exception) or not result:
            continue
        if result.match_id <= latest_known_match_id(player):
            continue
        # 群维度去重：同一场比赛只要有本群任一玩家播过，就不再重播。
        # 玩家维度的 last_DOTA2_match_ID 挡不住这种重复 —— 战绩被隐藏的玩家要等
        # 门控攒够轮次才回退小黑盒，等它拿到 ID 时，同群的公开玩家早已把这场播完了
        # （那时该隐藏玩家的 last_DOTA2_match_ID 还是 0，看起来仍是"新比赛"）。
        if result.match_id <= store.get_last_reported_match_id(gid):
            continue
        match = new_matches.setdefault(
            (gid, result.match_id),
            NewMatch(gid=gid, match_id=result.match_id, source=result.source),
        )
        match.players.append(player)

    # 战绩隐藏次数也会变化（累加 / 归零），即使本轮没有新比赛也要落盘，
    # 否则重启后门控计数丢失，可能刚启动就立刻访问小黑盒
    hidden_changed = any(p.hidden_fail_count != count for p, count in hidden_before)

    if not new_matches:
        if hidden_changed:
            store.save()
        return

    # 阶段二：并发获取每场新比赛的详情。
    # 比赛 ID 来自小黑盒时必须用小黑盒详情接口：这类玩家的战绩在 OpenDota 里是匿名的，
    # 拿 OpenDota 数据会定位不到本人（详见 request_match_info）。
    # 同一场比赛被多个群订阅时只拉取一次，因此按比赛 ID 去重。
    sources = {m.match_id: m.source for m in new_matches.values()}
    infos = await asyncio.gather(
        *(request_match_info(mid, src) for mid, src in sources.items()),
        return_exceptions=True,
    )
    info_map = dict(zip(sources, infos))

    changed = False
    for match in new_matches.values():
        match.match_info = info_map.get(match.match_id)
        if not match.has_valid_info():
            continue
        # 先记下群级已播报水位：即使本轮播报过程出问题，也不会在下一轮重播同一场。
        # 只增不减，旧比赛（小黑盒滞后下发）把水位拉低反而会造成重播。
        store.set_last_reported_match_id(match.gid, match.match_id)
        for player in match.players:
            player.last_DOTA2_match_ID = match.match_id
            if match.source == SOURCE_XIAOHEIHE:
                # 记录小黑盒给到的 ID：小黑盒数据可能滞后，留作跨数据源判旧的基准
                player.last_xiaoheihe_match_id = match.match_id
        changed = True
        # 自定义/活动等模式不播报
        if match.match_info.get("game_mode") in config.d2w_game_mode:
            continue
        await _report_match(match)

    if changed or hidden_changed:
        store.save()


def _roster_change_message(team: str, added: list[int], removed: list[int], names: dict) -> str:
    """拼装名单变动播报文案。names 为 {account_id: 显示名}。

    added / removed 需已按最近比赛时间降序排好（与 /战队 展示顺序一致）。
    """
    parts = []
    if added:
        parts.append("加入：" + "、".join(names.get(i, str(i)) for i in added))
    if removed:
        parts.append("离开：" + "、".join(names.get(i, str(i)) for i in removed))
    if not parts:
        return ""
    return f"{team} 名单变动\n" + "\n".join(parts)


async def poll_roster_changes() -> None:
    """轮询已订阅战队的登记名单，成员加入/离开时向订阅群播报。

    首次观察到某支队伍只建立基线、不播报（与新闻/TI 播报一致）；
    名单来源为 Valve 客户端登记值，含替补/教练，且存在跨队残留，故"离开"可能滞后。
    """
    # 按队聚合订阅群，同一支队伍只拉取一次；白/黑名单外的群不参与
    subs = store.subscribed_teams(is_group_allowed)
    if not subs:
        return
    key = team_roster.steam_key()
    if not key:
        return

    state = team_roster.load_state()
    teams_state = state["teams"]
    changed = False
    for team_id in sorted(subs):
        try:
            info = await team_roster.fetch_team(team_id, key)
        except Exception:
            logger.warning(f"战队 {team_id} 名单拉取失败")
            continue
        if not info:
            continue
        prev = teams_state.get(str(team_id))
        if prev is None:
            # 首次观察：仅建立基线，避免订阅瞬间补播历史名单
            teams_state[str(team_id)] = {"name": info["name"], "ids": info["ids"]}
            changed = True
            continue
        old_ids = set(prev.get("ids") or [])
        new_ids = set(info["ids"])
        if old_ids == new_ids:
            continue
        added, removed = new_ids - old_ids, old_ids - new_ids
        teams_state[str(team_id)] = {"name": info["name"], "ids": info["ids"]}
        changed = True
        names = await pro_names.display_names(ids=sorted(added | removed))
        msg = _roster_change_message(info["name"], sorted(added), sorted(removed), names)
        if msg:
            await _broadcast_groups(msg, subs[team_id])
    if changed:
        team_roster.save_state(state)
