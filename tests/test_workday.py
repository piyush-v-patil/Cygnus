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
    from autoapply.ats import FetchError
    with pytest.raises(FetchError):                    # bot protection or rate limits: retried, not closed
        WorkdayClient(_Http(_Resp(403, {"errorCode": "WAF"}))).fetch(parse_key(WD))
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
    def _make(password="Correct-Horse-1!", test_mode=True, sponsorship="Yes", params=""):
        ctx = make_ctx(test_mode=test_mode, secrets=dict(LOGIN))
        ctx.profile.filters.ats_allowed = ["greenhouse", "ashby", "lever", "workday"]
        ctx.profile.workday.after_create_wait_s = 0.5
        ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
        url = (FORMS / "workday.html").resolve().as_uri() + f"?email=jane@example.com&pw={password}{params}"
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
            first = await submit(ctx, bs, WD)                    # walks to Review, does not submit
            from autoapply.browser.workday import state
            on_review = await state(ctx.open_tabs[WD]) == "review"
            second = await submit(ctx, bs, WD)                   # you looked; now it is sent
            return first, on_review, second
    first, on_review, second = asyncio.run(go())
    assert first["state"] == "ready" and "Review page is open" in first["detail"], first
    assert on_review and second["state"] == "would_submit", second


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



def _fill(ctx, key=WD):
    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [key], RunStats(), session=bs)
    asyncio.run(go())


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_no_account_yet_creates_one_waits_and_signs_in(wctx):
    from autoapply.browser.workday import accounts
    ctx = wctx(params="&noaccount=1")
    seed(ctx, WD)
    _fill(ctx)
    assert ctx.ledger.get(WD).state == "needs_input", ctx.ledger.get(WD).reason      # signed in, walked on
    acc = accounts(ctx)["acme@acme.wd5.myworkdayjobs.com"]
    assert acc["status"] == "ok" and acc["note"] == "created by Cygnus"


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_account_that_exists_with_another_password_is_not_created_again(wctx):
    ctx = wctx(password="Someone-Elses-9!")       # an account exists, with a different password
    seed(ctx, WD)
    _fill(ctx)
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "already exists" in row.reason and "Forgot your password" in row.reason


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_new_account_that_needs_email_verification_says_so(wctx):
    ctx = wctx(params="&noaccount=1&verify=1")
    seed(ctx, WD)
    _fill(ctx)
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "workday_verify_email" in row.reason and "verif" in row.reason


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_account_creation_can_be_switched_off(wctx):
    ctx = wctx(params="&noaccount=1")
    ctx.profile.workday.create_accounts = False
    seed(ctx, WD)
    _fill(ctx)
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "workday_sign_in_failed" in row.reason and "workday open acme" in row.reason


def test_workday_password_rules():
    from autoapply.browser.workday import password_problems
    assert password_problems("Correct-Horse-1!") == []
    assert password_problems("Short1!") == ["at least 8 characters"]
    assert set(password_problems("password")) == {"a number", "a special character", "an uppercase letter"}
    assert password_problems("password1!") == ["an uppercase letter"]          # Workday's case rules too
    assert password_problems("PASSWORD1!") == ["a lowercase letter"]


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_workday_applications_run_one_at_a_time_after_single_page_forms(wctx, monkeypatch):
    import autoapply.browser.workday as wd_mod
    from .test_browser import GH, FORMS as GH_FORMS, simplify_mock as gh_mock
    ctx = wctx()
    ctx.profile.browser.parallel_tabs = 3
    other = "workday:beta@beta.wd5.myworkdayjobs.com:External/Backend-Engineer_R200"
    wd_url = ctx.form_url_for(WD)
    ctx.form_url_for = lambda key: (GH_FORMS / "greenhouse.html").resolve().as_uri() if key == GH else wd_url
    wd_simplify, gh_simplify = simplify_mock(), gh_mock()

    async def mock(page):
        await (gh_simplify if "greenhouse" in page.url else wd_simplify)(page)
    ctx.simplify_mock = mock
    order, running, most = [], [0], [0]
    real_walk = wd_mod.walk

    async def counting_walk(ctx_, page, key, *a, **kw):
        running[0] += 1
        most[0] = max(most[0], running[0])
        try:
            return await real_walk(ctx_, page, key, *a, **kw)
        finally:
            running[0] -= 1
            order.append(key)
    monkeypatch.setattr(wd_mod, "walk", counting_walk)
    seed(ctx, WD)
    seed(ctx, other, company="Beta")
    from .test_browser import seed as gh_seed
    gh_seed(ctx, GH, company="Gamma")

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD, other, GH], RunStats(), session=bs)
    asyncio.run(go())
    assert most[0] == 1                                    # never two Workday walks at once
    assert ctx.ledger.get(GH).state in ("ready", "needs_input")
    assert {ctx.ledger.get(k).state for k in (WD, other)} == {"needs_input"}


