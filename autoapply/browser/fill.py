"""Fill one application (§8.2-§8.7) and the parallel worker pool that runs many.

Order per form: open -> Simplify autofill -> read form -> swap resume -> fill
blanks (answer bank, then Claude) -> lane -> fill plan + screenshot. Nothing is
ever submitted here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from ..ats import application_url, form_hints, parse_key
from ..config import Profile
from ..ledger import ERROR, FILLING, NEEDS_INPUT, READY, SCORED, STUCK
from ..llm import BudgetExceeded, LLMError
from ..llm.answer import answer_fields
from ..llm.cover_letter import render_pdf, write_cover_letter
from ..normalize import normalize_question, slug
from .fields import Field, FillError, combobox_options, safe_screenshot, enumerate_fields, fill_field
from .simplify import SimplifyError, autofill

log = logging.getLogger(__name__)

CAPTCHA_CHALLENGE = ("iframe[src*='hcaptcha.com'][src*='challenge'], iframe[title*='challenge' i], "
                     "iframe[src*='recaptcha/api2/bframe'], iframe[src*='turnstile']")
LOGIN_WALL = "input[type=password]"
OPTION_CAP = 200


class Stuck(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class PlanEntry:
    key: str
    label: str
    kind: str
    value: str | list
    source: str               # simplify | answer_bank | claude | user | resume | cover_letter
    required: bool = False
    options: list = field(default_factory=list)
    evidence: str | None = None
    facts_used: list = field(default_factory=list)
    claude_kind: str | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "") or k in ("value", "source")}


@dataclass
class FillOutcome:
    lane: str
    plan: list[dict]
    missing: list[dict]
    stuck_reason: str | None
    screenshot: str | None
    resume_file: str
    generated: list[str] = field(default_factory=list)
    page: object = None
    corrected: list[str] = field(default_factory=list)   # Simplify legal answers replaced by the profile's


# ------------------------------------------------------------ the checks --
SPONSOR = re.compile(r"sponsor", re.I)
FUTURE = re.compile(r"future|at any (point|time)|now or|will you (ever|now|in)", re.I)
AUTHORIZED = re.compile(r"(legally )?authori[sz]ed to work|eligible to work|work authori[sz]ation", re.I)


def _yes_no(v: str) -> str | None:
    n = normalize_question(v)
    if n.startswith("yes") or n in ("true", "i do", "i will"):
        return "yes"
    if n.startswith("no") or n in ("false", "i do not", "i will not"):
        return "no"
    return None


def expected_legal_answer(label: str, profile: Profile) -> str | None:
    """What the user's confirmed visa answers say this yes/no question must be."""
    v = profile.visa
    if SPONSOR.search(label):
        needs_now = v.status == "needs_sponsorship_now"
        needs_future = needs_now or (v.status != "citizen_gc" and v.needs_sponsorship_future)
        return "yes" if (needs_future if FUTURE.search(label) else needs_now) else "no"
    if AUTHORIZED.search(label):
        return "no" if v.status == "needs_sponsorship_now" else "yes"
    return None


def legal_mismatches(fields: list[Field], profile: Profile) -> list[tuple[Field, str]]:
    """Simplify-filled work authorization / sponsorship answers that contradict the
    user's confirmed visa answers, with the answer the profile requires ("yes"/"no")."""
    out = []
    for f in fields:
        if not f.filled_by_simplify:
            continue
        exp = expected_legal_answer(f.label, profile)
        got = _yes_no(f.value)
        if exp and got and exp != got:
            out.append((f, exp))
    return out


async def correct_legal_answers(page, fields: list[Field], profile: Profile, plan: list, missing: list) -> list[str]:
    """§15.8: the profile's visa answers win over Simplify's. Each wrong answer is
    replaced (and flagged for review) or, if the form will not take it, asked in
    Needs input. The run goes on either way. Returns the corrected labels."""
    corrected = []
    for f, exp in legal_mismatches(fields, profile):
        note = f"Simplify answered {f.value!r}; your visa answers say {exp.capitalize()}"
        f.filled_by_simplify = False                 # not Simplify's answer any more
        if f.kind in ("combobox", "wd_select") and not f.options:
            f.options = await combobox_options(page, f)
        value = next((o for o in f.options if _yes_no(o) == exp), None) if f.options else exp.capitalize()
        if value is None:
            missing.append(_missing(f, note + " - pick the right option"))
            continue
        try:
            await fill_field(page, f, value)
        except FillError as e:
            missing.append(_missing(f, f"{note} - the form did not take the correction ({e})"))
            continue
        plan.append(PlanEntry(f.key, f.label, f.kind, value, "profile", f.required, f.options, note=note))
        corrected.append(f.label)
        log.warning("    corrected %r: %s", f.label, note)
    return corrected


