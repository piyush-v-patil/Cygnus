"""Submit an approved application (§8.8). The only module that clicks Submit."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import ats
from ..ats import form_hints, parse_key
from ..ledger import (CLOSED, NEEDS_INPUT, READY, STUCK, SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT)
from ..records import submission_dir, write_record
from ..writeback import applied_change
from .fields import enumerate_fields, safe_screenshot
from .fill import Stuck, detect_blockers
from .replay import replay

log = logging.getLogger(__name__)
RECHECK_AFTER = timedelta(hours=24)
GENERIC_SUBMIT = ("button[type=submit]:visible, input[type=submit]:visible, "
                  "button:has-text('Submit application'):visible, button:has-text('Submit Application'):visible")


# what each board shows once an application went through (Simplify 3.1.10's
# submittedSuccessPaths for these boards, as CSS / text)
SUCCESS_CSS = {"greenhouse": ".confirmation__content, div.confirmation",
               "ashby": "[class*='application-form-success-container']",
               "lever": "[data-qa='msg-submit-success']"}
SUCCESS_URL = re.compile(r"/(confirmation|thanks|thank-you|success)(/|\?|#|$)", re.I)
SUCCESS_TEXT = re.compile(r"thank you for (applying|your application|submitting)|application (has been |was )?"
                          r"(submitted|received|sent)|we('ve| have) received your application|we got your application",
                          re.I)
# Greenhouse: when its bot check is unsure it emails an 8-character security code and shows
# one box per character; the application is only sent once the code is entered and Submit
# is clicked again (VERIFIED 2026-10-23 in the live job-boards client)
CODE_BOX = "[id^='security-input-']"
CODE_TEXT = ".email-verification legend, #email-verification-label"
CODE_ERROR = "#email-verification-error"


class SubmitBlocked(Exception):
    """Raised if anything tries to click Submit in a never-submit run."""


def _waited_too_long(app: dict) -> bool:
    try:
        t = datetime.strptime(app["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return True
    return datetime.now(timezone.utc) - t > RECHECK_AFTER


async def click_submit(ctx, page, hints: dict) -> None:
    if ctx.never_submit:
        raise SubmitBlocked("never_submit is on - Submit must not be clicked")
    sel = hints.get("submit") or GENERIC_SUBMIT
    btn = page.locator(sel)
    if not await btn.count():
        btn = page.locator(GENERIC_SUBMIT)
    if not await btn.count():
        raise Stuck("submit_not_found", "no Submit button on the form")
    await btn.first.scroll_into_view_if_needed()
    await btn.first.click(timeout=15000)


async def _form_problems_after_submit(page, hints: dict) -> str | None:
    blocker = await detect_blockers(page)
    if blocker:
        return blocker
    err = hints.get("error")
    if err:
        loc = page.locator(err)
        for i in range(min(await loc.count(), 20)):
            el = loc.nth(i)
            if await el.is_visible():
                t = (await el.inner_text()).strip()
                if t:
                    return f"form error: {t[:200]}"
    return None


async def _headings(page) -> str:
    try:
        return await page.evaluate("""() => [...document.querySelectorAll(
            'h1, h2, h3, [role=alert], [role=status], [class*=confirmation], [class*=success]')]
            .filter(e => e.offsetParent !== null).map(e => e.innerText).join('\\n')""") or ""
    except Exception:  # noqa: BLE001 - navigating
        return ""


async def _code_prompt(page) -> str | None:
    """Greenhouse's "enter the code we emailed" step: its wording, or None."""
    boxes = page.locator(CODE_BOX)
    try:
        if not await boxes.count() or not await boxes.first.is_visible():
            return None
        t = page.locator(CODE_TEXT)
        return " ".join((await t.first.inner_text()).split()) if await t.count() else "a security code was emailed to you"
    except Exception:  # noqa: BLE001
        return None


SUBMIT_WAIT_S = 30.0


