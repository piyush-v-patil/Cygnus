"""Questions answered from what you already told the tool, without asking again.

Two sources, used in this order wherever a question is not answered word for
word by the answer bank:

  * standing_answer - your work preferences in profile.yaml (`work:`):
    relocation, any US location, on-site / hybrid / remote, relocation
    assistance, and "have you worked for <this company>?" when every employer
    you have had is on your resume. These fill the form directly.
  * near_answer - the saved answer to a question worded differently ("Open to
    relocation?" for "Are you willing to relocate?"). Only ever a suggestion:
    a Needs-input card shows it pre-filled with where it came from, and saving
    the card is what accepts it.

Neither ever answers a legal, visa or demographic question (PROTECTED): those
come from your visa answers or from you, every time.
"""
from __future__ import annotations

import re

from .normalize import normalize_question
from .us_person import normalize_company

PROTECTED = re.compile(
    r"sponsor|visa\b|authori[sz]|citizen|green card|immigra|h-?1b|\bopt\b|\bcpt\b|\bead\b"
    r"|permanent resident|work permit|eligib\w* (to|for) (work|employment)|right to work|legally"
    r"|clearance|export|itar\b|criminal|convict|felon|background check|drug (test|screen)"
    r"|gender|\brace\b|ethnic|hispanic|latin[oax]|veteran|disabilit|pronoun|sexual orientation"
    r"|transgender|lgbt|demographic|date of birth|\bage\b|how old", re.I)


def protected(label: str) -> bool:
    return bool(PROTECTED.search(label or ""))


def _yn(text: str) -> str | None:
    n = normalize_question(text)
    if n == "yes" or n.startswith("yes "):
        return "yes"
    if n == "no" or n.startswith("no "):
        return "no"
    return None


def as_value(yn: str, kind: str, options: list[str] | None) -> str | None:
    """A yes/no verdict as this field's value: the one option that says it, or the word."""
    if options:
        hits = [o for o in options if _yn(o) == yn]
        return hits[0] if len(hits) == 1 else None
    if kind in ("yes_no", "choice", "short_text", "long_text"):
        return "Yes" if yn == "yes" else "No"
    return None


# ----------------------------------------------------------- near answers --
# Words that carry no meaning of their own in an application question: the
# auxiliaries and pronouns, and the willingness words every one of them uses
# ("are you willing / open / able / comfortable to ...").
STOP = set("""
a an the to of for in on at with and or your you yours are do does did will would can could
is be been being am have has had i we us our this that these those it its if please any as by from
about into currently presently willing open able comfortable interested ok okay prepared happy
consider considering available position role job opportunity company organization team here
how many much what which when where who years year experience schedule arrangement model
environment basis require required requirement work working expectation expectations desired
""".split())
# a question about the past is a different question from one about the future
PAST = re.compile(r"^\s*(have|has|had|did|were|was)\b|\bever\b|previous|former|in the past|prior to", re.I)
NEGATED = re.compile(r"\b(not|without|unable|never)\b", re.I)
PHRASES = ((re.compile(r"\bon[\s-]?site\b|\bin[\s-]?(the[\s-])?office\b|\bin[\s-]?person\b", re.I), " onsite "),
           (re.compile(r"\bremotely\b", re.I), " remote "),
           (re.compile(r"\b(compensation|salary|pay)\b", re.I), " salary "),
           (re.compile(r"\b(move|moving)\b", re.I), " relocate "))
SUFFIXES = ("ings", "ing", "ions", "ion", "ied", "ies", "ed", "es", "ly", "s", "e")


def _stem(w: str) -> str:
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "s" and w.endswith(("ss", "us", "is")):
                continue
            return w[: -len(suf)]
    return w


def content(label: str) -> frozenset:
    """The meaning-bearing word stems of a question."""
    t = label or ""
    for pat, rep in PHRASES:
        t = pat.sub(rep, t)
    return frozenset(_stem(w) for w in normalize_question(t).split() if w not in STOP and len(w) > 1)


