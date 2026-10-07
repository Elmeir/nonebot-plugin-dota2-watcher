"""阴阳怪气（锐评）生成：按多个维度判定，为每位玩家单独挑一句。

设计要点：
- **必须与本局胜负挂钩**：这是最重要的一条。同一个数据，赢了和输了是完全不同的
  两种说法（KDA 高 + 赢 = 降维打击；KDA 高 + 输 = 带不动），因此句库里除
  win_* / lose_* / streak_* 外的每个维度都拆成了 `<维度>_win` / `<维度>_lose`
  两组，每句都写明本局结果；选句时由 _lines_for 按胜负取后缀，
  不允许出现「结果中立」的句子。
- **多维度判定**：KDA、阵亡、经济、输出、参团、人头、连胜连败、英雄梗、
  比赛时长等都可独立命中，命中后按权重随机选一句。其中数据类维度都要求
  「本局全场最高 / 最低」才成立（见「同场对比」一条）。
- **按独立数据来源组织判定**：K、D、A、输出、经济、参团各算一个来源，每个来源
  内部用一条 if/elif 链，同一件事不会被拆成多个维度重复计数。
  KDA 是 (K+A)/D 的派生量，与 K / D / A 说的是同一件事，因此不单独成维度。
  每个来源只保留一档：击杀 / 伤害只看「最高」，阵亡只看「最多」，
  人头与输出不放「最低」档——低端数据交给兜底组与极性裁决表达。
- **极性裁决**：一位玩家可能同时命中正面与负面维度（例如 12 杀 12 死），此时
  保留哪一边由 is_positive 的结论决定，而不是靠权重抽签（见 _resolve_polarity）。
  只命中单侧时不干预。
- **评分只作判定参考**：小黑盒综合分 / OpenDota benchmark 只用来判断这位玩家
  该走「高数据」还是「低数据」的评价分支（见 is_positive），本身不产出文案，
  也不在句子里列出来。
- **按权重随机**：命中的维度各自带权重，权重越高越容易被选中；选定维度后再从
  该维度的句库中随机取一句，从而让语句分支足够多、不总是同一类腔调。
- **逐人评价**：同局多位订阅玩家各自独立判定（队伍数据按各自所在阵营计算），
  不做平均，返回每人一行。
- **加速模式折算**：加速模式（game_mode=23）同样的真实时长里，进度约为普通模式的
  两倍，因此「膀胱局 / 速通局」按等效普通模式时长判定。
- **同场对比，且只看极值**：每个数据维度都收窄到「本局全场最高 / 最低」两种情况
  （见 _rank_extreme / _extreme_ok）——同一个 GPM 在不同模式 / 英雄 / 位置下含义
  不同，放回同场里才有可比性，也就不需要按模式折算。600 GPM 在弱场是碾压、在强场
  是垫底，因此光过绝对值门槛还不够，必须是同场第一或最后一名；这样句子里
  「经济碾压」「人头被你承包了」这类结论才真的站得住。名次算不出来时（匿名玩家 /
  数据源没给该项）退回纯绝对值门槛，避免整个维度失效；GPM 没有绝对值门槛，
  名次不可得时直接不判。
- **评价分支**：在「本局胜负」这条主轴之下，再用数据高低决定语气——
  高数据 → 正面（胜利口径）分支，低数据 → 负面（失败口径）分支；
  高低的判定顺序为 小黑盒综合分 / benchmark → 同场数据对比 → 本局胜负兜底。
"""

import random

from ..config import config
from ..dota_dicts import HERO_MEMES, HEROES_LIST_CHINESE, ROAST_LINES  # noqa: I001
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
# 加速模式的进度倍率：同样的真实时长里，推进量约为普通模式的两倍，
# 故「膀胱局 / 速通局」按等效普通模式时长判定
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


# 本局同场 10 人的数据对比指标：(指标名, 取值函数, 是否越大越好)
# 用于「没有数据源评分时判断偏正还是偏负」。
# 对比范围是同场 10 人，因此与模式无关，不需要按加速模式折算。
_PEER_METRICS = (
    ("KDA", _kda_of, True),
    ("补刀", lambda p: float(p.get("last_hits") or 0), True),
    ("输出", lambda p: float(p.get("hero_damage") or 0), True),
    ("阵亡", lambda p: float(p.get("deaths") or 0), False),
)

