"""Playwright Chromium with the Simplify extension (§8.1).

Branded Chrome/Edge no longer allow side-loading extensions, so this always
uses Playwright's bundled Chromium (never channel="chrome"). The persistent
profile in browser-profile/ keeps the Simplify login between runs.
"""
from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)


async def settle_extension(context, consent: str) -> None:
    """Wait for Simplify's service worker, re-apply the consent choice, keep its auto-submit
    off, close its tutorial tabs.

    Simplify treats every launch as a fresh install (see simplify.apply_consent).
    """
    from .simplify import apply_consent, auto_advance_off
    if not context.service_workers:
        try:
            await context.wait_for_event("serviceworker", timeout=10000)
        except Exception:  # noqa: BLE001
            log.warning("no extension service worker after 10s")
    await apply_consent(context, consent)
    await auto_advance_off(context)
    await asyncio.sleep(0.5)
    for p in list(context.pages):
        if p.url.startswith("chrome-extension://"):
            await p.close()


class BrowserSession:
    """`async with BrowserSession(ctx) as bs:` -> bs.context (a BrowserContext).

    Tests set ctx.browser_overrides = {"headless": True, "extension": False} to
    drive local fixture forms without a display or Simplify.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.pw = None
        self.context = None
        self.closed = False           # the window was closed (by the user, or a crash)
        ov = getattr(ctx, "browser_overrides", None) or {}
        self.headless = ov.get("headless", False)
        self.use_extension = ov.get("extension", True)
        self.user_data_dir = ov.get("user_data_dir") or ctx.paths.browser_profile
        self.windows: list = []       # each window's first tab; forms open as tabs beside it

    async def __aenter__(self) -> "BrowserSession":
        from playwright.async_api import async_playwright
        cfg = self.ctx.profile.browser
        # with several windows, a window covered by another must not count as hidden: Windows
        # reports covered windows to Chrome as occluded, and Simplify pauses in a hidden tab
        off = "Translate" + (",CalculateNativeWinOcclusion" if cfg.windows > 1 else "")
        args = ["--no-first-run", "--no-default-browser-check", f"--disable-features={off}"]
        if self.use_extension:
            ext = str(self.ctx.paths.extension)
            args += [f"--disable-extensions-except={ext}", f"--load-extension={ext}"]
        self.pw = await async_playwright().start()
        kw = dict(headless=self.headless, args=args, no_viewport=True, accept_downloads=True,
                  ignore_default_args=["--disable-extensions"] if self.use_extension else None)
        if cfg.executable_path:
            kw["executable_path"] = cfg.executable_path
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.context = await self.pw.chromium.launch_persistent_context(str(self.user_data_dir), **kw)
        self.context.set_default_timeout(cfg.page_timeout_s * 1000)
        self.context.on("close", lambda _ctx: setattr(self, "closed", True))
        if self.use_extension:
            await self._settle_extension()
        await self._open_windows(cfg.windows)
        return self

    async def _open_windows(self, n: int) -> None:
        """`browser.windows` windows. Simplify only works in a window's front tab, so each
        window can have one form being filled at a time; one window = one at a time."""
        first = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.windows = [first]
        for _ in range(max(1, n) - 1):
            try:
                cdp = await self.context.new_cdp_session(first)
                try:
                    async with self.context.expect_page() as info:
                        await cdp.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
                    self.windows.append(await info.value)
                finally:
                    await cdp.detach()
            except Exception as e:  # noqa: BLE001 - fewer windows is slower, never wrong
                log.warning("could not open another browser window (%s); using %d", e, len(self.windows))
                break

    @property
    def window_count(self) -> int:
        return max(1, len(self.windows))

    async def _settle_extension(self):
        await settle_extension(self.context, self.ctx.profile.browser.simplify_consent)

    def extension_loaded(self) -> bool:
        return any(w.url.startswith("chrome-extension://") for w in self.context.service_workers)

    async def new_page(self, window: int = 0):
        """A new tab in `window` (0 = the first). Each window keeps its blank first tab, so
        closing work tabs never closes a window."""
        if len(self.windows) <= 1:
            return await self.context.new_page()
        # one tab opened at a time: "the next new tab" must be this one, not another worker's
        if getattr(self, "_opening", None) is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if window <= 0 or window >= len(self.windows):
                return await self.context.new_page()
            async with self.context.expect_page() as info:
                await self.windows[window].evaluate("window.open('about:blank', '_blank', 'noopener')")
            return await info.value

    async def __aexit__(self, *exc):
        try:
            if self.context is not None:
                await self.context.close()
        finally:
            if self.pw is not None:
                await self.pw.stop()


# ------------------------------------------------- the window in front of you --
async def show_window(page) -> None:
    """Put this tab, and the tool's browser window, in front of everything else.

    Bringing a tab to the front only changes the tab inside the tool's browser; the
    review page you clicked on is in your own browser, which stays on top. Windows lets a
    program take the foreground only just after keyboard input, so on Windows the window
    is raised the way that allows (win_foreground); elsewhere, or if that does not take,
    the window is minimised and restored, which raises it (and brings it back from the
    taskbar if it was minimised)."""
    try:
        await page.bring_to_front()
    except Exception:  # noqa: BLE001 - a closed tab simply is not shown
        return
    if sys.platform == "win32":
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        if title and await asyncio.to_thread(win_foreground, title):
            return
    await _restore_on_top(page)


async def _restore_on_top(page) -> None:
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception:  # noqa: BLE001
        return
    try:
        tid = (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        wid = (await cdp.send("Browser.getWindowForTarget", {"targetId": tid}))["windowId"]
        was = (await cdp.send("Browser.getWindowBounds", {"windowId": wid}))["bounds"].get("windowState", "normal")
        if was == "fullscreen":
            return
        if was != "minimized":
            await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
        back = "maximized" if was == "maximized" else "normal"
        await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": back}})
    except Exception as e:  # noqa: BLE001 - a window that will not move is still open
        log.debug("could not raise the browser window: %s", e)
    finally:
        try:
            await cdp.detach()
        except Exception:  # noqa: BLE001
            pass


def win_foreground(title: str) -> bool:
    """Windows: bring the visible browser window whose title starts with `title` to the
    foreground. True when Windows says it is in front."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(title):
                found.append(hwnd)
        return True
    user32.EnumWindows(each, 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                       # SW_RESTORE
    # the foreground lock lifts for the process that last had keyboard input: a lone Alt
    # press and release (no character, no shortcut) is the documented way to qualify
    user32.keybd_event(0x12, 0, 0, 0)                    # VK_MENU down
    user32.keybd_event(0x12, 0, 0x0002, 0)               # VK_MENU up (KEYEVENTF_KEYUP)
    user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd
