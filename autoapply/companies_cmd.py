"""`autoapply companies`: pause a company (its 30-day application limit is reached), resume
it, and see which companies have postings waiting."""
from __future__ import annotations

from collections import Counter

from .companies import matches, pause, pauses, resume, until_text

WAITING = ("scored", "ready", "needs_input", "stuck", "held", "enriched", "discovered")


def _waiting(ctx) -> Counter:
    return Counter(r.get("company") or "?" for r in ctx.ledger.postings_in(WAITING))


def companies_command(ctx, action: str, name: str | None, days: int) -> int:
    if action in ("pause", "resume") and not name:
        print(f"Which company? e.g. `autoapply companies {action} \"capital one\"`.")
        return 1
    if action == "pause":
        entry = pause(ctx, name, days)
        hits = {c: n for c, n in _waiting(ctx).items() if matches(c, name)}
        print(f"{until_text(entry)}. Its postings are still scored but not filled until then; "
              "they fill on the first run after.")
        if hits:
            print("Covers: " + ", ".join(f"{c} ({n} waiting)" for c, n in sorted(hits.items(), key=lambda kv: -kv[1])))
        else:
            print(f"No posting from a company named {name!r} yet (check the spelling against the list in "
                  "`autoapply companies`); paused anyway.")
        return 0
    if action == "resume":
        print(f"{name}: pause ended." if resume(ctx, name) else f"{name} was not paused.")
        return 0
    active = pauses(ctx)
    if active:
        print("Paused (scored, not filled):")
        for v in sorted(active.values(), key=lambda v: v["until"]):
            print(f"  {until_text(v)}")
    else:
        print("No company is paused.")
    top = _waiting(ctx).most_common(30)
    if top:
        print("\nCompanies with the most postings waiting (use these names with --company / pause):")
        for c, n in top:
            print(f"  {n:>5}  {c}")
    return 0
