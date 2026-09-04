"""命令处理器：玩家订阅 / 播报开关 / d2pt / 战报 / 出装 / ti。

命令层只负责解析输入、调用 services 中的业务函数并返回结果，
业务逻辑统一放在 services/service.py 中。
"""

from nonebot import on_command, on_regex
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER

from ..services import service

# ---------------------------------------------------------------
# 命令注册
# ---------------------------------------------------------------
add_player_cmd = on_command("添加刀塔玩家", priority=10, block=True)
list_players_cmd = on_command("查看刀塔玩家", priority=10, block=True)
delete_player_cmd = on_command(
    "删除刀塔玩家", priority=10, block=True, permission=GROUP_ADMIN | GROUP_OWNER | SUPERUSER
)
close_broadcast_cmd = on_regex(r"关闭(\S+)的群播报", priority=10, block=True)
open_broadcast_cmd = on_regex(r"开启(\S+)的群播报", priority=10, block=True)
d2pt_cmd = on_command("d2pt", aliases={"D2PT"}, priority=10, block=True)
report_cmd = on_command("战报", priority=10, block=True)
build_cmd = on_command("出装", priority=10, block=True)
ti_cmd = on_command("ti", aliases={"TI"}, priority=10, block=True)
hero_pool_cmd = on_command("英雄池", priority=10, block=True)
pro_cmd = on_command("pro", aliases={"PRO"}, priority=10, block=True)
subscribe_cmd = on_command(
    "订阅", priority=10, block=True, permission=GROUP_ADMIN | GROUP_OWNER | SUPERUSER
)
help_cmd = on_command("help", aliases={"帮助"}, priority=10, block=True)


def _args(event: GroupMessageEvent) -> list[str]:
    """取命令名之后以空格分隔的参数列表。"""
    return event.get_plaintext().strip().split()[1:]


# ---------------------------------------------------------------
# /添加刀塔玩家：订阅玩家，新比赛自动播报
# ---------------------------------------------------------------
@add_player_cmd.handle()
async def handle_add_player(event: GroupMessageEvent):
    parts = event.get_plaintext().strip().split()
    if len(parts) != 3:
        await add_player_cmd.finish(
            "请输入：/添加刀塔玩家 [玩家昵称] [steam的id]\n如：/添加刀塔玩家 萧瑟先辈 898754153"
        )
    reply = await service.add_player(event.group_id, parts[1], parts[2])
    await add_player_cmd.finish(reply)


# ---------------------------------------------------------------
# /查看刀塔玩家：列出本群订阅玩家
# ---------------------------------------------------------------
@list_players_cmd.handle()
async def handle_list_players(event: GroupMessageEvent):
    await list_players_cmd.finish(service.list_players(event.group_id))


# ---------------------------------------------------------------
# /删除刀塔玩家：删除指定玩家（管理员以上）
# ---------------------------------------------------------------
@delete_player_cmd.handle()
async def handle_delete_player(event: GroupMessageEvent):
    args = _args(event)
    if len(args) != 1:
        await delete_player_cmd.finish("请输入：/删除刀塔玩家 [玩家昵称]")
    await delete_player_cmd.finish(service.delete_player(event.group_id, args[0]))


# ---------------------------------------------------------------
# 播报开关
# ---------------------------------------------------------------
@close_broadcast_cmd.handle()
async def handle_close_broadcast(matcher: Matcher, event: GroupMessageEvent):
    name = matcher.state["_matched_groups"][0]
    if reply := service.toggle_broadcast(event.group_id, name, display=False):
        await close_broadcast_cmd.finish(reply)


@open_broadcast_cmd.handle()
async def handle_open_broadcast(matcher: Matcher, event: GroupMessageEvent):
    name = matcher.state["_matched_groups"][0]
    if reply := service.toggle_broadcast(event.group_id, name, display=True):
        await open_broadcast_cmd.finish(reply)


# ---------------------------------------------------------------
# /d2pt
# ---------------------------------------------------------------
@d2pt_cmd.handle()
async def handle_d2pt(event: GroupMessageEvent):
    args = _args(event)
    if len(args) > 1:
        await d2pt_cmd.finish("请输入：/d2pt 或 /d2pt [位置(数字)]")
    pos = args[0] if args else "all"
    if pos != "all" and pos not in "12345":
        await d2pt_cmd.finish("请输入：/d2pt 或 /d2pt [位置(数字)]")
    await d2pt_cmd.finish(await service.d2pt_report(pos))


# ---------------------------------------------------------------
# /战报
# ---------------------------------------------------------------
@report_cmd.handle()
async def handle_report(event: GroupMessageEvent):
    args = _args(event)
    if len(args) != 1 or not args[0].isdigit():
        await report_cmd.finish("请输入：/战报 [比赛编号]")
    if path := await service.report_image(args[0]):
        await report_cmd.finish(MessageSegment.image(file=path))
    await report_cmd.finish("战报生成失败")


# ---------------------------------------------------------------
# /出装
# ---------------------------------------------------------------
@build_cmd.handle()
async def handle_build(event: GroupMessageEvent):
    args = _args(event)
    if not args:
        await build_cmd.finish("请输入：/出装 [英雄名] [位置(数字)] [dark|light]")
    hero, position, theme = args[0], None, "light"
    if len(args) >= 2 and args[1] in "12345":
        position = args[1]
        if len(args) >= 3:
            theme = "dark" if args[2] == "dark" else "light"
    elif len(args) >= 2:
        await build_cmd.finish("位置参数无效，请输入 1-5")
    if path := await service.build_image(hero, position, theme):
        await build_cmd.finish(MessageSegment.image(file=path))
    await build_cmd.finish("没有找到该数据")


