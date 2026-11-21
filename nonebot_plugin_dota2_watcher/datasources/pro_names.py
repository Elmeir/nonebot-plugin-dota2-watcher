"""OpenDota 职业选手表：account_id -> 职业名 / 队伍 / 头像。

接口：https://api.opendota.com/api/proPlayers（免 Key，全量 5000+ 条）。
缓存：data/pro_names.json，默认 24 小时。
用于战队名单与 /pro 的显示名；查不到时由调用方回退 Steam 昵称。
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from ..config import DATA_DIR, OPENDOTA_PRO_PLAYERS_URL
from ..utils import get_json, load_cache

CACHE_FILE = DATA_DIR / "pro_names.json"
CACHE_VERSION = 3  # 保留字段变化时递增，使旧缓存自动失效重新拉取
CACHE_TTL = 24 * 3600  # 职业名稳定，但转会会改队伍，故按天刷新

# 进程内索引：{account_id: {"name","personaname","team_name","team_tag",...}}
_index: dict[int, dict] | None = None
_index_at = 0.0

_KEEP_FIELDS = (
    "name",
    "personaname",
    "team_name",
    "team_tag",
    "avatar",
    "avatarmedium",
    "fantasy_role",
)

# fantasy_role 的排序权重（0/3 多为未收录选手）
ROLE_ORDER = {1: 0, 4: 1, 2: 2}
ROLE_UNKNOWN = 3  # 0 / 3 / None 及未收录值统一沉底


def role_sort_key(info: dict | None) -> int:
    """fantasy_role -> 排序键：核心(1) -> 中单(4) -> 辅助(2) -> 其他(0/3/None)。"""
    return ROLE_ORDER.get((info or {}).get("fantasy_role"), ROLE_UNKNOWN)


def _to_index(players: list[dict]) -> dict[int, dict]:
    """把接口返回的列表转成 {account_id: 精简字段} 索引。"""
    index: dict[int, dict] = {}
    for p in players:
        aid = p.get("account_id")
        if not aid:
            continue
        try:
            key = int(aid)
        except (TypeError, ValueError):
            continue
        index[key] = {k: p.get(k) for k in _KEEP_FIELDS if p.get(k)}
    return index


def _read_cache() -> dict[int, dict] | None:
    """读取磁盘缓存；版本不符/损坏返回 None。"""
    data = load_cache(CACHE_FILE)
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    return _to_index(data.get("players") or [])


def _write_cache(players: list[dict]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    import json

    CACHE_FILE.write_text(
        json.dumps({"version": CACHE_VERSION, "players": players}, ensure_ascii=False),
        encoding="utf-8",
    )


async def load_index(force: bool = False, max_age: int = CACHE_TTL) -> dict[int, dict]:
    """返回全量职业选手索引；拉取失败时回退磁盘缓存，都不可用则返回空表。

    调用方不应假定一定命中，应自行准备回退显示名。
    """
    global _index, _index_at
    now = time.time()
    if _index is not None and not force and now - _index_at < max_age:
        return _index

    cached = _read_cache()
    if cached is not None and not force:
        try:
            age = now - CACHE_FILE.stat().st_mtime
        except OSError:
            age = max_age
        if age < max_age:
            _index, _index_at = cached, now
            return _index

    try:
        raw = await get_json(OPENDOTA_PRO_PLAYERS_URL)
        players = [p for p in (raw or []) if isinstance(p, dict)]
        if not players:
            raise ValueError("职业选手表返回为空")
        _write_cache(players)
        _index, _index_at = _to_index(players), now
    except Exception:
        # 拉取失败：回退磁盘缓存（可能已过期但仍可用），否则空表
        _index, _index_at = (cached or {}), now
    return _index


async def resolve(ids: Iterable[int]) -> dict[int, dict]:
    """批量解析账号信息，返回 {account_id: 字段字典}（仅含命中项）。"""
    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    index = await load_index()
    return {i: index[i] for i in wanted if i in index}


async def display_names(pairs: dict[int, str] | None = None, ids: Iterable[int] = ()) -> dict[int, str]:
    """返回 {account_id: 显示名}：职业名优先，缺失时回退传入的 Steam 昵称。

    pairs 为 {account_id: Steam 昵称}（由 GetPlayerSummaries 解析而来，可为空）。
    """
    wanted = {int(i) for i in ids if i}
    if pairs:
        wanted |= {int(k) for k in pairs if k}
    if not wanted:
        return {}
    index = await load_index()
    result: dict[int, str] = {}
    for i in sorted(wanted):
        info = index.get(i) or {}
        name = info.get("name") or (pairs or {}).get(i) or info.get("personaname")
        result[i] = name or str(i)
    return result


def pro_name(account_id: int) -> str:
    """同步取职业名（索引已加载时）；未命中返回空串。

    适用于已在别处 await 过 load_index 的场景（如批量解析后的二次取用）。
    """
    info = (_index or {}).get(int(account_id))
    return (info or {}).get("name") or ""
