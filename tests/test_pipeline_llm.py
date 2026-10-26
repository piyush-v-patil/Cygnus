"""Sync -> prefilter -> enrich -> blockers -> score, budget and Claude output checks (M1, M2)."""
import json

import pytest

from autoapply.ats.base import FetchError, JobDetails
from autoapply.config import PriceCfg
from autoapply.llm import BudgetExceeded, FakeTransport, LLMError
from autoapply.llm.answer import validate
from autoapply.llm.client import KNOWN_PRICES
from autoapply.pipeline import RunStats, enrich, fill_candidates, run, score, sync

DESC = {
    "greenhouse:acme:1001": "Build Python backend services. 2+ years of experience.",
    "lever:beta:11111111-2222-3333-4444-555555555555": "We are unable to sponsor visas for this role. Python.",
    "ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "ML work. Security clearance is a plus.",
    "greenhouse:airbnb:7826337": "Software engineer, Python.",
}


class FakeClient:
    def __init__(self, closed=(), fail=()):
        self.closed, self.fail, self.calls = set(closed), set(fail), []

    def fetch(self, ref):
        key = f"{ref.ats}:{ref.board_token}:{ref.job_id}"
        self.calls.append(key)
        if key in self.fail:
            raise FetchError("HTTP 503")
        if key in self.closed:
            return JobDetails(closed=True)
        return JobDetails(description_text=DESC.get(key, "Python role"), questions=[])


def clients(**kw):
    c = FakeClient(**kw)
    return {"greenhouse": c, "lever": c, "ashby": c}, c