async def detect_blockers(page) -> str | None:
    if await page.locator(CAPTCHA_CHALLENGE).count():
        for i in range(await page.locator(CAPTCHA_CHALLENGE).count()):
            if await page.locator(CAPTCHA_CHALLENGE).nth(i).is_visible():
                return "captcha"
    pw = page.locator(LOGIN_WALL)
    for i in range(await pw.count()):
        if await pw.nth(i).is_visible():
            return "login_required"
    return None


# ------------------------------------------------------------- one form --
def form_url(ctx, posting_key: str) -> str:
    override = getattr(ctx, "form_url_for", None)
    return override(posting_key) if override else application_url(parse_key(posting_key))


async def open_form(page, url: str, hints: dict, timeout_s: float) -> None:
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
    except Exception as e:  # noqa: BLE001
        raise Stuck("page_load_failed", str(e).splitlines()[0][:200]) from e
    if resp is not None and resp.status == 404:
        raise Stuck("posting_gone", "the application page answered 404")
    try:
        await page.wait_for_selector("input[type=file], textarea, input[type=text], input[type=email]",
                                     state="attached", timeout=timeout_s * 1000)
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 - networkidle never settling is normal on some boards
        pass
    if not await page.locator("input, textarea, select").count():
        raise Stuck("form_not_found", "no form fields on the application page")
    blocker = await detect_blockers(page)
    if blocker:
        raise Stuck(blocker, "the page asks for a CAPTCHA or a login before the form")


_simplify_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def simplify_turn() -> asyncio.Lock:
    """One form at a time gets Simplify. Simplify pauses its filling while its tab is
    hidden (VERIFIED in the 3.1.10 content script), and only the front tab of the
    window is visible, so each form takes its turn in front."""
    loop = asyncio.get_running_loop()
    lock = _simplify_locks.get(loop)
    if lock is None:
        lock = _simplify_locks[loop] = asyncio.Lock()
    return lock


async def open_workday(ctx, page, posting_key: str) -> None:
    """Open a Workday application ("Apply Manually"); the walk handles what it shows."""
    try:
        await page.goto(form_url(ctx, posting_key), wait_until="domcontentloaded",
                        timeout=ctx.profile.browser.page_timeout_s * 1000)
    except Exception as e:  # noqa: BLE001
        raise Stuck("page_load_failed", str(e).splitlines()[0][:200]) from e


async def run_simplify(ctx, page, hints: dict) -> None:
    turn = simplify_turn()
    if turn.locked():
        log.info("    waiting for Simplify (one form at a time)")
    async with turn:
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001 - a tab that cannot come forward still gets its try
            log.debug("could not bring the tab to the front")
        mock = getattr(ctx, "simplify_mock", None)
        if mock is not None:
            await mock(page)
            return
        try:
            await autofill(page, hints, timeout_s=ctx.profile.browser.autofill_timeout_s)
        except SimplifyError as e:
            raise Stuck(e.kind, str(e)) from e


async def attach_resume(page, fields: list[Field], resume: Path, hints: dict) -> Field | None:
    target = next((f for f in fields if f.is_resume), None)
    if target is None:
        target = next((f for f in fields if f.kind == "file" and not f.is_cover_letter), None)
    if target is None:
        return None
    try:
        await fill_field(page, target, str(resume))
    except FillError as e:
        raise Stuck("resume_upload_failed", str(e)) from e
    await page.wait_for_timeout(1500)
    if not await _upload_registered(page, target, resume.name, hints):
        raise Stuck("resume_upload_failed", f"{resume.name} did not show up in the form")
    return target


