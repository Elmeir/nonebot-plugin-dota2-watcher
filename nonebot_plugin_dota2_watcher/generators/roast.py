"""阴阳怪气（锐评）生成：按多个维度判定，为每位玩家单独挑一句。

设计要点：
- **多维度判定**：数据源评分（小黑盒综合分 / OpenDota benchmark）只是众多维度
  之一，不再是唯一依据；KDA、阵亡、补刀、经济、输出、参团、人头、连胜连败、
  英雄梗、比赛时长等都可独立命中。
- **按权重随机**：命中的维度各自带权重，权重越高越容易被选中；选定维度后再从
  该维度的句库中随机取一句，从而让语句分支足够多、不总是同一类腔调。
- **逐人评价**：同局多位订阅玩家各自独立判定（队伍数据按各自所在阵营计算），
  不做平均，返回每人一行。
- **加速模式折算**：加速模式（game_mode=23）同样的真实时长里，等级 / 金钱的推进量
  约为普通模式的两倍，因此「膀胱局 / 速通局」按等效普通模式时长判定；而补刀数按
  真实分钟与普通模式基本持平（实测约 1.07 倍），故补刀效率仍用真实时长。
"""

import random

from ..config import config
from ..dota_dicts import HEROES_LIST_CHINESE, MEME_HEROES, ROAST_LINES
from ..utils import player_team

# OpenDota benchmark 中不参与评价的字段（与旧版保持一致）
EXCLUDED_BENCHMARKS = {
    "hero_healing_per_min",
    "tower_damage",
    "denies_per_min",
    "last_hits_per_min",
}

# 达到多少连胜/连败才会作为锐评分支参与随机
STREAK_MIN = 3

# 加速模式（dota_dicts.GAME_MODE[23] = "加速模式"）
TURBO_MODE = 23
# 小黑盒兜底数据源不返回 game_mode，只给原始中文模式文本（如"加速模式"）
TURBO_MODE_DESC_KEYWORD = "加速"
# 加速模式的进度倍率：同样的真实时长里，等级 / 金钱的推进量约为普通模式的两倍
# （实测 median 比值：等级/分钟 2.01、GPM 2.60、每次正补金钱 1.74；
#   而正补/分钟 1.07 基本持平，故补刀效率仍按真实时长判定）
TURBO_PROGRESS_MULTIPLIER = 2


def is_turbo(match_info: dict) -> bool:
    """是否加速模式。

    优先看 OpenDota 的 game_mode；小黑盒兜底数据源没有该字段，退而匹配
    mode_desc 里的中文模式名（见 datasources/xiaoheihe.py）。
    """
    if match_info.get("game_mode") == TURBO_MODE:
        return True
    if match_info.get("game_mode") is not None:
        return False
    return TURBO_MODE_DESC_KEYWORD in str(match_info.get("mode_desc") or "")


def _kda_of(info: dict) -> float:
    """从 OpenDota 原始玩家数据算 KDA。"""
    kills = info.get("kills", 0) or 0
    deaths = info.get("deaths", 0) or 0
    assists = info.get("assists", 0) or 0
    return (kills + assists) / max(deaths, 1)


# 没有数据源评分时，用于「本局全局 10 人横向比较」的指标：
# (展示名, 取值函数, 是否越大越好)
_PEER_METRICS = (
    ("KDA", _kda_of, True),
    ("GPM", lambda p: float(p.get("gold_per_min") or 0), True),
    ("XPM", lambda p: float(p.get("xp_per_min") or 0), True),
    ("补刀", lambda p: float(p.get("last_hits") or 0), True),
    ("输出", lambda p: float(p.get("hero_damage") or 0), True),
    ("阵亡", lambda p: float(p.get("deaths") or 0), False),
)