# 名次门槛：只有第 1 名算亮眼，倒数第 1 名（最后一名）算拉胯。
# 收窄到第一名是为了让「全场最高」这个结论真的站得住——放宽到前二时，
# 第 2 名也会被说成「全场最高」，与同场对比的初衷不符。
PEER_TOP_RANK = 1

# 「每个数据维度只看全场最高 / 全场最低」所用的指标，比 _PEER_METRICS 多出
# 击杀：这个维度也要收窄到极值，但不必参与偏正偏负的裁决。
# 「参团」要跨玩家汇总队伍击杀才能算，取值函数依赖 match_info，在 peer_ranks 内补。
_EXTREME_METRICS = (
    *_PEER_METRICS,
    ("击杀", lambda p: float(p.get("kills") or 0), True),
)

# 维度极性：+1 正面（夸）/ -1 负面（骂）/ 0 中性（与数据高低无关）。
# 一位玩家可能同时命中正面与负面维度（例如 12 杀 12 死），此时用哪一边
# 不能靠权重抛硬币，而应由 is_positive 的结论（评分 / benchmark / 同场名次，
# 最终兜底本局胜负）裁决——这就是「高数据走胜利分支、低数据走失败分支」。
_POLARITY = {
    # 正面
    "kill_many": 1,
    "lh_high": 1,
    "dmg_carry": 1,
    "teamfight_high": 1,
    "streak_win": 1,
    # 负面
    "death_many": -1,
    "teamfight_low": -1,
    "streak_lose": -1,
    # 中性：英雄梗与时长不表达「打得好不好」，不参与极性裁决
    "hero_meme": 0,
    "long_game": 0,
    # 兜底维度（无具体维度命中时才会出现，极性与其语气一致）
    "win_solid": 1,  # 赢了且数据高
    "win_plain": -1,  # 赢了但数据低（侥幸 / 躺赢）
    "lose_good": 1,  # 输了但数据高（尽力了）
    "lose_plain": -1,  # 输了且数据低
}


def _rank_pair(
    values: list[tuple[int | None, float]], higher_better: bool
) -> dict[int, tuple[int, int]]:
    """把一组 (account_id, 取值) 换算成 (最好名次, 最差名次)，匿名玩家不产出。

    两个名次都按「表现」轴算，与指标本身是越大越好还是越小越好无关：
    名次 1 = 全场表现最好，名次 = 总人数 = 全场表现最差。
    并列要两头都算极端，因此不能只存一个名次：并列最差时 `最好名次` 到不了
    总人数（例如两人并列送得最多，各自最好名次只有 总人数-1），若只用它判断
    「是不是最差」就会漏掉这几位。故额外算出 `最差名次 = 总人数 - 严格更差的人数`，
    它等于总人数 等价于「没有人比你更差」，并列最差者同样成立。
    """
    ranks: dict[int, tuple[int, int]] = {}
    for account_id, value in values:
        if account_id is None:
            continue
        if higher_better:
            better = sum(1 for _, other in values if other > value)
            worse = sum(1 for _, other in values if other < value)
        else:
            better = sum(1 for _, other in values if other < value)
            worse = sum(1 for _, other in values if other > value)
        ranks[account_id] = (better + 1, len(values) - worse)
    return ranks