async def _upload_registered(page, f: Field, filename: str, hints: dict) -> bool:
    for _ in range(10):
        fresh = {x.key: x for x in await enumerate_fields(page, hints)}
        val = fresh.get(f.key).value if f.key in fresh else ""
        if filename.lower() in (val or "").lower():
            return True
        # many boards replace the input with a "filename.pdf  x" chip
        if await page.get_by_text(filename, exact=False).count():
            return True
        await page.wait_for_timeout(500)
    return False


async def fill_one(ctx, bs, posting_key: str, check_consistency: bool = False,
                   track: dict | None = None) -> FillOutcome:
    """Fill the form for one scored posting. Leaves the page open in the outcome."""
    from ..pipeline import role_for
    led = ctx.ledger
    row = led.get(posting_key)
    ref = parse_key(posting_key)
    hints = form_hints(ref.ats)
    role = role_for(ctx, posting_key)
    resume_name = ctx.resumes.file_for(role)
    resume_path = ctx.resumes.path(resume_name)
    resume_text = ctx.resumes.text(resume_name)
    det = led.details(posting_key) or {}
    desc = det.get("description_text") or ""
    posting = dict(row.data)
    track = track if track is not None else {}
    page = await bs.new_page()
    track["page"] = page
    try:
        if hints.get("workday"):
            from .workday import fill_application
            return await fill_application(ctx, page, posting_key, resume_name, resume_path, resume_text,
                                          desc, posting, check_consistency, track)
        return await _fill_on_page(ctx, page, posting_key, ref, hints, resume_name, resume_path,
                                   resume_text, desc, posting, check_consistency, track)
    except BaseException as e:
        e.page = page        # the caller screenshots / closes it
        raise


async def _fill_on_page(ctx, page, posting_key, ref, hints, resume_name, resume_path, resume_text,
                        desc, posting, check_consistency, track) -> FillOutcome:
    def step(name: str):
        track["step"] = name
        log.info("    %s", name)

    profile = ctx.profile
    plan: list[PlanEntry] = []
    missing: list[dict] = []
    generated: list[str] = []
    step("opening the form")
    await open_form(page, form_url(ctx, posting_key), hints, ctx.profile.browser.page_timeout_s)
    before = {f.key: f.value for f in await enumerate_fields(page, hints)}
    step("Simplify autofill")
    await run_simplify(ctx, page, hints)
    fields = await enumerate_fields(page, hints)
    for f in fields:
        f.filled_by_simplify = bool(f.value.strip()) and not (before.get(f.key) or "").strip()

    corrected = await correct_legal_answers(page, fields, profile, plan, missing) if check_consistency else []

    for f in fields:
        if f.filled_by_simplify and f.kind != "file":
            plan.append(PlanEntry(f.key, f.label, f.kind, f.value, "simplify", f.required, f.options))

    # §8.4 the one exception to "Simplify wins": the resume chosen for this job
    log.info("    Simplify filled %d field(s)", sum(f.filled_by_simplify for f in fields))
    step(f"attaching {resume_name}")
    rf = await attach_resume(page, fields, resume_path, hints)
    if rf is not None:
        plan.append(PlanEntry(rf.key, rf.label, "file", resume_name, "resume", rf.required))
    elif any(f.required and f.kind == "file" for f in fields):
        raise Stuck("resume_upload_failed", "no resume file field found")

    await fill_blanks(ctx, page, fields, {rf.key} if rf is not None else set(), plan, missing, generated,
                      posting, desc, resume_text, step)

    step("checking the filled form")
    await final_check(page, hints, plan, missing)

    shot = ctx.paths.screenshots / f"{slug(posting_key, 80)}.png"
    shot = await safe_screenshot(page, shot)
    lane = NEEDS_INPUT if missing else READY
    return FillOutcome(lane=lane, plan=[p.to_dict() for p in plan], missing=missing, stuck_reason=None,
                       screenshot=shot, resume_file=resume_name, generated=generated, page=page,
                       corrected=corrected)


