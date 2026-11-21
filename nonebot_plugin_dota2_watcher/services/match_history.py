"""订阅玩家的最近比赛：Steam 优先，战绩被隐藏时按门控回退小黑盒。

Steam 对隐藏战绩返回 status 15；小黑盒 overview 仍能给出匿名比赛列表。
为避免触发小黑盒风控，连续隐藏 N 次才回退一次，每次尝试后计数归零。
两个数据源的「最近一场」可能不同步，统一按比赛 ID 大小判重。
overview 只在回退时写缓存，供 /战绩 复用；回退本身不读缓存。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nonebot.log import logger

from ..config import config
from ..datasources import xiaoheihe_overview as overview
from ..datasources.request_match import (
    SOURCE_STEAM,
    SOURCE_XIAOHEIHE,
    MatchHistoryHidden,
    request_match_history,
)
from .player import Player

# Steam 有速率限制，用信号量限制历史拉取并发；小黑盒回退稀疏，不占配额
_history_semaphore = asyncio.Semaphore(config.d2w_history_concurrency)


@dataclass(frozen=True)
class RecentMatch:
    """一次「最近比赛」查询的结果；match_id 为 0 表示本轮没有可播报的比赛。"""

    match_id: int = 0
    source: str = SOURCE_STEAM

    def __bool__(self) -> bool:
        return self.match_id > 0


def latest_known_match_id(player: Player) -> int:
    """玩家已知的最新比赛 ID（Steam 与小黑盒两个来源取较大者）。"""
    return max(int(player.last_DOTA2_match_ID or 0), int(player.last_xiaoheihe_match_id or 0))


def is_newer_match(match_id, player: Player) -> bool:
    """该比赛是否比已知最新比赛更新（严格大于才值得播报）。

    两个数据源交叉时用它判重：等于已播报的比赛不重复播报，
    小于已知最新的（小黑盒滞后返回的旧比赛）同样跳过，避免倒序播报。
    """
    return int(match_id or 0) > latest_known_match_id(player)


def _hidden_threshold() -> int:
    """回退阈值；配置为 0 或负数时按 1 处理（等价于每次都回退）。"""
    return max(1, int(config.d2w_hidden_fallback_threshold or 1))


async def _xiaoheihe_recent_match(player: Player) -> RecentMatch:
    """从小黑盒 overview 取最近比赛 ID；无数据或不比已知最新更新时返回空结果。

    必须 refresh=True 绕开 overview 的「/战绩」缓存（d2w_xiaoheihe_cache_seconds,
    默认 1 小时）：轮询关心的是「现在有没有新比赛」，读旧缓存会拿到过期列表，
    导致新比赛最长被压到缓存过期才发现（或反而播一场已过时的旧比赛）。
    访问频率由上面的门控限制（每 threshold 轮才调到这里），无需再用缓存节流；
    拉取失败时 fetch_overview 仍会回退本地缓存，不会让本轮回退直接失败。
    """
    data = await overview.fetch_overview(player.short_steamID, refresh=True)
    match_id = overview.latest_match_id(data)
    if not match_id:
        logger.info(f"{player.nickname} 小黑盒也没有可用的比赛记录")
        return RecentMatch()
    if not is_newer_match(match_id, player):
        logger.info(
            f"{player.nickname} 小黑盒最近比赛 {match_id} 不比已知最新 "
            f"{latest_known_match_id(player)} 更新，跳过"
        )
        return RecentMatch()
    logger.info(f"{player.nickname} 小黑盒最近比赛 {match_id} 比已知最新更新，采用")
    return RecentMatch(match_id=match_id, source=SOURCE_XIAOHEIHE)


async def _hidden_fallback(player: Player, error: Exception) -> RecentMatch:
    """战绩被隐藏时的门控：累计到阈值才访问一次小黑盒，访问后计数归零。"""
    player.hidden_fail_count = int(player.hidden_fail_count or 0) + 1

    if not config.d2w_xiaoheihe_fallback_enabled:
        logger.warning(f"{error}（小黑盒回退已关闭）")
        return RecentMatch()

    threshold = _hidden_threshold()
    if player.hidden_fail_count < threshold:
        logger.info(f"{error}（连续第 {player.hidden_fail_count}/{threshold} 次，暂不回退小黑盒）")
        return RecentMatch()

    logger.info(f"{error}（连续第 {player.hidden_fail_count} 次，尝试回退小黑盒）")
    # 本次已访问过小黑盒（无论成败），计数归零，避免连续重试触发风控
    player.hidden_fail_count = 0
    try:
        return await _xiaoheihe_recent_match(player)
    except Exception as e:
        logger.warning(f"{player.nickname} 小黑盒战绩回退失败: {e}")
        return RecentMatch()


async def fetch_recent_match(player: Player, api_key: str | None = None) -> RecentMatch:
    """获取玩家最近一场比赛；战绩被隐藏时按门控回退小黑盒。

    返回空 RecentMatch 表示本轮没有可播报的比赛（隐藏次数未达阈值、
    小黑盒也没有记录、或小黑盒数据不比已知最新更新）。
    Steam 的网络错误等其它异常照旧抛出，由调用方统一记日志。
    """
    try:
        async with _history_semaphore:
            match_id = await request_match_history(player, api_key)
    except MatchHistoryHidden as e:
        return await _hidden_fallback(player, e)
    # Steam 正常返回，说明战绩不再隐藏，清零计数
    player.hidden_fail_count = 0
    return RecentMatch(match_id=match_id, source=SOURCE_STEAM)


__all__ = [
    "RecentMatch",
    "fetch_recent_match",
    "is_newer_match",
    "latest_known_match_id",
]
