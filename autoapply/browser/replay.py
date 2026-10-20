"""Re-create a filled form from its fill plan (§8.7).

A filled tab does not survive hours until review, so approval usually means:
reopen the form, run Simplify again, apply the plan's non-Simplify values (no
new Claude calls), and verify every planned value is present.
"""
from __future__ import annotations

import logging

from ..ats import form_hints, parse_key
from ..normalize import normalize_question
from .fields import Field, FillError, enumerate_fields, fill_field
from .fill import Stuck, form_url, open_form, run_simplify

log = logging.getLogger(__name__)


def _match(fields: list[Field], entry: dict) -> Field | None:
    by_key = {f.key: f for f in fields}
    if entry["key"] in by_key:
        return by_key[entry["key"]]
    want = normalize_question(entry.get("label", ""))
    same = [f for f in fields if normalize_question(f.label) == want and f.kind == entry.get("kind")]
    return same[0] if len(same) == 1 else None


def file_name(entry: dict) -> str:
    return str(entry["value"]).replace("\\", "/").rsplit("/", 1)[-1]


def _value_ok(f: Field, entry: dict) -> bool:
    got = normalize_question(f.value)
    if entry["source"] == "simplify":
        return bool(got)                       # Simplify may word things slightly differently
    if f.kind == "file" or entry.get("kind") == "file":
        return file_name(entry).lower() in f.value.lower()
    want = entry["value"]
    if isinstance(want, list):
        want = "; ".join(want)
    want_n = normalize_question(str(want))
    if f.kind in ("text", "textarea"):
        return got == want_n
    return got == want_n or want_n in got


async def verify_plan(page, plan: list[dict], hints: dict) -> list[str]:
    fields = await enumerate_fields(page, hints)
    problems = []
    for e in plan:
        f = _match(fields, e)
        if f is None:
            problems.append(f"field gone: {e.get('label')!r}")
        elif not _value_ok(f, e):
            if e.get("kind") == "file" and await page.get_by_text(file_name(e), exact=False).count():
                continue          # the board shows the upload as a filename chip
            problems.append(f"{e.get('label')!r} is {f.value!r}, planned {str(e['value'])[:60]!r}")
    planned = {e["key"] for e in plan}
    for f in fields:
        if f.required and f.empty and f.visible and f.key not in planned and f.kind != "file":
            problems.append(f"required field {f.label!r} is empty")
    return problems


async def apply_plan(ctx, page, plan: list[dict], hints: dict, resume_path=None) -> list[str]:
    fields = await enumerate_fields(page, hints)
    problems = []
    for e in plan:
        if e["source"] == "simplify":
            continue
        f = _match(fields, e)
        if f is None:
            problems.append(f"field gone: {e.get('label')!r}")
            continue
        value = e["value"]
        if e["source"] == "resume" and resume_path is not None:
            value = str(resume_path)
        try:
            await fill_field(page, f, value)
        except FillError as err:
            problems.append(str(err))
    return problems


async def replay(ctx, bs, app: dict) -> object:
    """Open a fresh tab with the plan applied. Raises Stuck with a clear reason."""
    key = app["posting_key"]
    hints = form_hints(parse_key(key).ats)
    plan = app.get("fill_plan") or []
    resume_path = ctx.resumes.path(app["resume_file"]) if app.get("resume_file") else None
    page = await bs.new_page()
    try:
        await open_form(page, form_url(ctx, key), hints, ctx.profile.browser.page_timeout_s)
        await run_simplify(ctx, page, hints)
        problems = await apply_plan(ctx, page, plan, hints, resume_path)
        await page.wait_for_timeout(800)
        problems += await verify_plan(page, plan, hints)
        if problems:
            raise Stuck("replay_mismatch", "; ".join(problems)[:600])
        return page
    except BaseException as e:
        e.page = page
        raise