def peer_percentiles(match_info: dict) -> dict[int, dict[str, float]]:
    """按本局全局 10 人的数据，给出每位玩家各指标的百分位（0~100，越高越亮眼）。

    没有数据源评分（小黑盒综合分 / OpenDota benchmark）时，用它替代原先的
    「按 KDA 拍脑袋 + 抛硬币」，让正负倾向判定落在真实的全局横向比较上。
    「越小越好」的指标（阵亡）会自动反向。返回值以 account_id 为键，
    只含真实 account_id 的玩家（匿名玩家的 id 为 None，无法与订阅玩家对应）。
    """
    players = [p for p in (match_info.get("players") or []) if isinstance(p, dict)]
    if len(players) < 2:
        return {}

    table: dict[int, dict[str, float]] = {}
    for name, getter, higher_better in _PEER_METRICS:
        # 比较群体是本局全部 10 人（匿名玩家也参与排名，只是不产出自己的行）
        values = [(p.get("account_id"), getter(p)) for p in players]
        n = len(values)
        for account_id, value in values:
            if account_id is None:
                continue
            if higher_better:
                better = sum(1 for _, v in values if v < value)
            else:
                better = sum(1 for _, v in values if v > value)
            table.setdefault(account_id, {})[name] = 100.0 * better / (n - 1)
    return table


def _peer_average(peer: dict[str, float] | None) -> float | None:
    """各指标百分位的均值；无数据时返回 None。"""
    if not peer:
        return None
    return sum(peer.values()) / len(peer)


# 各维度权重：越具体、越有节目效果的维度权重越高；
# 兜底维度（win_plain / lose_plain 等）权重最低，保证句子不至于太单调。
_WEIGHTS = {
    # 连胜/连败
    "streak_win": 11,
    "streak_lose": 11,
    # KDA
    "kda_god": 9,
    "kda_trash": 9,
    "kda_high": 6,
    "kda_low": 6,
    # 阵亡
    "death_feed": 8,
    "death_many": 6,
    "death_zero": 5,
    # 输出
    "dmg_carry": 8,
    "dmg_low": 8,
    "dmg_huge": 6,
    # 经济 / 补刀
    "gpm_low": 6,
    "gpm_high": 5,
    "xpm_high": 4,
    "lh_tiny": 6,
    "lh_huge": 5,
    "lh_good": 3,
    # 参团
    "teamfight_low": 6,
    "teamfight_high": 4,
    # 人头
    "kill_many": 5,
    "kill_zero": 5,
    "assist_many": 4,
    # 评分 / benchmark
    "score_high": 5,
    "score_low": 6,
    "bench_high": 4,
    "bench_low": 5,
    # 本局全局 10 人横向比较（无数据源评分时启用）
    "peer_top": 6,
    "peer_bottom": 7,
    "peer_kda_top": 6,
    "peer_kda_bottom": 6,
    # 英雄梗 / 时长
    "hero_meme": 5,
    "long_game": 3,
    "short_game": 3,
    # 兜底
    "win_solid": 3,
    "win_plain": 3,
    "lose_good": 3,
    "lose_plain": 3,
}


class _SafeDict(dict):
    """缺键时保留原占位符，避免用户新增句子写了未知占位符就整条报错。"""

    def __missing__(self, key):  # noqa: D105
        return "{" + key + "}"


def _hero_name(hero_id) -> str:
    """英雄中文名；未知 id 退化为「英雄N」。"""
    try:
        return HEROES_LIST_CHINESE.get(int(hero_id), f"英雄{hero_id}")
    except (TypeError, ValueError):
        return f"英雄{hero_id}"


def _bench_avg(benchmarks: dict | None) -> float | None:
    """OpenDota benchmark 的可用百分比均值；无有效值时返回 None。"""
    if not benchmarks:
        return None
    pcts = [
        value.get("pct")
        for name, value in benchmarks.items()
        if name not in EXCLUDED_BENCHMARKS
        and isinstance(value, dict)
        and value.get("pct") is not None
    ]
    if not pcts:
        return None
    return sum(pcts) / len(pcts)


