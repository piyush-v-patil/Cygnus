"""Fit scoring with score_model (§9.1). Nothing is scored twice: results are keyed
by posting_key + resume hash + PROMPT_VERSION in the ledger."""
from __future__ import annotations

import json

from ..config import Profile
from . import prompts
from .client import LLM, cached_system

PROMPT_VERSION = "score-v1"
ROLES = ["ml-ai", "data", "security", "devops-sre", "mobile", "frontend", "fullstack",
         "backend", "embedded", "qa-test", "solutions", "software"]
BLOCKER_TYPES = ["us_person_required", "no_sponsorship", "yoe", "blocker_other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "role_family": {"type": "string", "enum": ROLES},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": BLOCKER_TYPES},
                           "evidence": {"type": "string"}},
            "required": ["type", "evidence"], "additionalProperties": False}},
        "yoe_required": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    },
    "required": ["score", "role_family", "reasons", "blockers", "yoe_required"],
    "additionalProperties": False,
}

VISA_TEXT = {
    "citizen_gc": "US citizen or green-card holder; never needs sponsorship.",
    "opt_stem_opt": "On F-1 OPT / STEM OPT: authorized to work now without sponsorship",
    "needs_sponsorship_now": "Needs H-1B (or similar) sponsorship to start work.",
}


def profile_summary(profile: Profile) -> str:
    v = profile.visa
    visa = VISA_TEXT[v.status]
    if v.status == "opt_stem_opt":
        visa += (", and will need H-1B sponsorship later." if v.needs_sponsorship_future
                 else ", and will not need sponsorship later.")
    needs = v.status != "citizen_gc" and (v.status == "needs_sponsorship_now" or v.needs_sponsorship_future)
    return "\n".join([
        f"- Target tiers: {', '.join(profile.filters.tiers)} (job-monitor tiers: intern | newgrad | experienced)",
        f"- Target role families: {', '.join(profile.filters.roles)}",
        f"- Location preference: {profile.filters.location}"
        + (f" ({', '.join(profile.filters.metros)})" if profile.filters.metros else ""),
        f"- Work authorization: {visa}",
        f"- Needs sponsorship (now or later): {'yes' if needs else 'no'}",
        f"- Skips roles requiring US citizenship / US-person status / clearance: "
        f"{'yes' if v.skip_us_person_roles else 'no'}",
    ])


def calibration_block(examples: list[dict]) -> str:
    """A compact few-shot reference: up to 6 yes + 6 no from `autoapply calibrate` (§5.3)."""
    if not examples:
        return ""
    yes = [e for e in examples if e.get("label") == "yes"][:6]
    no = [e for e in examples if e.get("label") == "no"][:6]
    lines = ["How this candidate has judged real postings (for calibration):"]
    for e in yes + no:
        lines.append(f"- {'WOULD apply' if e['label'] == 'yes' else 'would NOT apply'}: "
                     f"{e['title']} at {e['company']}" + (f" - {e['reason']}" if e.get("reason") else ""))
    return "\n".join(lines)


def score_posting(llm: LLM, profile: Profile, resume_text: str, posting: dict, description: str,
                  calibration: list[dict], regex_hints: list[str] | None = None) -> dict:
    system = cached_system(prompts.load(
        "score", roles=", ".join(ROLES), profile_summary=profile_summary(profile),
        calibration=calibration_block(calibration), resume=resume_text))
    hint = ""
    if regex_hints:
        hint = ("\n\nA keyword scan flagged these passages; decide whether each is a real blocker:\n"
                + "\n".join(f"- \"{h}\"" for h in regex_hints))
    user = (f"Company: {posting.get('company')}\nTitle: {posting.get('title')}\n"
            f"Location: {posting.get('location')}\nTier (from tracker): {posting.get('tier') or 'unknown'}\n\n"
            f"<job_description>\n{description[:30000]}\n</job_description>{hint}")
    res = llm.call("score", profile.llm.score_model, system, user, schema=SCHEMA,
                   posting_key=posting.get("posting_key"), max_tokens=3000, effort="low")
    d = res.data
    d["score"] = max(0, min(100, int(d.get("score", 0))))
    d["reasons"] = [str(r) for r in d.get("reasons", [])][:3]
    d["blockers"] = [b for b in d.get("blockers", []) if b.get("type") in BLOCKER_TYPES]
    return d


def dumps(d: dict) -> str:
    return json.dumps(d, sort_keys=True)
