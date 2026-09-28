"""OpenDota / Steam Web API 请求封装（httpx 实现）。"""

from __future__ import annotations

from ..config import (
    OPENDOTA_MATCH_URL,
    STEAM_MATCH_DETAILS_URL,
    STEAM_MATCH_HISTORY_URL,
    STEAM_NEWS_URL,
    config,
)
from ..services.player import Player
from ..utils import DOTA2HTTPError, get_json
from .xiaoheihe import request_match_info_xiaoheihe

# Steam Web API Key（未配置时相关播报会报错）
API_KEY = config.d2w_steam_api_key


async def request_match_history(player: Player, api_key: str | None = None) -> int:
    """获取玩家最近一场比赛的 ID。"""
    api_key = api_key or API_KEY
    url = STEAM_MATCH_HISTORY_URL.format(key=api_key, account_id=player.short_steamID)
    match = await get_json(url)
    if match.get("result", {}).get("status") == 15:
        raise DOTA2HTTPError(f"{player.nickname}的战绩被隐藏了,无法获取")
    matches = (match.get("result") or {}).get("matches") or []
    if not matches:
        raise DOTA2HTTPError(f"无法获取{player.nickname}的最近比赛ID")
    return matches[0]["match_id"]


async def request_match_info_steam(match_id: int, api_key: str | None = None) -> dict | None:
    """通过 Steam 官方 API 获取比赛详情。

    注意：7.36 版本后此接口已无法获取比赛结果，请优先使用 openDota。
    """
    api_key = api_key or API_KEY
    url = STEAM_MATCH_DETAILS_URL.format(key=api_key, match_id=match_id)
    data = await get_json(url)  # 网络/状态码异常在本层抛出，保留统一文案
    try:
        return (data or {}).get("result")
    except Exception:
        raise DOTA2HTTPError("DOTA2开黑战报生成失败")


async def _fetch_opendota_match(match_id: int) -> dict | None:
    """拉取 openDota 比赛详情；网络失败、解析失败或无玩家数据时返回 None。"""
    try:
        data = await get_json(OPENDOTA_MATCH_URL.format(match_id=match_id))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("players") else None


async def request_match_info_opendota(match_id: int, api_key: str | None = None) -> dict | None:
    """通过 openDota 获取比赛详情；获取不到时回退到小黑盒公开接口。

    openDota 免费版每天仅 2000 次访问，且偶发 522/超时；当 openDota 不可用、
    返回空或没有玩家数据时，改由小黑盒公开接口兜底（无需 Cookie / 登录）。
    """
    return await _fetch_opendota_match(match_id) or await request_match_info_xiaoheihe(match_id)


async def request_news() -> dict:
    """获取 DOTA2 官方新闻（最新 5 条，events 按时间倒序）。"""
    try:
        return await get_json(STEAM_NEWS_URL)
    except ValueError:
        raise DOTA2HTTPError("DOTA2新闻更新失败：返回内容不是 JSON")
