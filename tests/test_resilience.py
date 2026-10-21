"""Pages that navigate mid-read and screenshots that fail (from the first real runs)."""
import asyncio
from pathlib import Path

import pytest

from autoapply.browser.fields import enumerate_fields, safe_evaluate, safe_screenshot

from .conftest import CHROMIUM

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]

RELOADS_ONCE = """<!doctype html><html><body>
<label for=email>Email*</label><input id=email required>
<script>
// like a board re-routing (or Simplify redirecting) right after load: one navigation
if (!window.name) { window.name = 'navigated'; setTimeout(() => location.reload(), 150); }
</script></body></html>"""


def test_reading_survives_a_navigation(tmp_path):
    from playwright.async_api import async_playwright
    page_file = tmp_path / "form.html"
    page_file.write_text(RELOADS_ONCE)

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)
            page = await b.new_page()
            await page.goto(page_file.as_uri())
            slow = "() => new Promise(r => setTimeout(() => r(document.title + 'ok'), 600))"
            got = await safe_evaluate(page, slow)          # the reload lands mid-call
            fields = await enumerate_fields(page)
            await b.close()
            return got, [f.key for f in fields]
    got, keys = asyncio.run(go())
    assert got == "ok" and keys == ["id:email"]


def test_safe_screenshot_falls_back_then_gives_up(tmp_path):
    class Page:
        url = "https://x"

        def __init__(self, fail_full, fail_view):
            self.calls, self.fail_full, self.fail_view = [], fail_full, fail_view

        def is_closed(self):
            return False

        async def screenshot(self, path, full_page, timeout):
            self.calls.append(full_page)
            if (full_page and self.fail_full) or (not full_page and self.fail_view):
                raise RuntimeError("Protocol error (Page.captureScreenshot): Unable to capture screenshot")
            Path(path).write_bytes(b"png")

    p = Page(fail_full=True, fail_view=False)
    assert asyncio.run(safe_screenshot(p, tmp_path / "a.png")) == str(tmp_path / "a.png")
    assert p.calls == [True, False]
    assert asyncio.run(safe_screenshot(Page(True, True), tmp_path / "b.png")) is None


def test_submission_recorded_even_if_every_screenshot_fails(make_ctx, tmp_path, monkeypatch):
    from playwright.async_api import Page

    from autoapply.browser.launch import BrowserSession
    from autoapply.browser.submit import submit
    from .test_browser import _ready_greenhouse, FORMS, simplify_mock, GH

    async def broken(self, *a, **kw):
        raise RuntimeError("Protocol error (Page.captureScreenshot): Unable to capture screenshot")
    ctx = make_ctx()
    ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
    ctx.form_url_for = lambda key: (FORMS / "greenhouse.html").resolve().as_uri()
    ctx.simplify_mock = simplify_mock()
    _ready_greenhouse(ctx)
    monkeypatch.setattr(Page, "screenshot", broken)

    async def go():
        async with BrowserSession(ctx) as bs:
            return await submit(ctx, bs, GH)
    result = asyncio.run(go())
    assert result["state"] == "submitted"
    assert ctx.ledger.get(GH).state == "submitted"               # never left looking unsent
