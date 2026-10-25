"""Pre-run health check (§15). Any failure stops the run before a job is touched."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from .config import ConfigError
from .context import Context
from .github import GitHub, GitHubError

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    message: str
    warn_only: bool = False       # shown, but does not stop anything


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    soft: bool = False            # while set, failures are warnings (a review session's extras)

    def add(self, name: str, ok: bool, message: str, warn_only: bool = False) -> bool:
        warn_only = warn_only or self.soft
        self.checks.append(Check(name, ok, message, warn_only))
        (log.info if ok else log.warning if warn_only else log.error)(
            "health %s: %s - %s", name, "ok" if ok else "WARN" if warn_only else "FAIL", message)
        return ok

    @property
    def ok(self) -> bool:
        return all(c.ok or c.warn_only for c in self.checks)

    @property
    def warnings(self) -> list[str]:
        return [f"{c.name}: {c.message}" for c in self.checks if not c.ok and c.warn_only]

    def first_failure(self) -> str:
        c = next((c for c in self.checks if not c.ok and not c.warn_only), None)
        return f"{c.name}: {c.message}" if c else ""

    def render(self) -> str:
        return "\n".join(f"[{'PASS' if c.ok else 'WARN' if c.warn_only else 'FAIL'}] {c.name}: {c.message}"
                         for c in self.checks)


def run_health(ctx: Context, probe_browser: bool = True, review: bool = False) -> Report:
    rep = Report()
    # 1. profile + visa confirmation
    try:
        profile = ctx.profile
    except ConfigError as e:
        rep.add("profile", False, str(e))
        return rep
    rep.add("profile", True, str(ctx.paths.profile))
    rep.add("visa", profile.visa.confirmed,
            "visa answers confirmed" if profile.visa.confirmed else
            "visa answers not confirmed - run `autoapply setup` and confirm them")
    probs = ctx.resumes.check()
    rep.add("resumes", not probs, "; ".join(probs) or f"{len(ctx.resumes.all_files())} resume(s) readable")

    # 4-6 matter to a review session only as warnings: approving replays saved answers
    # (no Claude), and write-back and Discord failures are retried later
    rep.soft = review
    # 4. Anthropic key + budget + pricing
    _check_llm(ctx, rep)

    # 5. GitHub write-back
    src = profile.writeback_source
    if src is not None:
        _check_github(ctx, src, rep)

    # 6. Discord
    if profile.notifications.discord:
        url = ctx.secrets.get("DISCORD_WEBHOOK_URL")
        if not url:
            rep.add("discord", False, "notifications.discord is on but DISCORD_WEBHOOK_URL is missing")
        else:
            try:
                r = requests.get(url, timeout=15)
                rep.add("discord", r.status_code == 200, "webhook reachable" if r.status_code == 200
                        else f"webhook answered HTTP {r.status_code} (deleted or wrong URL?)")
            except requests.RequestException as e:
                rep.add("discord", False, f"webhook unreachable: {type(e).__name__}")

    # mail: codes and Workday links fall back to you when it does not work
    if profile.mail.enabled:
        from .mailbox import MailError
        box = ctx.mailbox
        if box is None:
            rep.add("mail", False, "mail.enabled is on but there is no app password: run `autoapply mail login`",
                    warn_only=True)
        else:
            try:
                rep.add("mail", True, box.check())
            except MailError as e:
                rep.add("mail", False, f"{e} - codes and Workday links will wait for you", warn_only=True)
    rep.soft = False

    # 7. job sources (skipped for review sessions: they do not read sources)
    if not review:
        for s in ctx.sources:
            try:
                rep.add(f"source {s.name}", True, s.check())
            except Exception as e:  # noqa: BLE001 - any failure here is a failed check
                rep.add(f"source {s.name}", False, str(e))

    # 2/3. Simplify extension, login, version
    if probe_browser and rep.ok:
        from .browser.simplify import health_probe
        health_probe(ctx, rep)
    return rep


def _check_llm(ctx: Context, rep: Report):
    profile = ctx.profile
    if not ctx.secrets.get("ANTHROPIC_API_KEY") and ctx._llm_transport is None:
        rep.add("anthropic", False, "ANTHROPIC_API_KEY is missing from secrets.env")
        return
    try:
        llm = ctx.llm
    except Exception as e:  # noqa: BLE001
        rep.add("anthropic", False, str(e))
        return
    for m in {profile.llm.score_model, profile.llm.write_model}:
        if not llm.has_price(m):
            rep.add("pricing", False, f"no price known for {m}: it is counted at the highest known "
                    f"rate until you set llm.pricing.{m} in profile.yaml", warn_only=True)
    ping = getattr(llm.transport, "ping", None)
    if ping is not None:
        try:
            ping(profile.llm.score_model)
            ping(profile.llm.write_model)
            rep.add("anthropic", True, "API key valid, models available")
        except Exception as e:  # noqa: BLE001
            rep.add("anthropic", False, f"API check failed: {type(e).__name__}: {e}")
            return
    spend, budget = llm.month_spend(), profile.llm.monthly_budget_usd
    rep.add("budget", spend < budget, f"${spend:.2f} of ${budget:.2f} spent this month"
            + ("" if spend < budget else " - budget reached, raise llm.monthly_budget_usd or wait"))


def _check_github(ctx: Context, src, rep: Report):
    tok = ctx.secrets.get("GITHUB_TOKEN")
    if not tok:
        rep.add("github", False, "writeback is on but GITHUB_TOKEN is missing")
        return
    try:
        perms = GitHub(tok).repo_permissions(src.repo)
    except GitHubError as e:
        msg = "token rejected (expired?)" if e.status == 401 else str(e)
        rep.add("github", False, msg)
        return
    # fine-grained tokens do not report `permissions`; a failed PUT is caught at write time
    can_write = perms.get("push", True) if perms else True
    rep.add("github", can_write, f"{src.repo}: " + ("can write" if can_write else
            "token cannot write (needs Contents: read and write)"))
