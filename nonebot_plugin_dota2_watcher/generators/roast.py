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
  内部用一条 if/elif 链，同一件事不会被拆成多个维度重复计数（例如 death_many 与
  death_feed 互斥）。KDA 是 K/D/A 的派生量，因此只在 K/D/A 都没命中时兜底，
  避免与它们重复表达同一个信息。
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
    ("GPM", lambda p: float(p.get("gold_per_min") or 0), True),
    ("输出", lambda p: float(p.get("hero_damage") or 0), True),
    ("阵亡", lambda p: float(p.get("deaths") or 0), False),
)

# 名次门槛：只有第 1 名算亮眼，倒数第 1 名（最后一名）算拉胯。
# 收窄到第一名是为了让「全场最高」这个结论真的站得住——放宽到前二时，
# 第 2 名也会被说成「全场最高」，与同场对比的初衷不符。
PEER_TOP_RANK = 1

# 「每个数据维度只看全场最高 / 全场最低」所用的指标，比 _PEER_METRICS 多出
# 击杀 / 助攻：这几个维度也要收窄到极值，但不必参与偏正偏负的裁决。
# 「参团」要跨玩家汇总队伍击杀才能算，取值函数依赖 match_info，在 peer_ranks 内补。
_EXTREME_METRICS = (
    *_PEER_METRICS,
    ("击杀", lambda p: float(p.get("kills") or 0), True),
    ("助攻", lambda p: float(p.get("assists") or 0), True),
)

# K、D、A 各自的绝对值维度：任一命中就说明这三个原始量已经说过话了，
# KDA（(K+A)/D 的派生量）不再重复参与。
_KDA_SOURCES = {
    "kill_many",
    "kill_zero",
    "death_zero",
    "death_many",
    "death_feed",
    "assist_many",
}

