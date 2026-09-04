#!/usr/bin/env python3
"""
DOTA2 Pro Tracker 核心出装图片生成器（HTML 渲染版）

依赖：
    pip install playwright
    playwright install chromium

用法：
    python core_build.py                       # 默认 Anti-Mage, Most Played 位置
    python core_build.py Anti-Mage 1
    python core_build.py Axe 3 -o out.png
"""

import asyncio
import base64
import os
import re
import sys
import time

# 兼容两种运行方式：作为插件包被导入，或作为独立脚本直接运行
if __package__:
    from ..dota_dicts import HEROES_LIST_CHINESE
    from ..hero_nicknames import resolve_nickname
    from . import shared_browser
else:
    import shared_browser
    from dota_dicts import HEROES_LIST_CHINESE
    from hero_nicknames import resolve_nickname

# ============================================================
# 目录配置
# ============================================================
# 兼容两种运行方式：作为插件包被导入，或作为独立脚本直接运行。
# 所有目录 / URL / 缓存等配置统一从 config.py 读取。
if __package__:
    from .. import config as _cfg
    from ..utils import async_download_bytes, dumpjson, image_to_data_uri, loadjson
else:
    import config as _cfg
    from utils import async_download_bytes, dumpjson, image_to_data_uri, loadjson

WORK_DIR = str(_cfg.BASE_DIR)
IMAGES_DIR = str(_cfg.IMAGES_DIR)
ABILITIES_IMAGES_DIR = os.path.join(IMAGES_DIR, "abilities/")
OUTPUT_DIR = str(_cfg.OUTPUT_DIR)
# 数据/缓存 JSON 统一放在 data/ 目录
DATA_FILE = _cfg.DATA_DIR / "d2pt_core_build.json"
ABILITIES_FILE = _cfg.DATA_DIR / "abilities.json"
TALENTS_CN_FILE = _cfg.DATA_DIR / "talents_cn.json"
ITEMS_FILE = _cfg.DATA_DIR / "items.json"
DATA_URL = _cfg.D2PT_CORE_BUILD_URL
TALENTS_CN_URL = _cfg.D2PT_TALENTS_CN_URL
DATA_CACHE_SECONDS = _cfg.config.d2w_core_build_cache_seconds  # 72 小时
# 生成图片缓存时长（秒）：在缓存期内复用已生成的图片，避免重复渲染
IMAGE_CACHE_SECONDS = _cfg.config.d2w_core_build_image_cache_seconds  # 24 小时 / 1 天

# 技能图片 CDN（加点图标）
ABILITY_IMAGE_URL = _cfg.ABILITY_IMAGE_URL
# 物品图片 CDN
ITEM_IMAGE_URL = _cfg.ITEM_IMAGE_URL
# OpenDota 物品字典（用于本地生成 items.json）
OPENDOTA_ITEMS_URL = _cfg.OPENDOTA_ITEMS_URL

# 仓库内图标（经 gh-proxy 从 GitHub 仓库拉取并本地缓存）
REPO_RAW_BASE = _cfg.D2PT_REPO_ICON_BASE
# 加点中天赋（special_bonus_*）使用的占位图标
ATTRIBUTE_BONUS_IMAGE_NAME = "attribute_bonus"
# 加点中空技能槽（ability_base）使用的占位图标
UNDEFINED_IMAGE_NAME = "undefined"

# npc_ability_ids.txt 源文件（用于本地生成 abilities.json）
NPC_ABILITY_IDS_FILE = os.path.join(WORK_DIR, "npc_ability_ids.txt")
NPC_ABILITY_IDS_URLS = [_cfg.NPC_ABILITY_IDS_URL]

ITEM_IMAGES_DIR = os.path.join(IMAGES_DIR, "item")

for d in [OUTPUT_DIR, ABILITIES_IMAGES_DIR, ITEM_IMAGES_DIR]:
    os.makedirs(d, exist_ok=True)

# ============================================================
# 颜色风格主题
# ============================================================
# 网页字体
FONT_FAMILY = (
    '"Segoe UI", Frutiger, "Frutiger Linotype", "Dejavu Sans", "Helvetica Neue", Arial, sans-serif'
)

# 暗色风格（照抄网页 div 计算样式）
THEME_DARK = {
    "container_bg": "#100f0f",  # bg-d-gray-5
    "container_border": "#1b1a1a",  # border-d-gray-8
    "card_bg": "#070707",  # bg-d2pt-gray-1
    "card_border": "rgba(255, 255, 255, 0.1)",  # border-one
    "core_bg": "#0b2b34",  # bg-d2pt-blue-3（深色）
    "core_fg": "#2e829d",  # text/border-d2pt-blue-9（浅色）
    "title_color": "#ffffff",  # text-white/100
    "time_color": "rgba(255, 255, 255, 0.4)",  # text-white/40
    "desc_color": "rgba(255, 255, 255, 0.4)",  # 描述文字
    # 加点/天赋专用颜色
    "ult_level_bg": "#1a3540",  # 6级（大招）高亮背景
    # 天赋新样式配色
    "talent_win_color": "#7cc45c",  # 胜率绿色
    "talent_pick_color": "#64c8f8",  # 选择率蓝色
    "talent_bg_alt1": "#1a1f25",  # 天赋卡片背景1（深色）
    "talent_bg_alt2": "#242b33",  # 天赋卡片背景2（稍浅）
    "talent_circle_bg": "#2a323c",  # 中间等级圆圈背景
    "talent_circle_shadow": "rgba(0,0,0,0.6)",  # 圆圈阴影
    "talent_line_color": "#2a323c",  # 连接线颜色
    "talent_title_color": "#ffffff",  # 天赋标题颜色
    "talent_text_color": "#e0e0e0",  # 天赋文字颜色
}

# 亮色风格（与暗色相反，物品图标不变）
THEME_LIGHT = {
    "container_bg": "#ffffff",  # 与 card_bg 互换
    "container_border": "#e0e0e0",
    "card_bg": "#f0f0f0",  # 与 container_bg 互换
    "card_border": "rgba(0, 0, 0, 0.1)",
    "core_bg": "#f5e8e4",  # CORE 背景色
    "core_fg": "#D27F64",  # CORE 文字/边框色
    "title_color": "#000000",
    "time_color": "#000000",  # 时间字体黑色
    "desc_color": "#888888",  # 描述文字灰色
    # 加点/天赋专用颜色（亮色主题与暗色反色）
    "ult_level_bg": "#f5e8e4",  # 6级（大招）高亮背景
    # 天赋新样式配色（亮色主题直接使用参考图配色）
    "talent_win_color": "#7DBF5D",  # 胜率绿色
    "talent_pick_color": "#6DD5FA",  # 选择率蓝色
    "talent_bg_alt1": "#F7F7F7",  # 天赋卡片背景1（浅白）
    "talent_bg_alt2": "#EFF0F4",  # 天赋卡片背景2（浅灰）
    "talent_circle_bg": "#EFF0F4",  # 中间等级圆圈背景
    "talent_circle_shadow": "rgba(0,0,0,0.15)",  # 圆圈阴影
    "talent_line_color": "#F7F7F7",  # 连接线颜色
    "talent_title_color": "#444444",  # 天赋标题颜色
    "talent_text_color": "#555555",  # 天赋文字颜色
}

