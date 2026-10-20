"""Hard-blocker regex pre-check on the job description (§7.1). No LLM.

A *confident* match skips the posting outright. An *ambiguous* match (the
phrase is there but could be read either way) is passed to the scoring call,
which returns its own blockers. All patterns live in this one list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Blocker:
    reason: str           # us_person_required | no_sponsorship
    confident: bool
    pattern: re.Pattern


def _p(rx: str) -> re.Pattern:
    return re.compile(rx, re.I)


PATTERNS: list[Blocker] = [
    # --- citizenship / US person / export control / clearance ---
    Blocker("us_person_required", True, _p(
        r"\b(u\.?s\.?|united states) citizenship (is )?(required|mandatory)")),
    Blocker("us_person_required", True, _p(
        r"\bmust (be|hold) (a )?(u\.?s\.?|united states) citizen(ship)?\b")),
    Blocker("us_person_required", True, _p(
        r"\bonly (u\.?s\.?|united states) citizens\b")),
    Blocker("us_person_required", True, _p(
        r"\bmust (be|qualify as) a ['\"“]?u\.?s\.? person\b")),
    Blocker("us_person_required", True, _p(
        r"\b(active|current) (secret|top secret|ts/sci|ts|dod|government|security) clearance\b")),
    Blocker("us_person_required", True, _p(
        r"\b(must|required to|ability to) (obtain|maintain|hold) (and maintain )?(a |an )?"
        r"(u\.?s\.? )?(government |dod )?(secret|top secret|ts/sci|security) clearance\b")),
    Blocker("us_person_required", True, _p(r"\bts/sci\b")),
    # export-control language usually *means* US person, but some postings
    # mention ITAR only as a product area - let the model judge those.
    Blocker("us_person_required", False, _p(r"\bitar\b|\bexport administration regulations\b|\b\(?ear\)?\s+controlled\b")),
    Blocker("us_person_required", False, _p(r"\bu\.?s\.? persons?\b")),
    Blocker("us_person_required", False, _p(r"\bsecurity clearance\b|\bclearance (is )?(preferred|a plus)\b")),
    # --- sponsorship ---
    Blocker("no_sponsorship", True, _p(
        r"\b(unable|not able|cannot|can ?not|will not|won't|do(es)? not|are not able to) "
        r"(to )?(provide |offer )?(visa |employment |immigration |h-?1b )?sponsor(ship)?\b")),
    Blocker("no_sponsorship", True, _p(
        r"\b(visa |employment |immigration )?sponsorship (is )?(not available|not offered|unavailable|will not be)\b")),
    Blocker("no_sponsorship", True, _p(
        r"\bmust not (now or in the future )?require (visa |employment |immigration )?sponsorship\b")),
    Blocker("no_sponsorship", True, _p(
        r"\bwithout (the )?(need for |requiring )(current or future )?(visa |employment )?sponsorship\b")),
    Blocker("no_sponsorship", False, _p(r"\bsponsorship\b")),
]


@dataclass
class BlockerHit:
    reason: str
    confident: bool
    evidence: str


def scan(text: str, check_us_person: bool = True, check_sponsorship: bool = True) -> list[BlockerHit]:
    hits: list[BlockerHit] = []
    for b in PATTERNS:
        if b.reason == "us_person_required" and not check_us_person:
            continue
        if b.reason == "no_sponsorship" and not check_sponsorship:
            continue
        m = b.pattern.search(text or "")
        if m:
            s, e = max(0, m.start() - 80), min(len(text), m.end() + 80)
            hits.append(BlockerHit(b.reason, b.confident, " ".join(text[s:e].split())))
    return hits


def confident_block(hits: list[BlockerHit]) -> BlockerHit | None:
    return next((h for h in hits if h.confident), None)