def test_m1_sync_is_idempotent_and_dedupes(make_ctx):
    ctx = make_ctx()
    st = RunStats()
    sync(ctx, st)
    counts = ctx.ledger.counts()
    # 13 tracker rows: the simplify duplicate collapses into acme:1001
    assert sum(counts.values()) == 12
    led = ctx.ledger
    assert led.get("greenhouse:acme:1001").state == "discovered"
    assert sorted(r[1] for r in led.refs_for("greenhouse:acme:1001")) == ["acme:000000000001", "acme:000000000002"]
    assert led.get("greenhouse:airbnb:7826337").state == "discovered"      # gh_jid resolved
    assert led.get("greenhouse:zeta:2002").reason == "tier"
    assert led.get("greenhouse:eta:3003").reason == "role"
    assert led.get("greenhouse:theta:4004").reason == "location"
    assert led.get("greenhouse:nofilings:5005").reason == "no_h1b_record"
    assert led.get("greenhouse:iota:6006").reason == "monitor_applied"
    assert led.get("greenhouse:kappa:7007").reason == "monitor_closed"
    assert st["eligible"] == 4
    before = led.db.execute("SELECT posting_key, state, reason FROM postings ORDER BY 1").fetchall()
    sync(ctx, RunStats())
    after = led.db.execute("SELECT posting_key, state, reason FROM postings ORDER BY 1").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_m2_enrich_blockers_score_and_no_rescoring(make_ctx, transport):
    ctx = make_ctx(secrets={"GITHUB_TOKEN": "t"})
    st = RunStats()
    sync(ctx, st)
    cl, fake = clients(closed=["greenhouse:airbnb:7826337"])
    passed = enrich(ctx, st, None, cl)
    led = ctx.ledger
    assert led.get("greenhouse:airbnb:7826337").state == "closed"
    assert led.get("lever:beta:11111111-2222-3333-4444-555555555555").reason == "no_sponsorship"
    assert "greenhouse:acme:1001" in passed
    # ambiguous blocker text reaches Claude as a hint instead of skipping
    assert led.get("ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee").state == "enriched"
    scored = score(ctx, st)
    assert set(scored) == {"greenhouse:acme:1001", "ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    hint_call = [c for c in transport.calls if "Security clearance is a plus" in c["messages"][0]["content"]]
    assert hint_call and "keyword scan flagged" in hint_call[0]["messages"][0]["content"]
    # resume prompt is cached, schema-constrained, scored on the cheap model
    call = transport.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    n_calls = len(transport.calls)
    assert led.month_spend() > 0
    # skipped writes are queued for the monitor with only a reason code
    pend = led.pending_writebacks(ctx.profile.sources[0].label)
    assert [p["change"] for p in pend] == [{"status": "skip", "skip_reason": "no_sponsorship", "skipped_by": "autoapply"}]
    # nothing is scored twice: put a scored posting back and score again
    led.db.execute("UPDATE postings SET state='enriched' WHERE posting_key='greenhouse:acme:1001'")
    score(ctx, RunStats())
    assert len(transport.calls) == n_calls
    assert led.get("greenhouse:acme:1001").state == "scored"


def test_low_fit_and_claude_blockers(make_ctx, transport):
    def responder(params):
        content = params["messages"][0]["content"]
        if "Gamma" in content:
            return {"score": 90, "role_family": "ml-ai", "reasons": ["x"],
                    "blockers": [{"type": "us_person_required", "evidence": "ITAR"}], "yoe_required": None}
        return {"score": 40, "role_family": "backend", "reasons": ["weak"], "blockers": [], "yoe_required": None}
    transport.responder = responder
    ctx = make_ctx()
    st = RunStats()
    sync(ctx, st)
    enrich(ctx, st, None, clients()[0])
    assert score(ctx, st) == []
    # fit.low_fit: hold (default) parks it instead of skipping it
    assert ctx.ledger.get("greenhouse:acme:1001").state == "held"
    assert ctx.ledger.get("ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee").reason == "us_person_required"
    assert st["held"] >= 1


def test_budget_stops_new_calls(make_ctx, transport):
    ctx = make_ctx()
    ctx.ledger.record_usage("score", "claude-haiku-5-5", {}, 5.0, None)     # budget is $5
    st = RunStats()
    sync(ctx, st)
    enrich(ctx, st, None, clients()[0])
    score(ctx, st)
    assert "stopped" in st and transport.calls == []
    assert ctx.ledger.get("greenhouse:acme:1001").state == "enriched"      # picked up next month


def test_fetch_errors_retry_then_skip(make_ctx):
    ctx = make_ctx()
    sync(ctx, RunStats())
    cl, _ = clients(fail=["greenhouse:acme:1001"])
    for _ in range(2):
        enrich(ctx, RunStats(), None, cl)
        assert ctx.ledger.get("greenhouse:acme:1001").state == "error"
    enrich(ctx, RunStats(), None, cl)
    row = ctx.ledger.get("greenhouse:acme:1001")            # no form yet: nothing for you to review
    assert row.state == "skipped" and row.reason == "failed_enrich"


def test_run_no_fill_end_to_end(make_ctx, monkeypatch):
    import autoapply.ats as ats_pkg
    cl, _ = clients()
    monkeypatch.setattr(ats_pkg, "clients", lambda http=None: cl)
    ctx = make_ctx()
    counts = run(ctx, no_fill=True)
    assert counts["scored"] == 3 and ctx.ledger.last_run()["ended_at"]
    assert fill_candidates(ctx, None)[0] == "greenhouse:acme:1001"          # newest first
    from autoapply.pipeline import status_report
    rep = status_report(ctx)                       # the last run's numbers, spelled out
    assert "    3  scored by Claude" in rep and "added to the review queue" in rep


def test_run_breakdown_lists_skips_and_stop_reason():
    from autoapply.pipeline import run_breakdown
    lines = run_breakdown({"enriched": 50, "held": 30, "skipped": 12, "ready": 1, "needs_input": 2,
                           "skipped_by_reason": {"no_sponsorship": 10, "yoe": 2}, "stopped": "budget reached"})
    text = "\n".join(lines)
    assert "   50  looked at this run" in text and "   30  held: fit score below your cutoff" in text
    assert "   12  skipped: no_sponsorship 10, yoe 2" in text
    assert "-> 3 application(s) added to the review queue" in text
    assert "Stopped early: budget reached" in text
    assert run_breakdown({}) == ["  (no numbers recorded for it)"]


def test_malformed_json_retried_once_then_error(make_ctx):
    outputs = ["not json", "{also bad"]
    t = FakeTransport(lambda p: outputs.pop(0) if outputs else {"score": 70, "role_family": "backend",
                                                                    "reasons": [], "blockers": [], "yoe_required": None})
    ctx = make_ctx()
    ctx._llm_transport = t
    with pytest.raises(LLMError):
        ctx.llm.call("score", "claude-haiku-5-5", [{"type": "text", "text": "s"}], "u", schema={"type": "object"})
    assert len(t.calls) == 2
    res = ctx.llm.call("score", "claude-haiku-5-5", [{"type": "text", "text": "s"}], "u", schema={"type": "object"})
    assert res.data["score"] == 70


def test_cost_uses_profile_pricing_then_known_table(make_ctx):
    ctx = make_ctx()
    llm = ctx.llm
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
    inp, out = KNOWN_PRICES["claude-haiku-5-5"]
    assert llm.cost("claude-haiku-5-5", u) == pytest.approx(inp + out + inp * 0.1)
    llm.cfg.pricing["claude-haiku-5-5"] = PriceCfg(input=1, output=2)
    assert llm.cost("claude-haiku-5-5", {"input_tokens": 1_000_000}) == pytest.approx(1)
    with pytest.raises(BudgetExceeded):
        ctx.ledger.record_usage("x", "m", {}, 99, None)
        llm.call("score", "claude-haiku-5-5", [], "u")



def test_dated_and_unknown_models_are_never_free(make_ctx, caplog):
    llm = make_ctx().llm
    one = {"input_tokens": 1_000_000}
    assert llm.cost("claude-sonnet-5-5-20260915", one) == pytest.approx(KNOWN_PRICES["claude-sonnet-5-5"][0])
    assert llm.has_price("claude-sonnet-5-5-20260915") and not llm.has_price("claude-mystery-9")
    top = max(KNOWN_PRICES.values())[0]
    with caplog.at_level("WARNING"):
        assert llm.cost("claude-mystery-9", one) == pytest.approx(top)
        llm.cost("claude-mystery-9", one)
    assert sum("claude-mystery-9" in r.getMessage() for r in caplog.records) == 1

RES = "Software Engineer, Acme Corp (2023 - present)\n- 3 years of Python experience building backend APIs"


@pytest.mark.parametrize("verdict,field,kind,answer", [
    ({"kind": "factual_from_resume", "answer": "3", "evidence": "3 years of Python experience"},
     {"type": "text"}, "factual_from_resume", "3"),
    ({"kind": "factual_from_resume", "answer": "5", "evidence": "5 years of Python"},
     {"type": "text"}, "needs_user", None),                                   # evidence not in resume
    ({"kind": "essay", "answer": "Yes", "evidence": None},
     {"type": "select", "options": ["Yes", "No"]}, "needs_user", None),       # a choice is a fact
    ({"kind": "factual_from_resume", "answer": "maybe", "evidence": "3 years of Python experience"},
     {"type": "select", "options": ["Yes", "No"]}, "needs_user", None),       # not an option
    ({"kind": "essay", "answer": "word " * 100, "evidence": None},
     {"type": "text", "max_length": 50}, "essay", None),
    ({"kind": "essay", "answer": "I agree", "evidence": None},
     {"type": "checkbox"}, "needs_user", None),
])
def test_answer_validation(verdict, field, kind, answer):
    v = validate({"facts_used": [], "needs_user_reason": None, **verdict}, field, RES)
    assert v["kind"] == kind
    if answer is not None:
        assert v["answer"] == answer
    if field.get("max_length") and kind == "essay":
        assert len(v["answer"]) <= 50


def test_score_prompt_contains_calibration_and_profile(make_ctx, transport):
    ctx = make_ctx()
    ctx.paths.calibration.write_text(json.dumps({"examples": [
        {"title": "Backend Eng", "company": "X", "label": "yes", "reason": "python"},
        {"title": "Staff Eng", "company": "Y", "label": "no", "reason": "too senior"}]}))
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), 1, clients()[0])
    score(ctx, RunStats(), 1)
    system = transport.calls[0]["system"][0]["text"]
    assert "WOULD apply: Backend Eng at X" in system and "would NOT apply: Staff Eng at Y" in system
    assert "Needs sponsorship (now or later): yes" in system