THEMES = {"dark": THEME_DARK, "light": THEME_LIGHT}


# ============================================================
# 工具函数
# ============================================================
async def _download_to(url, filepath, quiet=False):
    """异步下载 url 内容到本地文件，成功返回 True，失败返回 False。"""
    try:
        body = await async_download_bytes(url, timeout=_cfg.config.d2w_download_timeout)
    except Exception as e:
        if not quiet:
            print(f"警告: 下载 {os.path.basename(filepath)} 失败: {e}", file=sys.stderr)
        return False
    os.makedirs(os.path.dirname(os.path.abspath(filepath)) or ".", exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(body)
    return True


def _repo_icon_url(name):
    """仓库内技能图标名 -> gh-proxy 源 URL（国内可访问，不使用 jsDelivr）。"""
    return REPO_RAW_BASE + f"images/abilities/{name}.png"


async def _download_sources(sources, filepath):
    """按顺序尝试多个源下载，任一成功即返回 True；仅最后一个源失败时打印告警。"""
    for i, url in enumerate(sources):
        if await _download_to(url, filepath, quiet=i < len(sources) - 1):
            return True
    return False


def generate_abilities():
    """从 npc_ability_ids.txt 解析生成 abilities.json（技能 ID -> 名称）。成功返回 True。"""
    if not os.path.exists(NPC_ABILITY_IDS_FILE):
        return False
    with open(NPC_ABILITY_IDS_FILE, encoding="utf-8") as f:
        text = f.read()
    pattern = re.compile(r'"([a-zA-Z0-9_]+)"\s*"(\d+)"', re.M)
    ability_id = {int(m.group(2)): m.group(1) for m in pattern.finditer(text)}
    dumpjson({str(k): v for k, v in ability_id.items()}, ABILITIES_FILE)
    return True


async def ensure_data_file():
    """检查 d2pt_core_build.json 是否存在或超过 72 小时，需要时从远程下载。
    同时同步更新天赋中文名文件 talents_cn.json，并确保技能映射 abilities.json 存在。

    网络下载用原生异步 async_download_bytes，不阻塞事件循环；
    本地文件读取/生成仍保持同步（快且无 I/O 等待）。
    """
    global TALENTS_CN, ABILITIES
    need_download = False
    if not os.path.exists(DATA_FILE):
        need_download = True
    else:
        mtime = os.path.getmtime(DATA_FILE)
        if time.time() - mtime > DATA_CACHE_SECONDS:
            need_download = True

    if need_download:
        print(f"正在更新数据文件: {DATA_URL}")
        if await _download_to(DATA_URL, DATA_FILE):
            print(f"数据文件已更新: {DATA_FILE}")
        elif not os.path.exists(DATA_FILE):
            print(f"错误: 本地数据文件 {DATA_FILE} 不存在", file=sys.stderr)

        # 与主数据同步更新天赋中文名
        print(f"正在同步更新天赋中文名: {TALENTS_CN_URL}")
        if await _download_to(TALENTS_CN_URL, TALENTS_CN_FILE):
            print(f"天赋中文名已更新: {TALENTS_CN_FILE}")
        elif not os.path.exists(TALENTS_CN_FILE):
            print(f"警告: 本地天赋中文名文件 {TALENTS_CN_FILE} 不存在", file=sys.stderr)

    # 刷新天赋中文名映射（首次运行或更新后，导入时加载的空 dict 需重新读取）
    TALENTS_CN = loadjson(TALENTS_CN_FILE)

    # 确保技能映射存在：仅在 abilities.json 缺失时，用 npc_ability_ids.txt 本地生成（不从仓库拉取）
    if not os.path.exists(ABILITIES_FILE):
        if not os.path.exists(NPC_ABILITY_IDS_FILE):
            await _download_sources(NPC_ABILITY_IDS_URLS, NPC_ABILITY_IDS_FILE)
        if os.path.exists(NPC_ABILITY_IDS_FILE):
            generate_abilities()
            # 生成完成后删除临时源文件，只保留 abilities.json
            os.remove(NPC_ABILITY_IDS_FILE)
    ABILITIES = loadjson(ABILITIES_FILE)
    # 物品映射：items.json 可能缺失（如数据目录迁移后），兜底保证其就绪（内部同样含网络下载）
    await ensure_items_cache()


def _avg_time_minutes(time_str):
    """把 '8m' / '12m' / None 解析成分钟数（用于排序，空值排最前，无法解析排最后）。"""
    if not time_str:
        return 0
    s = time_str.strip().lower().rstrip("m")
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return 9999


def image_name_from_url(url):
    """把物品图标 URL 转成本地图标文件名，如 '/static/items/magic_wand.png' -> 'item_magic_wand.png'。"""
    if not url:
        return "item_None.png"
    base = os.path.basename(url)
    return f"item_{base}" if base else "item_None.png"


def _cn_name_to_hero_id(cn_name, data):
    """中文名 -> 英雄 ID（找不到返回 None）。"""
    for hid, cn in HEROES_LIST_CHINESE.items():
        if cn == cn_name:
            hid_str = str(hid)
            if hid_str in data:
                return hid_str
    return None


def find_hero(data, hero_query):
    """根据英雄名（英文 displayName / 中文名 / 昵称 / ID）查找，返回 (hero_id, hero_data)。"""
    # 1. 直接匹配 ID
    if hero_query in data:
        return hero_query, data[hero_query]
    q = hero_query.strip()
    # 2. 中文名 / 昵称 -> 中文名 -> 英雄
    for cn in (q, resolve_nickname(q)):
        if not cn:
            continue
        hid = _cn_name_to_hero_id(cn, data)
        if hid:
            return hid, data[hid]
    # 3. 精确匹配英文名
    ql = q.lower()
    for hid, hdata in data.items():
        if hdata.get("n", "").lower() == ql:
            return hid, hdata
    # 4. 模糊匹配英文名（仅当查询长度 >= 3 时，避免短词误匹配）
    if len(ql) >= 3:
        for hid, hdata in data.items():
            if ql in hdata.get("n", "").lower():
                return hid, hdata
    return None, None


# ============================================================
# HTML 构建 —— 直接照抄 div 结构与 Tailwind 类对应的 CSS
# ============================================================
def load_items_from_json():
    """从 items.json 加载物品 ID -> 名称映射（文件缺失时静默返回空）。"""
    data = loadjson(ITEMS_FILE)
    return {int(k): v for k, v in data.items()}


async def ensure_items_cache():
    """确保物品 ID -> 名称映射可用；items.json 缺失时从 OpenDota 拉取并缓存。

    items.json 原本由战报模块（match_report）生成，出装命令单独运行时可能缺失，
    这里在缺失时兜底拉取，保证物品图标能正确解析显示。
    """
    global ITEMS
    if ITEMS:
        return
    items = load_items_from_json()
    if items:
        ITEMS = items
        return
    # 从 OpenDota 拉取并转成与 match_report 一致的 {id: name} 缓存格式
    if await _download_to(OPENDOTA_ITEMS_URL, ITEMS_FILE):
        raw = loadjson(ITEMS_FILE)
        items = {
            int(it.get("id")): key.replace("item_", "")
            for key, it in raw.items()
            if isinstance(it, dict) and it.get("id")
        }
        if items:
            dumpjson({str(k): v for k, v in items.items()}, ITEMS_FILE)
    ITEMS = items or {}
    if not ITEMS:
        print(
            f"警告: 无法获取物品字典 {ITEMS_FILE}，物品图标可能无法显示",
            file=sys.stderr,
        )


ITEMS = load_items_from_json()

# 技能ID -> 技能名 映射（由 generate_abilities 从 npc_ability_ids.txt 本地生成）
ABILITIES = loadjson(ABILITIES_FILE)
# 天赋名 -> 中文名 映射（远程 talents_cn.json 缓存）
TALENTS_CN = loadjson(TALENTS_CN_FILE)


def item_id_to_name(item_id):
    """物品 ID -> 名称。"""
    return ITEMS.get(item_id, f"item_{item_id}")


def ability_id_to_name(ability_id):
    """技能 ID -> 技能名。"""
    return ABILITIES.get(str(ability_id), f"ability_{ability_id}")


# 记录下载失败的技能名，避免每次生成都重试 + 重复告警
_MISSING_ABILITIES = set()
# 记录下载失败的物品名，避免每次生成都重试 + 重复告警
_MISSING_ITEMS = set()


def _solid_png_data_url(rgb=(128, 128, 128), w=64, h=36):
    """生成一个纯色 PNG 的 data URL（用于无图标的占位）。"""
    import struct
    import zlib

    def _chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


async def ability_name_to_image(ability_name):
    """技能名 -> base64 data URL（优先本地缓存，缺失则异步下载）。

    - special_bonus_* 天赋：使用 attribute_bonus 图标（仓库 gh-proxy）。
    - ability_base（空槽）：使用 undefined 图标。
    - 其余技能缺失时按 仓库 gh-proxy -> steamstatic CDN 顺序下载；
      全部失败返回灰色占位图，并缓存失败结果避免每次重试。
    """
    local_path = os.path.join(ABILITIES_IMAGES_DIR, f"{ability_name}.png")
    # 天赋：用 attribute_bonus 图标（仓库 gh-proxy -> Steam CDN）
    if ability_name.startswith("special_bonus_"):
        bonus_path = os.path.join(ABILITIES_IMAGES_DIR, f"{ATTRIBUTE_BONUS_IMAGE_NAME}.png")
        if not os.path.exists(bonus_path):
            await _download_sources(
                [
                    _repo_icon_url(ATTRIBUTE_BONUS_IMAGE_NAME),
                    ABILITY_IMAGE_URL.format(name=ATTRIBUTE_BONUS_IMAGE_NAME),
                ],
                bonus_path,
            )
        if os.path.exists(bonus_path):
            return image_to_data_uri(bonus_path)
    # 空技能槽（ability_base，ID 0）：使用 undefined 图标（仓库 gh-proxy）
    if ability_name == "ability_base":
        undefined_path = os.path.join(ABILITIES_IMAGES_DIR, f"{UNDEFINED_IMAGE_NAME}.png")
        if not os.path.exists(undefined_path):
            await _download_to(_repo_icon_url(UNDEFINED_IMAGE_NAME), undefined_path)
        if os.path.exists(undefined_path):
            return image_to_data_uri(undefined_path)
        return _solid_png_data_url()
    if ability_name in _MISSING_ABILITIES:
        return _solid_png_data_url()
    if not os.path.exists(local_path):
        # 下载顺序：仓库 gh-proxy -> steamstatic CDN
        sources = [_repo_icon_url(ability_name), ABILITY_IMAGE_URL.format(name=ability_name)]
        if not await _download_sources(sources, local_path):
            # 全部失败（最后一个源已告警）：标记为缺失，返回占位图，不再重试
            _MISSING_ABILITIES.add(ability_name)
            return _solid_png_data_url()
    if os.path.exists(local_path):
        try:
            return image_to_data_uri(local_path)
        except Exception:
            pass
    return _solid_png_data_url()


def talent_cn(name):
    """天赋名 -> 中文名（找不到时回退英文名）。"""
    return TALENTS_CN.get(name, name)


def item_name_to_image(item_name):
    """物品名称 -> 图片路径（对应 images/item_*.png）。"""
    return f"/static/items/{item_name}.png"


def get_start_item_priority(item_name):
    """出门装物品排序优先级。"""
    priority_map = {
        "tango": 1,
        "magic_stick": 2,
        "magic_wand": 2,
        "blood_grenade": 3,
        "quelling_blade": 3,
        "ward_sentry": 4,
        "branches": 998,
        "ward_observer": 999,
    }
    return priority_map.get(item_name, 5)


def get_lategame_item_priority(item_name):
    """六格神装物品排序优先级。"""
    priority_map = {
        "boots_of_bearing": 1,
        "travel_boots_2": 1,
        "travel_boots": 1,
        "tranquil_boots": 1,
        "phase_boots": 1,
        "arcane_boots": 1,
        "boots": 1,
        "power_treads": 1,
        "guardian_greaves": 1,
        "magic_stick": 2,
        "magic_wand": 2,
        "arcane_blink": 2,
        "swift_blink": 2,
        "overwhelming_blink": 2,
        "blink": 2,
        "bfury": 3,
        "manta": 3,
        "black_king_bar": 4,
        "rapier": 999,
    }
    return priority_map.get(item_name, 5)


def convert_core_build(core_build_raw):
    """转换核心出装格式：[{'id': 36, 'at': '6m', 'ic': True}, ...]
    -> [{'item': ..., 'image': ..., 'avg_time': ..., 'is_core': ...}]。"""
    items = []
    for entry in core_build_raw:
        item_id = entry.get("id")
        item_name = item_id_to_name(item_id)
        items.append(
            {
                "item": item_name,
                "image": item_name_to_image(item_name),
                "avg_time": entry.get("at") or "0m",
                "is_core": entry.get("ic", False),
            }
        )
    return items


def _resolve_item_icons(item_ids, priority_fn):
    """把物品 ID 列表解析成按优先级排序的图标 dict（去掉内部 priority 字段）。"""
    items = []
    for item_id in item_ids:
        name = item_id_to_name(item_id)
        items.append(
            {
                "item": name,
                "image": item_name_to_image(name),
                "priority": priority_fn(name),
            }
        )
    items.sort(key=lambda x: x["priority"])
    return [{k: v for k, v in it.items() if k != "priority"} for it in items]


def convert_lategame_inventories(lategame_raw):
    """转换 lategame_inventories 格式：{'30+': [ids], ...} -> [{items, period}]。"""
    period_map = {
        "30+": "中期",
        "45+": "后期",
        "55+": "大后期",
    }
    results = []
    for time_key, item_ids in lategame_raw.items():
        results.append(
            {
                "period": period_map.get(time_key, time_key),
                "time_key": time_key,
                "items": _resolve_item_icons(item_ids, get_lategame_item_priority),
            }
        )
    return results


async def convert_ability_build(ability_build_raw, match_count=0):
    """转换加点格式：[{'p': ..., 'wr': ..., 'b': [skill_id, ...]}, ...]
    -> {most_used: {...}, best_wr: {...}}，各含 build/pick_rate/win_rate/matches。"""
    if not ability_build_raw:
        return None

    # 预先并发下载缺失的技能图标，避免后续逐个串行下载拖慢首张生成
    missing = set()
    for entry in ability_build_raw:
        for skill_id in entry.get("b", []):
            name = ability_id_to_name(skill_id)
            if not os.path.exists(os.path.join(ABILITIES_IMAGES_DIR, f"{name}.png")):
                missing.add(name)
    if missing:
        await asyncio.gather(*(ability_name_to_image(name) for name in missing))

    async def _build(entry):
        build = []
        for skill_id in entry.get("b", []):
            ability_name = ability_id_to_name(skill_id)
            build.append(
                {
                    "ability": ability_name,
                    "image": await ability_name_to_image(ability_name),
                }
            )
        # 大招固定在第 6 级（下标5）学习：仅该槽位标记为 is_ult
        for idx, ab in enumerate(build):
            ab["is_ult"] = idx == 5
        p = entry.get("p", 0)
        return {
            "build": build,
            "pick_rate": p,
            "win_rate": entry.get("wr", 0),
            "matches": round(match_count * p),
        }

    # 最多人使用（选用率最高）与最高胜率两套加点
    most_used = max(ability_build_raw, key=lambda x: x.get("p", 0))
    best_wr = max(ability_build_raw, key=lambda x: x.get("wr", 0))
    return {
        "most_used": await _build(most_used),
        "best_wr": await _build(best_wr),
    }


def convert_talents(talents_raw):
    """转换天赋格式：[{'l': 10, 'lf': {...}, 'rt': {...}, 'c': 'lt', 'wr': ...}, ...]
    -> [{level, left: {name, cn, p, wr}, right: {...}, chosen}]，按等级倒序。"""
    results = []
    for t in talents_raw:

        def _opt(side):
            o = t.get(side, {})
            return {
                "name": o.get("n", ""),
                "cn": talent_cn(o.get("n", "")),
                "p": o.get("p", 0),
                "wr": o.get("wr", 0),
            }

        chosen = "left" if t.get("c") == "lt" else "right"
        results.append(
            {
                "level": t.get("l"),
                "left": _opt("lf"),
                "right": _opt("rt"),
                "chosen": chosen,
            }
        )
    # 从高等级（25）到低等级（10）倒序显示
    results.sort(key=lambda x: x.get("level") or 0, reverse=True)
    return results


def _section_title(text, theme):
    """区块标题（如 最常见的出装路线 / 出门装 / 大后期）。"""
    return (
        f'<div style="font-size: 0.975rem; line-height: 1.5rem; '
        f'color: {theme["desc_color"]};">{text}</div>'
    )


def _section_block(title_html, rows_html, gap="0.375rem"):
    """构建"标题 + 内容"区块。标题包含在区块容器内，gap 控制标题到内容的间距；
    区域之间的间隔由外层统一 gap 容器控制。"""
    return (
        f'<div style="display: flex; flex-direction: column; gap: {gap};">'
        f"{title_html}"
        f'<div style="display: flex; gap: 0.234375rem; align-items: center;">'
        f'<div style="display: flex; flex-direction: column; gap: 0.234375rem; width: 100%;">'
        f"{rows_html}"
        "</div>"
        "</div>"
        "</div>"
    )


async def build_item_card(item, theme=THEME_DARK, show_time=True, show_core=True, stretch=False):
    """构建单个物品卡片 HTML，完全照抄网页 div 结构。

    stretch=True 时卡片按 6 格网格固定宽度（与天赋/加点对齐），不足 6 格也保持相同列位。
    """
    time_str = item.get("avg_time") or "0m"
    item_name = item.get("item") or ""
    image_url = item.get("image") or ""
    is_core = item.get("is_core", False)

    # 读取本地物品图标，转成 data URL（物品图已归档到 images/item/）
    img_filename = image_name_from_url(image_url)
    img_path = os.path.join(ITEM_IMAGES_DIR, img_filename)
    if not os.path.exists(img_path):
        # 本地缺失时按需从 CDN 下载（失败则标记，避免重复告警）
        if item_name and img_filename not in _MISSING_ITEMS:
            url_name = "recipe" if item_name.startswith("recipe") else item_name
            if not await _download_to(ITEM_IMAGE_URL.format(name=url_name), img_path, quiet=True):
                _MISSING_ITEMS.add(img_filename)
    if os.path.exists(img_path):
        bg_image = f"url('{image_to_data_uri(img_path)}')"
    else:
        bg_image = "none"

    # CORE 徽标（如果 is_core 且 show_core=True）
    core_badge = ""
    if show_core and is_core:
        core_badge = (
            '<div style="position: absolute; bottom: calc(-1.359375rem); left: 50%; '
            "z-index: 10; transform: translateX(-50%); "
            f"background-color: {theme['core_bg']}; "
            "font-size: 0.84375rem; "
            f"color: {theme['core_fg']}; "
            "padding: 0.1875rem 0.375rem; "  # py-0.5 px-1
            "border-radius: 0.5625rem; "  # rounded-md
            "font-weight: 500; "  # font-medium
            f"border: 0.09375rem solid {theme['core_fg']}; "  # border border-d2pt-blue-9
            'white-space: nowrap;">CORE</div>'
        )

    # 时间标签 HTML
    time_html = ""
    if show_time:
        time_html = (
            f'<div style="display: flex; gap: 0.28125rem; justify-content: center; '
            f"align-items: center; font-size: 1rem; line-height: 0.609375rem; "
            f'color: {theme["talent_text_color"]}; font-weight: 400;">{time_str}</div>'
        )

    # 卡片：flex flex-col gap-2 relative bg-d2pt-gray-1 border-one p-2 rounded-md
    # stretch：固定 6 格网格宽度（1.875rem = 5×gap），不足 6 格也保持相同列位
    stretch_css = "flex: 0 0 calc((100% - 1.875rem) / 6); min-width: 0; " if stretch else ""
    card = (
        f'<div style="{stretch_css}display: flex; flex-direction: column; gap: 0.46875rem; '
        "position: relative; "
        f"background-color: {theme['card_bg']}; "
        f"border: 0.09375rem solid {theme['card_border']}; "  # border-one
        "padding: 0.328125rem; "  # 缩小padding使图标在卡片内居中
        'border-radius: 0.5625rem;">'  # rounded-md
        f"{time_html}"
        f"{core_badge}"
        # 物品图标：mx-auto w-[32px] h-[24px] text-xs text-shadow font-medium text-right rounded-md
        f'<div style="margin: 0 auto; width: 3rem; height: 2.25rem; '
        f"font-size: 1.125rem; font-weight: 500; text-align: right; "
        f"border-radius: 0.5625rem; "
        f'background-image: {bg_image}; background-size: auto 2.25rem; background-position: center;" '
        f'title="{item_name}"></div>'
        "</div>"
    )
    return card


async def _build_item_cards(items, theme, show_time=True, show_core=True):
    """并发构建多个物品卡片 HTML（卡片内可能按需异步下载缺失图标）。"""
    return await asyncio.gather(
        *(build_item_card(it, theme, show_time, show_core, stretch=True) for it in items)
    )


async def build_items_rows(items, theme, show_time=True, show_core=True, max_per_row=7, wrap=True):
    """构建物品行 HTML。

    wrap=True 时物品过多会自动换行/分两行；wrap=False 时全部排列在一行不换行。
    """
    gap = "0.375rem"
    if not wrap:
        # 出门装/六格神装：固定 6 格网格宽度（与天赋/加点对齐），不足 6 格也保持相同列位
        cards = await _build_item_cards(items, theme, show_time, show_core)
        return (
            f'<div style="display: flex; flex-wrap: nowrap; gap: {gap}; width: 100%; justify-content: flex-start;">'
            f"{''.join(cards)}"
            f"</div>"
        )
    if len(items) > max_per_row:
        # 不平均分配：每行放满 max_per_row 个，逐行构建，行间统一大间距
        rows = [items[i : i + max_per_row] for i in range(0, len(items), max_per_row)]
        parts = []
        for i, chunk in enumerate(rows):
            last = i == len(rows) - 1
            has_core = show_core and any(it.get("is_core", False) for it in chunk)
            # 行间用统一间距（足够容纳 CORE 徽标下挂）；最后一行按需留徽标底部空间
            margin = f"margin-bottom: calc({gap} + 1.078125rem);"
            if last:
                margin = "margin-bottom: 1.125rem;" if has_core else ""
            cards = await _build_item_cards(chunk, theme, show_time, show_core)
            parts.append(
                f'<div style="display: flex; flex-wrap: wrap; gap: {gap};{margin}">'
                + "".join(cards)
                + "</div>"
            )
        return "".join(parts)
    else:
        # 核心出装：如果只有一行且有CORE物品，添加额外的底部间距
        has_core = show_core and any(it.get("is_core", False) for it in items)
        margin = "margin-bottom: 1.125rem;" if has_core else ""
        cards = await _build_item_cards(items, theme, show_time, show_core)
        return (
            f'<div style="display: flex; flex-wrap: wrap; gap: {gap};{margin}">'
            f"{''.join(cards)}"
            f"</div>"
        )


def build_ability_build_html(ability_build, theme):
    """构建加点（技能升级顺序）HTML。"""

    def _variant(tag_label, v):
        # 构建技能图标 + 等级数字（图标与数字间垂直间距 0.1875rem）
        cells = []
        for idx, ab in enumerate(v["build"][:10]):
            level_num = idx + 1
            is_ult = ab.get("is_ult", False)
            cell_bg = theme["ult_level_bg"] if is_ult else "transparent"
            cells.append(
                f'<div style="display: flex; flex-direction: column; align-items: center; gap: 0.1875rem; flex: 1;">'
                f'<img src="{ab["image"]}" title="{ab["ability"]}" '
                f'style="width: 100%; height: auto; display: block; '
                f'border-radius: 0.375rem; background-color: {cell_bg};">'
                f'<span style="font-size: 0.975rem; color: {theme["talent_text_color"]}; '
                f"background-color: {cell_bg}; text-align: center; width: 100%; "
                f'padding-bottom: 0.09375rem; line-height: 1; display: block;">{level_num}</span>'
                f"</div>"
            )
        matches = v.get("matches", 0)
        win_rate_pct = v.get("win_rate", 0) * 100
        info_text = f"{matches} 场次 · {win_rate_pct:.1f}% 胜率"
        return (
            '<div style="position: relative; display: flex; flex-direction: column; gap: 0.28125rem; '
            f"background-color: {theme['talent_bg_alt1']}; "
            f"border: 0.09375rem solid {theme['card_border']}; "
            'border-radius: 0.5625rem; padding: 0.375rem 0.75rem; width: 100%;">'
            # 右上角标签（背景与卡片一致，随主题变化；边框 CORE 风格）
            f'<div style="position: absolute; top: -0.375rem; right: -0.234375rem; '
            f"z-index: 10; "
            f"background-color: {theme['talent_bg_alt1']}; "
            f"font-size: 0.84375rem; "
            f"color: {theme['core_fg']}; "
            f"padding: 0.1875rem 0.375rem; "
            f"border-radius: 0.5625rem; "
            f"font-weight: 500; "
            f"border: 0.09375rem solid {theme['core_fg']}; "
            f'white-space: nowrap;">{tag_label}</div>'
            # 比赛信息（与天赋文字同色）
            f'<div style="font-size: 1.02rem; color: {theme["talent_text_color"]}; font-weight: 400;">'
            f"{info_text}</div>"
            # 技能图标行（水平间距 0.46875rem，自适应填满卡片宽度）
            '<div style="display: flex; gap: 0.46875rem;">' + "".join(cells) + "</div>"
            "</div>"
        )

    parts = []
    if ability_build.get("most_used"):
        parts.append(_variant("使用最多", ability_build["most_used"]))
    if ability_build.get("best_wr"):
        parts.append(_variant("胜率最高", ability_build["best_wr"]))
    return (
        '<div style="display: flex; flex-direction: column; gap: 0.609375rem; width: 100%;">'
        + "".join(parts)
        + "</div>"
    )


def build_talents_html(talents, theme):
    """构建天赋 HTML。新样式：标题+图例、交替背景、竖条显示胜率/选择率、中间连接线圆圈。"""
    # 标题和图例
    header = (
        '<div style="display: flex; justify-content: space-between; align-items: center; '
        'margin-bottom: 0.375rem;">'
        f'<div style="font-size: 0.975rem; color: {theme["talent_title_color"]};">天赋</div>'
        f'<div style="display: flex; gap: 0.890625rem; font-size: 0.7rem; line-height: 1; '
        f'color: {theme["desc_color"]};">'
        f'<span style="display: flex; align-items: center; gap: 0.1875rem; flex-shrink: 0;">'
        f'<span style="width: 0.3125rem; height: 0.7rem; background-color: {theme["talent_win_color"]}; '
        f'border-radius: 0.0625rem; flex-shrink: 0;"></span>胜率</span>'
        f'<span style="display: flex; align-items: center; gap: 0.1875rem; flex-shrink: 0;">'
        f'<span style="width: 0.3125rem; height: 0.7rem; background-color: {theme["talent_pick_color"]}; '
        f'border-radius: 0.0625rem; flex-shrink: 0;"></span>选择率</span>'
        "</div>"
        "</div>"
    )

    def _build_opt(o, bg):
        w_pct = o.get("wr", 0) * 100
        p_pct = o.get("p", 0) * 100
        return (
            f'<div style="flex: 1 1 0; background: {bg}; border-radius: 0.1875rem; '
            f"padding: 0.5rem 0.5rem; display: flex; flex-direction: column; "
            f'align-items: center; gap: 0.1875rem; height: 4.375rem; overflow: hidden;">'
            f'<div style="font-size: 0.825rem; color: {theme["talent_text_color"]}; font-weight: 500; '
            f"text-align: center; line-height: 1.25; width: 100%; height: 2.0625rem; "
            f'display: flex; align-items: center; justify-content: center;">{o["cn"]}</div>'
            f'<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem; '
            f"font-size: 0.825rem; line-height: 1; font-weight: 600; "
            f'color: {theme["talent_text_color"]}; height: 1.125rem; width: 100%;">'
            f'<div style="display: flex; align-items: center; justify-content: flex-end; gap: 0.2rem; '
            f'transform: translateX(0.26rem);">'
            f'<span style="width: 0.3125rem; height: 1.125rem; background-color: {theme["talent_win_color"]}; '
            f'border-radius: 0.0625rem; flex-shrink: 0;"></span>'
            f'<span style="font-variant-numeric: tabular-nums; width: 3rem; text-align: left;">'
            f"{w_pct:.1f}%</span></div>"
            f'<div style="display: flex; align-items: center; justify-content: flex-start; gap: 0.2rem; '
            f'margin-left: 0.3rem;">'
            f'<span style="width: 0.3125rem; height: 1.125rem; background-color: {theme["talent_pick_color"]}; '
            f'border-radius: 0.0625rem; flex-shrink: 0;"></span>'
            f'<span style="font-variant-numeric: tabular-nums; width: 3rem; text-align: left;">'
            f"{p_pct:.1f}%</span></div>"
            f"</div>"
            f"</div>"
        )

    rows = []
    n = len(talents)
    # 天赋布局常量（基于原 px 设计 ×2 的 rem 值）
    circle_size = 2.75  # 圆圈直径 rem（原 36px×1.5 缩小）
    mid_col_width = "1.875rem"  # 中间等级列宽（圆圈略宽，覆盖到两侧卡片上）
    row_gap = 0.28125  # 行间距 rem（每行上下各，相邻两行合计 0.5625rem）
    line_width = 0.5625  # 连线宽 rem（原 6px×1.5）
    line_height = 4.6875  # 首尾行连线高 rem（原 50px×1.5）

    def _line_div(vert_css, height):
        """中间连接线 div（横跨中间列，圆圈盖在上面实现连续连接）。"""
        return (
            f'<div style="position: absolute; {vert_css}; left: 50%; '
            f"transform: translateX(-50%); width: {line_width}rem; "
            f"height: {height}; "
            f'background-color: {theme["talent_line_color"]};"></div>'
        )

    for idx, t in enumerate(talents):
        level = t["level"]
        left = t["left"]
        right = t["right"]

        # 背景与选择状态联动：选中的选项用背景2（稍浅/高亮），未选中的用背景1
        chosen = t.get("chosen", "left")
        left_bg = theme["talent_bg_alt2"] if chosen == "left" else theme["talent_bg_alt1"]
        right_bg = theme["talent_bg_alt2"] if chosen == "right" else theme["talent_bg_alt1"]

        # 中间等级列竖线：首行只向下、末行只向上、中间行贯通上下
        if idx == 0:
            line_html = _line_div(f"top: {circle_size / 2}rem", f"{line_height}rem")
        elif idx == n - 1:
            line_html = _line_div(f"bottom: {circle_size / 2}rem", f"{line_height}rem")
        else:
            line_html = _line_div(f"top: -{row_gap}rem", f"calc(100% + {row_gap * 2}rem)")

        mid_col = (
            f'<div style="position: relative; width: {mid_col_width}; flex: 0 0 {mid_col_width}; '
            f'display: flex; align-items: center; justify-content: center;">'
            f"{line_html}"
            # 等级圆圈（z-index更高盖在竖线上，同色实现连续视觉效果）
            f'<div style="width: {circle_size}rem; height: {circle_size}rem; border-radius: 50%; '
            f"background-color: {theme['talent_circle_bg']}; "
            f"box-shadow: 0 0.09375rem 0.375rem {theme['talent_circle_shadow']}; "
            f"display: flex; align-items: center; justify-content: center; flex-shrink: 0; "
            f"font-size: 1rem; font-weight: 700; color: {theme['title_color']}; "
            f'z-index: 2; position: relative;">{level}</div>'
            f"</div>"
        )

        # 第一行不设上边距（标题已用 margin-bottom 控制间距），其余行上下等距
        top_margin = 0 if idx == 0 else row_gap
        rows.append(
            f'<div style="display: flex; align-items: stretch; width: 100%; margin: {top_margin}rem 0 {row_gap}rem;">'
            f"{_build_opt(left, left_bg)}"
            f"{mid_col}"
            f"{_build_opt(right, right_bg)}"
            "</div>"
        )

    return (
        '<div style="display: flex; flex-direction: column; width: 100%;">'
        + header
        + "".join(rows)
        + "</div>"
    )


async def build_html(
    hero_name_cn,
    pos_num,
    core_items,
    theme=THEME_DARK,
    start_items=None,
    lategame_inventories=None,
    win_rate=None,
    ability_build=None,
    talents=None,
):
    """构建完整的 HTML 页面，包含核心出装、加点、天赋、出门装与六格神装。

    Parameters
    ----------
    hero_name_cn : str  英雄中文名
    pos_num : str       位置编号，如 "1" / "2" / "3"
    core_items : list   核心出装物品列表
    theme : dict        颜色主题
    start_items : dict  出门装数据，含 items/count/win_rate
    lategame_inventories : list 六格神装数据列表
    win_rate : str      核心出装胜率，如 "50%"
    ability_build : dict | None 加点数据（含使用最多/胜率最高两套 build）
    talents : list | None 天赋数据
    """
    sorted_core = sorted(core_items, key=lambda x: _avg_time_minutes(x.get("avg_time")))
    core_rows = await build_items_rows(sorted_core, theme, show_time=True, show_core=True, max_per_row=6)

    title_text = f"{hero_name_cn} {pos_num} 号位"
    if win_rate:
        title_text += f"　{win_rate} 胜率"

    regions = []

    # 核心出装
    core_title = _section_title("最常见的出装路线", theme)
    regions.append(_section_block(core_title, core_rows))

    # 加点（技能升级顺序）
    if ability_build and (ability_build.get("most_used") or ability_build.get("best_wr")):
        regions.append(build_ability_build_html(ability_build, theme))

    # 天赋
    if talents:
        regions.append(build_talents_html(talents, theme))

    # 出门装 + 大后期：作为一组（内部用较小的间距，不参与区域大间隔）
    bottom = []
    if start_items:
        start_group = max(
            (s for s in start_items if s.get("items")),
            key=lambda x: x.get("count", 0),
            default=None,
        )
        if start_group:
            start_items_list = start_group.get("items", [])
            # 超过 6 个时，前 6 个一排，其余排到第二行
            if len(start_items_list) > 6:
                row1, row2 = start_items_list[:6], start_items_list[6:]
                start_rows = await build_items_rows(
                    row1, theme, show_time=False, show_core=False, wrap=False
                ) + await build_items_rows(
                    row2, theme, show_time=False, show_core=False, wrap=False
                )
            else:
                start_rows = await build_items_rows(
                    start_items_list, theme, show_time=False, show_core=False, wrap=False
                )
            start_title = _section_title("出门装", theme)
            bottom.append(_section_block(start_title, start_rows))

    # 六格神装（大后期）
    if lategame_inventories:
        for lg in lategame_inventories:
            if lg.get("time_key") != "55+":
                continue
            items = lg.get("items", [])
            if not items:
                continue
            period = lg.get("period", "")
            lg_rows = await build_items_rows(items, theme, show_time=False, show_core=False, wrap=False)
            lg_title = _section_title(period, theme)
            bottom.append(_section_block(lg_title, lg_rows))
    if bottom:
        regions.append(
            '<div style="display: flex; flex-direction: column; gap: 0.375rem;">'
            + "".join(bottom)
            + "</div>"
        )

    # 四大区域统一由外层 gap 容器分隔（不再用空白 spacer）
    sections_html = (
        '<div style="display: flex; flex-direction: column; gap: 0.75rem;">'
        + "".join(regions)
        + "</div>"
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: {theme["container_bg"]};
                font-family: {FONT_FAMILY};
                font-size: 19.2px;  /* 基准 16px × 1.8/1.5，使 rem 内容放大为原内容 1.8 倍 */
                line-height: 1.5; }}
