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
from .companies import pauses, paused_in, selected, until_text
from .context import Context
from .filters import location_ok, prefilter, us_person_company
from .h1b import H1BIndex
from .ledger import (CLOSED, DISCOVERED, ENRICHED, ERROR, HELD, MAX_ERROR_RETRIES, NEEDS_INPUT, READY,
                     RECHECKABLE, SCORED, SKIPPED, STUCK, IllegalTransition)
from .llm import BudgetExceeded, LLMError, LLMUnavailable
from .llm.score import PROMPT_VERSION as SCORE_VERSION
from .llm.score import score_posting
from .sources import SourceError
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
# a posting still waiting here stops when you mark it applied / skip in job-monitor, or it closes there
TRACKER_STOPPABLE = {ENRICHED, SCORED, HELD, ERROR, READY, NEEDS_INPUT, STUCK}


def tracker_says_stop(p) -> str | None:
    """Why the tracker now rules this posting out (None: it does not). Marks this tool
    wrote itself (skip by autoapply, applied via autoapply) are not the user's decision."""
    if p.closed_at:
        return "monitor_closed"
    st = (p.monitor_status or "new").strip().lower()
    if st == "new":
        return None
    extra = p.extra or {}
    if st == "skip" and extra.get("skipped_by") == "autoapply":
        return None
    if st == "applied" and extra.get("applied_via") == "autoapply":
        return None
    return f"monitor_{st}"


def sync(ctx: Context, stats: RunStats) -> None:
    """Read every source, record postings, run the free rules (§6.4)."""
    led, profile = ctx.ledger, ctx.profile
    eligible: set[str] = set()
    for src in ctx.sources:
        postings = list(src.postings())
        try:
            h1b_data = src.h1b() if profile.visa.sponsor_filter != "off" else None
        except SourceError as e:
            # without the list, new postings cannot be checked for sponsorship: this source
            # waits for the next run; postings already past this step carry on
            log.warning("%s - skipping new postings from this source this run", e)
            ctx.notifier.warn(f"{e}. New postings from it wait for the next run.")
            stats["source_skipped"] += 1
            continue
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
                if row.state in TRACKER_STOPPABLE:
                    why = tracker_says_stop(p)
                    if why:
                        led.set_state(key, CLOSED, why, "changed in the tracker")
                        stats["tracker_closed"] += 1
                        continue
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
    """One more failure for this posting at `stage`. The third in a row gives up on it: a form
    goes to Stuck for you; a posting that never got as far as a form (reading the job, scoring
    it) is skipped. The count resets whenever the posting makes progress."""
    led = ctx.ledger
    row = led.get(key)
    if row.get("error_count", 0) + 1 >= MAX_ERROR_RETRIES:
        try:
            if stage == "fill":
                led.set_state(key, STUCK, f"{stage}: {err}"[:300])
                led.upsert_application(key, lane=STUCK, stuck_reason=f"failed {MAX_ERROR_RETRIES} times "
                                       f"at {stage}: {err}"[:500])
                stats["stuck"] += 1
            else:
                led.set_state(key, SKIPPED, f"failed_{stage}", str(err)[:300])
                stats.skipped(f"failed_{stage}")
            return
        except IllegalTransition:
            pass                          # not reachable from where it is: count one more error
    led.set_state(key, ERROR, f"{stage}: {err}"[:300])
    stats["error"] += 1


def enrich(ctx: Context, stats: RunStats, limit: int | None, clients: dict | None = None) -> list[str]:
    """Fetch descriptions, detect closed postings, apply regex blockers (§7)."""
    led, profile = ctx.ledger, ctx.profile
    clients = clients or ats.clients()
    # retries first: behind a large backlog and a --limit they would otherwise never come up
    rows = [r for r in led.postings_in([ERROR]) if _retry_stage(r) == "enrich"] + led.postings_in([DISCOVERED])
    rows = [r for r in rows if selected(ctx, r.get("company"))]          # this run's --company / --skip-company
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
        except (ats.FetchError, ValueError, KeyError, TypeError) as e:
            # ValueError: a 200 that is not the JSON promised (a captive portal, an error page)
            fail(ctx, key, "enrich", e if isinstance(e, ats.FetchError) else f"{type(e).__name__}: {e}", stats)
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
    if us_person_company(row.get("company"), ctx.profile):
        return "us_person_company"
    if row.get("location") is not None and not location_ok(row.get("location"), f.location, f.metros):
        return "location"
    return None


# Stuck reasons that are usually passing trouble: filled again automatically by the next run,
# at most MAX_AUTO_RETRIES times per posting. Everything else waits for you on the Stuck page
# (CAPTCHA, login wall, emailed code, a Submit that may have gone through, account trouble).
AUTO_RETRY = {"timed_out", "page_load_failed", "form_not_found", "replay_mismatch", "resume_upload_failed",
              "cover_letter_upload_failed", "simplify_not_ready", "simplify_did_not_fill", "simplify_click_failed",
              "simplify_missing", "consistency_check", "workday_stuck_on_page", "workday_page_error",
              "workday_too_many_pages"}
