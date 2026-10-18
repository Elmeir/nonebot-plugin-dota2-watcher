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

# 比赛 ID 的来源。决定比赛详情该走哪个接口：
# 小黑盒取到的 ID 必须用小黑盒详情接口 —— 这类玩家的战绩在 OpenDota 里是匿名的，
# 无法按 account_id 定位到本人，用 OpenDota 数据会导致「找不到该玩家」而播报失败。
SOURCE_STEAM = "steam"
SOURCE_XIAOHEIHE = "xiaoheihe"


# Steam GetMatchHistory 用 status 15 表示「玩家未公开比赛历史」。
# 这是唯一需要回退小黑盒的情形：小黑盒对这类玩家仍能拿到匿名数据。
HIDDEN_STATUS = 15


class MatchHistoryHidden(DOTA2HTTPError):
    """玩家战绩被隐藏（Steam 返回 status 15）。

    独立成一个异常类型，便于上层把它与网络错误区分开：
    只有它才累加「隐藏失败」计数并触发小黑盒回退。
    """


async def request_match_history(player: Player, api_key: str | None = None) -> int:
    """获取玩家最近一场比赛的 ID。

    战绩被隐藏时抛 MatchHistoryHidden，其余失败抛 DOTA2HTTPError。
    """
    api_key = api_key or API_KEY
    url = STEAM_MATCH_HISTORY_URL.format(key=api_key, account_id=player.short_steamID)
    match = await get_json(url)
    if match.get("result", {}).get("status") == HIDDEN_STATUS:
        raise MatchHistoryHidden(f"{player.nickname}的战绩被隐藏了,无法获取")
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


async def request_match_info_xiaoheihe_only(match_id: int) -> dict | None:
    """只用小黑盒公开接口获取比赛详情；失败时返回 None（不回退 openDota）。

    用于「比赛 ID 来自小黑盒」的场景：这类玩家的战绩在 OpenDota 里是匿名的，
    拿 OpenDota 数据会定位不到本人，播报时反而被判成「数据无法获取」，
    因此宁可本轮跳过，等小黑盒接口恢复后再播。
    """
    try:
        return await request_match_info_xiaoheihe(match_id)
    except Exception:
        return None


async def request_match_info(
    match_id: int, source: str = SOURCE_STEAM, api_key: str | None = None
) -> dict | None:
    """按比赛 ID 的来源选择详情接口。

    source 为 SOURCE_XIAOHEIHE 时只走小黑盒（ID 由小黑盒给出，OpenDota 里该玩家匿名）；
    其余情况沿用「openDota 优先、小黑盒兜底」的默认策略。
    """
    if source == SOURCE_XIAOHEIHE:
        return await request_match_info_xiaoheihe_only(match_id)
    return await request_match_info_opendota(match_id, api_key)


async def request_news() -> dict:
    """获取 DOTA2 官方新闻（最新 5 条，events 按时间倒序）。"""
    try:
        return await get_json(STEAM_NEWS_URL)
    except ValueError:
        raise DOTA2HTTPError("DOTA2新闻更新失败：返回内容不是 JSON")
