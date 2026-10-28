"""Open form in review puts the tool's browser window in front of the review page."""
import asyncio
import sys
from pathlib import Path

import pytest

from autoapply.browser import launch

from .conftest import CHROMIUM


class FakePage:
    def __init__(self, title="Apply - Acme"):
        self.calls, self._title = [], title

    async def bring_to_front(self):
        self.calls.append("front")

    async def title(self):
        return self._title


def test_on_windows_the_window_is_raised_by_title(monkeypatch):
    page, raised = FakePage(), []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(launch, "win_foreground", lambda t: raised.append(t) or True)
    monkeypatch.setattr(launch, "_restore_on_top", lambda p: pytest.fail("no fallback needed"))
    asyncio.run(launch.show_window(page))
    assert page.calls == ["front"] and raised == ["Apply - Acme"]


def test_on_windows_a_refused_foreground_falls_back_to_restore(monkeypatch):
    page, restored = FakePage(), []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(launch, "win_foreground", lambda t: False)

    async def restore(p):
        restored.append(p)
    monkeypatch.setattr(launch, "_restore_on_top", restore)
    asyncio.run(launch.show_window(page))
    assert restored == [page]


def test_a_closed_tab_is_left_alone():
    class Closed(FakePage):
        async def bring_to_front(self):
            raise RuntimeError("Target page, context or browser has been closed")
    asyncio.run(launch.show_window(Closed()))          # no exception


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_a_minimised_window_comes_back(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    monkeypatch.setattr(sys, "platform", "linux")

    async def go():
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(str(tmp_path / "p"), headless=True,
                                                              executable_path=CHROMIUM)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            cdp = await ctx.new_cdp_session(page)
            tid = (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            wid = (await cdp.send("Browser.getWindowForTarget", {"targetId": tid}))["windowId"]
            await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
            await launch.show_window(page)
            state = (await cdp.send("Browser.getWindowBounds", {"windowId": wid}))["bounds"]["windowState"]
            await ctx.close()
            return state
    assert asyncio.run(go()) == "normal"