def _low_scores(transport, score=40):
    transport.responder = lambda p: {"score": score, "role_family": "backend", "reasons": ["weak"],
                                     "blockers": [], "yoe_required": None}


def test_held_postings_unheld_for_free_when_cutoff_drops(make_ctx, transport):
    _low_scores(transport)
    ctx = make_ctx(secrets={"GITHUB_TOKEN": "t"})
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), None, clients()[0])
    score(ctx, RunStats())
    led = ctx.ledger
    assert led.get("greenhouse:acme:1001").state == "held"
    assert led.pending_writebacks(ctx.profile.sources[0].label) == [] or all(
        p["change"].get("skip_reason") != "low_fit" for p in led.pending_writebacks(ctx.profile.sources[0].label))
    calls = len(transport.calls)
    ctx.profile.fit.cutoff = 30
    st = RunStats()
    assert "greenhouse:acme:1001" in score(ctx, st)
    assert len(transport.calls) == calls and st["unheld"] >= 1          # no new Claude calls
    assert "greenhouse:acme:1001" in fill_candidates(ctx, None)


def test_low_fit_skip_policy_still_skips(make_ctx, transport):
    from .conftest import base_profile
    _low_scores(transport)
    ctx = make_ctx(base_profile(fit={"cutoff": 65, "low_fit": "skip"}), secrets={"GITHUB_TOKEN": "t"})
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), None, clients()[0])
    score(ctx, RunStats())
    assert ctx.ledger.get("greenhouse:acme:1001").reason == "low_fit"
    changes = [p["change"] for p in ctx.ledger.pending_writebacks(ctx.profile.sources[0].label)]
    assert {"status": "skip", "skip_reason": "low_fit", "skipped_by": "autoapply"} in changes


