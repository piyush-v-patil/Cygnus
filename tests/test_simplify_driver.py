"""Simplify driver against a fake panel built like the real one (§8.2.3).

Simplify 3.1.10 renders in an open shadow root; #fill-button is disabled while
the profile loads, and #login-button reads "Fetching profile..." meanwhile.
"""
import asyncio
from pathlib import Path

import pytest

from autoapply.browser import simplify
from autoapply.browser.fill import fill_many
from autoapply.browser.simplify import SimplifyError, autofill, panel_state
from autoapply.pipeline import RunStats

from .conftest import CHROMIUM

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]

FORM = """<!doctype html><html><body>
<label for=first_name>First Name*</label><input id=first_name required>
<label for=email>Email*</label><input id=email required>
<label for=why>Why us?</label><textarea id=why></textarea>
<script>
const MODE = '%s';
let status = null;
if (MODE === 'bridge') {                 // Simplify's automation bridge (getFillStatus), as in 3.1.10
  window.addEventListener('message', e => {
    const d = e.data;
    if (!d || d.type !== '__SIMPLIFY_AUTOMATION_REQUEST__' || d.method !== 'getFillStatus') return;
    const until = Date.now() + Math.min(d.args[0] || 0, 120000);
    const poll = () => (['filled', 'error', 'paused'].includes(status) || Date.now() >= until)
      ? window.postMessage({type: '__SIMPLIFY_AUTOMATION_RESPONSE__', id: d.id,
                            result: {completed: status === 'filled', fillStatus: status}}, '*')
      : setTimeout(poll, 300);
    poll();
  });
}
const host = document.createElement('div'); host.className = 'simplify-jobs-shadow-root';
document.body.appendChild(host);
const root = host.attachShadow({mode: 'open'});
const login = document.createElement('a'); login.id = 'login-button'; login.textContent = 'Fetching profile...';
root.appendChild(login);
setTimeout(() => {                       // profile loaded: the login link gives way to the button
  login.remove();
  const b = document.createElement('button'); b.id = 'fill-button'; b.textContent = 'Autofill This Page';
  b.disabled = true; root.appendChild(b);
  setTimeout(() => { b.disabled = false; }, 800);
  let clicks = 0;
  b.addEventListener('click', () => {
    clicks += 1; window.__clicks = clicks;
    if (MODE === 'broken' || (MODE === 'second' && clicks < 2)) return;
    if (MODE === 'bridge') {           // a slow fill the button text does not show; only the status does
      status = 'filling';
      setTimeout(() => { document.getElementById('first_name').value = 'Jane'; }, 500);
      setTimeout(() => { document.getElementById('email').value = 'jane@example.com'; status = 'filled'; }, 3500);
      return;
    }
    b.textContent = 'Autofilling'; b.disabled = true;
    setTimeout(() => {
      document.getElementById('first_name').value = 'Jane';
      document.getElementById('email').value = 'jane@example.com';
      b.textContent = 'Run Autofill Again'; b.disabled = false;
    }, 700);
  });
}, %d);
</script></body></html>"""


async def _run(mode: str, fetch_ms: int = 600):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROMIUM)
        page = await b.new_page()
        await page.set_content(FORM % (mode, fetch_ms))
        try:
            state = await panel_state(page, timeout_s=10)
            snap = await autofill(page, {}, timeout_s=12)
            return state, snap, await page.evaluate("window.__clicks")
        finally:
            await b.close()


def test_waits_for_profile_and_enabled_button():
    state, snap, clicks = asyncio.run(_run("ok", fetch_ms=1500))
    assert state == "ready"                       # "Fetching profile..." is not "logged out"
    assert snap["id:first_name"] == "Jane" and clicks == 1


def test_waits_for_simplify_status_not_just_a_quiet_form():
    # 3 s pass between the two fields with the button idle: only the status says it is not done
    _, snap, clicks = asyncio.run(_run("bridge"))
    assert snap["id:first_name"] == "Jane" and snap["id:email"] == "jane@example.com" and clicks == 1


def test_fill_status_is_none_without_the_bridge():
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)
            page = await b.new_page()
            await page.set_content(FORM % ("bridge", 100))
            first = await simplify.fill_status(page)
            await page.set_content("<p>no Simplify here</p>")
            t = asyncio.get_running_loop().time()
            none = await simplify.fill_status(page)
            await b.close()
            return first, none, asyncio.get_running_loop().time() - t
    first, none, took = asyncio.run(go())
    assert first == "none" and none is None and took < 2      # a silent page is told apart quickly


def test_click_retried_when_simplify_does_not_start(monkeypatch):
    monkeypatch.setattr(simplify, "START_TIMEOUT_S", 1.0)
    _, snap, clicks = asyncio.run(_run("second"))
    assert snap["id:email"] == "jane@example.com" and clicks == 2


def test_autofill_that_fills_nothing_is_an_error(monkeypatch):
    monkeypatch.setattr(simplify, "START_TIMEOUT_S", 0.5)
    with pytest.raises(SimplifyError) as e:
        asyncio.run(_run("broken"))
    assert e.value.kind == "simplify_did_not_fill"


def test_slow_form_times_out_to_stuck(make_ctx, monkeypatch):
    from .test_browser import GH, seed
    import autoapply.browser.fill as fill_mod

    async def hang(ctx, bs, key, check_consistency=False, track=None):
        track["step"] = "asking Claude about 2 blank field(s)"
        await asyncio.sleep(30)

    monkeypatch.setattr(fill_mod, "fill_one", hang)
    ctx = make_ctx()
    ctx.profile.browser.form_timeout_s = 0.5
    seed(ctx, GH)
    stats = RunStats()
    asyncio.run(fill_many(ctx, [GH], stats, session=object()))
    row = ctx.ledger.get(GH)
    assert row.state == "stuck" and "timed_out" in row.reason and "asking Claude" in row.reason