def peer_ranks(match_info: dict) -> dict[int, dict[str, int]]:
    """本局同场 10 人各项数据的直接对比名次（1 = 表现最好）。

    没有数据源评分（小黑盒综合分 / OpenDota benchmark）时，用它替代原先的
    「按 KDA 拍脑袋 + 抛硬币」，让正负倾向落在同场实际数据的对比上。
    「越小越好」的指标（阵亡）名次会反向，即阵亡最少为第 1 名。
    返回值以 account_id 为键，只含真实 account_id 的玩家（匿名玩家无法对应）。

    每项指标存两个键：`<指标>` 是表现最好的名次，`<指标>_bottom` 是表现最差的
    名次，供「每个维度只看全场最高 / 最低」判断两头极端（见 _rank_extreme）。
    偏正偏负的裁决只取 _PEER_METRICS 的那几个名字（见 _peer_verdict），
    因此击杀 / 助攻 / 参团与 `_bottom` 后缀都不会重复计入。
    """
    players = [p for p in (match_info.get("players") or []) if isinstance(p, dict)]
    if len(players) < 2:
        return {}

    table: dict[int, dict[str, int]] = {}
    for name, getter, higher_better in _EXTREME_METRICS:
        # 对比范围是本局全部 10 人（匿名玩家也参与对比，只是不产出自己的行）
        pair = _rank_pair([(p.get("account_id"), getter(p)) for p in players], higher_better)
        for account_id, (top, bottom) in pair.items():
            table.setdefault(account_id, {})[name] = top
            table.setdefault(account_id, {})[f"{name}_bottom"] = bottom

    # 参团率：需要按阵营汇总队伍击杀，故单独算一遍（与 team_context 同口径）
    team_kills: dict[object, int] = {}
    for player in players:
        team = player_team(player)
        team_kills[team] = team_kills.get(team, 0) + int(player.get("kills") or 0)
    values = []
    for player in players:
        total_kills = team_kills.get(player_team(player), 0)
        involved = int(player.get("kills") or 0) + int(player.get("assists") or 0)
        values.append(
            (player.get("account_id"), 100.0 * involved / total_kills if total_kills else 0.0)
        )
    for account_id, (top, bottom) in _rank_pair(values, True).items():
        table.setdefault(account_id, {})["参团"] = top
        table.setdefault(account_id, {})["参团_bottom"] = bottom
    return table


def _peer_verdict(ranks: dict[str, int] | None, total: int) -> bool | None:
    """本局数据对比结论：名次在人数前半的指标更多则为正面，否则负面。

    名次都是「本局同场 10 人」里比出来的，故前半的门槛是总人数的一半。
    只取 _PEER_METRICS 那几项，避免击杀 / 助攻 / 参团与 KDA / 输出重复计入。
    全平局时人人第 1 名，视为正面（数据上确实不落后）。
    """
    if not ranks or total < 2:
        return None
    names = {name for name, _, _ in _PEER_METRICS}
    usable = {name: rank for name, rank in ranks.items() if name in names}
    if not usable:
        return None
    ahead = sum(1 for rank in usable.values() if rank * 2 <= total)
    return ahead * 2 >= len(usable)


def _rank_extreme(ranks: dict[str, int], total: int, metric: str) -> int | None:
    """该指标是否处在全场最高 / 最低（第 1 名或最后一名），返回极性，否则 None。

    名次只对同场 10 人里真实 account_id 产出，数据缺失（如匿名玩家）时返回 None，
    调用方据此退回绝对值门槛，避免因为算不出名次就整段不判。
    """
    if total < 2:
        return None
    top = ranks.get(metric)
    bottom = ranks.get(f"{metric}_bottom")
    if top is None and bottom is None:
        # 该项根本没有名次（数据源没给 / 玩家匿名），视为不可得而非「处于中间」
        return None
    if top is not None and top <= PEER_TOP_RANK:
        return 1
    if bottom is not None and bottom >= total:
        return -1
    return 0


