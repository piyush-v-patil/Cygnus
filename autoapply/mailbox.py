"""Reading the emails a site just sent: Greenhouse/Ashby security codes, Workday's
verify and password-reset links.

Read-only IMAP with a Gmail app password (MAIL_ADDRESS / MAIL_APP_PASSWORD in
secrets.env; `autoapply mail login`). Messages are fetched with BODY.PEEK, so
nothing is marked read, moved or deleted. Only mail that arrived after the action
being waited for is looked at, and a link or code is never logged.
"""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SKEW = timedelta(seconds=60)          # mail servers' clocks and Date headers drift a little
MAX_FETCH = 40                        # newest messages looked at per poll


class MailError(Exception):
    """The mailbox could not be reached (wrong app password, IMAP off, no network)."""


def _when(msg: EmailMessage) -> datetime | None:
    try:
        d = parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def text_of(msg: EmailMessage) -> str:
    """The message as plain text (the HTML part with tags removed when there is no text part)."""
    plain, rich = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content()
        except Exception:  # noqa: BLE001 - odd charsets
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        (plain if ctype == "text/plain" else rich).append(body)
    if plain:
        return "\n".join(plain)
    out = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(rich))
    out = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", out)
    return html.unescape(re.sub(r"<[^>]+>", " ", out))


def links_of(msg: EmailMessage) -> list[str]:
    raw = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                raw.append(part.get_content())
            except Exception:  # noqa: BLE001
                raw.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
    found = re.findall(r"""href=["']([^"']+)["']""", "\n".join(raw), re.I)
    found += re.findall(r"https?://[^\s<>\"')\]]+", "\n".join(raw))
    return list(dict.fromkeys(html.unescape(u).strip() for u in found if u.lower().startswith("http")))


def sender(msg: EmailMessage) -> str:
    return str(msg.get("From", ""))


def subject(msg: EmailMessage) -> str:
    return " ".join(str(msg.get("Subject", "")).split())


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ------------------------------------------------------------- extractors --
CODE_SENDERS = re.compile(r"greenhouse|ashby", re.I)
CODE_HINT = re.compile(r"(security|verification|confirmation|one[- ]time|access)\s+code|\bcode\b", re.I)


def security_code(msg: EmailMessage, company: str = "", length: int | None = None) -> str | None:
    """The code in a Greenhouse/Ashby "your security code" email, or None. With `length`
    (the boxes on the page) only a token of that length counts."""
    if not CODE_SENDERS.search(sender(msg)) and not CODE_HINT.search(subject(msg)):
        return None
    body = text_of(msg)
    if not CODE_HINT.search(subject(msg) + "\n" + body):
        return None
    if company and _norm(company) not in _norm(subject(msg) + " " + body) and not CODE_SENDERS.search(sender(msg)):
        return None
    size = f"{{{length}}}" if length else "{4,10}"
    # the token right after the code wording ("... on your application: ABCD1234"), else a line of its own
    for pat in (rf"code[^\n:]{{0,80}}[:\-]\s*([A-Za-z0-9]{size})\b", rf"(?m)^\s*([A-Za-z0-9]{size})\s*$"):
        for tok in re.findall(pat, body, re.I):
            if re.search(r"\d", tok) or tok.isupper():      # not an ordinary word
                return tok
    return None


VERIFY_PATH = re.compile(r"verif|activat|confirm", re.I)
RESET_PATH = re.compile(r"reset|password|forgot|recover", re.I)


def workday_link(msg: EmailMessage, tenant: str, host: str, kind: str) -> str | None:
    """A verify (kind="verify") or password-reset (kind="reset") link for this company's
    Workday: on its own host, or a Workday host that names the company."""
    want = VERIFY_PATH if kind == "verify" else RESET_PATH
    for url in links_of(msg):
        parts = urlsplit(url)
        h = (parts.hostname or "").lower()
        if not (h == host.lower() or (re.search(r"myworkday(jobs|site)?\.com$", h) and tenant.lower() in (h + parts.path).lower())):
            continue
        if want.search(parts.path + "?" + parts.query) and not (kind == "verify" and RESET_PATH.search(parts.path)):
            return url
    return None


