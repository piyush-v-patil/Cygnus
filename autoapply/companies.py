"""Choosing companies: a run's `--company` / `--skip-company`, companies paused by hand
(`autoapply companies pause`), and companies paused automatically once `limits.company_max`
applications went to them within `limits.company_days` days.

A paused company's postings are still scored but not filled; the pause lifts by itself on
its end date and they fill on the next run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .us_person import normalize_company

PAUSED_KEY = "paused_companies"
RESUMED_KEY = "auto_pause_resumed"      # {normalized name: "YYYY-MM-DD"}: earlier sends no longer count
SENT = ("submitted", "submitted_manual")


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
    """Active pauses {normalized name: {"name", "until", "at"}}, yours and the automatic
    ones ("auto": the count in the window); expired ones are dropped. Yours wins where both."""
    today = date.today().isoformat()
    mine = {k: v for k, v in (ctx.state_get(PAUSED_KEY) or {}).items() if v.get("until", "") >= today}
    return {**auto_pauses(ctx), **mine}


def sent_in_window(ctx) -> dict[str, list[tuple[str, str]]]:
    """{normalized name: [(day sent, company as written)...]} for applications sent in the last
    `limits.company_days` days, after any `companies resume` of that company."""
    days = ctx.profile.limits.company_days
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    resumed = ctx.state_get(RESUMED_KEY) or {}
    out: dict[str, list[tuple[str, str]]] = {}
    for company, sent_at in ctx.ledger.db.execute(
            f"SELECT p.company, a.submitted_at FROM applications a JOIN postings p USING(posting_key) "
            f"WHERE p.state IN ({','.join('?' * len(SENT))}) AND a.submitted_at IS NOT NULL", SENT):
        day = (sent_at or "")[:10]
        key = norm(company or "")
        if key and day >= since and day > resumed.get(key, ""):
            out.setdefault(key, []).append((day, company))
    return out


def auto_pauses(ctx) -> dict:
    """Companies at their `limits.company_max` for the window, paused until the oldest of
    those applications is `company_days` days old."""
    lim = ctx.profile.limits
    if not lim.company_max:
        return {}
    out = {}
    for key, sent in sent_in_window(ctx).items():
        if len(sent) >= lim.company_max:
            first = min(day for day, _ in sent)
            # the last day that application still counts, as `until` is for your own pauses
            until = date.fromisoformat(first) + timedelta(days=lim.company_days - 1)
            out[key] = {"name": sent[0][1], "until": until.isoformat(), "auto": len(sent),
                        "days": lim.company_days}
    return out


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
    """End your pause of `name`, and an automatic one: sends before today stop counting."""
    today = date.today().isoformat()
    mine = {k: v for k, v in (ctx.state_get(PAUSED_KEY) or {}).items() if v.get("until", "") >= today}
    gone = [k for k, v in mine.items() if k == norm(name) or matches(v["name"], name)]
    for k in gone:
        mine.pop(k)
    ctx.state_set(PAUSED_KEY, mine)
    auto = [k for k, v in auto_pauses(ctx).items() if k == norm(name) or matches(v["name"], name)]
    if auto:
        resumed = ctx.state_get(RESUMED_KEY) or {}
        resumed.update({k: today for k in auto})
        ctx.state_set(RESUMED_KEY, resumed)
    return bool(gone or auto)


def until_text(entry: dict, company: str | None = None) -> str:
    d = date.fromisoformat(entry["until"])
    why = f" ({entry['auto']} applications in {entry['days']} days)" if entry.get("auto") else ""
    return f"{company or entry['name']} paused until {d:%b} {d.day}{why}"


# --------------------------------------------------------- this run only --
def selected(ctx, company: str | None) -> bool:
    """Whether this run works on `company`: its --company list (if any) names it, and its
    --skip-company list does not."""
    only, skip = getattr(ctx, "run_companies", None) or ((), ())
    if only and not any(matches(company, w) for w in only):
        return False
    return not any(matches(company, w) for w in skip)