def test_earlier_low_fit_skips_are_released_and_unmarked(make_ctx, transport):
    from autoapply.pipeline import release_low_fit
    from autoapply.writeback import apply_changes
    from .conftest import base_profile
    _low_scores(transport)
    ctx = make_ctx(base_profile(fit={"cutoff": 65, "low_fit": "skip"}), secrets={"GITHUB_TOKEN": "t"})
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), None, clients()[0])
    score(ctx, RunStats())
    ctx.profile.fit.low_fit = "hold"
    st = RunStats()
    release_low_fit(ctx, st)
    assert ctx.ledger.get("greenhouse:acme:1001").state == "held" and st["released"] >= 1
    undo = ctx.ledger.pending_writebacks(ctx.profile.sources[0].label)[-1]["change"]
    doc = {"jobs": {"a": {"status": "skip", "skip_reason": "low_fit", "skipped_by": "autoapply", "title": "T"},
                    "b": {"status": "applied", "applied_on": "2026-10-20"}}}
    apply_changes(doc, {"a": undo, "b": undo})
    assert doc["jobs"]["a"] == {"status": "new", "title": "T"}               # our skip undone
    assert doc["jobs"]["b"] == {"status": "applied", "applied_on": "2026-10-20"}  # the user's mark kept


def test_narrowed_roles_apply_to_already_scored_postings(make_ctx, transport):
    ctx = make_ctx()
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), None, clients()[0])
    score(ctx, RunStats())
    assert "greenhouse:acme:1001" in fill_candidates(ctx, None)             # role: backend
    ctx.profile.filters.roles = ["software"]
    assert "greenhouse:acme:1001" not in fill_candidates(ctx, None)
    ctx.profile.filters.roles = ["software", "backend"]
    assert "greenhouse:acme:1001" in fill_candidates(ctx, None)             # nothing was lost



def test_narrowed_location_applies_to_already_scored_postings(make_ctx, transport):
    ctx = make_ctx()
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), None, clients()[0])
    score(ctx, RunStats())
    assert "greenhouse:acme:1001" in fill_candidates(ctx, None)
    ctx.profile.filters.location, ctx.profile.filters.metros = "metros", ["Nowhere City"]
    assert "greenhouse:acme:1001" not in fill_candidates(ctx, None)


def test_unreadable_h1b_list_holds_new_postings_back(make_ctx, monkeypatch):
    from autoapply.notify import Notifier
    from autoapply.sources import SourceError
    ctx = make_ctx()
    sync(ctx, RunStats())
    led = ctx.ledger
    assert led.get("greenhouse:nofilings:5005").reason == "no_h1b_record"
    warned = []
    monkeypatch.setattr(Notifier, "warn", lambda self, m: warned.append(m))
    src = ctx.sources[0]
    monkeypatch.setattr(src, "h1b", lambda: (_ for _ in ()).throw(SourceError("h1b.json could not be read")))
    st = RunStats()
    sync(ctx, st)
    assert led.get("greenhouse:nofilings:5005").reason == "no_h1b_record"     # not let back in
    assert warned and st["source_skipped"] == 1


PDF_RESUME = ("EXPERIENCE\nSoftware Engineer, Acme\n- 3 years of experi-\nence with Python and Go\n"
              "- Proﬁcient in AWS and Kubernetes\n- Led a team of 4 – payments migration")


@pytest.mark.parametrize("quote,ok", [
    ("3 years of experience with Python and Go", True),       # line-break hyphenation
    ("Proficient in AWS and Kubernetes", True),               # 'fi' ligature in the PDF text
    ("Led a team of 4 - payments migration", True),           # en dash vs hyphen
    ("“3 years of experience with Python”", True),            # quotes around the quote
    ("5 years of experience with Python", False),             # a different fact
    ("Proficient in AWS and Terraform", False),
    ("AWS", False),                                           # too short to prove anything
])
def test_evidence_survives_pdf_text_artefacts(quote, ok):
    from autoapply.llm.answer import evidence_in_resume
    assert evidence_in_resume(quote, PDF_RESUME) is ok