@pytest.mark.browser
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")
def test_thank_you_text_in_a_question_page_is_not_a_confirmation():
    from playwright.async_api import async_playwright
    from autoapply.browser.workday import state

    page_html = """<div data-automation-id="applyFlowPage"><div data-automation-id="richText">Thank you for applying
      to Acme! Please answer a few questions.</div><div data-automation-id="formField-q"><label>Why Acme?*</label>
      <textarea></textarea></div><button data-automation-id="bottom-navigation-next-button">Save and Continue</button></div>"""
    done_html = """<div role="dialog"><h2>Application Submitted</h2></div>"""

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)
            page = await b.new_page()
            await page.set_content(page_html)
            first = await state(page)
            await page.set_content(done_html)
            second = await state(page)
            await b.close()
            return first, second
    assert asyncio.run(go()) == ("form", "submitted")


BROWSER = [pytest.mark.browser, pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]


def _walk_with(ctx, key=WD):
    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [key], RunStats(), session=bs)
            tab = ctx.open_tabs.get(key)
            return await tab.evaluate("() => ({clicks: window.__applyClicks || 0, resume: window.__resumeUploaded || null, "
                                      "text: document.body.innerText})") if tab and not tab.is_closed() else {}
    return asyncio.run(go())


@BROWSER[0]
@BROWSER[1]
def test_two_steps_with_the_same_name_are_walked_through(wctx):
    ctx = wctx(params="&dupsteps=1")
    ctx.answers.add("Are you open to relocating to Austin?", "yes_no", "Yes")
    seed(ctx, WD)
    seen = _walk_with(ctx)
    row = ctx.ledger.get(WD)
    assert row.state == "ready", row.reason
    assert "formField-salary" in seen["text"]                 # the second "Application Questions" page was saved


@BROWSER[0]
@BROWSER[1]
def test_apply_without_apply_manually_stops_instead_of_clicking_forever(wctx):
    ctx = wctx(params="&job=1&noapply=1")
    seed(ctx, WD)
    seen = _walk_with(ctx)
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "workday_no_apply_manually" in row.reason, row.reason
    assert "Autofill with Resume" in row.reason and seen["clicks"] <= 2


@BROWSER[0]
@BROWSER[1]
def test_job_page_apply_manually_leads_into_the_application(wctx):
    ctx = wctx(params="&job=1")
    seed(ctx, WD)
    _walk_with(ctx)
    assert ctx.ledger.get(WD).state == "needs_input", ctx.ledger.get(WD).reason


@BROWSER[0]
@BROWSER[1]
def test_unknown_page_is_named_not_called_closed(wctx):
    ctx = wctx(params="&unknown=1")
    ctx.profile.browser.page_timeout_s = 3
    seed(ctx, WD)
    _walk_with(ctx)
    row = ctx.ledger.get(WD)
    assert row.state == "stuck" and "workday_unknown_page" in row.reason and "Candidate Home" in row.reason