MAX_AUTO_RETRIES = 2


def stuck_kind(reason: str | None) -> str:
    return (reason or "").split(":", 1)[0].strip()


def retry_stuck(ctx: Context, stats: RunStats) -> list[str]:
    """Queue again the Stuck applications whose trouble is usually temporary."""
    led = ctx.ledger
    keys = [r.posting_key for r in led.postings_in([STUCK])
            if stuck_kind(r.reason) in AUTO_RETRY and not ctx.has_live_tab(r.posting_key)
            and led.event_count(r.posting_key, "auto_retry") < MAX_AUTO_RETRIES]
    done = led.requeue_keys(keys, event="auto_retry")
    if done:
        stats["retried"] = len(done)
        log.info("%d Stuck application(s) will be filled again (temporary trouble last time)", len(done))
    return done


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
    rows = [r for r in rows if selected(ctx, r.get("company"))]
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
            except LLMUnavailable as e:
                stats["stopped"] = str(e)
                log.warning("Claude is unavailable - scoring stops for this run: %s", e)
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


def fill_candidates(ctx: Context, limit: int | None, stats: RunStats | None = None) -> list[str]:
    led = ctx.ledger
    active_pauses = pauses(ctx)
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
        if not selected(ctx, r.get("company")):
            continue                     # not one of this run's companies
        if paused_in(active_pauses, r.get("company")):
            if stats is not None:
                stats["company_paused"] += 1
            continue                     # paused by you (30-day limit): scored, filled after the pause
        if led.score(r.posting_key) is None:
            continue                     # never fill what was not scored (blockers, fit)
        if cap and taken[r.get("company")] >= cap:
            continue
        if r.posting_key.startswith("workday:"):
            from .browser.workday import account_blocked
            if account_blocked(ctx, r.posting_key):
                continue                 # its Workday sign-in failed; waits until the account is fixed
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
        back = led.recover_interrupted()
        if back:
            stats["interrupted"] = len(back)
            log.info("%d application(s) were mid-fill when the last run stopped; they are queued again", len(back))
        retry_stuck(ctx, stats)
        sync(ctx, stats)
        release_low_fit(ctx, stats)
        enrich(ctx, stats, limit)
        score(ctx, stats, limit)
        if not no_fill and "stopped" not in stats:
            keys = fill_candidates(ctx, limit, stats)
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
            ctx.notifier.run_summary(counts, led.month_spend(), ctx.profile.llm.monthly_budget_usd,
                                     run_cost=led.run_spend(led.run(run_id))["total"])
    return stats.as_dict()


# what each run counter means, in the order a posting meets them
RUN_LINES = [
    ("interrupted", "left mid-fill by a closed run, queued again"),
    ("retried", "Stuck last time for a temporary reason, filled again"),
    ("tracker_closed", "marked applied/skip or closed in job-monitor since, dropped here"),
    ("source_skipped", "sources whose new jobs wait for the next run (H-1B list unreadable)"),
    ("new", "new jobs found in your sources"),
    ("eligible", "jobs waiting to be looked at (passed the free filters)"),
    ("filtered_out", "filtered out by the free rules"),
    ("unsupported", "on a site the tool cannot apply on"),
    ("enriched", "looked at this run (description fetched; --limit caps this)"),
    ("closed", "closed postings"),
    ("not_targeted", "not targeted by your current filters (tier, role, site, location or a US-person-only employer)"),
    ("released", "earlier low-fit skips moved back to held"),
    ("scored", "scored by Claude"),
    ("held", "held: fit score below your cutoff"),
    ("unheld", "held earlier, now above the cutoff"),
    ("skipped", "skipped"),
    ("ready", "filled -> Ready"),
    ("needs_input", "filled -> Needs input"),
    ("stuck", "Stuck"),
    ("corrected", "forms where Simplify's visa answer was replaced by yours"),
    ("company_paused", "waiting: company paused by you (30-day limit), filled after the pause"),
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


PURPOSE_LABELS = {"score": "scoring", "answer": "form answers", "cover_letter": "cover letters",
                  "wizard": "setup"}


def cost_line(spend: dict, what: str = "this run") -> str:
    """"Claude cost this run: $0.42 (scoring $0.10, 85 calls · form answers $0.27, 12 calls)"."""
    parts = [f"{PURPOSE_LABELS.get(p, p)} ${c:.2f}, {n} call{'s' if n != 1 else ''}"
             for p, (c, n) in sorted(spend["by_purpose"].items(), key=lambda kv: -kv[1][0])]
    return f"Claude cost {what}: ${spend['total']:.2f}" + (f" ({' · '.join(parts)})" if parts else "")


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
        lines.append("  " + cost_line(led.run_spend(last)))
    active = pauses(ctx)
    if active:
        lines.append("Paused companies (scored, not filled): "
                     + "; ".join(until_text(v) for v in sorted(active.values(), key=lambda v: v["until"])))
    pend = led.db.execute("SELECT COUNT(*) FROM writeback_queue WHERE done_at IS NULL").fetchone()[0]
    if pend:
        lines.append(f"Write-backs pending: {pend}")
    return "\n".join(lines)
