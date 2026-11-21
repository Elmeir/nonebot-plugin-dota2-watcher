"""HTML → PNG 渲染与图片缓存：供各 HTML 生成器复用。

统一「共享页面截图 → 超采样降采样 → 落盘」流程，避免各生成器各写一份
截图与缩放代码；超采样倍率由调用方传入（项目内统一为 2）。
"""

from __future__ import annotations

import io
import os
import time

from . import shared_browser


def is_fresh(path: str, max_age: float) -> bool:
    """本地图片是否存在且未超过 max_age 秒（缓存命中判断）。"""
    try:
        return time.time() - os.path.getmtime(path) <= max_age
    except OSError:
        return False


async def _screenshot(html: str, supersample: int) -> bytes:
    """在共享页面上渲染 HTML 并截取 #container；超采样由 context 倍率决定。"""
    async with shared_browser.rendering(supersample) as page:
        await page.set_content(html, wait_until="load")
        return await page.locator("#container").screenshot()


async def render_to_png(html: str, out_path: str, supersample: int = 2) -> str:
    """渲染 #container 并按 supersample 降采样后保存为 RGB PNG，返回输出路径。"""
    png = await _screenshot(html, supersample)

    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = img.size
    img = img.resize((width // supersample, height // supersample), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    img.save(out_path)
    return out_path
