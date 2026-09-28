"""开黑记录图片生成器：渲染「XXX的开黑记录」列表图（HTML + Playwright，风格同 /出装）。

数据来源见 ../datasources/playmates.py（STRATZ 同队 peers 聚合，按共同场次降序取前 20）。

样式复用 /出装（core_build.py）的主题与容器规范：同一套 FONT_FAMILY / THEME，
外层 #container 宽度 28.125rem、圆角卡片行、描述文字用 desc_color，
胜率用胜/负配色（绿 / 红）突出。每行形如：

    {头像} {名字} {场次} {胜率}

头像为方形（2rem，轻微圆角），标题旁头像与行内头像保持一致。
"""

from __future__ import annotations

import base64
import io
import os
import time

from nonebot.log import logger

from ..config import OUTPUT_DIR, config, normalize_image_theme
from ..datasources import playmates as ds
from ..datasources.hero_pool import load_avatar_img
from . import shared_browser
from .core_build import FONT_FAMILY, THEME_DARK, THEME_LIGHT, THEMES

# 头像渲染尺寸（像素）：2rem ≈ 38px，超采样 2x 后取 96px 源图足够清晰
AVATAR_PX = 96

# 头像展示尺寸与圆角：方形（非正圆），保留轻微圆角避免边角生硬
AVATAR_SIZE = "2rem"
AVATAR_RADIUS = "0.1875rem"

# 胜率配色（绿=不亏，红=偏负）；与出装图的胜率绿色保持一致
WIN_COLOR = "#7cc45c"
LOSE_COLOR = "#d9534f"

IMAGE_CACHE_SECONDS = config.d2w_core_build_image_cache_seconds


async def _avatar_data_uri(url: str) -> str:
    """把玩家头像 URL 转成内嵌 data URI（复用 hero_pool 的下载/磁盘缓存）。

    失败或 URL 为空时返回空串，调用方渲染占位底色。
    """
    if not url:
        return ""
    try:
        img = await load_avatar_img(url)
        if img is None:
            return ""
        from PIL import Image

        thumb = img.resize((AVATAR_PX, AVATAR_PX), Image.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning(f"开黑头像处理失败：{e}")
        return ""


def _row_html(row: dict, avatar_uri: str, theme: dict) -> str:
    """构建单行「头像 + 名字 + 场次 + 胜率」卡片 HTML。"""
    games = int(row.get("games") or 0)
    wins = int(row.get("wins") or 0)
    rate = round(wins * 100 / games) if games else 0
    rate_color = WIN_COLOR if rate >= 50 else LOSE_COLOR

    if avatar_uri:
        avatar_css = f"background-image: url('{avatar_uri}'); background-size: cover;"
    else:
        # 无头像时用卡片底色占位，避免出现空白洞
        avatar_css = f"background-color: {theme['talent_circle_bg']};"

    name = str(row.get("name") or "").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<div style="display: flex; align-items: center; gap: 0.5625rem; '
        f"background-color: {theme['card_bg']}; "
        f"border: 0.09375rem solid {theme['card_border']}; "
        'border-radius: 0.5625rem; padding: 0.46875rem 0.65625rem;">'
        # 头像（方形 + 轻微圆角）
        f'<div style="flex: 0 0 auto; width: {AVATAR_SIZE}; height: {AVATAR_SIZE}; '
        f"border-radius: {AVATAR_RADIUS}; "
        f'{avatar_css} background-position: center;"></div>'
        # 名字（过长省略）
        f'<div style="flex: 1 1 auto; min-width: 0; overflow: hidden; '
        "text-overflow: ellipsis; white-space: nowrap; "
        f'font-size: 1rem; color: {theme["title_color"]};">{name}</div>'
        # 共同场次
        f'<div style="flex: 0 0 auto; font-size: 0.9375rem; color: {theme["desc_color"]}; '
        f'white-space: nowrap;">{games} 场</div>'
        # 胜率（绿/红）
        f'<div style="flex: 0 0 auto; width: 3.125rem; text-align: right; '
        f"font-size: 0.9375rem; font-weight: 500; color: {rate_color}; "
        f'white-space: nowrap;">{rate}%</div>'
        "</div>"
    )


