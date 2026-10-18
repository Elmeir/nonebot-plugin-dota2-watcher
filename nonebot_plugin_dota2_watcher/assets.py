"""插件统一图片素材管理。

所有图片的落盘位置、下载、缩放与 data URI 编码都集中在本模块；生成器只说明
「要哪一类素材」，不再各自拼接目录、各自实现取图与编码，避免同一张图被写到
不同位置、或被不同模块按不同规则复用。

目录约定（均位于 ``config.IMAGES_DIR`` 下，全部是可再生的缓存）：

    hero/       英雄宽幅图 ``hero_<short>.png``（战报 / 战绩图）
    item/       物品图标 ``item_<name>.png``
    abilities/  技能图标 ``<ability>.png``
    icons/      英雄小头像 ``<short>_stratz.webp``（Stratz CDN）
    avatars/    玩家 steam 头像 ``avatar_<url md5>.png``（按 URL 复用，跨数据源）
    logo/       战队 logo ``<team_id>.png``（TI 战报）
    rank/       段位徽章 ``rank_<url md5>.png``（小黑盒下发的段位原图）
    other/      杂项素材（logo_dire、rank_icon_N、scepter_N、pro_badge 等）

历史版本曾把 other/ 中的素材直接放在 ``images/`` 根目录；``image_path()`` 在
新路径不存在时会继续命中旧文件，升级后无需重新下载。
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import struct
import zlib
from pathlib import Path

from nonebot.log import logger

# 兼容两种运行方式：作为插件包被导入，或作为独立脚本直接运行（同 utils.py）。
if __package__:
    from .config import D2PT_REPO_BASE, HERO_ICON_URL, HERO_IMAGE_URL, IMAGES_DIR, config
    from .utils import async_download_file, download_file, image_to_data_uri
else:
    from config import D2PT_REPO_BASE, HERO_ICON_URL, HERO_IMAGE_URL, IMAGES_DIR, config
    from utils import async_download_file, download_file, image_to_data_uri

# ============================================================
# 目录
# ============================================================
IMAGES_DIR = Path(IMAGES_DIR)

HERO_IMAGES_DIR = IMAGES_DIR / "hero"
ITEM_IMAGES_DIR = IMAGES_DIR / "item"
ABILITY_IMAGES_DIR = IMAGES_DIR / "abilities"
ICON_IMAGES_DIR = IMAGES_DIR / "icons"
AVATAR_IMAGES_DIR = IMAGES_DIR / "avatars"
LOGO_IMAGES_DIR = IMAGES_DIR / "logo"
RANK_IMAGES_DIR = IMAGES_DIR / "rank"
OTHER_IMAGES_DIR = IMAGES_DIR / "other"

# 文件名前缀 -> 子目录：hero_* / item_* 沿用既有布局，其余统一归入 other/
_PREFIX_SUBDIRS = (
    ("hero_", HERO_IMAGES_DIR),
    ("item_", ITEM_IMAGES_DIR),
)

ALL_DIRS = (
    HERO_IMAGES_DIR,
    ITEM_IMAGES_DIR,
    ABILITY_IMAGES_DIR,
    ICON_IMAGES_DIR,
    AVATAR_IMAGES_DIR,
    LOGO_IMAGES_DIR,
    RANK_IMAGES_DIR,
    OTHER_IMAGES_DIR,
)


def ensure_dirs() -> None:
    """创建全部素材目录（幂等）。"""
    for directory in ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


ensure_dirs()


# ============================================================
# 路径
# ============================================================
def _subdir_for(name: str) -> Path:
    """按文件名前缀判断素材类别目录。"""
    for prefix, directory in _PREFIX_SUBDIRS:
        if name.startswith(prefix):
            return directory
    return OTHER_IMAGES_DIR


def image_path(name: str) -> str:
    """按文件名返回素材的本地路径（自动归入类别子目录）。

    兼容旧版布局：新路径不存在但旧版根目录下存在同名文件时继续使用旧文件，
    避免升级后把已下载的 logo / 段位 / 法杖等素材再下一遍。
    """
    path = _subdir_for(name) / name
    legacy = IMAGES_DIR / name
    if not path.exists() and legacy.exists():
        return str(legacy)
    return str(path)


def ability_image_path(ability_name: str) -> str:
    """技能图标路径（abilities/<ability>.png）。"""
    return str(ABILITY_IMAGES_DIR / f"{ability_name}.png")


def item_image_path(filename: str) -> str:
    """物品图标路径（item/<filename>）。"""
    return str(ITEM_IMAGES_DIR / filename)


def hero_icon_path(short: str) -> Path:
    """英雄小头像路径（icons/<short>_stratz.webp）。"""
    return ICON_IMAGES_DIR / f"{short}_stratz.webp"


def avatar_path(url: str) -> Path:
    """玩家头像路径：按 URL 哈希命名，跨进程稳定（hashlib 不受 PYTHONHASHSEED 影响）。"""
    name = hashlib.md5(str(url).encode("utf-8")).hexdigest()[:16]
    return AVATAR_IMAGES_DIR / f"avatar_{name}.png"


def logo_path(team_id) -> Path:
    """战队 logo 路径（logo/<team_id>.png）。"""
    return LOGO_IMAGES_DIR / f"{int(team_id)}.png"


def rank_badge_path(url: str) -> Path:
    """段位徽章路径：按 URL 哈希命名（同一 URL 对应同一张原图）。"""
    name = hashlib.md5(str(url).encode("utf-8")).hexdigest()[:16]
    return RANK_IMAGES_DIR / f"rank_{name}.png"


# ============================================================
# 编码：PIL 图 / 本地文件 -> data URI
# ============================================================
def pil_to_data_uri(img) -> str:
    """把 Pillow 图编码成内联 PNG data URI（HTML 里直接用，免去起本地服务）。"""
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def file_data_uri(path) -> str:
    """读取本地图片文件，转成 base64 PNG data URI（带进程内缓存）。"""
    return image_to_data_uri(path)


def solid_data_uri(rgb=(128, 128, 128), w: int = 64, h: int = 36) -> str:
    """生成一个纯色 PNG 的 data URI（用于缺图标时的占位）。"""

    def _chunk(tag, data):
        chunk = struct.pack(">I", len(data)) + tag + data
        return chunk + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


# ============================================================
# 下载
# ============================================================
async def download_to(url, filepath, quiet: bool = False, timeout=None) -> bool:
    """异步下载 url 内容到本地文件，成功返回 True，失败返回 False。"""
    return await async_download_file(url, filepath, quiet=quiet, timeout=timeout)


def download_to_sync(url, filepath, quiet: bool = False, timeout=None, retries: int = 1) -> bool:
    """同步下载 url 内容到本地文件（供线程池中的调用方使用）。"""
    return download_file(url, filepath, timeout=timeout, quiet=quiet, retries=retries)


# ============================================================
# 缩放
# ============================================================
def fit_max_side(img, max_side: int):
    """等比缩放到长边不超过 max_side（尺寸不一的图，如段位徽章）。"""
    from PIL import Image

    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / longest
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def fit_max_height(img, max_h: int):
    """等比缩放到高度不超过 max_h（战队 logo 用，保持横向长宽比）。"""
    from PIL import Image

    w, h = img.size
    if h <= max_h:
        return img
    scale = max_h / h
    return img.resize((max(1, round(w * scale)), max_h), Image.LANCZOS)


# ============================================================
# 取图：本地文件（进程内缓存）
# ============================================================
# 素材在一次渲染里会被反复取用，跨渲染也高度重复；缓存已解码的副本既省掉重复
# 解码，也避免调用方就地修改时互相污染。
_IMAGE_CACHE: dict[str, object] = {}
_IMAGE_CACHE_MAX = 2048  # 超出后整体清空，避免异常数据把内存撑爆

_SCALED_CACHE: dict[tuple, object] = {}
_SCALED_CACHE_MAX = 4096


def get_image(img_name: str):
    """按名称加载本地图片（进程内缓存已解码副本），失败时返回纯色占位图。"""
    from PIL import Image

    img = _IMAGE_CACHE.get(img_name)
    if img is not None:
        return img.copy()
    try:
        img = Image.open(image_path(img_name))
        img.load()
        img = img.copy()
    except Exception as e:
        logger.warning(f"读取图片失败 {img_name}: {e}")
        return Image.new("RGBA", (30, 30), (255, 160, 160))
    if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.clear()
    _IMAGE_CACHE[img_name] = img
    return img.copy()


def get_scaled_image(img_name: str, size, resample=None):
    """取图片并按 size 缩放，结果按 (名称, 尺寸) 缓存（返回副本，调用方可安全修改）。"""
    from PIL import Image

    key = (img_name, tuple(size))
    cached = _SCALED_CACHE.get(key)
    if cached is None:
        cached = get_image(img_name).resize(size, resample or Image.LANCZOS)
        if len(_SCALED_CACHE) >= _SCALED_CACHE_MAX:
            _SCALED_CACHE.clear()
        _SCALED_CACHE[key] = cached
    return cached.copy()


def open_image(path):
    """读取本地图片为 RGBA；失败返回 None（不做进程内缓存，供一次性取图）。"""
    from PIL import Image

    try:
        with Image.open(path) as img:
            return img.convert("RGBA")
    except Exception:
        return None


# ============================================================
# 取图：远端 URL（磁盘缓存）
# ============================================================
async def load_remote_image(url, filepath, *, max_side: int | None = None):
    """下载（并磁盘缓存）远端图片为 RGBA 图；失败返回 None。

    同一 URL 只下载一次：文件已存在时直接读盘。max_side 用于等比缩小长边。
    """
    if not url:
        return None
    path = Path(filepath)
    if not path.exists() and not await download_to(url, str(path), quiet=True):
        return None
    img = open_image(path)
    if img is None:
        return None
    return fit_max_side(img, max_side) if max_side else img


# 英雄小头像内存缓存：short -> RGBA，避免同一次渲染（取主色 + 画头像）重复读盘。
_icon_mem_cache: dict[str, object] = {}


async def load_hero_icon(short: str):
    """下载（并缓存）英雄小头像为 RGBA 图；失败返回 None。

    头像源为 Stratz CDN（返回 webp，体积小），落盘到 icons/<short>_stratz.webp。
    """
    if not short:
        return None
    if short in _icon_mem_cache:
        return _icon_mem_cache[short]
    path = hero_icon_path(short)
    if not path.exists() and not await download_to(
        HERO_ICON_URL.format(short=short), str(path), quiet=True
    ):
        logger.warning(f"英雄头像下载失败（{short}）")
        return None
    img = open_image(path)
    if img is None:
        logger.warning(f"英雄头像读取失败（{short}）")
        return None
    _icon_mem_cache[short] = img
    return img


# 玩家头像内存缓存：URL -> RGBA
_avatar_mem_cache: dict[str, object] = {}


async def load_avatar(url: str):
    """下载（并缓存）玩家 steam 头像为 RGBA 图；失败返回 None。

    头像 URL 来自各数据源（Stratz / 小黑盒 / Steam），按 URL 哈希落盘，跨数据源复用。
    """
    if not url:
        return None
    if url in _avatar_mem_cache:
        return _avatar_mem_cache[url]
    img = await load_remote_image(url, avatar_path(url))
    if img is None:
        logger.warning("玩家头像下载/读取失败")
        return None
    _avatar_mem_cache[url] = img
    return img


async def avatar_data_uri(url: str, size: int) -> str:
    """玩家头像 data URI（裁成 size×size）；失败返回空串，由调用方渲染占位底色。"""
    from PIL import Image

    img = await load_avatar(url)
    if img is None:
        return ""
    return pil_to_data_uri(img.resize((size, size), Image.LANCZOS))


# 段位徽章内存缓存：(url, 目标尺寸) -> data URI
_rank_badge_uri_cache: dict[tuple[str, int], str] = {}


async def rank_badge_data_uri(url: str, size: int) -> str:
    """段位徽章 data URI：直接用数据源下发的原图（已含段位底纹与星数）。

    原图尺寸不一（150x165 / 180x180 / 256x256），按长边等比缩到 size；
    容器是正方形 + background-size: contain，等比才不会把徽章拉变形。
    下载失败返回空串，由调用方的占位底色兜底。
    """
    if not url:
        return ""
    key = (url, int(size))
    cached = _rank_badge_uri_cache.get(key)
    if cached is not None:
        return cached
    img = await load_remote_image(url, rank_badge_path(url), max_side=int(size))
    if img is None:
        return ""
    uri = pil_to_data_uri(img)
    _rank_badge_uri_cache[key] = uri
    return uri


def logo_data_uri(team_id, url, max_h: int = 50) -> str | None:
    """战队 logo 的 data URI（同步，供线程池调用）：优先读本地缓存，缺失则下载。

    落盘到 logo/<team_id>.png；下载或读取失败返回 None。
    """
    if not team_id or not url:
        return None
    path = logo_path(team_id)
    if not path.exists() and not download_to_sync(url, str(path), quiet=True, timeout=5, retries=3):
        return None
    img = open_image(path)
    if img is None:
        return None
    img = fit_max_height(img, max_h)
    img.save(path, format="PNG")
    return pil_to_data_uri(img)


async def ensure_hero_image(short: str) -> str:
    """确保英雄宽幅图（hero/<short>.png）存在，返回本地路径；失败返回空串。

    战报初始化会批量下载全部英雄图；这里按需只补当前用到的那张，
    避免 /战绩 之类的小图任务为了几张图拉全量素材。
    """
    if not short:
        return ""
    path = image_path(f"hero_{short}.png")
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return path
    if await download_to(HERO_IMAGE_URL.format(name=short), path, quiet=True):
        return path
    return ""


# ============================================================
# 职业选手认证徽章（战报与 /战绩 共用同一份素材）
# ============================================================
# 透明 PNG：金色圆 + 白色对勾（上游 d2pt_bot 仓库 images/pro_badge.png）。
# 两个生成器都要用，因此落盘位置与下载逻辑放在这里统一管理，避免各存一份。
PRO_BADGE_NAME = "pro_badge.png"
PRO_BADGE_URL = f"{D2PT_REPO_BASE}/images/pro_badge.png"

# 徽章 data URI 的内存缓存（渲染频次高、内容恒定）：目标尺寸 -> data URI
_pro_badge_uri: dict[int, str] = {}


def pro_badge_path() -> str:
    """职业认证徽章的本地路径（other/pro_badge.png）。"""
    return image_path(PRO_BADGE_NAME)


async def ensure_pro_badge() -> bool:
    """确保职业认证徽章 PNG 存在（只需下载一次）；失败返回 False。

    查找顺序：本地缓存 → 上游仓库下载（经 gh-proxy 加速）。
    失败时调用方降级为「不显示徽章」，不影响出图。
    """
    path = pro_badge_path()
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return True
    if await download_to(PRO_BADGE_URL, path, quiet=True, timeout=config.d2w_download_timeout):
        logger.info(f"职业认证徽章已下载: {path}")
        return True
    logger.warning("职业认证徽章下载失败（降级为不显示徽章）")
    return False


def pro_badge_image():
    """读取职业认证徽章为 RGBA 图；未下载或读取失败返回 None。"""
    path = pro_badge_path()
    if not os.path.exists(path):
        return None
    return open_image(path)


async def pro_badge_data_uri(size: int) -> str:
    """职业认证徽章 data URI（缩放到 size×size）；不可用返回空串。

    HTML 出图用：徽章尺寸恒定，每个尺寸的 data URI 只编码一次后常驻内存。
    """
    size = int(size)
    cached = _pro_badge_uri.get(size)
    if cached is not None:
        return cached
    if not await ensure_pro_badge():
        return ""
    img = pro_badge_image()
    if img is None:
        return ""
    from PIL import Image

    uri = pil_to_data_uri(img.resize((size, size), Image.LANCZOS))
    _pro_badge_uri[size] = uri
    return uri


def clear_memory_caches() -> None:
    """清空进程内素材缓存（测试或长时间运行后释放内存用）。"""
    _pro_badge_uri.clear()
    _IMAGE_CACHE.clear()
    _SCALED_CACHE.clear()
    _icon_mem_cache.clear()
    _avatar_mem_cache.clear()
    _rank_badge_uri_cache.clear()
