"""职业选手对战记录图片生成器：渲染「XXX与职业选手的对战记录」列表图（HTML + Playwright，风格同 /出装）。

数据来源见 ../datasources/pro_peers.py（STRATZ + OpenDota 双源互补、Liquipedia 校验，
按队友+对手总场次降序取前 10 位）。

样式复用 /开黑（playmates.py）的主题与容器规范：同一套 FONT_FAMILY / THEME，
外层 #container 宽度 28.125rem、圆角卡片行，头像为方形（2rem，轻微圆角），
队友/对手战绩用胜/负配色（绿 / 红）突出。每行形如：

    {头像} {选手名 (游戏昵称)}                 {最近同局日期}
           队友 {胜/总} · 对手 {胜/总}

日期为「上次共同对局」时间（无记录显示「未知」）；不再展示比赛 ID，
因此无需调用 attach_last_match_ids（省去每位选手一次窗口查询）。
"""

from __future__ import annotations

import io
import os
import time
from datetime import datetime

from nonebot.log import logger

from ..config import OUTPUT_DIR, config, normalize_image_theme
from ..datasources import pro_peers as ds
from . import shared_browser
from .core_build import FONT_FAMILY, THEME_DARK, THEME_LIGHT, THEMES
from .playmates import _avatar_data_uri

# 头像渲染尺寸（像素）：2rem ≈ 38px，超采样 2x 后取 96px 源图足够清晰
AVATAR_PX = 96

# 头像展示尺寸与圆角：方形（非正圆），保留轻微圆角避免边角生硬
AVATAR_SIZE = "2rem"
AVATAR_RADIUS = "0.1875rem"

# 战绩配色（绿=不亏，红=偏负）；与开黑图的胜率配色保持一致
WIN_COLOR = "#7cc45c"
LOSE_COLOR = "#d9534f"

IMAGE_CACHE_SECONDS = config.d2w_core_build_image_cache_seconds


def _rate_color(win: int, total: int, theme: dict) -> str:
    """按胜率取配色：无记录用描述色，胜率≥50% 绿、否则红。"""
    if total <= 0:
        return theme["desc_color"]
    return WIN_COLOR if win * 100 / total >= 50 else LOSE_COLOR


def _row_html(row: dict, avatar_uri: str, theme: dict) -> str:
    """构建单行「头像 + 选手名 + 队友/对手战绩 + 日期」卡片 HTML。"""
    if avatar_uri:
        avatar_css = f"background-image: url('{avatar_uri}'); background-size: cover;"
    else:
        # 无头像时用卡片底色占位 + 描边，避免在浅色底上出现看不见的空白洞
        avatar_css = (
            f"background-color: {theme['talent_circle_bg']}; "
            f"border: 0.0625rem solid {theme['card_border']};"
        )

    name = str(row.get("name") or "").replace("<", "&lt;").replace(">", "&gt;")
    # 括号内显示玩家游戏内昵称；与选手名相同或为空时省略
    nickname = str(row.get("nickname") or "").replace("<", "&lt;").replace(">", "&gt;")
    tag = (
        f'<span style="font-size: 0.875rem; color: {theme["desc_color"]};"> ({nickname})</span>'
        if nickname and nickname != name
        else ""
    )

    games_with = int(row.get("with") or 0)
    games_against = int(row.get("against") or 0)
    with_color = _rate_color(int(row.get("with_win") or 0), games_with, theme)
    against_color = _rate_color(int(row.get("against_win") or 0), games_against, theme)
    last = row.get("last") or 0
    date_text = datetime.fromtimestamp(last).strftime("%Y/%m/%d") if last else "未知"

    return (
        '<div style="display: flex; align-items: center; gap: 0.5625rem; '
        f"background-color: {theme['card_bg']}; "
        f"border: 0.09375rem solid {theme['card_border']}; "
        'border-radius: 0.5625rem; padding: 0.46875rem 0.65625rem;">'
        # 头像（方形 + 轻微圆角）
        f'<div style="flex: 0 0 auto; width: {AVATAR_SIZE}; height: {AVATAR_SIZE}; '
        f"border-radius: {AVATAR_RADIUS}; "
        f'{avatar_css} background-position: center;"></div>'
        # 选手名 + 战绩两行
        '<div style="flex: 1 1 auto; min-width: 0; display: flex; '
        'flex-direction: column; gap: 0.0625rem;">'
        '<div style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; '
        f'font-size: 1rem; color: {theme["title_color"]};">{name}{tag}</div>'
        f'<div style="font-size: 0.875rem; color: {theme["desc_color"]}; '
        'white-space: nowrap;">队友 '
        f'<span style="color: {with_color};">{ds._fmt_games(row.get("with_win") or 0, games_with)}</span>'
        " · 对手 "
        f'<span style="color: {against_color};">{ds._fmt_games(row.get("against_win") or 0, games_against)}</span>'
        "</div></div>"
        # 最近同局日期
        f'<div style="flex: 0 0 auto; font-size: 0.8125rem; color: {theme["desc_color"]}; '
        f'white-space: nowrap;">{date_text}</div>'
        "</div>"
    )