async def final_check(page, hints: dict, plan: list, missing: list) -> None:
    """Final read: a value the page did not keep, or a required field nobody claimed, is missing."""
    final = await enumerate_fields(page, hints)
    by_key = {f.key: f for f in final}
    for p in list(plan):
        f = by_key.get(p.key)
        if p.source in ("answer_bank", "claude", "profile", "user") and f is not None and f.empty:
            plan.remove(p)
            if f.required:
                m = _missing(f, "the form did not keep the answer")
                m["suggested"] = p.value if isinstance(p.value, str) else None
                missing.append(m)
    claimed = {m["key"] for m in missing} | {p.key for p in plan}
    for f in final:
        if f.required and f.empty and f.visible and f.key not in claimed and f.kind != "file":
            missing.append(_missing(f, "still empty after autofill"))


async def fill_blanks(ctx, page, fields: list[Field], skip: set[str], plan: list, missing: list, generated: list,
                      posting: dict, desc: str, resume_text: str, step, known: dict | None = None) -> None:
    """Fill what Simplify left empty: answers already given for this application
    (`known`, by question), the answer bank, a cover letter, then Claude. What none
    of them may answer goes to `missing`."""
    profile = ctx.profile
    known = known or {}
    blanks = [f for f in fields if f.empty and (f.visible or f.kind == "file") and f.key not in skip]
    to_claude: list[Field] = []
    for f in blanks:
        if f.kind == "file":
            if f.is_cover_letter and f.required:
                entry = await _cover_letter_file(ctx, page, f, resume_text, posting, desc, generated)
                plan.append(entry)
            elif f.required:
                missing.append(_missing(f, "file upload the tool cannot provide"))
            continue
        if f.kind in ("combobox", "wd_select") and not f.options:
            f.options = await combobox_options(page, f)
        # a capped option list (School: thousands) is not a complete list to match against;
        # the combobox fill types the answer and fails loudly if no option matches
        truncated = len(f.options) >= OPTION_CAP
        prior = known.get((normalize_question(f.label), f.kind))
        if prior is not None and f.kind != "file":
            if await _try_fill(page, f, prior["value"], missing):
                plan.append(PlanEntry(f.key, f.label, f.kind, prior["value"], prior.get("source") or "user",
                                      f.required, f.options, evidence=prior.get("evidence"),
                                      facts_used=prior.get("facts_used") or [], claude_kind=prior.get("claude_kind")))
            continue
        banked = ctx.answers.lookup(f.label, f.answer_kind, None if truncated else (f.options or None))
        if banked is not None:
            if await _try_fill(page, f, banked, missing):
                plan.append(PlanEntry(f.key, f.label, f.kind, banked, "answer_bank", f.required, f.options))
            continue
        if f.is_cover_letter:
            if f.required and f.kind in ("text", "textarea"):
                text = await asyncio.to_thread(write_cover_letter, ctx.llm, profile, resume_text, posting, desc)
                if await _try_fill(page, f, text, missing):
                    plan.append(PlanEntry(f.key, f.label, f.kind, text, "claude", True, claude_kind="cover_letter"))
            continue           # optional cover letters stay empty (§8.5.3)
        if f.is_eeo or f.kind in ("checkbox", "checkbox_group"):
            if f.required:     # never guessed (§8.5.5); consent boxes are the user's call
                missing.append(_missing(f, "demographic / consent question" if f.is_eeo else "checkbox"))
            continue
        if f.required or f.kind in ("text", "textarea"):
            to_claude.append(f)

    if to_claude:
        step(f"asking Claude about {len(to_claude)} blank field(s)")
        listing = [{"id": f.key, "label": f.label, "type": f.kind if f.kind != "text" else (f.input_type or "text"),
                    "options": f.options, "required": f.required, "max_length": f.max_length} for f in to_claude]
        verdicts = await asyncio.to_thread(answer_fields, ctx.llm, profile, resume_text, posting, desc, listing)
        for f in to_claude:
            v = verdicts.get(f.key) or {"kind": "needs_user"}
            if v["kind"] == "needs_user":
                if f.required:
                    missing.append(_missing(f, v.get("needs_user_reason") or "needs your answer"))
                continue
            if await _try_fill(page, f, v["answer"], missing):
                plan.append(PlanEntry(f.key, f.label, f.kind, v["answer"], "claude", f.required, f.options,
                                      evidence=v.get("evidence"), facts_used=v.get("facts_used") or [],
                                      claude_kind=v["kind"]))



