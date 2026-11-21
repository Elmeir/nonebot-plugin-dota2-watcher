"""锐评生成：按多个维度判定，为每位玩家挑一句。

- 每句写明本局胜负（除 win_* / lose_* / streak_* 外按 _win / _lose 拆分）。
- 每个数据来源只走一条 if/elif 链，同一事实不重复计数。
- 数据类维度要求本局同场最高 / 最低；名次不可得时退回绝对值门槛。
- 正负维度同时命中时由 is_positive 裁决，不靠权重抽签。
- 评分（小黑盒综合分 / benchmark）只决定评价分支，不写进句子。
句库见 dota_dicts.ROAST_LINES。
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

# 连胜 / 连败维度：句子里的 {n} 是「跨局」的场数——同一批人一起开黑时大家
# 的场数往往本来就是同一个，因此允许在这种情况下合并（见 _streak_text）。
_STREAK_KEYS = ("streak_win", "streak_lose")

# 加速模式（dota_dicts.GAME_MODE[23] = "加速模式"）
TURBO_MODE = 23
# 小黑盒兜底数据源不返回 game_mode，只给原始中文模式文本（如"加速模式"）
TURBO_MODE_DESC_KEYWORD = "加速"
# 膀胱局门槛（真实分钟）：加速模式推进快，单独设阈值
LONG_GAME_MIN_NORMAL = 60
LONG_GAME_MIN_TURBO = 40


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


# 无数据源评分时，用同场 10 人的数据对比判断偏正 / 偏负
_PEER_METRICS = (
    ("KDA", _kda_of, True),
    ("补刀", lambda p: float(p.get("last_hits") or 0), True),
    ("输出", lambda p: float(p.get("hero_damage") or 0), True),
    ("阵亡", lambda p: float(p.get("deaths") or 0), False),
)

# 只有第 1 名算亮眼、最后一名算拉胯，保证「全场最高」成立
PEER_TOP_RANK = 1

# 数据维度极值判定用的指标（比 _PEER_METRICS 多击杀）
# 「参团」需汇总队伍击杀，取值函数依赖 match_info，在 peer_ranks 内补
_EXTREME_METRICS = (
    *_PEER_METRICS,
    ("击杀", lambda p: float(p.get("kills") or 0), True),
)

# 维度极性：+1 正面 / -1 负面 / 0 中性；正负同时命中由 is_positive 裁决
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
    """同场 10 人各项数据的对比名次（1 = 最好）。

    提供 <指标> 与 <指标>_bottom 两组名次，供极值判断；
    匿名玩家没有 account_id，不参与。
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


