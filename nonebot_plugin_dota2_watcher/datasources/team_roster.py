"""Valve 战队登记名单数据源。

接口：IDOTA2Match_570/GetTeamInfoByTeamID/v001（需 Steam Web API Key）

实测要点（与官方文档有出入，务必注意）：
  - 响应中**没有 team_id 字段**，只能用 name/tag/abbreviation 校验是否命中；
  - start_at_team_id 是**游标**而非精确匹配：若该 team_id 不存在会返回下一支队伍；
  - 每队返回 6~7 人（player_0..N_account_id），为「现役 + 替补/教练」，
    Valve **不提供任何字段区分现役与替补**，因此不能当作"五人首发"使用；
  - 名单会被维护（实测 Team Liquid 建队 13 年仅返回 7 人，2019 年离队的
    MATUMBAMAN / Miracle- 已被移除），故可用于「加入 / 离开」变动检测；
  - 存在跨队残留（同一 account_id 可能同时挂在两支队伍），"离开"事件可能滞后。

显示名：优先用 pro_names 的职业名（Steam 昵称会随意更改，如 Ame 当前叫
"NothingToGay"），查不到时用 GetPlayerSummaries 的昵称兜底。
"""

from __future__ import annotations

import asyncio
import json
import re

from ..config import (
    DATA_DIR,
    STEAM_PLAYER_SUMMARIES_URL,
    STEAM_TEAM_INFO_URL,
    config,
)
from ..utils import get_json, load_cache
from . import pro_names

# 已知战队表：TI2026（league_id=19719）参赛队 + 其它常用队伍，
# 来源均为官方 GetLeagueData 的 team_standings / OpenDota 战队数据。
# 仅用于「队名/缩写 -> team_id」的解析；表外队伍可直接给 team_id。
KNOWN_TEAMS: dict[int, str] = {
    2163: "Team Liquid",
    726228: "Vici Gaming",
    2586976: "OG",
    5017210: "Team Resilience",
    7119388: "Team Spirit",
    8255888: "BoomBoys",
    8261500: "Xtreme Gaming",
    9247354: "Team Falcons",
    9467224: "Aurora Gaming",
    9572001: "TEAM VISION",
    9823272: "Team Yandex",
    9964962: "GamerLegion",
    10136357: "Nigma Galaxy",
    10149530: "HULIGANI",
    10150413: "Iron Wing",
    10150538: "LGD Gaming",
    9351740: "Yakult Brothers",
    10007878: "Team Refuser",
}

# 官方缩写（取自 Valve 接口的 abbreviation 字段）。队名里往往不含缩写子串
# （如 "Xtreme Gaming" 中并无 "xg"），故单独建表用于匹配。
TEAM_ABBRS: dict[int, str] = {
    2163: "LIQ",
    726228: "VG",
    2586976: "OG",
    5017210: "RES",
    7119388: "TS",
    8255888: "BB",
    8261500: "XG",
    9247354: "FLCN",
    9467224: "AUR",
    9572001: "VSN",
    9823272: "YAN",
    9964962: "GL",
    10136357: "NGX",
    10149530: "HU",
    10150413: "IW",
    10150538: "LGD",
    9351740: "YB",
    10007878: "TR",
}

# 中国赛区战队集合（/阵容 CN 按此顺序输出：XG、YB、VG、LGD、RES、TR）
CN_TEAMS: tuple[int, ...] = (
    8261500,   # Xtreme Gaming
    9351740,   # Yakult Brothers
    726228,    # Vici Gaming
    10150538,  # LGD Gaming
    5017210,   # Team Resilience
    10007878,  # Team Refuser
)

# Steam 官方限流 1 req/s
REQUEST_INTERVAL = 1.2
_STEAM64_BASE = 76561197960265728

# 名单快照：用于与上次结果比对，检测成员加入 / 离开
STATE_FILE = DATA_DIR / "team_rosters.json"
STATE_VERSION = 1


def _norm(text: str) -> str:
    """队名归一化：转小写并去掉非字母数字字符，用于宽松匹配。"""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def team_name(team_id: int) -> str:
    """取队伍显示名；不在已知表中则返回 'Team{id}'。"""
    return KNOWN_TEAMS.get(int(team_id), f"Team{team_id}")


def is_cn_query(query: str) -> bool:
    """是否为「中国战队」查询（/阵容 CN / china / 中国）。"""
    return (query or "").strip().lower() in {"cn", "china", "中国"}


def resolve_team(query: str) -> int | None:
    """把用户输入的队名 / 缩写 / team_id 解析为 team_id。

    匹配顺序：纯数字 -> 队名精确 -> 官方缩写精确 -> 队名包含匹配。
    归一化时忽略大小写与非字母数字字符；未命中返回 None。
    """
    q = (query or "").strip()
    if not q:
        return None
    if q.isdigit():
        return int(q)
    target = _norm(q)
    if not target:
        return None
    for tid, name in KNOWN_TEAMS.items():
        if _norm(name) == target:
            return tid
    for tid, abbr in TEAM_ABBRS.items():
        if _norm(abbr) == target:
            return tid
    # 包含匹配：支持 "liquid"、"spirit"、"falcons" 等片段
    for tid, name in KNOWN_TEAMS.items():
        if target in _norm(name):
            return tid
    return None


