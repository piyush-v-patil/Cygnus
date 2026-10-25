"""`autoapply mail`: the mailbox Cygnus reads security codes and Workday links from."""
from __future__ import annotations

import getpass
import re
from datetime import timedelta

from .config import save_secret
from .mailbox import MailError, Mailbox, now_utc, sender, subject

RELEVANT = re.compile(r"greenhouse|ashby|workday", re.I)


def mail_command(ctx, action: str) -> int:
    if action == "login":
        return _login(ctx)
    if action == "recent":
        return _recent(ctx)
    return _test(ctx)


def set_mail_enabled(text: str, on: bool = True) -> str:
    """profile.yaml with mail.enabled set, comments and everything else kept."""
    val = "true" if on else "false"
    pat = re.compile(r"(^mail:[ \t]*(?:#.*)?\n(?:[ \t]+.*\n|[ \t]*\n)*?[ \t]+enabled:[ \t]*)(\w+)", re.M)
    if pat.search(text):
        return pat.sub(lambda m: m.group(1) + val, text, count=1)
    block = re.search(r"^mail:[ \t]*(#.*)?\n([ \t]+)\S", text, re.M)
    if block:
        at = text.index("\n", block.start()) + 1
        return text[:at] + f"{block.group(2)}enabled: {val}\n" + text[at:]
    return text.rstrip("\n") + f"\nmail:\n  enabled: {val}\n"


def _login(ctx) -> int:
    print("Cygnus can read the emails sites send right after it acts: Greenhouse/Ashby security\n"
          "codes and Workday verify / password-reset links. It reads only those, only mail that\n"
          "arrived after it acted, and never marks anything read or deletes it.\n\n"
          "Gmail: turn on 2-Step Verification, then create an app password at\n"
          "https://myaccount.google.com/apppasswords (name it Cygnus). Paste it below.")
    current = ctx.secrets.get("MAIL_ADDRESS") or ctx.secrets.get("WORKDAY_EMAIL") or ""
    address = input(f"Email address{f' [{current}]' if current else ''}: ").strip() or current
    if "@" not in address:
        print("No valid address given; nothing saved.")
        return 1
    pw = getpass.getpass("App password (16 letters, typing is hidden): ").replace(" ", "")
    if not pw:
        print("No app password given; nothing saved.")
        return 1
    try:
        print(Mailbox(ctx.profile.mail.imap_host, address, pw).check())
    except MailError as e:
        print(f"That did not work: {e}\nCheck the app password (not your normal Gmail password).")
        return 1
    save_secret(ctx.paths, "MAIL_ADDRESS", address)
    save_secret(ctx.paths, "MAIL_APP_PASSWORD", pw)
    text = ctx.paths.profile.read_text(encoding="utf-8-sig")
    ctx.paths.profile.write_text(set_mail_enabled(text, True), encoding="utf-8")
    print(f"Saved in {ctx.paths.secrets} (hidden in logs) and turned on mail.enabled in profile.yaml.")
    return 0


def _box(ctx) -> Mailbox | None:
    box = ctx.mailbox
    if box is None:
        print("Mail is off or not set up: run `autoapply mail login`.")
    return box


def _test(ctx) -> int:
    box = _box(ctx)
    if box is None:
        return 1
    try:
        print(box.check())
    except MailError as e:
        print(str(e))
        return 1
    return 0


def _recent(ctx) -> int:
    """Sender and subject (never the body) of the last few days' Greenhouse/Ashby/Workday mail."""
    box = _box(ctx)
    if box is None:
        return 1
    try:
        msgs = box.recent(now_utc() - timedelta(days=3))
    except MailError as e:
        print(str(e))
        return 1
    rows = [(str(m.get("Date", ""))[:31], sender(m)[:50], subject(m)[:70]) for m in msgs
            if RELEVANT.search(sender(m) + " " + subject(m))]
    if not rows:
        print("No Greenhouse, Ashby or Workday email in the last 3 days.")
    for d, f, s in rows[-25:]:
        print(f"{d:<31}  {f:<50}  {s}")
    return 0