# 各维度权重：越具体、越有节目效果的维度权重越高；
# 兜底维度（win_plain / lose_plain 等）权重最低，保证句子不至于太单调。
#
# 权重按「独立数据来源」分配，而不是按维度个数：K/D/A 三个来源各自只有
# 少数几个维度，伤害 / 经济 / 参团 / 时长这些独立来源也不该被挤到边缘。
_WEIGHTS = {
    # 连胜/连败（跨局信息，最具体）
    "streak_win": 11,
    "streak_lose": 11,
    # 阵亡（D）：只留「死得最多」一档
    "death_many": 6,
    # 输出（DMG）：只留「最高」一档
    "dmg_carry": 7,
    # 补刀（看同场名次，见 peer_ranks）：只留「最高」一档
    "lh_high": 5,
    # 击杀（K）：只留「最多」一档
    "kill_many": 5,
    # 参团（K+A 占全队击杀比，与 K 绝对值不是同一信息）
    "teamfight_low": 5,
    "teamfight_high": 4,
    # 英雄梗 / 时长
    "hero_meme": 5,
    "long_game": 3,
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
    """OpenDota benchmark 的可用百分位均值，归一化到 0~100（与小黑盒综合分同量纲）。

    OpenDota 返回的 pct 是 0~1 的小数，这里统一乘 100，避免与 0~100 的阈值比较时
    量纲错位（曾因此让 bench 恒小于 20，导致 bench_low 对所有人 100% 命中）。
    """
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
    return 100.0 * sum(pcts) / len(pcts)


def is_positive(
    stats: dict,
    win: bool,
    ranks: dict[str, int] | None = None,
    total: int = 0,
) -> bool:
    """判定这位玩家该用「高数据（正面）」还是「低数据（负面）」的评价分支。

    只作参考、不出现在文案里，依次尝试：
      小黑盒综合分 / OpenDota benchmark（同段位基准）
      → 本局同场 10 人数据对比（同场基准）
      → 都没有时用本局胜负兜底（赢了算高、输了算低）。
    """
    score = stats.get("xiaoheihe_score")
    if score is not None:
        return float(score) / 100 > config.d2w_benchmark_threshold

    bench = _bench_avg(stats.get("benchmarks"))
    if bench is not None:
        return bench / 100 > config.d2w_benchmark_threshold

    verdict = _peer_verdict(ranks, total)
    if verdict is not None:
        return verdict

    # 兜底：没有任何可参考的数据时，就用本局胜负本身
    return win


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
        "peer_total": len(players),
        "team_damage": team_damage,
        "team_kills": team_kills,
        "team_deaths": team_deaths,
        "damage_rate": _rate(int(stats.get("damage") or 0), team_damage),
        "participation": _rate(
            int(stats.get("kill") or 0) + int(stats.get("assist") or 0), team_kills
        ),
        "death_rate": _rate(int(stats.get("death") or 0), team_deaths),
    }


def _extreme_ok(ranks: dict[str, int], total: int, metric: str, want: int) -> bool:
    """该维度是否成立：既要达到原绝对值门槛，也要是本局该项的最高 / 最低。

    want 为 +1（要求全场最高）或 -1（要求全场最低）。
    名次不可得时（匿名玩家、数据源没给该项）放行，退回纯绝对值门槛，
    避免因为算不出名次就让整个维度失效。
    """
    polarity = _rank_extreme(ranks, total, metric)
    if polarity is None:
        return True
    return polarity == want


