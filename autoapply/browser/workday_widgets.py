"""Reading options from and filling Workday's own widgets.

Every Workday field block carries data-aa-key (workday_fields.js); the control is
found inside it. The waits follow what proved reliable for Simplify 3.1.10 on
Workday: open the listbox and wait for the active popup's options, match exact ->
starts-with -> contains, close with Escape, and confirm the button no longer
reads "Select One"; multiselects take a typed search + Enter; date boxes are
typed segment by segment; radios are re-checked because Workday can revert them.
"""
from __future__ import annotations

import logging
import re

from ..normalize import normalize_question

log = logging.getLogger(__name__)

POPUP_OPTIONS = ('[data-automation-activepopup="true"] [role="option"], [visibility="opened"] [role="option"], '
                 '[role="listbox"] [role="option"]')
PROMPT_OPTIONS = '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"]'


def _block(page, key: str):
    return page.locator(f'[data-aa-key="{key}"]').first


def _best(options: list[str], value: str) -> int | None:
    """Index of the option for `value`: exact, then starts-with, then contains (normalized)."""
    want = normalize_question(value)
    norm = [normalize_question(o) for o in options]
    for test in (lambda o: o == want, lambda o: o.startswith(want) or want.startswith(o) and o,
                 lambda o: want in o):
        for i, o in enumerate(norm):
            if o and test(o):
                return i
    return None


async def _visible_texts(page, selector: str) -> list[tuple[int, str]]:
    loc = page.locator(selector)
    out = []
    for i in range(min(await loc.count(), 400)):
        o = loc.nth(i)
        try:
            if await o.is_visible():
                out.append((i, " ".join((await o.inner_text()).split())))
        except Exception:  # noqa: BLE001 - popups re-render while being read
            continue
    return out


async def _close_popup(page) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(150)


async def select_options(page, key: str, limit: int = 200) -> list[str]:
    """Open a Workday listbox button, read its options, close it."""
    btn = _block(page, key).locator('button[aria-haspopup="listbox"]').first
    try:
        await btn.click(timeout=5000)
        await page.locator(POPUP_OPTIONS).first.wait_for(state="visible", timeout=1800)
        opts = [t for _, t in await _visible_texts(page, POPUP_OPTIONS)]
    except Exception as e:  # noqa: BLE001
        log.debug("could not read options of %s: %s", key, e)
        opts = []
    finally:
        await _close_popup(page)
    return list(dict.fromkeys(o for o in opts if o))[:limit]


async def fill_select(page, key: str, value: str) -> None:
    from .fields import FillError
    btn = _block(page, key).locator('button[aria-haspopup="listbox"]').first
    for attempt in range(2):
        await btn.click(timeout=8000)
        try:
            await page.locator(POPUP_OPTIONS).first.wait_for(state="visible", timeout=1800)
        except Exception:  # noqa: BLE001
            await _close_popup(page)
            continue
        shown = await _visible_texts(page, POPUP_OPTIONS)
        i = _best([t for _, t in shown], value)
        if i is None:
            await _close_popup(page)
            raise FillError(f"no option {value!r} (offered: {[t for _, t in shown][:15]})")
        await page.locator(POPUP_OPTIONS).nth(shown[i][0]).click(timeout=4000)
        await page.wait_for_timeout(300)
        if await btn.get_attribute("aria-expanded") == "true":
            await _close_popup(page)
        if not re.match(r"^\s*select one\s*$", await btn.inner_text(), re.I):
            return
    raise FillError(f"the dropdown did not take {value!r}")