@pytest.mark.parametrize("label,kind,open_", [
    ("Why do you want to work at Snowflake?*", "textarea", True),
    ("What interests you about this role?", "textarea", True),
    ("Tell us about a project you are proud of.", "textarea", True),
    ("Briefly describe your experience with distributed systems", "textarea", True),
    ("Anything else you'd like us to know?", "textarea", True),
    ("Why Asana?", "text", True),
    ("Explain why you will require visa sponsorship", "textarea", False),     # legal: never an essay
    ("Describe your salary expectations", "text", False),
    ("Are you willing to relocate? Explain.", "textarea", False),
    ("How did you hear about us? Describe.", "text", False),
    ("Why do you want to work here?", "select", False),                        # a choice is a fact
    ("LinkedIn Profile", "text", False),
])
def test_open_ended_detection(label, kind, open_):
    from autoapply.llm.answer import is_open_ended
    assert is_open_ended({"label": label, "type": kind, "options": ["a"] if kind == "select" else []}) is open_


def test_answers_use_short_ids_and_retry_open_ended(make_ctx):
    from autoapply.llm.answer import answer_fields
    calls = []

    def responder(params):
        content = params["messages"][0]["content"]
        listing = json.loads(content.split("Blank fields:\n", 1)[1].split("\n\nThese are open-ended", 1)[0])
        calls.append((listing, "Do not answer needs_user" in content))
        out = []
        for f in listing:
            if "Why" in f["label"] and not calls[-1][1]:
                # an over-cautious first answer: left for the user
                out.append({"id": f["id"], "kind": "needs_user", "answer": None, "evidence": None,
                            "facts_used": [], "needs_user_reason": "personal motivation"})
            elif "Why" in f["label"]:
                out.append({"id": f["id"], "kind": "essay", "answer": "Snowflake's data platform matches the "
                            "Python services I built at Acme.", "evidence": None,
                            "facts_used": ["Python services at Acme"], "needs_user_reason": None})
            else:
                out.append({"id": f["id"], "kind": "needs_user", "answer": None, "evidence": None,
                            "facts_used": [], "needs_user_reason": "not in resume"})
        return {"fields": out}
    ctx = make_ctx()
    ctx._llm_transport = FakeTransport(responder)
    fields = [{"id": "id:question_32968103003", "label": "Why do you want to work at Snowflake?*",
               "type": "textarea", "required": True},
              {"id": "name:cards[0e0cf60a-07b7][field0]", "label": "Expected start date", "type": "text",
               "required": True}]
    out = answer_fields(ctx.llm, ctx.profile, "Python services at Acme", {"company": "Snowflake"}, "desc", fields)
    assert [f["id"] for f in calls[0][0]] == ["f1", "f2"] and calls[0][0][0]["open_ended"] is True
    assert "open_ended" not in calls[0][0][1]
    assert len(calls) == 2 and calls[1][1] and [f["id"] for f in calls[1][0]] == ["f1"]   # only the essay retried
    essay = out["id:question_32968103003"]
    assert essay["kind"] == "essay" and "Snowflake" in essay["answer"]
    assert out["name:cards[0e0cf60a-07b7][field0]"]["kind"] == "needs_user"


def test_postings_already_scored_at_a_us_person_company_are_not_filled(make_ctx):
    from autoapply.ledger import ENRICHED, SCORED
    ctx = make_ctx()
    led = ctx.ledger
    for key, company in (("greenhouse:anduril:1", "Anduril Industries"), ("greenhouse:acme:2", "Acme")):
        led.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key,
                                    "company": company, "title": "SWE", "role": "software", "posted_at": "2026-10-20"})
        led.set_state(key, ENRICHED)
        led.save_score(key, {"score": 80, "role_family": "software", "reasons": [], "blockers": []}, "m", "v", "h")
        led.set_state(key, SCORED)
    assert fill_candidates(ctx, None) == ["greenhouse:acme:2"]


def test_postings_left_mid_fill_are_queued_again_by_the_next_run(make_ctx, monkeypatch):
    import autoapply.ats as ats_pkg
    from autoapply.ledger import ENRICHED, FILLING, SCORED
    cl, _ = clients()
    monkeypatch.setattr(ats_pkg, "clients", lambda http=None: cl)
    ctx = make_ctx()
    key = "greenhouse:acme:9"
    ctx.ledger.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key,
                                       "company": "Acme", "title": "SWE", "role": "software"})
    ctx.ledger.set_state(key, ENRICHED)
    ctx.ledger.save_score(key, {"score": 80, "role_family": "software", "reasons": [], "blockers": []}, "m", "v", "h")
    ctx.ledger.set_state(key, SCORED)
    ctx.ledger.set_state(key, FILLING)               # the PC was closed while this form was open
    counts = run(ctx, no_fill=True)
    assert counts["interrupted"] == 1 and ctx.ledger.get(key).state == SCORED
    assert key in fill_candidates(ctx, None)


