# DOTA2 Watcher

基于 [NoneBot2](https://nonebot.dev/) 的 DOTA2 QQ 群插件：开黑战报、比赛播报、新闻 / TI / 战队名单订阅，以及 D2PT 出装与玩家数据图片。

## 功能

- **开黑战报**：按比赛编号生成战报图片，并给每位订阅玩家一句锐评。
- **比赛播报**：轮询订阅玩家的新比赛，推送战报图片；战绩被隐藏时可按阈值回退小黑盒。
- **新闻 / TI / 战队名单**：定时检查官方新闻、TI 赛果与战队登记名单，有变化时播报。
- **D2PT 出装**：`/d2pt` 生成五位置总览或单位置排行，`/出装` 生成指定英雄核心出装。
- **加速模式**：`/加速` 生成加速模式英雄胜率图（Dotabuff 数据）。
- **玩家数据**：`/英雄池`、`/pro`、`/开黑`、`/战绩` 生成对应图片。
- **订阅管理**：按群 / 玩家开关播报，支持白名单 / 黑名单。

## 安装

需要 NoneBot2 + OneBot v11（Python >= 3.10）。

```bash
pip install nonebot2 nonebot-adapter-onebot
pip install httpx Pillow fonttools playwright
nb plugin install nonebot-plugin-apscheduler
nb plugin install nonebot-plugin-localstore
python -m playwright install chromium
```

将 `nonebot_plugin_dota2_watcher` 放入 NoneBot2 项目的 plugins 目录，或在 `bot.py` 中加载：

```python
nonebot.load_plugin("nonebot_plugin_dota2_watcher")
```

## 配置

默认值见 [`nonebot_plugin_dota2_watcher/config.py`](nonebot_plugin_dota2_watcher/config.py)。优先级：数据目录下的 `config.json`（首次运行自动生成）> `.env` / 环境变量 > 默认值。

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `D2W_STEAM_API_KEY` | Steam Web API Key，用于比赛历史（[申请](https://steamcommunity.com/dev/apikey)） | 空 |
| `D2W_TI_STEAM_API_KEY` | TI 赛事专用 Key，留空复用上方 | 空 |
| `D2W_STRATZ_TOKEN` | Stratz Token，`/英雄池` 等使用（[申请](https://stratz.com/api)） | 空 |
| `D2W_PROXIES` | 网络代理，如 `{"http": "http://127.0.0.1:7890"}` | `{}` |
| `D2W_TIMEOUT` | 请求超时（秒） | `20` |
| `D2W_DOWNLOAD_TIMEOUT` | 下载超时（秒） | `60` |
| `D2W_GH_PROXY` | GitHub 加速前缀 | `https://gh-proxy.com` |
| `D2W_GROUP_MODE` | 群组模式：`all` / `whitelist` / `blacklist` | `all` |
| `D2W_GROUP_WHITELIST` | 白名单群号列表 | `[]` |
| `D2W_GROUP_BLACKLIST` | 黑名单群号列表 | `[]` |
| `D2W_ALL_NICKNAME` | “全体”播报的昵称关键字 | `全体` |
| `D2W_GAME_MODE` | 不播报的游戏模式 | `[15, 19]` |
| `D2W_BENCHMARK_THRESHOLD` | 锐评正负倾向阈值（0~1） | `0.5` |
| `D2W_IMAGE_THEME` | 图片默认风格：`light` / `dark` | `light` |
| `D2W_TI_ENABLED` | TI 定时任务开关 | `false` |
| `D2W_NEWS_ENABLED` | 新闻定时任务开关 | `true` |
| `D2W_ROSTER_ENABLED` | 战队名单定时任务开关 | `true` |
| `D2W_TI_POLL_INTERVAL` | TI 轮询间隔（秒） | `10` |
| `D2W_NEWS_POLL_INTERVAL` | 新闻轮询间隔（秒） | `60` |
| `D2W_MATCH_POLL_INTERVAL` | 比赛轮询间隔（秒） | `60` |
| `D2W_ROSTER_POLL_INTERVAL` | 战队名单轮询间隔（秒） | `21600` |
| `D2W_HISTORY_CONCURRENCY` | 比赛历史拉取并发上限 | `3` |
| `D2W_XIAOHEIHE_FALLBACK_ENABLED` | 战绩被隐藏时是否回退小黑盒 | `true` |
| `D2W_HIDDEN_FALLBACK_THRESHOLD` | 连续隐藏多少次后回退小黑盒 | `10` |
| `D2W_CACHE_EXPIRE_SECONDS` | D2PT / 玩家数据缓存（秒） | `10800` |
| `D2W_CORE_BUILD_CACHE_SECONDS` | 核心出装数据缓存（秒） | `86400` |
| `D2W_CORE_BUILD_IMAGE_CACHE_SECONDS` | 核心出装图片缓存（秒） | `43200` |
| `D2W_XIAOHEIHE_CACHE_SECONDS` | `/战绩` 数据与图片缓存（秒） | `3600` |

示例 `.env`：

```dotenv
D2W_STEAM_API_KEY=你的Steam_Web_API_Key
D2W_PROXIES={"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
D2W_GROUP_MODE=all
D2W_IMAGE_THEME=light
LOCALSTORE_DATA_DIR=./data
LOCALSTORE_CACHE_DIR=./cache
```

运行期数据与缓存由 [nonebot-plugin-localstore](https://github.com/nonebot/plugin-localstore) 管理；`LOCALSTORE_DATA_DIR` / `LOCALSTORE_CACHE_DIR` 可覆盖目录。

## 命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/添加刀塔玩家 [昵称] [steam_id]` | 订阅玩家，新比赛自动播报 | 任意 |
| `/查看刀塔玩家` | 查看本群订阅玩家 | 任意 |
| `/删除刀塔玩家 [昵称]` | 删除订阅 | 管理员 |
| `开启/关闭[昵称]的群播报` | 开关玩家播报（“全体”= 全部） | 任意 |
| `/d2pt [位置1-5] [dark\|light]` | 排行图：不带位置为总览，带位置为该位置全部英雄 | 任意 |
| `/加速 [dark\|light]` | 加速模式英雄胜率图（仅 50% 以上，按胜率降序） | 任意 |
| `/战报 [比赛编号]` | 开黑战报图片 | 任意 |
| `/出装 [英雄名] [位置1-5] [dark\|light]` | 核心出装图片 | 任意 |
| `/ti [小组赛\|正赛]` | TI 战报图片（默认最新阶段） | 任意 |
| `/英雄池 [steam_id 或昵称] [min\|mid\|max]` | 英雄池环形图（默认 `min`，25 场） | 任意 |
| `/pro [steam_id 或昵称] [dark\|light]` | 与职业选手的对战记录图（前 10） | 任意 |
| `/开黑 [steam_id 或昵称] [dark\|light]` | 最常一起开黑的队友图（前 20） | 任意 |
| `/战绩 [steam_id 或昵称] [dark\|light]` | 小黑盒最近战绩图 | 任意 |
| `/添加刀塔战队 [队名或 team_id 或 CN]` | 订阅名单变动，`CN` 为全部中国战队 | 任意 |
| `/查看刀塔战队` | 查看本群订阅战队 | 任意 |
| `/删除刀塔战队 [队名或 team_id 或 CN]` | 取消订阅 | 管理员 |
| `/战队 [队名或 team_id 或 CN]` | 查询战队当前登记名单 | 任意 |
| `/订阅 [新闻\|ti] [开\|关]` | 查看或设置订阅 | 管理员 |

补充：

- 图片风格由 `D2W_IMAGE_THEME` 统一控制，作用于 `/d2pt`、`/加速`、`/出装`、`/英雄池`、`/pro`、`/开黑`、`/战绩`；其中 `/d2pt`、`/加速`、`/出装`、`/pro`、`/开黑`、`/战绩` 可用命令末尾的 `dark|light` 临时覆盖。
- 定时任务不叠加：上一轮未结束则跳过。单轮超过 15 分钟取消并告警，单次发送最多等待 60 秒。
- 战绩被隐藏（Steam `status 15`）的玩家累计达到阈值后，改从小黑盒取最近比赛，并用小黑盒详情生成战报；比赛 ID 必须大于已知最新值才播报。
- 战队名单来自 Valve 客户端登记值，含替补 / 教练，可能存在跨队残留；首次订阅只建立基线，不补播历史。

## 目录结构

```text
nonebot_plugin_dota2_watcher/
├── __init__.py              # 插件入口与元数据
├── config.py                # 配置与运行期目录
├── utils.py                 # HTTP / 缓存 / 并发工具
├── assets.py                # 图片素材管理
├── dota_dicts.py            # 静态字典与锐评语句
├── hero_nicknames.py        # 英雄昵称
├── handlers/
│   ├── commands.py          # 命令处理器
│   └── scheduler.py         # 定时任务
├── services/
│   ├── service.py           # 业务编排
│   ├── broadcast.py         # 群播报
│   ├── news.py              # 新闻监听与去重
│   ├── match_history.py     # 最近比赛获取（含小黑盒回退）
│   ├── store.py             # 订阅数据存储
│   └── player.py            # 玩家数据模型
├── datasources/
│   ├── stratz.py            # Stratz GraphQL 传输层
│   ├── stratz_peers.py      # peers 查询参数与解析
│   ├── cache.py             # 版本化 JSON 缓存
│   ├── request_match.py     # Steam / OpenDota 比赛详情
│   ├── hero_pool.py         # 英雄池
│   ├── playmates.py         # 开黑记录
│   ├── pro_peers.py         # 职业选手对战记录
│   ├── pro_names.py         # 职业选手表
│   ├── d2pt.py              # D2PT / 加速模式数据
│   ├── xiaoheihe.py         # 小黑盒比赛详情
│   ├── xiaoheihe_overview.py # 小黑盒战绩总览
│   ├── team_roster.py       # 战队登记名单
│   └── ti_results.py        # TI 赛果
└── generators/
    ├── core_build.py        # 核心出装图
    ├── d2pt_ranking.py      # D2PT 排行图
    ├── turbo_ranking.py     # 加速模式胜率图
    ├── match_builder.py     # 战报文本与图片调度
    ├── match_report.py      # 战报图片
    ├── roast.py             # 锐评判定
    ├── hero_pool.py         # 英雄池环形图
    ├── playmates.py         # 开黑记录图
    ├── pro_peers.py         # 职业选手对战记录图
    ├── xiaoheihe_overview.py # 战绩图
    ├── html_render.py       # HTML → PNG 渲染
    └── shared_browser.py    # 共享 Playwright 浏览器
```

图片素材统一由 `assets.py` 管理，目录为 `images/` 下的 `hero/`、`item/`、`abilities/`、`icons/`、`avatars/`、`logo/`、`rank/`、`other/`。

## 致谢

- [NoneBot2](https://nonebot.dev/)
- [OpenDota](https://www.opendota.com/) / Steam Web API
- [d2pt_bot](https://github.com/Elmeir/d2pt_bot) 的 D2PT 数据
- [Steam_watcher](https://github.com/SonodaHanami/Steam_watcher) 的战报素材
