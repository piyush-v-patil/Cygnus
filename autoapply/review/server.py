"""Local review web app (§10), bound to 127.0.0.1 and driving the tool's browser.

One asyncio loop runs both uvicorn and Playwright, so "Approve & submit"
replays the form in the visible browser and clicks Submit there.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..ledger import LANES, NEEDS_INPUT, READY, STUCK, SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT
from . import actions

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LANE_TITLES = {READY: "Ready", NEEDS_INPUT: "Needs input", STUCK: "Stuck"}
SOURCE_LABEL = {"simplify": "Simplify", "answer_bank": "Saved answer", "claude": "Claude", "user": "You",
                "profile": "Your visa answers", "workday": "Workday account",
                "resume": "Resume", "cover_letter": "Claude (cover letter)"}


def _ago(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    s = (datetime.now(timezone.utc) - t).total_seconds()
    if s < 3600:
        return f"{int(s // 60)} min"
    if s < 86400:
        return f"{int(s // 3600)} h"
    return f"{int(s // 86400)} d"


class Session:
    """Browser + bookkeeping for one review session (shared with the fill in `run --review`)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.bs = None
        self.lock = asyncio.Lock()
        self.submitted = 0
        self.manual = 0
        self.csrf = secrets.token_urlsafe(24)

    async def browser(self):
        if self.bs is None:
            from ..browser.launch import BrowserSession
            self.bs = await BrowserSession(self.ctx).__aenter__()
        return self.bs

    async def close(self):
        if self.bs is not None:
            await self.bs.__aexit__(None, None, None)
            self.bs = None


