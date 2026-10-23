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
import re
from pathlib import Path

from .fields import read_values, safe_evaluate, wait_stable

log = logging.getLogger(__name__)

EXTENSION_ID = "pbanhockgagggenencehbnadejlgchfc"     # Chrome Web Store id
HOST = ".simplify-jobs-shadow-root"
FILL_BUTTON = "#fill-button"                 # aria-label "Autofill this page"
LOGIN_BUTTON = "#login-button"               # "Log In to Autofill" when signed out
ONBOARDING_BUTTON = "#onboarding-button"     # profile not finished on simplify.jobs
FILLING_TEXT = ("Autofilling",)              # fill button text while running
START_TIMEOUT_S = 8.0                         # a click that shows no sign of filling by then is retried
FETCHING_TEXT = "Fetching profile"           # #login-button text while the profile loads (not logged out)
CONSENT_TEXT = "Before you autofill"         # first-run data-consent card
LOGIN_URL = "https://simplify.jobs/auth/login"
# Simplify's automation bridge: its content script answers these window messages when
# navigator.webdriver is true (as under Playwright). VERIFIED 2026-10-23 in the 3.1.10
# content script. getFillStatus replies once its fillStatus is final or the wait ends.
BRIDGE_REQUEST = "__SIMPLIFY_AUTOMATION_REQUEST__"
BRIDGE_RESPONSE = "__SIMPLIFY_AUTOMATION_RESPONSE__"
FILL_DONE = ("filled", "error", "paused")        # final for one page
FILL_RUNNING = ("filling", "submitting")
# Workday "My Experience": the fill waits at "Last step! Select which skills to autofill."
# until a panel button "Autofill N Skills" is clicked (3.1.10; no id on the button)
SKILLS_TEXT = "Select which skills to autofill"
SKILLS_BUTTON = re.compile(r"^\s*Autofill \d+ Skills?\s*$")
STOP_BUTTON = '[aria-label="Stop autofill"]'


CONSENT_KEY = "simplify_data_consent"   # chrome.storage.local, read by the "Before you autofill" card


def consent_value(choice: str) -> dict:
    """What Simplify's own buttons store: Accept -> all true, Essentials only -> analytics off."""
    on = choice == "accept"
    return {"privacy": True, "userEvents": on, "analyticsEvents": on, "debugging": on}


async def apply_consent(context, choice: str, timeout_s: float = 10) -> bool:
    """Re-apply the user's data-consent choice after Simplify resets it.

    An extension loaded with --load-extension is installed afresh on every
    browser start, so Simplify's onInstalled("install") handler sets the
    consent to {} each time and the card would ask again on every launch.
    VERIFIED 2026-10-21 against Simplify Copilot 3.1.10. Returns True when set.
    """
    workers = [w for w in context.service_workers if w.url.startswith("chrome-extension://")]
    if not workers:
        return False
    sw = workers[0]
    value = consent_value(choice)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    # wait for the install handler's reset to land, so it cannot overwrite ours
    while loop.time() < deadline:
        cur = await sw.evaluate(f"chrome.storage.local.get('{CONSENT_KEY}')")
        if CONSENT_KEY in (cur or {}):
            break
        await asyncio.sleep(0.25)
    for _ in range(3):
        await sw.evaluate("v => chrome.storage.local.set({%s: v})" % json.dumps(CONSENT_KEY), value)
        await asyncio.sleep(0.5)
        cur = await sw.evaluate(f"chrome.storage.local.get('{CONSENT_KEY}')")
        if (cur or {}).get(CONSENT_KEY) == value:
            return True
    log.warning("could not re-apply Simplify's data-consent choice")
    return False


SIGNUP_KEY = "simplifySignupCredentials"   # {email, password, updatedAt}: what Simplify's
                                           # "Create Account & Autofill" made up and used (3.1.10)


async def signup_credentials(context) -> dict | None:
    """The email + password Simplify generated when it created job-site accounts, if any."""
    workers = [w for w in context.service_workers if w.url.startswith("chrome-extension://")]
    if not workers:
        return None
    try:
        cur = await workers[0].evaluate(f"chrome.storage.local.get('{SIGNUP_KEY}')")
    except Exception:  # noqa: BLE001 - the worker can be restarting
        return None
    v = (cur or {}).get(SIGNUP_KEY) or {}
    return v if v.get("email") and v.get("password") else None


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
            try:
                text = await page.locator(LOGIN_BUTTON).first.inner_text(timeout=1000)
            except Exception:  # noqa: BLE001
                text = ""
            if FETCHING_TEXT.lower() not in text.lower():
                return "logged_out"
            # still loading the profile: keep waiting
        if await page.locator(ONBOARDING_BUTTON).count():
            return "onboarding"
        await asyncio.sleep(0.5)
    return "missing"


async def _has_text(page, text: str) -> bool:
    return bool(await safe_evaluate(page,
        """([host, text]) => [...document.querySelectorAll(host)]
             .some(h => h.shadowRoot && h.shadowRoot.textContent.includes(text))""", [HOST, text]))


_BRIDGE_JS = """([req, resp, id, ms, grace]) => new Promise(resolve => {
  const finish = v => { window.removeEventListener('message', on); clearTimeout(t); resolve(v); };
  const on = e => { const d = e.data;
    if (d && d.type === resp && d.id === id) finish(d.result ? (d.result.fillStatus || 'none') : 'none'); };
  const t = setTimeout(() => finish(null), ms + grace);
  window.addEventListener('message', on);
  window.postMessage({type: req, id, method: 'getFillStatus', args: [ms]}, '*');
})"""
_bridge_ids = iter(range(1, 1 << 62))