</style>
</head>
<body>
<!-- 外层容器：flex flex-col gap-0 p-4 bg-d-gray-5 border border-solid border-d-gray-8 -->
<div id="container" style="display: flex; flex-direction: column; gap: 0;
     padding: 1.5rem;
     background-color: {theme["container_bg"]};
     border: 0.09375rem solid {theme["container_border"]};
     width: 28.125rem;">
  <!-- 标题：flex font-medium text-white/100；下边距与其他区块标题→内容间距统一 -->
  <div style="display: flex; font-weight: 500; font-size: 1.5rem;
       color: {theme["title_color"]}; margin-bottom: 0.375rem;">{title_text}</div>
{sections_html}
</div>
</body>
</html>"""
    return html


# ============================================================
# 渲染生成图片
# ============================================================
# 浏览器实例由 shared_browser 模块统一管理（跨脚本复用同一个 chromium）
# 保留以下别名以兼容旧调用方式。

BrowserSession = shared_browser.BrowserSession


async def get_browser():
    """兼容旧接口：获取共享浏览器实例。"""
    return await shared_browser.get_browser()


async def close_shared_browser():
    """兼容旧接口：关闭共享浏览器实例。"""
    await shared_browser.close_browser()


async def generate_image(
    hero_query="Anti-Mage",
    position=None,
    output_path=None,
    device_scale_factor=1,
    theme="light",
    supersample=2,
):
    """通过 Playwright 渲染 HTML 生成图片。

    自动使用共享浏览器实例，首次调用时启动浏览器，后续调用复用该实例。
    批量生成图片时性能更优（约 3 倍提速）。

    Parameters
    ----------
    hero_query : str
        英雄名（如 "Anti-Mage"）或英雄 ID。
    position : str | int | None
        位置编号，如 "1" / "2" / "3" / "4" / "5"。为 None 时使用英雄的 Most Played 位置。
    output_path : str | None
        输出图片路径。为空时自动生成。
    device_scale_factor : float
        设备缩放倍率，默认 1（不缩放，内容尺寸已通过 rem 放大 2 倍）。
    theme : str
        颜色风格，'dark'（暗色）或 'light'（亮色），默认 'light'。
    supersample : int
        超采样倍率，>1 时以 device_scale_factor×supersample 渲染再 Lanczos 降采样，
        提升边缘/文字锐度；设为 1 关闭超采样。默认 2。

    Examples
    --------
    单张生成：
        await generate_image('Anti-Mage', '1')

    批量生成（自动复用浏览器）：
        await generate_image('Anti-Mage', '1')
        await generate_image('Kez', '1')
        await generate_image('Sven', '1')

    手动关闭浏览器（可选）：
        await close_shared_browser()
    """
    await ensure_data_file()
    data = loadjson(DATA_FILE)
    if not data:
        print(f"错误: 无法读取数据文件 {DATA_FILE}", file=sys.stderr)
        return False

    hero_id, hero_data = find_hero(data, hero_query)
    if not hero_data:
        print(f"错误: 未找到英雄 {hero_query}", file=sys.stderr)
        return False

    hero_name = hero_data.get("n", hero_query)

    # 位置：传入 None 时使用 Most Played
    if position is None:
        position = hero_data.get("mp", "")
        if not position:
            print(f"错误: {hero_name} 没有 Most Played 数据", file=sys.stderr)
            return False
    else:
        # 传入数字（如 1 / "1"）时转成 "pos 1"
        pos_num = str(position).strip().lower().replace("pos", "").strip()
        position = f"pos {pos_num}"

    pos_data = hero_data.get(position)
    if not pos_data:
        print(f"错误: {hero_name} 没有 {position} 数据", file=sys.stderr)
        return False

    core_build_raw = pos_data.get("cb", [])
    if not core_build_raw:
        print(f"错误: {hero_name} 的 {position} core_build 为空", file=sys.stderr)
        return False
    core_build = convert_core_build(core_build_raw)

    # 出门装：解析并按优先级排序（build_html 中取场次最多的一组展示）
    start_items_raw = pos_data.get("si", [])
    start_items_data = []
    for item_ids, stats in start_items_raw:
        start_items_data.append(
            {
                "items": _resolve_item_icons(item_ids, get_start_item_priority),
                "count": stats.get("cnt", 0),
                "win_rate": stats.get("wr", 0),
            }
        )

    # 六格神装：按时间区间分组
    lategame_raw = pos_data.get("lg", {})
    lategame_data = None
    if lategame_raw:
        lategame_data = convert_lategame_inventories(lategame_raw)
        lategame_data.sort(key=lambda x: ["30+", "45+", "55+"].index(x.get("time_key", "99+")))

    # 加点（技能升级顺序）
    ability_build_raw = pos_data.get("ab", [])
    match_count = pos_data.get("mc", 0) or 0
    ability_build_data = await convert_ability_build(ability_build_raw, match_count)

    # 天赋
    talents_raw = pos_data.get("tl", [])
    talents_data = convert_talents(talents_raw) if talents_raw else None

    # 英雄中文名
    try:
        hero_name_cn = HEROES_LIST_CHINESE[int(hero_id)]
    except (KeyError, ValueError):
        hero_name_cn = hero_name

    # 位置编号（如 "1"）
    pos_num = position.replace("pos", "").strip()

    # 核心出装胜率
    core_win_rate = pos_data.get("wr")

    theme_dict = THEMES.get(theme, THEME_DARK)
    # build_html 内部含技能/物品图标缺失时的按需异步下载
    html = await build_html(
        hero_name_cn,
        pos_num,
        core_build,
        theme_dict,
        start_items=start_items_data,
        lategame_inventories=lategame_data,
        win_rate=core_win_rate,
        ability_build=ability_build_data,
        talents=talents_data,
    )

    if not output_path:
        safe_hero = hero_name.replace(" ", "-")
        safe_pos = position.replace(" ", "_")
        output_path = os.path.join(OUTPUT_DIR, f"{safe_hero}_{safe_pos}.png")

    # 图片缓存：缓存期内（默认 24 小时 / 1 天）复用已生成的图片，避免重复渲染
    if (
        os.path.exists(output_path)
        and time.time() - os.path.getmtime(output_path) <= IMAGE_CACHE_SECONDS
    ):
        print(f"图片缓存命中（{IMAGE_CACHE_SECONDS} 秒内）: {output_path}")
        return output_path

    # 使用共享浏览器渲染（自动管理，无需手动创建）
    # 超采样抗锯齿：以更高倍率渲染，再用 PIL Lanczos 降采样回目标尺寸，
    # 使细边框/文字边缘更锐利，同时保持最终输出大小不变（默认 600px）。
    render_scale = device_scale_factor * supersample
    # 复用共享 page 反复 set_content（而非每次 new_context/new_page），批量生成时更快。
    page = await shared_browser.get_page(render_scale)
    # 技能/物品图标均以 base64 data URL 内嵌，无需等待网络 idle；
    # 等 load 事件（含本地资源）即可，避免 networkidle 固定 500ms 空窗。
    await page.set_content(html, wait_until="load")
    await page.wait_for_timeout(50)
    container = page.locator("#container")
    png_bytes = await container.screenshot()

    # 降采样回目标尺寸
    if supersample > 1:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        w, h = img.size
        img = img.resize((w // supersample, h // supersample), Image.LANCZOS)
        img.save(output_path)
    else:
        with open(output_path, "wb") as f:
            f.write(png_bytes)

    print(f"已生成: {output_path}")
    print(f"  英雄: {hero_name_cn}({hero_name})  位置: {pos_num}号位  物品数: {len(core_build)}")
    return output_path


# ============================================================
# 主函数
# ============================================================
def main():
    """解析命令行参数并渲染生成核心出装图片。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="DOTA2 Pro Tracker 核心出装图片生成器（HTML 渲染版）"
    )
    parser.add_argument(
        "hero",
        nargs="?",
        default="Anti-Mage",
        help="英雄名（如 Anti-Mage）或英雄 ID，默认 Anti-Mage",
    )
    parser.add_argument(
        "position",
        nargs="?",
        default=None,
        help="位置编号 1-5（如 1 / 2 / 3），默认使用 Most Played 位置",
    )
    parser.add_argument(
        "-o", "--output", default=None, help="输出图片路径，默认 output/<Hero>_<pos>.png"
    )
    parser.add_argument(
        "-s", "--scale", type=float, default=1, help="设备缩放倍率，默认 1（不缩放）"
    )
    parser.add_argument(
        "-ss",
        "--supersample",
        type=int,
        default=2,
        help="超采样倍率，>1 渲染后降采样提升锐度，1 关闭；默认 2",
    )
    parser.add_argument(
        "-t",
        "--theme",
        choices=["dark", "light"],
        default="light",
        help="颜色风格：dark（暗色）/ light（亮色），默认 light",
    )
    args = parser.parse_args()

    if args.scale < 0.5:
        args.scale = 0.5

    async def _run_with_cleanup():
        try:
            return await generate_image(
                hero_query=args.hero,
                position=args.position,
                output_path=args.output,
                device_scale_factor=args.scale,
                theme=args.theme,
                supersample=args.supersample,
            )
        finally:
            await close_shared_browser()

    result = asyncio.run(_run_with_cleanup())
    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
