"""插件配置。

- `Config`：NoneBot 用户可配置项，默认值在包内；用户配置优先从数据目录下的
  `config.json` 读取（首次运行自动生成），其次才是 `D2W_` 前缀的环境变量 / `.env`
  （例如 `D2W_STEAM_API_KEY`、`D2W_PROXIES`、`D2W_GH_PROXY`）。
  包内只保存默认值，用户无需（也不应）直接修改本文件，避免升级插件时配置被覆盖。
- 文件后半部分：运行期目录、数据源 URL 等常量；运行期数据/缓存目录由
  nonebot-plugin-localstore 提供（可用 `LOCALSTORE_DATA_DIR` / `LOCALSTORE_CACHE_DIR` 覆盖），
  GitHub 加速前缀则优先读取上面的 `D2W_GH_PROXY` 配置项。

本文件不强制依赖 NoneBot：在独立脚本中直接 `import config` 时，
会退化为使用默认配置，方便脱离框架单独测试生成器脚本。
"""

import json
import logging
from pathlib import Path

from pydantic import BaseModel


class Config(BaseModel):
    """NoneBot 用户可配置项（环境变量以 D2W_ 为前缀，如 D2W_TIMEOUT）。"""

    # ===================== API 密钥 =====================
    # Steam Web API Key（https://steamcommunity.com/dev/apikey 申请）
    # 用于拉取玩家比赛历史；留空时比赛播报不可用但其余功能正常
    d2w_steam_api_key: str = ""
    # TI 赛事/实时单局使用的 Steam Web API Key（可另申请一个分开用）
    # 留空时回退使用 d2w_steam_api_key
    d2w_ti_steam_api_key: str = ""
    # Stratz GraphQL API Token（/英雄池 使用，申请地址 https://stratz.com/api）
    # 可在 config.json 中设置，或使用环境变量 D2W_STRATZ_TOKEN
    d2w_stratz_token: str = ""

    # ===================== 网络 =====================
    # 代理，如 {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    d2w_proxies: dict[str, str] = {}
    # 网络请求超时（秒）
    d2w_timeout: int = 20
    # 下载超时（秒）
    d2w_download_timeout: int = 60
    # GitHub 加速前缀（国内访问 GitHub raw 资源时使用，可按需替换为其它代理）
    d2w_gh_proxy: str = "https://gh-proxy.com"

    # ===================== 播报与内容 =====================
    # 如何呼叫全体
    d2w_all_nickname: str = "全体"
    # 不播报的游戏模式（见 dota_dicts.GAME_MODE）
    d2w_game_mode: list[int] = [15, 19]
    # 评分标准（0~1），仅 openDota 支持
    d2w_benchmark_threshold: float = 0.5

    # ===================== 定时任务 =====================
    # 总开关：设为 false 时对应定时任务完全不注册、不轮询，零性能开销（彻底关闭）
    d2w_ti_enabled: bool = True
    d2w_news_enabled: bool = True
    # 各定时任务轮询间隔（秒）
    d2w_ti_poll_interval: int = 10
    d2w_news_poll_interval: int = 60
    d2w_match_poll_interval: int = 60
    # 拉取玩家比赛历史时的并发上限（Steam 接口存在速率限制，过大易触发 429/503）
    d2w_history_concurrency: int = 3

    # ===================== 缓存 =====================
    # 数据缓存时长（秒）
    d2w_cache_expire_seconds: int = 10800  # D2PT 位置数据缓存（3 小时）
    d2w_core_build_cache_seconds: int = 259200  # 核心出装数据缓存（72 小时）
    # 生成图片缓存时长（秒）：在缓存期内复用已生成的图片，避免重复渲染
    d2w_core_build_image_cache_seconds: int = 86400  # 生成图片缓存（24 小时 / 1 天）


try:
    # NoneBot 已初始化：读取环境变量 / .env 中的 D2W_ 配置
    from nonebot import get_plugin_config

    config = get_plugin_config(Config)
except Exception:
    # 独立脚本 / 无 NoneBot 环境：使用默认配置
    config = Config()

