#!/usr/bin/env python3
"""
Dota 2 战报图片生成器（异步版）
根据比赛编号ID生成战报图片
基于 https://github.com/SonodaHanami/Steam_watcher 的战报生成模块分离
"""

import asyncio
import logging
import os
import re
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# 目录配置
# ============================================================
# 兼容两种运行方式：作为插件包被导入，或作为独立脚本直接运行。
# 所有目录 / URL / 超时等配置统一从 config.py 读取。
if __package__:
    from .. import config as _cfg
    from ..datasources.xiaoheihe import fetch_xiaoheihe_match
    from ..dota_dicts import GAME_MODE, LOBBY, REGION
    from ..utils import async_download_bytes, dumpjson, get_http_client, loadjson
else:
    import config as _cfg
    from dota_dicts import GAME_MODE, LOBBY, REGION
    from utils import async_download_bytes, dumpjson, get_http_client, loadjson

    # 独立脚本模式无插件包上下文，无法引入小黑盒数据源，跳过匿名数据补充
    fetch_xiaoheihe_match = None

WORK_DIR = str(_cfg.BASE_DIR)
IMAGES_DIR = str(_cfg.IMAGES_DIR)
MATCHES_DIR = str(_cfg.MATCHES_DIR)
OUTPUT_DIR = str(_cfg.OUTPUT_DIR)

for d in [WORK_DIR, IMAGES_DIR, MATCHES_DIR, OUTPUT_DIR]:
    os.makedirs(d, exist_ok=True)

# 图片按类别归档的子目录（hero / item）
for _d in ["hero", "item"]:
    os.makedirs(os.path.join(IMAGES_DIR, _d), exist_ok=True)


# ============================================================
# 日志
# ============================================================
def init_logger(name, level=logging.WARNING):
    """创建并返回一个带控制台输出格式的日志器。"""
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.setLevel(level)
    if logger.hasHandlers():
        logger.handlers.clear()
    sh = logging.StreamHandler()
    formatter = logging.Formatter(fmt="[%(asctime)s] %(message)s")
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    return logger


logger = init_logger("dota2_match_report")


def set_verbose():
    """将日志级别调整为 INFO，用于命令行调试输出。"""
    logger.setLevel(logging.INFO)


# ============================================================
# Pillow 版本兼容
# ============================================================
try:
    _RESAMPLE_LANCZOS = Image.LANCZOS
except AttributeError:
    _RESAMPLE_LANCZOS = Image.ANTIALIAS


def font_getsize(font, text):
    """返回文本在指定字体下的 (宽, 高)，兼容不同 Pillow 版本。"""
    if hasattr(font, "getsize"):
        return font.getsize(text)
    else:
        bbox = font.getbbox(text)
        width = bbox[2] - bbox[0]
        # getbbox 返回紧凑包围盒，不包含完整行高（ascender + descender），
        # 使用 getmetrics 获取字体完整行高，避免文字（尤其含下伸部分）被截断
        try:
            ascent, descent = font.getmetrics()
            height = ascent + descent
        except Exception:
            height = bbox[3] - bbox[1]
        return (width, height)


_FONT_CMAP_CACHE = {}
_FONTTOOLS_AVAILABLE = None


def _check_fonttools():
    global _FONTTOOLS_AVAILABLE
    if _FONTTOOLS_AVAILABLE is not None:
        return _FONTTOOLS_AVAILABLE
    try:
        import importlib.util

        _FONTTOOLS_AVAILABLE = importlib.util.find_spec("fontTools") is not None
    except ImportError:
        _FONTTOOLS_AVAILABLE = False
    if not _FONTTOOLS_AVAILABLE:
        logger.warning(
            "未安装 fontTools 库，特殊字符检测可能不准确。\n"
            "请执行以下命令安装: pip install fonttools"
        )
    return _FONTTOOLS_AVAILABLE


def _load_font_cmap(font_path):
    global _FONT_CMAP_CACHE
    if font_path in _FONT_CMAP_CACHE:
        return _FONT_CMAP_CACHE[font_path]
    if not _check_fonttools():
        _FONT_CMAP_CACHE[font_path] = None
        return None
    try:
        from fontTools.ttLib import TTFont, TTLibError

        chars = set()
        try:
            if font_path.lower().endswith(".ttc") or font_path.lower().endswith(".otc"):
                from fontTools.ttLib import TTCollection

                ttc = TTCollection(font_path)
                for font in ttc.fonts:
                    cmap = font.getBestCmap()
                    if cmap:
                        chars.update(cmap.keys())
                ttc.close()
            else:
                font = TTFont(font_path)
                cmap = font.getBestCmap()
                if cmap:
                    chars = set(cmap.keys())
                font.close()
        except TTLibError:
            chars = None
    except Exception:
        chars = None
    _FONT_CMAP_CACHE[font_path] = chars
    return chars


def has_glyph(font_path, char):
    """判断单个字符是否在字体字形表中；无法解析时视为存在。"""
    cmap = _load_font_cmap(font_path)
    if cmap is None:
        return True
    return ord(char) in cmap


def _cluster_has_all_glyphs(cluster, font_path):
    for ch in cluster:
        if not has_glyph(font_path, ch):
            return False
    return True


def _split_into_clusters(text):
    """将文本按「基字符 + 组合附加符号」切分为字素簇。"""
    clusters = []
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and 0x0300 <= ord(text[j]) <= 0x036F:
            j += 1
        clusters.append(text[i:j])
        i = j
    return clusters


def _pick_font_for_cluster(cluster, font_paths, preferred_font=None):
    primary_fp = font_paths[0] if font_paths else None
    if primary_fp and _cluster_has_all_glyphs(cluster, primary_fp):
        return primary_fp
    if (
        preferred_font
        and preferred_font != primary_fp
        and _cluster_has_all_glyphs(cluster, preferred_font)
    ):
        return preferred_font
    for fp in font_paths:
        if _cluster_has_all_glyphs(cluster, fp):
            return fp
    return font_paths[0] if font_paths else None


def segment_text_by_fonts(text, font_paths):
    """按字形覆盖情况将文本切分为多段，每段对应一个可用字体。"""
    if not font_paths:
        return [(text, None)]
    clusters = _split_into_clusters(text)
    segments = []
    current_font = None
    for cluster in clusters:
        fp = _pick_font_for_cluster(cluster, font_paths, current_font)
        if segments and segments[-1][1] == fp:
            segments[-1] = (segments[-1][0] + cluster, fp)
        else:
            segments.append((cluster, fp))
        current_font = fp
    return segments


def font_getsize_with_fallback(text, font_paths, font_size):
    """在字体回退分段的场景下，计算文本整体的渲染宽高。"""
    total_width = 0
    total_height = 0
    segments = segment_text_by_fonts(text, font_paths)
    font_cache = {}
    primary_fp = font_paths[0] if font_paths else None
    if primary_fp and primary_fp not in font_cache:
        font_cache[primary_fp] = ImageFont.truetype(primary_fp, font_size)
        primary_ascent = _get_font_baseline(font_cache[primary_fp])
    else:
        primary_ascent = font_size
    for segment, fp in segments:
        if fp not in font_cache:
            font_cache[fp] = ImageFont.truetype(fp, font_size)
        font = font_cache[fp]
        if fp != primary_fp:
            font_ascent = _get_font_baseline(font)
            if font_ascent > 0:
                scale_factor = primary_ascent / font_ascent
                scaled_font = ImageFont.truetype(fp, int(font_size * scale_factor))
                w, h = font_getsize(scaled_font, segment)
            else:
                w, h = font_getsize(font, segment)
        else:
            w, h = font_getsize(font, segment)
        total_width += w
        total_height = max(total_height, h)
    return (total_width, total_height)


def _get_font_baseline(font):
    try:
        if hasattr(font, "getmetrics"):
            ascent, descent = font.getmetrics()
            return ascent
        if hasattr(font, "getbbox"):
            bbox = font.getbbox("Ag")
            if bbox:
                return -bbox[1]
    except Exception:
        pass
    if hasattr(font, "size"):
        return font.size
    return 12