def evaluate_candidates(
    stats: dict, ctx: dict, streak: tuple[int, int] = (0, 0)
) -> list[tuple[str, int]]:
    """返回本局命中的全部锐评维度及其权重（纯函数，便于测试与调试）。

    未命中任何具体维度时返回空列表，由调用方回退到基础结果向语句。

    每个数据维度都收窄到「本局全场最高 / 最低」两种情况：光达到绝对值门槛
    还不够（600 GPM 在弱场是碾压、在强场是垫底），必须是同场第一或最后一名
    才成立——句子里「经济碾压」「人头被你承包了」这类结论才真的站得住。
    """
    hits: list[tuple[str, int]] = []
    win = ctx["win"]
    eq_dur_min = ctx["eq_dur_min"]

    kills = int(stats.get("kill") or 0)
    deaths = int(stats.get("death") or 0)

    win_streak, lose_streak = streak

    ranks = ctx.get("peer") or {}
    total = ctx.get("peer_total", 0)

    def hit(key: str) -> None:
        hits.append((key, _WEIGHTS.get(key, 5)))

    def extreme(metric: str, want: int) -> bool:
        return _extreme_ok(ranks, total, metric, want)

    # 连胜 / 连败（含本局）
    if win and win_streak >= STREAK_MIN:
        hit("streak_win")
    if not win and lose_streak >= STREAK_MIN:
        hit("streak_lose")

    # 判定按「独立数据来源」组织，每个来源内部用一条 if/elif 链，保证
    # 同一件事不会被拆成多个维度重复计数（权重被重复计算）。
    # KDA 是 (K+A)/D 的派生量，与 K / D / A 说的是同一件事，已整体移除。

    # ---- 击杀（K）：只留「最多」一档 ----
    if kills >= 12 and extreme("击杀", 1):
        hit("kill_many")

    # ---- 阵亡（D）：只留「死得最多」一档 ----
    if deaths >= 8 and extreme("阵亡", -1):
        hit("death_many")

    # ---- 参团（K+A 占全队击杀比，与 K 的绝对值不是同一信息）----
    if ctx["participation"] <= 40 and extreme("参团", -1):
        hit("teamfight_low")
    elif ctx["participation"] >= 75 and extreme("参团", 1):
        hit("teamfight_high")

    # ---- 输出（DMG）：只留「最高」一档，按占全队伤害比判定 ----
    if ctx["damage_rate"] >= 35 and extreme("输出", 1):
        hit("dmg_carry")

    # ---- 补刀（LH）：同场名次第一 ----
    # 句库键为 lh_high，句子写的是「刷钱」「野区是你家开的」这类说法，
    # 补刀正是刷钱的结果，语义成立。
    # 没有绝对值门槛（100 刀在快节奏局是碾压、在膀胱局是垫底），因此名次
    # 不可得时直接不判，不能像其他维度那样退回绝对值。只留「最高」一档。
    if _rank_extreme(ranks, total, "补刀") == 1:
        hit("lh_high")

    # ---- 比赛时长（duration 缺失的简化数据源不参与）----
    # 用「等效普通模式时长」：加速模式同样的真实时长推进量翻倍。
    # 只留「长」一档。
    if ctx["duration"] > 0 and eq_dur_min >= 60:
        hit("long_game")

    # ---- 英雄梗（每个英雄自己一套词，见 dota_dicts.HERO_MEMES）----
    # 放在最后判定：要先看完上面所有维度，才知道本局有没有落在「两极」。
    # 只在两极时参与选句：赢了且命中正面维度（高数据的赢），或输了且命中
    # 负面维度（低数据的输）。中间地带不吐槽选人——赢了但数据难看时再损一句
    # 选人就是雪上加霜，输了但数据好看时尬吹也没意思。
    #
    # 权重取「其余维度权重之和」，这样它与原来的维度整体各占一半概率。
    # 句子库留空的英雄（_hero_lines 返回空）直接跳过，不参与。
    if _hero_lines(_hero_id_of(stats), win):
        if win:
            pole = any(_POLARITY.get(k, 0) > 0 for k, _ in hits)
        else:
            pole = any(_POLARITY.get(k, 0) < 0 for k, _ in hits)
        if pole:
            other = sum(w for _, w in hits)
            if other:
                hits.append(("hero_meme", other))

    return hits


def _hero_id_of(stats: dict) -> int | None:
    """从玩家数据里取英雄 ID；缺失或非法时返回 None。"""
    try:
        return int(stats.get("hero"))
    except (TypeError, ValueError):
        return None


def _hero_lines(hero_id: int | None, win: bool) -> list[str]:
    """取某个英雄的专属梗句库（按胜负分组）。

    英雄梗不是「同一批句子套在不同人身上」，而是每个英雄自己一套词——
    通用句套在米波身上没梗，得说这个英雄自己的笑话。见 dota_dicts.HERO_MEMES。

    该英雄没有配句子（两个键都留空）时返回空列表，调用方据此跳过这个维度。
    """
    if hero_id is None:
        return []
    group = HERO_MEMES.get(int(hero_id)) or {}
    return list(group.get("win" if win else "lose") or [])


def _lines_for(key: str, win: bool, hero_id: int | None = None) -> list[str]:
    """取某维度的句库，按本局胜负选择对应后缀的那一组。

    锐评必须与本局胜负挂钩：同一个数据赢了和输了是两种说法。句库里除
    win_* / lose_* / streak_* 外的维度都拆成了 `<key>_win` / `<key>_lose`
    两组，这里按 win 取；取不到再退回无后缀的 key（兼容未拆分的维度）。

    hero_meme 例外：它不走 ROAST_LINES，而是取该英雄自己的 HERO_MEMES 句子，
    需要 hero_id 才能查到；没有配句子的英雄返回空列表，维度自然不命中。
    """
    if key == "hero_meme":
        return _hero_lines(hero_id, win)
    return ROAST_LINES.get(f"{key}_win" if win else f"{key}_lose") or ROAST_LINES.get(key) or []