async def after_click(page, ats_name: str, hints: dict, before: str, timeout_s: float | None = None) -> tuple[str, str]:
    """What a Submit click led to: ("confirmed" | "code" | "error" | "unconfirmed", detail)."""
    import asyncio
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (SUBMIT_WAIT_S if timeout_s is None else timeout_s)
    css = SUCCESS_CSS.get(ats_name)
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if page.is_closed():
            return "unconfirmed", "the tab closed"
        code = await _code_prompt(page)
        if code:
            return "code", code
        if css and await page.locator(css).count():
            return "confirmed", "confirmation shown"
        if SUCCESS_URL.search(page.url or ""):
            return "confirmed", page.url
        now = await _headings(page)
        if SUCCESS_TEXT.search(now) and not SUCCESS_TEXT.search(before):
            return "confirmed", SUCCESS_TEXT.search(now).group(0)
        problem = await _form_problems_after_submit(page, hints)
        if problem:
            return "error", problem
    # no message either way: fields the browser itself refuses to send mean it never left
    try:
        invalid = await page.evaluate("""() => [...document.querySelectorAll('input, textarea, select')]
            .filter(e => !e.checkValidity()).map(e => {
              const box = e.closest('.field, .field-wrapper, .application-question, fieldset, li') || e.parentElement;
              const lab = box && box.querySelector('label, legend');
              return (lab && lab.innerText.trim()) || e.name || e.id || 'a field';
            })""")
    except Exception:  # noqa: BLE001
        invalid = []
    if invalid:
        return "error", "the form was not sent; not filled in: " + "; ".join(dict.fromkeys(invalid))[:300]
    # if the form is gone, it was sent
    try:
        still_there = await page.locator(hints.get("submit") or GENERIC_SUBMIT).count()
    except Exception:  # noqa: BLE001
        still_there = 1
    return ("confirmed", "the form went away") if not still_there else ("unconfirmed", "")


async def live_tab_problems(page, app: dict, hints: dict) -> list[str]:
    """Why a kept tab cannot be submitted as it stands ([] = it can).

    Deliberately lenient about values: in a live tab the user may have changed
    an answer by hand, and what is on the page is what gets sent (and saved).
    """
    if page.is_closed():
        return ["the tab was closed"]
    fields = await enumerate_fields(page, hints)
    if not fields:
        return ["the form is gone from the tab"]
    problems = [f"required field {f.label!r} is empty" for f in fields
                if f.required and f.empty and f.visible and f.kind != "file"]
    for e in app.get("fill_plan") or []:
        if e.get("kind") == "file" and e.get("source") in ("resume", "cover_letter"):
            name = str(e["value"]).replace("\\", "/").rsplit("/", 1)[-1]
            on_field = any(name.lower() in (f.value or "").lower() for f in fields if f.kind == "file")
            if not on_field and not await page.get_by_text(name, exact=False).count():
                problems.append(f"{name} is no longer attached")
    return problems


