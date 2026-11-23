"""业务编排：命令与定时任务共用的逻辑，不依赖 NoneBot matcher。

群播报与新闻去重分别见 broadcast.py / news.py。
"""

import asyncio
from dataclasses import dataclass, field

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger

from ..config import config, is_group_allowed, normalize_image_theme
from ..datasources import d2pt, pro_names, stratz, team_roster, ti_results
from ..datasources.request_match import (
    SOURCE_STEAM,
    SOURCE_XIAOHEIHE,
    request_match_info,
)
from ..datasources.xiaoheihe_overview import OverviewError
from ..generators import core_build, d2pt_ranking, hero_pool, match_builder, turbo_ranking
from ..generators import playmates as playmates_gen
from ..generators import pro_peers as pro_peers_gen
from ..generators import xiaoheihe_overview as xiaoheihe_gen
from ..utils import player_team, run_single_flight
from . import store
from .broadcast import broadcast_text, broadcast_text_to_groups, send_group_message
from .match_history import RecentMatch, fetch_recent_match, latest_known_match_id
from .news import sync_baseline
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
    """开启/关闭某玩家（或全体）的播报；返回提示文案。"""
    reply = store.set_display(str(group_id), player_name, display)
    store.save()
    return reply


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
    sync_baseline(key)
    return f"已{'开启' if enabled else '关闭'}{name}订阅\n" + subscription_status(group_id)


def set_subscription(group_id, key: str, enabled: bool) -> str:
    """将某类订阅设为指定开关状态，并返回含全部订阅状态的提示文案。"""
    name = _SUBSCRIPTIONS[key][0]
    enabled = store.set_subscription(str(group_id), key, bool(enabled))
    store.save()
    sync_baseline(key)
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


async def d2pt_report(theme: str | None = None, pos: str | None = None) -> Message:
    """D2PT 排行图片。pos 为空时输出五列总览，否则输出该位置全部英雄。

    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    """
    theme = normalize_image_theme(theme)
    pos_key = str(pos) if pos is not None else None

    async def _build() -> Message:
        try:
            # 默认走缓存，避免每次触发都重新拉取
            posdata = await d2pt.load_data(force_update=False)
        except Exception:
            logger.exception("d2pt 数据加载失败")
            return Message("d2pt读取数据失败")
        if not posdata:
            return Message("d2pt读取数据失败")

        try:
            path = await d2pt_ranking.generate_image(posdata, theme=theme, pos=pos_key)
        except Exception:
            logger.exception("d2pt 排行图生成失败")
            return Message("d2pt图片生成失败")
        if not path:
            return Message("d2pt读取数据失败")
        return Message(MessageSegment.image(file=path))

    return await run_single_flight(("d2pt", pos_key, theme), _build)


async def turbo_report(theme: str | None = None) -> Message:
    """加速模式英雄胜率图片（按胜率降序）。

    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    """
    theme = normalize_image_theme(theme)

    async def _build() -> Message:
        try:
            data = await d2pt.load_turbo_data()
        except Exception:
            logger.exception("加速模式数据加载失败")
            return Message("加速模式数据读取失败")
        if not data:
            return Message("加速模式数据读取失败")

        try:
            path = await turbo_ranking.generate_image(data, theme=theme)
        except Exception:
            logger.exception("加速模式胜率图生成失败")
            return Message("加速模式图片生成失败")
        if not path:
            return Message("加速模式数据读取失败")
        return Message(MessageSegment.image(file=path))

    return await run_single_flight(("turbo", theme), _build)


async def report_image(match_id: str) -> str:
    """生成开黑战报图片，返回本地路径；失败返回空串。相同比赛的并发查询共享同一次执行。"""

    async def _build() -> str:
        result = await match_builder.generate_report_img(match_id, force=True)
        return result or ""

    return await run_single_flight(("report", match_id), _build)


async def _render_cached_image(key, label: str, render, *, user_errors=()) -> str:
    """single-flight 渲染图片：领域错误转为提示文案，其余异常记日志后返回空串。"""

    async def _build() -> str:
        try:
            return await render() or ""
        except user_errors as e:
            raise ValueError(str(e)) from e
        except Exception:
            logger.exception(f"{label}生成失败")
            return ""

    return await run_single_flight(key, _build)


async def build_image(hero: str, position=None, theme: str | None = None) -> str:
    """生成核心出装图片，返回本地路径；失败返回空串。相同参数的并发查询共享同一次执行。

    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    """
    theme = normalize_image_theme(theme)
    return await _render_cached_image(
        ("build", hero, position, theme),
        "出装图",
        lambda: core_build.generate_image(hero, position, theme=theme),
    )


async def ti_image(stage: str = "auto") -> str:
    """生成 TI 赛事战报图片，返回本地路径；失败返回空串。

    stage 取值 auto/swiss/elimination_round/main_event，传 auto 表示自动判断当前（最新）阶段。
    相同阶段的并发查询共享同一次执行。
    """
    return await _render_cached_image(
        ("ti", stage),
        "TI 战报",
        lambda: ti_results.generate_league_report_image(stage=stage),
    )


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
    """生成英雄池环形图；失败返回空串，参数 / Token 错误抛 ValueError。

    arg 为 steam_id 或本群昵称；size 取 min/mid/max 或小/中/大（默认 25 场）。
    """
    steam_id = _resolve_steam_id(group_id, arg)
    count = _HERO_POOL_SIZES.get(size.strip().lower(), 25)
    theme = normalize_image_theme(theme)
    # 相同账号 + 相同档位 + 相同风格的并发查询共享同一次执行
    return await _render_cached_image(
        ("hero_pool", steam_id, count, theme),
        "英雄池",
        lambda: hero_pool.generate_image(steam_id, count=count, theme=theme),
        user_errors=(stratz.StratzError,),
    )


