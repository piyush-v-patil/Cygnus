"""Choosing companies: a run's `--company` / `--skip-company`, and companies paused by hand
(`autoapply companies pause`), for example when their 30-day application limit is reached.

A paused company's postings are still scored but not filled; the pause lifts by itself on
its end date and they fill on the next run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .us_person import normalize_company

PAUSED_KEY = "paused_companies"


def norm(name: str) -> str:
    return normalize_company(name or "")


def matches(company: str | None, wanted: str) -> bool:
    """`wanted` (what you typed) names `company`: the same name, or its first whole words
    ("capital one" -> "Capital One Financial"); spaces do not matter ("capitalone")."""
    n, w = norm(company or ""), norm(wanted)
    if not n or not w:
        return False
    return n == w or n.startswith(w + " ") or n.replace(" ", "") == w.replace(" ", "")


def split_names(values: list[str] | None) -> list[str]:
    """--company a --company "b, c" -> ["a", "b", "c"]."""
    return [v.strip() for raw in values or [] for v in raw.split(",") if v.strip()]


# ------------------------------------------------------------------ pauses --
def pauses(ctx) -> dict:
    """Active pauses {normalized name: {"name", "until", "at"}}; expired ones are dropped."""
    today = date.today().isoformat()
    return {k: v for k, v in (ctx.state_get(PAUSED_KEY) or {}).items() if v.get("until", "") >= today}


def paused_in(active: dict, company: str | None) -> dict | None:
    """The pause covering `company` in `active` (from pauses()), or None."""
    return next((v for v in active.values() if matches(company, v["name"])), None)


def paused(ctx, company: str | None) -> dict | None:
    return paused_in(pauses(ctx), company)


def pause(ctx, name: str, days: int = 30) -> dict:
    entry = {"name": name.strip(), "until": (date.today() + timedelta(days=days)).isoformat(),
             "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    active = pauses(ctx)
    active[norm(name)] = entry
    ctx.state_set(PAUSED_KEY, active)
    return entry


def resume(ctx, name: str) -> bool:
    active = pauses(ctx)
    gone = [k for k, v in active.items() if k == norm(name) or matches(v["name"], name)]
    for k in gone:
        active.pop(k)
    ctx.state_set(PAUSED_KEY, active)
    return bool(gone)


def until_text(entry: dict, company: str | None = None) -> str:
    d = date.fromisoformat(entry["until"])
    return f"{company or entry['name']} paused until {d:%b} {d.day}"


# --------------------------------------------------------- this run only --
def selected(ctx, company: str | None) -> bool:
    """Whether this run works on `company`: its --company list (if any) names it, and its
    --skip-company list does not."""
    only, skip = getattr(ctx, "run_companies", None) or ((), ())
    if only and not any(matches(company, w) for w in only):
        return False
    return not any(matches(company, w) for w in skip)
