"""After Submit: only a confirmation from the board counts as applied. Greenhouse's
emailed security code keeps the tab open and is entered from the review card."""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import autoapply.browser.submit as submit_mod
from autoapply.browser.fill import fill_many
from autoapply.pipeline import RunStats
from autoapply.review.server import create_app

from .conftest import CHROMIUM
from .test_browser import FORMS, GH, seed

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]


@pytest.fixture
def sctx(make_ctx, tmp_path):
    def _make(mode):
        ctx = make_ctx(secrets={"GITHUB_TOKEN": "t"})
        ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
        ctx.form_url_for = lambda key: (FORMS / "greenhouse_code.html").resolve().as_uri() + f"?mode={mode}"

        async def no_simplify(page):
            pass
        ctx.simplify_mock = no_simplify
        seed(ctx, GH)
        return ctx
    return _make


def _serve(ctx):
    app = create_app(ctx, port=8765)
    sess = app.state.session
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.__enter__()

    async def fill():
        await fill_many(ctx, [GH], RunStats(), session=await sess.browser())
    c.portal.call(fill)
    assert ctx.ledger.get(GH).state == "ready", ctx.ledger.application(GH)
    return c, sess, ctx.ledger.application(GH)["id"]


def test_emailed_code_keeps_the_tab_and_is_entered_from_the_card(sctx):
    ctx = sctx("code")
    c, sess, app_id = _serve(ctx)
    try:
        r = c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert "security code" in r.text
        row = ctx.ledger.get(GH)
        assert row.state == "stuck" and ctx.has_live_tab(GH)              # not closed, not "submitted"
        page = c.get("/lane/stuck").text
        assert "Enter code &amp; submit" in page and "jane@example.com" in page
        r = c.post(f"/app/{app_id}/code", data={"csrf": sess.csrf, "code": "WRONG000"})
        assert "Incorrect security code" in r.text and ctx.ledger.get(GH).state == "stuck"
        r = c.post(f"/app/{app_id}/code", data={"csrf": sess.csrf, "code": "ABCD 1234"})
        assert ctx.ledger.get(GH).state == "submitted", r.text[:2000]
        assert not ctx.has_live_tab(GH)
        pend = ctx.ledger.pending_writebacks(ctx.profile.sources[0].label)
        assert pend and pend[-1]["change"]["status"] == "applied"
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_confirmation_page_records_the_submission(sctx):
    ctx = sctx("ok")
    c, sess, app_id = _serve(ctx)
    try:
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert ctx.ledger.get(GH).state == "submitted"
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_no_confirmation_is_not_recorded_as_applied(sctx, monkeypatch):
    monkeypatch.setattr(submit_mod, "SUBMIT_WAIT_S", 3.0)
    ctx = sctx("silent")
    c, sess, app_id = _serve(ctx)
    try:
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        row = ctx.ledger.get(GH)
        assert row.state == "stuck" and "submit_unconfirmed" in row.reason and ctx.has_live_tab(GH)
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_a_form_the_browser_refuses_to_send_names_the_empty_fields(sctx, monkeypatch):
    monkeypatch.setattr(submit_mod, "SUBMIT_WAIT_S", 2.0)
    ctx = sctx("ok")
    c, sess, app_id = _serve(ctx)
    try:
        async def break_it():
            tab = ctx.open_tabs[GH]
            # a hidden required mirror left empty, as when a dropdown's value did not reach the page's state
            await tab.evaluate("""() => { const d = document.createElement('div'); d.className = 'field';
              d.innerHTML = '<label>Work authorization</label><input required style="display:none">';
              document.getElementById('application-form').insertBefore(d, document.getElementById('submit')); }""")
        c.portal.call(break_it)
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        row = ctx.ledger.get(GH)
        assert row.state == "stuck" and "not filled in: Work authorization" in row.reason, row.reason
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)
