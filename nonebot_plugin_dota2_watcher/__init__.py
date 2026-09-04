"""DOTA2 观察者 NoneBot2 插件。

功能：开黑战报、订阅玩家比赛播报、TI 赛事监听、DOTA2 新闻监听、
D2PT 位置数据、核心出装图片、TI 战报图片。

配置（环境变量 / NoneBot 配置项，均以 D2W_ 前缀）：
    D2W_STEAM_API_KEY   Steam Web API Key，用于拉取玩家比赛历史
    D2W_PROXIES         代理，如 {"http": "...", "https": "..."}
    D2W_TIMEOUT         网络超时（秒），默认 20
    D2W_GAME_MODE       不播报的游戏模式，默认 [15, 19]
"""

from nonebot import get_driver, require
from nonebot.log import logger
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")

# 下方 require() 必须先于插件模块导入执行，故豁免 E402。
# ruff: noqa: E402

# 以“模块”形式引入 config，避免包级名称 config 被实例覆盖
# （否则其它模块 `from . import config as _cfg` 会拿到 Config 实例而非模块）
from . import config as _plugin_config
from .config import Config
from .handlers import commands, scheduler  # noqa: F401

__plugin_meta__ = PluginMetadata(
    name="DOTA2 观察者",
    description="DOTA2 观察者 NoneBot2 插件：DOTA2 开黑战报 / 新闻推送 / TI 赛事 / D2PT 出装",
    usage=(
        "/添加刀塔玩家 [昵称] [steam的id]：订阅玩家，新比赛自动播报\n"
        "/查看刀塔玩家：列出本群订阅玩家\n"
        "/删除刀塔玩家 [昵称]：删除指定玩家\n"
        "开启/关闭[昵称]的群播报（昵称为“全体”时一次控制全部）\n"
        "/d2pt [位置1-5]：D2PT 胜率/线优数据\n"
        "/战报 [比赛编号]：生成开黑战报图片\n"
        "/出装 [英雄名] [位置1-5] [dark|light]：核心出装图\n"
        "/ti：TI 赛事战报图片\n"
        "/英雄池 [steam_id 或 昵称] [min|mid|max 或 小|中|大]：英雄池环形图（默认 min/25 场）\n"
        "/pro [steam_id 或 昵称]：查询与职业选手的对战记录\n"
        "/订阅 新闻|ti：切换新闻/TI 订阅开关\n"
        "/help：查看本插件指令列表"
    ),
    type="application",
    homepage="https://github.com/Elmeir/dota2-watcher-nonebot",
    config=Config,
    supported_adapters={"~onebot.v11"},
)


@get_driver().on_startup
async def _check_config() -> None:
    if not _plugin_config.config.d2w_steam_api_key:
        config_file = _plugin_config.DATA_DIR / "config.json"
        logger.warning(
            f"未配置 D2W_STEAM_API_KEY，订阅玩家比赛播报将不可用。"
            f"请在 {config_file} 中设置，或设置环境变量 D2W_STEAM_API_KEY"
            "（申请地址: https://steamcommunity.com/dev/apikey）"
        )