def build_html(
    player_name: str,
    rows: list[dict],
    avatar_uris: list[str],
    player_avatar_uri: str = "",
    total: int = 0,
    theme: dict = THEME_LIGHT,
) -> str:
    """构建完整的职业选手对战记录 HTML 页面。

    rows 为已截断的展示行（已按总场次降序）；total 为过滤后的选手总数，
    超过展示条数时在标题旁标注「共 N 位」。
    """
    head = f"{player_name}与职业选手的对战记录"
    count_text = ""
    if total > len(rows):
        count_text = (
            f'<span style="font-size: 0.875rem; color: {theme["desc_color"]};">'
            f"共 {total} 位，仅展示前 {len(rows)} 位</span>"
        )

    title_avatar = ""
    if player_avatar_uri:
        title_avatar = (
            f'<div style="flex: 0 0 auto; width: {AVATAR_SIZE}; height: {AVATAR_SIZE}; '
            f"border-radius: {AVATAR_RADIUS}; "
            f"background-image: url('{player_avatar_uri}'); background-size: cover; "
            'background-position: center;"></div>'
        )

    row_html = "".join(_row_html(row, uri, theme) for row, uri in zip(rows, avatar_uris))

    return f"""<!DOCTYPE html>
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
  <div style="display: flex; flex-direction: column; gap: 0.25rem;">
    <div style="display: flex; align-items: center; gap: 0.5625rem;">
      {title_avatar}
      <div style="font-weight: 500; font-size: 1.5rem;
           color: {theme["title_color"]};">{head}</div>
    </div>
    {count_text}
  </div>
  <div style="display: flex; flex-direction: column; gap: 0.375rem;">
    {row_html}
  </div>
</div>
</body>
</html>"""


async def generate_image(steam_id, theme: str | None = None, refresh: bool = False) -> str:
    """拉取职业选手对战记录并渲染 PNG，返回本地图片路径。

    theme 留空时使用配置项 d2w_image_theme（默认 'light'）。
    数据抓取失败时抛 ds.ProPeersError（供上层转为用户提示）。
    """
    player_name, player_avatar, stats = await ds.fetch_pro_peers(steam_id)
    od_stats = await ds.fetch_opendota_pros(steam_id)
    stats = ds.merge_stats(stats, od_stats)
    stats = await ds.filter_verified(stats)
    if not stats:
        raise ds.ProPeersError(f"未查询到 {player_name} 与职业选手的对战记录")

    theme = normalize_image_theme(theme)
    # 用共享的职业选手表统一显示名并补齐头像（缺失时保留 Stratz 原名/头像）
    await ds.apply_pro_names(stats)
    await ds.apply_pro_avatars(stats)

    top = stats[: ds.OUTPUT_LIMIT]
    avatar_uris = [await _avatar_data_uri(r.get("avatar") or "") for r in top]
    player_avatar_uri = await _avatar_data_uri(player_avatar)

    theme_dict = THEMES.get(theme, THEME_LIGHT)
    html = build_html(player_name, top, avatar_uris, player_avatar_uri, len(stats), theme_dict)

    # 文件名带上风格，避免切换 d2w_image_theme 后仍命中另一风格的旧缓存图
    out_path = os.path.join(OUTPUT_DIR, f"pro_peers_{int(steam_id)}_{theme}.png")

    # 图片缓存：缓存期内复用已生成的图片，避免重复渲染
    if (
        not refresh
        and os.path.exists(out_path)
        and time.time() - os.path.getmtime(out_path) <= IMAGE_CACHE_SECONDS
    ):
        logger.info(f"职业选手对战记录图片缓存命中：{out_path}")
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
    logger.info(
        f"生成职业选手对战记录：{player_name}（{steam_id}）共 {len(stats)} 位，展示前 {len(top)} 位"
    )
    return out_path


__all__ = ["build_html", "generate_image", "THEME_DARK", "THEME_LIGHT"]