def _missing(f: Field, reason: str) -> dict:
    return {"key": f.key, "label": f.label, "kind": f.kind, "answer_kind": f.answer_kind,
            "options": f.options, "required": f.required, "reason": reason,
            "question": normalize_question(f.label)}


async def _try_fill(page, f: Field, value, missing: list) -> bool:
    try:
        await fill_field(page, f, value)
        return True
    except FillError as e:
        if f.required:
            m = _missing(f, f"the form did not accept the answer ({e})")
            m["suggested"] = value if isinstance(value, str) else None
            missing.append(m)
        return False


async def _cover_letter_file(ctx, page, f: Field, resume_text, posting, desc, generated) -> PlanEntry:
    text = await asyncio.to_thread(write_cover_letter, ctx.llm, ctx.profile, resume_text, posting, desc)
    out = ctx.paths.generated / f"{slug(posting['posting_key'], 80)}-cover-letter.pdf"
    render_pdf(text, out)
    (out.with_suffix(".txt")).write_text(text, encoding="utf-8")
    generated.append(str(out))
    try:
        await fill_field(page, f, str(out))
    except FillError as e:
        raise Stuck("cover_letter_upload_failed", str(e)) from e
    return PlanEntry(f.key, f.label, "file", str(out), "cover_letter", True, claude_kind="cover_letter")


# ------------------------------------------------------ record outcomes --
def record_outcome(ctx, key: str, out: FillOutcome, stats) -> int:
    led = ctx.ledger
    app_id = led.upsert_application(key, lane=out.lane, resume_file=out.resume_file, fill_plan=out.plan,
                                    missing=out.missing, stuck_reason=out.stuck_reason,
                                    screenshot_path=out.screenshot)
    led.set_state(key, out.lane, out.stuck_reason)
    stats[out.lane] += 1
    if out.lane == NEEDS_INPUT:
        row = led.get(key)
        ctx.notifier.needs_input_item(row.get("company") or "", row.get("title") or "",
                                      [m["label"] for m in out.missing])
    return app_id


def record_stuck(ctx, key: str, reason: str, stats, resume_file: str | None = None,
                 screenshot: str | None = None) -> None:
    led = ctx.ledger
    led.upsert_application(key, lane=STUCK, stuck_reason=reason, resume_file=resume_file,
                           screenshot_path=screenshot)
    led.set_state(key, STUCK, reason[:300])
    stats[STUCK] += 1


