"""Submit an approved application (§8.8). The only module that clicks Submit."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

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

    try:
        page = await get_ready_page(ctx, bs, app)
    except Stuck as e:
        _stuck(ctx, posting_key, e, getattr(e, "page", None))
        return {"state": STUCK, "detail": str(e)}

    posting = dict(row.data)
    sub = submission_dir(ctx.paths.submissions, posting)
    await safe_screenshot(page, sub / "form.png")
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

    try:
        await click_submit(ctx, page, hints)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(2000)
        problem = await _form_problems_after_submit(page, hints)
        if problem:
            raise Stuck("submit_failed", problem)
    except Stuck as e:
        await safe_screenshot(page, sub / "after_submit.png")
        _stuck(ctx, posting_key, e, page)
        return {"state": STUCK, "detail": str(e)}

    # the application is sent: nothing from here on may fail before it is recorded
    await safe_screenshot(page, sub / "after_submit.png")
    write_record(sub, posting, sent, resume, generated, det.get("description_text", ""),
                 led.score(posting_key), {"final_url": page.url})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    led.upsert_application(posting_key, submission_dir=str(sub), submitted_at=now)
    led.set_state(posting_key, SUBMITTED)
    if ctx.writeback is not None:
        ctx.writeback.queue(posting_key, applied_change())
    await page.close()
    return {"state": SUBMITTED, "detail": str(sub)}


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
