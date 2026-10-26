"""Review UI (§10): lanes, answers learning + propagation, reject, guards, approve."""
import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoapply.ledger import NEEDS_INPUT, READY
from autoapply.review.actions import save_answers
from autoapply.review.server import create_app

from .conftest import CHROMIUM
from .test_browser import FORMS, GH, seed, simplify_mock

Q = "Are you willing to work onsite 5 days a week?*"


def needs_input_app(ctx, key, company, extra_missing=()):
    seed(ctx, key, company=company)
    ctx.ledger.set_state(key, "filling")
    missing = [{"key": "id:q105", "label": Q, "kind": "combobox", "answer_kind": "yes_no",
                "options": ["Yes", "No"], "required": True, "reason": "not in resume",
                "question": "are you willing to work onsite 5 days a week"}, *extra_missing]
    ctx.ledger.upsert_application(key, lane=NEEDS_INPUT, resume_file="general.txt",
                                  fill_plan=[{"key": "id:first_name", "label": "First Name*", "kind": "text",
                                              "value": "Jane", "source": "simplify"}], missing=missing)
    ctx.ledger.set_state(key, NEEDS_INPUT)


def test_answers_learned_and_propagated(make_ctx):
    ctx = make_ctx()
    needs_input_app(ctx, "greenhouse:a:1", "A")
    needs_input_app(ctx, "greenhouse:b:2", "B")
    needs_input_app(ctx, "greenhouse:c:3", "C", extra_missing=[
        {"key": "id:x", "label": "Salary expectation", "kind": "text", "answer_kind": "short_text",
         "options": [], "required": True, "reason": "", "question": "salary expectation"}])
    res = save_answers(ctx.ledger, ctx.answers, "greenhouse:a:1", {"id:q105": "yes"})
    assert sorted(res["ready"]) == ["greenhouse:a:1", "greenhouse:b:2"]
    assert ctx.ledger.get("greenhouse:c:3").state == NEEDS_INPUT          # still has the salary question
    c = ctx.ledger.application("greenhouse:c:3")
    assert [m["label"] for m in c["missing"]] == ["Salary expectation"]
    entry = ctx.ledger.application("greenhouse:a:1")["fill_plan"][-1]
    assert entry == {"key": "id:q105", "label": Q, "kind": "combobox", "value": "Yes", "source": "user",
                     "required": True, "options": ["Yes", "No"]}
    assert ctx.answers.lookup(Q, "yes_no") == "Yes"
    assert "are you willing to work onsite 5 days a week" in ctx.paths.answers.read_text()


def test_invalid_choice_is_not_saved(make_ctx):
    ctx = make_ctx()
    needs_input_app(ctx, "greenhouse:a:1", "A")
    res = save_answers(ctx.ledger, ctx.answers, "greenhouse:a:1", {"id:q105": "Sometimes"})
    assert res["still_missing"] == 1 and ctx.answers.items == []


