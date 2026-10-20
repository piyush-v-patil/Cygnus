"""`autoapply calibrate` (§5.3): label sample jobs blind, then pick the cutoff that
best separates the ones you would apply to from the ones you would not."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Callable

from .context import Context
from .ledger import ENRICHED, SCORED, SKIPPED
from .llm import BudgetExceeded, LLMError
from .llm.score import PROMPT_VERSION, score_posting
from .pipeline import RunStats, enrich, sync

log = logging.getLogger(__name__)


def spread(items: list[dict], n: int) -> list[dict]:
    """`n` items evenly spaced across the score range (lowest to highest)."""
    items = sorted(items, key=lambda x: x["score"])
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1) if n > 1 else 0
    idx = sorted({round(i * step) for i in range(n)})
    return [items[i] for i in idx]


def agreement(examples: list[dict], cutoff: int) -> float:
    if not examples:
        return 0.0
    hits = sum((e["score"] >= cutoff) == (e["label"] == "yes") for e in examples)
    return hits / len(examples)


def best_cutoff(examples: list[dict], current: int = 65) -> tuple[int, float]:
    """Highest agreement; ties go to the cutoff closest to the current one."""
    best = max(range(0, 101), key=lambda c: (round(agreement(examples, c), 6), -abs(c - current)))
    return best, agreement(examples, best)


def set_cutoff_in_yaml(text: str, cutoff: int) -> str:
    """Change fit.cutoff in place so the user's comments in profile.yaml survive."""
    pat = re.compile(r"(^fit:\s*\n(?:[ \t]+.*\n)*?[ \t]+cutoff:[ \t]*)(\d+)", re.M)
    if pat.search(text):
        return pat.sub(lambda m: f"{m.group(1)}{cutoff}", text, count=1)
    return text.rstrip("\n") + f"\nfit:\n  cutoff: {cutoff}\n"


def _candidates(ctx: Context, need: int, rescore: bool) -> list[dict]:
    led = ctx.ledger
    rows = led.postings_in([ENRICHED, SCORED]) + [r for r in led.postings_in([SKIPPED]) if r.reason == "low_fit"]
    out = []
    calibration = []          # score without the old examples, so they cannot bias the new ones
    for r in rows:
        if len(out) >= need:
            break
        key = r.posting_key
        resume = ctx.resumes.file_for(r.get("role"))
        rhash = ctx.resumes.hash(resume)
        s = None if rescore else led.score(key, PROMPT_VERSION, rhash)
        if s is None:
            det = led.details(key) or {}
            try:
                s = score_posting(ctx.llm, ctx.profile, ctx.resumes.text(resume), dict(r.data),
                                  det.get("description_text") or "", calibration)
            except (BudgetExceeded, LLMError) as e:
                log.warning("stopped scoring: %s", e)
                break
            led.save_score(key, s, ctx.profile.llm.score_model, PROMPT_VERSION, rhash)
        out.append({"posting_key": key, "title": r.get("title"), "company": r.get("company"),
                     "location": r.get("location"), "score": s["score"],
                     "description": (led.details(key) or {}).get("description_text", "")})
    return out


def run_calibration(ctx: Context, count: int = 40, rescore: bool = False,
                    ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> int:
    count = max(10, min(count, 60))
    say("Reading your job sources...")
    sync(ctx, RunStats())
    enrich(ctx, RunStats(), count * 3)
    say("Scoring a sample (scores stay hidden until you have labelled them)...")
    pool = _candidates(ctx, count * 3, rescore)
    sample = spread(pool, count)
    if len(sample) < 10:
        say(f"Only {len(sample)} eligible jobs are available - run `autoapply run --no-fill` first or widen filters.")
        return 1
    say(f"\nLabel {len(sample)} jobs: y = would apply, n = would not, s = skip, q = stop. "
        "Optionally add a short reason after the letter (e.g. `n too senior`).\n")
    examples = []
    for i, it in enumerate(sample, 1):
        say(f"[{i}/{len(sample)}] {it['title']} — {it['company']} ({it['location'] or 'n/a'})")
        say("    " + " ".join((it["description"] or "")[:700].split()) + " ...")
        while True:
            raw = ask("    apply? [y/n/s/q] ").strip()
            letter, _, reason = raw.partition(" ")
            letter = letter.lower()
            if letter in ("y", "n", "s", "q"):
                break
        if letter == "q":
            break
        if letter == "s":
            continue
        examples.append({"posting_key": it["posting_key"], "title": it["title"], "company": it["company"],
                         "score": it["score"], "label": "yes" if letter == "y" else "no",
                         "reason": reason.strip()[:120]})
    if len(examples) < 6 or len({e["label"] for e in examples}) < 2:
        say("Not enough labels (need at least 6, with both yes and no). Nothing changed.")
        return 1
    cur = ctx.profile.fit.cutoff
    best, acc = best_cutoff(examples, cur)
    say(f"\nAgreement at your current cutoff {cur}: {agreement(examples, cur):.0%}")
    say(f"Suggested cutoff: {best} (agreement {acc:.0%})")
    ctx.paths.calibration.write_text(json.dumps({
        "created": datetime.now().isoformat(timespec="seconds"), "suggested_cutoff": best,
        "agreement": round(acc, 3), "examples": examples}, indent=1), encoding="utf-8")
    if best != cur and ask(f"Use {best} as the new cutoff? [y/N] ").strip().lower() == "y":
        ctx.paths.profile.write_text(set_cutoff_in_yaml(ctx.paths.profile.read_text(encoding="utf-8"), best),
                                     encoding="utf-8")
        say(f"fit.cutoff set to {best}.")
    else:
        say(f"Kept cutoff {cur}.")
    say(f"Saved {len(examples)} examples to {ctx.paths.calibration}; they guide future scoring.")
    return 0
