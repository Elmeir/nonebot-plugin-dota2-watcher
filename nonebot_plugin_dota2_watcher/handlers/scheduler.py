"""定时任务：TI 赛事结果 / DOTA2 新闻 / 订阅玩家新比赛播报。

依赖 nonebot-plugin-apscheduler，请确保已安装。
"""

# 下方 require() 必须先于 nonebot_plugin_apscheduler 的导入执行，故豁免 E402。
# ruff: noqa: E402

from nonebot import require

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

from ..config import config
from ..services import service


# ---------------------------------------------------------------
# TI 赛事结果监听
# ---------------------------------------------------------------
if config.d2w_ti_enabled:

    @scheduler.scheduled_job(
        "interval", seconds=config.d2w_ti_poll_interval, coalesce=True, max_instances=1
    )
    async def watch_ti_results() -> None:
        """拉取最新 TI 赛果并广播（定时任务）。"""
        await service.poll_ti_results()


# ---------------------------------------------------------------
# DOTA2 官方新闻监听
# ---------------------------------------------------------------
if config.d2w_news_enabled:

    @scheduler.scheduled_job(
        "interval", seconds=config.d2w_news_poll_interval, coalesce=True, max_instances=1
    )
    async def watch_news() -> None:
        """监听 DOTA2 官方新闻，出现新头条时广播。"""
        await service.poll_news()


# ---------------------------------------------------------------
# 战队名单变动监听（成员加入 / 离开）
# ---------------------------------------------------------------
if config.d2w_roster_enabled:

    @scheduler.scheduled_job(
        "interval", seconds=config.d2w_roster_poll_interval, coalesce=True, max_instances=1
    )
    async def watch_roster_changes() -> None:
        """轮询已订阅战队的登记名单，成员变动时按群播报。"""
        await service.poll_roster_changes()


# ---------------------------------------------------------------
# 订阅玩家新比赛播报
# ---------------------------------------------------------------
@scheduler.scheduled_job(
    "interval", seconds=config.d2w_match_poll_interval, coalesce=True, max_instances=1
)
async def watch_new_matches() -> None:
    """轮询订阅玩家的新比赛并生成战报播报。"""
    await service.poll_new_matches()
