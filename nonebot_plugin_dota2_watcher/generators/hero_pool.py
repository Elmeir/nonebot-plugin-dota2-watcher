"""英雄池环形图生成器：渲染 STRATZ 风格 PNG 环形图（纯 Pillow，无 numpy 依赖）。

样式模仿 Stratz 站点（stratz.com/players/）：按出场占比划分的浅灰扇区（灰细边）、
出场前三英雄用按各自头像主色调生成的径向渐变高亮，英雄头像环绕环带且出场越多越大；
环心还有一圈按 position 占比着色的内环带（已缩小，外侧留空），并放置 1-5 号位
图标；中心洞内显示玩家 steam 头像（圆形裁切），玩家名以带黑色描边的纯色文字
按圆弧排在空环带上（自动缩字号、必要时截断）。

默认使用亮色主题，由配置项 d2w_image_theme 统一决定（见 config.normalize_image_theme），
生成函数也接受 theme 参数临时覆盖。
数据来源见 ../datasources/hero_pool.py。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence

from nonebot.log import logger

from ..config import OUTPUT_DIR, normalize_image_theme
from ..datasources import hero_pool as ds

SCALE = 2  # 分辨率倍率
SS = 4  # 超采样倍率：先 4x 绘制再降采样，抗锯齿

# 环形布局常量（viewBox 640x640 = 320*2）
CX = CY = 160 * SCALE
R_OUT = 160 * SCALE  # 外半径
R_IN = 100 * SCALE  # 内半径
ICON_RADIUS = 130 * SCALE  # 头像中心所在半径（环带中部）

# ============================================================
# 主题配置：亮色（默认）/ 暗色
# ============================================================
# 主题不再由本模块常量决定，统一由配置项 d2w_image_theme 提供默认值
# （见 config.normalize_image_theme），生成函数也接受 theme 参数临时覆盖。
DEFAULT_THEME = "light"

# 环心内环带（模仿 STRATZ 中间内环：真环形 + 描边），按 position 占比
# Stratz 320 坐标下内环外半径 84、内半径 37（环带宽 47，中间是洞）。
# 这里把内环整体缩小（外半径 84 -> 62），留出外侧空环带，用于排布玩家名弧。
# 各扇区 fill-opacity + 描边，圆心留空 —— 均为 stratz.com/players/ 实测值
INNER_OUT = 0.75 * R_IN  # 内环外半径（已缩小，腾出名字环带）
INNER_IN = INNER_OUT * 0.55  # 内环内半径（进一步缩小，中心洞更小、位置环带更宽）
INNER_ICON_RADIUS = (INNER_OUT + INNER_IN) / 2.0  # 内环位置图标所在半径（环带中部）

THEMES: dict[str, dict] = {
    "light": {
        "name": "亮色",
        "bg": "#F8FAFC",  # 页面背景色（冷调近白，干净清爽）
        "sector_fill": "hsl(214,22%,88%)",  # 普通扇区：淡蓝灰（与背景区分又不抢眼）
        "sector_opacity": 0.92,  # 扇区填充不透明度（提高对比、更利落）
        "gap_opacity": 0.72,  # 前三渐变扇区渐变 stop 透明度
        "stroke": "#9CA3AF",  # 扇区/内环描边：中性冷灰（清晰不刺眼）
        "inner_opacity": 0.52,  # 内环填充不透明度
        "inner_unknown": "hsl(0,0%,0%)",  # 内环未知扇区颜色（深灰）
        "inner_unknown_alpha": 0.2,  # 内环未知扇区透明度（稍作提亮以便辨识）
        "watermark": "#DFE1E2",  # 环心玩家名水印颜色（中性灰）
        "grad_center_light": 0.70,  # 头像渐变中心亮度（亮）
        "grad_edge_light": 0.42,  # 头像渐变外缘亮度（软过渡，避免发灰）
        # 位置扇区填充色（亮色主题稍亮、对比度更好）
        "position_colors": {
            1: "hsl(222,58%,52%)",  # 1 号位 优势路核心（蓝）
            2: "hsl(187,55%,44%)",  # 2 号位 中路（青）
            3: "hsl(32,90%,46%)",  # 3 号位 劣势路（橙）
            4: "hsl(335,66%,56%)",  # 4 号位 游走（粉）
            5: "hsl(152,72%,38%)",  # 5 号位 辅助（绿）
            "unknown": "hsl(0,0%,0%)",
        },
        # 位置图标填充（亮色背景用深灰，与暗色主题的浅灰相反）
        "icons": {
            "unknown": "#8E8E8E",  # 问号
            "blade": (("#5E5E5E", "#1F1F1F"), (3, 18, 6, 21.75)),  # 剑柄
            "sword": (("hsl(228,62%,58%)", "hsl(228,52%,46%)"), (23.915, 0, 6.38719, 17.6213)),
            "bow": (("hsl(187,64%,48%)", "hsl(188,56%,42%)"), (12, 0, 12, 24)),
            "shield": (("hsl(33,84%,52%)", "hsl(34,78%,42%)"), (12, 0.75, 12, 23.25)),
            "wrist": "#5E5E5E",  # 护腕
            "wrist_glow": (("#5E5E5E", "#1F1F1F"), (2.19928, 13.9623, 2.19928, 23.0759)),
            "wrist_glow5": (("#5E5E5E", "#1F1F1F"), (2.19928, 13.5766, 2.19928, 22.9711)),
            "flame": (("hsl(29,80%,48%)", "hsl(335,66%,56%)"), (20.1087, 0, 10.053, 15.0821)),
            "spark": (("hsl(155,44%,50%)", "hsl(158,78%,36%)"), (7.5, 0, 24, 13.5)),
        },
    },
    "dark": {
        "name": "暗色",
        "bg": "#0A0A0A",
        "sector_fill": "hsl(0,0%,8%)",
        "sector_opacity": 0.5,
        "gap_opacity": 0.5,
        "stroke": "#000000",
        "inner_opacity": 0.4,
        "inner_unknown": "hsl(0,0%,100%)",
        "inner_unknown_alpha": 0.16,
        "watermark": "#A1A1AA",  # 环心玩家名水印颜色（浅灰）
        "grad_center_light": 0.62,
        "grad_edge_light": 0.14,
        # 位置扇区填充色 = STRATZ 位置渐变第 2 个 stop（stratz.com/players/ 内环实测色值）
        "position_colors": {
            1: "hsl(230,43%,45%)",  # 1 号位 优势路核心（蓝）
            2: "hsl(188,48%,38%)",  # 2 号位 中路（青）
            3: "hsl(34,82%,36%)",  # 3 号位 劣势路（橙）
            4: "hsl(335,58%,51%)",  # 4 号位 游走（粉）
            5: "hsl(158,78%,28%)",  # 5 号位 辅助（绿）
            "unknown": "hsl(0,0%,100%)",
        },
        "icons": {
            "unknown": "hsl(0,0%,75%)",
            "blade": (("#DDDDDD", "#838383"), (3, 18, 6, 21.75)),
            "sword": (("hsl(231,54%,59%)", "hsl(230,43%,45%)"), (23.915, 0, 6.38719, 17.6213)),
            "bow": (("hsl(187,60%,40%)", "hsl(188,48%,38%)"), (12, 0, 12, 24)),
            "shield": (("hsl(33,79%,46%)", "hsl(34,82%,36%)"), (12, 0.75, 12, 23.25)),
            "wrist": "#DEDEDE",
            "wrist_glow": (("#DEDEDE", "#7B7373"), (2.19928, 13.9623, 2.19928, 23.0759)),
            "wrist_glow5": (("#DEDEDE", "#7B7373"), (2.19928, 13.5766, 2.19928, 22.9711)),
            "flame": (("hsl(29,76%,39%)", "hsl(335,58%,51%)"), (20.1087, 0, 10.053, 15.0821)),
            "spark": (("hsl(155,31%,48%)", "hsl(158,78%,28%)"), (7.5, 0, 24, 13.5)),
        },
    },
}

PositionKey = str | int  # 位置键：1-5 或 "unknown"

# 内环位置图标路径（viewBox 24x24，路径 d 取自 stratz.com/players/ 内环内联 SVG 原始数据）。
# 每段路径是 (path_d, fill_key, fill_opacity)：fill_key 对应 THEMES[*]["icons"] 中的填充，
# 可为单个颜色字符串（纯色填充）或 ((stops0, stops1), (x1,y1,x2,y2))（userSpaceOnUse 线性渐变）。
_ICON_PATHS: dict[PositionKey, list[tuple[str, str, float]]] = {
    # 未知位置：问号（STRATZ 用 text.tertiary，此处按主题近似为灰）
    "unknown": [
        (
            "M12 0C5.373 0 0 5.375 0 12c0 6.629 5.373 12 12 12s12-5.371 12-12c0-6.625-5.373-12-12-12zm0 21.677"
            "A9.672 9.672 0 0 1 2.323 12 9.674 9.674 0 0 1 12 2.323 9.674 9.674 0 0 1 21.677 12 9.672 9.672 0 0 1 12 21.677z"
            "M17.19 9.33c0 3.244-3.505 3.294-3.505 4.493v.307c0 .32-.26.58-.58.58h-2.21a.58.58 0 0 1-.58-.58v-.419"
            "c0-1.73 1.311-2.421 2.302-2.977.85-.476 1.37-.8 1.37-1.43 0-.835-1.064-1.39-1.924-1.39-1.122 0-1.64.532-2.369 1.451"
            "a.581.581 0 0 1-.806.103L7.542 8.446a.582.582 0 0 1-.128-.792c1.143-1.679 2.6-2.622 4.866-2.622 2.375 0 4.91 1.854 4.91 4.297"
            "zm-3.158 8.09A2.034 2.034 0 0 1 12 19.453a2.035 2.035 0 0 1-2.032-2.033c0-1.12.911-2.032 2.032-2.032 1.12 0 2.032.912 2.032 2.032z",
            "unknown",
            1.0,
        ),
    ],
    # 1 号位：剑柄 + 剑刃
    1: [
        (
            "M4.792 16.244L.623 20.388a2.107 2.107 0 000 2.992h.002a2.136 2.136 0 003.01 0l4.167-4.142-3.01-2.994z",
            "blade",
            0.7,
        ),
        (
            "M2.853 10.193c-.373.32-.597.78-.615 1.268-.018.49.17.964.517 1.309l8.53 8.478c.326.327.77.507 1.233.507"
            "a1.73 1.73 0 001.228-.51 1.717 1.717 0 00-.003-2.434c-.86-.855-1.857-1.843-1.857-1.843s8.881-7.06 10.836-8.612"
            "a1.18 1.18 0 00.43-.776c.17-1.423.668-5.646.845-7.124a.406.406 0 00-.119-.337.414.414 0 00-.34-.116l-6.767.843"
            "c-.304.038-.578.19-.77.427L7.134 12.245s-1.087-1.085-1.973-1.962a1.702 1.702 0 00-2.305-.09h-.003"
            "zm7.519 4.69l9.922-9.861a.79.79 0 10-1.124-1.116l-9.922 9.863a.782.782 0 000 1.114c.31.31.813.31 1.124 0z",
            "sword",
            1.0,
        ),
    ],
    # 2 号位：弓 + 箭
    2: [
        (
            "M19.262 3.015l-1.148-1.15A1.092 1.092 0 0118.884 0h4.025A1.092 1.092 0 0124 1.09v4.024a1.093 1.093 0 01-1.865.773"
            "l-1.152-1.15-1.05 1.051c3.603 4.439 3.448 10.915-.469 15.177l.763 1.271a.65.65 0 01-.165.857c-.31.234-.713.533"
            "-1.037.778a.636.636 0 01-.5.119.642.642 0 01-.432-.281c-.4-.598-1.016-1.52-1.376-2.063a1.206 1.206 0 00-.828-.522"
            "c-1.857-.26-8.092-1.13-10.479-1.462a1.26 1.26 0 01-1.07-1.073C3.957 15.857 2.877 8.11 2.877 8.11a1.197 1.197 0 00-.519-.825"
            "C1.81 6.92.89 6.305.291 5.907a.655.655 0 01-.162-.934c.245-.323.547-.726.778-1.034a.65.65 0 01.856-.167l1.271.762"
            "C6.731 1.141 12.088.571 16.34 2.827a.535.535 0 01.126.852L15.094 5.05a.538.538 0 01-.609.107 8.72 8.72 0 00-9.27 1.328"
            "l1.35 9.228L19.263 3.015zm-1.4 4.844l-9.576 9.578 9.227 1.347a8.723 8.723 0 00.35-10.925z",
            "bow",
            1.0,
        ),
    ],
    # 3 号位：盾牌
    3: [
        (
            "M.75 3.3C.75 1.892 1.84.75 3.187.75H20.81c1.347 0 2.441 1.142 2.441 2.55v7.52a8.265 8.265 0 01-.803 3.56"
            "C20.953 17.45 17.43 23.25 12 23.25c-5.432 0-8.957-5.8-10.444-8.878a8.259 8.259 0 01-.799-3.553A2510.5 2510.5 0 01.75 3.3"
            "zm14.198 2.2a.509.509 0 00-.014-.482.462.462 0 00-.4-.238h-2.48a.469.469 0 00-.41.25c-.558 1.048-2.711 5.076-3.464 6.484"
            "-.054.1-.05.223.004.324.058.1.162.162.274.162h2.196c.169 0 .324.094.414.245.086.151.09.338.01.497-.64 1.242-1.93 3.75"
            "-2.646 5.148a.16.16 0 00.044.198c.06.046.144.04.198-.018 1.67-1.815 5.673-6.156 7.095-7.697a.338.338 0 00.061-.357"
            ".31.31 0 00-.288-.198h-2.008a.477.477 0 01-.407-.24.514.514 0 01-.011-.49c.49-.958 1.343-2.634 1.832-3.588z",
            "shield",
            1.0,
        ),
    ],
    # 4 号位：护腕 + 火焰
    4: [
        (
            "M18.442 18.141l2.167-1.25c.398-.23.898-.219 1.286.03l1.93 1.238a.373.373 0 01.005.63c-1.77 1.183-8 5.211-10.744 5.211"
            "-.926 0-7.725-2.034-7.725-2.034v-6.999h2.704c.881 0 1.741.265 2.46.755l1.635 1.117h3.671c.438 0 1.482 0 1.482 1.302 0 1.41"
            "-1.14 1.41-1.482 1.41h-5.395a.555.555 0 00-.565.543c0 .3.254.543.565.543h5.75s.82.004 1.473-.56c.414-.359.783-.944.783-1.936z",
            "wrist",
            1.0,
        ),
        (
            "M4.399 15.02c0-.583-.494-1.058-1.1-1.058h-2.2c-.606 0-1.099.475-1.099 1.059v6.998c0 .583.493 1.057 1.099 1.057"
            "h2.2c.606 0 1.1-.474 1.1-1.057v-6.998z",
            "wrist_glow",
            0.7,
        ),
        (
            "M20.895 6.395a.32.32 0 00-.202-.246.336.336 0 00-.32.043c-.91.64-1.942.965-1.942.965.04-3.622-2.211-5.914-5.873-7.13"
            "a.51.51 0 00-.541.141.463.463 0 00-.065.537c.833 1.5 1.205 2.868 1.068 4.825 0 0-.924-.426-1.26-1.51a.314.314 0 00-.205-.21"
            ".344.344 0 00-.3.043c-3.528 2.588-2.893 10.11 4.131 10.11 5.095 0 5.928-4.594 5.51-7.568zm-5.31-.56a.14.14 0 00-.03-.152"
            ".149.149 0 00-.158-.03c-2.764 1.222-3.878 6.061-.325 6.061 3.384 0 2.143-3.47.852-4.149a.111.111 0 00-.116.01.108.108 0 00-.05.106"
            "c.065.512-.148.819-.686.779-.209-.812.152-1.83.513-2.624z",
            "flame",
            1.0,
        ),
    ],
    # 5 号位：护腕 + 火花
    5: [
        (
            "M18.442 17.96l2.167-1.289a1.216 1.216 0 011.286.03l1.929 1.278a.392.392 0 01.005.65c-1.77 1.219-8 5.371-10.743 5.371"
            "-.926 0-7.725-2.097-7.725-2.097V14.69h2.704c.883 0 1.741.27 2.46.777l1.635 1.152h3.671c.44 0 1.484 0 1.484 1.342 0 1.453"
            "-1.143 1.453-1.484 1.453h-5.395a.564.564 0 00-.565.56c0 .308.254.558.565.558h5.75s.82.006 1.473-.578c.414-.368.783-.972.783-1.993z",
            "wrist",
            1.0,
        ),
        (
            "M4.399 14.667c0-.602-.494-1.09-1.1-1.09h-2.2c-.606 0-1.099.488-1.099 1.09v7.214c0 .602.493 1.09 1.099 1.09h2.2"
            "c.607 0 1.1-.488 1.1-1.09v-7.214z",
            "wrist_glow5",
            0.7,
        ),
        (
            "M23.594 10.114a.142.142 0 00.002-.274c-1.165-.402-2.238-1.461-2.635-2.62a.142.142 0 00-.274 0c-.39 1.16-1.443 2.247"
            "-2.6 2.63a.141.141 0 00-.003.273c1.158.402 2.21 1.465 2.603 2.617a.142.142 0 00.274 0c.397-1.162 1.468-2.232 2.633-2.626"
            "zm-7.54-3.583a.215.215 0 00.158-.208.214.214 0 00-.157-.209c-1.774-.615-3.408-2.227-4.013-3.994a.213.213 0 00-.21-.158"
            ".214.214 0 00-.207.16c-.597 1.767-2.2 3.423-3.963 4.005a.216.216 0 00-.004.417c1.765.612 3.369 2.232 3.966 3.988.027.094"
            ".111.16.209.16a.214.214 0 00.207-.16c.606-1.77 2.24-3.401 4.014-4.001zm4.87-4.187a.11.11 0 00.08-.106.112.112 0 00-.08-.108"
            "c-.91-.314-1.749-1.142-2.058-2.048A.113.113 0 0018.76 0a.113.113 0 00-.108.082c-.306.908-1.128 1.758-2.032 2.055a.11.11 0 00-.082.106"
            ".109.109 0 00.08.108c.905.314 1.728 1.145 2.034 2.047a.11.11 0 00.108.08c.05 0 .093-.032.106-.08.31-.91 1.148-1.745 2.058-2.054z",
            "spark",
            1.0,
        ),
    ],
}


# 动态头像尺寸（占比驱动）：最小/最大基础尺寸与"顶到最大"的占比阈值
_ICON_MIN = 14  # 单场英雄的基础尺寸
_ICON_MAX = 32  # 最高占比英雄的基础尺寸
_ICON_RATIO_AT_MAX = 0.25  # 出场占比达 1/4 样本即顶到最大档
# 角度重叠约束：icon 的角宽最多占其所在扇区角宽的该比例，剩下留给相邻 icon 的空隙
_ICON_SECTOR_FRAC = 0.85


def _icon_size(count: int, total: int | None = None) -> int:
    """按出场占比动态决定头像尺寸（再乘分辨率倍率）。

    样本量不固定（默认最近 25 场），故不用硬编码场次阈值——那样在样本
    缩小时会大量落到最小档、图标偏小。改为直接用占比驱动：
    占比越高头像越大，连续成比例缩放；占比达到 1/4 样本即顶到最大档，
    单场英雄保持最小可见尺寸。
    """
    ratio = count / total if total else 0.0
    clamped = max(0.0, min(1.0, ratio / _ICON_RATIO_AT_MAX))
    base = _ICON_MIN + (_ICON_MAX - _ICON_MIN) * clamped
    return round(base) * SCALE


def _position_icon_size(percent: float, scale: float) -> float:
    """内环位置图标尺寸（同 STRATZ：占比较小时 4px，最大 20px，再乘分辨率倍率）。"""
    return min(20.0, max(4.0, 150.0 * percent)) * scale


# ============================================================
# 前三英雄扇区渐变：从英雄头像提取主色调
# ============================================================
def _icon_base_color(img):
    """从英雄头像提取主色调（按饱和度加权平均，忽略透明像素），返回 (h, s, l)。"""
    import colorsys

    from PIL import Image

    # 先缩小到最多 32x32（面积平均），大幅减少下方逐像素 Python 循环；
    # 主色调本就是加权平均，缩小后结果基本一致、仅用于取色，可安全加速。
    if max(img.size) > 32:
        ratio = 32 / max(img.size)
        img = img.resize(
            (max(1, round(img.size[0] * ratio)), max(1, round(img.size[1] * ratio))),
            Image.BILINEAR,
        )
    w, h = img.size
    px = img.load()
    acc = [0.0, 0.0, 0.0]
    weight = 0.0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a <= 32:
                continue
            _, _, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            wgt = s * s  # 饱和度高（更有代表性）的像素占更大权重
            acc[0] += r * wgt
            acc[1] += g * wgt
            acc[2] += b * wgt
            weight += wgt
    if weight <= 0:
        return None
    avg = tuple(round(c / weight) for c in acc)
    hh, ll, ss = colorsys.rgb_to_hls(*(c / 255 for c in avg))
    return (hh * 360) % 360, ss, ll


def _hsl_to_rgb(h: float, s: float, light: float) -> tuple[int, int, int]:
    """hsl(0-360, %, %) -> (r, g, b)，供 PNG 渲染复用 SVG 的颜色常量。"""
    from colorsys import hls_to_rgb

    r, g, b = hls_to_rgb((h % 360) / 360.0, light / 100.0, s / 100.0)
    return round(r * 255), round(g * 255), round(b * 255)


def _parse_hsl(s: str) -> tuple[float, float, float]:
    """把 'hsl(h,s%,l%)' 字符串解析为 (h, s, l)，用于 PNG 渲染。"""
    inner = s[s.find("(") + 1 : s.rfind(")")]
    parts = [p.strip().strip("%") for p in inner.split(",")]
    return float(parts[0]), float(parts[1]), float(parts[2])


def _hex_rgb(color: str) -> tuple[int, int, int]:
    """把 '#rrggbb' 转成 (r, g, b)，供 PNG 渲染。"""
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


def _color_rgb(c: str) -> tuple[int, int, int]:
    """把 '#rrggbb' 或 'hsl(h,s%,l%)' 转为 (r, g, b)，供 PNG 渲染复用颜色常量。"""
    c = c.strip()
    if c.startswith("#"):
        return _hex_rgb(c)
    return _hsl_to_rgb(*_parse_hsl(c))


def _hsl_hex(h: float, s: float, light: float) -> str:
    """把 (h, s, light)（s、light 为 0-1 小数）转成 '#RRGGBB' 字符串。"""
    r, g, b = _hsl_to_rgb(h, s * 100, light * 100)
    return f"#{r:02X}{g:02X}{b:02X}"


async def _hero_gradient(short: str, theme: str = DEFAULT_THEME) -> tuple[str, str] | None:
    """从英雄头像生成环带渐变 (stop0, stop1)：中心亮、外缘暗、同色系。

    图标不可用 / 提取失败时返回 None（此时扇区按普通灰色绘制）。
    """
    img = await ds.load_icon_img(short)
    base = _icon_base_color(img) if img else None
    if base is None:
        return None
    h, s, _ = base
    s = min(1.0, max(0.35, s * 1.5))  # 适当提高饱和度，让环带更鲜明
    th = THEMES[theme]
    return _hsl_hex(h, s, th["grad_center_light"]), _hsl_hex(h, s, th["grad_edge_light"])


async def build_hero_gradients(
    stats: list[dict], theme: str = DEFAULT_THEME
) -> list[tuple[str, str] | None]:
    """为排行前三的英雄从头像生成环带渐变；图标不可用/提取失败的项为 None。"""
    return [await _hero_gradient(item["short"], theme) for item in stats[:3]]


# ============================================================
# 环形几何 / 内环位置图标
# ============================================================
def _polar(deg: float, r: float, cx: float, cy: float) -> tuple[float, float]:
    """把角度（度）与半径转换为画布上的 (x, y) 坐标。"""
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def _svg_path_polys(d):
    """把 SVG path 的 d 解析为多个闭合多边形（曲线/圆弧用折线逼近，坐标按 24x24 单位）。

    兼容位置图标路径的写法：支持 M/L/H/V/C/S/A/Z（大小写、相对坐标）；
    圆弧的两个 flag 按 SVG 语法读取单个字符，允许 `0 01.75` 这种紧凑写法。
    """
    n = len(d)
    pos = 0

    def skip():
        nonlocal pos
        while pos < n and d[pos] in " \t\r\n,":
            pos += 1

    def num():
        nonlocal pos
        skip()
        start = pos
        if pos < n and d[pos] in "+-":
            pos += 1
        has = False
        while pos < n and d[pos].isdigit():
            pos += 1
            has = True
        if pos < n and d[pos] == ".":
            pos += 1
            while pos < n and d[pos].isdigit():
                pos += 1
            has = True
        if not has:
            raise ValueError(f"无效的 SVG 数字 @{pos}: {d[pos : pos + 8]!r}")
        if pos < n and d[pos] in "eE":
            pos += 1
            if pos < n and d[pos] in "+-":
                pos += 1
            while pos < n and d[pos].isdigit():
                pos += 1
        return float(d[start:pos])

    def flag():
        nonlocal pos
        skip()
        if pos < n and d[pos] in "01":
            f = int(d[pos])
            pos += 1
            return f
        return int(num())

    def more():
        skip()
        return pos < n and (d[pos].isdigit() or d[pos] in "+-.")

    st = {"cur": (0.0, 0.0), "start": (0.0, 0.0), "poly": [], "polys": [], "prev": "", "ctrl": None}

    def add(p):
        st["poly"].append(p)

    def bez(p0, c1, c2, p1, steps=28):
        for k in range(1, steps + 1):
            t = k / steps
            u = 1 - t
            add(
                (
                    u * u * u * p0[0]
                    + 3 * u * u * t * c1[0]
                    + 3 * u * t * t * c2[0]
                    + t * t * t * p1[0],
                    u * u * u * p0[1]
                    + 3 * u * u * t * c1[1]
                    + 3 * u * t * t * c2[1]
                    + t * t * t * p1[1],
                )
            )
        return p1

    def arcline(p0, rx, ry, phi, large, sweep, p1):
        if p0 == p1:
            return p1
        rx, ry = abs(rx), abs(ry)
        ph = math.radians(phi % 360)
        cp, sp = math.cos(ph), math.sin(ph)
        dx, dy = (p0[0] - p1[0]) / 2.0, (p0[1] - p1[1]) / 2.0
        x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
        lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
        if lam > 1:
            s = math.sqrt(lam)
            rx *= s
            ry *= s
        den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
        coef = (
            math.sqrt(
                max(0.0, (rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p) / den)
            )
            if den
            else 0.0
        )
        if large == sweep:
            coef = -coef
        cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
        cx = cp * cxp - sp * cyp + (p0[0] + p1[0]) / 2.0
        cy = sp * cxp + cp * cyp + (p0[1] + p1[1]) / 2.0

        def ang(ux, uy, vx, vy):
            nrm = math.hypot(ux, uy) * math.hypot(vx, vy)
            a = math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / nrm))) if nrm else 0.0
            if ux * vy - uy * vx < 0:
                a = -a
            return a

        t1 = ang(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
        dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
        if not sweep and dt > 0:
            dt -= 2 * math.pi
        elif sweep and dt < 0:
            dt += 2 * math.pi
        nsegs = max(1, int(math.ceil(abs(dt) / (math.pi / 2))))
        delta = dt / nsegs
        k4 = 4.0 / 3.0 * math.tan(delta / 4.0)
        curp = p0
        th = t1
        for _s in range(nsegs):
            ca, sa = math.cos(th), math.sin(th)
            cb, sb = math.cos(th + delta), math.sin(th + delta)
            q1 = (
                cx + rx * (ca - k4 * sa) * cp - ry * (sa + k4 * ca) * sp,
                cy + rx * (ca - k4 * sa) * sp + ry * (sa + k4 * ca) * cp,
            )
            q2 = (
                cx + rx * (cb + k4 * sb) * cp - ry * (sb - k4 * cb) * sp,
                cy + rx * (cb + k4 * sb) * sp + ry * (sb - k4 * cb) * cp,
            )
            q3 = (cx + rx * cb * cp - ry * sb * sp, cy + rx * cb * sp + ry * sb * cp)
            bez(curp, q1, q2, q3)
            curp = q3
            th += delta

    def close():
        poly = st["poly"]
        if len(poly) >= 2:
            if poly[0] != poly[-1]:
                poly.append(poly[0])
            st["polys"].append(poly)
        st["poly"] = []

    while pos < n:
        skip()
        if pos >= n:
            break
        c = d[pos]
        if c not in "MmLlHhVvCcSsAaZz":
            num()
            continue
        pos += 1
        up, rel = c.upper(), c.islower()
        if up == "Z":
            close()
            st["cur"] = st["start"]
            st["ctrl"] = None
            st["prev"] = c
            continue
        if up == "M":
            if st["poly"]:
                close()
            x, y = num(), num()
            st["cur"] = (st["cur"][0] + x, st["cur"][1] + y) if rel else (x, y)
            st["start"] = st["cur"]
            st["poly"] = [st["cur"]]
            st["ctrl"] = None
            while more():
                x, y = num(), num()
                st["cur"] = (st["cur"][0] + x, st["cur"][1] + y) if rel else (x, y)
                add(st["cur"])
            st["prev"] = c
            continue
        if up == "L":
            while more():
                x, y = num(), num()
                st["cur"] = (st["cur"][0] + x, st["cur"][1] + y) if rel else (x, y)
                add(st["cur"])
            st["prev"] = c
            continue
        if up == "H":
            while more():
                x = num()
                st["cur"] = (st["cur"][0] + x, st["cur"][1]) if rel else (x, st["cur"][1])
                add(st["cur"])
            st["prev"] = c
            continue
        if up == "V":
            while more():
                y = num()
                st["cur"] = (st["cur"][0], st["cur"][1] + y) if rel else (st["cur"][0], y)
                add(st["cur"])
            st["prev"] = c
            continue
        if up == "C":
            while more():
                p0 = st["cur"]
                if rel:
                    c1 = (p0[0] + num(), p0[1] + num())
                    c2 = (p0[0] + num(), p0[1] + num())
                    p = (p0[0] + num(), p0[1] + num())
                else:
                    c1 = (num(), num())
                    c2 = (num(), num())
                    p = (num(), num())
                bez(p0, c1, c2, p)
                st["cur"] = p
                st["ctrl"] = c2
            st["prev"] = c
            continue
        if up == "S":
            while more():
                p0 = st["cur"]
                if st["prev"] in ("C", "c", "S", "s") and st["ctrl"] is not None:
                    c1 = (2 * p0[0] - st["ctrl"][0], 2 * p0[1] - st["ctrl"][1])
                else:
                    c1 = p0
                if rel:
                    c2 = (p0[0] + num(), p0[1] + num())
                    p = (p0[0] + num(), p0[1] + num())
                else:
                    c2 = (num(), num())
                    p = (num(), num())
                bez(p0, c1, c2, p)
                st["cur"] = p
                st["ctrl"] = c2
            st["prev"] = c
            continue
        if up == "A":
            while more():
                rx, ry = num(), num()
                phi = num()
                large, sweep = flag(), flag()
                x, y = num(), num()
                p = (st["cur"][0] + x, st["cur"][1] + y) if rel else (x, y)
                arcline(st["cur"], rx, ry, phi, large, sweep, p)
                st["cur"] = p
                st["ctrl"] = None
            st["prev"] = c
            continue
    if st["poly"]:
        close()
    return st["polys"]


def _icon_fill_color(fill) -> tuple[int, int, int]:
    """取位置图标路径的填充 RGB：渐变取亮端(stops[0])，纯色直接解析。"""
    c = fill[0][0] if isinstance(fill, tuple) else fill
    return _color_rgb(c)


def _draw_position_icon(
    canvas, pos: PositionKey, ix: float, iy: float, size: float, theme: str = DEFAULT_THEME
) -> None:
    """在 (ix,iy) 处以 size 尺寸绘制内环位置图标（24x24 viewBox 按比例缩放）。

    每个路径用「偶奇规则」合并子路径生成遮罩（正确处理问号圆环的内孔），
    再按路径的 fill-opacity 上色合成到画布；半透明路径先画到临时图层再合成。
    """
    from PIL import Image, ImageChops, ImageDraw

    icons = THEMES[theme]["icons"]
    for d, fill_key, op in _ICON_PATHS.get(pos, []):
        color = _icon_fill_color(icons[fill_key])
        polys = _svg_path_polys(d)
        # 局部遮罩：以 (ix,iy) 为中心、size 为直径的超采样区域
        half = size / 2.0 + 2
        dim = int(size) + 8
        ox, oy = ix - half, iy - half
        mask = Image.new("1", (dim, dim), 0)
        for poly in polys:
            pts = [
                (ix + (px - 12.0) / 24.0 * size - ox, iy + (py - 12.0) / 24.0 * size - oy)
                for px, py in poly
            ]
            tmp = Image.new("1", (dim, dim), 0)
            ImageDraw.Draw(tmp).polygon(pts, fill=1)
            mask = ImageChops.logical_xor(mask, tmp)
        alpha = mask.convert("L").point([int(v * op) for v in range(256)])
        layer = Image.new("RGBA", (dim, dim), (*color, 0))
        layer.putalpha(alpha)
        canvas.alpha_composite(layer, (round(ox), round(oy)))


# ============================================================
# 环心玩家名水印（纯色 + 黑色描边署名，圆弧排布在空环带上）
# ============================================================
# 复用战报生成器 match_report 的字体系统（init_fonts / draw_text_with_fallback），
# 支持中文、emoji 等任意字符；结果懒加载并缓存，避免每次渲染都扫描字体。
# 内环缩小后外侧留出空环带，玩家名以圆弧（顶部居中、左右对称）排布其上，
# 不占用中心小圈，也不压到位置图标。
NAME_RADIUS = (INNER_OUT + R_IN) / 2.0  # 名字弧形所在半径（最终像素）
NAME_MAX_W = 2 * math.pi * NAME_RADIUS * 0.6  # 名字弧长上限（最终像素，留边距）
NAME_MAX_SIZE = 30  # 起始字号（最终像素）
NAME_MIN_SIZE = 18  # 最小可读字号
NAME_CHAR_SPACING = 5  # 弧上相邻字素簇之间的额外间距（最终像素）
_NAME_ELLIPSIS = "…"
_name_font_paths: list[str] | None = None


async def _get_name_font_paths() -> list[str]:
    """返回可用字体列表（复用战报字体系统，懒加载并缓存）。"""
    global _name_font_paths
    if _name_font_paths is None:
        from .match_report import init_fonts

        _name_font_paths = await init_fonts()
    return _name_font_paths


def _fit_name_text(name: str, font_paths: list[str], max_w: float) -> tuple[str, int]:
    """在 max_w 内自适应缩字号；缩到最小字号仍放不下则从尾部截断加省略号。

    返回 (绘制文本, 字号)；字号为最终像素，绘制时再乘超采样倍率 SS。
    """
    from .match_report import font_getsize_with_fallback

    def fits(text: str, size: int) -> bool:
        w, _ = font_getsize_with_fallback(text, font_paths, size)
        return w <= max_w

    size = NAME_MAX_SIZE
    while size > NAME_MIN_SIZE and not fits(name, size):
        size -= 2
    if fits(name, size):
        return name, size
    # 字号已缩到最小仍放不下：从尾部逐字符截断前缀，末尾补省略号
    for end in range(len(name) - 1, 0, -1):
        candidate = name[:end] + _NAME_ELLIPSIS
        if fits(candidate, size):
            return candidate, size
    return _NAME_ELLIPSIS, size


def _render_cluster_img(cluster, fp, font_size, fill, stroke_fill):
    """把单个字素簇渲染为带黑色描边的 RGBA 小图（供旋转贴合圆弧使用）。"""
    from PIL import Image, ImageDraw, ImageFont

    from .match_report import font_getsize

    font = ImageFont.truetype(fp, font_size)
    w, h = font_getsize(font, cluster)
    pad = 4
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0 = pad, pad
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
        d.text((x0 + dx, y0 + dy), cluster, font=font, fill=stroke_fill)
    d.text((x0, y0), cluster, font=font, fill=fill)
    return img


def _draw_name_on_ring(canvas, name, font_paths, cx, cy, fill, stroke_fill) -> None:
    """把玩家名按圆弧排布在空环带上（顶部居中、左右对称），保持字体回退与黑色描边。

    fill / stroke_fill 已预混为不透明色；逐字素簇渲染成小图再旋转，贴合圆周切线。
    """
    from PIL import Image, ImageFont

    from .match_report import _split_into_clusters, font_getsize, segment_text_by_fonts

    text, size = _fit_name_text(name, font_paths, NAME_MAX_W)
    font_size = size * SS
    radius = NAME_RADIUS * SS
    # 按字体回退分段，逐字素簇测量宽度，保证每段使用各自可用字体
    items: list[tuple[str, str, float]] = []  # (cluster, font_path, advance_w)
    total_w = 0.0
    for seg_text, fp in segment_text_by_fonts(text, font_paths):
        font = ImageFont.truetype(fp, font_size)
        for cluster in _split_into_clusters(seg_text):
            w, _ = font_getsize(font, cluster)
            items.append((cluster, fp, w))
            total_w += w
    if not items:
        return
    # 总角跨度以顶部（-90°）为中心左右对称（含字间距）；cur_deg 为当前字符的起点角度，依次向右推进
    total_span = total_w + NAME_CHAR_SPACING * SS * (len(items) - 1)
    cur_deg = -90.0 - math.degrees(total_span / radius) / 2.0
    for cluster, fp, w in items:
        center_deg = cur_deg + math.degrees(w / 2.0 / radius)
        px, py = _polar(center_deg, radius, cx, cy)
        img = _render_cluster_img(cluster, fp, font_size, fill, stroke_fill)
        # 旋转使字形基线贴合圆弧切线（PIL 正角为逆时针，故取负）
        img = img.rotate(-(center_deg + 90.0), resample=Image.BICUBIC, expand=True)
        iw, ih = img.size
        canvas.alpha_composite(img, (round(px - iw / 2), round(py - ih / 2)))
        cur_deg += math.degrees((w + NAME_CHAR_SPACING * SS) / radius)


def _draw_center_avatar(canvas, cx, cy, img, hole_r) -> None:
    """把玩家 steam 头像裁剪成圆形，居中绘制在中心洞里（hole_r 为洞半径，画布像素）。"""
    from PIL import Image, ImageChops, ImageDraw

    diam = max(1, int(hole_r * 2 * 0.9))  # 略盖过内环内缘，确保填满中心洞
    resized = img.resize((diam, diam), Image.LANCZOS)
    mask = Image.new("L", (diam, diam), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diam, diam], fill=255)
    resized.putalpha(ImageChops.multiply(resized.getchannel("A"), mask))
    canvas.alpha_composite(resized, (round(cx - diam / 2), round(cy - diam / 2)))


# ============================================================
# PNG 渲染
# ============================================================
def _radial_gradient(cx: float, cy: float, ro: float, size: int, c0, c1):
    """绘制中心 c0 -> 外缘 c1 的径向渐变图（纯 Pillow 无 numpy）。

    用实心圆盘从外到内逐层覆盖（每层 1px 半径），得到无缝的同心圆环填充；
    相比用 width=1 描边逐圈画圆，能避免相邻圈之间的空隙/振铃纹理。
    """
    from PIL import Image, ImageDraw

    grad = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(grad)
    steps = max(1, int(ro))
    for k in range(steps, 0, -1):  # 从最外层圆盘画到圆心，内层覆盖外层形成圆环
        t = k / steps
        r = ro * t
        col = tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    return grad


async def render_png(
    stats: list[dict],
    total: int,
    out_path,
    pos_dist=None,
    hero_gradients: Sequence[tuple[str, str] | None] = (),
    player_name: str = "",
    avatar_url: str = "",
    theme: str = DEFAULT_THEME,
) -> None:
    """用 Pillow 直接渲染 PNG 环形图（无 numpy 依赖）。"""
    from PIL import Image, ImageDraw

    size = 320 * SCALE
    W = size * SS

    th = THEMES[theme]
    bg = tuple(int(th["bg"][i : i + 2], 16) for i in (1, 3, 5))
    stroke_rgb = _color_rgb(th["stroke"])
    cx, cy, ro, ri, ric = CX * SS, CY * SS, R_OUT * SS, R_IN * SS, ICON_RADIUS * SS
    canvas = Image.new("RGBA", (W, W), (*bg, 255))
    draw = ImageDraw.Draw(canvas)

    def _pl(deg, r):
        return _polar(deg, r, cx, cy)

    def _wedge_pts(a0, a1, steps=240, r_out=ro, r_in=ri):
        pts = []
        for k in range(steps + 1):
            pts.append(_pl(a0 + (a1 - a0) * k / steps, r_out))
        for k in range(steps + 1):
            pts.append(_pl(a1 - (a1 - a0) * k / steps, r_in))
        return [(round(x), round(y)) for x, y in pts]

    # 扇区填充（超采样，边缘平滑）
    # 普通扇区均为纯色，先把「灰色 × 不透明度」混合到背景得到最终色，再直接填充多边形，
    # 避免为每个普通扇区分配 W×W 的掩码/RGBA 图层再做 alpha_composite（大幅减少内存与耗时）。
    # 这里的 alpha 归一化与下方 mask.point(int(v * opacity)) 完全一致，保证逐像素等价。
    sector_fill = _color_rgb(th["sector_fill"])
    sector_op = int(th["sector_opacity"] * 255) / 255.0
    flat_fill = tuple(round(sector_fill[i] * sector_op + bg[i] * (1 - sector_op)) for i in range(3))
    # 渐变扇区遮罩的固定透明度 LUT：point 传查表走 C 快速路径，避免对 2560×2560 逐像素回调 lambda
    gap_lut = [int(v * th["gap_opacity"] * th["sector_opacity"]) for v in range(256)]

    starts = []
    cursor = -90.0
    for i, item in enumerate(stats):
        span = item["count"] * 360.0 / total
        a0, a1 = cursor, cursor - span
        starts.append(a0)
        ptsi = _wedge_pts(a0, a1)

        if i < len(hero_gradients) and hero_gradients[i] is not None:
            c0 = _color_rgb(hero_gradients[i][0])
            c1 = _color_rgb(hero_gradients[i][1])
            # 径向渐变（中心亮、外缘暗）：先 1x 逐圈绘制，再放大到超采样画布
            grad = _radial_gradient(CX, CY, R_OUT, size, c0, c1).resize((W, W), Image.LANCZOS)
            # 渐变 stop 透明度与扇区 fill-opacity 叠加，同 STRATZ
            mask = Image.new("L", (W, W), 0)
            ImageDraw.Draw(mask).polygon(ptsi, fill=255)
            alpha = mask.point(gap_lut)
            grad.putalpha(alpha)
            canvas.alpha_composite(grad, (0, 0))
        else:
            draw.polygon(ptsi, fill=(*flat_fill, 255))
        cursor = a1

    # 挖掉环心圆洞（露出背景）
    draw.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=(*bg, 255))

    # 主题描边：只画一次（外圆 + 内圆 + 每条边界径向线各一笔），避免重叠变粗
    bw = SCALE * SS
    for a in starts:
        (x1, y1), (x2, y2) = _pl(a, ri), _pl(a, ro)
        draw.line([x1, y1, x2, y2], fill=(*stroke_rgb, 255), width=bw, joint="curve")
    draw.ellipse([cx - ro, cy - ro, cx + ro, cy + ro], outline=(*stroke_rgb, 255), width=bw)
    draw.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], outline=(*stroke_rgb, 255), width=bw)

    # 环心内环带：模仿 STRATZ 中间内环（真环形 + 描边），按位置占比着色
    i_out = INNER_OUT * SS
    i_in = INNER_IN * SS
    i_icon_r = INNER_ICON_RADIUS * SS
    i_cursor = -90.0
    pos_dist = pos_dist or []
    pos_total = sum(c for _, c in pos_dist) or 1
    i_starts, i_icons = [], []
    inner_op = th["inner_opacity"]
    inner_unknown = _color_rgb(th["inner_unknown"])
    position_colors = th["position_colors"]
    for pos, count in pos_dist:
        span = count * 360.0 / pos_total
        a0, a1 = i_cursor, i_cursor - span
        i_starts.append(a0)
        ptsi = _wedge_pts(a0, a1, r_out=i_out, r_in=i_in)
        if pos == "unknown":
            fill_rgb = inner_unknown
            alpha = inner_op * th["inner_unknown_alpha"]
        else:
            fill_rgb = _color_rgb(position_colors.get(pos, position_colors["unknown"]))
            alpha = inner_op
        # 纯色扇区：混到背景后直接填充，等价于原 mask + alpha_composite 路径
        a = int(alpha * 255) / 255.0
        blended = tuple(round(fill_rgb[j] * a + bg[j] * (1 - a)) for j in range(3))
        draw.polygon(ptsi, fill=(*blended, 255))
        # 位置图标（同 STRATZ：扇区质心处，尺寸随占比 4~20px）
        percent = count / pos_total
        isz = _position_icon_size(percent, SCALE * SS)
        i_icons.append(((a0 + a1) / 2.0, pos, isz))
        i_cursor = a1
    # 内环主题描边：外圆 + 内圆 + 每条边界径向线各一笔（避免重叠变粗）
    bw2 = SCALE * SS
    for a in i_starts:
        (x1, y1), (x2, y2) = _pl(a, i_in), _pl(a, i_out + bw2)
        draw.line([x1, y1, x2, y2], fill=(*stroke_rgb, 255), width=bw2, joint="curve")
    draw.ellipse(
        [cx - i_out, cy - i_out, cx + i_out, cy + i_out], outline=(*stroke_rgb, 255), width=bw2
    )
    draw.ellipse(
        [cx - i_in, cy - i_in, cx + i_in, cy + i_in], outline=(*stroke_rgb, 255), width=bw2
    )
    # 位置图标绘制在描边之上
    for imid, pos, isz in i_icons:
        ix, iy = _pl(imid, i_icon_r)
        _draw_position_icon(canvas, pos, ix, iy, isz, theme)

    # 头像：放在对应扇区中心（逆时针 mid 角）。
    # 先并发预取所有头像，避免首次渲染时逐个串行下载造成长时间等待。
    hero_imgs = await asyncio.gather(
        *(ds.load_icon_img(item["short"]) for item in stats),
        return_exceptions=True,
    )
    cursor = -90.0
    # 头像直径上限：不超过外环带宽度（ri~ro 间距），并留出边缘余量，防止图标溢出环带
    max_hero_diam = int((ro - ri) * 0.9)
    for item, img in zip(stats, hero_imgs):
        span = item["count"] * 360.0 / total
        mid = (cursor + (cursor - span)) / 2.0
        if isinstance(img, Image.Image):
            # 先受径向约束（不越出环带），再受角度约束（不侵占相邻扇区）
            sz = min(_icon_size(item["count"], total) * SS, max_hero_diam)
            # 该 icon 斜边角宽 = 2*asin((sz/2)/ric)，要求它不超过扇区角宽的一部分。
            # 由「目标角宽 = 扇区角宽 * 占比系数」反解出允许的最大直径。
            target_ang = math.radians(span) / 2.0 * _ICON_SECTOR_FRAC
            sz = min(sz, int(2 * ric * math.sin(target_ang)))
            ix, iy = _pl(mid, ric)
            resized = img.resize((sz, sz), Image.LANCZOS)
            canvas.alpha_composite(resized, (round(ix - sz / 2), round(iy - sz / 2)))
        cursor = cursor - span

    # 中心洞内的玩家 steam 头像（圆形裁切）
    if avatar_url:
        avatar_img = await ds.load_avatar_img(avatar_url)
        if isinstance(avatar_img, Image.Image):
            _draw_center_avatar(canvas, cx, cy, avatar_img, i_in)

    # 环心玩家名水印：纯色署名 + 黑色描边，按圆弧排在空环带上（降采样前绘制以抗锯齿）
    if player_name:
        font_paths = await _get_name_font_paths()
        if font_paths:
            wm_fill = _color_rgb(th["watermark"])
            _draw_name_on_ring(canvas, player_name, font_paths, cx, cy, wm_fill, (0, 0, 0))

    # 统一降采样到目标尺寸（LANCZOS 抗锯齿）
    canvas = canvas.resize((size, size), Image.LANCZOS)
    canvas.convert("RGB").save(out_path, "PNG")


async def generate_image(steam_id, count=25, refresh=False, theme: str | None = None) -> str:
    """拉取玩家英雄池数据并渲染 PNG 环形图，返回本地路径。

    theme 留空时使用配置项 d2w_image_theme（默认 'light'）。
    数据抓取/渲染失败时抛 ds.HeroPoolError（供上层转为用户提示）。
    """
    theme = normalize_image_theme(theme)
    player_name, avatar_url, matches = await ds.fetch_matches(
        steam_id, count=count, refresh=refresh
    )
    if not matches:
        raise ds.HeroPoolError("未获取到任何比赛数据，请检查 steam_id 与 Token")

    stats = ds.build_stats(matches)
    total = sum(item["count"] for item in stats)
    pos_dist = ds.pos_distribution(matches)
    hero_gradients = await build_hero_gradients(stats, theme)

    logger.info(f"生成英雄池：{player_name}（{steam_id}）共 {total} 场、{len(stats)} 名英雄")

    # 文件名带上风格，避免切换 d2w_image_theme 后仍命中另一风格的旧缓存图
    out_path = OUTPUT_DIR / f"hero_pool_{steam_id}_{theme}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await render_png(
        stats,
        total,
        out_path,
        pos_dist,
        hero_gradients,
        player_name,
        avatar_url=avatar_url,
        theme=theme,
    )
    return str(out_path)
