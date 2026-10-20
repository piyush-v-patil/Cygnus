"""Read and write form fields (§8.2.4). The enumeration itself is fields.js."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path

from ..normalize import normalize_question

log = logging.getLogger(__name__)
ENUMERATE_JS = resources.files(__package__).joinpath("fields.js").read_text(encoding="utf-8")

CHOICE_KINDS = {"select", "radio", "combobox", "buttons", "checkbox_group"}
COVER_LETTER = ("cover letter", "coverletter", "cover_letter")
RESUME = ("resume", "cv", "curriculum vitae")
EEO_HINTS = ("gender", "race", "ethnicity", "hispanic", "latino", "veteran", "disability",
             "sexual orientation", "transgender", "pronoun", "demographic", "lgbt")


@dataclass
class Field:
    key: str
    label: str
    kind: str                 # text|textarea|select|combobox|radio|checkbox|checkbox_group|buttons|file
    required: bool = False
    options: list[str] = field(default_factory=list)
    value: str = ""
    visible: bool = True
    input_type: str = ""
    max_length: int | None = None
    accept: str = ""
    filled_by_simplify: bool = False

    @property
    def empty(self) -> bool:
        return not (self.value or "").strip()

    @property
    def answer_kind(self) -> str:
        """The answer-bank kind (§5.4) this field takes."""
        if self.kind in CHOICE_KINDS:
            opts = {normalize_question(o) for o in self.options}
            return "yes_no" if opts and opts <= {"yes", "no"} else "choice"
        if self.kind == "checkbox":
            return "yes_no"
        if self.kind == "textarea":
            return "long_text"
        if self.input_type == "number":
            return "number"
        return "short_text"

    @property
    def is_cover_letter(self) -> bool:
        lab = (self.label + " " + self.key).lower()
        return any(c in lab for c in COVER_LETTER)

    @property
    def is_resume(self) -> bool:
        if self.kind != "file":
            return False
        lab = normalize_question(self.label + " " + self.key)
        return any(r in lab.split() or r in lab for r in RESUME) and not self.is_cover_letter

    @property
    def is_eeo(self) -> bool:
        lab = self.label.lower()
        return any(h in lab for h in EEO_HINTS)

    def to_dict(self) -> dict:
        return asdict(self)


async def enumerate_fields(page, hints: dict | None = None) -> list[Field]:
    raw = await page.evaluate(ENUMERATE_JS, hints or {})
    out = []
    for r in raw:
        out.append(Field(key=r["key"], label=r.get("label") or "", kind=r["kind"],
                         required=bool(r.get("required")), options=r.get("options") or [],
                         value=r.get("value") or "", visible=bool(r.get("visible", True)),
                         input_type=r.get("input_type") or "", max_length=r.get("max_length"),
                         accept=r.get("accept") or ""))
    return out


def snapshot(fields: list[Field]) -> dict[str, str]:
    return {f.key: f.value for f in fields}


def _sel(key: str) -> str:
    return f'[data-aa-key="{key.replace(chr(34), chr(92) + chr(34))}"]'


class FillError(Exception):
    pass


async def combobox_options(page, f: Field, limit: int = 200) -> list[str]:
    """Open a custom dropdown and read its options (react-select etc.), then close it."""
    loc = page.locator(_sel(f.key)).first
    try:
        await loc.click(timeout=5000)
        await page.wait_for_timeout(400)
        opts = await page.evaluate(
            """() => [...document.querySelectorAll('[role=option]')]
                 .filter(o => o.offsetParent !== null).map(o => o.textContent.replace(/\\s+/g,' ').trim())
                 .filter(Boolean)""")
    except Exception as e:  # noqa: BLE001
        log.debug("could not read options of %s: %s", f.key, e)
        opts = []
    finally:
        await page.keyboard.press("Escape")
    return list(dict.fromkeys(opts))[:limit]


async def fill_field(page, f: Field, value: str | list[str], files_dir: Path | None = None) -> None:
    """Set one field to `value`. Raises FillError when the page did not take it."""
    sel = _sel(f.key)
    loc = page.locator(sel)
    if await loc.count() == 0:
        raise FillError(f"field {f.label!r} not found on the page")
    try:
        if f.kind in ("text", "textarea"):
            await loc.first.fill(str(value), timeout=8000)
            await loc.first.dispatch_event("blur")
        elif f.kind == "select":
            await loc.first.select_option(label=str(value), timeout=8000)
        elif f.kind == "combobox":
            await _fill_combobox(page, loc.first, str(value))
        elif f.kind == "radio":
            idx = _option_index(f, str(value))
            await _check_option(page, page.locator(f'{sel}[data-aa-opt="{idx}"]').first)
        elif f.kind == "checkbox_group":
            wanted = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
            for w in wanted:
                idx = _option_index(f, w)
                await _check_option(page, page.locator(f'{sel}[data-aa-opt="{idx}"]').first)
        elif f.kind == "checkbox":
            want = normalize_question(str(value)) in ("yes", "true", "1", "checked", "agree", "i agree")
            box = loc.first
            if await box.is_checked() != want:
                await _check_option(page, box)
        elif f.kind == "buttons":
            await loc.first.locator("button", has_text=str(value)).first.click(timeout=8000)
        elif f.kind == "file":
            paths = value if isinstance(value, list) else [value]
            await loc.first.set_input_files([str(p) for p in paths], timeout=15000)
        else:
            raise FillError(f"unknown field kind {f.kind}")
    except FillError:
        raise
    except Exception as e:  # noqa: BLE001 - Playwright raises many types; all mean "did not take"
        raise FillError(f"{f.label!r}: {type(e).__name__}: {str(e).splitlines()[0][:200]}") from e


def _option_index(f: Field, value: str) -> int:
    target = normalize_question(value)
    for i, o in enumerate(f.options):
        if normalize_question(o) == target:
            return i
    raise FillError(f"{f.label!r}: option {value!r} not offered ({f.options})")


async def _check_option(page, loc):
    """Radios/checkboxes are often visually hidden behind a styled label."""
    try:
        await loc.check(timeout=4000)
        return
    except Exception:  # noqa: BLE001
        pass
    lid = await loc.get_attribute("id")
    if lid:
        lab = page.locator(f'label[for="{lid}"]')
        if await lab.count():
            await lab.first.click(timeout=4000)
            return
    await loc.evaluate("el => { (el.closest('label') || el).click(); }")


async def _fill_combobox(page, loc, value: str):
    await loc.click(timeout=8000)
    await loc.fill("")
    await loc.type(value[:60], delay=15)
    opt = page.get_by_role("option", name=value, exact=True)
    try:
        await opt.first.wait_for(state="visible", timeout=4000)
        await opt.first.click(timeout=4000)
        return
    except Exception:  # noqa: BLE001
        pass
    # fall back to the first visible option whose text matches after normalisation
    target = normalize_question(value)
    opts = page.locator("[role=option]")
    for i in range(min(await opts.count(), 100)):
        o = opts.nth(i)
        if await o.is_visible() and normalize_question(await o.inner_text()) == target:
            await o.click(timeout=4000)
            return
    await page.keyboard.press("Escape")
    raise FillError(f"combobox has no option {value!r}")


async def read_values(page, hints: dict | None = None) -> dict[str, str]:
    return snapshot(await enumerate_fields(page, hints))


async def wait_stable(page, hints: dict | None, quiet_s: float = 2.0, timeout_s: float = 45,
                      busy=None) -> dict[str, str]:
    """Poll field values until unchanged for `quiet_s` (and `busy()` is False)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last = await read_values(page, hints)
    since = loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        cur = await read_values(page, hints)
        still_busy = await busy() if busy else False
        if cur != last or still_busy:
            last, since = cur, loop.time()
        elif loop.time() - since >= quiet_s:
            return cur
    return last