def is_positive(
    stats: dict,
    win: bool,
    peer: dict[str, float] | None = None,
    rng=random,
) -> bool:
    """单名玩家本局表现偏正面还是负面（不再对多人取平均）。

    依次尝试小黑盒综合评分 → OpenDota benchmark → 本局全局 10 人横向比较
    （peer 为各指标百分位，见 peer_percentiles）→ KDA 经验判断。
    前两者是「同段位基准」，第三者是「本局相对水平」，都没有时才退回经验判断。
    """
    score = stats.get("xiaoheihe_score")
    if score is not None:
        return float(score) / 100 > config.d2w_benchmark_threshold

    bench = _bench_avg(stats.get("benchmarks"))
    if bench is not None:
        return bench / 100 > config.d2w_benchmark_threshold

    avg = _peer_average(peer)
    if avg is not None:
        return avg > 100 * config.d2w_benchmark_threshold

    kda = float(stats.get("kda") or 0)
    if (win and kda > 8) or (not win and kda > 6):
        return True
    if (win and kda < 4) or (not win and kda < 2):
        return False
    return rng.random() < 0.5


def team_context(match_info: dict, team_number, stats: dict) -> dict:
    """按玩家所在阵营汇总队伍数据，供伤害/参团/阵亡占比等维度使用。

    「等效时长」：加速模式（game_mode=23）同样的真实时长里推进量约为普通模式的
    两倍，因此膀胱局 / 速通局按折算后的等效普通模式时长判定，否则加速模式十几分钟
    的局会被误判成「速通局」。
    """
    players = match_info.get("players") or []
    teammates = [p for p in players if player_team(p) == team_number]
    team_damage = sum(p.get("hero_damage", 0) or 0 for p in teammates)
    team_kills = sum(p.get("kills", 0) or 0 for p in teammates)
    team_deaths = sum(p.get("deaths", 0) or 0 for p in teammates)
    radiant_win = bool(match_info.get("radiant_win"))
    win = radiant_win if team_number == 0 else not radiant_win
    duration = int(match_info.get("duration") or 0)

    turbo = is_turbo(match_info)
    # 真实时长：补刀 / 分均等「按真实分钟」的指标用它
    dur_min = max(duration // 60, 1)
    # 等效普通模式时长：等级 / 金钱等「进度」类指标用它
    eq_duration = duration * TURBO_PROGRESS_MULTIPLIER if turbo else duration
    eq_dur_min = max(eq_duration // 60, 1)

    def _rate(value: int, total: int) -> float:
        return 0.0 if not total else 100.0 * value / total

    return {
        "win": win,
        "turbo": turbo,
        "duration": duration,
        "dur_min": dur_min,
        "eq_duration": eq_duration,
        "eq_dur_min": eq_dur_min,
        "team_damage": team_damage,
        "team_kills": team_kills,
        "team_deaths": team_deaths,
        "damage_rate": _rate(int(stats.get("damage") or 0), team_damage),
        "participation": _rate(
            int(stats.get("kill") or 0) + int(stats.get("assist") or 0), team_kills
        ),
        "death_rate": _rate(int(stats.get("death") or 0), team_deaths),
    }


def evaluate_candidates(
    stats: dict, ctx: dict, streak: tuple[int, int] = (0, 0)
) -> list[tuple[str, int]]:
    """返回本局命中的全部锐评维度及其权重（纯函数，便于测试与调试）。

    未命中任何具体维度时返回空列表，由调用方回退到基础结果向语句。
    """
    hits: list[tuple[str, int]] = []
    win = ctx["win"]
    dur_min = ctx["dur_min"]
    eq_dur_min = ctx["eq_dur_min"]
    # 加速模式：等级 / 金钱的推进量约为普通模式两倍，按普通模式口径校准的阈值
    # 需要同比放大，否则几乎人人命中 gpm_high / xpm_high，把其它维度挤掉
    pace = TURBO_PROGRESS_MULTIPLIER if ctx.get("turbo") else 1

    kda = float(stats.get("kda") or 0)
    kills = int(stats.get("kill") or 0)
    deaths = int(stats.get("death") or 0)
    assists = int(stats.get("assist") or 0)
    gpm = int(stats.get("gpm") or 0)
    xpm = int(stats.get("xpm") or 0)
    lh = int(stats.get("last_hit") or 0)
    damage = int(stats.get("damage") or 0)

    win_streak, lose_streak = streak

    def hit(key: str) -> None:
        hits.append((key, _WEIGHTS.get(key, 5)))

    # 连胜 / 连败（含本局）
    if win and win_streak >= STREAK_MIN:
        hit("streak_win")
    if not win and lose_streak >= STREAK_MIN:
        hit("streak_lose")

    # KDA
    if kda >= 10:
        hit("kda_god")
    elif kda >= 6:
        hit("kda_high")
    if kda <= 0.8:
        hit("kda_trash")
    elif kda <= 1.5:
        hit("kda_low")

    # 阵亡
    if deaths == 0:
        hit("death_zero")
    elif deaths >= 8:
        hit("death_many")
    if deaths >= 5 and ctx["death_rate"] >= 30:
        hit("death_feed")

    # 补刀（按真实每分钟折算，避免快慢局不可比；时长缺失时不折算）
    # 加速模式实测正补/分钟与普通模式基本持平（约 1.07 倍），故不折算倍率
    if ctx["duration"] > 0:
        lh_per_min = lh / dur_min
        if dur_min >= 15 and lh_per_min < 2.5:
            hit("lh_tiny")
        elif lh_per_min >= 8:
            hit("lh_huge")
        elif lh_per_min >= 6:
            hit("lh_good")

    # 经济（阈值按模式节奏折算）
    if gpm and gpm <= 300 * pace:
        hit("gpm_low")
    elif gpm >= 700 * pace:
        hit("gpm_high")
    if xpm >= 800 * pace:
        hit("xpm_high")

    # 输出
    if damage >= 50000:
        hit("dmg_huge")
    if ctx["damage_rate"] >= 35:
        hit("dmg_carry")
    elif ctx["damage_rate"] <= 10:
        hit("dmg_low")

    # 参团
    if ctx["participation"] <= 30:
        hit("teamfight_low")
    elif ctx["participation"] >= 75:
        hit("teamfight_high")

    # 人头 / 助攻
    if kills >= 12:
        hit("kill_many")
    elif kills == 0:
        hit("kill_zero")
    if assists >= 20:
        hit("assist_many")

    # 数据源评分（仅作维度之一）
    score = stats.get("xiaoheihe_score")
    if score is not None:
        if float(score) >= 85:
            hit("score_high")
        elif float(score) <= 40:
            hit("score_low")
    bench = _bench_avg(stats.get("benchmarks"))
    if bench is not None:
        if bench >= 80:
            hit("bench_high")
        elif bench <= 20:
            hit("bench_low")

    # 没有数据源评分时，用本局全局 10 人的横向比较顶上（见 peer_percentiles）
    peer = ctx.get("peer") or {}
    if score is None and bench is None and peer:
        avg = _peer_average(peer)
        if avg is not None:
            if avg >= 75:
                hit("peer_top")
            elif avg <= 25:
                hit("peer_bottom")
        if peer.get("KDA", 50) >= 90:
            hit("peer_kda_top")
        elif peer.get("KDA", 50) <= 10:
            hit("peer_kda_bottom")

    # 英雄梗
    try:
        if int(stats.get("hero")) in MEME_HEROES:
            hit("hero_meme")
    except (TypeError, ValueError):
        pass

    # 比赛时长（duration 缺失的简化数据源不参与，避免「1 分钟速通」这类误判）
    # 用「等效普通模式时长」：加速模式同样的真实时长推进量翻倍
    if ctx["duration"] > 0:
        if eq_dur_min >= 60:
            hit("long_game")
        elif eq_dur_min <= 20:
            hit("short_game")

    return hits


def _format_kwargs(stats: dict, ctx: dict, name: str, streak: tuple[int, int]) -> _SafeDict:
    """句子模板可用的占位符集合。"""
    win_streak, lose_streak = streak
    bench = _bench_avg(stats.get("benchmarks"))
    score = stats.get("xiaoheihe_score")
    peer = ctx.get("peer") or {}
    peer_avg = _peer_average(peer)
    return _SafeDict(
        name=name,
        hero=_hero_name(stats.get("hero")),
        kda=f"{float(stats.get('kda') or 0):.2f}",
        kills=int(stats.get("kill") or 0),
        deaths=int(stats.get("death") or 0),
        assists=int(stats.get("assist") or 0),
        gpm=int(stats.get("gpm") or 0),
        xpm=int(stats.get("xpm") or 0),
        lh=int(stats.get("last_hit") or 0),
        dmg=int(stats.get("damage") or 0),
        dmg_rate=f"{ctx['damage_rate']:.0f}",
        death_rate=f"{ctx['death_rate']:.0f}",
        part=f"{ctx['participation']:.0f}",
        dur_min=ctx["dur_min"],
        eq_dur_min=ctx["eq_dur_min"],
        n=max(win_streak, lose_streak),
        score="" if score is None else f"{float(score):.0f}",
        bench="" if bench is None else f"{bench:.0f}",
        peer="" if peer_avg is None else f"{peer_avg:.0f}",
        peer_kda="" if "KDA" not in peer else f"{peer['KDA']:.0f}",
    )


def _fallback_keys(win: bool, positive: bool) -> list[str]:
    """没有任何具体维度命中时的兜底维度。"""
    if win:
        return ["win_solid"] if positive else ["win_plain"]
    return ["lose_good"] if positive else ["lose_plain"]


def roast_one(
    stats: dict,
    ctx: dict,
    name: str,
    streak: tuple[int, int] = (0, 0),
    used: set[str] | None = None,
    rng=random,
) -> str:
    """为一位玩家生成一句锐评。

    used 为同一场比赛内已用过的句子集合，用于尽量避免同局多人撞词。
    """
    candidates = evaluate_candidates(stats, ctx, streak)
    if not candidates:
        positive = is_positive(stats, ctx["win"], ctx.get("peer"), rng)
        candidates = [(key, _WEIGHTS.get(key, 3)) for key in _fallback_keys(ctx["win"], positive)]

    # 按权重抽维度，抽到的维度若句子都用过了则换下一个，全用过才允许重复
    pool = list(candidates)
    kwargs = _format_kwargs(stats, ctx, name, streak)
    for _ in range(len(pool)):
        keys = [k for k, _ in pool]
        weights = [w for _, w in pool]
        key = rng.choices(keys, weights=weights, k=1)[0]
        lines = ROAST_LINES.get(key) or []
        if not lines:
            pool = [(k, w) for k, w in pool if k != key]
            continue
        fresh = [ln for ln in lines if ln not in (used or ())]
        line = rng.choice(fresh or lines)
        if used is not None:
            used.add(line)
        return line.format_map(kwargs)
    # 理论上不会走到这里（pool 非空且至少有一个维度有句子）
    return f"{name}这局打得一言难尽"


def roast_players(
    player_list: list,
    match_info: dict,
    streaks: dict[int, tuple[int, int]] | None = None,
    rng=random,
) -> str:
    """为一局中的每位订阅玩家各生成一句锐评，返回多行文本。

    streaks 为 {steam_id: (连胜, 连败)}，缺省表示无连胜/连败信息。
    """
    streaks = streaks or {}
    # 全局 10 人百分位只算一次，供同局所有玩家复用
    peers = peer_percentiles(match_info)
    used: set[str] = set()
    lines: list[str] = []
    for player in player_list:
        stats = player.stats
        ctx = team_context(match_info, stats.get("dota2_team"), stats)
        ctx["peer"] = peers.get(player.short_steamID) or {}
        streak = streaks.get(player.short_steamID, (0, 0))
        lines.append(roast_one(stats, ctx, player.nickname, streak, used, rng))
    return "\n".join(lines)
