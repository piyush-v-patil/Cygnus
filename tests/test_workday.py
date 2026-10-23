"""Workday: URL identity, job data, and the multi-page application against a local
stand-in (fixtures/forms/workday.html) built from live Workday element ids."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoapply.ats import application_url, detect, parse_key, posting_key
from autoapply.ats.workday import WorkdayClient
from autoapply.browser.fill import fill_many
from autoapply.browser.launch import BrowserSession
from autoapply.browser.submit import submit
from autoapply.browser.workday import account_blocked, clear_accounts
from autoapply.ledger import ENRICHED, SCORED
from autoapply.pipeline import RunStats, fill_candidates
from autoapply.review.server import create_app

from .conftest import CHROMIUM

FORMS = Path(__file__).parent / "fixtures" / "forms"
WD = "workday:acme@acme.wd5.myworkdayjobs.com:External/Backend-Engineer_R100"
WD2 = "workday:acme@acme.wd5.myworkdayjobs.com:External/Platform-Engineer_R101"
LOGIN = {"WORKDAY_EMAIL": "jane@example.com", "WORKDAY_PASSWORD": "Correct-Horse-1!"}


# ------------------------------------------------------------- identity --
@pytest.mark.parametrize("url,key", [
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Romania-Remote/Senior-Dev_JR2027944",
     "workday:nvidia@nvidia.wd5.myworkdayjobs.com:NVIDIAExternalCareerSite/Senior-Dev_JR2027944"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-TX-Austin/Senior-Dev_JR2027944/apply",
     "workday:nvidia@nvidia.wd5.myworkdayjobs.com:NVIDIAExternalCareerSite/Senior-Dev_JR2027944"),
    ("https://leidos.wd5.myworkdayjobs.com/External/details/Entry-Level_R-00186923",
     "workday:leidos@leidos.wd5.myworkdayjobs.com:External/Entry-Level_R-00186923"),
    ("https://wd3.myworkdaysite.com/en-US/recruiting/mondelez/External/job/Chicago-IL/SWE_R123",
     "workday:mondelez@wd3.myworkdaysite.com:External/SWE_R123"),
])
def test_workday_urls_identity_ignores_location_and_locale(url, key):
    ref = detect(url)
    assert ref.supported and posting_key(ref, url) == key


def test_workday_application_url_goes_straight_to_apply_manually():
    assert application_url(parse_key(WD)) == \
        "https://acme.wd5.myworkdayjobs.com/External/job/Backend-Engineer_R100/apply/applyManually"
    assert application_url(parse_key("workday:mondelez@wd3.myworkdaysite.com:External/SWE_R123")).startswith(
        "https://wd3.myworkdaysite.com/recruiting/mondelez/External/job/SWE_R123")


def test_not_a_workday_job_page():
    assert not detect("https://x.wd5.myworkdayjobs.com/a").supported


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp, self.urls = resp, []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.resp


def test_workday_job_data_and_closed_postings():
    ok = _Http(_Resp(200, {"jobPostingInfo": {"title": "Backend Engineer", "location": "Austin, TX",
                                              "jobDescription": "<p>Build <b>Python</b> services.</p>",
                                              "canApply": True, "posted": True}}))
    d = WorkdayClient(ok).fetch(parse_key(WD))
    assert not d.closed and d.title == "Backend Engineer" and "Python services" in d.description_text
    assert ok.urls == ["https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Backend-Engineer_R100"]
    assert WorkdayClient(_Http(_Resp(403, {"errorCode": "S22"}))).fetch(parse_key(WD)).closed
    assert WorkdayClient(_Http(_Resp(200, {"jobPostingInfo": {"canApply": False}}))).fetch(parse_key(WD)).closed


# -------------------------------------------------------------- the walk --
def seed(ctx, key, company="Acme"):
    led = ctx.ledger
    led.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key,
                                "company": company, "title": "Backend Engineer", "role": "backend",
                                "posted_at": "2026-10-20"})
    led.set_state(key, ENRICHED)
    led.save_details(key, "Build Python backend services at Acme.", [])
    led.save_score(key, {"score": 80, "role_family": "backend", "reasons": ["match"], "blockers": []}, "m", "score-v1", "h")
    led.set_state(key, SCORED)


def simplify_mock(sponsorship="Yes"):
    async def mock(page):          # fills what Simplify knows on whichever page is showing
        await page.evaluate("""(sp) => {
          const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
          set('legalName--firstName', 'Jane'); set('legalName--lastName', 'Doe'); set('email', 'jane@example.com');
          set('phone-number', '5125550100');
          const pick = (id, v) => { const b = document.getElementById(id); if (b) b.textContent = v; };
          pick('countryDropdown', 'United States of America'); pick('phone-device-type', 'Mobile');
          pick('sponsorship', sp);
        }""", sponsorship)
    return mock


@pytest.fixture
def wctx(make_ctx, tmp_path):
    def _make(password="Correct-Horse-1!", test_mode=True, sponsorship="Yes"):
        ctx = make_ctx(test_mode=test_mode, secrets=dict(LOGIN))
        ctx.profile.filters.ats_allowed = ["greenhouse", "ashby", "lever", "workday"]
        ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
        url = (FORMS / "workday.html").resolve().as_uri() + f"?email=jane@example.com&pw={password}"
        ctx.form_url_for = lambda key: url
        ctx.simplify_mock = simplify_mock(sponsorship)
        ctx.keep_tabs = True
        return ctx
    return _make




@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_walks_the_pages_and_stops_where_the_user_must_answer(wctx, transport):
    ctx = wctx()
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
            tab = ctx.open_tabs.get(WD)
            from autoapply.browser.workday import step_name
            return await step_name(tab) if tab else None
    where = asyncio.run(go())
    app = ctx.ledger.application(WD)
    assert ctx.ledger.get(WD).state == "needs_input", ctx.ledger.get(WD).reason
    assert where == "Application Questions"                              # the tab waits on that page
    assert [m["label"] for m in app["missing"]] == ["Are you open to relocating to Austin?*"]
    plan = {e["label"].rstrip("*"): e for e in app["fill_plan"]}
    assert plan["Given Name(s)"]["source"] == "simplify" and plan["Country"]["value"] == "United States of America"
    assert plan["Resume/CV"]["source"] == "resume" and plan["Resume/CV"]["value"] == "backend.txt"
    assert plan["Why do you want to work at Acme?"]["source"] == "claude"
    assert app["missing"][0]["options"] == ["Yes", "No"]


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_answering_goes_on_to_review_then_approve_submits_with_confirmation(wctx):
    ctx = wctx(test_mode=False)
    seed(ctx, WD)
    app = create_app(ctx, port=8765)
    sess = app.state.session
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        async def fill():
            await fill_many(ctx, [WD], RunStats(), session=await sess.browser())
        c.portal.call(fill)
        a = ctx.ledger.application(WD)
        key = a["missing"][0]["key"]
        r = c.post(f"/app/{a['id']}/answers", data={"csrf": sess.csrf, f"a__{key}": "Yes", "then": "save"})
        assert "went on to the Review page" in r.text, r.text[:3000]
        assert ctx.ledger.get(WD).state == "ready"
        assert ctx.answers.lookup("Are you open to relocating to Austin?", "yes_no") == "Yes"   # learned
        r = c.post(f"/app/{a['id']}/approve", data={"csrf": sess.csrf, "lane": "ready"})
        assert "submitted" in r.text, r.text[:3000]
        tab_ok = c.portal.call(lambda: ctx.ledger.get(WD).state)
        c.portal.call(sess.close)
    assert tab_ok == "submitted"
    sent = json.loads((Path(ctx.ledger.application(WD)["submission_dir"]) / "fields.json").read_text())
    assert {f["label"].rstrip("*"): f["value"] for f in sent}["Are you open to relocating to Austin?"] == "Yes"


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_lost_tab_is_walked_again_from_the_start_with_the_answers_given(wctx, monkeypatch):
    ctx = wctx()
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
            from autoapply.review.actions import save_answers
            a = ctx.ledger.application(WD)
            save_answers(ctx.ledger, ctx.answers, WD, {a["missing"][0]["key"]: "No"})
            tab, _ = ctx.take_tab(WD)
            await tab.close()                                    # the tab is gone: approval reopens it
            return await submit(ctx, bs, WD)
    res = asyncio.run(go())
    assert res["state"] == "would_submit", res


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_wrong_simplify_sponsorship_answer_is_corrected_on_workday(wctx):
    ctx = wctx(sponsorship="No")
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
    asyncio.run(go())
    entry = next(e for e in ctx.ledger.application(WD)["fill_plan"] if "sponsorship" in e["label"])
    assert entry["source"] == "profile" and entry["value"] == "Yes"


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_failed_sign_in_holds_that_companys_postings_until_requeued(wctx):
    ctx = wctx(password="Different-Password-9!")          # the account does not take our login
    seed(ctx, WD)
    seed(ctx, WD2)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
    asyncio.run(go())
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "workday_sign_in_failed" in row.reason
    assert account_blocked(ctx, WD2) and WD2 not in fill_candidates(ctx, None)
    clear_accounts(ctx, [WD])                               # what `autoapply requeue --lane stuck` does
    assert WD2 in fill_candidates(ctx, None)


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_no_workday_login_is_a_clear_stuck_reason(wctx):
    ctx = wctx()
    ctx._secrets = {}
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
    asyncio.run(go())
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "autoapply workday login" in row.reason


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_workday_widgets_take_saved_answers(wctx):
    ctx = wctx()
    ctx.answers.add("Type to Add Skills", "choice", "Python")              # search-and-pick list
    ctx.answers.add("Earliest start date", "short_text", "06/2027")        # month / year boxes
    ctx.answers.add("Gender", "choice", "Decline to self-identify")        # listbox button
    ctx.answers.add("Are you open to relocating to Austin?", "yes_no", "No")  # radio
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
            tab = ctx.open_tabs[WD]
            return await tab.evaluate("() => document.querySelector('[data-automation-id=applyFlowReviewPage]').innerText")
    review = asyncio.run(go())
    assert ctx.ledger.get(WD).state == "ready", ctx.ledger.application(WD)["missing"]
    assert "formField-skills: Python" in review and "formField-availableFrom: 06/2027" in review
    assert "formField-gender: Decline to self-identify" in review and "formField-relocate: No" in review


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_account_made_with_simplifys_button_signs_in_with_its_password(wctx, monkeypatch):
    import autoapply.browser.simplify as simplify_mod
    from autoapply.browser.workday import accounts

    async def simplify_saved(context):        # what Simplify's Create Account button stored
        return {"email": "Jane@Example.com", "password": "Simplify-Made-7!"}
    monkeypatch.setattr(simplify_mod, "signup_credentials", simplify_saved)
    ctx = wctx(password="Simplify-Made-7!")   # the site only takes Simplify's password
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
    asyncio.run(go())
    assert ctx.ledger.get(WD).state == "needs_input", ctx.ledger.get(WD).reason   # signed in, walked on
    acc = accounts(ctx)["acme@acme.wd5.myworkdayjobs.com"]
    assert acc["status"] == "ok" and acc["password"] == "simplify"


def test_sign_in_failure_explains_the_usual_causes():
    from autoapply.browser.workday import NOT_ACCEPTED
    msg = NOT_ACCEPTED.format(company="capitalone", text="wrong email or password")
    assert "Forgot your password" in msg and "verified" in msg and "workday open capitalone" in msg
