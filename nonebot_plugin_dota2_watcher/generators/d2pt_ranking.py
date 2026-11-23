"""D2PT 五位置排行图：HTML + Playwright 渲染。

五列（1-5 号位），每列综合评分前 15；数量不足时各列统一截断。
主题与容器规范复用 core_build.THEMES。
"""

from __future__ import annotations

import asyncio
import html

from nonebot.log import logger

from .. import assets
from ..config import DATA_DIR, OUTPUT_DIR, normalize_image_theme
from ..utils import loadjson
from . import html_render
from .core_build import FONT_FAMILY, THEMES
from .hero_pool import _ICON_PATHS
from .hero_pool import THEMES as ICON_THEMES

ROOT_FONT_PX = 19.2  # 1rem 的像素值，与 /出装、/战绩 一致
CONTAINER_REM = 46  # 五列总览的容器宽度（rem），按内容收紧，避免线优右侧留白
SINGLE_CONTAINER_REM = 28  # 单位置图片的容器宽度（rem）
OUTER_PADDING_REM = 0.6  # 图片最外层留白（rem），避免卡片贴边
SINGLE_COLUMNS = 3  # 单位置图片的网格列数
ROWS_PER_POSITION = 15  # 五列总览中每列显示的英雄数上限
SUPERSAMPLE = 2  # 先按 2x 渲染再缩回，边缘与文字更细腻

# 位置 -> 列标题
POSITIONS = {
    1: "优势路",
    2: "中路",
    3: "劣势路",
    4: "半辅助",
    5: "纯辅助",
}

# 线优正负着色（与主题的胜率绿保持一致，负值用红）
LANE_COLORS = {
    "light": {"pos": "#2E9B64", "neg": "#D9534F"},
    "dark": {"pos": "#7CC45C", "neg": "#E06C75"},
}


def _esc(value) -> str:
    """HTML 转义（英雄名来自本地字典，仍统一处理）。"""
    return html.escape(str(value))


