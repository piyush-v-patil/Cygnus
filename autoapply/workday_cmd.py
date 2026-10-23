"""`autoapply workday`: the Workday login the tool signs in with, and which companies
still need an account. With workday.create_accounts (the default) a run creates a
missing account with that login; otherwise you create them, once per company."""
from __future__ import annotations

import getpass
from collections import Counter, defaultdict

from .ats import application_url, parse_key
from .browser.workday import accounts, account_key
from .config import save_secret

WAITING = ("scored", "held", "enriched", "discovered", "needs_input", "ready", "stuck")
STATUS = {"ok": "signed in fine", "no_account": "sign-in failed - see `autoapply workday` notes",
          "verify_email": "verify your email address", "created": "account created, sign-in pending"}


def workday_command(ctx, action: str, company: str | None) -> int:
    if action == "login":
        return _login(ctx)
    if action == "open":
        return _open(ctx, company)
    return _list(ctx)


def _login(ctx) -> int:
    print("Cygnus signs in to each company's Workday with one email and password. Use the same\n"
          "two when you create an account on a company's Workday site. They are saved in\n"
          f"{ctx.paths.secrets} and hidden in logs.")
    current = ctx.secrets.get("WORKDAY_EMAIL", "")
    email = input(f"Workday email{f' [{current}]' if current else ''}: ").strip() or current
    if not email or "@" not in email:
        print("No valid email given; nothing saved.")
        return 1
    while True:
        pw = getpass.getpass("Workday password (typing is hidden): ")
        if not pw:
            print("No password given; nothing saved.")
            return 1
        from .browser.workday import password_problems
        weak = password_problems(pw)
        if weak:
            print(f"Workday passwords need {', '.join(weak)}. Try another.")
            continue
        if getpass.getpass("Type it again: ") == pw:
            break
        print("The two did not match; try again.")
    save_secret(ctx.paths, "WORKDAY_EMAIL", email)
    save_secret(ctx.paths, "WORKDAY_PASSWORD", pw)
    print("Saved.")
    if "workday" not in ctx.profile.filters.ats_allowed:
        print("To apply on Workday, add workday to filters.ats_allowed in profile.yaml:\n"
              "  ats_allowed: [greenhouse, ashby, lever, workday]")
    return 0


def _companies(ctx) -> dict[str, Counter]:
    by: dict[str, Counter] = defaultdict(Counter)
    for row in ctx.ledger.postings_in(WAITING):
        if row.posting_key.startswith("workday:"):
            by[account_key(row.posting_key)][row.state] += 1
    return by


def _list(ctx) -> int:
    has_login = bool(ctx.secrets.get("WORKDAY_EMAIL") and ctx.secrets.get("WORKDAY_PASSWORD"))
    print(f"Login: {ctx.secrets['WORKDAY_EMAIL'] if has_login else 'not set - run `autoapply workday login`'}")
    if "workday" not in ctx.profile.filters.ats_allowed:
        print("Workday is off: add workday to filters.ats_allowed in profile.yaml.")
    acc = accounts(ctx)
    by = _companies(ctx)
    if not by:
        print("No Workday postings waiting yet (they appear after a run with workday allowed).")
        return 0
    print(f"\n{'Company':<22} {'Waiting':>7}  Account")
    for k, counts in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
        tenant = k.partition("@")[0]
        st = acc.get(k, {}).get("status")
        print(f"{tenant:<22} {sum(counts.values()):>7}  {STATUS.get(st, 'not tried yet')}")
    if ctx.profile.workday.create_accounts:
        print("\nWhere signing in fails, the next run creates the account with your Workday login and\n"
              "signs in (workday.create_accounts). `autoapply requeue --lane stuck` retries the ones\n"
              "that stopped at sign-in.")
    else:
        print("\nCreate an account: `autoapply workday open <company>` (Create Account is filled in for you).\n"
              "Then `autoapply requeue --lane stuck` retries the ones that stopped at sign-in.")
    return 0


async def fill_create_account(page, email: str, password: str) -> None:
    """Workday sign-in page -> Create Account, with the user's email and password typed in.
    The final click (and the terms box) stay with the user."""
    from .browser import workday as wd
    await wd.wait_state(page, 60)
    if await wd._visible(page, wd.EMAIL_SIGN_IN):
        await page.locator(wd.EMAIL_SIGN_IN).first.click()
    link = page.locator('[data-automation-id="createAccountLink"]').first
    await link.wait_for(state="visible", timeout=15000)
    await link.click()
    await page.locator(wd.VERIFY_PASSWORD).first.wait_for(state="visible", timeout=15000)
    await page.wait_for_timeout(2500)              # typing before the form is wired up is dropped
    await page.locator(wd.EMAIL).first.fill(email)
    await page.locator(wd.PASSWORD).first.fill(password)
    await page.locator(wd.VERIFY_PASSWORD).first.fill(password)


def _open(ctx, company: str | None) -> int:
    if not company:
        print("Which company? e.g. `autoapply workday open nvidia` (names from `autoapply workday`).")
        return 1
    key = next((r.posting_key for r in ctx.ledger.postings_in(WAITING)
                if r.posting_key.startswith("workday:") and account_key(r.posting_key).partition("@")[0]
                == company.lower()), None)
    if key is None:
        print(f"No waiting Workday posting at {company!r}.")
        return 1
    from .cli import RunLock
    from .setup_wizard import IO, login_browser
    url = application_url(parse_key(key))
    print(f"Opening {url}")
    email, pw = ctx.secrets.get("WORKDAY_EMAIL"), ctx.secrets.get("WORKDAY_PASSWORD")
    if not email or not pw:
        print("Set your Workday login first: `autoapply workday login`.")
        return 1
    b = ctx.profile.browser
    with RunLock(ctx.paths.lock):
        # without Simplify: its "Create Account & Autofill" button would make up its own password
        login_browser(ctx.paths, IO(), url=url, executable_path=b.executable_path, extension=False,
                      prepare=lambda page: fill_create_account(page, email, pw),
                      note="  In the browser window, Create Account is filled in with your Workday email and\n"
                           "  password. Tick the agreement box, click Create Account, and finish any email\n"
                           "  verification Workday sends. Use only this password for Workday accounts.")
    return 0