async def fill_status(page, wait_s: float = 0) -> str | None:
    """Simplify's own fill status for the page ("ready", "filling", "filled", "error",
    "paused", ... or "none" before its first fill), waiting up to `wait_s` for it to
    become final. None when the bridge does not answer (older/newer Simplify, not
    loaded yet, page navigating): callers then fall back to the panel button."""
    ms = int(max(0.0, wait_s) * 1000)
    try:
        # the bridge answers a 0 ms question at once, so a silent page is told apart quickly
        grace = 400 if ms == 0 else 2500
        return await safe_evaluate(page, _BRIDGE_JS,
                                   [BRIDGE_REQUEST, BRIDGE_RESPONSE, f"cygnus-{next(_bridge_ids)}", ms, grace])
    except Exception:  # noqa: BLE001 - a closed or navigating page means "no answer"
        return None


async def _busy(page) -> bool:
    if await fill_status(page) in FILL_RUNNING:
        return True
    btn = page.locator(FILL_BUTTON)
    if not await btn.count():
        return False
    try:
        t = (await btn.first.inner_text(timeout=1000)).strip()
        disabled = await btn.first.is_disabled(timeout=1000)
    except Exception:  # noqa: BLE001
        return False
    return disabled or any(t.startswith(x) for x in FILLING_TEXT)


async def autofill(page, hints: dict, timeout_s: float = 45, require_fill: bool = True) -> dict[str, str]:
    """Click Simplify's autofill and wait until the form stops changing.

    Returns the field snapshot afterwards. Raises SimplifyError (-> Stuck).
    `require_fill=False` accepts a page Simplify has nothing for (later Workday pages).
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
                            "Simplify's data-consent card is still showing (the tool's automatic answer did not stick)")
    start = await read_values(page, hints)
    if len(_filled(start)) >= 2:
        # Simplify can autofill on page load by itself; one more click is harmless
        log.debug("Simplify already filled %d fields on load", len(_filled(start)))
    for attempt in range(3):
        # the button stays disabled while Simplify fetches the profile; a click on a
        # disabled button does nothing, so wait for it to become clickable first
        if not await _wait_enabled(page, timeout_s=min(30.0, timeout_s)):
            raise SimplifyError("simplify_not_ready",
                                "Simplify's Autofill button stayed disabled (its profile did not load)")
        try:
            await page.locator(FILL_BUTTON).first.evaluate("el => el.click()")   # works while collapsed too
        except Exception as e:  # noqa: BLE001
            raise SimplifyError("simplify_click_failed", f"could not click Simplify autofill: {e}") from e
        if await _wait_started(page, hints, start, timeout_s=START_TIMEOUT_S):
            break
        log.info("  Simplify did not start filling; clicking Autofill again (%d of 3)", attempt + 2)
    after = await _wait_done(page, hints, timeout_s)
    if require_fill and not (_filled(after) - _filled(start)) and len(_filled(start)) < 2:
        raise SimplifyError("simplify_did_not_fill", "Simplify's autofill filled no field on this form")
    return after


def _filled(snapshot: dict[str, str]) -> set[str]:
    return {k for k, v in snapshot.items() if (v or "").strip()}


async def _wait_done(page, hints: dict, timeout_s: float) -> dict[str, str]:
    """Wait for Simplify to finish the page: its own status when the bridge answers,
    otherwise (or as well) until the form stops changing with the button idle."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    status = await fill_status(page)                # None: no bridge, the button and the form decide
    while status is not None and status not in FILL_DONE and loop.time() < deadline:
        await answer_manual_step(page)
        status = await fill_status(page, wait_s=min(3.0, max(0.0, deadline - loop.time())))
    if status in FILL_DONE:
        log.debug("Simplify reports %s", status)
    left = max(5.0, deadline - loop.time())
    return await wait_stable(page, hints, quiet_s=1.5, timeout_s=left, busy=lambda: _busy(page))


async def answer_manual_step(page) -> bool:
    """Get Simplify past a step that waits for a click (Workday skills). True if it acted."""
    if not await _has_text(page, SKILLS_TEXT):
        return False
    btn = page.locator("button").filter(has_text=SKILLS_BUTTON)
    try:
        if await btn.count() and not await btn.first.is_disabled(timeout=1000):
            log.info("    letting Simplify fill the skills it picked")
            await btn.first.evaluate("el => el.click()")
            return True
        stop = page.locator(STOP_BUTTON)
        if await stop.count():
            log.info("    no skills to fill; stopping Simplify's skills step")
            await stop.first.evaluate("el => el.click()")
            return True
    except Exception as e:  # noqa: BLE001 - the panel re-renders while being read
        log.debug("skills step: %s", e)
    return False


async def wait_for_fill(page, hints: dict, timeout_s: float) -> dict[str, str]:
    """Wait for a fill Simplify started by itself (its continuous multi-page mode)."""
    return await _wait_done(page, hints, timeout_s)


async def _wait_enabled(page, timeout_s: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        btn = page.locator(FILL_BUTTON)
        try:
            if await btn.count() and not await btn.first.is_disabled(timeout=1000):
                return True
        except Exception:  # noqa: BLE001 - the panel re-renders while loading
            pass
        await asyncio.sleep(0.5)
    return False


async def _wait_started(page, hints: dict, start: dict[str, str], timeout_s: float) -> bool:
    """True once Simplify shows it is filling, or any field gained a value."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await _busy(page):
            return True
        if _filled(await read_values(page, hints)) - _filled(start):
            return True
        await asyncio.sleep(0.5)
    return False


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
        "logged_out": (False, "Simplify is logged out - run `autoapply browser` and log in"),
        "onboarding": (False, "finish your Simplify profile at simplify.jobs, then retry"),
        "consent": (False, "Simplify's data-consent card is still showing although the tool answers it "
                           "on every launch - Simplify may have changed; see browser/simplify.py apply_consent"),
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
