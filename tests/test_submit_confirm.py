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


def test_submitted_by_you_in_the_tab_is_recorded_not_sent_again(sctx):
    ctx = sctx("ok")
    c, sess, app_id = _serve(ctx)
    try:
        async def send_by_hand():
            tab = ctx.open_tabs[GH]
            await tab.click("#submit")
            await tab.wait_for_selector(".confirmation__content")
        c.portal.call(send_by_hand)
        r = c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert "already shows the confirmation" in r.text, r.text[:1500]
        assert ctx.ledger.get(GH).state == "submitted_manual"
        assert not ctx.ledger.submit_clicked(GH)                 # Cygnus never clicked Submit
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_a_click_that_was_never_confirmed_is_not_resent_without_you(sctx, monkeypatch):
    monkeypatch.setattr(submit_mod, "SUBMIT_WAIT_S", 2.0)
    ctx = sctx("silent")
    c, sess, app_id = _serve(ctx)
    led = ctx.ledger
    try:
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert led.get(GH).state == "stuck" and led.submit_clicked(GH)
        page = c.get("/lane/stuck").text
        assert "Not sent - Submit it again" in page
        r = c.post(f"/app/{app_id}/retry", data={"csrf": sess.csrf, "lane": "stuck"})
        assert "Submit was already clicked" in r.text and led.get(GH).state == "stuck"   # Fill again refuses
        assert led.requeue(["stuck"]) == []                                              # so does requeue
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_interrupted_between_click_and_confirmation_shows_up_in_stuck(make_ctx):
    from autoapply.ledger import READY
    from .test_browser import seed as gh_seed
    ctx = make_ctx()
    gh_seed(ctx, GH)
    led = ctx.ledger
    led.set_state(GH, "filling")
    led.upsert_application(GH, lane=READY, fill_plan=[], missing=[], submit_clicked_at="2026-10-23T20:00:00Z")
    led.set_state(GH, READY)                    # the PC went off right after Submit was clicked
    led.recover_interrupted()
    row = led.get(GH)
    assert row.state == "stuck" and row.reason.startswith("submit_interrupted")
    assert led.requeue_keys([GH]) == []


@pytest.mark.parametrize("mode,state,reason", [
    ("gone", "stuck", "submit_unconfirmed"),      # form vanished, nothing confirmed: not "submitted"
    ("banner", "submitted", None),                # a notice already on the page is not the click's error
])
def test_only_the_boards_confirmation_counts(sctx, monkeypatch, mode, state, reason):
    monkeypatch.setattr(submit_mod, "SUBMIT_WAIT_S", 4.0)
    ctx = sctx(mode)
    c, sess, app_id = _serve(ctx)
    try:
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        row = ctx.ledger.get(GH)
        assert row.state == state, row.reason
        if reason:
            assert reason in row.reason
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def _mail(ctx, *builders, wait_s=10):
    from .mailfake import inbox
    ctx.profile.mail.enabled = True
    ctx.profile.mail.wait_s = wait_s
    ctx.mail_fetch = inbox(*builders)
    ctx.mail_poll_s = 0.2


def test_the_emailed_code_is_read_and_entered_after_approve(sctx):
    from .mailfake import greenhouse_code
    ctx = sctx("code")
    _mail(ctx, greenhouse_code("ABCD1234"))
    c, sess, app_id = _serve(ctx)
    try:
        r = c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert ctx.ledger.get(GH).state == "submitted", r.text[:2000]
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_a_one_time_code_page_of_another_board(sctx):
    from .mailfake import message
    ctx = sctx("otp")
    _mail(ctx, lambda: message("Ashby <no-reply@ashbyhq.com>", "Your verification code",
                               "Your verification code is: 482913"))
    c, sess, app_id = _serve(ctx)
    try:
        r = c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert ctx.ledger.get(GH).state == "submitted", r.text[:2000]
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_without_mail_the_one_time_code_waits_on_the_card(sctx):
    ctx = sctx("otp")
    c, sess, app_id = _serve(ctx)
    try:
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        row = ctx.ledger.get(GH)
        assert row.state == "stuck" and row.reason.startswith("email_code") and "verification code" in row.reason
        assert "Get it from my email" not in c.get("/lane/stuck").text           # mail is off
        r = c.post(f"/app/{app_id}/code", data={"csrf": sess.csrf, "code": "000000"})
        assert "not valid" in r.text and ctx.ledger.get(GH).state == "stuck"
        c.post(f"/app/{app_id}/code", data={"csrf": sess.csrf, "code": "482913"})
        assert ctx.ledger.get(GH).state == "submitted"
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)


def test_no_code_email_in_time_falls_back_to_the_card(sctx):
    from .mailfake import greenhouse_code
    ctx = sctx("code")
    _mail(ctx, wait_s=1)                                  # mail on, but nothing arrives
    c, sess, app_id = _serve(ctx)
    try:
        c.post(f"/app/{app_id}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        row = ctx.ledger.get(GH)
        assert row.state == "stuck" and "no code email arrived" in row.reason and ctx.has_live_tab(GH)
        assert "Get it from my email" in c.get("/lane/stuck").text
        ctx.mail_fetch = lambda since: [greenhouse_code("ABCD1234")()]          # it came in late
        c.post(f"/app/{app_id}/code-from-mail", data={"csrf": sess.csrf})
        assert ctx.ledger.get(GH).state == "submitted"
        c.portal.call(sess.close)
    finally:
        c.__exit__(None, None, None)