def _format_kwargs(stats: dict, ctx: dict, name: str, streak: tuple[int, int]) -> _SafeDict:
    """句子模板可用的占位符集合。

    只保留句库里真正在用的占位符：锐评是把结论说出来，不是把数据表念一遍，
    因此 KDA / GPM / 伤害占比 / 参战率这些「字段名 + 数值」的写法已经从句库
    里去掉，对应的占位符也一并删掉（测试会校验两边完全一致，避免留下死占位符，
    也避免模板引用了没提供的占位符而渲染出字面的 `{xxx}`）。
    """
    win_streak, lose_streak = streak
    return _SafeDict(
        name=name,
        hero=_hero_name(stats.get("hero")),
        kills=int(stats.get("kill") or 0),
        deaths=int(stats.get("death") or 0),
        dur_min=ctx["dur_min"],
        n=max(win_streak, lose_streak),
    )


def _fallback_keys(win: bool, positive: bool) -> list[str]:
    """没有任何具体维度命中时的兜底维度。"""
    if win:
        return ["win_solid"] if positive else ["win_plain"]
    return ["lose_good"] if positive else ["lose_plain"]


def _resolve_polarity(candidates: list[tuple[str, int]], positive: bool) -> list[tuple[str, int]]:
    """同一位玩家同时命中正面与负面维度时，用评价分支的结论裁决留哪一边。

    例如 12 杀 12 死会同时命中 kill_many(+) 与 death_many(-)，夸还是骂不该由
    权重抽签决定，而应跟随 is_positive（评分 / benchmark / 同场名次 / 胜负兜底）。

    只命中单侧时不做干预——数据本身已经很明确，不该被评分推翻。
    """
    polarities = {_POLARITY.get(k, 0) for k, _ in candidates}
    if 1 not in polarities or -1 not in polarities:
        return candidates
    keep = 1 if positive else -1
    # 中性维度（英雄梗 / 时长）始终保留，它们不表达打得好不好
    filtered = [(k, w) for k, w in candidates if _POLARITY.get(k, 0) in (0, keep)]
    return filtered or candidates


def _candidates_for(stats: dict, ctx: dict, streak: tuple[int, int]) -> list[tuple[str, int]]:
    """本局的候选维度及权重（含极性裁决；没命中具体维度时回退到兜底组）。"""
    candidates = evaluate_candidates(stats, ctx, streak)
    positive = is_positive(stats, ctx["win"], ctx.get("peer"), ctx.get("peer_total", 0))
    if candidates:
        return _resolve_polarity(candidates, positive)
    return [(key, _WEIGHTS.get(key, 3)) for key in _fallback_keys(ctx["win"], positive)]


def _pick_key(candidates: list[tuple[str, int]], win: bool, rng, hero_id=None) -> str | None:
    """按权重抽一个维度；抽到的维度若没有句子就换下一个。

    英雄梗要按英雄查句子，因此这里要把 hero_id 透传给 _lines_for，
    否则配了空句库的英雄会被误判成「有句子」而抽中后渲染不出内容。
    """
    pool = list(candidates)
    for _ in range(len(pool)):
        keys = [k for k, _ in pool]
        weights = [w for _, w in pool]
        key = rng.choices(keys, weights=weights, k=1)[0]
        if _lines_for(key, win, hero_id):
            return key
        pool = [(k, w) for k, w in pool if k != key]
    return None