def draw_text_with_fallback(draw, pos, text, font_paths, font_size, fill):
    """在指定位置按字体回退分段绘制文本，返回绘制的总宽度。"""
    x, y = pos
    segments = segment_text_by_fonts(text, font_paths)
    if not segments:
        return 0
    font_cache = {}
    primary_fp = font_paths[0] if font_paths else None
    if primary_fp:
        if primary_fp not in font_cache:
            font_cache[primary_fp] = ImageFont.truetype(primary_fp, font_size)
        primary_baseline = _get_font_baseline(font_cache[primary_fp])
    else:
        primary_baseline = font_size
    for segment, fp in segments:
        if fp not in font_cache:
            font_cache[fp] = ImageFont.truetype(fp, font_size)
        font = font_cache[fp]
        if fp != primary_fp:
            font_baseline = _get_font_baseline(font)
            if font_baseline > 0:
                scale_factor = primary_baseline / font_baseline
                scaled_font = ImageFont.truetype(fp, int(font_size * scale_factor))
                scaled_baseline = _get_font_baseline(scaled_font)
                y_offset = primary_baseline - scaled_baseline
                draw.text((x, y + y_offset), segment, font=scaled_font, fill=fill)
                w, _ = font_getsize(scaled_font, segment)
            else:
                y_offset = primary_baseline - font_baseline
                draw.text((x, y + y_offset), segment, font=font, fill=fill)
                w, _ = font_getsize(font, segment)
        else:
            draw.text((x, y), segment, font=font, fill=fill)
            w, _ = font_getsize(font, segment)
        x += w
    return x - pos[0]


def draw_text_stroke(draw, pos, text, font, fill, stroke_fill, stroke_width=1):
    """先绘制八方向描边再绘制前景文字，实现字体描边效果。"""
    x, y = pos
    for dx in range(-stroke_width, stroke_width + 1):
        for dy in range(-stroke_width, stroke_width + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=stroke_fill)
    draw.text((x, y), text, font=font, fill=fill)


# ============================================================
# 职业选手认证徽章（OpenDota iconConfirmed 图标，金色圆 + 白色对勾）
# ============================================================
PRO_BADGE_URL = f"{_cfg.D2PT_REPO_BASE}/images/pro_badge.png"


async def _ensure_pro_badge() -> bool:
    """确保职业认证徽章 PNG 存在，只需下载一次。

    查找顺序：运行缓存（images/other/pro_badge.png）→ 上游仓库网络下载
    （d2pt_bot 仓库 images/pro_badge.png，经 gh-proxy 加速）。
    全部失败时返回 False，战报降级为无徽章。
    """
    badge_path = image_path("pro_badge.png")
    if os.path.exists(badge_path) and os.path.getsize(badge_path) > 100:
        return True
    try:
        data = await async_download_bytes(PRO_BADGE_URL, timeout=_cfg.config.d2w_download_timeout)
        if not data or len(data) <= 100:
            logger.warning(f"职业认证徽章下载内容异常: {len(data or b'')} bytes")
            return False
        with open(badge_path, "wb") as f:
            f.write(data)
        logger.info(f"职业认证徽章已下载: {badge_path}")
        return True
    except Exception as e:
        logger.warning(f"职业认证徽章下载失败（战报降级为无徽章）: {e}")
    return False


# ============================================================
# 图片/字体 URL
# ============================================================
HERO_IMAGE_URL = _cfg.HERO_IMAGE_URL
ITEM_IMAGE_URL = _cfg.ITEM_IMAGE_URL
OTHER_IMAGE_URL = _cfg.OTHER_IMAGE_URL

OPENDOTA_MATCHES = _cfg.OPENDOTA_MATCH_URL
OPENDOTA_REQUEST = _cfg.OPENDOTA_REQUEST_URL
OPENDOTA_LOGS = _cfg.OPENDOTA_LOGS_URL
OPENDOTA_HEROES = _cfg.OPENDOTA_HEROES_URL
OPENDOTA_ITEMS = _cfg.OPENDOTA_ITEMS_URL

HEROES_CACHE = os.path.join(_cfg.DATA_DIR, "heroes.json")
ITEMS_CACHE = os.path.join(_cfg.DATA_DIR, "items.json")

# ============================================================
# Dota 2 数据字典（由 refresh_dicts 从 OpenDota 获取并缓存到本地）
# ============================================================
HEROES = {}
ITEMS = {}


def _load_heroes_cache():
    """从本地缓存加载英雄 ID → 名称字典，成功返回 True。"""
    global HEROES
    cached = loadjson(HEROES_CACHE)
    if cached:
        HEROES = {int(k): v for k, v in cached.items()}
        return True
    return False


def _load_items_cache():
    """从本地缓存加载物品 ID → 名称字典，成功返回 True。"""
    global ITEMS
    cached = loadjson(ITEMS_CACHE)
    if cached:
        ITEMS = {int(k): v for k, v in cached.items()}
        return True
    return False


async def _fetch_heroes():
    """从 OpenDota 拉取英雄字典并更新内存与缓存。"""
    global HEROES
    try:
        client = await get_http_client()
        resp = await client.get(OPENDOTA_HEROES, timeout=_cfg.config.d2w_download_timeout)
        raw = resp.json()
        new_heroes = {}
        for h in raw.values():
            hid = h.get("id")
            name = h.get("name", "")
            if not hid or not name:
                continue
            new_heroes[int(hid)] = name.replace("npc_dota_hero_", "")
        if new_heroes:
            HEROES = new_heroes
            dumpjson({str(k): v for k, v in new_heroes.items()}, HEROES_CACHE)
            logger.info(f"英雄字典已更新，共 {len(new_heroes)} 个，已缓存到 {HEROES_CACHE}")
        else:
            logger.warning(f"英雄字典解析为空（raw={len(raw)} 项），跳过保存")
    except Exception as e:
        logger.warning(f"更新英雄字典失败（{type(e).__name__}），使用缓存数据")
        _load_heroes_cache()


async def _fetch_items():
    """从 OpenDota 拉取物品字典并更新内存与缓存。"""
    global ITEMS
    try:
        client = await get_http_client()
        resp = await client.get(OPENDOTA_ITEMS, timeout=_cfg.config.d2w_download_timeout)
        raw = resp.json()
        new_items = {}
        for key, it in raw.items():
            iid = it.get("id")
            if not iid:
                continue
            name = key.replace("item_", "")
            new_items[int(iid)] = name
        if new_items:
            ITEMS = new_items
            dumpjson({str(k): v for k, v in new_items.items()}, ITEMS_CACHE)
            logger.info(f"物品字典已更新，共 {len(new_items)} 个，已缓存到 {ITEMS_CACHE}")
        else:
            logger.warning(f"物品字典解析为空（raw={len(raw)} 项），跳过保存")
    except Exception as e:
        logger.warning(f"更新物品字典失败（{type(e).__name__}），使用缓存数据")
        _load_items_cache()


async def refresh_dicts(match=None):
    """按需加载字典：优先本地缓存；仅当比赛数据中出现缓存无法映射的英雄/物品时才请求 OpenDota 更新"""
    need_heroes = not _load_heroes_cache()
    need_items = not _load_items_cache()
    if match and not (need_heroes and need_items):
        for p in match.get("players", []):
            if not need_heroes and p.get("hero_id") and p["hero_id"] not in HEROES:
                logger.info(f"英雄 {p['hero_id']} 不在本地缓存中，需更新英雄字典")
                need_heroes = True
            if not need_items:
                for key in ITEM_SLOTS + BACKPACK_SLOTS:
                    iid = p.get(key, 0)
                    if iid and iid not in ITEMS:
                        logger.info(f"物品 {iid} 不在本地缓存中，需更新物品字典")
                        need_items = True
            if need_heroes and need_items:
                break
    tasks = []
    if need_heroes:
        tasks.append(_fetch_heroes())
    if need_items:
        tasks.append(_fetch_items())
    if tasks:
        await asyncio.gather(*tasks)


