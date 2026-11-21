"""定时任务：TI 赛果 / 官方新闻 / 战队名单 / 玩家新比赛。

每个作业只启动后台轮询并立即返回：上一轮未结束则跳过本轮；
单轮超过 _CYCLE_TIMEOUT 秒会被取消并告警。
"""

# require() 必须先执行，导入因此分两段：豁免 E402 与 I001
# ruff: noqa: E402, I001

import asyncio
from collections.abc import Awaitable, Callable

from nonebot import get_driver, require

require("nonebot_plugin_apscheduler")

from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from ..config import config
from ..services import news, service

# 单轮最长执行时间，超时取消本轮，避免任务永久卡住
_CYCLE_TIMEOUT = 900

# 正在执行的轮询：任务名 -> 后台任务
_running: dict[str, asyncio.Task] = {}


async def _run_cycle(name: str, poll: Callable[[], Awaitable[None]]) -> None:
    """执行一轮轮询：超时取消并告警，其余异常只记日志，不中断后续轮询。"""
    try:
        await asyncio.wait_for(poll(), timeout=_CYCLE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"定时任务 {name} 超过 {_CYCLE_TIMEOUT} 秒仍未结束，已取消本轮")
    except asyncio.CancelledError:
        # 插件重载 / 进程关闭时取消，正常向上传播
        raise
    except Exception:
        logger.exception(f"定时任务 {name} 执行失败")


def _dispatch(name: str, poll: Callable[[], Awaitable[None]]) -> None:
    """启动一轮轮询：已有一轮在跑则跳过本轮，否则交给后台任务，自身立即返回。"""
    current = _running.get(name)
    if current is not None and not current.done():
        logger.debug(f"定时任务 {name} 上一轮尚未结束，跳过本轮")
        return
    _running[name] = asyncio.create_task(_run_cycle(name, poll))


@get_driver().on_shutdown
async def _cancel_running() -> None:
    """关闭时取消仍在执行的一轮，避免留下悬挂任务拖住退出。"""
    pending = [task for task in _running.values() if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _running.clear()


# ---------------------------------------------------------------
# TI 赛事结果监听
# ---------------------------------------------------------------
if config.d2w_ti_enabled:

    @scheduler.scheduled_job(
        "interval", seconds=config.d2w_ti_poll_interval, coalesce=True, max_instances=1
    )
    async def watch_ti_results() -> None:
        """拉取最新 TI 赛果并广播（定时任务）。"""
        _dispatch("watch_ti_results", service.poll_ti_results)


# ---------------------------------------------------------------
# DOTA2 官方新闻监听
# ---------------------------------------------------------------
if config.d2w_news_enabled:

    @scheduler.scheduled_job(
        "interval", seconds=config.d2w_news_poll_interval, coalesce=True, max_instances=1
    )
    async def watch_news() -> None:
        """监听 DOTA2 官方新闻，出现新头条时广播。"""
        _dispatch("watch_news", news.poll_news)


# ---------------------------------------------------------------
# 战队名单变动监听（成员加入 / 离开）
# ---------------------------------------------------------------
if config.d2w_roster_enabled:

    @scheduler.scheduled_job(
        "interval", seconds=config.d2w_roster_poll_interval, coalesce=True, max_instances=1
    )
    async def watch_roster_changes() -> None:
        """轮询已订阅战队的登记名单，成员变动时按群播报。"""
        _dispatch("watch_roster_changes", service.poll_roster_changes)


# ---------------------------------------------------------------
# 订阅玩家新比赛播报
# ---------------------------------------------------------------
@scheduler.scheduled_job(
    "interval", seconds=config.d2w_match_poll_interval, coalesce=True, max_instances=1
)
async def watch_new_matches() -> None:
    """轮询订阅玩家的新比赛并生成战报播报。"""
    _dispatch("watch_new_matches", service.poll_new_matches)