async def pro_image(group_id, arg: str, theme: str | None = None) -> str:
    """生成与职业选手的对战记录图（前 10）；失败返回空串，参数错误抛 ValueError。

    arg 为 steam_id 或本群昵称。
    """
    theme = normalize_image_theme(theme)
    steam_id = _resolve_steam_id(group_id, arg)
    return await _render_cached_image(
        ("pro_peers", steam_id, theme),
        "职业选手对战记录",
        lambda: pro_peers_gen.generate_image(steam_id, theme=theme),
        user_errors=(stratz.StratzError,),
    )


async def playmates_image(group_id, arg: str, theme: str | None = None) -> str:
    """生成开黑记录图（前 20）；失败返回空串，参数错误抛 ValueError。

    arg 为 steam_id 或本群昵称。
    """
    theme = normalize_image_theme(theme)
    steam_id = _resolve_steam_id(group_id, arg)
    return await _render_cached_image(
        ("playmates", steam_id, theme),
        "开黑记录",
        lambda: playmates_gen.generate_image(steam_id, theme=theme),
        user_errors=(stratz.StratzError,),
    )


async def xiaoheihe_overview_image(group_id, arg: str, theme: str | None = None) -> str:
    """生成小黑盒最近战绩图；失败返回空串，查不到玩家时抛 ValueError。

    数据与图片均按 d2w_xiaoheihe_cache_seconds 缓存。
    """
    theme = normalize_image_theme(theme)
    steam_id = _resolve_steam_id(group_id, arg)
    return await _render_cached_image(
        ("xiaoheihe_overview", steam_id, theme),
        "小黑盒战绩图",
        lambda: xiaoheihe_gen.generate_image(steam_id, theme=theme),
        user_errors=(OverviewError,),
    )


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
    added = [
        team_roster.team_name(t) for t in team_roster.CN_TEAMS if store.add_team(str(group_id), t)
    ]
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
    # 连胜/连败用于锐评，按本局结果就地累加
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
        # 复用阶段二的详情；定时播报不补充匿名玩家数据
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
    await send_group_message(match.gid, msg, log_prefix="发送战报")


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
    await broadcast_text(msg, "subscribe_ti")


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

    # 阶段一会改动隐藏计数，先记录初值用于判断是否落盘
    hidden_before = [(p, p.hidden_fail_count) for p in {id(p): p for _, p in watched}.values()]

    # 阶段一：并发拉取最近比赛（Steam 优先，隐藏战绩按门控回退小黑盒）
    fetched = await asyncio.gather(
        *(_fetch_history(player) for player in unique_list),
        return_exceptions=True,
    )
    history = dict(zip((p.short_steamID for p in unique_list), fetched))

    # 同一账号在各群是不同实例，把门控状态同步给其余实例；小黑盒 ID 取较大者
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
    # 本群已播报过的比赛：不重播，但仍要补记胜负
    quiet: list[tuple[Player, RecentMatch]] = []
    for gid, player in watched:
        result = history.get(player.short_steamID)
        # 只处理比已知最新更新的比赛，挡掉小黑盒滞后返回的旧比赛
        if isinstance(result, Exception) or not result:
            continue
        if result.match_id <= latest_known_match_id(player):
            continue
        # 群维度去重：同群任一人播过即跳过，但这一场仍要计入该玩家连胜/连败
        if result.match_id <= store.get_last_reported_match_id(gid):
            quiet.append((player, result))
            continue
        match = new_matches.setdefault(
            (gid, result.match_id),
            NewMatch(gid=gid, match_id=result.match_id, source=result.source),
        )
        match.players.append(player)

    # 隐藏计数可能已变化，即使无新比赛也要落盘
    hidden_changed = any(p.hidden_fail_count != count for p, count in hidden_before)

    if not new_matches and not quiet:
        if hidden_changed:
            store.save()
        return

    # 阶段二：按（比赛 ID, 数据源）并发拉取详情；两源数据不等价，不能共用
    wanted: dict[tuple[int, str], None] = {}
    for match in new_matches.values():
        wanted[(match.match_id, match.source)] = None
    for _, result in quiet:
        wanted.setdefault((result.match_id, result.source), None)
    infos = await asyncio.gather(
        *(request_match_info(mid, src) for mid, src in wanted),
        return_exceptions=True,
    )
    info_map = dict(zip(wanted, infos))

    changed = False
    for match in new_matches.values():
        match.match_info = info_map.get((match.match_id, match.source))
        if not match.has_valid_info():
            continue
        # 先推进群级水位，避免播报异常时下一轮重播
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

    # 被跳过的比赛只推进水位并累加纪录，不发消息
    for player, result in quiet:
        info = info_map.get((result.match_id, result.source))
        if not isinstance(info, dict) or "radiant_win" not in info:
            continue
        player.last_DOTA2_match_ID = result.match_id
        if result.source == SOURCE_XIAOHEIHE:
            player.last_xiaoheihe_match_id = result.match_id
        changed = True
        # 与播报链路保持一致：自定义/活动等模式既不出战报，也不计入连胜/连败
        if info.get("game_mode") in config.d2w_game_mode:
            continue
        update_streak(
            player,
            _player_win(info, player.short_steamID),
            int(info.get("match_id") or result.match_id or 0),
        )

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
            await broadcast_text_to_groups(msg, subs[team_id])
    if changed:
        team_roster.save_state(state)