@BROWSER[0]
@BROWSER[1]
def test_a_failed_sign_in_is_not_repeated_for_the_same_company(wctx):
    ctx = wctx(password="Different-Password-9!")
    ctx.profile.workday.create_accounts = False
    seed(ctx, WD)
    seed(ctx, WD2)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD, WD2], RunStats(), session=bs)
    asyncio.run(go())
    assert ctx.ledger.get(WD).state == "stuck"
    assert ctx.ledger.get(WD2).state == SCORED                # never tried: it waits for the fix
    assert "filling" not in str(ctx.ledger.events(WD2))     # not even opened


@BROWSER[0]
@BROWSER[1]
def test_sections_sub_menus_and_a_loose_resume_box(wctx):
    from autoapply.browser.workday import resume
    from autoapply.review.actions import save_answers
    ctx = wctx(params="&sections=1&hier=1&loose=1")
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
            a = ctx.ledger.application(WD)
            missing = {m["label"].rstrip("*"): m for m in a["missing"]}
            uploaded = await ctx.open_tabs[WD].evaluate("() => window.__resumeUploaded || null")
            answers = {missing["Work Experience 1 › Job Title"]["key"]: "Backend Engineer",
                       missing["Work Experience 2 › Job Title"]["key"]: "Intern",
                       missing["How Did You Hear About Us?"]["key"]: "Job Board > LinkedIn Job Posting"}
            save_answers(ctx.ledger, ctx.answers, WD, answers)
            on = await resume(ctx, ctx.open_tabs[WD], WD)
            from autoapply.browser.workday import step_name
            return missing, uploaded, on, await step_name(ctx.open_tabs[WD])
    missing, uploaded, on, where = asyncio.run(go())
    assert set(missing) == {"Work Experience 1 › Job Title", "Work Experience 2 › Job Title", "How Did You Hear About Us?"}
    assert missing["How Did You Hear About Us?"]["options"] == [
        "Job Board > LinkedIn Job Posting", "Job Board > Indeed", "Referral > Employee Referral"]
    assert all(m.get("no_bank") for k, m in missing.items() if k.startswith("Work Experience"))
    assert uploaded == "backend.txt"                          # the role's resume, outside any formField block
    assert where == "Application Questions" and not on        # past My Experience: every answer was taken
    assert ctx.answers.lookup("Work Experience 1 › Job Title", "short_text") is None       # never banked
    assert ctx.answers.lookup("How Did You Hear About Us?", "choice",
                              ["Job Board > LinkedIn Job Posting", "Job Board > Indeed"]) == "Job Board > LinkedIn Job Posting"


def test_workday_field_errors_are_read_from_input_error():
    from playwright.async_api import async_playwright
    from autoapply.browser.workday import banner_errors, page_errors
    html = """<div data-automation-id="formField-q" data-aa-key="wd:q"><label>Start date*</label><input>
      <div data-automation-id="inputError">Enter a date in the future</div></div>
      <div data-automation-id="errorWidgetBarMessage">Errors Found: 1</div>"""

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)
            page = await b.new_page()
            await page.set_content(html)
            out = await page_errors(page), await banner_errors(page)
            await b.close()
            return out
    errs, banner = asyncio.run(go())
    assert errs == {"wd:q": "Enter a date in the future"} and banner == "Errors Found: 1"


def test_simplify_auto_submit_setting_is_turned_off():
    from playwright.async_api import async_playwright
    from autoapply.browser.simplify import AUTO_ADVANCE_OFF_JS

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)
            page = await b.new_page()
            await page.evaluate("""() => {
              const store = {"persist:settings": JSON.stringify({settings: {autoAdvance: true, other: 1}}),
                             "obj": {value: {settings: {autoAdvance: true}}}, "consent": {x: 1}};
              window.__store = store;
              window.chrome = {storage: {local: {
                get: async () => JSON.parse(JSON.stringify(store)),
                set: async (o) => Object.assign(store, o)}}};
            }""")
            first = await page.evaluate(AUTO_ADVANCE_OFF_JS)
            again = await page.evaluate(AUTO_ADVANCE_OFF_JS)
            store = await page.evaluate("() => window.__store")
            await b.close()
            return first, again, store
    first, again, store = asyncio.run(go())
    assert first == 2 and again == 0
    assert json.loads(store["persist:settings"])["settings"] == {"autoAdvance": False, "other": 1}
    assert store["obj"]["value"]["settings"]["autoAdvance"] is False and store["consent"] == {"x": 1}