async def get_ready_page(ctx, bs, app: dict):
    """The kept tab if it is still good to send, otherwise a freshly refilled one."""
    key = app["posting_key"]
    hints = form_hints(parse_key(key).ats)
    page, age = ctx.take_tab(key)
    if hints.get("workday"):
        return await _workday_ready_page(ctx, bs, app, page)
    if page is not None:
        max_age = ctx.profile.browser.tab_max_age_min * 60
        try:
            if age is not None and age > max_age:
                log.info("tab for %s is %d min old; refilling it", key, age // 60)
            else:
                problems = await live_tab_problems(page, app, hints)
                if not problems:
                    return page
                log.info("kept tab for %s not usable (%s); refilling", key, "; ".join(problems))
        except Exception:  # noqa: BLE001 - a dead tab just means replay
            pass
        if not page.is_closed():
            await page.close()
    return await replay(ctx, bs, app)


async def _workday_ready_page(ctx, bs, app: dict, page):
    """A Workday tab on Review: the kept tab (walked on to Review if it stopped earlier), else
    the application reopened from the account's draft and walked again."""
    from . import workday
    key = app["posting_key"]
    if page is not None and not page.is_closed():
        try:
            st = await workday.state(page)
            if st == "review":
                return page
            if st == "form" and await workday.resume(ctx, page, key):
                return page
            if st == "form":
                a = ctx.ledger.application(key) or {}
                raise workday.NeedsInputError(a.get("missing") or [], a.get("fill_plan") or [])
        except workday.NeedsInputError as e:
            e.page = page
            raise
        except Exception as e:  # noqa: BLE001 - a dead tab just means reopening
            log.info("kept Workday tab for %s not usable (%s); reopening", key, e)
        if not page.is_closed():
            await page.close()
    return await replay(ctx, bs, app)


async def submit(ctx, bs, posting_key: str) -> dict:
    """Approve & submit. Returns {"state": ..., "detail": ...}."""
    led = ctx.ledger
    row = led.get(posting_key)
    app = led.application(posting_key)
    if row is None or app is None:
        return {"state": "missing", "detail": "no such application"}
    if row.state == NEEDS_INPUT and not app.get("missing"):
        led.set_state(posting_key, READY, None, "all questions answered")
        row = led.get(posting_key)
    if row.state != READY:
        return {"state": row.state, "detail": f"cannot submit from {row.state}"}
    ref = parse_key(posting_key)
    hints = form_hints(ref.ats)

    if _waited_too_long(app) and ref.supported and getattr(ctx, "form_url_for", None) is None:
        try:
            det = ats.clients()[ref.ats].fetch(ref)
            if det.closed:
                led.set_state(posting_key, CLOSED, "closed_before_submit")
                return {"state": CLOSED, "detail": "the posting closed while waiting"}
        except ats.FetchError as e:
            log.warning("could not recheck %s: %s", posting_key, e)

    from .workday import NeedsInputError
    try:
        page = await get_ready_page(ctx, bs, app)
    except Stuck as e:
        _stuck(ctx, posting_key, e, getattr(e, "page", None))
        return {"state": STUCK, "detail": str(e)}
    except NeedsInputError as e:
        led.upsert_application(posting_key, missing=e.missing, fill_plan=e.plan, lane=NEEDS_INPUT)
        if led.get(posting_key).state != NEEDS_INPUT:
            led.set_state(posting_key, NEEDS_INPUT, None, "Workday asked more questions")
        if getattr(e, "page", None) is not None:
            ctx.remember_tab(posting_key, e.page)
        return {"state": NEEDS_INPUT, "detail": f"Workday asked {len(e.missing)} more question(s)"}

    posting = dict(row.data)
    sub = submission_dir(ctx.paths.submissions, posting)
    await safe_screenshot(page, sub / "form.png")
    if hints.get("workday"):
        # Review shows answers as text, not fields: the record is what each page was given
        app = led.application(posting_key) or app
        sent = [{"label": e.get("label"), "value": e.get("value"), "kind": e.get("kind"),
                 "required": e.get("required", False), "source": e.get("source")} for e in app.get("fill_plan") or []]
    else:
        sent = [{"label": f.label, "value": f.value, "kind": f.kind, "required": f.required,
                 "source": _source(app, f.key)} for f in await enumerate_fields(page, hints)]
    resume = ctx.resumes.path(app["resume_file"]) if app.get("resume_file") else None
    generated = [e["value"] for e in app.get("fill_plan") or [] if e.get("source") == "cover_letter"]
    det = led.details(posting_key) or {}

    if ctx.never_submit:
        write_record(sub, posting, sent, resume, generated, det.get("description_text", ""),
                     led.score(posting_key), {"mode": "test (never_submit) - NOT submitted"})
        led.upsert_application(posting_key, submission_dir=str(sub))
        led.set_state(posting_key, WOULD_SUBMIT, "never_submit")
        _keep_or_close(ctx, posting_key, page)
        return {"state": WOULD_SUBMIT, "detail": f"test mode: saved to {sub}"}

    if not hints.get("workday"):
        write_record(sub, posting, sent, resume, generated, det.get("description_text", ""),
                     led.score(posting_key), {"mode": "Submit clicked - waiting for the board's confirmation"})
        led.upsert_application(posting_key, submission_dir=str(sub))
    before = await _headings(page)
    try:
        await click_submit(ctx, page, hints)
        if hints.get("workday"):
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            from .workday import banner_errors, wait_submitted
            if not await wait_submitted(page):
                why = await banner_errors(page)
                raise Stuck("submit_failed", f"Workday did not confirm the submission{': ' + why if why else ''}")
        else:
            outcome, info = await after_click(page, ref.ats, hints, before)
            if outcome == "code":
                return await _needs_code(ctx, posting_key, page, sub, info)
            if outcome == "error":
                raise Stuck("submit_failed", info)
            if outcome == "unconfirmed":
                raise Stuck("submit_unconfirmed", "Submit was clicked but no confirmation appeared. Check the "
                                                  "open tab: if it went through, click I submitted it")
    except Stuck as e:
        await safe_screenshot(page, sub / "after_submit.png")
        _stuck(ctx, posting_key, e, page)
        return {"state": STUCK, "detail": str(e)}
    return await _record_submitted(ctx, posting_key, page, sub, sent)


async def _record_submitted(ctx, posting_key: str, page, sub, sent: list[dict]) -> dict:
    """The application is sent: nothing from here on may fail before it is recorded."""
    led = ctx.ledger
    app = led.application(posting_key) or {}
    posting = dict(led.get(posting_key).data)
    resume = ctx.resumes.path(app["resume_file"]) if app.get("resume_file") else None
    generated = [e["value"] for e in app.get("fill_plan") or [] if e.get("source") == "cover_letter"]
    det = led.details(posting_key) or {}
    await safe_screenshot(page, sub / "after_submit.png")
    write_record(sub, posting, sent, resume, generated, det.get("description_text", ""),
                 led.score(posting_key), {"final_url": page.url})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    led.upsert_application(posting_key, submission_dir=str(sub), submitted_at=now)
    led.set_state(posting_key, SUBMITTED)
    if ctx.writeback is not None:
        ctx.writeback.queue(posting_key, applied_change())
    ctx.take_tab(posting_key)
    await page.close()
    return {"state": SUBMITTED, "detail": str(sub)}


async def _needs_code(ctx, posting_key: str, page, sub, info: str) -> dict:
    """The board emailed a code: keep the tab exactly as it is and ask for the code."""
    await safe_screenshot(page, sub / "code_requested.png")
    reason = f"email_code: {info}. Type the code from that email on this card"
    _stuck(ctx, posting_key, Stuck("email_code", reason), page)
    row = ctx.ledger.get(posting_key)
    ctx.notifier.warn(f"{row.get('company')} - {row.get('title')}: the application needs the security code "
                      "that was just emailed to you. Enter it on the Stuck card in the review page.")
    return {"state": STUCK, "detail": "the board emailed you a security code - enter it on the Stuck card"}


async def enter_code(ctx, posting_key: str, code: str) -> dict:
    """Type the emailed security code into the kept tab, click Submit again, and record it
    once the board confirms."""
    led = ctx.ledger
    if not ctx.has_live_tab(posting_key):
        return {"state": STUCK, "detail": "the tab with this form is gone. Click Open form, submit it there "
                                          "with a new code, then click I submitted it"}
    page = ctx.open_tabs[posting_key]
    hints = form_hints(parse_key(posting_key).ats)
    code = re.sub(r"\s+", "", code or "")
    boxes = page.locator(CODE_BOX)
    n = await boxes.count()
    if not n:
        return {"state": STUCK, "detail": "the form is not asking for a code (any more). Check the tab"}
    if len(code) != n:
        return {"state": STUCK, "detail": f"the code has {n} characters; you typed {len(code)}"}
    for i, ch in enumerate(code):
        box = page.locator(f"#security-input-{i}")
        await box.fill("")
        await box.type(ch, delay=30)
    before = await _headings(page)
    await click_submit(ctx, page, hints)
    outcome, info = await after_click(page, parse_key(posting_key).ats, hints, before)
    if outcome == "code":
        err = page.locator(CODE_ERROR)
        why = " ".join((await err.first.inner_text()).split()) if await err.count() else "the code was not accepted"
        return {"state": STUCK, "detail": f"{why}. Check the email and try again"}
    if outcome == "confirmed":
        app = led.application(posting_key) or {}
        sub = Path(app["submission_dir"]) if app.get("submission_dir") else submission_dir(
            ctx.paths.submissions, dict(led.get(posting_key).data))
        sent_file = sub / "fields.json"
        sent = json.loads(sent_file.read_text(encoding="utf-8")) if sent_file.exists() else []
        return await _record_submitted(ctx, posting_key, page, sub, sent)
    detail = info or "no confirmation appeared; check the tab, and click I submitted it if it went through"
    led.upsert_application(posting_key, stuck_reason=f"submit_unconfirmed: {detail}")
    return {"state": STUCK, "detail": detail}


async def mark_manual(ctx, bs, posting_key: str) -> dict:
    """'I submitted it' on a Stuck card: record it with a screenshot of the current page."""
    led = ctx.ledger
    row = led.get(posting_key)
    app = led.application(posting_key) or {}
    posting = dict(row.data)
    sub = submission_dir(ctx.paths.submissions, posting)
    page, _ = ctx.take_tab(posting_key)
    if page is not None:
        await safe_screenshot(page, sub / "after_submit.png")
    det = led.details(posting_key) or {}
    resume = ctx.resumes.path(app["resume_file"]) if app.get("resume_file") else None
    write_record(sub, posting, app.get("fill_plan") or [], resume, [], det.get("description_text", ""),
                 led.score(posting_key), {"mode": "submitted by hand"})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    led.upsert_application(posting_key, submission_dir=str(sub), submitted_at=now)
    led.set_state(posting_key, SUBMITTED_MANUAL)
    if ctx.writeback is not None:
        ctx.writeback.queue(posting_key, applied_change())
    return {"state": SUBMITTED_MANUAL, "detail": str(sub)}


async def type_into_tab(ctx, posting_key: str, entries: list[dict]) -> bool:
    """Put review-page answers into the application's kept tab, if it has one.

    False when there is no live tab, or the tab did not take an answer - then the
    tab is closed and approval refills the form from the plan instead.
    """
    from .replay import apply_plan
    if not entries or not ctx.has_live_tab(posting_key):
        return False
    page = ctx.open_tabs[posting_key]
    problems = await apply_plan(ctx, page, entries, form_hints(parse_key(posting_key).ats))
    if problems:
        log.info("answers did not go into the tab for %s (%s); it will be refilled on approval",
                 posting_key, "; ".join(problems))
        page, _ = ctx.take_tab(posting_key)
        if page is not None:
            await page.close()
        return False
    return True


async def read_tab_answers(ctx, posting_key: str) -> tuple[dict[str, str], set[str]] | None:
    """What the user typed into the open tab for the application's missing questions.

    Returns (answers by missing key, upload fields now attached), or None when
    there is no open tab.
    """
    from .replay import _match
    if not ctx.has_live_tab(posting_key):
        return None
    app = ctx.ledger.application(posting_key) or {}
    fields = await enumerate_fields(ctx.open_tabs[posting_key], form_hints(parse_key(posting_key).ats))
    answers, attached = {}, set()
    for m in app.get("missing") or []:
        f = _match(fields, m)
        if f is None or f.empty:
            continue
        if m.get("kind") == "file":
            attached.add(m["key"])
        else:
            answers[m["key"]] = f.value
    return answers, attached


def _source(app: dict, key: str) -> str:
    for e in app.get("fill_plan") or []:
        if e["key"] == key:
            return e["source"]
    return "page"


def _stuck(ctx, key: str, err: Stuck, page) -> None:
    led = ctx.ledger
    led.upsert_application(key, stuck_reason=str(err), lane=STUCK)
    if led.get(key).state != STUCK:
        led.set_state(key, STUCK, str(err)[:300])
    if page is not None:
        _keep_or_close(ctx, key, page)


def _keep_or_close(ctx, key: str, page) -> None:
    # in a review session the tab stays open so "Open form" can bring it to the front
    ctx.remember_tab(key, page)


def dump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False)
