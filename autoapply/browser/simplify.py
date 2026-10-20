"""Everything that knows about the Simplify Copilot extension lives in this file.

Selectors VERIFIED 2026-10-20 against Simplify Copilot 3.1.10 (content script
source + a live load in Playwright Chromium 141): the panel renders inside an
open shadow root on `.simplify-jobs-shadow-root`; Playwright's CSS engine
pierces open shadow roots, so the ids below are addressable directly. When an
extension update breaks autofill, this is the one file to fix.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .fields import wait_stable

log = logging.getLogger(__name__)

EXTENSION_ID = "pbanhockgagggenencehbnadejlgchfc"     # Chrome Web Store id
HOST = ".simplify-jobs-shadow-root"
FILL_BUTTON = "#fill-button"                 # aria-label "Autofill this page"
LOGIN_BUTTON = "#login-button"               # "Log In to Autofill" when signed out
ONBOARDING_BUTTON = "#onboarding-button"     # profile not finished on simplify.jobs
FILLING_TEXT = ("Autofilling",)              # fill button text while running
CONSENT_TEXT = "Before you autofill"         # first-run data-consent card
LOGIN_URL = "https://simplify.jobs/auth/login"


class SimplifyError(Exception):
    """Autofill could not run. `kind` is a short code for the Stuck reason."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def extension_version(ext_dir: Path) -> str | None:
    try:
        return json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


async def panel_state(page, timeout_s: float = 20) -> str:
    """'ready' | 'logged_out' | 'onboarding' | 'consent' | 'missing'."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await page.locator(FILL_BUTTON).count():
            if await _has_text(page, CONSENT_TEXT):
                return "consent"
            return "ready"
        if await page.locator(LOGIN_BUTTON).count():
            return "logged_out"
        if await page.locator(ONBOARDING_BUTTON).count():
            return "onboarding"
        await asyncio.sleep(0.5)
    return "missing"


async def _has_text(page, text: str) -> bool:
    return bool(await page.evaluate(
        """([host, text]) => [...document.querySelectorAll(host)]
             .some(h => h.shadowRoot && h.shadowRoot.textContent.includes(text))""", [HOST, text]))


async def _busy(page) -> bool:
    btn = page.locator(FILL_BUTTON)
    if not await btn.count():
        return False
    try:
        t = (await btn.first.inner_text(timeout=1000)).strip()
        disabled = await btn.first.is_disabled(timeout=1000)
    except Exception:  # noqa: BLE001
        return False
    return disabled or any(t.startswith(x) for x in FILLING_TEXT)


async def autofill(page, hints: dict, timeout_s: float = 45) -> dict[str, str]:
    """Click Simplify's autofill and wait until the form stops changing.

    Returns the field snapshot afterwards. Raises SimplifyError (-> Stuck).
    """
    state = await panel_state(page, timeout_s=min(20, timeout_s))
    if state == "missing":
        raise SimplifyError("simplify_missing", "Simplify widget did not appear on the form")
    if state == "logged_out":
        raise SimplifyError("simplify_logged_out", "Simplify is logged out in the tool's browser")
    if state == "onboarding":
        raise SimplifyError("simplify_onboarding", "Simplify profile onboarding is not finished")
    if state == "consent":
        raise SimplifyError("simplify_consent",
                            "Simplify is asking for one-time data consent - accept it in the tool's browser")
    btn = page.locator(FILL_BUTTON).first
    try:
        await btn.evaluate("el => el.click()")   # works while the panel is collapsed, too
    except Exception as e:  # noqa: BLE001
        raise SimplifyError("simplify_click_failed", f"could not click Simplify autofill: {e}") from e
    await asyncio.sleep(1.5)
    return await wait_stable(page, hints, quiet_s=2.0, timeout_s=timeout_s, busy=lambda: _busy(page))


async def health_probe(ctx, rep) -> None:
    """§15 checks 2 and 3: extension present, loads, logged in; version change noted."""
    from .launch import BrowserSession
    ext = ctx.paths.extension
    version = extension_version(ext)
    if version is None:
        rep.add("simplify", False, f"extension not found at {ext} - run `autoapply setup`")
        return
    last_good = ctx.state_get("simplify_last_good_version")
    ctx.simplify_probe_pending = bool(last_good and last_good != version)
    note = f" (changed from {last_good}: the first form of this run is a probe)" if ctx.simplify_probe_pending else ""
    try:
        async def go():
            async with BrowserSession(ctx) as bs:
                if not bs.extension_loaded():
                    return "not_loaded"
                page = await bs.context.new_page()
                try:
                    await page.goto(probe_url(ctx), wait_until="domcontentloaded", timeout=45000)
                    return await panel_state(page, timeout_s=25)
                finally:
                    await page.close()
        state = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        rep.add("simplify", False, f"browser failed to start: {type(e).__name__}: {e}")
        return
    msgs = {
        "ready": (True, f"Simplify {version} loaded and logged in{note}"),
        "not_loaded": (False, "Chromium started but the Simplify extension did not load"),
        "logged_out": (False, "Simplify is logged out - run `autoapply setup` (step: Simplify login)"),
        "onboarding": (False, "finish your Simplify profile at simplify.jobs, then retry"),
        "consent": (False, "Simplify wants one-time data consent: open the tool's browser "
                           "(`autoapply setup`, Simplify step) and click Accept"),
        "missing": (False, "Simplify panel did not appear on a Greenhouse page"),
    }
    ok, msg = msgs[state]
    rep.add("simplify", ok, msg)


def probe_url(ctx) -> str:
    """A live Greenhouse job page, only to see whether the panel renders on it."""
    from ..ats.detect import application_url, parse_key
    row = next(iter(ctx.ledger.postings_in(["scored", "enriched"])), None)
    if row and row.posting_key.startswith("greenhouse:"):
        return application_url(parse_key(row.posting_key))
    import requests
    try:
        jobs = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{PROBE_BOARD}/jobs",
                            timeout=20).json().get("jobs") or []
        if jobs:
            return f"https://job-boards.greenhouse.io/{PROBE_BOARD}/jobs/{jobs[0]['id']}"
    except (requests.RequestException, ValueError):
        pass
    return f"https://job-boards.greenhouse.io/{PROBE_BOARD}"


PROBE_BOARD = "anthropic"
