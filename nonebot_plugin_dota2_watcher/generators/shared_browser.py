"""共享浏览器实例管理模块。

供多个脚本在同一个进程内复用同一个 chromium 浏览器，避免重复启动。

关键约束：
    playwright 的浏览器实例绑定创建它的 event loop，因此只有在
    **同一个事件循环** 内多次调用才会复用同一浏览器；跨事件循环
    （如多次 asyncio.run）会自动重建，旧实例由 atexit 统一清理。

渲染上下文按 device_scale_factor 分组缓存（每个倍率一份 context + page）：
    /出装、/开黑、/pro 用 supersample=2，/ti 用 1，两类渲染交替发生时不会
    再互相顶掉对方、反复重建 context。同一 page 上的「set_content → 截图」
    必须串行，否则两个并发生成会互相污染页面，因此对外提供 rendering()
    上下文管理器，内部加锁后再交付页面。

用法：
    import shared_browser

    async def main():
        # 需要独占页面时用 rendering()
        async with shared_browser.rendering(2) as page:
            await page.set_content(html, wait_until="load")
            png = await page.locator("#container").screenshot()

    # 只想要一个可复用的浏览器实例（自己管理 page）
    browser = await shared_browser.get_browser()

    # 可选：手动关闭（程序退出时也会自动清理）
    await shared_browser.close_browser()

    # 或用上下文管理器自动清理
    async with shared_browser.BrowserSession():
        ...
"""

import asyncio
import atexit

from playwright.async_api import async_playwright

_playwright = None
_browser = None
_loop_id = None

# 共享渲染上下文 / 页面：按 device_scale_factor 分组复用，
# 避免不同倍率的调用互相顶掉、反复 new_context / new_page。
_contexts: dict[float, object] = {}
_pages: dict[float, object] = {}
_page_loop_id = None
# 每个 page 一把锁：同一页面上的 set_content → 截图必须串行
_page_locks: dict[float, asyncio.Lock] = {}


async def get_browser():
    """获取（或创建）共享浏览器实例。

    同一事件循环内多次调用复用同一浏览器；跨循环自动重建。
    """
    global _playwright, _browser, _loop_id

    loop_id = id(asyncio.get_running_loop())
    if _browser is not None and _loop_id == loop_id and _browser.is_connected():
        return _browser

    # 跨事件循环：旧浏览器已失效，直接丢弃引用（旧循环关闭后由系统回收）
    if _browser is not None:
        _browser = None
        _playwright = None

    # start() 返回的是真正的 Playwright 实例（含 chromium 属性）
    if _playwright is None:
        _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    _loop_id = loop_id
    return _browser


def _page_lock(device_scale_factor: float) -> asyncio.Lock:
    """取（或创建）指定倍率页面的串行锁。"""
    lock = _page_locks.get(device_scale_factor)
    if lock is None:
        lock = asyncio.Lock()
        _page_locks[device_scale_factor] = lock
    return lock


async def get_page(device_scale_factor: float = 1.0):
    """获取（或创建）共享页面实例，复用同一个 page 反复 set_content。

    按 device_scale_factor 分组缓存：同一倍率下复用同一 page，不同倍率各有
    自己的 context/page（互不干扰）；跨事件循环或页面已关闭时自动重建。

    注意：直接使用该页面时，调用方需自行保证「set_content → 截图」不被并发
    打断；需要独占时请改用 rendering()。
    """
    global _page_loop_id

    loop_id = id(asyncio.get_running_loop())
    if _page_loop_id is not None and _page_loop_id != loop_id:
        # 跨事件循环：旧 context/page 与锁都绑定在已关闭的循环上，直接丢弃
        _contexts.clear()
        _pages.clear()
        _page_locks.clear()
    _page_loop_id = loop_id

    page = _pages.get(device_scale_factor)
    if page is None or page.is_closed():
        browser = await get_browser()
        old_context = _contexts.pop(device_scale_factor, None)
        if old_context is not None:
            try:
                await old_context.close()
            except Exception:
                pass
        context = await browser.new_context(device_scale_factor=device_scale_factor)
        page = await context.new_page()
        _contexts[device_scale_factor] = context
        _pages[device_scale_factor] = page
    return page


class rendering:
    """独占共享页面的异步上下文管理器。

    async with shared_browser.rendering(2) as page:
        ...  # 这里可以安全地 set_content + 截图

    同一倍率的页面在锁内串行使用，避免并发生成时页面内容被互相覆盖。
    """

    def __init__(self, device_scale_factor: float = 1.0):
        self._dsf = device_scale_factor
        self._lock = None

    async def __aenter__(self):
        self._lock = _page_lock(self._dsf)
        await self._lock.acquire()
        try:
            return await get_page(self._dsf)
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._lock is not None and self._lock.locked():
            self._lock.release()
        return False


async def close_browser():
    """关闭共享浏览器实例（可选，程序退出时会自动清理）。"""
    global _playwright, _browser, _loop_id, _contexts, _pages, _page_loop_id, _page_locks

    for context in list(_contexts.values()):
        try:
            await context.close()
        except Exception:
            pass
    _contexts.clear()
    _pages.clear()
    _page_locks.clear()

    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
    _page_loop_id = None
    _loop_id = None


class BrowserSession:
    """浏览器会话上下文管理器，退出时自动关闭共享浏览器。

    Usage:
        async with BrowserSession():
            await generate_image('Anti-Mage', '1')
            await generate_image('Kez', '1')
        # 退出时自动关闭浏览器
    """

    async def __aenter__(self):
        await get_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await close_browser()
        return False


def _cleanup_at_exit():
    """程序退出时的同步清理回调（尽力关闭浏览器，避免资源泄漏）。"""
    global _playwright, _browser

    if _browser is None and _playwright is None:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_browser())
        loop.close()
    except Exception:
        pass


# 注册程序退出清理
atexit.register(_cleanup_at_exit)