# 游戏模式 / 房间类型 / 服务器名称等静态字典统一来自 dota_dicts（单一数据源），
# 与播报文本（match_builder）、D2PT 等模块保持一致，避免多处维护造成漂移。
SKILL_LEVEL = {1: "Normal", 2: "High", 3: "Very High"}

RANK_NAMES = {
    1: "先锋",
    2: "卫士",
    3: "中军",
    4: "统帅",
    5: "传奇",
    6: "万古流芳",
    7: "超凡入圣",
    8: "冠绝一世",
}

RANK_MMR = {
    1: 0,
    2: 770,
    3: 1540,
    4: 2310,
    5: 3800,
    6: 3850,
    7: 4620,
    8: 5620,
}


def estimate_mmr_from_rank(rank_tier):
    """根据天梯段位编码（段位*10+星级）估算 MMR 分数。"""
    tier = rank_tier // 10
    star = rank_tier % 10
    tier = max(1, min(8, tier))
    star_mmr = 154
    if tier == 8:
        return 6000
    elif tier == 7:
        star_mmr = 200
    return int(RANK_MMR[tier] + (star - 1) * star_mmr)


def mmr_to_rank_name(mmr):
    """将 MMR 分数映射为对应的天梯段位名称。"""
    if mmr >= RANK_MMR[8]:
        return "冠绝一世"
    elif mmr >= RANK_MMR[7]:
        return "超凡入圣"
    elif mmr >= RANK_MMR[6]:
        return "万古流芳"
    elif mmr >= RANK_MMR[5]:
        return "传奇"
    elif mmr >= RANK_MMR[4]:
        return "统帅"
    elif mmr >= RANK_MMR[3]:
        return "中军"
    elif mmr >= RANK_MMR[2]:
        return "卫士"
    else:
        return "先锋"


def avg_skill(match):
    """估算比赛平均段位，优先基于玩家段位，其次回退到比赛 skill 字段。"""
    players = match.get("players", [])
    estimated_mmrs = []
    for p in players:
        if p.get("rank_tier"):
            estimated_mmrs.append(estimate_mmr_from_rank(p["rank_tier"]))
    if estimated_mmrs:
        avg = sum(estimated_mmrs) / len(estimated_mmrs)
        return mmr_to_rank_name(avg)
    if match.get("skill"):
        return SKILL_LEVEL.get(match["skill"], "Unknown")
    return "未知"


PLAYER_RANK = {
    0: "未知",
    1: "先锋",
    2: "卫士",
    3: "中军",
    4: "统帅",
    5: "传奇",
    6: "万古",
    7: "超凡",
    8: "冠绝",
}

SLOT = ["Radiant", "Dire"]
SLOT_CHINESE = ["天辉", "夜魇"]
ITEM_SLOTS = ["item_0", "item_1", "item_2", "item_3", "item_4", "item_5", "item_neutral"]
BACKPACK_SLOTS = ["backpack_0", "backpack_1", "backpack_2"]

OTHER_IMAGES = [
    "hero_None",
    "item_None",
    "logo_dire",
    "logo_radiant",
    "rank_icon_0",
    "rank_icon_1",
    "rank_icon_2",
    "rank_icon_3",
    "rank_icon_4",
    "rank_icon_5",
    "rank_icon_6",
    "rank_icon_7",
    "rank_icon_8",
    "rank_star_1",
    "rank_star_2",
    "rank_star_3",
    "rank_star_4",
    "rank_star_5",
    "scepter_0",
    "scepter_1",
    "shard_0",
    "shard_1",
]


# ============================================================
# 异步 HTTP 会话（全局复用 utils 的 httpx 客户端）
# ============================================================
async def get_session():
    """获取并复用全局复用的 httpx 异步客户端。"""
    return await get_http_client()


async def close_session():
    """关闭全局复用的 httpx 异步客户端。"""
    client = await get_http_client()
    await client.aclose()


# ============================================================
# 初始化：字体
# ============================================================
def _check_font_usable(font_path):
    """校验字体文件是否存在且能被 Pillow 正常加载。"""
    if not os.path.exists(font_path):
        return False
    try:
        ImageFont.truetype(font_path, 12)
        return True
    except Exception:
        return False


async def init_fonts():
    """扫描系统字体候选，返回所有可用字体的路径列表（首个为主字体）。"""
    logger.info("初始化字体")

    font_candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/malgunbd.ttf",
        "C:/Windows/Fonts/malgun.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/msgothic.ttc",
        "C:/Windows/Fonts/Nirmala.ttf",
        "C:/Windows/Fonts/LeelawUI.ttf",
        "C:/Windows/Fonts/seguisym.ttf",
        "C:/Windows/Fonts/seguiemj.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]

    all_font_paths = []
    primary_path = None

    for fp in font_candidates:
        if _check_font_usable(fp):
            all_font_paths.append(fp)
            if primary_path is None:
                primary_path = fp
                logger.info(f"使用主字体: {fp}")

    if not primary_path:
        logger.error("未找到可用字体，战报图片可能无法正常显示中文")
    elif len(all_font_paths) < 3:
        logger.warning(f"可用字体较少 ({len(all_font_paths)} 个)，部分特殊字符可能无法显示")

    logger.info(f"可用字体列表: {len(all_font_paths)} 个")
    for i, fp in enumerate(all_font_paths):
        logger.info(f"  [{i}] {os.path.basename(fp)}")

    return all_font_paths


# ============================================================
# 图片路径：按类别归档（hero_* → hero/，item_* → item/，其余 → 根目录）
# ============================================================
def image_path(img_name):
    """返回图片在本地的完整路径，按文件名前缀自动归入对应子目录。"""
    if img_name.startswith("hero_"):
        subdir = "hero"
    elif img_name.startswith("item_"):
        subdir = "item"
    else:
        subdir = ""
    return os.path.join(IMAGES_DIR, subdir, img_name)


# ============================================================
# 初始化：图片（异步并发下载）
# ============================================================
async def init_images():
    """并发下载缺失或损坏的英雄/物品/通用图片到本地归档目录。"""
    logger.info("初始化图片资源")
    images = []
    for hero in HEROES.values():
        images.append(("hero", f"hero_{hero}.png", HERO_IMAGE_URL.format(name=hero)))
    for item in ITEMS.values():
        if item.startswith("recipe"):
            img_path = "item_recipe.png"
            img_url = ITEM_IMAGE_URL.format(name="recipe")
        else:
            img_path = f"item_{item}.png"
            img_url = ITEM_IMAGE_URL.format(name=item)
        images.append(("item", img_path, img_url))
    for img in OTHER_IMAGES:
        images.append(("other", f"{img}.png", OTHER_IMAGE_URL.format(img)))

    need_download = []
    successful = 0
    seen = set()
    for img_type, img_name, img_url in images:
        if img_name in seen:
            continue
        seen.add(img_name)
        img_path = image_path(img_name)
        file_ok = False
        try:
            if os.path.getsize(img_path) > 100:
                with Image.open(img_path) as img:
                    img.verify()
                file_ok = True
        except Exception:
            pass
        if file_ok:
            successful += 1
            continue
        if os.path.exists(img_path):
            os.remove(img_path)
        need_download.append((img_name, img_url, img_path))

    downloaded = 0
    failed = 0

    async def download_one(name, url, path):
        nonlocal downloaded, failed
        try:
            body = await async_download_bytes(url, timeout=_cfg.config.d2w_download_timeout)
            with open(path, "wb") as f:
                f.write(body)
            downloaded += 1
        except Exception:
            failed += 1

    if need_download:
        sem = asyncio.Semaphore(20)

        async def limited_download(name, url, path):
            async with sem:
                await download_one(name, url, path)

        tasks = [limited_download(n, u, p) for n, u, p in need_download]
        await asyncio.gather(*tasks)

    done = downloaded + successful + failed  # noqa: F841
    logger.info(f"图片初始化完成：本地读取{successful}，重新下载{downloaded}，失败{failed}")


