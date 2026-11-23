"""加速模式胜率图：HTML + Playwright 渲染。

按胜率降序排列英雄，布局与 /d2pt 一致（标题 + 五列英雄卡片网格）。
"""

from __future__ import annotations

import asyncio
import html

from nonebot.log import logger

from .. import assets
from ..config import OUTPUT_DIR, normalize_image_theme
from . import html_render
from .core_build import FONT_FAMILY, THEMES

ROOT_FONT_PX = 19.2  # 1rem 的像素值，与 /d2pt、/出装 一致
COLUMNS = 5  # 卡片网格列数
GAP_REM = 0.25  # 卡片之间的间距（rem）
SUPERSAMPLE = 2  # 先按 2x 渲染再缩回，边缘与文字更细腻

# 卡片内部布局尺寸（rem）：用于按最长英雄名计算单格宽度
ICON_REM = 1.56  # 英雄图标边长
ICON_GAP_REM = 0.3  # 图标与文字的间距
PADDING_REM = 0.3  # 卡片左右内边距
NAME_FONT_REM = 0.74  # 英雄名字号（中文字宽约等于字号）
WR_MIN_REM = 3.9  # 「胜率 100.00%」一行所需宽度，通常比短英雄名更宽
WIDTH_BUFFER_REM = 0.15  # 预留字体渲染误差

# 标题图标与强调色（胜率绿）
ACCENTS = {"light": "#2E9B64", "dark": "#7CC45C"}


def _esc(value) -> str:
    """HTML 转义（英雄名来自本地字典，仍统一处理）。"""
    return html.escape(str(value))


def _bolt_svg(color: str, size: str = "1.2rem") -> str:
    """标题图标：加速模式闪电（内联 SVG，随主题换色）。"""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'style="width:{size};height:{size};flex:none;">'
        f'<path d="M13 2 4.5 13.5H11l-1 8.5L18.5 10H12l1-8z" fill="{color}"></path></svg>'
    )


async def _hero_icon_uri(short: str) -> tuple[str, str]:
    """英雄图标 -> data URI（裁边居中后的 32×32 缓存图）。"""
    path = await assets.ensure_hero_icon_32(short)
    if not path:
        return short, ""
    return short, assets.file_data_uri(path)


def _row_html(item: dict, theme: dict, icons: dict[str, str]) -> str:
    """单个英雄行：英雄图标 + 名字 + 胜率（数值行对齐）。"""
    image_uri = icons.get(str(item.get("npc") or ""), "")
    image_bg = f"background-image: url('{image_uri}');" if image_uri else ""
    return (
        '<div style="display:flex;align-items:center;gap:0.3rem;padding:0.14rem 0.3rem;">'
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
        "</div></div></div>"
    )


def _card_width_rem(heroes: list[dict]) -> float:
    """按展示英雄的最长名字估算单格宽度，避免名字右侧出现大片空白。

    名字与胜率行取较宽者：中文字宽约等于字号，胜率行宽度按最长的
    「胜率 100.00%」估算；再加上图标、间距与内边距。
    """
    name_rem = max(
        (len(str(item.get("name") or "")) * NAME_FONT_REM for item in heroes),
        default=0,
    )
    text_rem = max(name_rem, WR_MIN_REM)
    return round(ICON_REM + ICON_GAP_REM + text_rem + PADDING_REM * 2 + WIDTH_BUFFER_REM, 2)


def _card_html(item: dict, theme: dict, icons: dict[str, str]) -> str:
    """单个英雄卡片（与 /d2pt 单位置图的卡片一致）。"""
    return (
        '<div style="background-color:'
        f"{theme['card_bg']};border:0.05rem solid {theme['card_border']};"
        'border-radius:0.28rem;overflow:hidden;">'
        + _row_html(item, theme, icons)
        + "</div>"
    )


def build_html(data: dict, theme_name: str, icons: dict[str, str]) -> str:
    """组装完整页面（同步）：标题 + 五列英雄卡片网格。"""
    theme = THEMES.get(theme_name) or THEMES["light"]
    accent = ACCENTS[theme_name]
    heroes = data.get("heroes") or []
    card_width = _card_width_rem(heroes)
    container_rem = round(COLUMNS * card_width + (COLUMNS - 1) * GAP_REM, 2)
    cards = "".join(_card_html(item, theme, icons) for item in heroes)

    updated_at = str(data.get("updated_at") or "")
    date_part, _, time_part = updated_at.partition(" ")
    meta_html = (
        '<div style="margin-left:auto;text-align:right;line-height:1.15;'
        f"font-size:0.55rem;color:{theme['desc_color']};"
        'font-variant-numeric:tabular-nums;white-space:nowrap;">'
        f"<div>{_esc(date_part)}</div>"
        + (f"<div>{_esc(time_part)}</div>" if time_part else "")
        + "</div>"
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
     background-color: {theme["container_bg"]}; width: {container_rem}rem;">
  <div style="display: flex; align-items: center; gap: 0.35rem; padding: 0 0.05rem;">
    {_bolt_svg(accent)}
    <span style="font-size: 1.05rem; font-weight: 600; color: {theme["title_color"]};">
      加速模式胜率
    </span>
    {meta_html}
  </div>
  <div style="display: grid; grid-template-columns: repeat({COLUMNS}, {card_width}rem);
       gap: {GAP_REM}rem;">
    {cards}
  </div>
</div>
</body>
</html>"""


async def generate_image(data: dict, theme: str | None = None) -> str:
    """渲染加速模式胜率图，返回本地路径；数据为空时返回空串。

    theme 留空时使用配置项 d2w_image_theme（默认 light）。
    """
    heroes = (data or {}).get("heroes") or []
    if not heroes:
        return ""
    theme = normalize_image_theme(theme)
    shorts = {str(item.get("npc") or "") for item in heroes}
    shorts.discard("")
    results = await asyncio.gather(*(_hero_icon_uri(short) for short in shorts))
    icons = dict(results)

    out_path = OUTPUT_DIR / f"turbo_wr_{theme}.png"
    await html_render.render_to_png(build_html(data, theme, icons), str(out_path), SUPERSAMPLE)
    logger.info(f"已生成加速模式胜率图：{out_path}")
    return str(out_path)
