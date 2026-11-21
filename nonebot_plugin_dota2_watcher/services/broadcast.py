"""群播报：按群过滤与订阅开关向 OneBot 群发送消息。"""

import asyncio

from nonebot import get_bots
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message
from nonebot.log import logger

from ..config import is_group_allowed
from . import store

# 单次发送超时兜底，避免协议端无响应卡住整轮轮询
_SEND_TIMEOUT = 60


async def _send(bot: Bot, gid: str, msg: Message) -> None:
    await asyncio.wait_for(bot.send_group_msg(group_id=int(gid), message=msg), _SEND_TIMEOUT)


async def _send_to_gid(bots: dict, gid: str, msg: Message, log_prefix: str) -> None:
    """对每个已连接 bot 发送一条消息；单个 bot 失败不影响其它 bot。"""
    for bot in bots.values():
        try:
            await _send(bot, gid, msg)
        except Exception:
            logger.exception(f"{log_prefix}到群 {gid} 失败")


async def send_group_message(gid: str, msg: Message, *, log_prefix: str = "广播消息") -> None:
    """向指定群发送一条消息（超时即放弃，后端不响应时不无限等待）。"""
    bots = get_bots()
    if bots:
        await _send_to_gid(bots, gid, msg, log_prefix)


async def _broadcast_to(bots: dict, text: str, gids, log_prefix: str) -> None:
    msg = Message(f"[DOTA2]{text}")
    for gid in gids:
        if is_group_allowed(gid):
            await _send_to_gid(bots, gid, msg, log_prefix)


async def broadcast_text(text: str | None, filter_key: str | None = None) -> None:
    """向所有允许的群广播 [DOTA2] 前缀文本。

    filter_key 为 "subscribe_news"/"subscribe_ti" 时按群订阅开关过滤。
    """
    bots = get_bots()
    if not text or not bots:
        return
    groups = store.get_all_groups()
    gids = [gid for gid, info in groups.items() if not filter_key or info.get(filter_key, True)]
    await _broadcast_to(bots, text, gids, "广播消息")


async def broadcast_text_to_groups(text: str | None, gids) -> None:
    """向指定的若干群广播（用于按战队订阅关系定向播报）。"""
    bots = get_bots()
    if not text or not bots:
        return
    await _broadcast_to(bots, text, gids, "广播战队名单变动")
