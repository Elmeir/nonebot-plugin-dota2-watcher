"""开黑战报：一句话锐评生成 + 战报图片调度。

文字战报只保留一句阴阳怪气（锐评），详细数据一律由战报图片承载，
因此这里不再拼接开始时间 / KDA / 补刀等明细文本。
锐评的多维度判定与语句库见 generators/roast.py 与 dota_dicts.ROAST_LINES。
"""

import asyncio

from nonebot.log import logger

from ..config import config
from . import match_report, roast


def _collect_players(match_info: dict, player_list: list) -> list:
    """按 steam_id 匹配并加载每个订阅玩家的对局数据。"""
    collected = []
    for player in player_list:
        for info in match_info.get("players", []):
            if player.short_steamID == info.get("account_id", 0):
                player.load_player_info(info)
                collected.append(player)
                break
        else:
            logger.warning(f"{player.nickname}的数据无法获取，可能已被屏蔽")
    return collected


def generate_message(
    match_info: dict,
    player_list: list,
    streaks: dict[int, tuple[int, int]] | None = None,
) -> str | None:
    """生成一句话锐评（每位订阅玩家各一句，多行返回）。

    返回 None 表示该比赛模式不需要播报（如自定义/活动模式）。
    streaks 为 {steam_id: (连胜, 连败)}，缺省时不参与判定。
    """
    if match_info.get("game_mode") in config.d2w_game_mode:
        return None

    player_list = _collect_players(match_info, player_list)
    if not player_list:
        return None

    return roast.roast_players(player_list, match_info, streaks)


_report_lock = asyncio.Lock()


async def generate_report_img(
    match_id, force: bool = False, match_data=None, supplement_anonymous: bool = True
):
    """生成战报图片并返回本地路径；失败返回 None/False。

    使用全局锁串行化图片生成，避免多个协程并发操作共享的 httpx 会话。
    match_data 为调用方已拉取的比赛详情，传入可避免图片生成时重复请求数据源；
    supplement_anonymous 为 False 时不接入小黑盒补充匿名玩家数据。
    """
    async with _report_lock:
        try:
            return await match_report.generate_match_image(
                match_id,
                force=force,
                match_data=match_data,
                supplement_anonymous=supplement_anonymous,
            )
        finally:
            await match_report.close_session()