# 维度极性：+1 正面（夸）/ -1 负面（骂）/ 0 中性（与数据高低无关）。
# 一位玩家可能同时命中正面与负面维度（例如 12 杀 12 死），此时用哪一边
# 不能靠权重抛硬币，而应由 is_positive 的结论（评分 / benchmark / 同场名次，
# 最终兜底本局胜负）裁决——这就是「高数据走胜利分支、低数据走失败分支」。
_POLARITY = {
    # 正面
    "kda_god": 1,
    "kda_high": 1,
    "kill_many": 1,
    "assist_many": 1,
    "death_zero": 1,
    "gpm_high": 1,
    "dmg_carry": 1,
    "dmg_huge": 1,
    "teamfight_high": 1,
    "streak_win": 1,
    # 负面
    "kda_low": -1,
    "kda_trash": -1,
    "kill_zero": -1,
    "death_many": -1,
    "death_feed": -1,
    "gpm_low": -1,
    "dmg_low": -1,
    "teamfight_low": -1,
    "streak_lose": -1,
    # 中性：英雄梗与时长不表达「打得好不好」，不参与极性裁决
    "hero_meme": 0,
    "long_game": 0,
    "short_game": 0,
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
    # 阵亡（D）
    "death_many": 6,
    "death_feed": 6,
    "death_zero": 5,
    # 输出（DMG，统一按占全队伤害比）
    "dmg_carry": 7,
    "dmg_huge": 7,
    "dmg_low": 7,
    # 经济（GPM 高不高看同场名次，见 peer_ranks）
    "gpm_low": 6,
    "gpm_high": 5,
    # 击杀（K）
    "kill_many": 5,
    "kill_zero": 5,
    # 助攻（A）
    "assist_many": 4,
    # 参团（K+A 占全队击杀比，与 K/A 绝对值不是同一信息）
    "teamfight_low": 5,
    "teamfight_high": 4,
    # KDA 综合（K/D/A 的派生量，仅在三者都没命中时兜底，故权重压低）
    "kda_god": 5,
    "kda_trash": 5,
    "kda_high": 4,
    "kda_low": 4,
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

    kda = float(stats.get("kda") or 0)
    kills = int(stats.get("kill") or 0)
    deaths = int(stats.get("death") or 0)
    assists = int(stats.get("assist") or 0)
    damage = int(stats.get("damage") or 0)

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
    # KDA 是 K/D/A 的派生量，因此放在最后，只在 K/D/A 都没命中时兜底。

    # ---- 击杀（K）----
    if kills >= 12 and extreme("击杀", 1):
        hit("kill_many")
    elif kills == 0 and extreme("击杀", -1):
        hit("kill_zero")

    # ---- 阵亡（D）：death_many 与 death_feed 是同义，取一个 ----
    if deaths == 0 and extreme("阵亡", 1):
        hit("death_zero")
    elif deaths >= 5 and ctx["death_rate"] >= 30 and extreme("阵亡", -1):
        # 死得多且占全队阵亡比例高，用更有节目效果的 death_feed
        hit("death_feed")
    elif deaths >= 8 and extreme("阵亡", -1):
        hit("death_many")

    # ---- 助攻（A）----
    if assists >= 20 and extreme("助攻", 1):
        hit("assist_many")

    # ---- 参团（K+A 占全队击杀比，与上面 K/A 的绝对值不是同一信息）----
    if ctx["participation"] <= 30 and extreme("参团", -1):
        hit("teamfight_low")
    elif ctx["participation"] >= 75 and extreme("参团", 1):
        hit("teamfight_high")

    # ---- 输出（DMG）：统一用「占全队伤害比」，不再混用绝对值 ----
    # 正面门槛提高到占比 30%：低于三成谈不上「把对面当木桩」。
    if ctx["damage_rate"] >= 35 and extreme("输出", 1):
        hit("dmg_carry")
    elif damage >= 50000 and ctx["damage_rate"] >= 30 and extreme("输出", 1):
        hit("dmg_huge")
    elif ctx["damage_rate"] <= 10 and extreme("输出", -1):
        hit("dmg_low")

    # ---- 经济（GPM）：同场名次第一 / 最后一名 ----
    # GPM 没有绝对值门槛（600 在弱场是碾压、在强场是垫底），因此名次不可得时
    # 直接不判，不能像其他维度那样退回绝对值。
    gpm_polarity = _rank_extreme(ranks, total, "GPM")
    if gpm_polarity == 1:
        hit("gpm_high")
    elif gpm_polarity == -1:
        hit("gpm_low")

    # ---- 英雄梗 ----
    try:
        if int(stats.get("hero")) in MEME_HEROES:
            hit("hero_meme")
    except (TypeError, ValueError):
        pass

    # ---- 比赛时长（duration 缺失的简化数据源不参与，避免「1 分钟速通」这类误判）
    # 用「等效普通模式时长」：加速模式同样的真实时长推进量翻倍 ----
    if ctx["duration"] > 0:
        if eq_dur_min >= 60:
            hit("long_game")
        elif eq_dur_min <= 20:
            hit("short_game")

    # ---- KDA 综合（K/D/A 的派生量）：只在 K、D、A 三者都没产出维度时兜底，
    # 避免与上面三个来源重复计数（曾占 33% 权重，实际 67% 与其他维度重复）----
    if not any(k in _KDA_SOURCES for k, _ in hits):
        if kda >= 10 and extreme("KDA", 1):
            hit("kda_god")
        elif kda >= 6 and extreme("KDA", 1):
            hit("kda_high")
        elif kda <= 0.8 and extreme("KDA", -1):
            hit("kda_trash")
        elif kda <= 1.5 and extreme("KDA", -1):
            hit("kda_low")

    return hits


def _lines_for(key: str, win: bool) -> list[str]:
    """取某维度的句库，按本局胜负选择对应后缀的那一组。

    锐评必须与本局胜负挂钩：同一个数据赢了和输了是两种说法。句库里除
    win_* / lose_* / streak_* 外的维度都拆成了 `<key>_win` / `<key>_lose`
    两组，这里按 win 取；取不到再退回无后缀的 key（兼容未拆分的维度）。
    """
    return ROAST_LINES.get(f"{key}_win" if win else f"{key}_lose") or ROAST_LINES.get(key) or []


def _format_kwargs(stats: dict, ctx: dict, name: str, streak: tuple[int, int]) -> _SafeDict:
    """句子模板可用的占位符集合。"""
    win_streak, lose_streak = streak
    return _SafeDict(
        name=name,
        hero=_hero_name(stats.get("hero")),
        kda=f"{float(stats.get('kda') or 0):.2f}",
        kills=int(stats.get("kill") or 0),
        deaths=int(stats.get("death") or 0),
        assists=int(stats.get("assist") or 0),
        gpm=int(stats.get("gpm") or 0),
        dmg=int(stats.get("damage") or 0),
        dmg_rate=f"{ctx['damage_rate']:.0f}",
        death_rate=f"{ctx['death_rate']:.0f}",
        part=f"{ctx['participation']:.0f}",
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
    positive = is_positive(stats, ctx["win"], ctx.get("peer"), ctx.get("peer_total", 0))
    if candidates:
        candidates = _resolve_polarity(candidates, positive)
    else:
        candidates = [(key, _WEIGHTS.get(key, 3)) for key in _fallback_keys(ctx["win"], positive)]

    # 按权重抽维度，抽到的维度若句子都用过了则换下一个，全用过才允许重复
    pool = list(candidates)
    kwargs = _format_kwargs(stats, ctx, name, streak)
    for _ in range(len(pool)):
        keys = [k for k, _ in pool]
        weights = [w for _, w in pool]
        key = rng.choices(keys, weights=weights, k=1)[0]
        lines = _lines_for(key, ctx["win"])
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
    # 同场 10 人名次只算一次，供同局所有玩家复用
    ranks = peer_ranks(match_info)
    used: set[str] = set()
    lines: list[str] = []
    for player in player_list:
        stats = player.stats
        ctx = team_context(match_info, stats.get("dota2_team"), stats)
        ctx["peer"] = ranks.get(player.short_steamID) or {}
        streak = streaks.get(player.short_steamID, (0, 0))
        lines.append(roast_one(stats, ctx, player.nickname, streak, used, rng))
    return "\n".join(lines)
