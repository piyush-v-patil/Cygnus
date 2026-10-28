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
# a job source's link is only ever shown as a web link
TEMPLATES.env.filters["web_url"] = lambda u: u if isinstance(u, str) and u.lower().startswith(("https://", "http://")) else ""
LANE_TITLES = {READY: "Ready", NEEDS_INPUT: "Needs input", STUCK: "Stuck"}
SOURCE_LABEL = {"simplify": "Simplify", "answer_bank": "Saved answer", "claude": "Claude", "user": "You",
                "profile": "Your visa answers", "work_prefs": "Your work preferences",
                "workday": "Workday account",
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
        self.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def browser(self):
        if self.bs is not None and self.bs.closed:
            log.info("the browser window was closed: opening a new one")
            try:
                await self.bs.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - already gone
                pass
            self.bs = None
            self.ctx.live_tabs()                      # forget the tabs that went with it
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

    class Gone(Exception):
        """A card acted on after its application moved on (another tab, an older page)."""

        def __init__(self, key: str, state: str):
            p = ctx.ledger.get(key)
            super().__init__(f"{p.get('company')} - {p.get('title')} is already {state.replace('_', ' ')}; "
                             "nothing was changed.")
            self.lane = state if state in LANES else READY

    @app.exception_handler(Gone)
    async def gone(request: Request, e: Gone):
        return _back(e.lane, str(e))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, e: Exception):
        log.error("review: %s %s failed: %s: %s", request.method, request.url.path, type(e).__name__, e,
                  exc_info=e)
        return _back(READY, f"That did not work ({type(e).__name__}: {str(e)[:200]}). "
                            "Details are in the log; nothing was submitted by this error.")

    def _active(key: str) -> str:
        """`key`, if its application is still in a review lane."""
        state = ctx.ledger.get(key).state
        if state not in LANES:
            raise Gone(key, state)
        return key

    def check_csrf(token: str):
        if not secrets.compare_digest(token or "", sess.csrf):
            raise HTTPException(403, "stale page - reload and try again")

    def suggest(missing: list[dict], company: str, resume_file: str | None) -> list[dict]:
        """Needs-input questions with a pre-filled answer where one is close at hand:
        your work preferences, else the saved answer to a question worded differently.
        Shown with where it came from; saving the card is what accepts it."""
        from ..questions import near_answer, protected, standing_value
        resume_text = None
        out = []
        for m in missing:
            m = dict(m)
            out.append(m)
            if m.get("suggested") or m.get("no_bank") or protected(m.get("label", "")) \
                    or m.get("kind") in ("checkbox", "checkbox_group", "file"):
                continue
            opts = m.get("options") or None
            if resume_text is None:
                try:
                    resume_text = ctx.resumes.text(resume_file) if resume_file else ""
                except Exception:  # noqa: BLE001 - a moved resume only costs the employer rule
                    resume_text = ""
            hit = standing_value(m["label"], m.get("answer_kind", ""), opts, ctx.profile.work,
                                 company, resume_text)
            if hit is not None:
                m["suggested"], m["from"] = hit[0], f"your work preferences: {hit[1]}"
                continue
            near = near_answer(ctx.answers.items, m["label"], m.get("answer_kind", ""), opts)
            if near is not None:
                m["suggested"], m["from"] = near[0], f"your answer to “{near[1]}”"
        return out

    def view(app_row: dict) -> dict:
        led = ctx.ledger
        key = app_row["posting_key"]
        p = led.get(key)
        sc = led.score(key) or {}
        from ..companies import paused, until_text
        pause = paused(ctx, p.get("company") if p else None)
        return {"app": app_row, "p": p.data if p else {}, "state": p.state if p else "",
                "paused": until_text(pause, p.get("company")) if pause else None,
                "live": ctx.has_live_tab(key),
                "score": sc, "waited": _ago(app_row.get("updated_at")),
                "plan": app_row.get("fill_plan") or [],
                "missing": suggest(app_row.get("missing") or [], p.get("company", "") if p else "",
                                   app_row.get("resume_file"))}

    def page(request: Request, name: str, **kw):
        led = ctx.ledger
        counts = {lane: len(led.applications_in_lane(lane)) for lane in LANES}
        return TEMPLATES.TemplateResponse(request, name, {
            "counts": counts, "lane_titles": LANE_TITLES, "spend": led.month_spend(),
            "budget": ctx.profile.llm.monthly_budget_usd, "csrf": sess.csrf, "source_label": SOURCE_LABEL,
            "test_mode": ctx.never_submit, "flash": request.query_params.get("msg"),
            "mail_on": ctx.profile.mail.enabled,
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
        from ..browser.fill import AlreadyApplied, Stuck
        from ..browser.replay import replay
        from ..browser.submit import record_already_applied
        from ..browser.workday import NeedsInputError
        async with sess.lock:
            _active(key)                               # never reopen (and refill) a sent application
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
                except AlreadyApplied as e:
                    await record_already_applied(ctx, key, e, getattr(e, "page", None))
                    return f"{e}: recorded as submitted (see History), not filled again."
                except Stuck as e:
                    tab = getattr(e, "page", None)
                    note = f"Reopened the form; finish it by hand ({e})."
                except Exception as e:  # noqa: BLE001
                    tab = getattr(e, "page", None)
                    note = f"Reopened the form; finish it by hand ({type(e).__name__})."
                if tab is not None:
                    ctx.remember_tab(key, tab)
            if tab is not None:
                from ..browser.launch import show_window
                await show_window(tab)               # in front of the review page, not only inside its window
        return note

    async def _workday_on(key: str) -> str:
        """A Workday application stops on the page with questions; once they are answered
        the walk goes on from there in the same tab, to Review or to the next questions."""
        from ..browser import workday
        from ..browser.fill import AlreadyApplied, Stuck
        from ..browser.submit import _stuck, record_already_applied
        async with sess.lock:
            page = ctx.open_tabs[key]
            try:
                if await workday.resume(ctx, page, key):
                    return "Workday: went on to the Review page."
                n = len((ctx.ledger.application(key) or {}).get("missing") or [])
                return f"Workday asked {n} more question(s) on a later page; answer them here."
            except AlreadyApplied as e:
                await record_already_applied(ctx, key, e, page)
                return f"{e}: recorded as submitted (see History)."
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
        return _active(a["posting_key"])

    async def _submit(key: str, force: bool = False) -> str:
        from ..browser.submit import submit
        async with sess.lock:
            _active(key)                               # another click may have finished it meanwhile
            bs = await sess.browser()
            res = await submit(ctx, bs, key, force=force)
        if res["state"] == SUBMITTED:
            sess.submitted += 1
        p = ctx.ledger.get(key)
        return f"{p.get('company')} — {p.get('title')}: {res['state']} ({res['detail']})"

    @app.post("/app/{app_id}/approve")
    async def approve(app_id: int, csrf: str = Form(""), lane: str = Form(READY), force: str = Form("")):
        """force=1: you checked and it was not sent, so Submit may be clicked again."""
        check_csrf(csrf)
        return _back(lane if lane in LANES else READY, await _submit(_key(app_id), force=force == "1"))

    @app.post("/approve-all")
    async def approve_all(csrf: str = Form(""), ids: str = Form("")):
        check_csrf(csrf)
        from ..companies import pauses, paused_in
        active = pauses(ctx)
        msgs, held = [], []
        for i in [int(x) for x in ids.split(",") if x.strip().isdigit()]:
            a = ctx.ledger.application(app_id=i)
            if a is None or ctx.ledger.get(a["posting_key"]).state != READY:
                continue
            company = ctx.ledger.get(a["posting_key"]).get("company")
            if paused_in(active, company):         # paused (by you, or at its limit): only its own Approve sends it
                held.append(company or a["posting_key"])
                continue
            try:                                       # each still submits on its own
                msgs.append(await _submit(a["posting_key"]))
                active = pauses(ctx)               # that one may have taken it to limits.company_max
            except Gone:
                continue
            except Exception as e:  # noqa: BLE001 - one failure must not lose the others' results
                log.error("approve-all: %s failed: %s: %s", a["posting_key"], type(e).__name__, e, exc_info=e)
                msgs.append(f"{a['posting_key']}: not submitted ({type(e).__name__})")
        note = (f" Left for you (company paused): {', '.join(sorted(set(held)))}." if held else "")
        return _back(READY, f"{len(msgs)} processed: " + " | ".join(msgs) + note)

    @app.post("/app/{app_id}/reject")
    async def reject(app_id: int, csrf: str = Form(""), lane: str = Form(READY)):
        check_csrf(csrf)
        key = _key(app_id)
        async with sess.lock:
            actions.reject(ctx, _active(key))
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
            res = await enter_code(ctx, _active(key), code)
        if res["state"] == SUBMITTED:
            sess.submitted += 1
        p = ctx.ledger.get(key)
        lane = STUCK if res["state"] == STUCK else READY
        return _back(lane, f"{p.get('company')} - {p.get('title')}: {res['state']} ({res['detail']})", app_id)

    @app.post("/app/{app_id}/code-from-mail")
    async def code_from_mail(app_id: int, csrf: str = Form("")):
        """Read the code from your mailbox (mail.enabled) instead of typing it."""
        check_csrf(csrf)
        key = _key(app_id)
        from datetime import datetime, timedelta, timezone

        from ..browser.submit import enter_code, find_code
        if not ctx.has_live_tab(key):
            return _back(STUCK, "The tab with this form is gone.", app_id)
        clicked = (ctx.ledger.application(key) or {}).get("submit_clicked_at")
        since = (datetime.strptime(clicked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if clicked
                 else datetime.now(timezone.utc) - timedelta(hours=1))
        async with sess.lock:
            got = await find_code(ctx, _active(key), ctx.open_tabs[key], since, timeout_s=60)
            if not got:
                return _back(STUCK, "No code email found (mail off, or nothing arrived yet). Type the code instead.",
                             app_id)
            res = await enter_code(ctx, key, got)
        if res["state"] == SUBMITTED:
            sess.submitted += 1
        p = ctx.ledger.get(key)
        return _back(STUCK if res["state"] == STUCK else READY,
                     f"{p.get('company')} - {p.get('title')}: {res['state']} ({res['detail']})", app_id)

    @app.post("/app/{app_id}/manual")
    async def manual(app_id: int, csrf: str = Form(""), lane: str = Form(STUCK)):
        """You sent it yourself in the form's tab (e.g. after a code or check there)."""
        check_csrf(csrf)
        from ..browser.submit import mark_manual
        key = _key(app_id)
        async with sess.lock:                  # no browser needed: it records, and screenshots an open tab
            await mark_manual(ctx, None, _active(key))
        sess.manual += 1
        return _back(lane if lane in LANES else STUCK, "Recorded as submitted by hand.")

    async def _refill(keys: list[str]) -> tuple[int, int]:
        """(queued again, skipped because Submit was already clicked for them)."""
        clicked = [k for k in keys if ctx.ledger.submit_clicked(k)]
        keys = [k for k in keys if k not in clicked]
        for k in keys:
            tab, _ = ctx.take_tab(k)
            if tab is not None and not tab.is_closed():
                await tab.close()
        from ..browser.workday import clear_accounts
        clear_accounts(ctx, keys)              # a Workday sign-in that stopped gets another try
        return len(ctx.ledger.requeue_keys(keys)), len(clicked)

    def _refill_msg(n: int, skipped: int, one: bool = False) -> str:
        msg = (("It will be filled again on the next `autoapply run`." if n else "") if one else
               f"{n} application(s) will be filled again on the next `autoapply run --review`.")
        if skipped:
            msg += (f" {skipped} not filled again: Submit was already clicked for "
                    f"{'it' if one else 'them'}, so refilling could send it twice. Check, then use "
                    "I submitted it or Submit it again.")
        return msg.strip() or "Nothing to fill again."

    @app.post("/app/{app_id}/retry")
    async def retry(app_id: int, csrf: str = Form(""), lane: str = Form(STUCK)):
        """Fill this one again from scratch on the next run (after you fixed the cause, or saved
        answers that cover its questions)."""
        check_csrf(csrf)
        key = _key(app_id)
        async with sess.lock:
            n, skipped = await _refill([_active(key)])
        return _back(lane if lane in LANES else STUCK, _refill_msg(n, skipped, one=True))

    @app.post("/retry-lane/{lane}")
    async def retry_all(lane: str, csrf: str = Form("")):
        check_csrf(csrf)
        if lane not in (STUCK, NEEDS_INPUT):
            raise HTTPException(404)
        async with sess.lock:
            n, skipped = await _refill([a["posting_key"] for a in ctx.ledger.applications_in_lane(lane)])
        return _back(lane, _refill_msg(n, skipped))

    @app.post("/app/{app_id}/giveup")
    async def giveup(app_id: int, csrf: str = Form("")):
        check_csrf(csrf)
        key = _key(app_id)
        async with sess.lock:
            actions.give_up(ctx, _active(key))
        tab, _ = ctx.take_tab(key)
        if tab is not None:
            await tab.close()
        return _back(STUCK, "Gave up on it.")

    return app


def session_cost(ctx, sess: Session) -> str:
    from ..pipeline import cost_line, duration
    took = (datetime.now(timezone.utc) - datetime.strptime(sess.started_at, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)).total_seconds()
    return f"Review took {duration(took)}. " + cost_line(ctx.ledger.spend_between(sess.started_at), "during review")


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
            from ..pipeline import time_line
            print(_summary(counts))
            if counts.get("timing"):
                print(time_line(counts["timing"]))
        except Exception as e:  # noqa: BLE001 - what is already queued can still be reviewed
            log.exception("the run failed")
            print(f"The run stopped early ({type(e).__name__}: {e}); opening the review queue anyway.")
        from ..pipeline import cost_line
        last = ctx.ledger.last_run()
        if last:
            print(cost_line(ctx.ledger.run_spend(last)))
        sess.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # the review part from here
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
        print(f"Session over: {sess.submitted} submitted, {sess.manual} recorded as submitted by hand. "
              + session_cost(ctx, sess))
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
        print(f"Session over: {sess.submitted} submitted, {sess.manual} recorded as submitted by hand. "
              + session_cost(ctx, sess))
    return 0


__all__ = ["create_app", "serve", "Session", "SUBMITTED_MANUAL", "WOULD_SUBMIT"]
