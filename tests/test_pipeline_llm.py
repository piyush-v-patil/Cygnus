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


def test_fetch_errors_retry_then_stuck(make_ctx):
    ctx = make_ctx()
    sync(ctx, RunStats())
    cl, _ = clients(fail=["greenhouse:acme:1001"])
    for _ in range(2):
        enrich(ctx, RunStats(), None, cl)
        assert ctx.ledger.get("greenhouse:acme:1001").state == "error"
    enrich(ctx, RunStats(), None, cl)
    assert ctx.ledger.get("greenhouse:acme:1001").state == "stuck"
    assert "failed 3 times" in ctx.ledger.application("greenhouse:acme:1001")["stuck_reason"]


def test_run_no_fill_end_to_end(make_ctx, monkeypatch):
    import autoapply.ats as ats_pkg
    cl, _ = clients()
    monkeypatch.setattr(ats_pkg, "clients", lambda http=None: cl)
    ctx = make_ctx()
    counts = run(ctx, no_fill=True)
    assert counts["scored"] == 3 and ctx.ledger.last_run()["ended_at"]
    assert fill_candidates(ctx, None)[0] == "greenhouse:acme:1001"          # newest first


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