def build_html(
    player_name: str,
    rows: list[dict],
    avatar_uris: list[str],
    player_avatar_uri: str = "",
    theme: dict = THEME_LIGHT,
) -> str:
    """构建完整的开黑记录 HTML 页面。

    rows 为已截断的展示行（已按共同场次降序）。
    """
    head = f"{player_name}的开黑记录"

    title_avatar = ""
    if player_avatar_uri:
        title_avatar = (
            f'<div style="flex: 0 0 auto; width: {AVATAR_SIZE}; height: {AVATAR_SIZE}; '
            f"border-radius: {AVATAR_RADIUS}; "
            f"background-image: url('{player_avatar_uri}'); background-size: cover; "
            'background-position: center;"></div>'
        )

    row_html = "".join(_row_html(row, uri, theme) for row, uri in zip(rows, avatar_uris))

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: {theme["container_bg"]};
                font-family: {FONT_FAMILY};
                font-size: 19.2px;
                line-height: 1.5; }}
</style>
</head>
<body>
<div id="container" style="display: flex; flex-direction: column; gap: 0.75rem;
     padding: 1.5rem;
     background-color: {theme["container_bg"]};
     border: 0.09375rem solid {theme["container_border"]};
     width: 28.125rem;">
  <div style="display: flex; align-items: center; gap: 0.5625rem;">
    {title_avatar}
    <div style="font-weight: 500; font-size: 1.5rem;
         color: {theme["title_color"]};">{head}</div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 0.375rem;">
    {row_html}
  </div>
</div>
</body>
</html>"""
    return html


async def generate_image(steam_id, theme: str | None = None, refresh: bool = False) -> str:
    """拉取开黑记录并渲染 PNG，返回本地图片路径。

    theme 留空时使用配置项 d2w_image_theme（默认 'light'）。
    数据抓取失败时抛 ds.PlaymatesError（供上层转为用户提示）。
    """
    player_name, player_avatar, rows = await ds.fetch_playmates(steam_id)
    if not rows:
        raise ds.PlaymatesError(f"未查询到 {player_name} 的开黑记录")

    theme = normalize_image_theme(theme)
    top = rows[: ds.OUTPUT_LIMIT]
    avatar_uris = [await _avatar_data_uri(r.get("avatar") or "") for r in top]
    player_avatar_uri = await _avatar_data_uri(player_avatar)

    theme_dict = THEMES.get(theme, THEME_LIGHT)
    html = build_html(player_name, top, avatar_uris, player_avatar_uri, theme_dict)

    # 文件名带上风格，避免切换 d2w_image_theme 后仍命中另一风格的旧缓存图
    out_path = os.path.join(OUTPUT_DIR, f"playmates_{int(steam_id)}_{theme}.png")

    # 图片缓存：缓存期内复用已生成的图片，避免重复渲染
    if (
        not refresh
        and os.path.exists(out_path)
        and time.time() - os.path.getmtime(out_path) <= IMAGE_CACHE_SECONDS
    ):
        logger.info(f"开黑记录图片缓存命中：{out_path}")
        return out_path

    supersample = 2
    page = await shared_browser.get_page(supersample)
    await page.set_content(html, wait_until="load")
    await page.wait_for_timeout(50)
    png_bytes = await page.locator("#container").screenshot()

    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    img = img.resize((w // supersample, h // supersample), Image.LANCZOS)
    img.save(out_path)
    logger.info(f"生成开黑记录：{player_name}（{steam_id}）共 {len(rows)} 位，展示前 {len(top)} 位")
    return out_path


__all__ = ["build_html", "generate_image", "THEME_DARK", "THEME_LIGHT"]