def _stuck_posting(ctx, key, reason):
    from autoapply.ledger import ENRICHED, FILLING, STUCK
    led = ctx.ledger
    led.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key,
                                "company": key, "title": "SWE", "role": "software"})
    for st in (ENRICHED, SCORED_STATE, FILLING):
        led.set_state(key, st)
        if st == ENRICHED:
            led.save_score(key, {"score": 80, "role_family": "software", "reasons": [], "blockers": []}, "m", "v", "h")
    led.upsert_application(key, lane=STUCK, stuck_reason=reason)
    led.set_state(key, STUCK, reason)


SCORED_STATE = "scored"


def test_temporary_stuck_reasons_are_filled_again_at_most_twice(make_ctx):
    from autoapply.pipeline import retry_stuck
    ctx = make_ctx()
    _stuck_posting(ctx, "greenhouse:a:1", "timed_out: no result after 420s while Simplify autofill")
    _stuck_posting(ctx, "greenhouse:b:2", "captcha: the page asks for a CAPTCHA")
    _stuck_posting(ctx, "greenhouse:c:3", "email_code: Enter the 8-character code")
    _stuck_posting(ctx, "greenhouse:d:4", "submit_unconfirmed: Submit was clicked")
    stats = RunStats()
    assert retry_stuck(ctx, stats) == ["greenhouse:a:1"] and stats["retried"] == 1
    assert ctx.ledger.get("greenhouse:a:1").state == "scored"
    assert {ctx.ledger.get(k).state for k in ("greenhouse:b:2", "greenhouse:c:3", "greenhouse:d:4")} == {"stuck"}
    for _ in range(2):                          # it keeps timing out
        from autoapply.ledger import FILLING, STUCK
        ctx.ledger.set_state("greenhouse:a:1", FILLING)
        ctx.ledger.upsert_application("greenhouse:a:1", lane=STUCK, stuck_reason="timed_out: again")
        ctx.ledger.set_state("greenhouse:a:1", STUCK, "timed_out: again")
        retry_stuck(ctx, RunStats())
    assert ctx.ledger.get("greenhouse:a:1").state == "stuck"            # two automatic tries, then it waits


@pytest.mark.parametrize("label,ftype,answer,ok", [
    ("Why do you want to work at Acme?", "textarea", "I built Python services at Acme.", True),
    ("Is there anything else you'd like to share?", "textarea", "I enjoy distributed systems.", True),
    ("Are you legally authorized to work in the US?", "text", "Yes", False),
    ("Explain why you will need visa sponsorship", "textarea", "Because I am on OPT.", False),
    ("How many years of Python experience do you have?", "text", "5 years", False),
    ("Desired salary", "text", "Competitive and negotiable.", False),
    ("What is your GPA?", "text", "3.9", False),
    ("Tell us about a project you are proud of", "textarea", "Yes", False),          # a bare fact as prose
])
def test_claude_essays_only_for_open_questions(label, ftype, answer, ok):
    from autoapply.llm.answer import validate
    v = validate({"kind": "essay", "answer": answer, "evidence": None, "facts_used": []},
                 {"id": "f1", "label": label, "type": ftype, "options": []}, RES)
    assert (v["kind"] == "essay") is ok, v


def test_tracker_changes_after_discovery_stop_waiting_postings(make_ctx, tmp_path):
    import json as _json
    from autoapply.ledger import ENRICHED
    from .conftest import FIXTURES, base_profile
    jobs = _json.loads((FIXTURES / "jobs.json").read_text())
    path = tmp_path / "jobs.json"
    path.write_text(_json.dumps(jobs))
    prof = base_profile()
    prof["sources"][0]["path"] = str(path)
    ctx = make_ctx(prof)
    sync(ctx, RunStats())
    led = ctx.ledger
    key = "greenhouse:acme:1001"
    led.set_state(key, ENRICHED)
    led.set_state(key, SCORED_STATE)
    other = next(r.posting_key for r in led.postings_in(["discovered"]) if r.posting_key != key)
    led.set_state(other, ENRICHED)
    led.set_state(other, SCORED_STATE)
    ref_other = led.refs_for(other)[0][1]
    jobs["jobs"]["acme:000000000001"]["status"] = "applied"               # you applied by hand
    jobs["jobs"]["acme:000000000002"]["status"] = "applied"
    jobs["jobs"][ref_other].update(status="skip", skipped_by="autoapply")  # this tool's own mark
    path.write_text(_json.dumps(jobs))
    ctx2 = make_ctx(prof)
    st = RunStats()
    sync(ctx2, st)
    assert ctx2.ledger.get(key).state == "closed" and ctx2.ledger.get(key).reason == "monitor_applied"
    assert ctx2.ledger.get(other).state == "scored" and st["tracker_closed"] == 1