# ============================================================
# 获取图片
# ============================================================
def get_image(img_name):
    """按名称加载本地图片，失败时返回纯色占位图。"""
    try:
        return Image.open(image_path(img_name))
    except Exception as e:
        logger.warning(f"读取图片失败 {img_name}: {e}")
        return Image.new("RGBA", (30, 30), (255, 160, 160))


# ============================================================
# 获取比赛数据（异步）
# ============================================================
async def _fetch_match_json(session, match_id, retries=3):
    """从 OpenDota 获取比赛 JSON；对网络错误 / 5xx / 429 自动重试。

    OpenDota 经 Cloudflare 返回 522 等临时错误时响应体是 HTML 页面，
    直接 resp.json() 会因解析失败抛异常，这里先判断状态码再解析。
    """
    for attempt in range(1, retries + 1):
        try:
            resp = await session.get(OPENDOTA_MATCHES.format(match_id=match_id))
            if resp.status_code == 200:
                return resp.json()
            err = f"HTTP {resp.status_code}"
            if resp.status_code < 500 and resp.status_code != 429:
                # 4xx（限流除外）重试无意义
                logger.warning(f"获取比赛数据失败: {err}")
                return None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        logger.warning(f"获取比赛数据失败: {err}（第 {attempt}/{retries} 次）")
        if attempt < retries:
            await asyncio.sleep(5 * attempt)
    return None


async def get_match(match_id, wait=True, timeout=None, force=False, match_data=None):
    """获取比赛数据：优先使用调用方已拉取的数据，其次本地缓存，必要时从 OpenDota 拉取。

    match_data 由调用方（如轮询新比赛阶段二）传入已获取的比赛详情，
    可避免生成战报图片时对 OpenDota 主源场次重复请求；为 None 时保持原有流程。
    """
    match_file = os.path.join(MATCHES_DIR, f"{match_id}.json")

    if match_data is not None:
        if not match_data.get("players"):
            return None
        if match_data.get("game_mode") in (15, 19):
            logger.info("活动模式，跳过分析")
            return None
        if match_data["players"][0].get("damage_inflictor_received", None) is None:
            # 分析不完整：与下方 force 分支一致，标记后按简化版处理
            match_data["from_valve"] = True
        dumpjson(match_data, match_file)
        logger.info(f"比赛 {match_id} 使用调用方传入的数据")
        return match_data

    if os.path.exists(match_file):
        match = loadjson(match_file)
        received = match["players"][0].get("damage_inflictor_received", None)
        if received is not None or force:
            logger.info(f"比赛 {match_id} 使用本地缓存数据")
            return match
        logger.info("本地缓存数据不完整，重新获取...")

    logger.info(f"正在从 OpenDota 获取比赛 {match_id} 数据...")
    session = await get_session()

    match = await _fetch_match_json(session, match_id)
    if match is None:
        return None

    if not match.get("players"):
        if match.get("error"):
            logger.warning(f"OpenDota 返回错误: {match['error']}")
        return None

    if match.get("game_mode") in (15, 19):
        logger.info("活动模式，跳过分析")
        return None

    received = match["players"][0].get("damage_inflictor_received", None)

    if received is not None:
        dumpjson(match, match_file)
        logger.info(f"比赛 {match_id} 数据已保存")
        return match

    if force:
        match["from_valve"] = True
        dumpjson(match, match_file)
        logger.info(f"比赛 {match_id} 数据（简化版）已保存")
        return match

    if not wait:
        logger.info(
            "比赛分析结果不完整，使用 --wait 参数等待分析完成，或使用 --force 生成简化版战报"
        )
        return None

    # 请求分析并通过 SSE 流监控状态
    logger.info("比赛分析结果不完整，正在请求 OpenDota 分析...")
    try:
        resp = await session.post(OPENDOTA_REQUEST.format(match_id=match_id))
        j = resp.json()
        job_id = j.get("job", {}).get("jobId")
        logger.info(f"已请求分析，job_id={job_id}")
    except Exception as e:
        logger.warning(f"请求分析失败: {e}")
        return None

    if job_id is None:
        logger.warning("未获取到 job_id")
        return None

    # 通过 SSE 流监控解析进度（单次，不重试）
    try:
        async with session.stream(
            "GET",
            OPENDOTA_LOGS.format(job_id=job_id),
            timeout=httpx.Timeout(300, read=5),
        ) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                msg = line[5:].strip()
                # 去除 ANSI 颜色码
                clean = re.sub(r"\x1b\[\d+m", "", msg)
                logger.info(f"[SSE] {clean}")
                if "Replay not found" in clean or "[fail]" in clean:
                    logger.warning(f"解析失败: {clean}")
                    return False
                if "[success]" in clean:
                    logger.info("解析成功，获取比赛数据...")
                    break
    except Exception as e:
        logger.warning(f"SSE 连接异常: {e}")
        return None

    # 解析成功后重新获取比赛数据
    match = await _fetch_match_json(session, match_id, retries=1)
    if (
        match
        and match.get("players")
        and match["players"][0].get("damage_inflictor_received") is not None
    ):
        dumpjson(match, match_file)
        logger.info(f"比赛 {match_id} 数据已保存")
        return match

    logger.warning(f"比赛 {match_id} 分析结果仍不完整")
    return None


# ============================================================
# 匿名玩家数据补充（小黑盒）
# ============================================================
PRO_PLAYERS_CACHE = os.path.join(_cfg.DATA_DIR, "pro_players.json")
PRO_PLAYERS_TTL = 7 * 86400  # 职业注册表更新缓慢，本地缓存 7 天
_pro_registry = None  # 进程内缓存：account_id -> 职业名（空串表示确认职业但注册表无名）


async def _load_pro_registry() -> dict:
    """加载 OpenDota 职业选手注册表（account_id -> 职业名）。

    本地缓存 7 天；过期或缺失时从 /api/proPlayers 拉取；拉取失败时退回
    过期缓存。完全无数据返回空表（补充流程按非职业处理，不影响渲染）。
    """
    global _pro_registry
    if _pro_registry is not None:
        return _pro_registry
    cached = loadjson(PRO_PLAYERS_CACHE) or {}
    if time.time() - cached.get("fetched_at", 0) < PRO_PLAYERS_TTL:
        _pro_registry = cached.get("players") or {}
        return _pro_registry
    try:
        client = await get_http_client()
        resp = await client.get(
            f"{_cfg.OPENDOTA_BASE}/api/proPlayers", timeout=_cfg.config.d2w_download_timeout
        )
        players = {}
        for pro in resp.json():
            aid = pro.get("account_id")
            if aid:
                players[int(aid)] = pro.get("name") or ""
        if players:
            _pro_registry = players
            dumpjson({"fetched_at": time.time(), "players": players}, PRO_PLAYERS_CACHE)
            logger.info(f"职业选手注册表已更新，共 {len(players)} 人，已缓存到 {PRO_PLAYERS_CACHE}")
            return _pro_registry
        logger.warning("职业选手注册表解析为空，跳过保存")
    except Exception as e:
        logger.warning(f"职业选手注册表拉取失败（{type(e).__name__}），使用缓存数据")
    _pro_registry = cached.get("players") or {}
    return _pro_registry