# 各维度权重：越具体权重越高，兜底最低；按独立数据来源分配
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

    时长只给真实分钟（dur_min）：加速模式同样真实时长里推进量约为普通模式的两倍，
    膀胱局门槛按模式分别判定（见 LONG_GAME_MIN_NORMAL / LONG_GAME_MIN_TURBO）。
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
    dur_min = max(duration // 60, 1)

    def _rate(value: int, total: int) -> float:
        return 0.0 if not total else 100.0 * value / total

    return {
        "win": win,
        "turbo": turbo,
        "duration": duration,
        "dur_min": dur_min,
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
    dur_min = ctx["dur_min"]

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

    # 每个数据来源一条 if/elif 链，避免同一事实重复计数

    # ---- 击杀（K）：只留「最多」一档 ----
    if kills >= 20 and extreme("击杀", 1):
        hit("kill_many")

    # ---- 阵亡（D）：只留「死得最多」一档 ----
    if deaths >= 10 and extreme("阵亡", -1):
        hit("death_many")

    # ---- 参团（K+A 占全队击杀比，与 K 的绝对值不是同一信息）----
    if ctx["participation"] <= 40 and extreme("参团", -1):
        hit("teamfight_low")
    elif ctx["participation"] >= 75 and extreme("参团", 1):
        hit("teamfight_high")

    # ---- 输出（DMG）：只留「最高」一档，按占全队伤害比判定 ----
    if ctx["damage_rate"] >= 35 and extreme("输出", 1):
        hit("dmg_carry")

    # ---- 补刀（LH）：只看同场第一；无绝对值门槛，名次不可得时不判 ----
    if _rank_extreme(ranks, total, "补刀") == 1:
        hit("lh_high")

    # ---- 比赛时长：普通 60 分钟 / 加速 40 分钟，只留「长」一档 ----
    _long_min = LONG_GAME_MIN_TURBO if ctx["turbo"] else LONG_GAME_MIN_NORMAL
    if ctx["duration"] > 0 and dur_min >= _long_min:
        hit("long_game")

    # ---- 英雄梗：只在两极时参与；权重取其余维度之和 ----
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
    """取某英雄的专属梗句库（按胜负分组）；未配置时返回空列表。"""
    if hero_id is None:
        return []
    group = HERO_MEMES.get(int(hero_id)) or {}
    return list(group.get("win" if win else "lose") or [])


def _lines_for(key: str, win: bool, hero_id: int | None = None) -> list[str]:
    """取某维度的句库，按胜负选 _win / _lose；hero_meme 走 HERO_MEMES。"""
    if key == "hero_meme":
        return _hero_lines(hero_id, win)
    return ROAST_LINES.get(f"{key}_win" if win else f"{key}_lose") or ROAST_LINES.get(key) or []


def _format_kwargs(
    stats: dict, ctx: dict, name: str, streak: tuple[int, int], n_text: str | None = None
) -> _SafeDict:
    """句子模板可用的占位符集合。

    与句库实际使用的占位符保持一致（测试会校验）；n_text 为多人合并时的共有场数。
    """
    win_streak, lose_streak = streak
    return _SafeDict(
        name=name,
        hero=_hero_name(stats.get("hero")),
        kills=int(stats.get("kill") or 0),
        deaths=int(stats.get("death") or 0),
        dur_min=ctx["dur_min"],
        n=n_text if n_text is not None else max(win_streak, lose_streak),
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


def _fallback_candidates(stats: dict, ctx: dict) -> list[tuple[str, int]]:
    """兜底组的候选（没命中具体维度时，或命中的维度在本局胜负下没句子时）。

    单独抽出来是为了让 roast_players 能在**归堆之前**就把兜底维度定下来：
    否则这类玩家归到 None 组各自回退，两个都回退到 lose_good 的人就合并不上，
    同一句被说两遍（见 roast_players）。
    """
    positive = is_positive(stats, ctx["win"], ctx.get("peer"), ctx.get("peer_total", 0))
    return [(key, _WEIGHTS.get(key, 3)) for key in _fallback_keys(ctx["win"], positive)]


def _candidates_for(stats: dict, ctx: dict, streak: tuple[int, int]) -> list[tuple[str, int]]:
    """本局的候选维度及权重（含极性裁决；没命中具体维度时回退到兜底组）。"""
    candidates = evaluate_candidates(stats, ctx, streak)
    if candidates:
        positive = is_positive(
            stats, ctx["win"], ctx.get("peer"), ctx.get("peer_total", 0)
        )
        return _resolve_polarity(candidates, positive)
    return _fallback_candidates(stats, ctx)


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
    n_text: str | None = None,
) -> str | None:
    """渲染指定维度的一句；该维度没句子时返回 None。"""
    lines = _lines_for(key, ctx["win"], _hero_id_of(stats))
    if not lines:
        return None
    fresh = [ln for ln in lines if ln not in (used or ())]
    line = rng.choice(fresh or lines)
    if used is not None:
        used.add(line)
    return line.format_map(_format_kwargs(stats, ctx, name, streak, n_text))


def _solo_pool(
    key: str,
    stats: dict,
    ctx: dict,
    streak: tuple[int, int],
    allow_hero: bool,
) -> list[tuple[str, int]]:
    """锁定归堆时选定的维度，避免渲染阶段重抽导致同维度重复。

    allow_hero 为真时把英雄梗放回候选池。
    """
    return [
        (k, w)
        for k, w in _candidates_for(stats, ctx, streak)
        if k == key or (allow_hero and k == "hero_meme")
    ]


def _streak_text(streaks: list[tuple[int, int]]) -> str | None:
    """多人共用的连胜 / 连败场数文案；场数不一致时返回 None。

    一句里只有一个 {n}，装不下多份数字：3 连胜和 4 连胜并成一句，无论填哪个
    都是在替另一个人报错。因此只有**大家场数完全相同**时才敢合并，此时填那个
    共有的场数（「P1、P2 都是 3 连胜」）。场数不同 -> None，各说各的。
    """
    nums = {max(win, lose) for win, lose in streaks}
    if len(nums) != 1:
        return None
    return str(next(iter(nums)))


def roast_one(
    stats: dict,
    ctx: dict,
    name: str,
    streak: tuple[int, int] = (0, 0),
    used: set[str] | None = None,
    rng=random,
    lock: str | None = None,
    n_text: str | None = None,
) -> str:
    """为一位玩家生成一句锐评。

    used 为同一场比赛内已用过的句子集合，用于尽量避免同局多人撞词。
    lock 为归堆阶段定下的维度：给了就锁定它（至多再带上英雄梗），不再重新
    抽签，避免把别的组已经说过的维度又说一遍（见 _solo_pool）。
    n_text 为多人共用一句时的连胜 / 连败场数文案（见 _streak_text）。
    """
    if lock is None:
        candidates = _candidates_for(stats, ctx, streak)
    else:
        candidates = _solo_pool(lock, stats, ctx, streak, allow_hero=True)
    key = _pick_key(candidates, ctx["win"], rng, _hero_id_of(stats))
    if key is not None:
        line = _render_one(key, stats, ctx, name, streak, used, rng, n_text)
        if line:
            return line
    # 命中的维度在本局胜负下没有句子（例如 lh_high 只写了 _win 组），
    # 退回兜底组——绝不能输出「这局打得一言难尽」这种占位文案。
    fallback = _fallback_keys(
        ctx["win"], is_positive(stats, ctx["win"], ctx.get("peer"), ctx.get("peer_total", 0))
    )
    key = _pick_key([(k, _WEIGHTS.get(k, 3)) for k in fallback], ctx["win"], rng)
    if key is not None:
        line = _render_one(key, stats, ctx, name, streak, used, rng, n_text)
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


# 含单人数据的句子不能合并；{n} 在场数相同时可合并（由 merge_n 放行）
_MERGE_BLOCKERS = ("{kills}", "{deaths}", "{assists}", "{hero}")


def _merge_safe(line: str, merge_n: bool = False) -> bool:
    """这句能不能同时套在多人身上（不含各自的数值 / 英雄 / 连胜场数）。

    merge_n 为真表示这几个人的连胜 / 连败场数完全相同（见 _streak_text），
    {n} 就成了大家共有的一份数字，可以合并；否则 {n} 与各自的数值一样不可合并。
    """
    blockers = _MERGE_BLOCKERS if merge_n else (*_MERGE_BLOCKERS, "{n}")
    return not any(p in line for p in blockers)


def roast_players(
    player_list: list,
    match_info: dict,
    streaks: dict[int, tuple[int, int]] | None = None,
    rng=random,
) -> str:
    """为一局的每位订阅玩家生成锐评，返回多行文本。

    同维度多人合并成一句；英雄梗只在独占维度时参与。
    streaks 为 {steam_id: (连胜, 连败)}，可省略。
    """
    streaks = streaks or {}
    # 同场 10 人名次只算一次，供同局所有玩家复用
    ranks = peer_ranks(match_info)
    used: set[str] = set()

    # 先归堆再选句；兜底组也参与归堆，避免同一句被说两遍
    picks: list[tuple[str | None, dict, dict, tuple[int, int], str, int | None]] = []
    for player in player_list:
        stats = player.stats
        ctx = team_context(match_info, stats.get("dota2_team"), stats)
        ctx["peer"] = ranks.get(player.short_steamID) or {}
        streak = streaks.get(player.short_steamID, (0, 0))
        hero_id = _hero_id_of(stats)
        dims, _hero = _split_hero(_candidates_for(stats, ctx, streak))
        key = _pick_key(dims, ctx["win"], rng, hero_id)
        if key is None:
            # 命中的维度在本局胜负下没有句子 -> 兜底组（win_*/lose_* 之一）
            key = _pick_key(_fallback_candidates(stats, ctx), ctx["win"], rng, hero_id)
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
            # 兜底组也没句子（句库被删空）：逐人走 roast_one 走最后保底
            for _, m_stats, m_ctx, m_streak, m_name, _m_hero in members:
                lines.append(roast_one(m_stats, m_ctx, m_name, m_streak, used, rng))
            continue
        # 连胜 / 连败按场数再分组，避免一句里套错 {n}
        if len(members) > 1 and key in _STREAK_KEYS:
            by_n: dict[int, list] = {}
            for m in members:
                by_n.setdefault(max(m[3][0], m[3][1]), []).append(m)
            subgroups = list(by_n.values())
        else:
            subgroups = [members]

        for sub in subgroups:
            _, m0_stats, m0_ctx, m0_streak, _, m0_hero = sub[0]
            merged = False
            if len(sub) > 1:
                # 同组场数相同，{n} 为共有数字，可合并成一句
                n_text = _streak_text([m[3] for m in sub]) if key in _STREAK_KEYS else None
                n_ok = n_text is not None
                # 只取能同时套在所有人身上的句子；没有就还是各说各的
                pool = [
                    ln
                    for ln in _lines_for(key, m0_ctx["win"], m0_hero)
                    if _merge_safe(ln, n_ok)
                ]
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
                                m0_stats,
                                m0_ctx,
                                _join_names([m[4] for m in sub]),
                                m0_streak,
                                n_text,
                            )
                        )
                    )
                    merged = True
            if merged:
                continue
            for _, m_stats, m_ctx, m_streak, m_name, m_hero in sub:
                # 多人共享这个维度但句子不可合并（例如各自的数值 / 英雄）：
                # 各说各的，但维度仍然锁死在这个 key 上，不许重抽到别人说过的维度。
                line = roast_one(m_stats, m_ctx, m_name, m_streak, used, rng, lock=key)
                lines.append(line)
    return "\n".join(lines)
