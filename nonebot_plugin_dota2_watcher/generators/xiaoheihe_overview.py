"""小黑盒「最近战绩」图片生成器（HTML + Playwright，风格同 /出装、/开黑）。

数据来源见 ``../datasources/xiaoheihe_overview.py``（小黑盒 overview 接口，
免签名、只需 steam_id，数据与生成图各缓存 1 小时）。

版式参考小黑盒玩家主页，但按机器人图片的可读性重排：顶部玩家卡 →
近20场战绩条（胜负小方块）→ 比赛列表（结果/英雄/KDA/模式时长/对局段位）→
常用英雄（英雄/胜率/场次/MMR排名/KDA）→ 生涯纪录（图标/数值/说明，4 列等分）。

模块自下而上分层，改版式时按层定位：

    常量/主题 → 素材预取 → 数据工具 → 视图组件 → 页面组装 → 渲染入口

视图组件一律「输入原始 JSON + 已取好的素材、输出 HTML 字符串」，不读写全局
状态、也不做网络请求；所有图片在 ``_prefetch_assets`` 里一次性并发取好，因此
某个模块缺数据时整张卡片自然消失，不会拖慢或影响其它模块。

样式复用 /出装（core_build.py）的主题与容器规范：同一套 FONT_FAMILY / THEME，
外层 #container 宽度 28.125rem（超采样 2x 后 540px），胜/负沿用项目既有的
绿色 / 红色，金色用于段位与 MMR 点缀。
"""

from __future__ import annotations

import asyncio
import io
import os
import time

from nonebot.log import logger

from .. import assets
from ..config import OUTPUT_DIR, config, normalize_image_theme
from ..datasources import xiaoheihe_overview as ds
from . import shared_browser
from .core_build import FONT_FAMILY, THEME_DARK, THEME_LIGHT, THEMES

# 容器宽度：与项目其它出图一致（28.125rem = 540px）
CONTAINER_REM = 28.125
ROOT_FONT_PX = 19.2  # 1rem 的像素值，与 /开黑、/pro 一致
SUPERSAMPLE = 2  # 先按 2x 渲染再缩回，边缘与文字更细腻

# 配色：胜/负沿用项目既有出图，金色用于段位与 MMR 点缀
WIN_COLOR = "#7cc45c"
LOSE_COLOR = "#d9534f"
GOLD = "#c8a45c"
MVP_BG = "#f5c542"
MVP_FG = "#3a2a00"

# 反复出现的 CSS 声明片段，抽出来避免每处各写一遍、改字号时漏改
NUMERIC = "font-variant-numeric: tabular-nums;"  # 数字等宽，列内不跳动
ELLIPSIS = "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
CARD_PADDING = "0.62rem 0.72rem"

# 【Ta的比赛】各列宽度：表头与数据行共用这一份定义，避免两处各写一套导致错位。
# 行内用 flex + space-between，剩余空间自动均分成列间距，不再让某一列吃掉全部余量。
#   result 列 = 色条 + 间隙 + 文字，三者合成一个 flex 组，
#   否则色条会被 space-between 甩到最左边，和「胜利/失败」之间空出一大块。
MATCH_COLS = {
    "bar": "0.16rem",  # 胜负色条
    "gap": "0.34rem",  # 色条与结果文字之间
    "result": "3.0rem",  # 胜利/失败 + X天前
    "hero": "3.05rem",  # 英雄宽幅图 + 等级/MVP 角标
    "kda": "3.6rem",  # KDA 比值 + k/d/a
    "mode": "4.4rem",  # 模式 + 时长
    "dan": "3.1rem",  # 对局段位勋章 + 文字
}
# 结果组总宽（表头用它对齐）
MATCH_RESULT_GROUP = f"calc({MATCH_COLS['bar']} + {MATCH_COLS['gap']} + {MATCH_COLS['result']})"

# 近20场：两行 x 10 列
RECENT_COLUMNS = 10
RECENT_LIMIT = RECENT_COLUMNS * 2

# 常用英雄：表头与数据行共用的列定义（含垂直居中，两处必须完全一致）
HERO_COLS = "grid-template-columns: 2.1rem 3.5rem 1fr 2.5rem 3.6rem 3.4rem; align-items: center;"

# 生涯纪录每行 4 项等分
CAREER_COLUMNS = 4
CAREER_SPACER = '<div style="flex: 1 1 0; min-width: 0;"></div>'

# 素材目标尺寸（超采样 2x 后取图，缩回后仍清晰）
WIDE_HERO_SIZE = (128, 72)  # 比赛列表英雄宽幅图（显示 3.05rem ≈ 58px）
PLAYER_RANK_SIZE = 96  # 玩家卡段位勋章（显示 2.55rem ≈ 49px）
MATCH_RANK_SIZE = 64  # 比赛列表对局段位勋章（显示 1.1rem ≈ 21px）
AVATAR_SIZE = 128  # 玩家头像（显示 3.5rem ≈ 67px）
PRO_BADGE_SIZE = 64  # 职业认证徽章（显示 0.78rem ≈ 15px）

IMAGE_CACHE_SECONDS = config.d2w_xiaoheihe_cache_seconds