@pytest.fixture
def client(make_ctx):
    ctx = make_ctx(secrets={"GITHUB_TOKEN": "t"})
    app = create_app(ctx, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.ctx = ctx
        c.csrf = app.state.session.csrf
        yield c


def test_lanes_render_with_badges_and_sources(client):
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    r = client.get("/lane/needs_input")
    assert r.status_code == 200
    assert "Acme — Backend Engineer" in r.text and Q in r.text
    assert re.search(r"Needs input<span class=\"badge\">1</span>", r.text)
    assert "Spend this month" in r.text
    assert client.get("/lane/ready").text.count('class="card"') == 0
    assert client.get("/history").status_code == 200


def test_host_and_csrf_guards(client):
    assert client.get("/lane/ready", headers={"host": "evil.example"}).status_code == 403
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    app_id = client.ctx.ledger.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/reject", data={"csrf": "wrong"})
    assert r.status_code == 403
    r = client.post(f"/app/{app_id}/reject", data={"csrf": client.csrf},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_reject_writes_back_skip(client):
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    app_id = client.ctx.ledger.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/reject", data={"csrf": client.csrf, "lane": "needs_input"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.ctx.ledger.get("greenhouse:a:1").state == "rejected_by_user"
    # the job-monitor id this posting came from gets status skip / user_rejected
    pend = client.ctx.ledger.pending_writebacks(client.ctx.profile.sources[0].label)
    assert pend[-1]["change"] == {"status": "skip", "skip_reason": "user_rejected", "skipped_by": "autoapply"}


def test_uploads_attached_in_the_tab_count_as_answered(make_ctx):
    ctx = make_ctx()
    needs_input_app(ctx, "greenhouse:a:1", "A", extra_missing=[
        {"key": "id:t", "label": "Transcript", "kind": "file", "answer_kind": "short_text",
         "options": [], "required": True, "reason": "", "question": "transcript"}])
    res = save_answers(ctx.ledger, ctx.answers, "greenhouse:a:1", {"id:q105": "Yes"}, attached={"id:t"})
    assert res["still_missing"] == 0 and ctx.ledger.get("greenhouse:a:1").state == READY
    plan = ctx.ledger.application("greenhouse:a:1")["fill_plan"]
    assert [e["key"] for e in plan] == ["id:first_name", "id:q105"]      # nothing to replay for the upload


def test_answering_lands_on_the_card_in_ready(client):
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    app_id = client.ctx.ledger.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/answers", data={"csrf": client.csrf, "a__id:q105": "No", "then": "save"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/lane/ready?") and r.headers["location"].endswith(f"#app{app_id}")
    assert client.ctx.ledger.get("greenhouse:a:1").state == READY


def test_from_form_without_an_open_tab_explains(client):
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    app_id = client.ctx.ledger.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/from-form", data={"csrf": client.csrf})
    assert "That form is not open" in r.text
    assert client.ctx.ledger.get("greenhouse:a:1").state == NEEDS_INPUT


def test_answer_form_post(client):
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    app_id = client.ctx.ledger.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/answers", data={"csrf": client.csrf, "a__id:q105": "No", "then": "save"})
    assert r.status_code == 200 and "Saved." in r.text
    assert client.ctx.ledger.get("greenhouse:a:1").state == READY


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_approve_in_test_mode_through_the_ui(make_ctx, tmp_path):
    from autoapply.browser.fill import fill_many
    from autoapply.pipeline import RunStats
    ctx = make_ctx(test_mode=True)
    ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
    ctx.form_url_for = lambda key: (FORMS / "greenhouse.html").resolve().as_uri()
    ctx.simplify_mock = simplify_mock()
    seed(ctx, GH)
    asyncio.run(fill_many(ctx, [GH], RunStats()))
    assert ctx.ledger.get(GH).state == NEEDS_INPUT
    app = create_app(ctx, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        app_id = ctx.ledger.application(GH)["id"]
        key = ctx.ledger.application(GH)["missing"][0]["key"]
        r = c.post(f"/app/{app_id}/answers", data={"csrf": app.state.session.csrf, f"a__{key}": "Yes",
                                                   "then": "submit"})
        # answering never submits (not even the old "Save & approve & submit" button): it shows the real form and the card waits in Ready
        assert ctx.ledger.get(GH).state == READY and ctx.has_live_tab(GH)
        assert "Check every field in the form" in r.text and "Open form to check" in r.text
        r = c.post(f"/app/{app_id}/approve", data={"csrf": app.state.session.csrf, "lane": "ready"})
        assert "would_submit" in r.text, r.text[:2000]
        c.portal.call(app.state.session.close)
    assert ctx.ledger.get(GH).state == "would_submit"


def test_i_submitted_it_from_ready_and_needs_input(client):
    from autoapply.ledger import SUBMITTED_MANUAL
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    needs_input_app(client.ctx, "greenhouse:b:2", "Beta", extra_missing=[       # stays in Needs input
        {"key": "id:x", "label": "Salary expectation", "kind": "text", "answer_kind": "short_text",
         "options": [], "required": True, "reason": "", "question": "salary expectation"}])
    save_answers(client.ctx.ledger, client.ctx.answers, "greenhouse:a:1", {"id:q105": "Yes"})
    assert client.ctx.ledger.get("greenhouse:a:1").state == READY
    page = client.get("/lane/ready").text
    assert page.count("I submitted it") == 1
    for key, lane in (("greenhouse:a:1", "ready"), ("greenhouse:b:2", "needs_input")):
        app_id = client.ctx.ledger.application(key)["id"]
        r = client.post(f"/app/{app_id}/manual", data={"csrf": client.csrf, "lane": lane}, follow_redirects=False)
        assert r.headers["location"].startswith(f"/lane/{lane}?")
        assert client.ctx.ledger.get(key).state == SUBMITTED_MANUAL
    pend = client.ctx.ledger.pending_writebacks(client.ctx.profile.sources[0].label)
    assert [p["change"]["status"] for p in pend[-2:]] == ["applied", "applied"]


def test_fill_again_from_the_stuck_page(client):
    from autoapply.ledger import STUCK
    led = client.ctx.ledger
    for key in ("greenhouse:a:1", "greenhouse:b:2", "greenhouse:c:3"):
        needs_input_app(client.ctx, key, key.split(":")[1])
        led.upsert_application(key, lane=STUCK, stuck_reason="captcha: the page asks for a CAPTCHA")
        led.set_state(key, STUCK, "captcha")
    page = client.get("/lane/stuck").text
    assert "Fill all again on the next run (3)" in page and page.count(">Fill again</button>") == 3
    app_id = led.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/retry", data={"csrf": client.csrf})
    assert "filled again on the next" in r.text and led.get("greenhouse:a:1").state == "scored"
    r = client.post("/retry-lane/stuck", data={"csrf": client.csrf})
    assert "2 application(s) will be filled again" in r.text
    assert {led.get(k).state for k in ("greenhouse:b:2", "greenhouse:c:3")} == {"scored"}


def test_fill_again_from_needs_input(client):
    led = client.ctx.ledger
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    needs_input_app(client.ctx, "greenhouse:b:2", "Beta")
    page = client.get("/lane/needs_input").text
    assert "Fill all again on the next run (2)" in page and page.count(">Fill again</button>") == 2
    app_id = led.application("greenhouse:a:1")["id"]
    r = client.post(f"/app/{app_id}/retry", data={"csrf": client.csrf, "lane": "needs_input"}, follow_redirects=False)
    assert r.headers["location"].startswith("/lane/needs_input?") and led.get("greenhouse:a:1").state == "scored"
    client.post("/retry-lane/needs_input", data={"csrf": client.csrf})
    assert led.get("greenhouse:b:2").state == "scored"
    assert client.post("/retry-lane/ready", data={"csrf": client.csrf}).status_code == 404


def test_stale_card_actions_change_nothing(client):
    from autoapply.ledger import SUBMITTED
    led = client.ctx.ledger
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    save_answers(led, client.ctx.answers, "greenhouse:a:1", {"id:q105": "Yes"})
    led.upsert_application("greenhouse:a:1", submission_dir="sent-folder")
    led.set_state("greenhouse:a:1", SUBMITTED)                    # sent from another tab meanwhile
    app_id = led.application("greenhouse:a:1")["id"]
    for route in ("manual", "reject", "giveup", "retry", "open", "approve"):
        r = client.post(f"/app/{app_id}/{route}", data={"csrf": client.csrf}, follow_redirects=False)
        assert r.status_code == 303, route
        assert "already%20submitted" in r.headers["location"], route
    assert led.get("greenhouse:a:1").state == SUBMITTED
    assert led.application("greenhouse:a:1")["submission_dir"] == "sent-folder"


def test_review_opens_despite_budget_and_github_trouble(make_ctx):
    from autoapply.health import run_health
    ctx = make_ctx()                                                 # writeback on, no GITHUB_TOKEN
    ctx.ledger.record_usage("x", "m", {}, 99, None)                   # over the budget
    rep = run_health(ctx, probe_browser=False, review=True)
    assert rep.ok and any("budget" in w for w in rep.warnings) and any("github" in w for w in rep.warnings)
    assert not run_health(ctx, probe_browser=False).ok               # a run still stops


def test_only_web_links_render(client):
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    client.ctx.ledger.db.execute("UPDATE postings SET apply_url=? WHERE posting_key=?",
                                 ("javascript:alert(1)", "greenhouse:a:1"))
    page = client.get("/lane/needs_input").text
    assert "Acme" in page and "javascript:" not in page


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Chromium")
def test_closed_browser_window_is_reopened(make_ctx, tmp_path):
    from autoapply.review.server import Session
    ctx = make_ctx()
    ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
    sess = Session(ctx)

    async def go():
        first = await sess.browser()
        await first.context.close()                   # the user closed the window
        second = await sess.browser()
        assert second is not first and not second.closed
        page = await second.new_page()
        await page.goto("about:blank")
        await sess.close()
    asyncio.run(go())


def test_paused_company_card_says_so_and_approve_all_leaves_it(client):
    from autoapply.companies import pause
    needs_input_app(client.ctx, "greenhouse:a:1", "Acme")
    save_answers(client.ctx.ledger, client.ctx.answers, "greenhouse:a:1", {"id:q105": "Yes"})
    pause(client.ctx, "acme")
    page = client.get("/lane/ready").text
    assert "Acme paused until" in page
    app_id = client.ctx.ledger.application("greenhouse:a:1")["id"]
    r = client.post("/approve-all", data={"csrf": client.csrf, "ids": str(app_id)})
    assert "Left for you (company paused): Acme" in r.text
    assert client.ctx.ledger.get("greenhouse:a:1").state == READY