# ---------------------------------------------------------------- mailbox --
class Mailbox:
    """`fetch` (tests) replaces IMAP: since -> messages."""

    def __init__(self, host: str, address: str, password: str,
                 fetch: Callable[[datetime], list[EmailMessage]] | None = None, poll_s: float = 5.0):
        self.host, self.address, self.password = host, address, password.replace(" ", "")
        self._fetch = fetch
        self.poll_s = poll_s

    def _connect(self):
        try:
            m = imaplib.IMAP4_SSL(self.host, timeout=30)
            m.login(self.address, self.password)
            m.select("INBOX", readonly=True)
            return m
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(f"could not open {self.address}'s mailbox at {self.host}: {e}") from e

    @staticmethod
    def _messages(m, since: datetime) -> list[EmailMessage]:
        typ, data = m.search(None, "SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y"))
        if typ != "OK":
            return []
        out = []
        for num in (data[0] or b"").split()[-MAX_FETCH:]:
            typ, d = m.fetch(num, "(BODY.PEEK[])")
            if typ == "OK" and d and isinstance(d[0], tuple):
                out.append(email.message_from_bytes(d[0][1], policy=email.policy.default))
        return out

    def check(self) -> str:
        """Log in and out; raises MailError."""
        if self._fetch is not None:
            return "test mailbox"
        m = self._connect()
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
        return f"{self.address}: mailbox reachable (read-only)"

    def recent(self, since: datetime) -> list[EmailMessage]:
        if self._fetch is not None:
            return self._fetch(since)
        m = self._connect()
        try:
            return self._messages(m, since)
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def wait_for(self, match: Callable[[EmailMessage], str | None], since: datetime, timeout_s: float,
                 what: str = "the email") -> str | None:
        """Poll until `match` returns something for a message that arrived after `since`.
        Blocking: call it with asyncio.to_thread. None after `timeout_s`."""
        deadline = time.monotonic() + timeout_s
        m = None if self._fetch is not None else self._connect()
        seen: set[str] = set()
        try:
            while True:
                msgs = self._fetch(since) if m is None else self._messages(m, since)
                for msg in reversed(msgs):                       # newest first
                    when = _when(msg)
                    mid = str(msg.get("Message-ID") or id(msg))
                    if when is None or when < since - SKEW or mid in seen:
                        continue
                    seen.add(mid)
                    got = match(msg)
                    if got:
                        log.info("    found %s (from %s, %r)", what, sender(msg)[:80], subject(msg)[:80])
                        return got
                    log.debug("    not %s: from %s, %r", what, sender(msg)[:80], subject(msg)[:80])
                if time.monotonic() >= deadline:
                    return None
                time.sleep(self.poll_s)
                if m is not None:
                    m.noop()
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(f"reading the mailbox failed: {e}") from e
        finally:
            if m is not None:
                try:
                    m.logout()
                except Exception:  # noqa: BLE001
                    pass


def mailbox_for(ctx) -> Mailbox | None:
    """The configured mailbox, or None when mail is off or its login is missing."""
    cfg = ctx.profile.mail
    if not cfg.enabled:
        return None
    fetch = getattr(ctx, "mail_fetch", None)
    address = ctx.secrets.get("MAIL_ADDRESS") or ctx.secrets.get("WORKDAY_EMAIL")
    password = ctx.secrets.get("MAIL_APP_PASSWORD")
    if fetch is None and not (address and password):
        log.warning("mail.enabled is on but MAIL_APP_PASSWORD is missing: run `autoapply mail login`")
        return None
    return Mailbox(cfg.imap_host, address or "", password or "", fetch=fetch,
                   poll_s=getattr(ctx, "mail_poll_s", 5.0))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