async def fill_multi(page, key: str, value: str | list[str]) -> None:
    from .fields import FillError
    block = _block(page, key)
    wanted = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
    box = block.locator('input[placeholder="Search"], input').first
    for w in wanted:
        await box.click(timeout=8000)
        await box.fill("")
        await box.type(w[:60], delay=20)
        await page.keyboard.press("Enter")
        try:
            await page.locator(PROMPT_OPTIONS).first.wait_for(state="visible", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(500)
        shown = await _visible_texts(page, PROMPT_OPTIONS)
        i = _best([t for _, t in shown], w)
        if i is None:
            await _close_popup(page)
            raise FillError(f"no match for {w!r} in the list")
        await page.locator(PROMPT_OPTIONS).nth(shown[i][0]).click(timeout=4000)
        await page.wait_for_timeout(400)
        await _close_popup(page)
    chosen = await block.locator('[data-automation-id="selectedItem"]').count()
    if not chosen:
        raise FillError(f"nothing was selected for {value!r}")


def split_date(value: str) -> dict[str, str]:
    """'MM/YYYY', 'MM/DD/YYYY' or 'YYYY-MM-DD' -> {"Month": .., "Day": .., "Year": ..}."""
    v = value.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", v)
    if m:
        return {"Year": m.group(1), "Month": m.group(2).zfill(2), "Day": (m.group(3) or "").zfill(2) if m.group(3) else ""}
    parts = re.split(r"[/.\-\s]+", v)
    if len(parts) == 2:
        return {"Month": parts[0].zfill(2), "Year": parts[1], "Day": ""}
    if len(parts) == 3:
        return {"Month": parts[0].zfill(2), "Day": parts[1].zfill(2), "Year": parts[2]}
    return {}


async def fill_date(page, key: str, value: str) -> None:
    from .fields import FillError
    parts = split_date(str(value))
    if not parts:
        raise FillError(f"{value!r} is not a date (MM/YYYY or MM/DD/YYYY)")
    block = _block(page, key)
    for name in ("Month", "Day", "Year"):
        box = block.locator(f'input[data-automation-id="dateSection{name}-input"]')
        if not await box.count():
            continue
        if not parts.get(name):
            raise FillError(f"the date needs a {name.lower()}")
        await box.first.click(timeout=5000)
        await box.first.press("Control+A")
        await box.first.type(parts[name], delay=40)
        await page.wait_for_timeout(200)


async def fill_choice(page, key: str, kind: str, options: list[str], value: str | list[str]) -> None:
    """Radio group, checkbox group or a single checkbox inside a Workday block."""
    from .fields import FillError
    block = _block(page, key)
    if kind == "checkbox":
        want = normalize_question(str(value)) in ("yes", "true", "1", "checked", "agree", "i agree")
        box = block.locator('input[type="checkbox"]').first
        if await box.is_checked() != want:
            await box.click(timeout=5000, force=True)
        if await box.is_checked() != want:
            raise FillError(f"the box did not {'tick' if want else 'untick'}")
        return
    wanted = value if isinstance(value, list) else (
        [v.strip() for v in str(value).split(";") if v.strip()] if kind == "checkbox_group" else [str(value)])
    inputs = block.locator('input[type="radio"]' if kind == "radio" else 'input[type="checkbox"]')
    if kind == "checkbox_group":                       # exactly the wanted ones ticked
        keep = {_best(options, w) for w in wanted}
        for i in range(await inputs.count()):
            if i not in keep and await inputs.nth(i).is_checked():
                await inputs.nth(i).click(timeout=5000, force=True)
    for w in wanted:
        i = _best(options, w)
        if i is None:
            raise FillError(f"option {w!r} not offered ({options})")
        target = inputs.nth(i)
        for _ in range(3):                     # Workday can revert a radio right after the click
            if await target.is_checked():
                break
            await target.click(timeout=5000, force=True)
            await page.wait_for_timeout(600)
        if not await target.is_checked():
            raise FillError(f"{w!r} did not stay selected")


async def fill_wd(page, f, value) -> None:
    """fields.fill_field for a Workday block."""
    from .fields import FillError
    block = _block(page, f.key)
    if not await block.count():
        raise FillError(f"field {f.label!r} not found on the page")
    if f.kind == "wd_select":
        await fill_select(page, f.key, str(value))
    elif f.kind == "wd_multi":
        await fill_multi(page, f.key, value)
    elif f.kind == "wd_date":
        await fill_date(page, f.key, str(value))
    elif f.kind in ("radio", "checkbox", "checkbox_group"):
        await fill_choice(page, f.key, f.kind, f.options, value)
    elif f.kind in ("text", "textarea"):
        ctrl = block.locator("textarea" if f.kind == "textarea" else
                             'input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"])').first
        await ctrl.fill(str(value), timeout=8000)
        await ctrl.dispatch_event("blur")
    elif f.kind == "file":
        paths = value if isinstance(value, list) else [value]
        old = block.locator('[data-automation-id="delete-file"]')
        for _ in range(min(await old.count(), 5)):
            await old.first.click(timeout=4000)
            await page.wait_for_timeout(300)
        await block.locator('input[type="file"]').first.set_input_files([str(p) for p in paths], timeout=15000)
        try:
            await block.locator('[data-automation-id="file-upload-item"], '
                                '[data-automation-id="file-upload-successful"]').first.wait_for(timeout=10000)
        except Exception:  # noqa: BLE001
            raise FillError("the upload did not finish") from None
    else:
        raise FillError(f"unknown Workday field kind {f.kind}")
