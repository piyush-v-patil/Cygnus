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
from .fields import enumerate_fields
from .fill import Stuck, detect_blockers
from .replay import replay, verify_plan

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


async def get_ready_page(ctx, bs, app: dict):
    """The open, still-correct tab if there is one, otherwise a replayed one."""
    key = app["posting_key"]
    hints = form_hints(parse_key(key).ats)
    page = ctx.open_tabs.pop(key, None)
    if page is not None and not page.is_closed():
        try:
            if not await verify_plan(page, app.get("fill_plan") or [], hints):
                return page
        except Exception:  # noqa: BLE001 - a dead tab just means replay
            pass
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
    await page.screenshot(path=str(sub / "form.png"), full_page=True)
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
        await page.screenshot(path=str(sub / "after_submit.png"), full_page=True)
        _stuck(ctx, posting_key, e, page)
        return {"state": STUCK, "detail": str(e)}

    await page.screenshot(path=str(sub / "after_submit.png"), full_page=True)
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
    page = ctx.open_tabs.pop(posting_key, None)
    if page is not None and not page.is_closed():
        try:
            await page.screenshot(path=str(sub / "after_submit.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
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
    ctx.open_tabs[key] = page


def dump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False)