def create_app(ctx, session: Session | None = None, port: int = 8765) -> FastAPI:
    ctx.keep_tabs = True
    sess = session or Session(ctx)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session = sess
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return HTMLResponse("Forbidden host", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in allowed_hosts:
                return HTMLResponse("Forbidden origin", status_code=403)
        return await call_next(request)

    def check_csrf(token: str):
        if not secrets.compare_digest(token or "", sess.csrf):
            raise HTTPException(403, "stale page - reload and try again")

    def view(app_row: dict) -> dict:
        led = ctx.ledger
        key = app_row["posting_key"]
        p = led.get(key)
        sc = led.score(key) or {}
        return {"app": app_row, "p": p.data if p else {}, "state": p.state if p else "",
                "live": ctx.has_live_tab(key),
                "score": sc, "waited": _ago(app_row.get("updated_at")),
                "plan": app_row.get("fill_plan") or [], "missing": app_row.get("missing") or []}

    def page(request: Request, name: str, **kw):
        led = ctx.ledger
        counts = {lane: len(led.applications_in_lane(lane)) for lane in LANES}
        return TEMPLATES.TemplateResponse(request, name, {
            "counts": counts, "lane_titles": LANE_TITLES, "spend": led.month_spend(),
            "budget": ctx.profile.llm.monthly_budget_usd, "csrf": sess.csrf, "source_label": SOURCE_LABEL,
            "test_mode": ctx.never_submit, "flash": request.query_params.get("msg"),
            "live_tabs": len(ctx.live_tabs()), **kw})

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return RedirectResponse("/lane/ready", 303)

    @app.get("/lane/{lane}", response_class=HTMLResponse)
    async def lane_view(request: Request, lane: str):
        if lane not in LANES:
            raise HTTPException(404)
        cards = [view(a) for a in ctx.ledger.applications_in_lane(lane)]
        return page(request, "lane.html", lane=lane, cards=cards)

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request, q: str = ""):
        cards = [view(a) for a in ctx.ledger.submitted_applications(q.strip())]
        return page(request, "history.html", cards=cards, q=q, lane="history")

    @app.get("/app/{app_id}/screenshot")
    async def screenshot(app_id: int):
        a = ctx.ledger.application(app_id=app_id)
        if not a or not a.get("screenshot_path") or not Path(a["screenshot_path"]).exists():
            raise HTTPException(404)
        return FileResponse(a["screenshot_path"])

    @app.get("/app/{app_id}/file/{name}")
    async def saved_file(app_id: int, name: str):
        a = ctx.ledger.application(app_id=app_id)
        if not a or not a.get("submission_dir"):
            raise HTTPException(404)
        base = Path(a["submission_dir"]).resolve()
        f = (base / name).resolve()
        if base not in f.parents or not f.exists():
            raise HTTPException(404)
        return FileResponse(f)

    def _back(lane: str, msg: str, app_id: int | None = None) -> RedirectResponse:
        from urllib.parse import quote
        anchor = f"#app{app_id}" if app_id is not None else ""
        return RedirectResponse(f"/lane/{lane}?msg={quote(msg)}{anchor}", 303)

    def _lane_of(key: str) -> str:
        state = ctx.ledger.get(key).state
        return state if state in LANES else NEEDS_INPUT

    async def _show(key: str) -> str:
        """Bring the application's tab to the front, or reopen + refill it. Returns a note."""
        from ..browser.fill import Stuck
        from ..browser.replay import replay
        from ..browser.workday import NeedsInputError
        async with sess.lock:
            bs = await sess.browser()
            tab = ctx.open_tabs.get(key) if ctx.has_live_tab(key) else None
            note = "The form is in front in the tool's browser."
            if tab is None or tab.is_closed():
                try:
                    tab = await replay(ctx, bs, ctx.ledger.application(key))
                    note = "Reopened and refilled the form in the tool's browser."
                except NeedsInputError as e:
                    tab = getattr(e, "page", None)
                    led = ctx.ledger
                    led.upsert_application(key, missing=e.missing, fill_plan=e.plan, lane=NEEDS_INPUT)
                    if led.get(key).state != NEEDS_INPUT:
                        led.set_state(key, NEEDS_INPUT, None, "Workday asked more questions")
                    note = f"Reopened; Workday asks {len(e.missing)} more question(s) - answer them here."
                except Stuck as e:
                    tab = getattr(e, "page", None)
                    note = f"Reopened the form; finish it by hand ({e})."
                except Exception as e:  # noqa: BLE001
                    tab = getattr(e, "page", None)
                    note = f"Reopened the form; finish it by hand ({type(e).__name__})."
                if tab is not None:
                    ctx.remember_tab(key, tab)
            if tab is not None:
                await tab.bring_to_front()
        return note

    async def _workday_on(key: str) -> str:
        """A Workday application stops on the page with questions; once they are answered
        the walk goes on from there in the same tab, to Review or to the next questions."""
        from ..browser import workday
        from ..browser.fill import Stuck
        from ..browser.submit import _stuck
        async with sess.lock:
            page = ctx.open_tabs[key]
            try:
                if await workday.resume(ctx, page, key):
                    return "Workday: went on to the Review page."
                n = len((ctx.ledger.application(key) or {}).get("missing") or [])
                return f"Workday asked {n} more question(s) on a later page; answer them here."
            except Stuck as e:
                _stuck(ctx, key, e, page)
                return f"Workday stopped: {e}"

    async def _after_answers(key: str, res: dict, show: bool) -> str:
        """Type new answers into open tabs, optionally show this form; the flash message."""
        msg = f"Saved. {res['still_missing']} question(s) left." if res["still_missing"] else "Saved."
        added = {k: v for k, v in (res.get("added") or {}).items() if v}
        if added and ctx.live_tabs():
            from ..browser.submit import type_into_tab
            async with sess.lock:
                typed = [k for k, entries in added.items() if await type_into_tab(ctx, k, entries)]
            if typed:
                msg += f" Typed into {len(typed)} open tab(s)."
        others = [k for k in res["ready"] if k != key]
        if others:
            msg += f" {len(others)} other application(s) moved to Ready."
        if key.startswith("workday:") and ctx.ledger.get(key).state == READY and ctx.has_live_tab(key):
            msg += " " + await _workday_on(key)
        if show:
            msg += " " + await _show(key)
        if ctx.ledger.get(key).state == READY:
            msg += " Check every field in the form, then click Approve & submit here."
        return msg

    def _key(app_id: int) -> str:
        a = ctx.ledger.application(app_id=app_id)
        if a is None:
            raise HTTPException(404)
        return a["posting_key"]

    async def _submit(key: str) -> str:
        from ..browser.submit import submit
        async with sess.lock:
            bs = await sess.browser()
            res = await submit(ctx, bs, key)
        if res["state"] == SUBMITTED:
            sess.submitted += 1
        p = ctx.ledger.get(key)
        return f"{p.get('company')} — {p.get('title')}: {res['state']} ({res['detail']})"

    @app.post("/app/{app_id}/approve")
    async def approve(app_id: int, csrf: str = Form(""), lane: str = Form(READY)):
        check_csrf(csrf)
        return _back(lane, await _submit(_key(app_id)))

    @app.post("/approve-all")
    async def approve_all(csrf: str = Form(""), ids: str = Form("")):
        check_csrf(csrf)
        msgs = []
        for i in [int(x) for x in ids.split(",") if x.strip().isdigit()]:
            key = _key(i)
            if ctx.ledger.get(key).state == READY:     # each still submits on its own
                msgs.append(await _submit(key))
        return _back(READY, f"{len(msgs)} processed: " + " | ".join(msgs))

    @app.post("/app/{app_id}/reject")
    async def reject(app_id: int, csrf: str = Form(""), lane: str = Form(READY)):
        check_csrf(csrf)
        key = _key(app_id)
        actions.reject(ctx, key)
        tab, _ = ctx.take_tab(key)
        if tab is not None:
            await tab.close()
        return _back(lane, "Rejected.")

    @app.post("/app/{app_id}/answers")
    async def answers(request: Request, app_id: int):
        form = await request.form()
        check_csrf(form.get("csrf", ""))
        key = _key(app_id)
        vals: dict[str, str] = {}
        for k in form.keys():
            if k.startswith("a__"):
                many = form.getlist(k)
                vals[k[3:]] = "; ".join(v for v in many if v) if len(many) > 1 else (many[0] or "")
        res = actions.save_answers(ctx.ledger, ctx.answers, key, vals)
        # never submits from here: answering leads to the real form, approval happens after checking it
        msg = await _after_answers(key, res, show=form.get("then") in ("show", "submit"))
        return _back(_lane_of(key), msg, app_id)

    @app.post("/app/{app_id}/from-form")
    async def from_form(app_id: int, csrf: str = Form("")):
        """The user answered the questions in the form itself: read them back from the tab."""
        check_csrf(csrf)
        key = _key(app_id)
        from ..browser.submit import read_tab_answers
        async with sess.lock:
            got = await read_tab_answers(ctx, key)
        if got is None:
            return _back(NEEDS_INPUT, "That form is not open. Click Open form, answer the questions there, "
                                      "then click “I answered in the form” again.", app_id)
        vals, attached = got
        if not vals and not attached:
            return _back(NEEDS_INPUT, "None of the questions listed here are answered in the form yet.", app_id)
        res = actions.save_answers(ctx.ledger, ctx.answers, key, vals, attached=attached)
        res["added"] = {k: v for k, v in res["added"].items() if k != key}   # already in this tab
        msg = await _after_answers(key, res, show=False)
        return _back(_lane_of(key), f"Read {len(vals) + len(attached)} answer(s) from the form. " + msg, app_id)

    @app.post("/app/{app_id}/open")
    async def open_form(app_id: int, csrf: str = Form(""), lane: str = Form(STUCK)):
        """Bring the tab to the front, or reopen + replay the plan for the user to finish."""
        check_csrf(csrf)
        return _back(lane if lane in LANES else STUCK, await _show(_key(app_id)), app_id)

    @app.post("/app/{app_id}/code")
    async def code(app_id: int, csrf: str = Form(""), code: str = Form("")):
        """The security code a board emailed after Submit: type it in the kept tab and submit."""
        check_csrf(csrf)
        key = _key(app_id)
        from ..browser.submit import enter_code
        async with sess.lock:
            res = await enter_code(ctx, key, code)
        if res["state"] == SUBMITTED:
            sess.submitted += 1
        p = ctx.ledger.get(key)
        lane = STUCK if res["state"] == STUCK else READY
        return _back(lane, f"{p.get('company')} - {p.get('title')}: {res['state']} ({res['detail']})", app_id)

    @app.post("/app/{app_id}/manual")
    async def manual(app_id: int, csrf: str = Form(""), lane: str = Form(STUCK)):
        """You sent it yourself in the form's tab (e.g. after a code or check there)."""
        check_csrf(csrf)
        from ..browser.submit import mark_manual
        async with sess.lock:                  # no browser needed: it records, and screenshots an open tab
            await mark_manual(ctx, None, _key(app_id))
        sess.manual += 1
        return _back(lane if lane in LANES else STUCK, "Recorded as submitted by hand.")

    async def _refill(keys: list[str]) -> int:
        for k in keys:
            tab, _ = ctx.take_tab(k)
            if tab is not None and not tab.is_closed():
                await tab.close()
        return len(ctx.ledger.requeue_keys(keys))

    @app.post("/app/{app_id}/retry")
    async def retry(app_id: int, csrf: str = Form("")):
        """Fill this one again from scratch on the next run (after you fixed the cause)."""
        check_csrf(csrf)
        n = await _refill([_key(app_id)])
        return _back(STUCK, "It will be filled again on the next `autoapply run`." if n else "Nothing to fill again.")

    @app.post("/retry-stuck")
    async def retry_all(csrf: str = Form("")):
        check_csrf(csrf)
        n = await _refill([a["posting_key"] for a in ctx.ledger.applications_in_lane(STUCK)])
        return _back(STUCK, f"{n} application(s) will be filled again on the next `autoapply run --review`.")

    @app.post("/app/{app_id}/giveup")
    async def giveup(app_id: int, csrf: str = Form("")):
        check_csrf(csrf)
        key = _key(app_id)
        actions.give_up(ctx, key)
        tab, _ = ctx.take_tab(key)
        if tab is not None:
            await tab.close()
        return _back(STUCK, "Gave up on it.")

    return app


