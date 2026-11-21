"""D2PT 位置数据：抓取、缓存与解析。"""

from typing import Any

from nonebot.log import logger

from ..config import D2PT_POS_URL, DATA_DIR, config
from ..dota_dicts import HEROES_LIST_CHINESE
from ..utils import cache_with_fallback, dumpjson, get_json

CACHE_EXPIRE_SECONDS = config.d2w_cache_expire_seconds  # 缓存时长（秒）
POS_RAW_FILE = DATA_DIR / "d2pt_pos.json"  # 远程合并的全位置原始数据缓存
POS_DATA_FILE = DATA_DIR / "d2pt_data.json"  # 解析后的全位置数据
_URL = D2PT_POS_URL


async def fetch_d2pt_cheatsheet() -> dict:
    """从远程拉取合并后的全位置原始数据，并写入本地缓存。"""
    d2pt_data = await get_json(_URL)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dumpjson(d2pt_data, POS_RAW_FILE)
    return d2pt_data


async def get_data_with_cache() -> dict:
    """读取全位置原始数据，缓存过期或缺失时自动拉取；
    远程拉取失败时回退使用本地缓存（即使已过期）。"""
    return await cache_with_fallback(
        POS_RAW_FILE,
        fetch_d2pt_cheatsheet,
        CACHE_EXPIRE_SECONDS,
        warn=lambda: logger.warning("D2PT 远程数据拉取失败，回退使用本地缓存"),
    )


def parse_data_pos(pos: int, data: list) -> list:
    """将原始数据解析为按综合评分排序的英雄列表。"""
    if not data:
        return []
    matches_filter = max(h.get("matches", 0) for h in data) / 10

    result = []
    for h in data:
        hero_id = h["hero_id"]
        hero_name_cn = HEROES_LIST_CHINESE.get(hero_id, str(hero_id))
        matches = h.get("matches", 0)
        wins = h.get("wins", 0)
        lane = h.get("detailed_stats", {}).get("lane_avg_adv_pct", 0)
        if lane < 0.01 and lane > -0.01:
            lane_score = lane
        elif pos < 5:
            lane_score = (abs(lane * 100) ** 0.5) * (-1 if lane < 0 else 1) / 100
        else:
            lane_score = lane
        win_rate = wins / matches if matches else 0
        score = win_rate + lane_score
        if matches >= matches_filter and win_rate >= 0.5:
            result.append(
                {
                    "id": hero_id,
                    "name": hero_name_cn,
                    "npc": h.get("npc", ""),  # 英雄短名（排行图图标用）
                    "WR": f"{win_rate:.1%}",
                    "lane": f"{lane:.1%}",
                    "lane_score": f"{lane_score:.1%}",
                    "score": f"{score:.1%}",
                }
            )
    result.sort(key=lambda item: item["score"], reverse=True)
    return result


async def parse_data(force_update: bool = False) -> dict:
    """抓取/刷新全部 1-5 号位数据，并缓存到 d2pt_data.json。"""
    raw = await get_data_with_cache()
    pos_all: dict[str, Any] = {}
    for pos in (1, 2, 3, 4, 5):
        raw_pos = (raw or {}).get(f"pos{pos}", [])
        if raw_pos:
            pos_all[str(pos)] = parse_data_pos(pos, raw_pos)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dumpjson(pos_all, POS_DATA_FILE)
    return pos_all


async def load_data(force_update: bool = False) -> dict | None:
    """读取合并后的全位置数据；d2pt_data.json 缓存过期或缺失时重新解析。
    远程拉取失败时回退使用本地缓存（即使已过期）。"""
    return await cache_with_fallback(
        POS_DATA_FILE,
        lambda: parse_data(force_update),
        CACHE_EXPIRE_SECONDS,
        force_update=force_update,
        warn=lambda: logger.warning("D2PT 远程数据拉取失败，回退使用本地缓存"),
    )