def test_claude_out_of_credit_stops_the_run_without_blaming_postings(make_ctx, monkeypatch):
    import autoapply.ats as ats_pkg
    from autoapply.llm import FakeTransport

    class BadRequestError(Exception):
        pass

    def responder(params):
        raise BadRequestError("Your credit balance is too low to access the Anthropic API.")
    cl, _ = clients()
    monkeypatch.setattr(ats_pkg, "clients", lambda http=None: cl)
    ctx = make_ctx()
    ctx._llm_transport = FakeTransport(responder)
    counts = run(ctx, no_fill=True)
    assert "unavailable" in counts["stopped"].lower()
    assert ctx.ledger.counts().get("error", 0) == 0 and ctx.ledger.counts().get("stuck", 0) == 0


def test_repeated_failures_before_a_form_skip_the_posting_instead_of_crashing(make_ctx):
    from autoapply.ledger import ENRICHED, HELD
    from autoapply.pipeline import fail
    ctx = make_ctx()
    led = ctx.ledger
    key = "greenhouse:z:1"
    led.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key, "company": "Z"})
    st = RunStats()
    led.set_state(key, ENRICHED)
    for _ in range(3):                                   # three scoring failures in a row
        fail(ctx, key, "score", "boom", st)
    row = led.get(key)
    assert row.state == "skipped" and row.reason == "failed_score"
    key2 = "greenhouse:z:2"                              # progress resets the count
    led.upsert_discovered(key2, {"source_name": ctx.profile.sources[0].label, "source_ref": key2, "company": "Z"})
    fail(ctx, key2, "enrich", "x", st)
    fail(ctx, key2, "enrich", "x", st)
    led.set_state(key2, ENRICHED)
    led.set_state(key2, HELD)
    fail(ctx, key2, "score", "x", st)
    assert led.get(key2).state == "error"


def test_requeue_of_a_never_scored_posting_starts_it_over(make_ctx):
    from autoapply.ledger import ENRICHED, FILLING, STUCK
    ctx = make_ctx()
    led = ctx.ledger
    key = "greenhouse:y:1"
    led.upsert_discovered(key, {"source_name": ctx.profile.sources[0].label, "source_ref": key, "company": "Y"})
    led.set_state(key, ENRICHED)
    led.set_state(key, SCORED_STATE)
    led.set_state(key, FILLING)
    led.upsert_application(key, lane=STUCK, stuck_reason="score: boom")
    led.set_state(key, STUCK, "score: boom")              # an old-style Stuck from before scoring
    assert led.requeue_keys([key]) == [key] and led.get(key).state == "discovered"
    assert key not in fill_candidates(ctx, None)


def test_a_runs_cost_counts_only_its_own_claude_calls(make_ctx):
    from autoapply.pipeline import cost_line, status_report
    ctx = make_ctx()
    led = ctx.ledger
    led.db.execute("INSERT INTO llm_usage VALUES ('2026-10-01T08:00:00Z','score','m',0,0,0,0,5.0,NULL)")  # an older run
    led.db.execute("INSERT INTO runs (started_at, ended_at, kind, counts_json) VALUES "
                   "('2026-10-25T08:00:00Z','2026-10-25T08:30:00Z','run','{}')")
    for ts, purpose, cost in (("2026-10-25T08:05:00Z", "score", 0.02), ("2026-10-25T08:06:00Z", "score", 0.03),
                              ("2026-10-25T08:10:00Z", "answer", 0.25), ("2026-10-25T09:00:00Z", "answer", 1.0)):
        led.db.execute("INSERT INTO llm_usage VALUES (?,?,'m',0,0,0,0,?,NULL)", (ts, purpose, cost))
    spend = led.run_spend(led.last_run())
    assert spend["total"] == pytest.approx(0.30) and spend["calls"] == 3
    line = cost_line(spend)
    assert line == "Claude cost this run: $0.30 (form answers $0.25, 1 call · scoring $0.05, 2 calls)"
    assert line in status_report(ctx)
    assert cost_line(led.spend_between("2030-01-01T00:00:00Z")) == "Claude cost this run: $0.00"


