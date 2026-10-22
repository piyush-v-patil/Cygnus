"""The run: sync sources -> prefilter -> enrich -> blockers -> score -> fill -> queue (§13).

Each stage only advances postings through the ledger, so a run that stops
anywhere (budget, crash, PC asleep) resumes from where it was next time.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import ats, blockers
from .ats.detect import parse_key
from .context import Context
from .filters import prefilter
from .h1b import H1BIndex
from .ledger import (CLOSED, DISCOVERED, ENRICHED, ERROR, HELD, MAX_ERROR_RETRIES, RECHECKABLE, SCORED,
                     SKIPPED, STUCK)
from .llm import BudgetExceeded, LLMError
from .llm.score import PROMPT_VERSION as SCORE_VERSION
from .llm.score import score_posting
from .writeback import skip_change, unskip_change

log = logging.getLogger(__name__)
DETAILS_MAX_AGE = timedelta(days=7)
WRITEBACK_SKIPS = {"low_fit", "us_person_required", "no_sponsorship", "yoe", "blocker_other"}


class RunStats(Counter):
    def skipped(self, reason: str):
        self["skipped"] += 1
        self[f"skip:{reason}"] += 1

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.items() if not k.startswith("skip:")}
        d["skipped_by_reason"] = {k[5:]: v for k, v in self.items() if k.startswith("skip:")}
        return d


# ------------------------------------------------------------------- sync --
def sync(ctx: Context, stats: RunStats) -> None:
    """Read every source, record postings, run the free rules (§6.4)."""
    led, profile = ctx.ledger, ctx.profile
    eligible: set[str] = set()
    for src in ctx.sources:
        postings = list(src.postings())
        h1b_data = src.h1b()
        tokens = src.gh_tokens()
        h1b = H1BIndex(h1b_data) if h1b_data is not None else None
        with led.tx():
            for p in postings:
                ref = ats.detect(p.apply_url, p.company, tokens)
                key = ats.posting_key(ref, p.apply_url)
                fields = p.as_fields()
                fields.update(ats=ref.ats, board_token=ref.board_token, ats_job_id=ref.job_id)
                if led.upsert_discovered(key, fields):
                    stats["new"] += 1
                row = led.get(key)
                recheck = (row.state, row.reason if row.state == SKIPPED else None) in RECHECKABLE
                if row.state != DISCOVERED and not recheck:
                    continue      # §6.4.1: already terminal or in flight
                verdict = prefilter(p, ref, profile, h1b)
                if verdict is None:
                    if row.state != DISCOVERED:
                        led.set_state(key, DISCOVERED, None, "re-qualified")
                    eligible.add(key)
                    continue
                state, reason = verdict
                if row.state == state and row.reason == reason:
                    continue
                if row.state != DISCOVERED:
                    led.set_state(key, DISCOVERED, None, "re-check")
                led.set_state(key, state, reason)
                if state == SKIPPED:
                    stats.skipped(reason)
                else:
                    stats[state] += 1
    stats["eligible"] = len(eligible)


# ----------------------------------------------------------------- enrich --
def _details_fresh(d: dict | None) -> bool:
    if not d or not d.get("fetched_at"):
        return False
    t = datetime.strptime(d["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - t < DETAILS_MAX_AGE


def _retry_stage(row) -> str | None:
    """For a posting in `error`, the stage to re-run (encoded in its reason)."""
    return (row.reason or "").split(":", 1)[0] or None


def fail(ctx: Context, key: str, stage: str, err: Exception | str, stats: RunStats):
    row = ctx.ledger.get(key)
    if row.get("error_count", 0) + 1 >= MAX_ERROR_RETRIES:
        ctx.ledger.set_state(key, STUCK, f"{stage}: {err}"[:300])
        ctx.ledger.upsert_application(key, lane=STUCK, stuck_reason=f"failed {MAX_ERROR_RETRIES} times "
                                      f"at {stage}: {err}"[:500])
        stats["stuck"] += 1
        return
    ctx.ledger.set_state(key, ERROR, f"{stage}: {err}"[:300])
    stats["error"] += 1


def enrich(ctx: Context, stats: RunStats, limit: int | None, clients: dict | None = None) -> list[str]:
    """Fetch descriptions, detect closed postings, apply regex blockers (§7)."""
    led, profile = ctx.ledger, ctx.profile
    clients = clients or ats.clients()
    rows = led.postings_in([DISCOVERED]) + [r for r in led.postings_in([ERROR]) if _retry_stage(r) == "enrich"]
    done: list[str] = []
    for row in rows:
        if limit is not None and len(done) >= limit:
            break
        key = row.posting_key
        ref = parse_key(key)
        cached = led.details(key)
        try:
            if _details_fresh(cached):
                desc = cached["description_text"]
            else:
                det = clients[ref.ats].fetch(ref)
                if det.closed:
                    led.set_state(key, CLOSED, "posting_closed")
                    stats["closed"] += 1
                    continue
                desc = det.description_text
                led.save_details(key, desc, det.questions)
                fill = {k: getattr(det, k) for k in ("company", "title", "location")
                        if getattr(det, k) and not row.get(k)}
                led.update_fields(key, **fill)
        except ats.FetchError as e:
            fail(ctx, key, "enrich", e, stats)
            continue
        led.set_state(key, ENRICHED)
        stats["enriched"] += 1
        hits = blockers.scan(desc, check_us_person=profile.visa.skip_us_person_roles,
                             check_sponsorship=needs_sponsorship(ctx))
        hit = blockers.confident_block(hits)
        if hit:
            skip(ctx, key, hit.reason, stats, detail=hit.evidence)
            continue
        done.append(key)
    return done


def needs_sponsorship(ctx: Context) -> bool:
    v = ctx.profile.visa
    return v.status == "needs_sponsorship_now" or (v.status != "citizen_gc" and v.needs_sponsorship_future)


def skip(ctx: Context, key: str, reason: str, stats: RunStats, detail: str | None = None):
    ctx.ledger.set_state(key, SKIPPED, reason, detail)
    stats.skipped(reason)
    if reason in WRITEBACK_SKIPS and ctx.writeback is not None:
        ctx.writeback.queue(key, skip_change(reason))


# ------------------------------------------------------------------ score --
def not_targeted(ctx: Context, row) -> str | None:
    """Why a posting is no longer wanted under the current filters (None = still wanted).

    The prefilter only sees new postings; this applies a narrowed `filters:` to
    postings already past it, without spending anything or losing their state.
    """
    f = ctx.profile.filters
    if row.get("tier") and row["tier"] not in f.tiers:
        return "tier"
    if row.get("role") and row["role"] not in f.roles:
        return "role"
    if row.get("ats") and row["ats"] not in f.ats_allowed:
        return "ats_not_allowed"
    return None


def release_low_fit(ctx: Context, stats: RunStats) -> None:
    """With fit.low_fit: hold, earlier low_fit skips become held again and their
    `skip` mark in job-monitor is undone (only where this tool set it)."""
    if ctx.profile.fit.low_fit != "hold":
        return
    for row in [r for r in ctx.ledger.postings_in([SKIPPED]) if r.reason == "low_fit"]:
        ctx.ledger.set_state(row.posting_key, HELD, "below_cutoff", "released: fit.low_fit is hold")
        stats["released"] += 1
        if ctx.writeback is not None:
            ctx.writeback.queue(row.posting_key, unskip_change("low_fit"))


def score(ctx: Context, stats: RunStats, limit: int | None = None, rescore: bool = False) -> list[str]:
    """Claude fit score; blockers and the cutoff decide skip / hold / fill (§9.1).

    Held postings are re-decided every run from their stored score (free), so a
    lower cutoff takes effect at once; they are scored again only when the resume
    used for them changed. `limit` counts postings that need a Claude call.
    """
    led, profile = ctx.ledger, ctx.profile
    rows = (led.postings_in([ENRICHED]) + [r for r in led.postings_in([ERROR]) if _retry_stage(r) == "score"]
            + led.postings_in([HELD]))
    calibration = ctx.calibration_examples()
    passed: list[str] = []
    calls = 0
    for row in rows:
        key = row.posting_key
        if not_targeted(ctx, row):
            stats["not_targeted"] += 1
            continue
        resume = ctx.resumes.file_for(row.get("role"))
        rhash = ctx.resumes.hash(resume)
        s = None if rescore else led.score(key, SCORE_VERSION, rhash)
        if s is None:
            if limit is not None and calls >= limit:
                continue
            calls += 1
            det = led.details(key) or {}
            desc = det.get("description_text") or ""
            hints = [h.evidence for h in blockers.scan(
                desc, ctx.profile.visa.skip_us_person_roles, needs_sponsorship(ctx)) if not h.confident]
            try:
                s = score_posting(ctx.llm, profile, ctx.resumes.text(resume), dict(row.data), desc,
                                  calibration, hints)
            except BudgetExceeded as e:
                stats["stopped"] = str(e)
                log.warning("budget reached - no more Claude calls this run")
                break
            except LLMError as e:
                fail(ctx, key, "score", e, stats)
                continue
            led.save_score(key, s, profile.llm.score_model, SCORE_VERSION, rhash)
            stats["scored"] += 1
        decide(ctx, key, row, s, stats)
        if led.get(key).state == SCORED:
            passed.append(key)
    return passed


def decide(ctx: Context, key: str, row, s: dict, stats: RunStats):
    profile = ctx.profile
    if row.state == ERROR:
        ctx.ledger.set_state(key, ENRICHED, None, "retry")
    reasons = {b["type"] for b in s.get("blockers", [])}
    if "us_person_required" in reasons and profile.visa.skip_us_person_roles:
        return skip(ctx, key, "us_person_required", stats)
    if "no_sponsorship" in reasons and needs_sponsorship(ctx):
        return skip(ctx, key, "no_sponsorship", stats)
    if "yoe" in reasons:
        return skip(ctx, key, "yoe", stats)
    if "blocker_other" in reasons:
        return skip(ctx, key, "blocker_other", stats)
    if not row.get("role") and s.get("role_family") not in profile.filters.roles:
        # CSV rows have no role until scoring (§6.4.5)
        ctx.ledger.set_state(key, SKIPPED, "role")
        stats.skipped("role")
        return
    if s["score"] < profile.fit.cutoff:
        if profile.fit.low_fit == "skip":
            return skip(ctx, key, "low_fit", stats)
        if row.state != HELD:
            ctx.ledger.set_state(key, HELD, "below_cutoff", f"score {s['score']} < {profile.fit.cutoff}")
            stats["held"] += 1
        return
    ctx.ledger.set_state(key, SCORED, None, "cutoff met" if row.state == HELD else None)
    if row.state == HELD:
        stats["unheld"] += 1


def role_for(ctx: Context, key: str) -> str | None:
    row = ctx.ledger.get(key)
    if row and row.get("role"):
        return row["role"]
    s = ctx.ledger.score(key)
    return s.get("role_family") if s else None


def fill_candidates(ctx: Context, limit: int | None) -> list[str]:
    led = ctx.ledger
    rows = led.postings_in([SCORED]) + [r for r in led.postings_in([ERROR]) if _retry_stage(r) == "fill"]
    cap = ctx.profile.filters.per_company_cap
    taken: Counter = Counter()
    if cap:
        for r in led.db.execute("SELECT p.company, COUNT(*) FROM applications a JOIN postings p "
                                "USING(posting_key) GROUP BY p.company"):
            taken[r[0]] = r[1]
    out = []
    for r in rows:
        if limit is not None and len(out) >= limit:
            break
        if not_targeted(ctx, r):
            continue                     # filters narrowed since it was scored: not filled now
        if cap and taken[r.get("company")] >= cap:
            continue
        taken[r.get("company")] += 1
        out.append(r.posting_key)
    return out


# -------------------------------------------------------------------- run --
def run(ctx: Context, limit: int | None = None, no_fill: bool = False, fill_runner=None) -> dict:
    """`autoapply run` after the health check. Returns the run's counts."""
    led = ctx.ledger
    run_id = led.start_run("run")
    stats = RunStats()
    error = None
    try:
        sync(ctx, stats)
        release_low_fit(ctx, stats)
        enrich(ctx, stats, limit)
        score(ctx, stats, limit)
        if not no_fill and "stopped" not in stats:
            keys = fill_candidates(ctx, limit)
            if keys:
                if fill_runner is None:
                    from .browser.fill import run_fill
                    fill_runner = run_fill
                fill_runner(ctx, keys, stats)
    except Exception as e:  # noqa: BLE001 - recorded on the run, then re-raised
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        if ctx.writeback is not None:
            stats["written_back"] += ctx.writeback.flush()
        ctx.notifier.flush_needs_input()
        counts = stats.as_dict()
        led.end_run(run_id, counts, error)
        if error is None:
            ctx.notifier.run_summary(counts, led.month_spend(), ctx.profile.llm.monthly_budget_usd)
    return stats.as_dict()