try:
    from nonebot.log import logger
except Exception:
    logger = logging.getLogger(__name__)

# ============================================================
# 数据目录下的 config.json 配置文件（优先于环境变量/.env），便于运行期直接编辑。
# 首次运行会自动生成默认配置；之后以该 JSON 为准。
# ============================================================
try:
    from nonebot_plugin_localstore import get_data_dir

    _CONFIG_FILE = get_data_dir("nonebot_plugin_dota2_watcher") / "config.json"
    if _CONFIG_FILE.exists():
        try:
            _loaded = json.loads(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
            # 结构校验：丢弃 Config 未定义的无效项
            _valid_keys = set(Config.model_fields)
            _cleaned = {k: v for k, v in _loaded.items() if k in _valid_keys}
            # 合并 config.json 中有效项，缺失项自动补齐默认值
            config = Config(**{**config.model_dump(), **_cleaned})
            # 存在无效项被删除 / 缺失项被补齐时，回写整理后的配置
            if _loaded != config.model_dump():
                _CONFIG_FILE.write_text(
                    json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"已整理配置文件（清除无效项 / 补齐缺失项）：{_CONFIG_FILE}")
        except Exception:
            # config.json 内容损坏：备份损坏文件并重新生成默认配置，避免启动失败
            logger.warning(f"配置文件损坏，已备份并重新生成默认配置：{_CONFIG_FILE}")
            _backup = _CONFIG_FILE.with_suffix(".json.bak")
            try:
                _CONFIG_FILE.replace(_backup)
                logger.warning(f"损坏配置已备份到：{_backup}")
            except Exception:
                pass  # 备份失败不影响后续重新生成
            _CONFIG_FILE.write_text(
                json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    else:
        # 首次运行：写入默认配置（含当前已生效的环境变量值），便于后续编辑
        _CONFIG_FILE.write_text(
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"已生成配置文件，路径：{_CONFIG_FILE}")
except Exception:
    # 独立脚本 / 无 NoneBot：忽略 JSON 配置，使用默认值
    pass

# ============================================================
# 运行期目录：优先使用 nonebot-plugin-localstore 提供的标准数据/缓存目录，
# 独立脚本（无 NoneBot）退化为当前工作目录，从而避免污染插件包。
# 目录位置可用 LOCALSTORE_DATA_DIR / LOCALSTORE_CACHE_DIR 覆盖。
# ============================================================
try:
    from nonebot_plugin_localstore import get_cache_dir, get_data_dir

    _cache_dir = get_cache_dir("nonebot_plugin_dota2_watcher")
    _data_dir = get_data_dir("nonebot_plugin_dota2_watcher")
    BASE_DIR = _cache_dir  # 工作/临时文件（如 npc_ability_ids.txt）
    DATA_DIR = _data_dir  # 持久数据（玩家订阅、D2PT/TI/英雄缓存等）
    IMAGES_DIR = _cache_dir / "images"  # 运行期下载的图片素材
    OUTPUT_DIR = _cache_dir / "output"  # 生成的战报图片
    MATCHES_DIR = _cache_dir / "matches"  # 比赛 JSON 缓存
except Exception:
    # 独立脚本 / 无 NoneBot 环境：使用默认配置，数据写入当前工作目录
    _DATA_ROOT = Path.cwd()
    BASE_DIR = _DATA_ROOT
    DATA_DIR = _DATA_ROOT / "data"
    IMAGES_DIR = _DATA_ROOT / "images"
    OUTPUT_DIR = _DATA_ROOT / "output"
    MATCHES_DIR = _DATA_ROOT / "matches"

# ============================================================
# 上游数据源
# ============================================================
# GitHub 加速前缀（优先读取 config.d2w_gh_proxy）
GH_PROXY = config.d2w_gh_proxy

# 上游数据仓库：https://github.com/Elmeir/d2pt_bot
# raw 文件根地址（refs/heads/main 分支）
D2PT_REPO_RAW = "https://raw.githubusercontent.com/Elmeir/d2pt_bot/refs/heads/main"
# 经 gh-proxy 加速后的仓库根 / 数据目录
D2PT_REPO_BASE = f"{GH_PROXY}/{D2PT_REPO_RAW}"
D2PT_DATA_BASE = f"{D2PT_REPO_BASE}/data"
# data/ 目录下的数据 JSON
D2PT_POS_URL = f"{D2PT_DATA_BASE}/d2pt_pos.json"  # D2PT 位置数据（已合并所有位置）
D2PT_CORE_BUILD_URL = f"{D2PT_DATA_BASE}/d2pt_core_build.json"  # 核心出装数据
D2PT_TALENTS_CN_URL = f"{D2PT_DATA_BASE}/talents_cn.json"  # 天赋中文名
# 仓库根 images/abilities/ 技能图标
D2PT_REPO_ICON_BASE = f"{D2PT_REPO_BASE}/images/abilities/"

# 第三方仓库（dotabuff/d2vpkr）技能 ID 列表源
NPC_ABILITY_IDS_URL = f"{GH_PROXY}/https://raw.githubusercontent.com/dotabuff/d2vpkr/master/dota/scripts/npc/npc_ability_ids.txt"

# OpenDota
OPENDOTA_BASE = "https://api.opendota.com"
OPENDOTA_MATCH_URL = f"{OPENDOTA_BASE}/api/matches/{{match_id}}"
OPENDOTA_REQUEST_URL = f"{OPENDOTA_BASE}/api/request/{{match_id}}"
OPENDOTA_LOGS_URL = f"{OPENDOTA_BASE}/logs/{{job_id}}"
OPENDOTA_HEROES_URL = f"{OPENDOTA_BASE}/api/constants/heroes"
OPENDOTA_ITEMS_URL = f"{OPENDOTA_BASE}/api/constants/items"

# Steam Web API
STEAM_API_BASE = "https://api.steampowered.com"
STEAM_MATCH_HISTORY_URL = f"{STEAM_API_BASE}/IDOTA2Match_570/GetMatchHistory/v001/?key={{key}}&account_id={{account_id}}&matches_requested=1"
STEAM_MATCH_DETAILS_URL = (
    f"{STEAM_API_BASE}/IDOTA2Match_570/GetMatchDetails/V001/?key={{key}}&match_id={{match_id}}"
)
STEAM_LIVE_GAMES_URL = f"{STEAM_API_BASE}/IDOTA2Match_570/GetLiveLeagueGames/v1?key={{key}}"
STEAM_NEWS_URL = "https://store.steampowered.com/events/ajaxgetpartnereventspageable/?clan_accountid=0&appid=570&offset=0&count=1&l=schinese"

# Valve DOTA2 官网 / CDN
DOTA2_API_URL = "https://www.dota2.com/webapi/IDOTA2League/GetLeagueData/v001?league_id={league_id}&delay_seconds=0"
DOTA2_HEROES_URL = "https://www.dota2.com/datafeed/herolist?language=schinese"
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react"
HERO_IMAGE_URL = f"{STEAM_CDN}/heroes/{{name}}.png"
ITEM_IMAGE_URL = f"{STEAM_CDN}/items/{{name}}.png"
ABILITY_IMAGE_URL = f"{STEAM_CDN}/abilities/{{name}}.png"
# 战报杂项素材（logo / 段位图标等），同样走 gh-proxy 加速
OTHER_IMAGE_URL = (
    f"{GH_PROXY}/https://raw.githubusercontent.com/SonodaHanami/Steam_watcher/web/images/{{}}.png"
)

# Liquipedia
LIQUIPEDIA_API_URL = "https://liquipedia.net/dota2/api.php?action=parse&page=The_International/2026/Group_Stage&format=json&prop=text&disablelimitreport=1"
LIQUIPEDIA_CDN = "https://liquipedia.net"

# TI 赛事
TI_LEAGUE_ID = 19719
TI_REFERER = "https://www.dota2.com/esports/ti15/schedule"