def _hero_short_map() -> dict[int, str]:
    """英雄 ID -> Steam CDN 短名（OpenDota 缓存 heroes.json，缺失时返回空表）。"""
    cached = loadjson(DATA_DIR / "heroes.json")
    result: dict[int, str] = {}
    for key, value in (cached or {}).items():
        try:
            result[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return result


def _position_icon_svg(pos: int, theme: str, size: str = "1.05rem") -> str:
    """位置图标：内联 SVG（路径与 dota2protracker 位置图标集一致，随主题换色）。"""
    icons = ICON_THEMES[theme]["icons"]
    defs: list[str] = []
    paths: list[str] = []
    for index, (path_d, fill_key, opacity) in enumerate(_ICON_PATHS[pos]):
        fill = icons[fill_key]
        if isinstance(fill, tuple):
            (start, end), (x1, y1, x2, y2) = fill
            gradient_id = f"d2pt-pos{pos}-{index}"
            defs.append(
                f'<linearGradient id="{gradient_id}" x1="{x1}" y1="{y1}" '
                f'x2="{x2}" y2="{y2}" gradientUnits="userSpaceOnUse">'
                f'<stop stop-color="{start}"></stop>'
                f'<stop offset="1" stop-color="{end}"></stop></linearGradient>'
            )
            fill_attr = f"url(#{gradient_id})"
        else:
            fill_attr = fill
        paths.append(
            f'<path fill-rule="evenodd" clip-rule="evenodd" d="{path_d}" '
            f'fill="{fill_attr}" fill-opacity="{opacity}"></path>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'style="width:{size};height:{size};flex:none;">'
        f"<defs>{''.join(defs)}</defs>{''.join(paths)}</svg>"
    )


async def _hero_icon_uri(short: str) -> tuple[str, str]:
    """英雄图标 -> data URI（裁边居中后的 32×32 缓存图）。"""
    path = await assets.ensure_hero_icon_32(short)
    if not path:
        return short, ""
    return short, assets.file_data_uri(path)


def _hero_short(item: dict, short_map: dict[int, str]) -> str:
    """取英雄短名：优先数据里的 npc，旧缓存回退 heroes.json。"""
    npc = item.get("npc")
    if npc:
        return str(npc)
    try:
        return short_map.get(int(item.get("id")), "")
    except (TypeError, ValueError):
        return ""


def _row_count(data: dict) -> int:
    """统一每列的行数：取各位置可用数量的最小值，最多 ROWS_PER_POSITION。"""
    counts = [len(data.get(str(pos)) or []) for pos in POSITIONS]
    counts = [count for count in counts if count > 0]
    if not counts:
        return 0
    return min(ROWS_PER_POSITION, min(counts))


def _lane_text(item: dict) -> str:
    """线优显示：正值补 '+'（数据里正数不带符号）。"""
    lane = str(item.get("lane") or "").strip()
    if not lane:
        return "-"
    return lane if lane.startswith(("-", "+")) else f"+{lane}"


def _row_html(
    item: dict,
    theme: dict,
    lane_colors: dict,
    icons: dict[str, str],
    short_map: dict[int, str],
    *,
    is_last: bool,
) -> str:
    """单个英雄行：英雄图标 + 名字 + 胜率 + 线优（数值行对齐）。"""
    short = _hero_short(item, short_map)
    image_uri = icons.get(short, "")
    lane = _lane_text(item)
    lane_color = lane_colors["neg"] if lane.startswith("-") else lane_colors["pos"]
    border = "" if is_last else f"border-bottom: 0.05rem solid {theme['card_border']};"
    image_bg = f"background-image: url('{image_uri}');" if image_uri else ""
    return (
        '<div style="display:flex;align-items:center;gap:0.3rem;'
        f'padding:0.14rem 0.3rem;{border}">'
        '<div style="width:1.56rem;height:1.56rem;flex:none;border-radius:0.22rem;'
        f"overflow:hidden;{image_bg}"
        'background-size:cover;background-position:center;"></div>'
        '<div style="flex:1;min-width:0;display:flex;flex-direction:column;">'
        '<div style="font-size:0.74rem;font-weight:400;line-height:1.15;'
        f"color:{theme['title_color']};white-space:nowrap;overflow:hidden;"
        f'text-overflow:ellipsis;">{_esc(item.get("name", ""))}</div>'
        '<div style="display:flex;align-items:baseline;gap:0.28rem;'
        "margin-top:0.04rem;font-size:0.6rem;line-height:1.2;"
        f"color:{theme['desc_color']};font-variant-numeric:tabular-nums;"
        'white-space:nowrap;">'
        f'<span>胜率 <span style="font-weight:500;color:{theme["title_color"]};">'
        f"{_esc(item.get('WR', '-'))}</span></span>"
        f'<span>线优 <span style="font-weight:500;color:{lane_color};">'
        f"{_esc(lane)}</span></span>"
        "</div></div></div>"
    )


def _column_html(
    pos: int,
    items: list[dict],
    row_count: int,
    theme_name: str,
    theme: dict,
    icons: dict[str, str],
    short_map: dict[int, str],
) -> str:
    """单个位置列：图标 + 列名 + 数据更新时间 + 英雄行。"""
    accent = ICON_THEMES[theme_name]["position_colors"][pos]
    lane_colors = LANE_COLORS[theme_name]
    heroes = items[:row_count]
    updated_at = next(
        (str(item.get("updated_at")) for item in items if item.get("updated_at")),
        "",
    )
    date_part, _, time_part = updated_at.partition(" ")
    updated_html = (
        '<div style="margin-left:auto;text-align:right;line-height:1.15;'
        f"font-size:0.55rem;color:{theme['desc_color']};"
        'font-variant-numeric:tabular-nums;white-space:nowrap;">'
        f"<div>{_esc(date_part)}</div>"
        + (f"<div>{_esc(time_part)}</div>" if time_part else "")
        + "</div>"
        if updated_at
        else ""
    )
    rows = "".join(
        _row_html(
            item,
            theme,
            lane_colors,
            icons,
            short_map,
            is_last=index == len(heroes) - 1,
        )
        for index, item in enumerate(heroes)
    )
    return (
        '<div style="display:flex;flex-direction:column;background-color:'
        f"{theme['card_bg']};border:0.05rem solid {theme['card_border']};"
        f'border-top:0.12rem solid {accent};border-radius:0.3rem;overflow:hidden;">'
        '<div style="display:flex;align-items:center;gap:0.25rem;'
        f'padding:0.3rem;border-bottom:0.05rem solid {theme["card_border"]};">'
        f"{_position_icon_svg(pos, theme_name)}"
        f'<span style="font-size:0.84rem;font-weight:600;color:{theme["title_color"]};'
        'white-space:nowrap;">'
        f"{POSITIONS[pos]}</span>"
        f"{updated_html}"
        "</div>"
        f"{rows}"
        "</div>"
    )


def build_html(
    data: dict,
    theme_name: str,
    icons: dict[str, str],
    short_map: dict[int, str],
) -> str:
    """组装完整页面（同步）：五列排行。"""
    theme = THEMES.get(theme_name) or THEMES["light"]
    row_count = _row_count(data)
    columns = "".join(
        _column_html(
            pos,
            data.get(str(pos)) or [],
            row_count,
            theme_name,
            theme,
            icons,
            short_map,
        )
        for pos in POSITIONS
    )
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
<div id="container" style="display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
     gap: 0.3rem; background-color: {theme["container_bg"]};
     padding: {OUTER_PADDING_REM}rem;
     width: {CONTAINER_REM + OUTER_PADDING_REM * 2}rem;">
  {columns}
</div>
</body>
</html>"""


def build_position_html(
    data: dict,
    pos: int,
    theme_name: str,
    icons: dict[str, str],
    short_map: dict[int, str],
) -> str:
    """组装单位置完整排行页（同步）：标题 + 三列英雄卡片。"""
    items = data.get(str(pos)) or []
    if not items:
        return ""
    theme = THEMES.get(theme_name) or THEMES["light"]
    lane_colors = LANE_COLORS[theme_name]
    cards = "".join(
        '<div style="background-color:'
        f"{theme['card_bg']};border:0.05rem solid {theme['card_border']};"
        'border-radius:0.28rem;overflow:hidden;">'
        + _row_html(item, theme, lane_colors, icons, short_map, is_last=True)
        + "</div>"
        for item in items
    )
    icon = _position_icon_svg(pos, theme_name, size="1.2rem")
    updated_at = next(
        (str(item.get("updated_at")) for item in items if item.get("updated_at")),
        "",
    )
    meta_html = (
        '<span style="margin-left: auto; font-size: 0.6rem; '
        f'color: {theme["desc_color"]};">{_esc(updated_at)}</span>'
        if updated_at
        else ""
    )
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
<div id="container" style="display: flex; flex-direction: column; gap: 0.35rem;
     background-color: {theme["container_bg"]};
     padding: {OUTER_PADDING_REM}rem;
     width: {SINGLE_CONTAINER_REM + OUTER_PADDING_REM * 2}rem;">
  <div style="display: flex; align-items: center; gap: 0.35rem;">
    {icon}
    <span style="font-size: 1.05rem; font-weight: 600; color: {theme["title_color"]};">
      {POSITIONS[pos]}
    </span>
    {meta_html}
  </div>
  <div style="display: grid; grid-template-columns: repeat({SINGLE_COLUMNS}, minmax(0, 1fr));
       gap: 0.25rem;">
    {cards}
  </div>
</div>
</body>
</html>"""


async def generate_image(data: dict, theme: str | None = None, pos: str | int | None = None) -> str:
    """按解析后的位置数据渲染排行图，返回本地路径；数据为空时返回空串。

    pos 为空时输出五列总览；指定 1-5 时输出该位置全部满足条件的英雄。
    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    """
    if not data:
        return ""
    theme = normalize_image_theme(theme)
    short_map = _hero_short_map()

    if pos is not None:
        try:
            position = int(pos)
        except (TypeError, ValueError):
            return ""
        items = data.get(str(position)) or []
        if position not in POSITIONS or not items:
            return ""
        shorts = {_hero_short(item, short_map) for item in items}
        shorts.discard("")
        results = await asyncio.gather(*(_hero_icon_uri(short) for short in shorts))
        icons = dict(results)
        document = build_position_html(data, position, theme, icons, short_map)
        out_name = f"d2pt_pos{position}_{theme}.png"
    else:
        row_count = _row_count(data)
        if not row_count:
            return ""
        shorts: set[str] = set()
        for p in POSITIONS:
            for item in (data.get(str(p)) or [])[:row_count]:
                short = _hero_short(item, short_map)
                if short:
                    shorts.add(short)
        results = await asyncio.gather(*(_hero_icon_uri(short) for short in shorts))
        icons = dict(results)
        document = build_html(data, theme, icons, short_map)
        out_name = f"d2pt_positions_{theme}.png"

    if not document:
        return ""
    out_path = OUTPUT_DIR / out_name
    await html_render.render_to_png(document, str(out_path), SUPERSAMPLE)
    logger.info(f"已生成 D2PT 排行图：{out_path}")
    return str(out_path)