# ============================================================
# 素材预取：一次渲染内的所有图片在这里并发取好
# ============================================================
def _hero_short(url: str) -> str:
    """从接口给的图片 URL 里取出英雄短名。

    .../icons/zuus_icon.png      -> zuus
    .../hphover/zuus_hphover.png -> zuus
    """
    base = str(url or "").rsplit("/", 1)[-1]
    for suffix in ("_icon.png", "_hphover.png", ".png", ".webp"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


class _Assets:
    """一次渲染内用到的素材：预取后按短名/URL 同步取用，视图组件因此保持纯字符串拼接。"""

    def __init__(self) -> None:
        self.wide: dict[str, str] = {}
        self.small: dict[str, str] = {}
        self.avatars: dict[str, str] = {}
        self.ranks: dict[tuple[str, int], str] = {}
        self.pro_badge: str = ""

    def hero_wide(self, url: str) -> str:
        return self.wide.get(_hero_short(url), "")

    def hero_small(self, url: str) -> str:
        return self.small.get(_hero_short(url), "")

    def avatar(self, url: str) -> str:
        return self.avatars.get(str(url or ""), "")

    def rank(self, url: str, size: int) -> str:
        return self.ranks.get((str(url or ""), int(size)), "")

    def pro(self) -> str:
        """职业认证徽章（与战报共用 assets 里那张透明 PNG）；不可用时为空串。"""
        return self.pro_badge


def _collect_urls(modules: dict[str, dict]) -> tuple[set[str], set[str]]:
    """收集本次渲染用到的 (宽幅图 URL, 小图标 URL)。

    宽幅图只用于比赛列表的英雄列；小图标用于近20场方块、最高记录、常用英雄、
    生涯纪录与段位旁的英雄图，因此两类分开收集，避免为小图标多下宽幅图。
    """
    wide: set[str] = set()
    small: set[str] = set()

    for match in (modules.get("match_list") or {}).get("match_list") or []:
        wide.add(str(match.get("hero_image") or match.get("hero_icon") or ""))
    for match in (modules.get("recent_match") or {}).get("match_list") or []:
        small.add(str(match.get("hero_icon") or ""))
    for hero in (modules.get("common_hero") or {}).get("status_list") or []:
        small.add(str(hero.get("hero_icon") or hero.get("hero_image") or ""))
    for stat in (modules.get("r20_data_stats") or {}).get("recent_stats_list") or []:
        small.add(str(stat.get("icon") or ""))
    for item in (modules.get("career_record") or {}).get("career_record") or []:
        small.add(str(item.get("icon") or ""))

    wide.discard("")
    small.discard("")
    return wide, small


def _rank_urls(modules: dict[str, dict]) -> dict[str, set[int]]:
    """收集段位勋章 URL 及其需要的目标尺寸。"""
    wanted: dict[str, set[int]] = {}
    mmr_info = (modules.get("mmr_stats") or {}).get("mmr_info") or {}
    if mmr_info.get("dan_icon"):
        wanted.setdefault(str(mmr_info["dan_icon"]), set()).add(PLAYER_RANK_SIZE)
    for match in (modules.get("match_list") or {}).get("match_list") or []:
        if match.get("dan_icon"):
            wanted.setdefault(str(match["dan_icon"]), set()).add(MATCH_RANK_SIZE)
    return wanted


async def _wide_uri(short: str) -> tuple[str, str]:
    """英雄宽幅图 -> data URI（缺失时按需下载该英雄的原图，不拉全量素材）。"""
    path = await assets.ensure_hero_image(short)
    if not path:
        return short, ""
    img = assets.open_image(path)
    if img is None:
        return short, ""
    from PIL import Image

    if img.size != WIDE_HERO_SIZE:
        img = img.resize(WIDE_HERO_SIZE, Image.LANCZOS)
    return short, assets.pil_to_data_uri(img)


async def _small_uri(short: str) -> tuple[str, str]:
    """英雄小头像 -> data URI（缺失时自动下载，失败返回空串）。"""
    img = await assets.load_hero_icon(short)
    return short, assets.pil_to_data_uri(img) if img is not None else ""


async def _rank_uri(url: str, size: int) -> tuple[tuple[str, int], str]:
    """段位勋章 -> data URI（按 URL + 尺寸缓存）。"""
    return (url, size), await assets.rank_badge_data_uri(url, size)


async def _prefetch_assets(modules: dict[str, dict]) -> _Assets:
    """并发预取本次渲染需要的全部素材（头像 / 英雄宽图 / 小头像 / 段位勋章 / 职业徽章）。

    冷缓存时逐个 await 会是 N × RTT（一张战绩图可达 30+ 张图），并发后只需
    1 个 RTT；gather 保序，各字典按短名/URL 回填。
    """
    result = _Assets()
    wide_urls, small_urls = _collect_urls(modules)
    wide_shorts = {_hero_short(url) for url in wide_urls}
    small_shorts = {_hero_short(url) for url in small_urls}
    wide_shorts.discard("")
    small_shorts.discard("")

    header_info = (modules.get("header") or {}).get("header_info") or {}
    avatar_url = str(header_info.get("avatar") or "")
    rank_jobs = [(url, size) for url, sizes in _rank_urls(modules).items() for size in sizes]

    wide_results, small_results, avatar_uri, rank_results, pro_uri = await asyncio.gather(
        asyncio.gather(*(_wide_uri(short) for short in wide_shorts)),
        asyncio.gather(*(_small_uri(short) for short in small_shorts)),
        assets.avatar_data_uri(avatar_url, AVATAR_SIZE),
        asyncio.gather(*(_rank_uri(url, size) for url, size in rank_jobs)),
        # 只有接口标记为认证玩家时才需要徽章，非职业玩家不必为它多下一次
        assets.pro_badge_data_uri(PRO_BADGE_SIZE) if is_pro(header_info) else _noop(),
    )
    result.wide = dict(wide_results)
    result.small = dict(small_results)
    result.avatars = {avatar_url: avatar_uri} if avatar_uri else {}
    result.ranks = dict(rank_results)
    result.pro_badge = pro_uri
    return result


async def _noop() -> str:
    """占位协程：保持 gather 的返回值元数固定，省去调用处的分支。"""
    return ""


# ============================================================
# 数据工具
# ============================================================
def _esc(value) -> str:
    """转义文本节点里的 & < >；None 视为空串，避免渲染出字面量 "None"。"""
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _to_int(value, default: int = 0) -> int:
    """宽松转 int：'3' -> 3；None / '' / 'abc' -> default。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _percent(value) -> float:
    """把 '54.7%' 解析成 54.7；解析失败按 0 处理（胜率条宽度用）。"""
    try:
        return float(str(value or "0").rstrip("%"))
    except ValueError:
        return 0.0


def parse_kda(kda: dict | None) -> tuple[int, int, int, str]:
    """kda = {'kill','death','assist','kd'} -> (k, d, a, kd 文本)。"""
    kda = kda or {}
    k = _to_int(kda.get("kill"))
    d = _to_int(kda.get("death"))
    a = _to_int(kda.get("assist"))
    kd = str(kda.get("kd") or "")
    if not kd:
        kd = f"{(k + a) / d:.1f}" if d else f"{k + a:.1f}"
    return k, d, a, kd


def _is_win(match: dict) -> bool:
    """接口对负场直接不下发 win 字段（实测真实玩家都有），只认字符串 '1'。"""
    return str(match.get("win")) == "1"


def _is_mvp(match: dict) -> bool:
    return str(match.get("is_mvp") or "0") == "1"


def is_pro(header_info: dict) -> bool:
    """是否职业认证玩家：只看小黑盒下发的 ``is_verified`` 字段。

    接口口径是 ``"1"`` / ``"0"`` 字符串（实测职业选手为 "1"、普通玩家为 "0"）。
    这里不再叠加以往战报流程里的 OpenDota 名录、职业队伍登记等外部判断，
    以接口为准，避免同一玩家在两张图里职业标识不一致。
    """
    return str(header_info.get("is_verified") or "0") == "1"


def is_plus(header_info: dict) -> bool:
    """是否小黑盒 PLUS 会员：接口口径同样是 "1" / "0" 字符串。"""
    return str(header_info.get("is_plus_subscriber") or "0") == "1"


def _argb_rgba(value: str) -> str:
    """把接口给的 8 位 #AARRGGBB 转成 CSS rgba()。

    生涯纪录里「最高连胜 / 最高连败」两项不带英雄图标，只给 color + bg。
    color 是 AARRGGBB（B292A525 = 70% 的橄榄绿、B2C23C2A = 70% 的红），
    正好对应连胜/连败；按 RRGGBBAA 读会得到 15% 的紫色，语义上说不通。
    解析不出来时返回空串，调用方回退到卡片边框色。
    """
    text = str(value or "").strip().lstrip("#")
    if len(text) == 8:
        try:
            a, r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4, 6))
        except ValueError:
            return ""
        return f"rgba({r}, {g}, {b}, {a / 255:.3f})"
    if len(text) == 6:
        try:
            int(text, 16)
        except ValueError:
            return ""
        return f"#{text}"
    return ""


# ============================================================
# 视图组件：复用的 HTML 片段
# ============================================================
def _card(theme: dict, inner: str) -> str:
    return (
        f'<div style="background-color: {theme["card_bg"]}; '
        f"border: 0.078rem solid {theme['card_border']}; "
        f'border-radius: 0.5625rem; padding: {CARD_PADDING};">{inner}</div>'
    )


def _card_if(content: str, theme: dict) -> str:
    """内容为空时不包卡片：否则会渲染出一排空白圆角条。"""
    return _card(theme, content) if content else ""


def _img_placeholder_bg(theme: dict) -> str:
    """图片未就绪时的占位底色。

    talent_circle_bg 在深色主题下与卡片底色几乎同色，图片加载失败时会看不出
    那里本该有图；这里用 card_border（半透明白/黑）兜底，两种主题都可见。
    """
    return theme.get("card_border") or theme["talent_circle_bg"]


def _image_bg(uri: str, *, fit: str = "cover", repeat: bool = True) -> str:
    """background-image 声明；uri 为空返回空串，由占位底色兜底。

    fit="contain" 用于尺寸不一、需要等比完整显示的图（段位勋章 / 英雄小图），
    此时要配 repeat=False：平铺会在留白处露出第二份图的边缘。
    """
    if not uri:
        return ""
    repeat_css = "" if repeat else " background-repeat: no-repeat;"
    return (
        f"background-image: url('{uri}'); background-size: {fit}; "
        f"background-position: center;{repeat_css}"
    )


def _section_title(text: str, theme: dict, margin_bottom: str | None = None) -> str:
    """卡片标题（0.98rem / 600）；margin_bottom 为空时不写该声明，由外层容器控距。"""
    margin = f"margin-bottom: {margin_bottom}; " if margin_bottom else ""
    return (
        f'<div style="font-size: 0.98rem; font-weight: 600; {margin}'
        f'color: {theme["title_color"]};">{text}</div>'
    )


def _value_label(
    value: str,
    label: str,
    theme: dict,
    *,
    value_color: str,
    label_gap: str,
    value_size: str = "",
    label_size: str = "",
) -> str:
    """「大数值 + 小标签」内联一组，用于胜场/负场/KDA、比赛场次/胜率等。

    字号留空时继承父元素字号（玩家卡里胜场那一行就是这么排的）。
    """
    value_size_css = f"font-size: {value_size}; " if value_size else ""
    label_size_css = f"font-size: {label_size}; " if label_size else ""
    return (
        '<span style="white-space: nowrap;">'
        f'<span style="{value_size_css}font-weight: 600; color: {value_color}; {NUMERIC}">'
        f"{value}</span>"
        f'<span style="{label_size_css}color: {theme["desc_color"]}; '
        f'margin-left: {label_gap};">{label}</span>'
        "</span>"
    )


# ============================================================
# 顶部玩家卡
# ============================================================
def _avatar(uri: str, theme: dict) -> str:
    return (
        '<div style="flex: 0 0 auto; width: 3.5rem; height: 3.5rem; '
        f"border-radius: 0.5rem; background-color: {_img_placeholder_bg(theme)}; "
        f"{_image_bg(uri)} "
        f'box-shadow: 0 0 0 0.075rem {theme["card_border"]};"></div>'
    )


def _plus_badge() -> str:
    """小黑盒 PLUS 会员胶囊。

    top 负偏移是「光学对齐」而非布局对齐：外层 flex 的 align-items: center 让
    胶囊盒与 SteamID 文字的**盒子**居中，但 0.62rem 小字的墨迹（数字/大写字母）
    只占 em 框上半部分，墨迹中心比盒子中心高约 0.75px，胶囊因此看着偏低。
    实测 -0.035rem（≈0.67px）后两者墨迹中心差 <0.25px（约 1 个 2x 子像素），
    且 position: relative 不参与布局，卡片高度不变。
    """
    return (
        '<div style="display: inline-flex; align-items: center; flex: 0 0 auto; '
        "position: relative; top: -0.035rem; "
        f"background-color: {GOLD}; color: #fff; font-size: 0.54rem; font-weight: 700; "
        "padding: 0.02rem 0.26rem; border-radius: 0.5rem; letter-spacing: 0.04rem; "
        'line-height: 1.5;">PLUS</div>'
    )


def _pro_badge(art: _Assets) -> str:
    """职业认证徽章：与战报同一张透明 PNG，昵称右侧的小圆章。

    素材缺失（首次运行且下载失败）时返回空串，退化为「只有昵称」，不占位。
    这里不做光学微调：实测徽章与昵称墨迹中心差 ≤0.5px，已是 2x 子像素精度；
    PLUS 胶囊需要微调是因为它更矮，且与 0.62rem 的小字并排。
    """
    uri = art.pro()
    if not uri:
        return ""
    return (
        '<div style="flex: 0 0 auto; width: 0.78rem; height: 0.78rem; '
        f'{_image_bg(uri, fit="contain", repeat=False)}"></div>'
    )


def _name_line(header_info: dict, theme: dict, art: _Assets) -> str:
    """昵称 + 职业认证徽章；昵称过长时省略，徽章不参与压缩。"""
    name = _esc(header_info.get("name"))
    pro = _pro_badge(art) if is_pro(header_info) else ""
    return (
        '<div style="display: flex; align-items: center; gap: 0.3rem; min-width: 0;">'
        f'<div style="font-size: 1.02rem; font-weight: 600; line-height: 1.3; '
        f'color: {theme["title_color"]}; {ELLIPSIS} min-width: 0;">{name}</div>'
        f"{pro}</div>"
    )


def _steam_id_line(header_info: dict, theme: dict) -> str:
    """SteamID + PLUS 徽章：PLUS 是小黑盒会员标识，跟在账号后面比跟在昵称后更贴切。"""
    plus = _plus_badge() if is_plus(header_info) else ""
    return (
        '<div style="display: flex; align-items: center; gap: 0.28rem; '
        'margin-top: 0.04rem; min-width: 0;">'
        f'<div style="font-size: 0.62rem; color: {theme["desc_color"]}; '
        f'white-space: nowrap; {NUMERIC}">SteamID: '
        f"{_esc(header_info.get('steam_id'))}</div>"
        f"{plus}</div>"
    )


def _overall_line(stats: list[dict], theme: dict) -> str:
    """胜场 / 负场 / KDA：放在 SteamID 下方一行内联文字。"""
    color_by_desc = {"胜场": WIN_COLOR, "负场": LOSE_COLOR}
    pairs = "".join(
        _value_label(
            _esc(stat.get("value")),
            _esc(stat.get("desc")),
            theme,
            value_color=color_by_desc.get(str(stat.get("desc") or ""), theme["title_color"]),
            label_gap="0.14rem",
        )
        for stat in stats
    )
    if not pairs:
        return ""
    return (
        '<div style="display: flex; align-items: baseline; gap: 0.5rem; '
        f'margin-top: 0.24rem; font-size: 0.75rem; white-space: nowrap;">{pairs}</div>'
    )


def _dan_badge(uri: str, value: str) -> str:
    """段位勋章图；分数压在勋章底部，比另起一行更接近小黑盒版式。"""
    score = (
        '<div style="position: absolute; left: 0; right: 0; bottom: 0.04rem; '
        "text-align: center; font-size: 0.58rem; font-weight: 700; color: #fff; "
        "line-height: 1.1; letter-spacing: 0.01rem; "
        f'text-shadow: 0 0 0.16rem rgba(0,0,0,0.95); {NUMERIC}">{value}</div>'
        if value
        else ""
    )
    return (
        '<div style="position: relative; flex: 0 0 auto; width: 2.55rem; height: 2.55rem; '
        f'{_image_bg(uri, fit="contain", repeat=False)}">{score}</div>'
    )


def _dan_name_line(dan_name: str, dan_value: str, theme: dict, *, has_badge: bool) -> str:
    """段位名；没有勋章图时，分数退化成段位名后面的小字。"""
    score = (
        f'<span style="font-size: 0.62rem; color: {theme["desc_color"]}; '
        f'margin-left: 0.16rem; {NUMERIC}">{dan_value}</span>'
        if dan_value and not has_badge
        else ""
    )
    return (
        '<div style="display: flex; align-items: baseline; white-space: nowrap;">'
        f'<span style="font-size: 0.88rem; font-weight: 600; color: {GOLD}; '
        f'line-height: 1.2;">{dan_name}</span>{score}</div>'
    )


def _match_stats_line(stats: list[dict], theme: dict) -> str:
    """比赛场次 / 胜率：与小黑盒「5634比赛 49.7%胜率」一致的内联写法。"""
    pairs = "".join(
        _value_label(
            _esc(stat.get("value")),
            _esc(stat.get("desc")),
            theme,
            value_color=theme["title_color"],
            label_gap="0.1rem",
            value_size="0.76rem",
            label_size="0.54rem",
        )
        for stat in stats
    )
    return (
        '<div style="display: flex; align-items: baseline; gap: 0.34rem; '
        f'margin-top: 0.12rem; white-space: nowrap;">{pairs}</div>'
    )


def _player_rank_block(mmr_info: dict, theme: dict, art: _Assets) -> str:
    """右侧段位卡：勋章在左（分数压在勋章底部），右侧两行文字左缘对齐。"""
    dan_name = _esc(mmr_info.get("dan_name"))
    dan_value = _esc(mmr_info.get("dan_value"))
    badge_uri = art.rank(mmr_info.get("dan_icon") or "", PLAYER_RANK_SIZE)
    stats = [
        stat for stat in (mmr_info.get("match_stats") or []) if str(stat.get("value") or "").strip()
    ]

    lines = []
    if dan_name:
        lines.append(_dan_name_line(dan_name, dan_value, theme, has_badge=bool(badge_uri)))
    if stats:
        lines.append(_match_stats_line(stats, theme))
    if not lines:
        return ""

    badge_html = _dan_badge(badge_uri, dan_value) if badge_uri else ""
    return (
        '<div style="flex: 0 0 auto; display: flex; align-items: center; gap: 0.28rem;">'
        f"{badge_html}"
        '<div style="display: flex; flex-direction: column; align-items: flex-start;">'
        f"{''.join(lines)}</div></div>"
    )


def _player_card(header_info: dict, mmr: dict, theme: dict, art: _Assets) -> str:
    """顶部玩家卡：头像 + 昵称/职业徽章 + SteamID/PLUS + 段位勋章 + 胜负场/KDA/总场次/胜率。"""
    identity = (
        '<div style="flex: 1 1 auto; min-width: 0;">'
        f"{_name_line(header_info, theme, art)}"
        f"{_steam_id_line(header_info, theme)}"
        f"{_overall_line(mmr.get('match_stats') or [], theme)}"
        "</div>"
    )
    rank_block = _player_rank_block(mmr.get("mmr_info") or {}, theme, art)
    inner = (
        '<div style="display: flex; align-items: center; gap: 0.6rem;">'
        f"{_avatar(art.avatar(header_info.get('avatar') or ''), theme)}"
        f"{identity}"
        f"{rank_block}"
        "</div>"
    )
    return _card(theme, inner)


# ============================================================
# 近20场比赛：胜负方块 + 最高记录
# ============================================================
def _recent_tile(match: dict, theme: dict, art: _Assets) -> str:
    """单个胜负方块：外格铺胜/负底色，内部英雄图标缩小居中，MVP 贴底。"""
    icon = art.hero_small(match.get("hero_icon") or "")
    icon_html = (
        '<div style="width: 1.16rem; height: 1.16rem; border-radius: 0.14rem; '
        f'{_image_bg(icon)}"></div>'
        if icon
        else ""
    )
    # MVP 角标：贴底的细条 + 金色，不压住英雄脸部；有角标时图标上移让位
    mvp = _is_mvp(match)
    mvp_html = (
        '<div style="position: absolute; left: 0; right: 0; bottom: 0; '
        "font-size: 0.4rem; line-height: 1.3; text-align: center; font-weight: 700; "
        f'color: {MVP_FG}; background: {MVP_BG}; letter-spacing: 0.02rem;">MVP</div>'
        if mvp
        else ""
    )
    tile_bg = WIN_COLOR if _is_win(match) else LOSE_COLOR
    # 图标缩小后不再描边：缩小时 box-shadow 会被放大成明显的「内边框」
    return (
        '<div style="position: relative; flex: 0 0 auto; width: 1.7rem; height: 1.7rem; '
        f"border-radius: 0.28rem; overflow: hidden; background-color: {tile_bg}; "
        "display: flex; align-items: center; justify-content: center; "
        f'padding-bottom: {"0.2rem" if mvp else "0"};">'
        f"{icon_html}{mvp_html}</div>"
    )


def _recent_head(recent: dict, theme: dict) -> str:
    return (
        '<div style="display: flex; align-items: baseline; justify-content: space-between; '
        'margin-bottom: 0.45rem;">'
        f"{_section_title(_esc(recent.get('title') or '近20场比赛'), theme)}"
        '<div style="font-size: 0.74rem; display: flex; gap: 0.75rem;">'
        f'<span style="color: {theme["desc_color"]};">KD {_esc(recent.get("kda"))}</span>'
        f'<span style="color: {WIN_COLOR};">胜 {_to_int(recent.get("win"))}</span>'
        f'<span style="color: {LOSE_COLOR};">负 {_to_int(recent.get("lose"))}</span>'
        "</div></div>"
    )


def _recent_strip(recent: dict, theme: dict, art: _Assets) -> str:
    """近20场：胜负小方块（英雄图标 + 胜/负底色 + MVP 角标）。"""
    matches = recent.get("match_list") or []
    if not matches:
        return ""
    tiles = [_recent_tile(match, theme, art) for match in matches[:RECENT_LIMIT]]
    return _recent_head(recent, theme) + (
        f'<div style="display: grid; grid-template-columns: repeat({RECENT_COLUMNS}, 1fr); '
        f'gap: 0.3rem;">{"".join(tiles)}</div>'
    )


def _best_cell(item: dict, theme: dict, art: _Assets) -> str:
    icon = art.hero_small(item.get("icon") or "")
    icon_html = (
        '<div style="width: 0.95rem; height: 0.95rem; border-radius: 0.16rem; '
        f'{_image_bg(icon)} flex: 0 0 auto;"></div>'
        if icon
        else ""
    )
    return (
        '<div style="display: flex; align-items: center; justify-content: center; '
        'gap: 0.2rem; flex: 1 1 0; min-width: 0;">'
        f"{icon_html}"
        f'<span style="font-size: 0.62rem; color: {theme["desc_color"]}; '
        f'line-height: 1.2; white-space: nowrap;">{_esc(item.get("desc"))}</span>'
        f'<span style="font-size: 0.82rem; font-weight: 600; '
        f'color: {theme["title_color"]}; line-height: 1.2; {NUMERIC}">'
        f"{_esc(item.get('value'))}</span></div>"
    )


def _recent_best(r20: dict, theme: dict, art: _Assets) -> str:
    """近20场最高：击杀 / KDA / 助攻 / GPM，一行式（图 描述 数值），无标题。

    作为「近20场比赛」卡片的下半部分渲染，与上方方块之间只用留白分隔。
    """
    items = [
        stat
        for stat in (r20.get("recent_stats_list") or [])
        if str(stat.get("value") or "").strip()
    ]
    if not items:
        return ""
    cells = [_best_cell(item, theme, art) for item in items]
    # 四项等分，格间不画竖线：四项各自居中，留白本身就够区分
    return f'<div style="display: flex; align-items: center;">{"".join(cells)}</div>'


# ============================================================
# Ta的比赛：逐行拆成「结果 / 英雄 / KDA / 模式 / 段位」五个单元
# ============================================================
def _match_result_bar(match: dict) -> str:
    """胜负色条：胜绿负红（细条，与小黑盒截图左缘一致）。"""
    color = WIN_COLOR if _is_win(match) else LOSE_COLOR
    return (
        f'<div style="flex: 0 0 auto; width: {MATCH_COLS["bar"]}; height: 1.9rem; '
        f'border-radius: 0.1rem; background-color: {color};"></div>'
    )


def _match_result_text(match: dict, theme: dict) -> str:
    """结果列：上「胜利/失败」，下「X天前」。"""
    win = _is_win(match)
    color = WIN_COLOR if win else LOSE_COLOR
    result = "胜利" if win else "失败"
    # 时间文案长度不受接口保证（「3天前」「刚刚」「昨天 23:14」等），
    # 定宽列里一律单行 + 省略号截断，宁可变「…」也不许换行或挤出列外。
    time_desc = _esc(match.get("time_desc"))
    time_html = (
        f'<div style="font-size: 0.54rem; color: {theme["desc_color"]}; '
        f"margin-top: 0.1rem; line-height: 1.2; white-space: nowrap; "
        f'{ELLIPSIS}">{time_desc}</div>'
        if time_desc
        else ""
    )
    return (
        f'<div style="flex: 0 0 auto; width: {MATCH_COLS["result"]}; text-align: center;">'
        f'<div style="font-size: 0.72rem; font-weight: 600; color: {color}; '
        f'line-height: 1.2;">{result}</div>'
        f"{time_html}</div>"
    )


def _match_hero(match: dict, theme: dict, art: _Assets) -> str:
    """英雄列：宽幅英雄图 + 右上 MVP 角标 + 左下等级。"""
    icon = art.hero_wide(match.get("hero_image") or match.get("hero_icon") or "")
    mvp_html = (
        '<div style="position: absolute; right: 0; top: 0; padding: 0 0.14rem; '
        "font-size: 0.48rem; line-height: 1.2; font-weight: 700; "
        f'color: {MVP_FG}; background: {MVP_BG}; border-radius: 0 0.28rem 0 0.2rem;">'
        "MVP</div>"
        if _is_mvp(match)
        else ""
    )
    # 左右各让出 0.2rem，避免和「结果」「KDA」两列的间距看起来被吃掉
    return (
        f'<div style="position: relative; flex: 0 0 auto; width: {MATCH_COLS["hero"]}; '
        "height: 1.72rem; margin: 0 0.2rem; border-radius: 0.24rem; overflow: hidden; "
        f'background-color: {_img_placeholder_bg(theme)}; {_image_bg(icon)}">'
        f"{mvp_html}"
        '<div style="position: absolute; left: 0; bottom: 0; min-width: 0.9rem; '
        "text-align: center; font-size: 0.5rem; line-height: 1.35; color: #fff; "
        "background: rgba(0,0,0,0.6); border-radius: 0 0.18rem 0 0.24rem; "
        f'{NUMERIC}">{_esc(match.get("hero_level"))}</div></div>'
    )


def _match_kda(match: dict, theme: dict) -> str:
    k, d, a, kd = parse_kda(match.get("kda"))
    return (
        f'<div style="flex: 0 0 auto; width: {MATCH_COLS["kda"]}; text-align: center;">'
        f'<div style="font-size: 0.8rem; font-weight: 600; line-height: 1.2; '
        f'color: {theme["title_color"]}; {NUMERIC}">{kd}</div>'
        f'<div style="font-size: 0.6rem; color: {theme["desc_color"]}; margin-top: 0.06rem; '
        f'{NUMERIC}">{k} / {d} / {a}</div></div>'
    )


def _match_mode(match: dict, theme: dict) -> str:
    """模式 / 时长（「X天前」已移到左侧结果列下方）。"""
    return (
        f'<div style="flex: 0 0 auto; width: {MATCH_COLS["mode"]}; min-width: 0; '
        'text-align: center;">'
        f'<div style="font-size: 0.62rem; color: {theme["title_color"]}; opacity: 0.78; '
        f'{ELLIPSIS}">{_esc(match.get("module"))}</div>'
        f'<div style="font-size: 0.58rem; color: {theme["desc_color"]}; margin-top: 0.06rem; '
        f'{NUMERIC}">{_esc(match.get("duration"))}</div></div>'
    )


def _match_dan(match: dict, theme: dict, art: _Assets) -> str:
    """对局段位：勋章图 + 段位文字。"""
    dan_desc = _esc(match.get("dan_desc"))
    dan_icon = match.get("dan_icon") or ""
    # 用原始字段判空：dan_icon 下载失败时仍保留定宽列，避免整行右缘错位
    if not dan_desc and not dan_icon:
        return ""

    badge_uri = art.rank(dan_icon, MATCH_RANK_SIZE)
    icon_html = (
        '<div style="width: 1.1rem; height: 1.1rem; margin: 0 auto; '
        f'{_image_bg(badge_uri, fit="contain", repeat=False)}"></div>'
        if badge_uri
        else ""
    )
    return (
        f'<div style="flex: 0 0 auto; width: {MATCH_COLS["dan"]}; text-align: center;">'
        f"{icon_html}"
        f'<div style="font-size: 0.55rem; color: {theme["desc_color"]}; line-height: 1.25; '
        f'margin-top: 0.04rem; {ELLIPSIS}">{dan_desc}</div>'
        "</div>"
    )


def _match_row(match: dict, theme: dict, art: _Assets, *, is_last: bool) -> str:
    # 行间不画分隔线，只用上下内边距拉出间距；最后一行去掉下内边距，
    # 否则那截 padding 会叠在卡片 padding 上，底部留白比顶部大一截。
    row_pad = "0.2rem 0 0" if is_last else "0.2rem 0"
    # 结果组：色条 + 文字（合成一个 flex，避免被 space-between 拉开）
    result_group = (
        '<div style="flex: 0 0 auto; display: flex; align-items: center; '
        f'gap: {MATCH_COLS["gap"]};">{_match_result_bar(match)}'
        f"{_match_result_text(match, theme)}</div>"
    )
    return (
        '<div style="display: flex; align-items: center; justify-content: space-between; '
        f'padding: {row_pad};">'
        f"{result_group}"
        f"{_match_hero(match, theme, art)}"
        f"{_match_kda(match, theme)}"
        f"{_match_mode(match, theme)}"
        f"{_match_dan(match, theme, art)}"
        "</div>"
    )


def _match_head(match_list_mod: dict, theme: dict) -> str:
    consec = match_list_mod.get("consecutive_info") or {}
    # 接口返回的标题是「我的比赛」，实际渲染的是查询对象（他/她）的视角，改为「Ta的比赛」
    title = str(match_list_mod.get("title") or "比赛")
    if title.startswith("我的比赛"):
        title = title.replace("我的比赛", "Ta的比赛", 1)

    badge = ""
    if consec.get("consecutive_num"):
        color = consec.get("consecutive_color") or WIN_COLOR
        badge = (
            f'<div style="font-size: 0.68rem; background-color: {color}; color: #fff; '
            'padding: 0.05rem 0.42rem; border-radius: 0.9rem; font-weight: 600;">'
            f"{_esc(consec.get('consecutive_num'))} "
            f"{_esc(consec.get('consecutive_desc'))}</div>"
        )
    return (
        '<div style="display: flex; align-items: center; justify-content: space-between; '
        'margin-bottom: 0.12rem;">'
        f"{_section_title(_esc(title), theme)}{badge}</div>"
    )


def _match_table_head(theme: dict) -> str:
    """列标题：与数据行共用 MATCH_COLS，宽度与 space-between 完全一致。"""
    return (
        '<div style="display: flex; justify-content: space-between; padding: 0 0 0.1rem; '
        f'font-size: 0.55rem; color: {theme["desc_color"]};">'
        f'<div style="width: {MATCH_RESULT_GROUP}; display: flex; align-items: center; '
        f'gap: {MATCH_COLS["gap"]};">'
        f'<div style="width: {MATCH_COLS["bar"]};"></div>'
        f'<div style="width: {MATCH_COLS["result"]}; text-align: center;">结果</div></div>'
        f'<div style="width: {MATCH_COLS["hero"]}; margin: 0 0.2rem; '
        'text-align: center;">英雄</div>'
        f'<div style="width: {MATCH_COLS["kda"]}; text-align: center;">KDA</div>'
        f'<div style="width: {MATCH_COLS["mode"]}; text-align: center;">模式 / 时长</div>'
        f'<div style="width: {MATCH_COLS["dan"]}; text-align: center;">对局段位</div>'
        "</div>"
    )


def _match_keep(matches: list[dict]) -> list[dict]:
    """过滤完全空白的条目：没有英雄图、没有模式、KDA 全零的占位行不渲染。"""
    kept = []
    for match in matches:
        kda = match.get("kda") or {}
        has_kda = any(
            str(kda.get(field) or "0") not in ("", "0") for field in ("kill", "death", "assist")
        )
        if match.get("hero_image") or match.get("hero_icon") or match.get("module") or has_kda:
            kept.append(match)
    return kept


def _match_rows(match_list_mod: dict, theme: dict, art: _Assets) -> str:
    """比赛列表：结果 / 英雄(宽幅图+等级+MVP) / KDA / 模式·时长 / 对局段位(勋章+文字)。"""
    matches = _match_keep(match_list_mod.get("match_list") or [])
    if not matches:
        return ""
    rows = [
        _match_row(match, theme, art, is_last=index == len(matches) - 1)
        for index, match in enumerate(matches)
    ]
    return _match_head(match_list_mod, theme) + _match_table_head(theme) + "".join(rows)


# ============================================================
# 常用英雄
# ============================================================
def _hero_avatar(icon: str, theme: dict) -> str:
    """头像不占满整格：2.1rem 的列里放 1.72rem 外框，内嵌 1.42rem 图标。"""
    return (
        '<div style="width: 1.72rem; height: 1.72rem; display: flex; '
        "align-items: center; justify-content: center; "
        f"border-radius: 0.3rem; background-color: {_img_placeholder_bg(theme)}; "
        'overflow: hidden;">'
        '<div style="width: 1.42rem; height: 1.42rem; border-radius: 0.24rem; '
        f'{_image_bg(icon)}"></div></div>'
    )


def _hero_win_rate(hero: dict, theme: dict) -> str:
    """胜率 + 进度条（超过 50% 绿，否则红）。"""
    rate = _percent(hero.get("win_rate"))
    bar_color = WIN_COLOR if rate >= 50 else LOSE_COLOR
    return (
        '<div style="min-width: 0;">'
        f'<div style="font-size: 0.8rem; font-weight: 600; color: {theme["title_color"]}; '
        f'line-height: 1.2; {NUMERIC}">{_esc(hero.get("win_rate") or "0%")}</div>'
        f'<div style="height: 0.26rem; border-radius: 0.13rem; margin-top: 0.14rem; '
        f'background-color: {theme["card_border"]}; overflow: hidden;">'
        f'<div style="width: {min(rate, 100):.1f}%; height: 100%; '
        f'background-color: {bar_color}; border-radius: 0.13rem;"></div></div></div>'
    )


def _hero_mmr(hero_mmr: dict, theme: dict) -> str:
    """MMR + 增减量 + 排名；没有 MMR 时整块留空。"""
    if not hero_mmr.get("mmr"):
        return ""
    delta = _to_int(hero_mmr.get("delta"))
    delta_color = WIN_COLOR if delta >= 0 else LOSE_COLOR
    delta_text = f"{delta:+d}" if delta else ""  # 0 时不显示「+0」
    # align-items: center 只对齐行盒；两处字号不同（0.8 / 0.54rem），
    # 字形墨迹在各自行盒里的偏移随字号成比例变化，仅对齐盒子肉眼仍错位。
    # 实测需把小的那个下移约 2px，这里用 translateY 补偿（随字号等比）。
    html = (
        '<div style="display: flex; align-items: center; justify-content: flex-start; '
        'gap: 0.12rem;">'
        f'<span style="font-size: 0.8rem; font-weight: 600; color: {theme["title_color"]}; '
        f'{NUMERIC}">{_esc(hero_mmr.get("mmr"))}</span>'
        f'<span style="font-size: 0.54rem; color: {delta_color}; '
        f'transform: translateY(0.105rem); {NUMERIC}">'
        f"{delta_text}</span></div>"
    )
    if hero_mmr.get("rank"):
        html += (
            f'<div style="font-size: 0.56rem; color: {theme["desc_color"]}; '
            f'text-align: left; margin-top: 0.05rem; {NUMERIC}">'
            f"{_esc(hero_mmr.get('rank'))}</div>"
        )
    return html


def _hero_kda(hero: dict, theme: dict) -> str:
    k, d, a, kd = parse_kda(hero.get("kda"))
    return (
        '<div style="text-align: center;">'
        f'<div style="font-size: 0.82rem; font-weight: 600; '
        f'color: {theme["title_color"]}; {NUMERIC}">{kd}</div>'
        f'<div style="font-size: 0.58rem; color: {theme["desc_color"]}; margin-top: 0.05rem; '
        f'{NUMERIC}">{k} / {d} / {a}</div></div>'
    )


def _hero_row(hero: dict, theme: dict, art: _Assets, *, is_last: bool) -> str:
    icon = art.hero_small(hero.get("hero_icon") or hero.get("hero_image") or "")
    # 行间不画分隔线（理由同比赛列表），仅靠内边距分隔。
    # 行高由 1.72rem 的头像撑起，上下内边距只决定「块与块之间的缝」，
    # 所以这里压到 0.08rem——再大就会让 5 行英雄显得松散。
    row_pad = "0.08rem 0.1rem 0" if is_last else "0.08rem 0.1rem"
    return (
        f'<div style="display: grid; {HERO_COLS} gap: 0.5rem; padding: {row_pad};">'
        f"{_hero_avatar(icon, theme)}"
        f'<div style="font-size: 0.74rem; color: {theme["title_color"]}; {ELLIPSIS}">'
        f"{_esc(hero.get('cn_name'))}</div>"
        f"{_hero_win_rate(hero, theme)}"
        f'<div style="text-align: center; font-size: 0.76rem; '
        f'color: {theme["desc_color"]}; {NUMERIC}">{_esc(hero.get("match_num"))}</div>'
        f"<div>{_hero_mmr(hero.get('hero_mmr') or {}, theme)}</div>"
        f"{_hero_kda(hero, theme)}"
        "</div>"
    )


def _hero_table_head(theme: dict) -> str:
    return (
        f'<div style="display: grid; {HERO_COLS} gap: 0.5rem; padding: 0 0.1rem 0.1rem; '
        f'font-size: 0.6rem; color: {theme["desc_color"]};">'
        "<div></div><div>英雄</div><div>胜率</div>"
        '<div style="text-align: center;">场次</div>'
        '<div style="text-align: center;">MMR / 排名</div>'
        '<div style="text-align: center;">KDA</div>'
        "</div>"
    )


def _hero_keep(heroes: list[dict]) -> list[dict]:
    """过滤空白条目：既无英雄名/图标、又无场次与胜率的占位行不渲染。"""
    return [
        hero
        for hero in heroes
        if (
            hero.get("cn_name")
            or hero.get("hero_icon")
            or hero.get("hero_image")
            or str(hero.get("match_num") or "").strip()
        )
    ]


def _hero_rows(common_hero: dict, theme: dict, art: _Assets) -> str:
    """常用英雄：英雄 / 胜率(进度条) / 场次 / MMR排名 / KDA。"""
    heroes = _hero_keep(common_hero.get("status_list") or [])
    if not heroes:
        return ""
    rows = [
        _hero_row(hero, theme, art, is_last=index == len(heroes) - 1)
        for index, hero in enumerate(heroes)
    ]
    return (
        _section_title("常用英雄", theme, margin_bottom="0.16rem")
        + _hero_table_head(theme)
        + "".join(rows)
    )


# ============================================================
# 生涯纪录
# ============================================================
def _career_cell(item: dict, theme: dict, art: _Assets) -> str:
    """单项纪录：图标 + 数值 + 说明。"""
    icon = art.hero_small(item.get("icon") or "")
    value = item.get("value") or ""
    desc = item.get("desc") or ""

    if icon:
        # 有图标时不再垫占位底色：接口给的是 32x32 带透明边的 PNG，
        # 垫上底色后透明区会显成一个方框，包住英雄头像。
        icon_html = (
            '<div style="width: 0.95rem; height: 0.95rem; flex: 0 0 auto; '
            f'{_image_bg(icon, fit="contain", repeat=False)}"></div>'
        )
    else:
        # 没有图标时用接口给的 color 占住图标位：连胜绿、连败红，
        # 比灰色空块更能说明「这一项本来就没有英雄图」。
        placeholder_bg = _argb_rgba(item.get("color")) or theme["card_border"]
        icon_html = (
            '<div style="width: 0.95rem; height: 0.95rem; flex: 0 0 auto; '
            f'border-radius: 0.2rem; background-color: {placeholder_bg};"></div>'
        )

    value_html = (
        f'<div style="font-size: 0.86rem; font-weight: 600; line-height: 1.15; '
        f'color: {theme["title_color"]}; {ELLIPSIS} {NUMERIC}">{_esc(value)}</div>'
        if str(value).strip()
        else ""
    )
    desc_margin = "margin-top: 0.06rem; " if value_html else ""
    desc_html = (
        f'<div style="font-size: 0.56rem; line-height: 1.2; {desc_margin}'
        f'color: {theme["desc_color"]}; {ELLIPSIS}">{_esc(desc)}</div>'
        if str(desc).strip()
        else ""
    )
    return (
        '<div style="display: flex; align-items: center; gap: 0.24rem; '
        'flex: 1 1 0; min-width: 0;">'
        f"{icon_html}"
        f'<div style="min-width: 0;">{value_html}{desc_html}</div></div>'
    )


def _career_rows(career: dict, theme: dict, art: _Assets) -> str:
    """生涯纪录：最高连胜 / 最高连败 / 各项最高记录，4 列等分，每行 4 项。"""
    items = [
        item
        for item in (career.get("career_record") or [])
        if str(item.get("value") or "").strip() or str(item.get("desc") or "").strip()
    ]
    if not items:
        return ""

    rows = []
    for start in range(0, len(items), CAREER_COLUMNS):
        chunk = items[start : start + CAREER_COLUMNS]
        cells = [_career_cell(item, theme, art) for item in chunk]
        # 空单元与真实单元等宽（flex 1 1 0 + 同一个 gap），
        # 这样残缺的最后一行也不会把已有单元拉宽、与上面几行错位。
        cells += [CAREER_SPACER] * (CAREER_COLUMNS - len(chunk))
        # 列之间只用间距分隔，不画竖线：竖线在高密度小格子里显得很碎，
        # 且与卡片自身 1px 边框、行分隔线叠在一起更乱。
        rows.append(
            '<div style="display: flex; align-items: flex-start; gap: 0.42rem;">'
            f"{''.join(cells)}</div>"
        )

    head = _section_title(_esc(career.get("title") or "生涯纪录"), theme, margin_bottom="0.5rem")
    body = (
        f'<div style="display: flex; flex-direction: column; gap: 0.42rem;">{"".join(rows)}</div>'
    )
    return head + body


# ============================================================
# 页面组装
# ============================================================
def _page(theme: dict, blocks: list[str]) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: {theme["container_bg"]};
                font-family: {FONT_FAMILY};
                font-size: {ROOT_FONT_PX}px;
                line-height: 1.5;
                -webkit-font-smoothing: antialiased; }}
</style>
</head>
<body>
<div id="container" style="display: flex; flex-direction: column; gap: 0.6rem;
     padding: 0.8rem 0.8rem 0.45rem;
     background-color: {theme["container_bg"]};
     width: {CONTAINER_REM}rem;">
 {"".join(blocks)}
</div>
</body>
</html>"""


def build_html(data: dict, theme_name: str, art: _Assets) -> str:
    """组装完整页面（同步）：玩家卡 → 近20场 → Ta的比赛 → 常用英雄 → 生涯纪录。"""
    theme = THEMES.get(theme_name) or THEME_LIGHT
    modules = ds.modules(data)

    recent = modules.get("recent_match") or {}
    r20 = modules.get("r20_data_stats") or {}

    # 「近20场最高」并入「近20场比赛」下方，不带标题，一行式。
    # 只有在胜负方块存在时才并入，否则会渲染出一张只有统计行、没有标题的怪卡片。
    # 上下之间只用留白分隔，不画横线（横线会把同一张卡片切成两块）。
    strip = _recent_strip(recent, theme, art)
    best = _recent_best(r20, theme, art) if (r20 and strip) else ""
    best_html = f'<div style="margin-top: 0.45rem;">{best}</div>' if best else ""

    blocks = [
        _player_card(
            (modules.get("header") or {}).get("header_info") or {},
            modules.get("mmr_stats") or {},
            theme,
            art,
        ),
        _card_if(strip + best_html, theme),
        _card_if(_match_rows(modules.get("match_list") or {}, theme, art), theme),
        _card_if(_hero_rows(modules.get("common_hero") or {}, theme, art), theme),
        _card_if(_career_rows(modules.get("career_record") or {}, theme, art), theme),
    ]
    return _page(theme, blocks)


# ============================================================
# 渲染
# ============================================================
async def _screenshot(html: str) -> bytes:
    """在共享页面上渲染 HTML 并截取 #container（超采样由 context 的倍率决定）。"""
    async with shared_browser.rendering(SUPERSAMPLE) as page:
        await page.set_content(html, wait_until="load")
        return await page.locator("#container").screenshot()


async def render(data: dict, steam_id, theme: str, out_path: str) -> str:
    """把 overview 数据渲染成 PNG 并落盘，返回输出路径。"""
    art = await _prefetch_assets(ds.modules(data))
    html = build_html(data, theme, art)
    png = await _screenshot(html)

    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    img = img.resize((w // SUPERSAMPLE, h // SUPERSAMPLE), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    img.save(out_path)
    logger.info(f"生成小黑盒战绩图：steam_id={steam_id}（{theme}）")
    return out_path


async def generate_image(steam_id, theme: str | None = None, refresh: bool = False) -> str:
    """拉取玩家小黑盒战绩并渲染 PNG，返回本地图片路径。

    theme 留空时使用配置项 d2w_image_theme（默认 'light'）；
    refresh=True 时忽略数据与图片缓存强制重拉重渲染。
    查询失败 / 无战绩时抛 ds.OverviewError（供上层转为用户提示）。
    """
    theme = normalize_image_theme(theme)
    data = await ds.fetch_overview(steam_id, refresh=refresh)
    if not ds.has_any_content(data):
        # 接口自带说明（如「该玩家未产生战绩」）时优先用它，否则给出通用提示
        raise ds.OverviewError(
            ds.notice_message(data) or f"未查询到玩家战绩（steam_id={steam_id}）"
        )

    # 文件名带上风格，避免切换 d2w_image_theme 后仍命中另一风格的旧缓存图
    out_path = os.path.join(OUTPUT_DIR, f"xiaoheihe_overview_{int(steam_id)}_{theme}.png")

    # 图片缓存：战绩时效性强，缓存期与数据缓存一致（默认 1 小时）
    if (
        not refresh
        and os.path.exists(out_path)
        and time.time() - os.path.getmtime(out_path) <= IMAGE_CACHE_SECONDS
    ):
        logger.info(f"小黑盒战绩图缓存命中：{out_path}")
        return out_path

    return await render(data, steam_id, theme, out_path)


__all__ = ["build_html", "generate_image", "render", "THEME_DARK", "THEME_LIGHT"]