def test_run_reports_its_cost_to_discord(make_ctx, transport, monkeypatch):
    import autoapply.pipeline as pl
    ctx = make_ctx()
    cl, _ = clients()
    monkeypatch.setattr(pl.ats, "clients", lambda http=None: cl)
    sent = []
    monkeypatch.setattr(ctx.notifier.discord, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(ctx.notifier.discord, "enabled", True)
    run(ctx, no_fill=True)
    cost = ctx.ledger.run_spend(ctx.ledger.last_run())["total"]
    assert cost > 0 and any(f"This run cost ${cost:.2f}" in m for m in sent)


def test_cli_run_prints_the_cost(profile_dir, monkeypatch, capsys):
    import autoapply.health as health
    import autoapply.pipeline as pl
    from autoapply.cli import main
    from autoapply.health import Report
    monkeypatch.setattr(health, "run_health", lambda ctx, **kw: Report())
    monkeypatch.setattr(pl, "run", lambda ctx, **kw: (ctx.ledger.end_run(ctx.ledger.start_run("run"), {}), {})[1])
    assert main(["--profile-dir", str(profile_dir.root), "run", "--no-fill"]) == 0
    assert "Claude cost this run: $0.00" in capsys.readouterr().out


@pytest.mark.parametrize("company,wanted,hit", [
    ("Capital One Financial", "capital one", True), ("CapitalOne", "capital one", True),
    ("Capital One", "capitalone", True), ("Capital One, Inc.", "Capital One", True),
    ("Capital One", "one", False), ("Acme Robotics", "acme", True), ("Acmeco", "acme", False),
])
def test_company_names_match_as_typed(company, wanted, hit):
    from autoapply.companies import matches
    assert matches(company, wanted) is hit


def _scored(make_ctx):
    ctx = make_ctx()
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), None, clients()[0])
    score(ctx, RunStats())
    return ctx


def test_a_run_can_take_only_some_companies_or_leave_some_out(make_ctx, transport):
    ctx = _scored(make_ctx)
    ctx.run_companies = (["acme", "Gamma"], [])
    assert set(fill_candidates(ctx, None)) == {"greenhouse:acme:1001", "ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    ctx.run_companies = ([], ["airbnb"])
    assert "greenhouse:airbnb:7826337" not in fill_candidates(ctx, None)
    ctx.run_companies = None
    assert len(fill_candidates(ctx, None)) == 3


def test_only_the_chosen_companies_are_fetched_and_scored(make_ctx, transport):
    ctx = make_ctx()
    sync(ctx, RunStats())
    ctx.run_companies = (["acme"], [])
    done = enrich(ctx, RunStats(), None, clients()[0])
    assert done and all(ctx.ledger.get(k).get("company") == "Acme" for k in done)
    assert ctx.ledger.get("ashby:gamma:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee").state == "discovered"   # untouched


def test_a_paused_company_is_scored_but_filled_only_after_the_pause(make_ctx, transport):
    from datetime import date, timedelta
    from autoapply.companies import PAUSED_KEY, pause, resume
    from autoapply.pipeline import status_report
    ctx = _scored(make_ctx)
    pause(ctx, "Airbnb")
    st = RunStats()
    keys = fill_candidates(ctx, None, st)
    assert "greenhouse:airbnb:7826337" not in keys and st["company_paused"] == 1
    assert ctx.ledger.get("greenhouse:airbnb:7826337").state == "scored"            # waits, nothing lost
    assert "Airbnb paused until" in status_report(ctx)
    old = ctx.state_get(PAUSED_KEY)                                                   # 30 days later
    for v in old.values():
        v["until"] = (date.today() - timedelta(days=1)).isoformat()
    ctx.state_set(PAUSED_KEY, old)
    assert "greenhouse:airbnb:7826337" in fill_candidates(ctx, None)
    pause(ctx, "airbnb")
    assert resume(ctx, "AIRBNB") and "greenhouse:airbnb:7826337" in fill_candidates(ctx, None)


def test_companies_command(make_ctx, transport, capsys):
    from autoapply.companies_cmd import companies_command
    ctx = _scored(make_ctx)
    assert companies_command(ctx, "pause", "airbnb", 30) == 0
    out = capsys.readouterr().out
    assert "Airbnb (1 waiting)" in out and "paused until" in out
    companies_command(ctx, "list", None, 30)
    out = capsys.readouterr().out
    assert "airbnb paused until" in out and "Acme" in out
    companies_command(ctx, "pause", "nosuchco", 30)
    assert "No posting from a company named 'nosuchco'" in capsys.readouterr().out
    companies_command(ctx, "resume", "airbnb", 30)
    assert "pause ended" in capsys.readouterr().out
