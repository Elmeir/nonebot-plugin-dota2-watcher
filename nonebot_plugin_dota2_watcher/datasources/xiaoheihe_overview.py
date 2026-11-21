"""小黑盒玩家战绩总览（/战绩 数据源）。

接口：v5api.maxjia.com/game/dota2/player/overview?steam_id=...（免签名）。
原始响应按 steam_id 缓存到 data/xiaoheihe_overview/，有效期见
d2w_xiaoheihe_cache_seconds；请求失败时回退旧缓存。
"""

from __future__ import annotations

import json
from pathlib import Path

from nonebot.log import logger

from ..config import DATA_DIR, config
from ..utils import DOTA2HTTPError, cache_with_fallback, get_json, load_cache

# 免签名的 overview 接口（v5api 域名，与 match_detail 的 api 域名不同）
API_URL = "https://v5api.maxjia.com/game/dota2/player/overview"
HEADERS = {
    "User-Agent": "ApiMaxJia/1.0",
    "Referer": "http://api.maxjia.com/",
}

CACHE_DIR = DATA_DIR / "xiaoheihe_overview"
CACHE_VERSION = 1

# 接口对不存在的玩家也会下发空壳模块，需同时检查列表与数值
CONTENT_MODULES = {
    "recent_match": "match_list",
    "match_list": "match_list",
    "common_hero": "status_list",
    "r20_data_stats": "recent_stats_list",
    "career_record": "career_record",
}

# notify 只表示部分数据缺失，不能作为「无战绩」的判定依据
NOTICE_MODULE = "notify"

# 视为「没有数值」的取值：接口用 0 / 0.0 / 0.0% 表示该统计项无数据
_ZERO_VALUES = {"", "0", "0.0", "0.0%", "0%"}


def _has_meaningful(items) -> bool:
    """列表里是否存在真实条目（数值不能全是 0，否则只是接口的占位行）。"""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if value and value not in _ZERO_VALUES:
            return True
        # 比赛列表里的条目没有 value，但只要有英雄/模式就说明是真比赛
        if item.get("hero_icon") or item.get("hero_image") or item.get("module"):
            return True
    return False


class OverviewError(Exception):
    """战绩总览查询失败（供上层转为用户提示）。"""


def _cache_path(steam_id) -> Path:
    """返回指定 steam 账号对应的缓存文件路径。"""
    return CACHE_DIR / f"overview_{int(steam_id)}.json"


def _load_cache(cache_path: Path, steam_id):
    """读取缓存；命中（结构/账号一致）即返回原始响应，否则 None。"""
    data = load_cache(cache_path)
    if not isinstance(data, dict) or data.get("status") != "ok":
        return None
    if str((data.get("result") or {}).get("steam_id")) != str(int(steam_id)):
        return None
    return data


def _save_cache(cache_path: Path, data: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def fetch_overview(steam_id, refresh: bool = False) -> dict:
    """拉取玩家战绩总览原始响应。

    缓存有效期内直接返回本地缓存；``refresh=True`` 时强制重新拉取。
    拉取失败（网络/接口异常）且本地有缓存时回退缓存，否则抛 OverviewError。
    """
    steam_id = int(steam_id)
    cache_path = _cache_path(steam_id)

    async def _fetch() -> dict:
        try:
            data = await get_json(
                API_URL,
                params={"steam_id": str(steam_id)},
                headers=HEADERS,
                timeout=config.d2w_timeout,
            )
        except DOTA2HTTPError as e:
            raise OverviewError(str(e)) from e
        except Exception as e:  # 解析失败等
            raise OverviewError(f"小黑盒战绩接口返回异常：{e}") from e

        if not isinstance(data, dict) or data.get("status") != "ok":
            msg = (data or {}).get("msg") if isinstance(data, dict) else None
            raise OverviewError(f"小黑盒战绩接口返回异常：{msg or '无有效数据'}")
        try:
            _save_cache(cache_path, data)
        except Exception as e:
            logger.warning(f"小黑盒战绩缓存写入失败：{e}")
        return data

    max_age = None if refresh else config.d2w_xiaoheihe_cache_seconds
    return await cache_with_fallback(
        cache_path,
        _fetch,
        max_age=max_age,
        force_update=refresh,
        loader=lambda p: _load_cache(p, steam_id),
        warn=lambda: logger.warning(f"小黑盒战绩抓取失败，回退本地缓存 {cache_path.name}"),
    )


# ============================================================
# 数据解析
# ============================================================
def modules(data: dict) -> dict[str, dict]:
    """把 data_list 按 item_type 建索引（同名只取第一个，保持页面顺序）。"""
    index: dict[str, dict] = {}
    for item in (data.get("result") or {}).get("data_list") or []:
        index.setdefault(item.get("item_type"), item)
    return index


def has_any_content(data: dict) -> bool:
    """是否存在可展示的战绩内容。

    接口对不存在的玩家返回空壳；notify 只表示部分数据缺失，
    因此只能检查各模块的实际条目。
    """
    index = modules(data)
    return any(
        _has_meaningful((index.get(item_type) or {}).get(list_key))
        for item_type, list_key in CONTENT_MODULES.items()
    )


def notice_message(data: dict) -> str:
    """取接口下发的说明文案（如「该玩家未公开战绩，数据可能有所缺失」），无则空串。

    该文案是「数据缺失」的提醒，不代表没有战绩；仅在确实无内容时才作为提示语。
    """
    notice = modules(data).get(NOTICE_MODULE) or {}
    return str(notice.get("msg") or "").strip()


def player_name(data: dict) -> str:
    """取玩家昵称（用于提示文案与图片标题）。"""
    header_info = (modules(data).get("header") or {}).get("header_info") or {}
    return str(header_info.get("name") or "").strip()


def recent_match_ids(data: dict) -> list[int]:
    """按「新 → 旧」返回 overview 里能拿到的比赛 ID 列表（去重、已过滤非法值）。

    数据来自 ``recent_match``（近 20 场方块）与 ``match_list``（比赛列表），
    两者顺序本身都是新→旧，合并后按 ID 降序排序即可（比赛 ID 全局单调递增）。
    """
    ids: list[int] = []
    for item_type in ("recent_match", "match_list"):
        for match in (modules(data).get(item_type) or {}).get("match_list") or []:
            try:
                mid = int(match.get("match_id"))
            except (TypeError, ValueError):
                continue
            if mid > 0:
                ids.append(mid)
    return sorted(set(ids), reverse=True)


def latest_match_id(data: dict) -> int:
    """取 overview 里最新的比赛 ID；没有任何比赛时返回 0。

    仅用于「战绩被隐藏」时的回退：小黑盒能拿到匿名玩家的比赛列表，
    但数据可能滞后，调用方仍需与本地已播报的 ID 比较后再决定是否采纳。
    """
    ids = recent_match_ids(data)
    return ids[0] if ids else 0
