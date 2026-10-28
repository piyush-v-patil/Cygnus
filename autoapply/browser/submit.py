"""Submit an approved application (§8.8). The only module that clicks Submit."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import ats
from ..ats import form_hints, parse_key
from ..ledger import (CLOSED, NEEDS_INPUT, READY, STUCK, SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT)
from ..records import submission_dir, write_record
from ..writeback import applied_change
from .fields import enumerate_fields, safe_screenshot
from .fill import AlreadyApplied, Stuck, detect_blockers
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
# other boards (Ashby and the like): a one-time-code input next to "verification code"
# wording. Not seen live yet - kept loose: any visible code-like input (not a postal, phone
# or promo code) on a page that says a code was sent or must be entered
OTP_JS = """() => {
  const shown = (e) => e.offsetParent !== null;
  const words = (e) => [e.getAttribute('aria-label'), e.placeholder, e.name, e.id,
                        e.labels && e.labels[0] && e.labels[0].innerText].join(' ');
  const text = document.body ? document.body.innerText : '';
  const ask = /(verification|security|confirmation|one[- ]time|access)\\s+code|enter (the|your) code|code (we|was|has been) (sent|emailed)/i;
  if (!ask.test(text)) return [];
  const inputs = [...document.querySelectorAll('input')].filter(shown)
    .filter((e) => !['hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'email', 'password'].includes(e.type))
    .filter((e) => e.autocomplete === 'one-time-code' || /code|otp|\\bpin\\b/i.test(words(e)))
    .filter((e) => !/postal|zip|country|area|promo|discount|referral|coupon|phone/i.test(words(e)));
  inputs.forEach((e, i) => e.setAttribute('data-aa-otp', String(i)));
  const m = text.match(new RegExp('[^\\n.]{0,80}(' + ask.source + ')[^\\n.]{0,80}', 'i'));
  return inputs.length ? [m ? m[0].trim() : 'a code was emailed to you', inputs.map((e) => e.maxLength > 0 ? e.maxLength : 0)] : [];
}"""
CODE_SUBMIT_JS = """() => {
  // the button that goes with the code input: Verify / Confirm / Continue / Submit near it
  let el = document.querySelector('[data-aa-otp="0"]');
  for (let i = 0; el && i < 6; i++, el = el.parentElement) {
    const b = [...el.querySelectorAll('button, [role=button], input[type=submit]')]
      .find((x) => x.offsetParent !== null && /verify|confirm|continue|submit/i.test(x.innerText || x.value || ''));
    if (b) { b.click(); return true; }
  }
  return false;
}"""


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
    """The board's "enter the code we emailed" step (Greenhouse's boxes, or a generic
    one-time-code input): its wording, or None."""
    boxes = page.locator(CODE_BOX)
    try:
        if await boxes.count() and await boxes.first.is_visible():
            t = page.locator(CODE_TEXT)
            return " ".join((await t.first.inner_text()).split()) if await t.count() else "a security code was emailed to you"
        found = await page.evaluate(OTP_JS)
        return " ".join(found[0].split())[:200] if found else None
    except Exception:  # noqa: BLE001
        return None


async def code_fields(page) -> tuple[list, int | None]:
    """(inputs to type the code into, the code's length if the page tells it)."""
    boxes = page.locator(CODE_BOX)
    n = await boxes.count()
    if n:
        return [page.locator(f"#security-input-{i}") for i in range(n)], n
    found = await page.evaluate(OTP_JS)
    if not found:
        return [], None
    sizes = found[1]
    inputs = [page.locator(f'[data-aa-otp="{i}"]') for i in range(len(sizes))]
    if len(inputs) > 1:
        return inputs, len(inputs)
    return inputs, sizes[0] or None


async def _code_error(page) -> str:
    err = page.locator(CODE_ERROR)
    if await err.count() and (await err.first.inner_text()).strip():
        return " ".join((await err.first.inner_text()).split())
    try:
        return (await page.evaluate("""() => { const e = [...document.querySelectorAll('[role=alert], [class*=error]')]
            .find((x) => x.offsetParent !== null && x.innerText.trim()); return e ? e.innerText.trim() : ''; }""")
                or "")[:200] or "the code was not accepted"
    except Exception:  # noqa: BLE001
        return "the code was not accepted"


SUBMIT_WAIT_S = 30.0


async def after_click(page, ats_name: str, hints: dict, before: str, timeout_s: float | None = None,
                      before_problem: str | None = None, code_grace_s: float = 0,
                      code_error_before: str | None = None) -> tuple[str, str]:
    """What a Submit click led to: ("confirmed" | "code" | "error" | "unconfirmed", detail).

    Only the board's own confirmation counts as sent; a page that merely lost its form (a
    network error, a reload) is "unconfirmed". An error already on the page before the
    click (`before_problem`, e.g. a cookie banner) is not the click's error. After a code was
    typed, the code page staying up counts only after `code_grace_s` or with an error shown."""
    import asyncio
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + (SUBMIT_WAIT_S if timeout_s is None else timeout_s)
    css = SUCCESS_CSS.get(ats_name)
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if page.is_closed():
            return "unconfirmed", "the tab closed"
        code = await _code_prompt(page)
        if code and (loop.time() - start >= code_grace_s
                     or await _code_error(page) not in ("the code was not accepted", code_error_before)):
            return "code", code
        if css and await page.locator(css).count():
            return "confirmed", "confirmation shown"
        if SUCCESS_URL.search(page.url or ""):
            return "confirmed", page.url
        now = await _headings(page)
        if SUCCESS_TEXT.search(now) and not SUCCESS_TEXT.search(before):
            return "confirmed", SUCCESS_TEXT.search(now).group(0)
        problem = await _form_problems_after_submit(page, hints)
        if problem and problem != before_problem:
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
    return "unconfirmed", ""


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
        return await _workday_ready_page(ctx, bs, app, page, age)
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


async def _workday_ready_page(ctx, bs, app: dict, page, age: float | None = None):
    """A Workday tab on Review: the kept tab (walked on to Review if it stopped earlier), else
    the application reopened from the account's draft and walked again. A tab older than
    browser.tab_max_age_min is reopened too: Workday's sign-in may have run out."""
    from . import workday
    key = app["posting_key"]
    if page is not None and not page.is_closed() and age is not None and age > ctx.profile.browser.tab_max_age_min * 60:
        log.info("Workday tab for %s is %d min old; reopening it", key, age // 60)
        await page.close()
    if page is not None and not page.is_closed():
        try:
            st = await workday.state(page)
            if st == "review":
                return page                       # the page the user could look at: submit it
            if st == "form" and await workday.resume(ctx, page, key):
                raise workday.ReviewFirst(page)
            if st == "form":
                a = ctx.ledger.application(key) or {}
                raise workday.NeedsInputError(a.get("missing") or [], a.get("fill_plan") or [])
        except (workday.NeedsInputError, workday.ReviewFirst) as e:
            e.page = page
            raise
        except Exception as e:  # noqa: BLE001 - a dead tab just means reopening
            log.info("kept Workday tab for %s not usable (%s); reopening", key, e)
        if not page.is_closed():
            await page.close()
    raise workday.ReviewFirst(await replay(ctx, bs, app))


async def shows_confirmation(page, ats_name: str, hints: dict) -> bool:
    """The tab is on the board's "application received" page (sent from it already)."""
    if page is None or page.is_closed():
        return False
    try:
        if hints.get("workday"):
            from .workday import state
            return await state(page) == "submitted"
        css = SUCCESS_CSS.get(ats_name)
        if css and await page.locator(css).count():
            return True
        if SUCCESS_URL.search(page.url or ""):
            return True
        if await page.locator(hints.get("submit") or GENERIC_SUBMIT).count():
            return False
        return bool(SUCCESS_TEXT.search(await _headings(page)))
    except Exception:  # noqa: BLE001 - a navigating tab is not a confirmation
        return False


async def submit(ctx, bs, posting_key: str, force: bool = False) -> dict:
    """Approve & submit. Returns {"state": ..., "detail": ...}.

    Submit is never clicked twice for an application on its own: once clicked, a resend
    needs `force` (the user said it did not go out)."""
    led = ctx.ledger
    row = led.get(posting_key)
    app = led.application(posting_key)
    if row is None or app is None:
        return {"state": "missing", "detail": "no such application"}
    if row.state == NEEDS_INPUT and not app.get("missing"):
        led.set_state(posting_key, READY, None, "all questions answered")
        row = led.get(posting_key)
    if force and row.state == STUCK and led.submit_clicked(posting_key):
        led.set_state(posting_key, READY, None, "you said it was not sent; submitting again")
        row = led.get(posting_key)
    if row.state != READY:
        return {"state": row.state, "detail": f"cannot submit from {row.state}"}
    ref = parse_key(posting_key)
    hints = form_hints(ref.ats)

    # sent from the kept tab already (by you, or by an earlier click): record it, never resend
    tab = ctx.open_tabs.get(posting_key) if ctx.has_live_tab(posting_key) else None
    if tab is not None and await shows_confirmation(tab, ref.ats, hints):
        if led.submit_clicked(posting_key):
            sub = Path(app["submission_dir"]) if app.get("submission_dir") else submission_dir(
                ctx.paths.submissions, dict(row.data))
            sent_file = sub / "fields.json"
            sent = json.loads(sent_file.read_text(encoding="utf-8")) if sent_file.exists() else []
            ctx.take_tab(posting_key)
            return await _record_submitted(ctx, posting_key, tab, sub, sent)
        await mark_manual(ctx, bs, posting_key)
        return {"state": SUBMITTED_MANUAL, "detail": "the open tab already shows the confirmation, "
                                                     "so it was recorded as sent by you"}
    clicked = led.submit_clicked(posting_key)
    if clicked and not force:
        e = Stuck("submit_clicked_before", f"Submit was already clicked for this application at {clicked}. "
                  "Check your email or the site: if it went through, click I submitted it; if not, "
                  "click Submit it again")
        _stuck(ctx, posting_key, e, tab)
        return {"state": STUCK, "detail": str(e)}

    if _waited_too_long(app) and ref.supported and getattr(ctx, "form_url_for", None) is None:
        try:
            det = ats.clients()[ref.ats].fetch(ref)
            if det.closed:
                led.set_state(posting_key, CLOSED, "closed_before_submit")
                return {"state": CLOSED, "detail": "the posting closed while waiting"}
        except ats.FetchError as e:
            log.warning("could not recheck %s: %s", posting_key, e)

    from .workday import NeedsInputError, ReviewFirst
    try:
        page = await get_ready_page(ctx, bs, app)
    except ReviewFirst as e:
        # never submit pages the user has not had a chance to see
        ctx.remember_tab(posting_key, e.page)
        from .launch import show_window
        await show_window(e.page)
        return {"state": READY, "detail": "Workday's Review page is open in the tool's browser with your "
                                          "answers. Check it, then click Approve & submit again"}
    except AlreadyApplied as e:
        return await record_already_applied(ctx, posting_key, e, getattr(e, "page", None))
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

    write_record(sub, posting, sent, resume, generated, det.get("description_text", ""),
                 led.score(posting_key), {"mode": "Submit clicked - waiting for the board's confirmation"})
    led.upsert_application(posting_key, submission_dir=str(sub))
    before = await _headings(page)
    before_problem = None if hints.get("workday") else await _form_problems_after_submit(page, hints)
    try:
        # recorded before the click: if anything stops between here and the confirmation,
        # the application is never sent again without you saying so
        clicked = datetime.now(timezone.utc)
        led.upsert_application(posting_key, submit_clicked_at=clicked.strftime("%Y-%m-%dT%H:%M:%SZ"))
        await click_submit(ctx, page, hints)
        if hints.get("workday"):
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            from .workday import banner_errors, wait_submitted
            if not await wait_submitted(page):
                why = await banner_errors(page)
                if why:
                    raise Stuck("submit_failed", f"Workday did not take the submission: {why}")
                raise Stuck("submit_unconfirmed", "Submit was clicked but Workday showed no confirmation. Check "
                                                  "the open tab: if it went through, click I submitted it")
        else:
            outcome, info = await after_click(page, ref.ats, hints, before, before_problem=before_problem)
            if outcome == "code":
                return await code_from_mail(ctx, posting_key, page, sub, info, clicked)
            if outcome == "error":
                raise Stuck("submit_failed", info)
            if outcome == "unconfirmed":
                raise Stuck("submit_unconfirmed", "Submit was clicked but no confirmation appeared. Check the "
                                                  "open tab: if it went through, click I submitted it")
    except Exception as e:  # noqa: BLE001 - after the click nothing may pass for "not sent"
        if not isinstance(e, Stuck):
            log.error("submit %s: %s after clicking Submit", posting_key, type(e).__name__, exc_info=e)
            e = Stuck("submit_unconfirmed", f"Submit was clicked, then the page failed ({type(e).__name__}). "
                                            "Check the open tab: if it went through, click I submitted it")
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


async def find_code(ctx, posting_key: str, page, since: datetime, timeout_s: float | None = None) -> str | None:
    """The code from the board's email, read from your mailbox (None: mail is off or no email came)."""
    import asyncio
    from ..mailbox import MailError, security_code
    box = ctx.mailbox
    if box is None:
        return None
    _, length = await code_fields(page)
    company = ctx.ledger.get(posting_key).get("company") or ""
    wait = ctx.profile.mail.wait_s if timeout_s is None else timeout_s
    log.info("    waiting up to %ds for the security code email", int(wait))
    try:
        return await asyncio.to_thread(box.wait_for, lambda m: security_code(m, company, length), since, wait,
                                       "the security code email")
    except MailError as e:
        log.warning("    could not read the mailbox: %s", e)
        return None


async def code_from_mail(ctx, posting_key: str, page, sub, info: str, since: datetime) -> dict:
    """After Approve: the board emailed a code. With mail on, read it, type it and finish;
    otherwise (or when that fails) the card waits in Stuck with a box for the code."""
    if ctx.mailbox is None:
        return await _needs_code(ctx, posting_key, page, sub, info)
    code = await find_code(ctx, posting_key, page, since)
    if not code:
        return await _needs_code(ctx, posting_key, page, sub, f"{info} (no code email arrived in "
                                 f"{int(ctx.profile.mail.wait_s)}s)")
    ctx.remember_tab(posting_key, page)
    res = await enter_code(ctx, posting_key, code)
    if res["state"] == SUBMITTED:
        return res
    if ctx.ledger.get(posting_key).state != STUCK:
        if await _code_prompt(page):
            return await _needs_code(ctx, posting_key, page, sub, f"{info} (the code from the email was not "
                                     f"accepted: {res['detail']})")
        _stuck(ctx, posting_key, Stuck("submit_unconfirmed", res["detail"]), page)
    return {"state": STUCK, "detail": res["detail"]}


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
    inputs, length = await code_fields(page)
    if not inputs:
        return {"state": STUCK, "detail": "the form is not asking for a code (any more). Check the tab"}
    if length and len(code) != length:
        return {"state": STUCK, "detail": f"the code has {length} characters; you typed {len(code)}"}
    if len(inputs) > 1:                        # one box per character
        for box, ch in zip(inputs, code):
            await box.fill("")
            await box.type(ch, delay=30)
    else:
        await inputs[0].fill("")
        await inputs[0].type(code, delay=30)
    before = await _headings(page)
    old_error = await _code_error(page)                     # from an earlier wrong code: not this code's
    greenhouse_boxes = await page.locator(CODE_BOX).count()
    before_problem = await _form_problems_after_submit(page, hints) if greenhouse_boxes else None
    if greenhouse_boxes or not await page.evaluate(CODE_SUBMIT_JS):
        if greenhouse_boxes:
            await click_submit(ctx, page, hints)
        else:
            await inputs[-1].press("Enter")
    outcome, info = await after_click(page, parse_key(posting_key).ats, hints, before, before_problem=before_problem,
                                      code_grace_s=10, code_error_before=old_error)
    if outcome == "code":
        return {"state": STUCK, "detail": f"{await _code_error(page)}. Check the email and try again"}
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


def applied_day(when: str) -> date | None:
    """"Oct 14, 2026" (Simplify's card) as a date; None when it does not read as one."""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            day = datetime.strptime((when or "").strip(), fmt).date()
        except ValueError:
            continue
        return min(day, date.today())
    return None


async def record_already_applied(ctx, posting_key: str, err, page=None) -> dict:
    """The form says you applied to this job before: recorded as submitted by you (on the
    day it shows), written back to job-monitor, and never filled again."""
    led = ctx.ledger
    row = led.get(posting_key)
    app = led.application(posting_key) or {}
    posting = dict(row.data)
    sub = submission_dir(ctx.paths.submissions, posting)
    if page is not None and not page.is_closed():
        await safe_screenshot(page, sub / "already_applied.png")
    det = led.details(posting_key) or {}
    write_record(sub, posting, app.get("fill_plan") or [], None, [], det.get("description_text", ""),
                 led.score(posting_key), {"mode": "already applied", "detail": str(err)})
    day = applied_day(getattr(err, "when", ""))
    at = f"{day.isoformat()}T12:00:00Z" if day else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    led.upsert_application(posting_key, submission_dir=str(sub), submitted_at=at, stuck_reason=None)
    if led.get(posting_key).state != SUBMITTED_MANUAL:
        led.set_state(posting_key, SUBMITTED_MANUAL, str(err)[:300])
    if ctx.writeback is not None:
        ctx.writeback.queue(posting_key, applied_change(day))
    kept, _ = ctx.take_tab(posting_key)               # nothing left to do in its tab
    for p in {kept, page} - {None}:
        if not p.is_closed():
            await p.close()
    return {"state": SUBMITTED_MANUAL, "detail": f"{err}; recorded as submitted, not filled again"}


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