def similarity(a: str, b: str) -> float:
    if bool(PAST.search(a)) != bool(PAST.search(b)) or bool(NEGATED.search(a)) != bool(NEGATED.search(b)):
        return 0.0
    ca, cb = content(a), content(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


NEAR = 0.6


def _compatible(saved: str, field_kind: str) -> bool:
    return saved == field_kind or {saved, field_kind} in ({"yes_no", "choice"}, {"short_text", "long_text"})


def _fits(answer: str, options: list[str] | None) -> str | None:
    if not options:
        return answer
    norm = {normalize_question(o): o for o in options}
    return norm.get(normalize_question(answer or ""))


def near_answer(items, label: str, kind: str, options: list[str] | None = None) -> tuple[str, str] | None:
    """(value, the saved question it comes from) for the closest saved answer, or None.

    `items` are AnswerBank entries. A saved answer qualifies only for a field of a
    compatible kind and, where the field has options, when it is one of them.
    """
    if protected(label):
        return None
    best = None
    for a in items:
        if protected(a.original) or not _compatible(a.kind, kind):
            continue
        value = _fits(a.answer, options)
        if value is None:
            continue
        score = similarity(label, a.original)
        if score >= NEAR and (best is None or (score, a.added) > (best[0], best[1])):
            best = (score, a.added, value, a.original)
    return (best[2], best[3]) if best else None


# -------------------------------------------------------- standing answers --
WILLING = re.compile(r"\b(willing|able|open|comfortable|agree|accept|okay|ok|available|prepared|interested"
                     r"|commit|consider|can you|could you|would you|will you|do you plan)\b", re.I)
ASKS = re.compile(r"^\s*(are|do|does|did|have|has|had|will|would|can|could|is|was|were)\b", re.I)
ASSISTANCE = re.compile(r"relocation (assistance|package|support|benefit|reimburse|expense|stipend|help|bonus)"
                        r"|(assistance|package|support|help|reimburse\w*|stipend) (with |for )?(your )?relocat", re.I)
RELOCATE = re.compile(r"relocat|\b(move|moving) (to|for)\b", re.I)
HYBRID = re.compile(r"\bhybrid\b", re.I)
ONSITE = re.compile(r"on[\s-]?site\b|\bin[\s-]?(the[\s-])?office\b|\bin[\s-]?person\b"
                    r"|\b(\d|three|four|five) days (a|per|each) week\b", re.I)
REMOTE = re.compile(r"\bremote(ly)?\b", re.I)
ANYWHERE = re.compile(r"\b(any|all|other|multiple|different) (of )?(our |your |the |these )?(u\.? ?s\.? |united states )?"
                      r"(office |work )?(locations?|offices?|cities|city|sites?|states?)\b"
                      r"|\banywhere in the (u ?s|united states|country)\b|\blocation flexib|\bflexible (on|about|with) (your )?location", re.I)
EMPLOYED = re.compile(r"\b(work|worked|working|employed|employee|employment)\b", re.I)
EMPLOYED_BEFORE = re.compile(r"\b(previous|previously|ever|currently|current|former|formerly|past|prior|before)\b", re.I)
NOT_YOURS = re.compile(r"relative|family|friend|spouse|partner|know (anyone|someone|any)|referr|related to"
                       r"|government|federal|military|agency|competitor|non-?compete|applied", re.I)
GENERIC_EMPLOYER = re.compile(r"\b(us|our (company|organization|firm|group)|this (company|organization|employer|firm))\b", re.I)


def _squash(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _names_company(label: str, company: str) -> bool:
    lab = normalize_question(label)
    co = normalize_company(company or "")
    if co and (f" {co} " in f" {lab} " or (len(co.split()[0]) >= 3 and f" {co.split()[0]} " in f" {lab} ")):
        return True
    return bool(GENERIC_EMPLOYER.search(lab))


def on_resume(company: str, resume_text: str) -> bool:
    """Whether the resume could mention this employer - read generously, because a
    "yes" here only means the question goes to you instead."""
    co = normalize_company(company or "")
    if not co:
        return True
    if _squash(co) in _squash(resume_text):
        return True
    first = co.split()[0]
    return len(first) >= 4 and re.search(rf"\b{re.escape(first)}\b", (resume_text or "").lower()) is not None


def standing_answer(label: str, work, company: str = "", resume_text: str = "") -> tuple[str, str] | None:
    """("yes"/"no", why) from your work preferences for a yes/no question, or None.

    `work` is profile.work; a preference left unset (None) answers nothing.
    """
    if work is None or protected(label) or not ASKS.search(label or ""):
        return None
    lab = label or ""

    def pref(value, why_yes: str, why_no: str):
        if value is None:
            return None
        return ("yes", why_yes) if value else ("no", why_no)

    if EMPLOYED.search(lab) and EMPLOYED_BEFORE.search(lab) and not NOT_YOURS.search(lab) \
            and _names_company(lab, company):
        if work.only_resume_employers and not on_resume(company, resume_text):
            return "no", f"every employer you have had is on your resume, and {company or 'this company'} is not"
        return None
    if ASSISTANCE.search(lab):
        return pref(work.relocation_assistance, "you need relocation assistance",
                    "you do not need relocation assistance")
    if not WILLING.search(lab):
        return None                       # "do you currently live in ...?" is a fact, not a preference
    if RELOCATE.search(lab):
        return pref(work.relocate, "you will relocate", "you will not relocate")
    if HYBRID.search(lab):
        return pref(work.hybrid, "you will work hybrid", "you will not work hybrid")
    if ONSITE.search(lab):
        return pref(work.onsite, "you will work on-site", "you will not work on-site")
    if REMOTE.search(lab):
        return pref(work.remote, "you will work remotely", "you will not work remotely")
    if ANYWHERE.search(lab):
        return pref(work.anywhere_in_us, "you are open to any US location",
                    "you are not open to any US location")
    return None


def standing_value(label: str, kind: str, options, work, company: str = "",
                   resume_text: str = "") -> tuple[str, str] | None:
    """(value for this field, why) from your work preferences, or None."""
    hit = standing_answer(label, work, company, resume_text)
    if hit is None or kind not in ("yes_no", "choice", "short_text", "long_text"):
        return None
    value = as_value(hit[0], kind, options)
    return (value, hit[1]) if value is not None else None
