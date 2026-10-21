"""`autoapply run --review`: filled forms stay open and are approved as they stand."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import autoapply.browser.submit as submit_mod
from autoapply.browser.fill import fill_many
from autoapply.browser.launch import BrowserSession
from autoapply.browser.submit import submit
from autoapply.pipeline import RunStats
from autoapply.review.server import create_app

from .conftest import CHROMIUM
from .test_browser import ASHBY, FORMS, GH, LEVER, seed, simplify_mock

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]

FORM_FOR = {GH: "greenhouse.html", ASHBY: "ashby.html", LEVER: "lever.html"}


@pytest.fixture
def lctx(make_ctx, tmp_path):
    ctx = make_ctx(test_mode=True)
    ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
    ctx.form_url_for = lambda key: (FORMS / FORM_FOR[key]).resolve().as_uri()
    ctx.simplify_mock = simplify_mock()
    ctx.keep_tabs = True
    return ctx


def no_replay(monkeypatch):
    calls = []

    async def fake(ctx, bs, app):
        calls.append(app["posting_key"])
        raise AssertionError("refilled although a good tab was open")
    monkeypatch.setattr(submit_mod, "replay", fake)
    return calls


def test_filled_tabs_are_kept_up_to_the_cap(lctx):
    lctx.profile.browser.keep_tabs_max = 1
    lctx.profile.browser.parallel_tabs = 1
    seed(lctx, GH)
    seed(lctx, LEVER, company="Beta", role="software")

    async def go():
        async with BrowserSession(lctx) as bs:
            await fill_many(lctx, [GH, LEVER], RunStats(), session=bs)
            return sorted(lctx.live_tabs())
    assert asyncio.run(go()) == [GH]                      # the second form closed: cap of 1


def test_live_tab_is_submitted_as_it_stands_with_hand_edits(lctx, monkeypatch):
    lctx.answers.add("Are you willing to work onsite 5 days a week?", "yes_no", "Yes")
    seed(lctx, GH)
    no_replay(monkeypatch)

    async def go():
        async with BrowserSession(lctx) as bs:
            await fill_many(lctx, [GH], RunStats(), session=bs)
            assert lctx.ledger.get(GH).state == "ready"
            tab = lctx.open_tabs[GH]
            await tab.fill("#question_101", "https://linkedin.com/in/jane-edited")   # a hand edit
            return await submit(lctx, bs, GH)
    result = asyncio.run(go())
    assert result["state"] == "would_submit", result
    sent = json.loads((Path(lctx.ledger.application(GH)["submission_dir"]) / "fields.json").read_text())
    assert {f["label"]: f["value"] for f in sent}["LinkedIn Profile"] == "https://linkedin.com/in/jane-edited"


@pytest.mark.parametrize("breakage", ["empty_required", "too_old"])
def test_unusable_tab_falls_back_to_refill(lctx, monkeypatch, breakage):
    lctx.answers.add("Are you willing to work onsite 5 days a week?", "yes_no", "Yes")
    seed(lctx, GH)
    refilled = []
    real_replay = submit_mod.replay

    async def spy(ctx, bs, app):
        refilled.append(app["posting_key"])
        return await real_replay(ctx, bs, app)
    monkeypatch.setattr(submit_mod, "replay", spy)

    async def go():
        async with BrowserSession(lctx) as bs:
            await fill_many(lctx, [GH], RunStats(), session=bs)
            if breakage == "empty_required":
                await lctx.open_tabs[GH].fill("#email", "")
            else:
                lctx.profile.browser.tab_max_age_min = 0
            return await submit(lctx, bs, GH)
    result = asyncio.run(go())
    assert refilled == [GH] and result["state"] == "would_submit", result


def test_review_answers_are_typed_into_the_open_tab(lctx):
    seed(lctx, GH)
    app = create_app(lctx, port=8765)
    sess = app.state.session
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        async def fill():
            bs = await sess.browser()
            await fill_many(lctx, [GH], RunStats(), session=bs)
        c.portal.call(fill)
        assert lctx.ledger.get(GH).state == "needs_input" and lctx.has_live_tab(GH)
        assert "open tab" in c.get("/lane/needs_input").text
        a = lctx.ledger.application(GH)
        key = a["missing"][0]["key"]
        r = c.post(f"/app/{a['id']}/answers", data={"csrf": sess.csrf, f"a__{key}": "Yes", "then": "save"})
        assert "Typed into 1 open tab" in r.text
        tab = lctx.open_tabs[GH]

        async def shown():
            return await tab.evaluate("""() => document.querySelector('#question_105')
                .closest('.select__container').querySelector('.select__single-value').textContent""")
        assert c.portal.call(shown) == "Yes"
        assert lctx.ledger.get(GH).state == "ready"
        c.portal.call(sess.close)


def test_answers_typed_into_the_form_are_picked_up(lctx):
    seed(lctx, GH)
    app = create_app(lctx, port=8765)
    sess = app.state.session
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        async def fill():
            bs = await sess.browser()
            await fill_many(lctx, [GH], RunStats(), session=bs)
        c.portal.call(fill)
        a = lctx.ledger.application(GH)
        assert lctx.ledger.get(GH).state == "needs_input"
        r = c.post(f"/app/{a['id']}/from-form", data={"csrf": sess.csrf})
        assert "None of the questions" in r.text

        from autoapply.browser.fields import fill_field, enumerate_fields

        async def answer_by_hand():
            tab = lctx.open_tabs[GH]
            f = next(f for f in await enumerate_fields(tab) if f.key == a["missing"][0]["key"])
            await fill_field(tab, f, "Yes")
        c.portal.call(answer_by_hand)
        r = c.post(f"/app/{a['id']}/from-form", data={"csrf": sess.csrf})
        assert "Read 1 answer(s) from the form" in r.text, r.text[:2000]
        assert lctx.ledger.get(GH).state == "ready" and lctx.has_live_tab(GH)
        assert lctx.answers.lookup(a["missing"][0]["label"], "yes_no") == "Yes"     # learned for next time
        r = c.post(f"/app/{a['id']}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert "would_submit" in r.text
        c.portal.call(sess.close)


def test_run_review_fills_then_serves_with_tabs_open(lctx, monkeypatch):
    import autoapply.pipeline as pl
    import autoapply.review.server as server

    def fake_run(ctx, limit=None, no_fill=False, fill_runner=None):
        seed(ctx, GH)
        stats = RunStats()
        fill_runner(ctx, [GH], stats)                    # bridges into the session's browser
        return stats.as_dict()

    seen = {}

    async def fake_serve(app, port, open_browser):
        seen["live"] = sorted(lctx.live_tabs())
        seen["state"] = lctx.ledger.get(GH).state

    monkeypatch.setattr(pl, "run", fake_run)
    monkeypatch.setattr(server, "_serve", fake_serve)
    lctx.keep_tabs = False                               # run_and_review must turn it on itself
    assert server.run_and_review(lctx, open_browser=False) == 0
    assert seen == {"live": [GH], "state": "needs_input"}
