"""`autoapply workday`: the Workday login the tool signs in with, and which companies
still need an account. The tool never creates accounts: you do, once per company,
with the same email and password."""
from __future__ import annotations

import getpass
from collections import Counter, defaultdict

from .ats import application_url, parse_key
from .browser.workday import accounts, account_key
from .config import save_secret

WAITING = ("scored", "held", "enriched", "discovered", "needs_input", "ready", "stuck")
STATUS = {"ok": "signed in fine", "no_account": "sign-in failed - create an account",
          "verify_email": "verify your email address"}


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
    print("\nCreate an account: `autoapply workday open <company>`, choose Create Account, and use\n"
          "your Workday email and password. Then `autoapply requeue --lane stuck` retries the ones\n"
          "that stopped at sign-in.")
    return 0


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
    from .browser.simplify import extension_version
    from .cli import RunLock
    from .setup_wizard import IO, login_browser
    url = application_url(parse_key(key))
    print(f"Opening {url}")
    if extension_version(ctx.paths.extension) is None:
        print("(Simplify is not installed in the tool's browser; that is fine for creating the account.)")
    b = ctx.profile.browser
    with RunLock(ctx.paths.lock):
        login_browser(ctx.paths, IO(), url=url, executable_path=b.executable_path, consent=b.simplify_consent,
                      note="  In the browser window: Sign in with email -> Create Account, using your Workday\n"
                           "  email and password. Finish any email verification Workday asks for.")
    return 0