def _split_hero(
    candidates: list[tuple[str, int]],
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """把候选拆成「数据维度」与「英雄梗」两组。

    英雄梗是每人自己的英雄，含 {hero} 因此永远不会合并成一句（见
    _merge_safe）。多人共享一个数据维度时，若让其中某人随机抽中英雄梗，
    这一句就合并不起来了——所以归堆阶段只按数据维度定位。
    """
    dims = [(k, w) for k, w in candidates if k != "hero_meme"]
    hero = [(k, w) for k, w in candidates if k == "hero_meme"]
    return dims, hero


def _render_one(
    key: str,
    stats: dict,
    ctx: dict,
    name: str,
    streak: tuple[int, int],
    used: set[str] | None,
    rng,
) -> str | None:
    """渲染指定维度的一句；该维度没句子时返回 None。"""
    lines = _lines_for(key, ctx["win"], _hero_id_of(stats))
    if not lines:
        return None
    fresh = [ln for ln in lines if ln not in (used or ())]
    line = rng.choice(fresh or lines)
    if used is not None:
        used.add(line)
    return line.format_map(_format_kwargs(stats, ctx, name, streak))


def _solo_pool(
    key: str,
    stats: dict,
    ctx: dict,
    streak: tuple[int, int],
    allow_hero: bool,
) -> list[tuple[str, int]]:
    """归堆之后，这个人真正还能抽的候选：数据维度**锁定为 key**。

    归堆时已经按权重定了这个人说哪个维度，渲染时不能再抽一次——重抽有可能
    抽回另一个维度，而那个维度可能已经被别的组说过了（7 人合并说了 long_game，
    这位重抽又抽到 long_game），同一个维度就被说两遍。组与组的 key 互不相同，
    锁定之后同一局里每个数据维度最多只说一次。

    allow_hero 为真（此人独占这个维度）时才把英雄梗放回池子：多人共享维度时
    英雄梗含 {hero} 合并不起来，塞进去只会把该合的那一句拆散。
    """
    return [
        (k, w)
        for k, w in _candidates_for(stats, ctx, streak)
        if k == key or (allow_hero and k == "hero_meme")
    ]


def roast_one(
    stats: dict,
    ctx: dict,
    name: str,
    streak: tuple[int, int] = (0, 0),
    used: set[str] | None = None,
    rng=random,
    lock: str | None = None,
) -> str:
    """为一位玩家生成一句锐评。

    used 为同一场比赛内已用过的句子集合，用于尽量避免同局多人撞词。
    lock 为归堆阶段定下的维度：给了就锁定它（至多再带上英雄梗），不再重新
    抽签，避免把别的组已经说过的维度又说一遍（见 _solo_pool）。
    """
    if lock is None:
        candidates = _candidates_for(stats, ctx, streak)
    else:
        candidates = _solo_pool(lock, stats, ctx, streak, allow_hero=True)
    key = _pick_key(candidates, ctx["win"], rng, _hero_id_of(stats))
    if key is not None:
        line = _render_one(key, stats, ctx, name, streak, used, rng)
        if line:
            return line
    # 命中的维度在本局胜负下没有句子（例如 lh_high 只写了 _win 组），
    # 退回兜底组——绝不能输出「这局打得一言难尽」这种占位文案。
    fallback = _fallback_keys(
        ctx["win"], is_positive(stats, ctx["win"], ctx.get("peer"), ctx.get("peer_total", 0))
    )
    key = _pick_key([(k, _WEIGHTS.get(k, 3)) for k in fallback], ctx["win"], rng)
    if key is not None:
        line = _render_one(key, stats, ctx, name, streak, used, rng)
        if line:
            return line
    # 兜底组也取不到（句库被删空），用最后保底的一句
    return f"{name}这局打得一言难尽"


def _join_names(names: list[str]) -> str:
    """把多个人的名字并成一个主语：「A」「A和B」「A、B和C」。"""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}和{names[1]}"
    return f"{'、'.join(names[:-1])}和{names[-1]}"


# 合并成一句时句子里不能出现「每个人各自不同的数据」：同一句装不下多份数字，
# 硬套第一个人的数值等于替别人报错数据。{dur_min} 是全场共享的，不受影响。
_MERGE_BLOCKERS = ("{kills}", "{deaths}", "{assists}", "{hero}", "{n}")


def _merge_safe(line: str) -> bool:
    """这句能不能同时套在多人身上（不含各自的数值 / 英雄 / 连胜场数）。"""
    return not any(p in line for p in _MERGE_BLOCKERS)


