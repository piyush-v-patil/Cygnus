"""Fake mailbox messages for tests (ctx.mail_fetch)."""
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone


def message(frm: str, subject: str, text: str = "", html: str = "", when: datetime | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["Subject"] = frm, subject
    m["Date"] = format_datetime(when or datetime.now(timezone.utc))
    m["Message-ID"] = f"<{abs(hash((frm, subject, text, html)))}@test>"
    m.set_content(text or "see the HTML part")
    if html:
        m.add_alternative(html, subtype="html")
    return m


def inbox(*builders):
    """mail_fetch: every message dated now (each looks freshly arrived)."""
    return lambda since: [b() for b in builders]


def greenhouse_code(code="ABCD1234", company="Acme"):
    return lambda: message("Greenhouse <no-reply@us.greenhouse-mail.io>", f"Security code for your application to {company}",
                           f"Hi Jane,\nCopy and paste this code into the security code field on your application: {code}\n"
                           "This code expires in 10 minutes.")


def workday_mail(kind: str, host="acme.wd5.myworkdayjobs.com"):
    path = "External/activate/tok123" if kind == "verify" else "External/passwordreset/tok456"
    word = "Verify your account" if kind == "verify" else "Reset your password"
    return lambda: message("Acme <acme@myworkday.com>", word,
                           html=f'<p>{word}</p><a href="https://{host}/{path}">{word}</a>')
