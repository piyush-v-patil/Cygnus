"""Playwright Chromium with the Simplify extension (§8.1).

Branded Chrome/Edge no longer allow side-loading extensions, so this always
uses Playwright's bundled Chromium (never channel="chrome"). The persistent
profile in browser-profile/ keeps the Simplify login between runs.
"""
from __future__ import annotations

import asyncio
import logging

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

    async def __aenter__(self) -> "BrowserSession":
        from playwright.async_api import async_playwright
        cfg = self.ctx.profile.browser
        args = ["--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]
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
        return self

    async def _settle_extension(self):
        await settle_extension(self.context, self.ctx.profile.browser.simplify_consent)

    def extension_loaded(self) -> bool:
        return any(w.url.startswith("chrome-extension://") for w in self.context.service_workers)

    async def new_page(self):
        page = await self.context.new_page()
        # keep a blank first tab around so closing work tabs never closes the window
        return page

    async def __aexit__(self, *exc):
        try:
            if self.context is not None:
                await self.context.close()
        finally:
            if self.pw is not None:
                await self.pw.stop()