def roast_players(
    player_list: list,
    match_info: dict,
    streaks: dict[int, tuple[int, int]] | None = None,
    rng=random,
) -> str:
    """为一局中的每位订阅玩家生成锐评，返回多行文本。

    同一条消息里若有多人抽中同一个维度，合并成一句一起说（「P1、P3 和 P5 野区
    都快被你们躺成坟场了」），而不是每人来一句相似的——同样的句式连着刷三遍
    读起来很啰嗦。合并只在句子不含各自的数值 / 英雄 / 连胜场数时进行：一句
    装不下多份数字，硬套第一个人的数值等于替别人报错数据。

    多人共享同一个数据维度时，优先让这个维度合并成一句，不再给其中某人随机
    换成英雄梗——英雄梗含 {hero}，本来就合并不起来，随机塞进去会把该合的
    那一句拆散。英雄梗只在某人**单独**命中（没人跟他同维度）时才参与抽取。

    streaks 为 {steam_id: (连胜, 连败)}，缺省表示无连胜/连败信息。
    """
    streaks = streaks or {}
    # 同场 10 人名次只算一次，供同局所有玩家复用
    ranks = peer_ranks(match_info)
    used: set[str] = set()

    # 先按「数据维度」给每人定位，再归堆——合并必须在选句之前做。
    # 英雄梗不参与这一步：它含 {hero}，永远合并不起来，会让该合的合不上。
    # key 可能为 None（命中的维度在本局胜负下没句子，例如 lh_high 只写了
    # _win 组而本局是输局）：这类玩家不能丢，归到 None 组由 roast_one 兜底。
    picks: list[tuple[str | None, dict, dict, tuple[int, int], str, int | None]] = []
    for player in player_list:
        stats = player.stats
        ctx = team_context(match_info, stats.get("dota2_team"), stats)
        ctx["peer"] = ranks.get(player.short_steamID) or {}
        streak = streaks.get(player.short_steamID, (0, 0))
        hero_id = _hero_id_of(stats)
        dims, _hero = _split_hero(_candidates_for(stats, ctx, streak))
        key = _pick_key(dims, ctx["win"], rng, hero_id)
        picks.append((key, stats, ctx, streak, player.nickname, hero_id))

    groups: dict[str | None, list] = {}
    order: list[str | None] = []
    for item in picks:
        if item[0] not in groups:
            groups[item[0]] = []
            order.append(item[0])
        groups[item[0]].append(item)

    lines: list[str] = []
    for key in order:
        members = groups[key]
        if key is None:
            # 没有可用的数据维度：逐人走 roast_one（内含兜底组回退）
            for _, m_stats, m_ctx, m_streak, m_name, _m_hero in members:
                lines.append(roast_one(m_stats, m_ctx, m_name, m_streak, used, rng))
            continue
        _, m0_stats, m0_ctx, m0_streak, _, m0_hero = members[0]
        if len(members) > 1:
            # 只取能同时套在所有人身上的句子；没有就还是各说各的
            pool = [ln for ln in _lines_for(key, m0_ctx["win"], m0_hero) if _merge_safe(ln)]
            if pool:
                fresh = [ln for ln in pool if ln not in used]
                line = rng.choice(fresh or pool)
                used.add(line)
                # 多人并成一句后，「你」要跟着变成「你们」，否则主谓对不上
                if "你们" not in line:
                    line = line.replace("你", "你们")
                lines.append(
                    line.format_map(
                        _format_kwargs(
                            m0_stats, m0_ctx, _join_names([m[4] for m in members]), m0_streak
                        )
                    )
                )
                continue
        for _, m_stats, m_ctx, m_streak, m_name, m_hero in members:
            # 多人共享这个维度但句子都不可合并（例如 streak_* 每句都含 {n}）：
            # 各说各的，但维度仍然锁死在这个 key 上，不许重抽到别人说过的维度。
            line = roast_one(m_stats, m_ctx, m_name, m_streak, used, rng, lock=key)
            lines.append(line)
    return "\n".join(lines)