def _roster_ids(team: dict) -> list[int]:
    """按 player_0/1/2... 顺序抽出 account_id（跳过空位）。"""
    ids: list[int] = []
    n = 0
    while f"player_{n}_account_id" in team:
        raw = team[f"player_{n}_account_id"]
        if raw:
            ids.append(int(raw))
        n += 1
    return ids


def steam_key() -> str:
    """取可用的 Steam Key（TI 专用 Key 优先，回退通用 Key）。"""
    return config.d2w_ti_steam_api_key or config.d2w_steam_api_key


async def fetch_team(team_id: int, key: str = "") -> dict | None:
    """拉取一支队伍的登记名单。

    返回 {'team_id','name','tag','ids': [account_id,...]}；
    未命中（游标错位到其它队伍）或无数据时返回 None。
    """
    key = key or steam_key()
    if not key:
        return None
    await asyncio.sleep(REQUEST_INTERVAL)  # Steam 限流 1 req/s
    data = await get_json(
        STEAM_TEAM_INFO_URL,
        params={"key": key, "start_at_team_id": int(team_id), "teams_requested": 1},
    )
    teams = (data.get("result") or {}).get("teams") or []
    if not teams:
        return None
    team = teams[0]
    name = (team.get("name") or "").strip()
    # 已知队伍必须校验队名，避免游标落到下一支队伍后返回错误名单
    expect = KNOWN_TEAMS.get(int(team_id))
    if expect and name.lower() != expect.strip().lower():
        return None
    ids = _roster_ids(team)
    if not ids:
        return None
    return {
        "team_id": int(team_id),
        "name": name or team_name(team_id),
        "tag": team.get("tag") or team.get("abbreviation") or "",
        "ids": ids,
    }


async def fetch_steam_names(ids: list[int], key: str = "") -> dict[int, str]:
    """用 GetPlayerSummaries 取 Steam 昵称（pro_names 未命中时的兜底）。

    注意：返回顺序不保证与请求一致，必须按 steamid 回映射。
    失败返回空表（调用方会退化为显示 account_id）。
    """
    ids = [int(i) for i in ids if i]
    key = key or steam_key()
    if not ids or not key:
        return {}
    await asyncio.sleep(REQUEST_INTERVAL)
    data = await get_json(
        STEAM_PLAYER_SUMMARIES_URL,
        params={"key": key, "steamids": ",".join(str(i + _STEAM64_BASE) for i in ids)},
    )
    result: dict[int, str] = {}
    for p in (data.get("response") or {}).get("players") or []:
        try:
            aid = int(p.get("steamid")) - _STEAM64_BASE
        except (TypeError, ValueError):
            continue
        if p.get("personaname"):
            result[aid] = p["personaname"]
    return result


async def sort_by_role(ids: list[int]) -> list[int]:
    """按 fantasy_role 排序：核心(1) -> 中单(4) -> 辅助(2) -> 其他。

    无职业名的成员（多为教练/替补或未收录选手）统一排到最后；
    同一分类内保持 Valve 返回的原始顺序（稳定排序）。
    """
    ids = [int(i) for i in ids]
    if len(ids) <= 1:
        return ids
    infos = await pro_names.resolve(ids)

    def sort_key(i: int) -> tuple[int, int]:
        info = infos.get(i)
        return (0 if info and info.get("name") else 1, pro_names.role_sort_key(info))

    return sorted(ids, key=sort_key)


async def members(team_id: int, key: str = "") -> tuple[str, list[tuple[int, str]]]:
    """取一支队伍的成员显示名列表（按 fantasy_role 排序）。

    返回 (队名, [(account_id, 显示名), ...])；显示名优先职业名，缺失回退 Steam 昵称。
    队伍不可用时返回 (队名, [])。
    """
    info = await fetch_team(team_id, key)
    if not info:
        return team_name(team_id), []
    await pro_names.load_index()
    ordered = await sort_by_role(info["ids"])
    names = await pro_names.display_names(ids=ordered)
    # 未解析出职业名的成员（多为教练/替补），用 Steam 昵称兜底
    missing = [i for i in ordered if not pro_names.pro_name(i)]
    if missing:
        for aid, nick in (await fetch_steam_names(missing, key)).items():
            names[aid] = nick
    return info["name"], [(i, names.get(i, str(i))) for i in ordered]


def load_state() -> dict:
    """读取名单快照 {'version':1,'teams':{tid:{'name','ids'}}}；损坏返回空表。"""
    data = load_cache(STATE_FILE)
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION, "teams": {}}
    data.setdefault("teams", {})
    return data


def save_state(state: dict) -> None:
    """持久化名单快照。失败静默忽略（下一轮会重新建立基线）。"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