# ---------------------------------------------------------------
# /ti
# ---------------------------------------------------------------
# 阶段关键字 -> 生成器阶段名；空参数表示自动判断当前（最新）阶段
_TI_STAGE_ALIASES = {
    "小组赛": "swiss",
    "swiss": "swiss",
    "晋级赛": "elimination_round",
    "加赛": "elimination_round",
    "elimination": "elimination_round",
    "正赛": "main_event",
    "淘汰赛": "main_event",
    "main": "main_event",
    "playoff": "main_event",
}


@ti_cmd.handle()
async def handle_ti(event: GroupMessageEvent):
    args = _args(event)
    if len(args) > 1:
        await ti_cmd.finish("参数过多，请输入：/ti 或 /ti 小组赛|正赛")
    stage = "auto"
    if args:
        stage = _TI_STAGE_ALIASES.get(args[0].strip().lower(), "")
        if not stage:
            await ti_cmd.finish("阶段参数无效，请输入：/ti 或 /ti 小组赛|正赛")
    if path := await service.ti_image(stage):
        await ti_cmd.finish(MessageSegment.image(file=path))
    await ti_cmd.finish("查询失败，官网炸了")


# ---------------------------------------------------------------
# /英雄池
# ---------------------------------------------------------------
@hero_pool_cmd.handle()
async def handle_hero_pool(event: GroupMessageEvent):
    args = _args(event)
    if not (1 <= len(args) <= 2):
        await hero_pool_cmd.finish(
            "请输入：/英雄池 [steam_id 或 玩家昵称] [min|mid|max 或 小|中|大（可选，默认 min）]"
        )
    size = args[1] if len(args) > 1 else ""
    try:
        path = await service.hero_pool_image(event.group_id, args[0], size)
    except ValueError as e:
        await hero_pool_cmd.finish(str(e))
    if path:
        await hero_pool_cmd.finish(MessageSegment.image(file=path))
    await hero_pool_cmd.finish("英雄池生成失败")


# ---------------------------------------------------------------
# /pro：查询与职业选手的对战记录
# ---------------------------------------------------------------
@pro_cmd.handle()
async def handle_pro(event: GroupMessageEvent):
    args = _args(event)
    if len(args) != 1:
        await pro_cmd.finish("请输入：/pro [steam_id 或 玩家昵称]")
    try:
        text = await service.pro_report(event.group_id, args[0])
    except ValueError as e:
        await pro_cmd.finish(str(e))
    if text:
        await pro_cmd.finish(text)
    await pro_cmd.finish("职业选手对战记录查询失败")


# ---------------------------------------------------------------
# /订阅：查看订阅状态 / 切换或指定开、关新闻、TI 订阅（管理员以上）
# ---------------------------------------------------------------
_SUBSCRIBE_HINT = "请输入：/订阅、/订阅 新闻|ti、/订阅 新闻|ti 开|关"
_SUBSCRIBE_KEYWORDS = {"新闻": "news", "news": "news", "ti": "ti", "赛事": "ti"}
_ON_WORDS = {"开", "on", "开启", "1"}
_OFF_WORDS = {"关", "off", "关闭", "0"}


@subscribe_cmd.handle()
async def handle_subscribe(event: GroupMessageEvent):
    args = _args(event)
    if not args:
        # 无参数：仅查看全局总开关与本群订阅开关状态
        await subscribe_cmd.finish(service.subscription_status(event.group_id))
    if len(args) > 2:
        await subscribe_cmd.finish(_SUBSCRIBE_HINT)
    key = _SUBSCRIBE_KEYWORDS.get(args[0].strip().lower())
    if key is None:
        await subscribe_cmd.finish(_SUBSCRIBE_HINT)

    # 可选的开关参数：省略时默认切换
    if len(args) > 1:
        raw = args[1].strip().lower()
        if raw in _ON_WORDS:
            await subscribe_cmd.finish(service.set_subscription(event.group_id, key, True))
        if raw in _OFF_WORDS:
            await subscribe_cmd.finish(service.set_subscription(event.group_id, key, False))
        await subscribe_cmd.finish("开关参数无效，请输入 开 或 关")

    await subscribe_cmd.finish(service.toggle_subscription(event.group_id, key))


# ---------------------------------------------------------------
# /help
# ---------------------------------------------------------------
_HELP_TEXT = (
    "DOTA2 观察者插件 指令列表：\n"
    "/添加刀塔玩家 [昵称] [steam的id]：订阅玩家，新比赛自动播报\n"
    "/查看刀塔玩家：列出本群订阅玩家\n"
    "/删除刀塔玩家 [昵称]：删除指定玩家（管理员以上）\n"
    "开启/关闭[昵称]的群播报：控制某玩家（或“全体”）播报\n"
    "/d2pt [位置1-5]：D2PT 各位置胜率/线优数据\n"
    "/战报 [比赛编号]：生成开黑战报图片\n"
    "/出装 [英雄名] [位置1-5] [dark|light]：核心出装图\n"
    "/ti [小组赛|正赛]：TI 赛事战报图片（默认最新阶段）\n"
    "/英雄池 [steam_id 或 玩家昵称]：生成英雄池环形图\n"
    "/pro [steam_id 或 玩家昵称]：与职业选手的对战记录\n"
    "/订阅：查看订阅状态（总开关与本群开关）\n"
    "/订阅 新闻|ti [开|关]：切换或指定开、关订阅（管理员以上）"
)


@help_cmd.handle()
async def handle_help():
    await help_cmd.finish(_HELP_TEXT)
