"""Workday's multi-page application.

A Workday application is a wizard behind a per-company account:
  Create Account/Sign In -> My Information -> My Experience -> Application Questions
  (one or more pages) -> Voluntary Disclosures -> (Self Identify) -> Review.
The progress bar names every step ("current step 2 of 6 My Information"). The tool
signs in with the user's Workday email and password (secrets.env), lets Simplify fill
each page, fills what is left like any other form, clicks Save and Continue, and stops
on Review: Submit is only clicked by submit.py after approval. A page with questions
nobody may answer stops the walk there (Needs input); once answered, the walk goes on
from that page. Workday keeps an unfinished application in the account, so a lost
tab is reopened and walked again with the answers already given.

Element ids VERIFIED 2026-10-23 on live tenants (nvidia.wd5, capitalone.wd12) for the
job page, apply flow, sign-in, create-account and sign-in error; the question pages
follow Workday's formField-* blocks as used by Simplify 3.1.10's Workday selectors.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
from datetime import datetime, timezone

from ..ats import form_hints, parse_key
from ..ledger import NEEDS_INPUT, READY
from ..normalize import normalize_question, slug
from .fields import FillError, enumerate_fields, fill_field, safe_evaluate, safe_screenshot

log = logging.getLogger(__name__)

APPLY_FLOW = '[data-automation-id="applyFlowPage"]'
SIGN_IN = '[data-automation-id="signInContent"]'
EMAIL_SIGN_IN = '[data-automation-id="SignInWithEmailButton"]'
SIGN_IN_LINK = '[data-automation-id="signInLink"]'
VERIFY_PASSWORD = '[data-automation-id="verifyPassword"]'
EMAIL = 'input[data-automation-id="email"]'
PASSWORD = 'input[data-automation-id="password"]'
SIGN_IN_SUBMIT = '[data-automation-id="signInSubmitButton"]'
CLICK_FILTER = '[data-automation-id="click_filter"]'        # the overlay that takes the sign-in click
CREATE_LINK = '[data-automation-id="createAccountLink"]'
CREATE_AGREE = 'input[data-automation-id="createAccountCheckbox"]'
CREATE_SUBMIT = '[data-automation-id="createAccountSubmitButton"]'
ERROR = '[data-automation-id="errorMessage"]'
JOB_APPLY = 'a[data-automation-id="adventureButton"]'
APPLY_MANUALLY = '[data-automation-id="applyManually"]'
NEXT = ('button[data-automation-id="bottom-navigation-next-button"], '
        'button[data-automation-id="pageFooterNextButton"]')
REVIEW = '[data-automation-id="applyFlowReviewPage"], [data-automation-id="reviewJobApplicationPage"]'
ACTIVE_STEP = '[data-automation-id="progressBarActiveStep"]'
FIELD_BLOCK = '[data-automation-id^="formField-"]'
# what Workday shows once an application went through (Simplify 3.1.10's Workday
# submittedSuccessPaths, as text)
SUCCESS_TEXT = re.compile(r"application (submitted|sent)|thank you for applying|your application has been"
                          r"|successfully been submitted", re.I)
SUCCESS = '[data-automation-id="candidateHomeTaskModal"], [role="dialog"] svg.wd-icon-check-circle'
MAX_PAGES = 12
ACCOUNTS_KEY = "workday_accounts"


# set while a Workday application holds the front of the browser for its whole walk
_holding_front = contextvars.ContextVar("workday_holding_front", default=False)


@contextlib.asynccontextmanager
async def in_front(page):
    """One Workday application at a time, in front from sign-in to Review. Simplify fills each
    new page by itself and pauses whenever its tab is hidden ("Please stay on this Workday
    page"), so no other form may take the front between pages."""
    from .fill import simplify_turn
    if _holding_front.get():
        yield
        return
    turn = simplify_turn()
    if turn.locked():
        log.info("    waiting for the browser's front (another form is using Simplify)")
    async with turn:
        token = _holding_front.set(True)
        try:
            try:
                await page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            yield
        finally:
            _holding_front.reset(token)


class NeedsInputError(Exception):
    """The walk stopped on a page with questions for the user (raised during approval)."""

    def __init__(self, missing: list[dict], plan: list[dict]):
        super().__init__(f"{len(missing)} question(s) need your answer")
        self.missing, self.plan = missing, plan


def _stuck(reason: str, detail: str):
    from .fill import Stuck
    return Stuck(reason, detail)


# ---------------------------------------------------------------- accounts --
def account_key(posting_key: str) -> str:
    """"<tenant>@<host>": one Workday account per tenant."""
    return parse_key(posting_key).board_token or ""


def company_of(posting_key: str) -> str:
    return account_key(posting_key).partition("@")[0]


def accounts(ctx) -> dict:
    return dict(ctx.state_get(ACCOUNTS_KEY) or {})


def set_account(ctx, posting_key: str, status: str, note: str = "", password: str | None = None) -> None:
    """Record how signing in went; `password` says which one worked ("yours" | "simplify")."""
    acc = accounts(ctx)
    acc[account_key(posting_key)] = {"status": status, "note": note[:200],
                                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if password:
        acc[account_key(posting_key)]["password"] = password
    ctx.state_set(ACCOUNTS_KEY, acc)


def account_status(ctx, posting_key: str) -> str | None:
    return accounts(ctx).get(account_key(posting_key), {}).get("status")


def account_blocked(ctx, posting_key: str) -> bool:
    """A company whose sign-in failed: its postings wait until the account is fixed."""
    return accounts(ctx).get(account_key(posting_key), {}).get("status") in ("no_account", "verify_email")


def clear_accounts(ctx, posting_keys) -> None:
    acc = accounts(ctx)
    for k in {account_key(p) for p in posting_keys if p.startswith("workday:")}:
        acc.pop(k, None)
    ctx.state_set(ACCOUNTS_KEY, acc)


# -------------------------------------------------------------- page state --
async def _visible(page, selector: str) -> bool:
    loc = page.locator(selector)
    try:
        for i in range(min(await loc.count(), 5)):
            if await loc.nth(i).is_visible():
                return True
    except Exception:  # noqa: BLE001 - the page re-renders
        return False
    return False


async def submitted(page) -> bool:
    if await _visible(page, SUCCESS):
        return True
    try:
        heads = await safe_evaluate(page, """() => [...document.querySelectorAll(
            'h1, h2, h3, [role=alert], [role=dialog], [data-automation-id=richText]')]
            .filter(e => e.offsetParent !== null).map(e => e.innerText).join('\\n')""")
    except Exception:  # noqa: BLE001
        return False
    return bool(SUCCESS_TEXT.search(heads or ""))


async def state(page) -> str:
    """'review' | 'form' | 'signin' | 'job' | 'submitted' | 'loading'."""
    if await _visible(page, REVIEW):
        return "review"
    if await _visible(page, SIGN_IN):
        return "signin"
    if await submitted(page):
        return "submitted"
    if await _visible(page, APPLY_FLOW) and (await _visible(page, NEXT) or await _visible(page, FIELD_BLOCK)):
        return "form"
    if await _visible(page, JOB_APPLY):
        return "job"
    return "loading"


async def wait_state(page, timeout_s: float) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        st = await state(page)
        if st != "loading":
            return st
        await asyncio.sleep(0.5)
    return "gone"


async def step_name(page) -> str:
    try:
        t = await page.locator(ACTIVE_STEP).first.inner_text(timeout=2000)
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"^\s*current step \d+ of \d+\s*", "", " ".join(t.split()), flags=re.I)


# ----------------------------------------------------------------- sign in --
NOT_ACCEPTED = ("{company} did not accept your Workday sign-in ({text}). Usually one of these: "
                "1) no account there yet: `autoapply workday open {company}` and choose Create Account; "
                "2) the account was made with Simplify's Create Account button, which picks its own password: "
                "use Forgot your password on the sign-in page and set it to your Workday password; "
                "3) the email address is not verified yet: click the link in Workday's email. "
                "Then run `autoapply requeue --lane stuck`.")


async def _passwords(ctx, page, posting_key: str, email: str, mine: str) -> list[tuple[str, str]]:
    """(source, password) to try: yours, and Simplify's generated one for the same email
    (accounts made with its Create Account button use that). The one that worked last time first."""
    out = [("yours", mine)]
    from .simplify import signup_credentials
    simp = await signup_credentials(page.context)
    if simp and simp["email"].strip().lower() == email.strip().lower() and simp["password"] != mine:
        out.append(("simplify", simp["password"]))
    if accounts(ctx).get(account_key(posting_key), {}).get("password") == "simplify":
        out.reverse()
    return out


async def _attempt(page, email: str, password: str) -> str | None:
    """One sign-in. None when signed in; Workday's error text when refused. Typing before
    the form is wired up is silently dropped and the click then sends nothing (VERIFIED on
    a live tenant): settle first, and type + click again when nothing happens."""
    loop = asyncio.get_running_loop()
    for attempt in range(3):
        await page.locator(EMAIL).first.fill(email)
        await page.locator(PASSWORD).first.fill(password)
        target = CLICK_FILTER if await _visible(page, CLICK_FILTER) else SIGN_IN_SUBMIT
        await page.locator(target).first.click(force=True)
        deadline = loop.time() + 15
        while loop.time() < deadline:
            await asyncio.sleep(0.75)
            if await _visible(page, ERROR):
                return " ".join((await page.locator(ERROR).first.inner_text()).split())
            if not await _visible(page, SIGN_IN):
                return None
        log.info("    the Workday sign-in did not respond; trying again (%d of 3)", attempt + 2)
        await page.wait_for_timeout(1500)
    raise _stuck("workday_sign_in_failed", "signing in to Workday did not finish")


async def _email_form(page, company: str) -> None:
    if await _visible(page, VERIFY_PASSWORD) and await _visible(page, SIGN_IN_LINK):
        await page.locator(SIGN_IN_LINK).first.click()            # on Create Account: switch to Sign In
        await page.wait_for_timeout(1500)
    if await _visible(page, EMAIL_SIGN_IN):
        await page.locator(EMAIL_SIGN_IN).first.click()
    try:
        await page.locator(EMAIL).first.wait_for(state="visible", timeout=15000)
    except Exception:  # noqa: BLE001
        raise _stuck("workday_sign_in_failed", f"no email sign-in on {company}'s Workday page") from None
    await page.wait_for_timeout(2500)


def password_problems(pw: str) -> list[str]:
    """Workday's password rules, as its Create Account form lists them (live tenant, 2026-10-23)."""
    out = []
    if len(pw) < 8:
        out.append("at least 8 characters")
    if not re.search(r"[A-Za-z]", pw):
        out.append("a letter")
    if not re.search(r"\d", pw):
        out.append("a number")
    if not re.search(r"[^A-Za-z0-9]", pw):
        out.append("a special character")
    return out


async def _click_create(page) -> None:
    overlays = page.locator(f"{CLICK_FILTER}[aria-label='Create Account']")
    target = overlays if await overlays.count() and await overlays.first.is_visible() else page.locator(CREATE_SUBMIT)
    await target.first.click(force=True)


async def create_account(ctx, page, posting_key: str, email: str, password: str) -> None:
    """Create the account at this company with your Workday login, wait, and sign in with it.
    Raises Stuck when Workday wants an email verification or says the account exists."""
    company = company_of(posting_key)
    weak = password_problems(password)
    if weak:
        raise _stuck("workday_sign_in_failed", f"cannot create an account at {company}: Workday passwords need "
                     f"{', '.join(weak)}. Pick a new one with `autoapply workday login`")
    log.info("    no account at %s's Workday yet: creating it with your Workday login", company)
    await page.reload(wait_until="domcontentloaded")
    await wait_state(page, ctx.profile.browser.page_timeout_s)
    if await _visible(page, EMAIL_SIGN_IN):
        await page.locator(EMAIL_SIGN_IN).first.click()
    try:
        await page.locator(CREATE_LINK).first.wait_for(state="visible", timeout=15000)
        await page.locator(CREATE_LINK).first.click()
        await page.locator(VERIFY_PASSWORD).first.wait_for(state="visible", timeout=15000)
    except Exception:  # noqa: BLE001
        raise _stuck("workday_sign_in_failed", f"no Create Account form on {company}'s Workday page") from None
    await page.wait_for_timeout(2500)              # typing before the form is wired up is dropped
    loop = asyncio.get_running_loop()
    text = None
    for attempt in range(3):
        await page.locator(EMAIL).first.fill(email)
        await page.locator(PASSWORD).first.fill(password)
        await page.locator(VERIFY_PASSWORD).first.fill(password)
        agree = page.locator(CREATE_AGREE)
        if await agree.count() and not await agree.first.is_checked():
            await agree.first.click(force=True)
        await _click_create(page)
        deadline = loop.time() + 20
        while loop.time() < deadline:
            await asyncio.sleep(0.75)
            if await _visible(page, ERROR):
                text = " ".join((await page.locator(ERROR).first.inner_text()).split())
                break
            if not await _visible(page, VERIFY_PASSWORD):       # the form went: created
                text = ""
                break
        if text is not None:
            break
        log.info("    Create Account did not respond; trying again (%d of 3)", attempt + 2)
    if text is None:
        raise _stuck("workday_sign_in_failed", f"creating the account at {company} did not finish")
    if text:
        if re.search(r"verif", text, re.I):
            set_account(ctx, posting_key, "verify_email", text)
            raise _stuck("workday_verify_email", f"{company}: account created - verify your email first ({text})")
        set_account(ctx, posting_key, "no_account", text)
        raise _stuck("workday_sign_in_failed",
                     f"{company} did not create the account ({text}). If it says the account exists, its "
                     "password is not your Workday password: use Forgot your password on the sign-in page "
                     "and set it to yours, then `autoapply requeue --lane stuck`")
    set_account(ctx, posting_key, "created")
    if not await _visible(page, SIGN_IN):
        set_account(ctx, posting_key, "ok", "created by Cygnus", password="yours")
        log.info("    created the account at %s and Workday signed you in", company)
        return
    wait = ctx.profile.workday.after_create_wait_s
    log.info("    created the account at %s; signing in after %ss", company, int(wait))
    await page.wait_for_timeout(wait * 1000)
    await page.reload(wait_until="domcontentloaded")
    if await wait_state(page, ctx.profile.browser.page_timeout_s) != "signin":
        set_account(ctx, posting_key, "ok", "created by Cygnus", password="yours")
        return
    await _email_form(page, company)
    text = await _attempt(page, email, password)
    if text is None:
        set_account(ctx, posting_key, "ok", "created by Cygnus", password="yours")
        log.info("    signed in to %s's new account", company)
        return
    status = "verify_email" if re.search(r"verif", text, re.I) else "no_account"
    set_account(ctx, posting_key, status, text)
    raise _stuck("workday_verify_email" if status == "verify_email" else "workday_sign_in_failed",
                 f"{company}: the account was created but signing in failed ({text}). If Workday emailed you "
                 "a verification link, click it, then `autoapply requeue --lane stuck`")


async def sign_in(ctx, page, posting_key: str) -> None:
    email, password = ctx.secrets.get("WORKDAY_EMAIL"), ctx.secrets.get("WORKDAY_PASSWORD")
    company = company_of(posting_key)
    if not email or not password:
        raise _stuck("workday_no_login", "add the email and password you use for Workday accounts: "
                                         "run `autoapply workday login`")
    text = ""
    for i, (source, pw) in enumerate(await _passwords(ctx, page, posting_key, email, password)):
        if i:                    # a fresh form: the last attempt's error must not answer for this one
            await page.reload(wait_until="domcontentloaded")
            await wait_state(page, ctx.profile.browser.page_timeout_s)
        await _email_form(page, company)
        text = await _attempt(page, email, pw)
        if text is None:
            set_account(ctx, posting_key, "ok", password=source)
            log.info("    signed in to %s's Workday%s", company,
                     " with the password Simplify made when it created the account" if source == "simplify" else "")
            return
        if re.search(r"verif", text, re.I):
            set_account(ctx, posting_key, "verify_email", text)
            raise _stuck("workday_verify_email", f"{company}: verify your email first ({text})")
    if ctx.profile.workday.create_accounts and account_status(ctx, posting_key) not in ("created",):
        return await create_account(ctx, page, posting_key, email, password)
    set_account(ctx, posting_key, "no_account", text)
    raise _stuck("workday_sign_in_failed", NOT_ACCEPTED.format(company=company, text=text))


# ---------------------------------------------------------------- Simplify --
async def simplify_page(ctx, page, hints: dict) -> None:
    """Simplify for one Workday page. After the first fill Simplify keeps filling each new
    page by itself about 1.5 s after it renders (continuous mode, on by default), so wait
    for that fill before clicking Autofill; a click during a fill is ignored anyway."""
    from . import simplify
    async with in_front(page):
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        mock = getattr(ctx, "simplify_mock", None)
        if mock is not None:
            await mock(page)
            return
        timeout = ctx.profile.browser.autofill_timeout_s
        try:
            status = await simplify.fill_status(page)
            if status not in simplify.FILL_RUNNING:
                await asyncio.sleep(2.5)
                status = await simplify.fill_status(page)
            if status in simplify.FILL_RUNNING:
                await simplify.wait_for_fill(page, hints, timeout)
            else:
                await simplify.autofill(page, hints, timeout_s=timeout, require_fill=False)
        except simplify.SimplifyError as e:
            raise _stuck(e.kind, str(e)) from e


# ------------------------------------------------------------------ errors --
async def page_errors(page) -> dict[str, str]:
    """data-aa-key -> Workday's error text for blocks it marked invalid after Continue."""
    return await safe_evaluate(page, """() => {
      const out = {};
      for (const b of document.querySelectorAll('[data-automation-id^="formField-"][data-aa-key]')) {
        const bad = b.querySelector('[aria-invalid="true"]');
        const msg = b.querySelector('[data-automation-id="errorMessage"], [role="alert"]');
        if (bad || (msg && msg.innerText.trim())) out[b.getAttribute('data-aa-key')] =
          (msg && msg.innerText.trim()) || 'Workday marked this field as invalid';
      }
      return out;
    }""") or {}


async def banner_errors(page) -> str:
    try:
        return await safe_evaluate(page, """() => [...document.querySelectorAll(
          '[data-automation-id="errorBanner"], [data-automation-id="errorMessage"]')]
          .filter(e => e.offsetParent !== null && !e.closest('[data-automation-id^="formField-"]'))
          .map(e => e.innerText.trim()).filter(Boolean).join(' | ')""") or ""
    except Exception:  # noqa: BLE001
        return ""


async def advance(page, before: str, timeout_s: float = 25) -> str:
    """Click Save and Continue: 'next' | 'review' | 'errors' | 'same'."""
    btn = page.locator(NEXT).first
    label = " ".join((await btn.inner_text()).split())
    if re.search(r"submit", label, re.I):
        raise _stuck("workday_unexpected_submit", "the next button reads Submit but this is not the Review page")
    await btn.click(timeout=10000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        await asyncio.sleep(0.75)
        if await _visible(page, REVIEW):
            return "review"
        if await page_errors(page) or await banner_errors(page):
            return "errors"
        now = await step_name(page)
        if now and now != before and (await _visible(page, FIELD_BLOCK) or await _visible(page, NEXT)):
            return "next"
    return "same"


# -------------------------------------------------------------------- walk --
def known_answers(plan: list[dict]) -> dict:
    """Answers already given for this application, by (question, kind)."""
    out = {}
    for e in plan:
        if e.get("source") in ("simplify", "workday", "resume") or e.get("kind") == "file":
            continue
        out[(normalize_question(e.get("label", "")), e.get("kind"))] = e
    return out


def merge_plan(old: list[dict], new: list[dict]) -> list[dict]:
    """Later entries for the same field replace earlier ones; order is kept."""
    out: dict[str, dict] = {}
    for e in [*old, *new]:
        out[e["key"]] = e
    return list(out.values())


async def walk(ctx, page, posting_key: str, plan: list, missing: list, generated: list, *,
               known: dict | None = None, resume_name: str | None = None, resume_path=None,
               resume_text: str = "", posting: dict | None = None, desc: str = "",
               skip_simplify_first: bool = False, check_consistency: bool = True,
               track: dict | None = None) -> bool:
    """Walk the wizard from the current page. True on Review; False when a page has
    questions for the user (they are in `missing`). Plan entries are PlanEntry objects."""
    from .fill import PlanEntry, correct_legal_answers, fill_blanks, final_check, _missing
    hints = form_hints("workday")
    track = track if track is not None else {}
    timeout = ctx.profile.browser.page_timeout_s

    def step(name: str):
        track["step"] = name
        log.info("    %s", name)

    pages = 0
    while pages < MAX_PAGES:
        st = await wait_state(page, timeout)
        if st == "gone":
            raise _stuck("posting_gone", "the Workday application did not load (the posting may be closed)")
        if st == "job":
            await page.locator(JOB_APPLY).first.click()
            try:
                await page.locator(APPLY_MANUALLY).first.click(timeout=8000)
            except Exception:  # noqa: BLE001 - some tenants go straight to the form
                pass
            continue
        if st == "signin":
            step("signing in to Workday")
            await sign_in(ctx, page, posting_key)
            continue
        if st == "submitted":
            raise _stuck("already_submitted", "Workday shows this application as already submitted")
        if st == "review":
            return True

        name = await step_name(page) or f"page {pages + 1}"
        before = {f.key: f.value for f in await enumerate_fields(page, hints)}
        if not (skip_simplify_first and pages == 0):
            step(f"{name}: Simplify autofill")
            await simplify_page(ctx, page, hints)
        fields = await enumerate_fields(page, hints)
        mine = {p.key for p in plan if p.source not in ("simplify", "workday")}
        for f in fields:
            # Workday pages open empty, apart from what the account kept from an earlier
            # application; anything filled that the tool did not fill counts as Simplify's
            f.filled_by_simplify = bool(f.value.strip()) and f.key not in mine
        if check_consistency:
            await correct_legal_answers(page, fields, ctx.profile, plan, missing)
        planned = {p.key for p in plan}
        for f in fields:
            if f.filled_by_simplify and f.kind != "file" and f.key not in planned:
                src = "simplify" if not (before.get(f.key) or "").strip() else "workday"
                plan.append(PlanEntry(f.key, f.label, f.kind, f.value, src, f.required, f.options))
        skip: set[str] = set()
        rf = next((f for f in fields if f.is_resume), None)
        if rf is not None and resume_path is not None:
            skip.add(rf.key)
            if resume_name and resume_name.lower() not in rf.value.lower():
                step(f"{name}: attaching {resume_name}")
                try:
                    await fill_field(page, rf, str(resume_path))
                except FillError as e:
                    raise _stuck("resume_upload_failed", str(e)) from e
            plan.append(PlanEntry(rf.key, rf.label, "file", resume_name, "resume", rf.required))
        already = len(missing)
        await fill_blanks(ctx, page, fields, skip, plan, missing, generated, posting or {}, desc,
                          resume_text, step, known=known)
        await final_check(page, hints, plan, missing)
        if len(missing) > already:
            log.info("    %s: %d question(s) need your answer", name, len(missing) - already)
            return False
        step(f"{name}: Save and Continue")
        res = await advance(page, name)
        if res == "review":
            return True
        if res == "errors":
            errs = await page_errors(page)
            by_key = {f.key: f for f in await enumerate_fields(page, hints)}
            for k, text in errs.items():
                if k in by_key and k not in {m["key"] for m in missing}:
                    missing.append(_missing(by_key[k], f"Workday says: {text}"[:200]))
            banner = await banner_errors(page)
            if not errs and banner:
                raise _stuck("workday_page_error", f"{name}: {banner[:300]}")
            return False
        if res == "same":
            raise _stuck("workday_stuck_on_page", f"{name}: Save and Continue did not move on")
        pages += 1
    raise _stuck("workday_too_many_pages", f"more than {MAX_PAGES} pages without reaching Review")


async def fill_application(ctx, page, posting_key, resume_name, resume_path, resume_text, desc, posting,
                           check_consistency, track):
    """fill.fill_one for Workday: open, sign in, walk to Review (or to a page that needs the user)."""
    from .fill import FillOutcome, open_workday
    plan: list = []
    missing: list[dict] = []
    generated: list[str] = []
    async with in_front(page):
        await open_workday(ctx, page, posting_key)
        await walk(ctx, page, posting_key, plan, missing, generated, resume_name=resume_name,
                   resume_path=resume_path, resume_text=resume_text, posting=posting, desc=desc,
                   check_consistency=check_consistency, track=track)
    shot = await safe_screenshot(page, ctx.paths.screenshots / f"{slug(posting_key, 80)}.png")
    corrected = [p.label for p in plan if getattr(p, "note", None)]
    return FillOutcome(lane=NEEDS_INPUT if missing else READY, plan=[p.to_dict() for p in plan],
                       missing=missing, stuck_reason=None, screenshot=shot, resume_file=resume_name,
                       generated=generated, page=page, corrected=corrected)


async def resume(ctx, page, posting_key: str) -> bool:
    """Go on from the page the tab is on, using the answers already recorded; saves the
    outcome to the ledger. True when the tab is on Review with nothing missing."""
    from .fill import PlanEntry
    led = ctx.ledger
    app = led.application(posting_key) or {}
    old = app.get("fill_plan") or []
    plan = [PlanEntry(**{k: v for k, v in e.items() if k in PlanEntry.__dataclass_fields__}) for e in old]
    missing: list[dict] = []
    row = led.get(posting_key)
    res = app.get("resume_file")
    det = led.details(posting_key) or {}
    async with in_front(page):
        on_review = await walk(ctx, page, posting_key, plan, missing, [], known=known_answers(old),
                               resume_name=res, resume_path=ctx.resumes.path(res) if res else None,
                               resume_text=ctx.resumes.text(res) if res else "", posting=dict(row.data),
                               desc=det.get("description_text") or "", skip_simplify_first=True)
    led.upsert_application(posting_key, fill_plan=merge_plan(old, [p.to_dict() for p in plan]), missing=missing)
    lane = READY if on_review and not missing else NEEDS_INPUT
    if led.get(posting_key).state != lane:
        led.set_state(posting_key, lane, None, "Workday walk went on")
    return lane == READY


async def replay(ctx, bs, app: dict):
    """Reopen the application (Workday kept the draft) and walk it to Review again with the
    recorded answers. Raises Stuck, or NeedsInputError when Workday asks something new."""
    from .fill import PlanEntry, open_workday
    key = app["posting_key"]
    old = app.get("fill_plan") or []
    page = await bs.new_page()
    try:
        plan = [PlanEntry(**{k: v for k, v in e.items() if k in PlanEntry.__dataclass_fields__}) for e in old]
        missing: list[dict] = []
        res = app.get("resume_file")
        async with in_front(page):
            await open_workday(ctx, page, key)
            on_review = await walk(ctx, page, key, plan, missing, [], known=known_answers(old), resume_name=res,
                                   resume_path=ctx.resumes.path(res) if res else None,
                                   resume_text=ctx.resumes.text(res) if res else "",
                                   posting=dict(ctx.ledger.get(key).data),
                                   desc=(ctx.ledger.details(key) or {}).get("description_text") or "")
        if missing or not on_review:
            raise NeedsInputError(missing, merge_plan(old, [p.to_dict() for p in plan]))
        return page
    except BaseException as e:
        e.page = page
        raise


async def wait_submitted(page, timeout_s: float = 40) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await submitted(page):
            return True
        await asyncio.sleep(1.0)
    return False