@BROWSER[0]
@BROWSER[1]
def test_an_old_workday_tab_is_walked_again_before_submit(wctx):
    import time
    from autoapply.browser.workday import resume
    from autoapply.review.actions import save_answers
    ctx = wctx(test_mode=False)
    seed(ctx, WD)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
            a = ctx.ledger.application(WD)
            save_answers(ctx.ledger, ctx.answers, WD, {a["missing"][0]["key"]: "Yes"})
            assert await resume(ctx, ctx.open_tabs[WD], WD)                 # on Review
            ctx.tab_opened[WD] = time.monotonic() - (ctx.profile.browser.tab_max_age_min + 1) * 60
            return await submit(ctx, bs, WD)
    res = asyncio.run(go())
    assert res["state"] == "ready" and "Review page is open" in res["detail"], res     # shown again, not sent
    assert ctx.ledger.get(WD).state == "ready"


@BROWSER[0]
@BROWSER[1]
def test_a_crash_after_clicking_submit_is_unconfirmed_not_failed(wctx, monkeypatch):
    import autoapply.browser.submit as sub_mod
    from autoapply.browser.workday import resume
    from autoapply.review.actions import save_answers
    ctx = wctx(test_mode=False)
    seed(ctx, WD)

    async def boom(*a, **k):
        raise RuntimeError("Target page, context or browser has been closed")

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD], RunStats(), session=bs)
            a = ctx.ledger.application(WD)
            save_answers(ctx.ledger, ctx.answers, WD, {a["missing"][0]["key"]: "Yes"})
            assert await resume(ctx, ctx.open_tabs[WD], WD)
            monkeypatch.setattr(sub_mod, "click_submit", boom)
            return await submit(ctx, bs, WD)
    res = asyncio.run(go())
    row = ctx.ledger.get(WD)
    assert res["state"] == "stuck" and row.reason.startswith("submit_unconfirmed"), row.reason
    assert ctx.ledger.submit_clicked(WD)                     # never sent again without your say-so
    meta = (Path(ctx.ledger.application(WD)["submission_dir"]) / "meta.json")
    assert meta.exists() and "Submit clicked" in meta.read_text()



@BROWSER[0]
@BROWSER[1]
@pytest.mark.parametrize("params,expect", [("&createfirst=1", "ok"), ("&createfirst=1&noaccount=1", "created by Cygnus")])
def test_a_site_that_opens_on_create_account(wctx, params, expect):
    """Mastercard's Workday opens on Create Account, also after a reload (live, 2026-10-24)."""
    from autoapply.browser.workday import accounts
    ctx = wctx(params=params)
    seed(ctx, WD)
    _walk_with(ctx)
    assert ctx.ledger.get(WD).state == "needs_input", ctx.ledger.get(WD).reason          # signed in, walked on
    acc = accounts(ctx)["acme@acme.wd5.myworkdayjobs.com"]
    assert acc["status"] == "ok" and (acc.get("note") == expect or expect == "ok")


@BROWSER[0]
@BROWSER[1]
def test_no_create_form_blocks_the_company_instead_of_retrying(wctx, monkeypatch):
    import autoapply.browser.workday as wdm
    from autoapply.browser.workday import account_blocked
    ctx = wctx(params="&noaccount=1")
    monkeypatch.setattr(wdm, "CREATE_LINK", '[data-automation-id="nothing-here"]')   # a form the tool cannot find
    seed(ctx, WD)
    seed(ctx, WD2)

    async def go():
        async with BrowserSession(ctx) as bs:
            await fill_many(ctx, [WD, WD2], RunStats(), session=bs)
    asyncio.run(go())
    assert "no Create Account form" in ctx.ledger.get(WD).reason and account_blocked(ctx, WD)
    assert ctx.ledger.get(WD2).state == SCORED                # not tried again in the same run