async def _supplement_anonymous(match_id, match):
    """用小黑盒数据补充 OpenDota 匿名玩家（无任何名字）的昵称与段位。

    OpenDota 对隐私资料玩家拿不到昵称，战报中只能显示「匿名玩家」；
    小黑盒对同一比赛的数据覆盖更全，可为这些玩家补回昵称（personaname），
    缺段位（rank_tier）时一并补充。按 player_slot 匹配（与 Valve 槽位一致，
    最可靠），account_id 兜底。补充成功后回写比赛缓存，后续渲染不再重复请求。

    职业认证：隐私保护会同时隐藏 account_id，OpenDota 无法关联职业注册表，
    职业选手也会显示成「匿名玩家」（无认证徽章）；小黑盒补回 account_id 后
    与 OpenDota 职业注册表比对，命中即写入职业名 name（渲染时触发认证徽章，
    与 OpenDota 对职业选手的展示一致）。普通玩家的昵称不写入 name，避免误加
    徽章。
    """
    players = match.get("players") or []
    anonymous = [p for p in players if not (p.get("name") or p.get("personaname"))]
    if not anonymous:
        return
    if match.get("data_source") == "xiaoheihe":
        return  # 数据本身来自小黑盒，无更全的补充来源
    if fetch_xiaoheihe_match is None:
        return
    try:
        xh = await fetch_xiaoheihe_match(match_id)
    except Exception as e:
        logger.warning(f"小黑盒匿名玩家数据补充失败（按原数据渲染）: {e}")
        return
    by_slot = {xp.get("player_slot"): xp for xp in xh.get("players") or []}
    by_account = {
        xp.get("account_id"): xp for xp in xh.get("players") or [] if xp.get("account_id")
    }
    pro_names = await _load_pro_registry()
    changed = False
    for p in anonymous:
        xp = by_slot.get(p.get("player_slot")) or by_account.get(p.get("account_id"))
        if not xp:
            continue
        xp_aid = xp.get("account_id")
        if xp_aid and xp_aid in pro_names:
            # 职业选手：注册表有名则写职业名（触发徽章），无名则仅标记
            pro_name = pro_names[xp_aid]
            if pro_name:
                p["name"] = pro_name
            else:
                p["is_pro"] = True
            changed = True
        if xp.get("personaname") and not p.get("personaname"):
            p["personaname"] = xp["personaname"]
            changed = True
        if xp.get("rank_tier") and not p.get("rank_tier"):
            p["rank_tier"] = xp["rank_tier"]
            changed = True
    if changed:
        try:
            dumpjson(match, os.path.join(MATCHES_DIR, f"{match_id}.json"))
        except Exception as e:
            logger.warning(f"补充匿名玩家数据后回写比赛缓存失败: {e}")
        logger.info(f"比赛 {match_id} 已从小黑盒补充 {len(anonymous)} 名匿名玩家数据")


# ============================================================
# 玩家数据初始化
# ============================================================
def init_player(player):
    """为玩家数据缺失的字段填充默认值，避免后续计算报错。"""
    if not player.get("net_worth"):
        player["net_worth"] = player.get("total_gold") or 0
    if not player.get("total_xp"):
        player["total_xp"] = 0
    if not player.get("hero_damage"):
        player["hero_damage"] = 0
    if not player.get("damage_inflictor_received"):
        player["damage_inflictor_received"] = {}
    if not player.get("tower_damage"):
        player["tower_damage"] = 0
    if not player.get("hero_healing"):
        player["hero_healing"] = 0
    if not player.get("stuns"):
        player["stuns"] = 0
    if not player.get("purchase_log"):
        player["purchase_log"] = []
    if not player.get("lane_role"):
        player["lane_role"] = 0
    if not player.get("permanent_buffs"):
        player["permanent_buffs"] = {}
    if not player.get("has_bkb"):
        player["has_bkb"] = False


# ============================================================
# 根据slot判断队伍，0=天辉，1=夜魇
# ============================================================
def get_team_by_slot(slot):
    """根据玩家槽位判断阵营，返回 0（天辉）或 1（夜魇）。"""
    return slot // 100


# ============================================================
# 战报图片生成（异步）
# ============================================================
async def generate_match_image(
    match_id,
    output_path=None,
    wait=True,
    timeout=120,
    force=False,
    scale=1.4,
    match_data=None,
    supplement_anonymous=True,
):
    """根据比赛编号生成战报图片，返回图片路径或 False 表示失败。

    supplement_anonymous 为 False 时不接入小黑盒补充匿名玩家数据
    （定时任务播报路径使用，避免额外请求拖慢播报）。
    """
    t0 = time.time()

    # 缓存机制：已生成过的完整战报直接返回
    if not force:
        cached_complete = os.path.join(OUTPUT_DIR, f"{match_id}.png")
        if os.path.exists(cached_complete):
            logger.info(f"检测到已生成的完整战报缓存，直接返回: {cached_complete}")
            return cached_complete

    match = await get_match(
        match_id, wait=wait, timeout=timeout, force=force, match_data=match_data
    )
    if match is False:
        logger.error("解析失败（Replay not found），无法生成战报")
        return False
    if not match:
        logger.error("无法获取比赛数据")
        return False

    if supplement_anonymous:
        await _supplement_anonymous(match_id, match)
    await refresh_dicts(match)

    font_paths = await init_fonts()
    if not font_paths:
        logger.error("字体初始化失败")
        return False

    await init_images()
    await _ensure_pro_badge()

    # 同步的 PIL 绘制 + 保存是 CPU/IO 密集操作，放到线程执行，避免阻塞事件循环。
    return await asyncio.to_thread(
        _render_match_image, match, font_paths, scale, output_path, match_id, t0
    )


