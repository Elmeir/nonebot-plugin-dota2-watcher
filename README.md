# DOTA2 Watcher

基于 [NoneBot2](https://nonebot.dev/) 的 DOTA2 观察者插件，为 QQ 群提供开黑战报、玩家比赛播报、TI 赛事监听、DOTA2 新闻推送与 D2PT 出装等能力。

## 功能特性

- **开黑战报**：输入比赛编号，生成包含对局详情与 MVP 评分的战报图片（基于 OpenDota）。
- **玩家比赛播报**：添加 / 查看 / 删除订阅玩家，自动轮询其最新比赛并生成战报推送。
- **新闻推送**：DOTA2 官方新闻出现新头条时自动向群广播。
- **TI 赛事**：定时拉取 TI 赛果并推送，支持 `/ti` 查看实时战报图片。
- **D2PT 出装**：查询 D2PT 各位置胜率 / 线优数据，以及指定英雄的核心出装图片（支持明暗主题）。
- **英雄池**：通过 Stratz 数据生成玩家最近比赛的英雄池环形图（含出场占比与位置占比内环）。
- **播报开关**：按群 / 按玩家开启或关闭播报（昵称填「全体」可一次控制全部）。

## 安装

本插件基于 NoneBot2 + OneBot v11 适配器，请先部署好 NoneBot2 运行环境（Python >= 3.10）。

```bash
# 1. 安装 NoneBot2 与 OneBot v11 适配器
pip install nonebot2 nonebot-adapter-onebot

# 2. 安装本插件依赖
pip install httpx Pillow fonttools playwright

# 3. 安装 NoneBot 插件
nb plugin install nonebot-plugin-apscheduler
nb plugin install nonebot-plugin-localstore

# 4. 将本插件目录放入 NoneBot2 项目的 plugins/ 目录
```

> 本插件使用 Playwright 渲染部分页面，请额外安装浏览器内核：
>
> ```bash
> python -m playwright install chromium
> ```

在 NoneBot2 的 `pyproject.toml`（或机器人的 `bot.py`）中加载插件：

```python
# bot.py
import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_plugin("nonebot_plugin_dota2_watcher")  # 或使用 plugins 目录自动加载
nonebot.run()
```

## 配置

所有配置项的默认值保存在 [`nonebot_plugin_dota2_watcher/config.py`](nonebot_plugin_dota2_watcher/config.py)（请勿直接修改，升级会被覆盖）。用户配置的优先级为：**数据目录下的 `config.json`（首次运行自动生成）> `.env` / 环境变量 > 默认值**。启动机器人一次后，即可在 `<数据目录>/config.json` 中直接编辑全部配置项。

| 环境变量                           | 说明                                                                           | 默认值        |
| ------------------------------ | ---------------------------------------------------------------------------- | ---------- |
| **API 密钥**                    |                                                                              |            |
| `D2W_STEAM_API_KEY`            | Steam Web API Key（用于拉取玩家比赛历史），[申请地址](https://steamcommunity.com/dev/apikey)  | 空          |
| `D2W_TI_STEAM_API_KEY`         | TI 赛事 / 实时单局使用的独立 API Key，留空时复用上面的 Key                                       | 空          |
| `D2W_STRATZ_TOKEN`             | Stratz GraphQL API Token（`/英雄池` 使用），[申请地址](https://stratz.com/api)           | 空          |
| **网络**                        |                                                                              |            |
| `D2W_PROXIES`                  | 网络代理，如 `{"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}` | `{}`       |
| `D2W_TIMEOUT`                  | 网络请求超时（秒）                                                                    | `20`       |
| `D2W_DOWNLOAD_TIMEOUT`         | 下载超时（秒）                                                                      | `60`       |
| `D2W_GH_PROXY`                 | GitHub 加速前缀（国内访问 GitHub raw 资源时使用，可替换为其它代理）                                | `https://gh-proxy.com` |
| **播报与内容**                   |                                                                              |            |
| `D2W_ALL_NICKNAME`             | “全体”播报的昵称关键字                                                                 | `全体`       |
| `D2W_GAME_MODE`                | 不播报的游戏模式列表                                                                   | `[15, 19]` |
| `D2W_BENCHMARK_THRESHOLD`      | 评分标准（0\~1，仅 OpenDota 支持）                                                     | `0.5`      |
| **定时任务**                     |                                                                              |            |
| `D2W_TI_ENABLED`               | TI 赛事定时任务总开关；设为 `false` 时完全不注册、不轮询，零性能开销（彻底关闭）                            | `false`    |
| `D2W_NEWS_ENABLED`             | 官方新闻定时任务总开关；设为 `false` 时完全不注册、不轮询，零性能开销（彻底关闭）                            | `true`     |
| `D2W_TI_POLL_INTERVAL`         | TI 赛果轮询间隔（秒）                                                                 | `10`       |
| `D2W_NEWS_POLL_INTERVAL`       | 新闻轮询间隔（秒）                                                                    | `60`       |
| `D2W_MATCH_POLL_INTERVAL`      | 玩家比赛轮询间隔（秒）                                                                  | `60`       |
| `D2W_HISTORY_CONCURRENCY`     | 拉取玩家比赛历史的并发上限（避免触发 Steam 限流）                                        | `3`        |
| **缓存**                        |                                                                              |            |
| `D2W_CACHE_EXPIRE_SECONDS`     | D2PT / 玩家数据缓存时长（秒）                                                           | `10800`    |
| `D2W_CORE_BUILD_CACHE_SECONDS` | 核心出装数据缓存时长（秒）                                                                | `259200`   |
| `D2W_CORE_BUILD_IMAGE_CACHE_SECONDS` | 核心出装生成图片缓存时长（秒）                                                   | `86400`    |

> 运行期数据与缓存目录由 [nonebot-plugin-localstore](https://github.com/nonebot/plugin-localstore) 统一管理，默认写入系统用户数据目录；如需自定义，可设置 `LOCALSTORE_DATA_DIR` / `LOCALSTORE_CACHE_DIR`。

示例 `.env`：

```dotenv
D2W_STEAM_API_KEY=你的Steam_Web_API_Key
D2W_PROXIES={"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
D2W_GH_PROXY=https://gh-proxy.com
D2W_TIMEOUT=20
D2W_TI_ENABLED=false      # 可选：TI 赛事定时任务总开关（默认已关闭），设 true 开启
D2W_NEWS_ENABLED=true     # 可选：官方新闻定时任务总开关，设 false 彻底关闭以省性能
LOCALSTORE_DATA_DIR=./data     # 可选：数据目录（订阅信息、持久缓存）
LOCALSTORE_CACHE_DIR=./cache   # 可选：图片/战报等可再生缓存目录
```

## 使用方法

在群内发送以下命令（`/` 前缀命令需确保机器人已启用命令前缀）：

| 命令                                | 说明                       | 权限  |
| --------------------------------- | ------------------------ | --- |
| `/添加刀塔玩家 [昵称] [steam的id]`         | 订阅玩家，新比赛自动播报             | 任意  |
| `/查看刀塔玩家`                         | 查看本群已订阅玩家列表              | 任意  |
| `/删除刀塔玩家 [昵称]`                    | 删除本群指定玩家                 | 管理员 |
| `开启[昵称]的群播报`                      | 开启某玩家的播报（昵称填“全体”可一次控制全部） | 任意  |
| `关闭[昵称]的群播报`                      | 关闭某玩家的播报                 | 任意  |
| `/d2pt [位置1-5]`                   | 查看 D2PT 胜率 / 线优数据        | 任意  |
| `/战报 [比赛编号]`                      | 生成开黑战报图片                 | 任意  |
| `/出装 [英雄名] [位置1-5] [dark\|light]` | 生成核心出装图片                 | 任意  |
| `/ti [小组赛\|正赛]`                  | 查看 TI 赛事战报图片（默认自动判断最新阶段） | 任意  |
| `/英雄池 [steam_id 或 玩家昵称] [min\|mid\|max 或 小\|中\|大]` | 生成玩家英雄池环形图（数量档位可选，默认 min/25 场） | 任意  |
| `/订阅`                            | 查看订阅状态（全局总开关与本群开关）        | 管理员 |
| `/订阅 新闻 [开\|关]`                 | 切换或指定开、关官方新闻订阅并展示状态       | 管理员 |
| `/订阅 ti [开\|关]`                   | 切换或指定开、关 TI 赛事订阅并展示状态      | 管理员 |

> 示例：`/添加刀塔玩家 萧瑟先辈 898754153`、`/出装 敌法师 1 dark`、`/战报 1000000000`、`/英雄池 277774684`。

## 目录结构

```
nonebot_plugin_dota2_watcher/     # 插件包
├── __init__.py              # 插件入口与元数据
├── config.py                # 插件配置
├── utils.py                 # 网络请求与通用工具
├── dota_dicts.py            # DOTA2 静态字典
├── hero_nicknames.py        # 英雄昵称映射
├── handlers/                # NoneBot 交互层
│   ├── commands.py          # 命令处理器
│   └── scheduler.py         # 定时任务（TI / 新闻 / 玩家比赛轮询）
├── services/                # 业务逻辑层
│   ├── service.py           # 命令与定时任务共用的业务函数
│   ├── store.py             # 订阅数据存储
│   └── player.py            # 玩家数据模型
├── datasources/             # 外部数据源
│   ├── request_match.py     # Steam / OpenDota 请求
│   ├── d2pt.py              # D2PT 数据
│   ├── xiaoheihe.py         # 小黑盒比赛数据源（OpenDota 兜底）
│   ├── ti_results.py        # TI 赛果
│   └── hero_pool.py         # Stratz 英雄池数据
├── generators/              # 图片 / 文本生成
│   ├── core_build.py        # 核心出装图生成
│   ├── match_builder.py     # 开黑战报生成
│   ├── match_report.py      # 战报图片绘制
│   ├── hero_pool.py         # 英雄池环形图生成
│   └── shared_browser.py    # 共享 Playwright 浏览器
```

## 致谢

- [NoneBot2](https://nonebot.dev/)
- [OpenDota](https://www.opendota.com/) / Steam Web API 提供的比赛数据
- [d2pt\_bot](https://github.com/Elmeir/d2pt_bot) 提供的 D2PT 出装与位置数据
- [Steam\_watcher](https://github.com/SonodaHanami/Steam_watcher) 提供的战报素材