def finish_session(ctx, sess: Session) -> None:
    if ctx.writeback is not None:
        n = ctx.writeback.flush()
        log.info("wrote %d status change(s) back to job-monitor", n)
    if sess.submitted or sess.manual:
        ctx.notifier.review_summary(sess.submitted, sess.manual)
    run_id = ctx.ledger.start_run("review")
    ctx.ledger.end_run(run_id, {"submitted": sess.submitted, "submitted_manual": sess.manual})


async def _serve(app, port: int, open_browser: bool) -> None:
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)
    url = f"http://127.0.0.1:{port}/"
    print(f"Review queue: {url}  (Ctrl+C to end the session)")
    if open_browser:
        asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
    await server.serve()


def run_and_review(ctx, limit: int | None = None, port: int = 8765, open_browser: bool = True) -> int:
    """`autoapply run --review`: fill in the tool's browser, keep the filled tabs open,
    then serve the review queue in the same session so approval submits those tabs
    as they stand (anything without a live tab is refilled as usual)."""
    from ..browser.fill import fill_many
    from ..pipeline import run as run_pipeline

    sess = Session(ctx)
    app = create_app(ctx, sess, port)            # sets ctx.keep_tabs

    async def main():
        loop = asyncio.get_running_loop()
        bs = await sess.browser()

        def fill_in_this_session(ctx_, keys, stats):
            # the pipeline runs in a worker thread; the browser lives on this loop
            asyncio.run_coroutine_threadsafe(fill_many(ctx_, keys, stats, session=bs), loop).result()

        try:
            counts = await asyncio.to_thread(run_pipeline, ctx, limit, False, fill_in_this_session)
            from ..cli import _summary
            print(_summary(counts))
        except Exception as e:  # noqa: BLE001 - what is already queued can still be reviewed
            log.exception("the run failed")
            print(f"The run stopped early ({type(e).__name__}: {e}); opening the review queue anyway.")
        kept = len(ctx.live_tabs())
        print(f"{kept} filled form(s) are open in the tool's browser and will be submitted as they stand "
              "when you approve them" + (" (test mode: nothing is submitted)." if ctx.never_submit else "."))
        try:
            await _serve(app, port, open_browser)
        finally:
            await sess.close()

    try:
        asyncio.run(main())
    finally:
        finish_session(ctx, sess)
        print(f"Session over: {sess.submitted} submitted, {sess.manual} recorded as submitted by hand.")
    return 0


def serve(ctx, port: int = 8765, open_browser: bool = True) -> int:
    sess = Session(ctx)
    app = create_app(ctx, sess, port)

    async def main():
        try:
            await _serve(app, port, open_browser)
        finally:
            await sess.close()

    try:
        asyncio.run(main())
    finally:
        finish_session(ctx, sess)
        print(f"Session over: {sess.submitted} submitted, {sess.manual} recorded as submitted by hand.")
    return 0


__all__ = ["create_app", "serve", "Session", "SUBMITTED_MANUAL", "WOULD_SUBMIT"]