def _render_match_image(match, font_paths, scale, output_path, match_id, t0):
    # 职业认证徽章（透明 PNG，_ensure_pro_badge 预生成；缺失时降级为无徽章）
    badge_img = None
    badge_file = image_path("pro_badge.png")
    if os.path.exists(badge_file):
        try:
            badge_img = Image.open(badge_file).convert("RGBA")
        except Exception:
            badge_img = None

    def _s(v):
        return int(v * scale)

    def _sf(v):
        # 向下取整到一位小数，避免整数字号导致微软雅黑字形被截断
        return int(v * scale * 10) / 10

    BASE_W, BASE_H = 700, 900
    W, H = _s(BASE_W), _s(BASE_H)

    image = Image.new("RGB", (W, H), (255, 255, 255))
    font_size = _sf(12)  # 11*1.4≈15.4px，避开 15.5~16.4px（微软雅黑丢失"济"顶部小笔画的区间）
    font = ImageFont.truetype(font_paths[0], font_size)
    font2 = ImageFont.truetype(font_paths[0], _sf(18))
    draw = ImageDraw.Draw(image)

    RADIANT_GREEN = (60, 144, 40)
    DIRE_RED = (156, 54, 40)
    total_tower_hp = (
        4500 + 2600 * 2 + 1000 * 7 + 2200 * 3 + 1300 * 3 + 2500 * 3 + 2500 * 3 + 1800 * 3
    )

    draw.rectangle((0, 0, _s(700), _s(70)), "black")
    title = "" + str(match["match_id"])
    draw.text((_s(20), _s(15)), title, font=font2, fill=(255, 255, 255))

    for label, x in [
        ("开始时间", 150),
        ("持续时间", 300),
        ("Level", 380),
        ("地区", 460),
        ("比赛模式", 550),
    ]:
        draw.text((_s(x), _s(20)), label, font=font, fill=(255, 255, 255))

    start_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(match["start_time"]))
    duration = "{}分{}秒".format(match["duration"] // 60, match["duration"] % 60)
    skill = avg_skill(match)
    region_val = None
    for p in match.get("players", []):
        if p.get("region") is not None:
            region_val = p["region"]
            break
    if region_val is None:
        region_val = match.get("region")
    region_id = f"region_{region_val}"
    # 小黑盒数据源已提供原始服务器文本（如"东南亚"），直接展示，无需查 REGION 表转换
    region = match.get("server") or (REGION[region_id] if region_id in REGION else "中国")
    mode_id = match.get("game_mode")
    lobby_id = match.get("lobby_type")
    mode_desc = match.get("mode_desc")
    if mode_desc:
        # 小黑盒数据源已提供原始模式文本（如"加速模式"），直接展示，无需查 GAME_MODE 表转换
        mode_text = mode_desc
    else:
        mode = GAME_MODE[mode_id] if mode_id in GAME_MODE else "未知"
        lobby = LOBBY[lobby_id] if lobby_id in LOBBY else "未知"
        mode_text = f"{mode}/{lobby}"

    draw.text((_s(150), _s(40)), start_time, font=font, fill=(255, 255, 255))
    draw.text((_s(300), _s(40)), duration, font=font, fill=(255, 255, 255))
    draw.text((_s(380), _s(40)), skill, font=font, fill=(255, 255, 255))
    draw.text((_s(460), _s(40)), region, font=font, fill=(255, 255, 255))
    draw.text((_s(550), _s(40)), mode_text, font=font, fill=(255, 255, 255))

    if match.get("from_valve"):
        draw.text((_s(20), _s(40)), "※分析结果不完整", font=font, fill=(255, 180, 0))
    else:
        draw.text((_s(20), _s(40)), "※录像分析成功", font=font, fill=(123, 163, 52))

    winner = 1 - int(match["radiant_win"])
    draw.text(
        (_s(314), _s(81)),
        SLOT_CHINESE[winner] + "胜利",
        font=font2,
        fill=[RADIANT_GREEN, DIRE_RED][winner],
    )

    radiant_score = str(match["radiant_score"])
    radiant_score_size = font_getsize(font2, radiant_score)
    draw.text(
        (_s(288) - radiant_score_size[0], _s(81)), radiant_score, font=font2, fill=RADIANT_GREEN
    )
    draw.text((_s(410), _s(81)), str(match["dire_score"]), font=font2, fill=DIRE_RED)

    draw.rectangle((0, _s(120), _s(700), _s(122)), RADIANT_GREEN)
    draw.rectangle((0, _s(505), _s(700), _s(507)), DIRE_RED)

    image.paste(
        get_image("logo_radiant.png").resize((_s(32), _s(32)), _RESAMPLE_LANCZOS), (_s(10), _s(125))
    )
    image.paste(
        get_image("logo_dire.png").resize((_s(32), _s(32)), _RESAMPLE_LANCZOS), (_s(10), _s(510))
    )
    draw.text(
        (_s(100), _s(128) + _s(385) * winner),
        "胜利",
        font=font2,
        fill=[RADIANT_GREEN, DIRE_RED][winner],
    )

    max_stats = {
        "net": [0, 0],
        "xpm": [0, 0],
        "kills": [0, 0, 0],
        "deaths": [0, 0, 99999],
        "assists": [0, 0, 0],
        "hero_damage": [0, 0],
        "tower_damage": [0, 0],
        "stuns": [0, 0],
        "healing": [0, 0],
        "hurt": [0, 0],
        "participation": [0, 999, 999, 999999],
    }

    for slot in range(0, 2):
        team_damage = 0
        team_damage_received = 0
        team_score = [match["radiant_score"], match["dire_score"]][slot]
        team_kills = 0
        team_deaths = 0
        team_gold = 0
        team_exp = 0
        max_mvp_point = [0, 0]

        draw.text(
            (_s(50), _s(126) + _s(385) * slot),
            SLOT[slot],
            font=font,
            fill=[RADIANT_GREEN, DIRE_RED][slot],
        )
        draw.text(
            (_s(50), _s(140) + _s(385) * slot),
            SLOT_CHINESE[slot],
            font=font,
            fill=[RADIANT_GREEN, DIRE_RED][slot],
        )

        for i in range(5):
            idx = slot * 5 + i
            p = match["players"][idx]
            init_player(p)
            p["hurt"] = sum(p["damage_inflictor_received"].values())
            p["participation"] = (
                0 if team_score == 0 else 100 * (p["kills"] + p["assists"]) / team_score
            )
            team_damage += p["hero_damage"]
            team_damage_received += p["hurt"]
            team_kills += p["kills"]
            team_deaths += p["deaths"]
            team_gold += p["net_worth"]
            team_exp += p["total_xp"]

            hero_img = get_image("hero_{}.png".format(HEROES.get(p["hero_id"])))
            hero_img = hero_img.resize((_s(64), _s(36)), _RESAMPLE_LANCZOS)
            image.paste(hero_img, (_s(10), _s(170) + _s(60) * slot + _s(65) * idx))

            draw.rectangle(
                (
                    _s(54),
                    _s(191) + _s(60) * slot + _s(65) * idx,
                    _s(73),
                    _s(205) + _s(60) * slot + _s(65) * idx,
                ),
                fill=(50, 50, 50),
            )
            level = str(p["level"])
            level_size = font_getsize(font, level)
            draw.text(
                (_s(71) - level_size[0], _s(190) + _s(60) * slot + _s(65) * idx),
                level,
                font=font,
                fill=(255, 255, 255),
            )

            rank = p.get("rank_tier") if p.get("rank_tier") else 0
            rank_num, star = rank // 10, rank % 10
            rank_img = get_image(f"rank_icon_{rank_num}.png")
            if star:
                rank_star = get_image(f"rank_star_{star}.png")
                rank_img = Image.alpha_composite(rank_img, rank_star)
            rank_img = Image.alpha_composite(
                Image.new("RGBA", rank_img.size, (255, 255, 255)), rank_img
            )
            rank_img = rank_img.convert("RGB")
            rank_img = rank_img.resize((_s(45), _s(45)), _RESAMPLE_LANCZOS)
            image.paste(rank_img, (_s(75), _s(164) + _s(60) * slot + _s(65) * idx))

            rank_str = f"[{PLAYER_RANK[rank_num]}{star if star > 0 else ''}]"
            rank_size = font_getsize(font, rank_str)
            draw.text(
                (_s(122), _s(167) + _s(60) * slot + _s(65) * idx),
                rank_str,
                font=font,
                fill=(128, 128, 128),
            )

            # 玩家名：职业选手优先显示职业名（OpenDota 名录 name，普通玩家为 null），
            # 其次 steam 昵称，最后匿名占位
            pname = p.get("name") or p.get("personaname")
            if not pname:
                pname = "匿名玩家 {}".format(p.get("account_id") if p.get("account_id") else "")
            # 职业标识：OpenDota 职业名 name，或小黑盒补充流程写入的 is_pro 标记
            is_pro = (bool(p.get("name")) or bool(p.get("is_pro"))) and badge_img is not None
            badge_size = _s(13)
            badge_w = badge_size + _s(3) if is_pro else 0
            pname_y = _s(167) + _s(60) * slot + _s(65) * idx
            pname_size = font_getsize_with_fallback(pname, font_paths, font_size)
            while rank_size[0] + badge_w + pname_size[0] > _s(240):
                pname = pname[:-2] + "…"
                pname_size = font_getsize_with_fallback(pname, font_paths, font_size)
            if is_pro:
                # 认证徽章（SVG 渲染的透明 PNG），垂直居中对齐玩家名
                badge = badge_img.resize((badge_size, badge_size), _RESAMPLE_LANCZOS)
                image.paste(badge, (_s(165), pname_y + (pname_size[1] - badge_size) // 2), badge)
            draw_text_with_fallback(
                draw,
                (_s(165) + badge_w, pname_y),
                pname,
                font_paths,
                font_size,
                [RADIANT_GREEN, DIRE_RED][slot],
            )

            kda = (
                (p["kills"] + p["assists"])
                if p["deaths"] == 0
                else (p["kills"] + p["assists"]) / p["deaths"]
            )
            kda_1 = f"{p['kills']} / {p['deaths']} / {p['assists']}"
            kda_2 = f"KDA {kda:.1f}"

            pick = "第?手"
            if match.get("picks_bans"):
                for bp in match.get("picks_bans"):
                    if bp["hero_id"] == p["hero_id"]:
                        pick = "第{}手".format(bp["order"] + 1)
                        break
            if p.get("randomed"):
                pick = "随机"
            if p.get("lane_role"):
                lane = ["优势路", "中路", "劣势路", "打野"][p["lane_role"] - 1]  # noqa: F841
            draw.text(
                (_s(122), _s(181) + _s(60) * slot + _s(65) * idx), pick, font=font, fill=(0, 0, 0)
            )
            draw.text(
                (_s(165), _s(181) + _s(60) * slot + _s(65) * idx), kda_1, font=font, fill=(0, 0, 0)
            )

            net = "{:,}".format(p["net_worth"])
            net_size = font_getsize(font, net)  # noqa: F841

            draw_text_stroke(
                draw,
                (_s(122), _s(195) + _s(60) * slot + _s(65) * idx),
                net,
                font,
                (255, 235, 0),
                (0, 0, 0),
                stroke_width=1,
            )
            draw.text(
                (_s(165), _s(195) + _s(60) * slot + _s(65) * idx), kda_2, font=font, fill=(0, 0, 0)
            )

            draw.text(
                (_s(240), _s(195) + _s(60) * slot + _s(65) * idx),
                "治疗 {:,}".format(p["hero_healing"]),
                font=font,
                fill=(0, 0, 0),
            )
            tower_damage_rate = (
                0 if p["tower_damage"] == 0 else (100 * p["tower_damage"] / total_tower_hp)
            )
            draw.text(
                (_s(240), _s(209) + _s(60) * slot + _s(65) * idx),
                "塔伤 {:,} ({:.1f}%)".format(p["tower_damage"], tower_damage_rate),
                font=font,
                fill=(0, 0, 0),
            )

            p["title_position"] = [_s(10), _s(209) + _s(60) * slot + _s(65) * idx]
            mvp_point = (
                p["kills"] * 5
                + p["assists"] * 3
                + p["stuns"] * 0.5
                + p["hero_damage"] * 0.001
                + p["tower_damage"] * 0.01
                + p["hero_healing"] * 0.002
            )
            if mvp_point > max_mvp_point[1]:
                max_mvp_point = [idx, mvp_point]

            stat_checks = [
                ("net", p["net_worth"], "value"),
                ("xpm", p["xp_per_min"], "value"),
                ("kills", p["kills"], "damage"),
                ("deaths", p["deaths"], "worth"),
                ("assists", p["assists"], "damage"),
                ("hero_damage", p["hero_damage"], "value"),
                ("tower_damage", p["tower_damage"], "value"),
                ("stuns", p["stuns"], "value"),
                ("healing", p["hero_healing"], "value"),
                ("hurt", p["hurt"], "value"),
            ]
            for stat_name, stat_val, comp_type in stat_checks:
                cur = max_stats[stat_name]
                if comp_type == "value":
                    if stat_val > cur[1]:
                        max_stats[stat_name] = [idx, stat_val]
                elif comp_type == "damage":
                    if stat_val > cur[1] or (stat_val == cur[1] and p["hero_damage"] > cur[2]):
                        max_stats[stat_name] = [idx, stat_val, p["hero_damage"]]
                elif comp_type == "worth":
                    if stat_val > cur[1] or (stat_val == cur[1] and p["net_worth"] < cur[2]):
                        max_stats[stat_name] = [idx, stat_val, p["net_worth"]]

            pp = p["participation"]
            kpa = p["kills"] + p["assists"]
            mp = max_stats["participation"]
            if (
                (pp < mp[1])
                or (pp == mp[1] and kpa < mp[2])
                or (pp == mp[1] and kpa == mp[2] and p["hero_damage"] < mp[3])
            ):
                max_stats["participation"] = [idx, pp, kpa, p["hero_damage"]]

            scepter = 0
            shard = 0
            # Valve 基础数据（force 简化模式）自带的神杖/魔晶字段
            if p.get("aghanims_scepter"):
                scepter = 1
            if p.get("aghanims_shard"):
                shard = 1
            # 物品栏背景（四周与格子间隔的边框宽度统一）
            border = max(2, _s(2))
            cell_w, cell_h = _s(40), _s(30)
            bar_x = _s(373)
            bar_y = _s(171) + _s(60) * slot + _s(65) * idx
            bar_w = border * 7 + cell_w * 6
            bar_h = border * 2 + cell_h
            image.paste(Image.new("RGB", (bar_w, bar_h), (185, 185, 185)), (bar_x, bar_y))
            p["purchase_log"].reverse()
            for pl in p["purchase_log"]:
                if pl["key"] == ITEMS.get(116):
                    p["has_bkb"] = True
                    break
            for item in ITEM_SLOTS:
                if p[item] == 0:
                    item_img = Image.new("RGB", (cell_w, cell_h), (170, 170, 170))
                else:
                    if ITEMS.get(p[item], "").startswith("recipe"):
                        item_img = get_image("item_recipe.png")
                    else:
                        item_img = get_image(f"item_{ITEMS.get(p[item])}.png")
                if p[item] == 108:
                    scepter = 1
                if item == "item_neutral":
                    ima = item_img.convert("RGBA")
                    size = ima.size
                    r1 = min(size[0], size[1])
                    if size[0] != size[1]:
                        ima = ima.crop(
                            (
                                (size[0] - r1) // 2,
                                (size[1] - r1) // 2,
                                (size[0] + r1) // 2,
                                (size[1] + r1) // 2,
                            )
                        )
                    r2 = r1 // 2
                    imb = Image.new("RGBA", (r2 * 2, r2 * 2), (255, 255, 255, 0))
                    pima = ima.load()
                    pimb = imb.load()
                    r = r1 / 2
                    for ii in range(r1):
                        for jj in range(r1):
                            dist = ((ii - r) ** 2 + (jj - r) ** 2) ** 0.5
                            if dist < r2:
                                pimb[ii - (r - r2), jj - (r - r2)] = pima[ii, jj]
                    imb = imb.resize((_s(30), _s(30)), _RESAMPLE_LANCZOS)
                    imb = Image.alpha_composite(Image.new("RGBA", imb.size, (255, 255, 255)), imb)
                    item_img = imb.convert("RGB")
                    image.paste(item_img, (_s(733 - 100), _s(170) + _s(60) * slot + _s(65) * idx))
                else:
                    item_x = bar_x + border + ITEM_SLOTS.index(item) * (cell_w + border)
                    item_y = bar_y + border
                    item_img = item_img.resize((cell_w, cell_h), _RESAMPLE_LANCZOS)
                    image.paste(item_img, (item_x, item_y))
                    purchase_time = None
                    for pl in p["purchase_log"]:
                        if p[item] == 0:
                            continue
                        if pl["key"] == ITEMS.get(p[item]):
                            purchase_time = pl["time"]
                            pl["key"] += "_"
                            break
                    if purchase_time:
                        draw.rectangle(
                            (item_x, item_y + _s(19), item_x + cell_w - 1, item_y + cell_h - 1),
                            fill=(50, 50, 50),
                        )
                        time_str = (
                            f"{purchase_time // 60:0>2}:{purchase_time % 60:0>2}"
                            if purchase_time > 0
                            else f"-{-purchase_time // 60}:{-purchase_time % 60:0>2}"
                        )
                        draw.text(
                            (item_x + _s(4), item_y + _s(16)),
                            time_str,
                            font=font,
                            fill=(192, 192, 192),
                        )

            # 背包物品（物品格下方，带细边框；背景上移 border 与物品栏共享底边框，避免叠加变粗）
            # 总宽与物品栏前两格对齐：右边框与物品栏第二格的右边框重合
            bp_h = _s(19)
            bp_span = border * 3 + cell_w * 2
            base_bp_w, bp_extra = divmod(bp_span - border * 4, 3)
            image.paste(
                Image.new("RGB", (bp_span, border * 2 + bp_h), (185, 185, 185)),
                (bar_x, bar_y + bar_h - border),
            )
            bp_x = bar_x + border
            for i, item in enumerate(BACKPACK_SLOTS):
                bp_w = base_bp_w + (1 if i < bp_extra else 0)
                if p.get(item, 0) == 0:
                    item_img = Image.new("RGB", (bp_w, bp_h), (170, 170, 170))
                else:
                    if ITEMS.get(p[item], "").startswith("recipe"):
                        item_img = get_image("item_recipe.png")
                    else:
                        item_img = get_image(f"item_{ITEMS.get(p[item])}.png")
                item_img = item_img.resize((bp_w, bp_h), _RESAMPLE_LANCZOS)
                image.paste(item_img, (bp_x, bar_y + bar_h))
                bp_x += bp_w + border

            for buff in p["permanent_buffs"]:
                if buff["permanent_buff"] == 2:
                    scepter = 1
                if buff["permanent_buff"] == 12:
                    shard = 1
            scepter_img = get_image(f"scepter_{scepter}.png")
            scepter_img = scepter_img.resize((_s(20), _s(20)), _RESAMPLE_LANCZOS)
            image.paste(scepter_img, (_s(770 - 100), _s(170) + _s(60) * slot + _s(65) * idx))
            shard_img = get_image(f"shard_{shard}.png")
            shard_img = shard_img.resize((_s(20), _s(11)), _RESAMPLE_LANCZOS)
            image.paste(shard_img, (_s(770 - 100), _s(190) + _s(60) * slot + _s(65) * idx))

        for i in range(4):
            draw.rectangle(
                (
                    0,
                    _s(228) + _s(385) * slot + _s(65) * i,
                    _s(700),
                    _s(228) + _s(385) * slot + _s(65) * i,
                ),
                (225, 225, 225),
            )

        for i in range(5):
            idx = slot * 5 + i
            p = match["players"][idx]
            damage_rate = 0 if team_damage == 0 else 100 * (p["hero_damage"] / team_damage)
            draw.text(
                (_s(240), _s(181) + _s(60) * slot + _s(65) * idx),
                "伤害 {:,} ({:.1f}%)".format(p["hero_damage"], damage_rate),
                font=font,
                fill=(0, 0, 0),
            )
            draw.text(
                (_s(165), _s(209) + _s(60) * slot + _s(65) * idx),
                "参战 {:.1f}%".format(p["participation"]),
                font=font,
                fill=(0, 0, 0),
            )

        if slot == winner:
            title_pos = match["players"][max_mvp_point[0]]["title_position"]
            draw.text((title_pos[0], title_pos[1]), "MVP", font=font, fill=(255, 127, 39))
            title_pos[0] += font_getsize(font, "MVP")[0] + _s(1)
        else:
            title_pos = match["players"][max_mvp_point[0]]["title_position"]
            draw.text((title_pos[0], title_pos[1]), "魂", font=font, fill=(0, 162, 232))
            title_pos[0] += font_getsize(font, "魂")[0] + _s(1)

        draw.text((_s(375), _s(128) + _s(385) * slot), "杀敌", font=font, fill=(64, 64, 64))
        draw.text((_s(452), _s(128) + _s(385) * slot), "总伤害", font=font, fill=(64, 64, 64))
        draw.text((_s(536), _s(128) + _s(385) * slot), "总经济", font=font, fill=(64, 64, 64))
        draw.text((_s(626), _s(128) + _s(385) * slot), "总经验", font=font, fill=(64, 64, 64))
        draw.text(
            (_s(375), _s(142) + _s(385) * slot), f"{team_kills}", font=font, fill=(128, 128, 128)
        )
        draw.text(
            (_s(452), _s(142) + _s(385) * slot), f"{team_damage}", font=font, fill=(128, 128, 128)
        )
        draw.text(
            (_s(536), _s(142) + _s(385) * slot), f"{team_gold}", font=font, fill=(128, 128, 128)
        )
        draw.text(
            (_s(626), _s(142) + _s(385) * slot), f"{team_exp}", font=font, fill=(128, 128, 128)
        )

    titles = [
        ("富", (255, 192, 30), max_stats["net"]),
        ("睿", (30, 30, 255), max_stats["xpm"]),
        ("控", (255, 0, 128), max_stats["stuns"]),
        ("爆", (192, 0, 255), max_stats["hero_damage"]),
        ("破", (224, 36, 36), max_stats["kills"]),
        ("鬼", (192, 192, 192), max_stats["deaths"]),
        ("助", (0, 132, 66), max_stats["assists"]),
        ("拆", (128, 0, 255), max_stats["tower_damage"]),
        ("奶", (0, 228, 120), max_stats["healing"]),
        ("耐", (112, 146, 190), max_stats["hurt"]),
    ]
    for title_text, color, stat in titles:
        if stat[1] > 0:
            title_pos = match["players"][stat[0]]["title_position"]
            draw.text((title_pos[0], title_pos[1]), title_text, font=font, fill=color)
            title_pos[0] += font_getsize(font, title_text)[0] + _s(1)

    if max_stats["participation"][1] < 999:
        stat = max_stats["participation"]
        title_pos = match["players"][stat[0]]["title_position"]
        draw.text((title_pos[0], title_pos[1]), "摸", font=font, fill=(200, 190, 230))
        title_pos[0] += font_getsize(font, "摸")[0] + _s(1)

    if match.get("data_source") == "xiaoheihe":
        footer = "※比赛数据来自小黑盒，DOTA2游戏图片素材版权归Valve所有。"
    else:
        footer = "※录像分析数据来自OpenDota，DOTA2游戏图片素材版权归Valve所有。"
    draw.text(
        (_s(10), _s(880)),
        footer,
        font=font,
        fill=(128, 128, 128),
    )

    used_fonts = set()
    for p in match.get("players", []):
        pname = p.get("name") or p.get("personaname") or ""
        if pname:
            segs = segment_text_by_fonts(pname, font_paths)
            for _, fp in segs:
                if fp:
                    used_fonts.add(os.path.basename(fp))
    if used_fonts:
        logger.info(f"玩家名使用字体: {', '.join(sorted(used_fonts))}")

    if not output_path:
        if match.get("from_valve"):
            output_path = os.path.join(OUTPUT_DIR, f"{match_id}_simple.png")
        else:
            output_path = os.path.join(OUTPUT_DIR, f"{match_id}.png")
    image.save(output_path, "png")
    logger.info(f"战报图片已生成: {output_path}，耗时{time.time() - t0:.1f}s")
    return output_path


# ============================================================
# 同步兼容接口（方便外部调用）
# ============================================================
def generate_match_image_sync(*args, **kwargs):
    """同步版本：内部自动运行 asyncio 事件循环"""

    async def _run():
        try:
            return await generate_match_image(*args, **kwargs)
        finally:
            await close_session()

    return asyncio.run(_run())


# ============================================================
# 主函数
# ============================================================
async def async_main():
    """解析命令行参数并执行生成流程，输出结果提示。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Dota 2 战报图片生成器（异步版）")
    parser.add_argument("match_id", help="Dota 2 比赛编号ID")
    parser.add_argument("-o", "--output", help="输出图片路径", default=None)
    parser.add_argument(
        "--no-wait", action="store_true", help="不等待 OpenDota 分析完成（默认自动等待）"
    )
    parser.add_argument(
        "-t", "--timeout", type=int, default=120, help="等待分析的超时时间（秒），默认120秒"
    )
    parser.add_argument(
        "-f", "--force", action="store_true", help="强制生成简化版战报（即使分析不完整）"
    )
    parser.add_argument(
        "-s",
        "--scale",
        type=float,
        default=1.4,
        help="输出分辨率倍率：1=700x900，1.4=1120x1260(推荐)，2=1600x1800，默认1.4",
    )
    args = parser.parse_args()

    try:
        match_id = str(int(args.match_id))
    except ValueError:
        print("错误: 比赛编号必须是数字")
        sys.exit(1)

    if args.scale < 1:
        args.scale = 1

    try:
        output = await generate_match_image(
            match_id,
            args.output,
            wait=not args.no_wait,
            timeout=args.timeout,
            force=args.force,
            scale=args.scale,
        )
        if output:
            print(f"\n✓ 战报图片已保存到: {output}")
        else:
            print("\n✗ 生成战报图片失败")
            sys.exit(1)
    finally:
        await close_session()


def main():
    """程序入口：运行异步主流程。"""
    asyncio.run(async_main())


if __name__ == "__main__":
    set_verbose()
    main()
