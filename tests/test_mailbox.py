"""Reading codes and Workday links from mail (no network: messages are built here)."""
import pytest

from autoapply.mail_cmd import set_mail_enabled
from autoapply.mailbox import Mailbox, security_code, workday_link
from autoapply.mailbox import now_utc

from .mailfake import greenhouse_code, message, workday_mail


def test_greenhouse_security_code():
    assert security_code(greenhouse_code("ABCD1234")(), "Acme", 8) == "ABCD1234"
    assert security_code(greenhouse_code("ABCD1234")(), "Acme", 6) is None             # not the length asked
    other = message("News <hi@shop.example>", "Your weekly code deals", "Use code SAVE20NOW")
    assert security_code(other, "Acme", None) is None


def test_ashby_style_six_digit_code():
    m = message("Ashby <no-reply@ashbyhq.com>", "Your verification code",
                "Your verification code is: 482913\nIt expires in 15 minutes.")
    assert security_code(m, "Acme", 6) == "482913"


def test_workday_links_by_kind_and_host():
    tenant, host = "acme", "acme.wd5.myworkdayjobs.com"
    assert workday_link(workday_mail("verify")(), tenant, host, "verify").endswith("/activate/tok123")
    assert workday_link(workday_mail("reset")(), tenant, host, "reset").endswith("/passwordreset/tok456")
    assert workday_link(workday_mail("reset")(), tenant, host, "verify") is None
    assert workday_link(workday_mail("verify", host="evil.example")(), tenant, host, "verify") is None
    assert workday_link(workday_mail("verify", host="other.wd1.myworkdayjobs.com")(), tenant, host, "verify") is None


def test_wait_for_only_mail_after_the_action():
    from datetime import timedelta
    old = message("Greenhouse <no-reply@us.greenhouse-mail.io>", "Security code for your application to Acme",
                  "Copy and paste this code into the security code field on your application: OLDCODE1",
                  when=now_utc() - timedelta(hours=2))
    box = Mailbox("imap.example", "jane@example.com", "x", fetch=lambda since: [old, greenhouse_code("NEWCODE2")()],
                  poll_s=0.05)
    got = box.wait_for(lambda m: security_code(m, "Acme", 8), now_utc() - timedelta(seconds=5), 1)
    assert got == "NEWCODE2"
    assert box.wait_for(lambda m: security_code(m, "Acme", 8), now_utc() + timedelta(minutes=5), 0.2) is None


@pytest.mark.parametrize("text,want", [
    ("fit:\n  cutoff: 60\n", "fit:\n  cutoff: 60\nmail:\n  enabled: true\n"),
    ("mail:\n  enabled: false   # off for now\n  wait_s: 120\n", "mail:\n  enabled: true   # off for now\n  wait_s: 120\n"),
    ("mail:\n  wait_s: 120\n", "mail:\n  enabled: true\n  wait_s: 120\n"),
])
def test_mail_login_turns_mail_on_in_place(text, want):
    assert set_mail_enabled(text) == want


def test_mail_secrets_are_hidden_in_logs():
    from autoapply.config import SECRET_KEYS
    assert "MAIL_APP_PASSWORD" in SECRET_KEYS and "MAIL_ADDRESS" in SECRET_KEYS
