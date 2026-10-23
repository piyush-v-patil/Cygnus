"""Fill, lanes, replay and never-submit against local fixture forms (§18).

Simplify is mocked by a function that fills what the real extension would
(contact details, legal yes/no); Claude is the FakeTransport from conftest.
"""
import asyncio
import json
from pathlib import Path

import pytest

from autoapply.browser.fill import fill_many
from autoapply.browser.launch import BrowserSession
from autoapply.browser.submit import SubmitBlocked, click_submit, submit
from autoapply.ledger import ENRICHED, SCORED
from autoapply.pipeline import RunStats

from .conftest import CHROMIUM

FORMS = Path(__file__).parent / "fixtures" / "forms"
pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]

GH = "greenhouse:acme:1001"
ASHBY = "ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
LEVER = "lever:beta:11111111-2222-3333-4444-555555555555"
FORM_FOR = {GH: "greenhouse.html", ASHBY: "ashby.html", LEVER: "lever.html"}


def simplify_mock(sponsorship="Yes"):
    async def mock(page):
        await page.evaluate("""(sp) => {
          const set = (sel, v) => { const e = document.querySelector(sel); if (e) { e.value = v;
            e.dispatchEvent(new Event('input', {bubbles: true})); } };
          set('#first_name', 'Jane'); set('#last_name', 'Doe'); set('#email', 'jane@example.com');
          set('#_systemfield_name', 'Jane Doe'); set('#_systemfield_email', 'jane@example.com');
          set('input[name=name]', 'Jane Doe'); set('input[name=email]', 'jane@example.com');
          const box = document.querySelector('#question_104');
          if (box) { const sv = document.createElement('div'); sv.className = 'select__single-value';
                     sv.textContent = sp; box.closest('.select__container').appendChild(sv); }
          const auth = document.querySelector('[for=q-auth]');
          if (auth) auth.parentElement.querySelector('button').click();
        }""", sponsorship)
    return mock


def seed(ctx, key, company="Acme", title="Backend Engineer", role="backend"):
    led = ctx.ledger
    led.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key,
                                "company": company, "title": title, "role": role, "posted_at": "2026-10-18"})
    led.set_state(key, ENRICHED)
    led.save_details(key, "Build Python backend services at " + company, [])
    led.save_score(key, {"score": 80, "role_family": role, "reasons": ["match"], "blockers": []}, "m", "score-v1", "h")
    led.set_state(key, SCORED)


@pytest.fixture
def bctx(make_ctx, tmp_path):
    ctx = make_ctx()
    ctx.browser_overrides = {"headless": True, "extension": False, "user_data_dir": tmp_path / "bp"}
    ctx.form_url_for = lambda key: (FORMS / FORM_FOR[key]).resolve().as_uri()
    ctx.simplify_mock = simplify_mock()
    return ctx


def test_greenhouse_fill_lanes_and_plan(bctx, transport):
    seed(bctx, GH)
    stats = RunStats()
    asyncio.run(fill_many(bctx, [GH], stats))
    led = bctx.ledger
    assert led.get(GH).state == "needs_input", led.get(GH).reason
    app = led.application(GH)
    by_label = {e["label"]: e for e in app["fill_plan"]}
    assert by_label["First Name*"]["source"] == "simplify"
    assert app["resume_file"] == "backend.txt"                       # resumes.map[backend]
    assert any(e["source"] == "resume" and e["value"] == "backend.txt" for e in app["fill_plan"])
    essay = by_label["Why do you want to work at Acme?*"]
    assert essay["source"] == "claude" and essay["claude_kind"] == "essay"
    yoe = by_label["How many years of Python experience do you have?*"]
    assert yoe["value"] == "3" and yoe["evidence"] == "3 years of Python experience"
    missing = {m["label"] for m in app["missing"]}
    assert missing == {"Are you willing to work onsite 5 days a week?*"}
    # optional cover letter / pronouns / EEO stay empty, and Claude never saw EEO or optional choices
    sent = json.loads(transport.calls[-1]["messages"][0]["content"].split("Blank fields:\n", 1)[1])
    labels = {f["label"] for f in sent}
    assert "Gender" not in labels and "Pronouns" not in labels
    assert "LinkedIn Profile" in labels                                # optional text may be answered
    assert Path(app["screenshot_path"]).exists()
    assert stats["needs_input"] == 1


