"""定时任务：TI 赛事结果 / DOTA2 新闻 / 订阅玩家新比赛播报。

依赖 nonebot-plugin-apscheduler，请确保已安装。

每个作业只负责「确保某一轮轮询在后台跑起来」，自身立即返回，因此：
- 上一轮尚未结束时本轮直接跳过（debug 记录），不会再出现 APScheduler 的
  "Execution of job ... skipped: maximum number of running instances reached" 警告；
- 单轮超过 _CYCLE_TIMEOUT 秒会被取消并告警，避免某次请求一直等不到结果（数据源无响应、
  协议端不回包等）后该任务从此每轮都被跳过、播报彻底停摆。
"""

# 下方 require() 必须先于 nonebot_plugin_apscheduler 的导入执行，导入语句因此被
# require() 分隔成两段：豁免 E402（导入不在文件顶部）与 I001（导入块未排序）。
# ruff: noqa: E402, I001

import asyncio
from collections.abc import Awaitable, Callable

from nonebot import get_driver, require

require("nonebot_plugin_apscheduler")

from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from ..config import config
from ..services import service

# 单轮轮询的最长执行时间（秒）：正常一轮只有几秒到一分钟，该值仅用于兜底「卡死」，
# 超时即取消本轮，让下一轮重新开始，而不是让任务永久停在未完成状态。
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
        _dispatch("watch_news", service.poll_news)


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