# what each run counter means, in the order a posting meets them
RUN_LINES = [
    ("new", "new jobs found in your sources"),
    ("eligible", "jobs waiting to be looked at (passed the free filters)"),
    ("filtered_out", "filtered out by the free rules"),
    ("unsupported", "on a site the tool cannot apply on"),
    ("enriched", "looked at this run (description fetched; --limit caps this)"),
    ("closed", "closed postings"),
    ("not_targeted", "not targeted by your current filters (tier, role or site)"),
    ("released", "earlier low-fit skips moved back to held"),
    ("scored", "scored by Claude"),
    ("held", "held: fit score below your cutoff"),
    ("unheld", "held earlier, now above the cutoff"),
    ("skipped", "skipped"),
    ("ready", "filled -> Ready"),
    ("needs_input", "filled -> Needs input"),
    ("stuck", "Stuck"),
    ("error", "errors (retried next run)"),
    ("written_back", "status changes written to job-monitor"),
]


def run_breakdown(counts: dict) -> list[str]:
    """The last run's counters as readable lines for `autoapply status`."""
    if not counts:
        return ["  (no numbers recorded for it)"]
    known = {k for k, _ in RUN_LINES} | {"skipped_by_reason", "stopped"}
    why = counts.get("skipped_by_reason") or {}
    lines = []
    for k, label in RUN_LINES:
        if not counts.get(k):
            continue
        if k == "skipped" and why:
            label += ": " + ", ".join(f"{r} {n}" for r, n in sorted(why.items(), key=lambda kv: -kv[1]))
        lines.append(f"  {counts[k]:>5}  {label}")
    lines += [f"  {v:>5}  {k}" for k, v in sorted(counts.items())
              if k not in known and isinstance(v, int) and v]
    filled = sum(counts.get(k, 0) for k in ("ready", "needs_input", "stuck"))
    lines.append(f"  -> {filled} application(s) added to the review queue")
    if counts.get("stopped"):
        lines.append(f"  Stopped early: {counts['stopped']}")
    return lines


def status_report(ctx: Context) -> str:
    led = ctx.ledger
    counts = led.counts()
    lines = ["Postings by state:"]
    for state, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {state:<18} {n}")
    reasons = led.reason_counts()
    if reasons:
        lines.append("Top reasons:")
        for k, n in list(reasons.items())[:15]:
            lines.append(f"  {k:<40} {n}")
    spend = led.month_spend()
    budget = ctx.profile.llm.monthly_budget_usd
    lines.append(f"Spend this month: ${spend:.2f} of ${budget:.2f}")
    last = led.last_run()
    if last:
        lines.append(f"Last run: {last['started_at']} -> {last['ended_at'] or 'running/crashed'}"
                     + (f" (error: {last['error']})" if last.get("error") else ""))
        counts_json = last.get("counts_json")
        lines += run_breakdown(json.loads(counts_json) if counts_json else {})
    pend = led.db.execute("SELECT COUNT(*) FROM writeback_queue WHERE done_at IS NULL").fetchone()[0]
    if pend:
        lines.append(f"Write-backs pending: {pend}")
    return "\n".join(lines)