def test_answer_bank_fills_before_claude(bctx, transport):
    bctx.answers.add("Are you willing to work onsite 5 days a week?", "yes_no", "Yes")
    seed(bctx, GH)
    asyncio.run(fill_many(bctx, [GH], RunStats()))
    app = bctx.ledger.application(GH)
    assert bctx.ledger.get(GH).state == "ready", app["missing"]
    entry = next(e for e in app["fill_plan"] if e["label"].startswith("Are you willing"))
    assert entry["source"] == "answer_bank" and entry["value"] == "Yes"


def test_wrong_simplify_visa_answer_is_corrected_and_the_run_goes_on(bctx):
    bctx.simplify_mock = simplify_mock(sponsorship="No")   # profile says sponsorship needed later
    bctx.profile.browser.parallel_tabs = 1
    bctx.answers.add("Are you willing to work onsite 5 days a week?", "yes_no", "Yes")
    sent = []
    bctx.notifier.warn = sent.append
    seed(bctx, GH)
    seed(bctx, ASHBY, company="Gamma", role="ml-ai")
    stats = RunStats()
    asyncio.run(fill_many(bctx, [GH, ASHBY], stats))
    assert "stopped" not in stats
    assert bctx.ledger.get(GH).state == "ready", bctx.ledger.application(GH)["missing"]
    assert bctx.ledger.get(ASHBY).state != "scored"                    # the next posting was filled too
    entry = next(e for e in bctx.ledger.application(GH)["fill_plan"] if "sponsorship" in e["label"])
    assert entry["source"] == "profile" and entry["value"] == "Yes"
    assert entry["note"] == "Simplify answered 'No'; your visa answers say Yes"
    assert stats["corrected"] == 1 and len(sent) == 1 and "Fix your Simplify profile" in sent[0]

    from autoapply.browser.replay import replay

    async def refill():                                   # approval later refills it the same way
        async with BrowserSession(bctx) as bs:
            page = await replay(bctx, bs, bctx.ledger.application(GH))
            await page.close()
    asyncio.run(refill())

    from fastapi.testclient import TestClient
    from autoapply.review.server import create_app
    with TestClient(create_app(bctx, port=8765), base_url="http://127.0.0.1:8765") as c:
        html = c.get("/lane/ready").text
    assert "Your visa answers" in html and "visa answer was corrected" in html


def test_simplify_gets_one_form_at_a_time(bctx):
    bctx.profile.browser.parallel_tabs = 3
    inner = simplify_mock()
    running, most = [0], [0]

    async def mock(page):                     # Simplify pauses in hidden tabs: never two at once
        running[0] += 1
        most[0] = max(most[0], running[0])
        await asyncio.sleep(0.4)
        await inner(page)
        running[0] -= 1
    bctx.simplify_mock = mock
    seed(bctx, GH)
    seed(bctx, ASHBY, company="Gamma", role="ml-ai")
    seed(bctx, LEVER, company="Beta", role="software")
    asyncio.run(fill_many(bctx, [GH, ASHBY, LEVER], RunStats()))
    assert most[0] == 1
    assert all(bctx.ledger.get(k).state in ("ready", "needs_input") for k in (GH, ASHBY, LEVER))


def test_ashby_buttons_radio_and_eeo(bctx):
    seed(bctx, ASHBY, company="Gamma", role="ml-ai")
    asyncio.run(fill_many(bctx, [ASHBY], RunStats()))
    app = bctx.ledger.application(ASHBY)
    plan = {e["label"]: e for e in app["fill_plan"]}
    assert plan["Are you authorized to work in the United States?"]["source"] == "simplify"
    assert plan["Tell us about a project you are proud of."]["source"] == "claude"
    assert {m["label"] for m in app["missing"]} == {"Are you open to relocating to New York?"}
    assert app["missing"][0]["options"] == ["Yes", "No"]