# --------------------------------------------------------- worker pool --
async def fill_many(ctx, keys: list[str], stats, session=None) -> None:
    from .launch import BrowserSession
    from ..pipeline import fail
    led = ctx.ledger
    # newest first, as selected; Workday last: each one holds the browser's front for its
    # whole walk, so single-page forms go first instead of waiting behind it
    pending = [k for k in keys if not k.startswith("workday:")] + [k for k in keys if k.startswith("workday:")]
    busy: set[str] = set()                    # companies with a form in flight
    busy_keys: set[str] = set()               # the postings in flight
    state = {"stop": None, "probe_done": not getattr(ctx, "simplify_probe_pending", False)}

    def company(k: str) -> str:
        return (led.get(k).get("company") or k).strip().lower()

    total = len(pending)
    done = {"n": 0}

    async def handle(bs, key):
        row = led.get(key)
        done["n"] += 1
        log.info("[%d/%d] %s - %s", done["n"], total, row.get("company"), row.get("title"))
        if row.state == ERROR:
            led.set_state(key, FILLING, None, "retry")
        else:
            led.set_state(key, FILLING)
        page = None
        track: dict = {}
        limit = (ctx.profile.browser.workday_form_timeout_s if key.startswith("workday:")
                 else ctx.profile.browser.form_timeout_s)
        try:
            # §15.8 asks for the check after the first autofill; it is free, so every
            # form is checked - the first one that carries the question is the first check
            try:
                out = await asyncio.wait_for(fill_one(ctx, bs, key, check_consistency=True, track=track), limit)
            except asyncio.TimeoutError:
                err = Stuck("timed_out", f"no result after {int(limit)}s while {track.get('step', 'starting')}")
                err.page = track.get("page")
                raise err from None
            page = out.page
            record_outcome(ctx, key, out, stats)
            if out.corrected:
                stats["corrected"] += 1
            log.info("    -> %s", "Ready" if out.lane == READY else
                     f"Needs input ({len(out.missing)} question(s): {', '.join(m['label'][:40] for m in out.missing[:3])})")
            if not state["probe_done"] or not state.get("marked"):
                state["probe_done"] = state["marked"] = True
                _mark_simplify_good(ctx)
        except Stuck as e:
            page = getattr(e, "page", None)
            log.info("    -> Stuck: %s", e)
            record_stuck(ctx, key, str(e), stats, screenshot=await _shot(ctx, page, key))
            if not state["probe_done"] and e.reason.startswith("simplify"):
                state["stop"] = f"Simplify was updated and autofill failed on the probe form: {e}"
            elif e.reason in ("simplify_logged_out", "simplify_missing", "simplify_consent", "simplify_not_ready"):
                state["stop"] = f"Simplify is not usable: {e}"
        except BudgetExceeded as e:
            page = getattr(e, "page", None)
            led.set_state(key, SCORED, None, "requeued: budget")
            state["stop"] = str(e)
            stats["stopped"] = str(e)
        except LLMError as e:
            page = getattr(e, "page", None)
            log.info("    -> Error, will be retried next run: %s", e)
            fail(ctx, key, "fill", e, stats)
        except Exception as e:  # noqa: BLE001 - one bad form must not end the run
            page = getattr(e, "page", None)
            log.exception("fill %s crashed", key)
            log.info("    -> Error, will be retried next run: %s", str(e).splitlines()[0][:200])
            fail(ctx, key, "fill", f"{type(e).__name__}: {e}", stats)
        finally:
            # with `run --review`, a filled (or stuck) form stays open so approval can submit
            # it as it stands; past keep_tabs_max it closes and approval refills it instead
            lane = led.get(key).state
            if page is not None and not page.is_closed():
                if lane in (READY, NEEDS_INPUT, STUCK) and ctx.can_keep_tab():
                    ctx.remember_tab(key, page)
                else:
                    await page.close()

    async def worker(bs):
        while not state["stop"]:
            # one in-flight form per company (§8.1); asyncio is single-threaded,
            # so nothing can slip in between this check and busy.add()
            # one company at a time, and one Workday application at a time (it needs the front)
            wd_busy = any(b.startswith("workday:") for b in busy_keys)
            key = next((k for k in pending if company(k) not in busy
                        and not (wd_busy and k.startswith("workday:"))), None)
            if key is None:
                if not pending:
                    return
                await asyncio.sleep(0.3)
                continue
            pending.remove(key)
            co = company(key)
            busy.add(co)
            busy_keys.add(key)
            try:
                await handle(bs, key)
            finally:
                busy.discard(co)
                busy_keys.discard(key)

    async def go(bs):
        if not state["probe_done"] and pending:
            # §15.3: after a Simplify update, one form alone decides whether to go on
            await handle(bs, pending.pop(0))
        if state["stop"]:
            return
        n = max(1, ctx.profile.browser.parallel_tabs)
        await asyncio.gather(*(worker(bs) for _ in range(n)))

    if session is not None:
        await go(session)
    else:
        async with BrowserSession(ctx) as bs:
            await go(bs)
    if stats.get("corrected"):
        msg = (f"On {stats['corrected']} form(s) Simplify's work authorization / sponsorship answer disagreed "
               "with your visa answers; the tool used yours (marked on the card). Fix your Simplify profile "
               "(simplify.jobs > Profile > work authorization), or `autoapply setup` if your visa answers are wrong.")
        log.warning(msg)
        ctx.notifier.warn(msg)
    if state["stop"]:
        stats["stopped"] = state["stop"]
        log.error("run stopped: %s", state["stop"])
        ctx.notifier.alert(state["stop"])


async def _shot(ctx, page, key: str) -> str | None:
    if page is None:
        return None
    return await safe_screenshot(page, ctx.paths.screenshots / f"{slug(key, 80)}.png")


def _mark_simplify_good(ctx):
    from .simplify import extension_version
    v = extension_version(ctx.paths.extension)
    if v:
        ctx.state_set("simplify_last_good_version", v)


def run_fill(ctx, keys: list[str], stats) -> None:
    asyncio.run(fill_many(ctx, keys, stats))