def test_lever_bank_select_and_required_checkbox(bctx):
    bctx.answers.add("Where did you first hear about this job?", "choice", "LinkedIn")
    seed(bctx, LEVER, company="Beta", role="software")
    asyncio.run(fill_many(bctx, [LEVER], RunStats()))
    app = bctx.ledger.application(LEVER)
    plan = {e["label"]: e for e in app["fill_plan"]}
    assert plan["Where did you first hear about this job?✱"]["source"] == "answer_bank"
    missing = {m["label"]: m for m in app["missing"]}
    assert set(missing) == {"Do you require a reasonable accommodation?✱", "I confirm the information is accurate✱"}
    assert missing["I confirm the information is accurate✱"]["answer_kind"] == "yes_no"


def test_parallel_tabs_one_company_at_a_time(bctx):
    seed(bctx, GH)
    seed(bctx, ASHBY, company="Gamma", role="ml-ai")
    seed(bctx, LEVER, company="Beta", role="software")
    stats = RunStats()
    asyncio.run(fill_many(bctx, [GH, ASHBY, LEVER], stats))
    assert {bctx.ledger.get(k).state for k in (GH, ASHBY, LEVER)} == {"needs_input"}
    assert stats["needs_input"] == 3


def _ready_greenhouse(bctx):
    bctx.answers.add("Are you willing to work onsite 5 days a week?", "yes_no", "Yes")
    seed(bctx, GH)
    asyncio.run(fill_many(bctx, [GH], RunStats()))
    assert bctx.ledger.get(GH).state == "ready"


def test_replay_and_test_mode_never_clicks_submit(bctx):
    _ready_greenhouse(bctx)
    bctx.test_mode = True                     # `autoapply run --test` / never_submit

    async def go():
        async with BrowserSession(bctx) as bs:
            result = await submit(bctx, bs, GH)          # no open tab -> replay
            page = bctx.open_tabs[GH]
            assert await page.evaluate("window.__submitted || 0") == 0
            assert await page.locator("#confirm").count() == 0
            with pytest.raises(SubmitBlocked):
                await click_submit(bctx, page, {})
            return result
    result = asyncio.run(go())
    assert result["state"] == "would_submit", result
    sub = Path(bctx.ledger.application(GH)["submission_dir"])
    assert (sub / "form.png").exists() and (sub / "fields.json").exists() and (sub / "meta.json").exists()
    assert "NOT submitted" in (sub / "meta.json").read_text()
    sent = json.loads((sub / "fields.json").read_text())
    assert {f["label"]: f["source"] for f in sent}["Why do you want to work at Acme?*"] == "claude"


def test_real_submit_from_replay_records_and_queues_writeback(bctx):
    _ready_greenhouse(bctx)
    bctx._secrets = {"GITHUB_TOKEN": "t"}
    bctx.__dict__.pop("secrets", None)
    bctx.__dict__.pop("writeback", None)

    async def go():
        async with BrowserSession(bctx) as bs:
            return await submit(bctx, bs, GH)
    result = asyncio.run(go())
    assert result["state"] == "submitted", result
    sub = Path(result["detail"])
    assert (sub / "after_submit.png").exists() and (sub / "backend.txt").exists()
    pend = bctx.ledger.pending_writebacks(bctx.profile.sources[0].label)
    assert pend[-1]["change"]["status"] == "applied" and pend[-1]["change"]["applied_via"] == "autoapply"


def test_replay_mismatch_goes_stuck(bctx):
    _ready_greenhouse(bctx)
    app = bctx.ledger.application(GH)
    plan = app["fill_plan"] + [{"key": "id:gone", "label": "A field that vanished", "kind": "text",
                                "value": "x", "source": "user"}]
    bctx.ledger.upsert_application(GH, fill_plan=plan)

    async def go():
        async with BrowserSession(bctx) as bs:
            return await submit(bctx, bs, GH)
    result = asyncio.run(go())
    assert result["state"] == "stuck" and "vanished" in result["detail"]
