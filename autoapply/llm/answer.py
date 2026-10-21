"""Field classification + answers with write_model (§8.5.2).

Claude's output is checked in code, not trusted: a factual answer survives only
if its evidence is found verbatim in the resume, and a choice answer only if it
is one of the field's options. Anything else becomes Needs input.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata

from ..config import Profile
from ..normalize import normalize_question
from . import prompts
from .client import LLM, cached_system

PROMPT_VERSION = "answer-v2"
log = logging.getLogger(__name__)
KINDS = ["essay", "factual_from_resume", "needs_user"]

SCHEMA = {
    "type": "object",
    "properties": {"fields": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "answer": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "evidence": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "facts_used": {"type": "array", "items": {"type": "string"}},
            "needs_user_reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["id", "kind", "answer", "evidence", "facts_used", "needs_user_reason"],
        "additionalProperties": False}}},
    "required": ["fields"],
    "additionalProperties": False,
}


def _squash(t: str) -> str:
    """Text reduced to its letters and digits, for verbatim comparison.

    Resume text comes out of a PDF with ligatures (\ufb01 for "fi"), words split
    across lines ("experi-\nence") and assorted dashes and quotes, while Claude
    quotes the words as a person reads them. The same words in the same order
    still have to be there; only those artefacts are ignored.
    """
    t = unicodedata.normalize("NFKC", t or "")
    t = re.sub(r"(\w)[-\u2010\u00ad]\s*\n\s*(\w)", r"\1\2", t)   # re-join hyphenated line breaks
    return re.sub(r"[^0-9a-z]+", "", t.lower())


def evidence_in_resume(evidence: str | None, resume: str) -> bool:
    ev = _squash(evidence or "")
    return len(ev) >= 6 and ev in _squash(resume)


def match_option(answer: str, options: list[str]) -> str | None:
    if not options:
        return answer
    norm = {normalize_question(o): o for o in options}
    return norm.get(normalize_question(answer or ""))


# Open-ended questions the candidate wants written for them: always an essay from
# the resume + job description, never left blank for lack of a "fact".
OPEN_ENDED = re.compile(
    r"\bwhy\b|interest(s|ed)? you|interested in|excite|motivat|passion|what (draws|attracts|appeals|makes)"
    r"|tell us|about yourself|describe|share (a|an|your|with)|walk us through|explain"
    r"|project|proud|accomplish|challenge|what would you|how would you|what do you (hope|want|bring|enjoy)"
    r"|cover letter|anything else|additional (information|comments)|in your own words|fit for", re.I)
# ...except anything legal, money, logistics or demographic: those are facts the
# candidate states, however the question is phrased ("Explain why you require sponsorship")
NOT_OPEN = re.compile(
    r"sponsor|visa|authori[sz]|citizen|clearance|export|criminal|convict|background check|salary|compensation"
    r"|pay\b|relocat|start date|notice period|availab|commute|on-?site|in[- ]office|hybrid|travel"
    r"|gender|race|ethnic|veteran|disabilit|pronoun|age\b|referr|how did you (hear|find|learn)", re.I)
TEXT_TYPES = {"text", "textarea", "email", "url", "tel", "number", ""}


def is_open_ended(field: dict) -> bool:
    label = field.get("label") or ""
    return ((field.get("type") or "") in TEXT_TYPES and not field.get("options")
            and bool(OPEN_ENDED.search(label)) and not NOT_OPEN.search(label))


def answer_fields(llm: LLM, profile: Profile, resume_text: str, posting: dict, description: str,
                  fields: list[dict]) -> dict[str, dict]:
    """`fields`: [{id, label, type, options, required, max_length}] -> {id: verdict}.

    A verdict is {kind, answer, evidence, facts_used, needs_user_reason}, already
    validated; kind == needs_user means the tool must not fill it.
    """
    if not fields:
        return {}
    # short ids: form keys are long and odd, and an id echoed back even slightly
    # changed would silently drop that answer
    short = {f"f{i}": f for i, f in enumerate(fields, 1)}
    out = _ask(llm, profile, resume_text, posting, description, short, retry=False)
    missed = {sid: f for sid, f in short.items() if is_open_ended(f) and out[f["id"]]["kind"] == "needs_user"}
    if missed:
        log.info("    asking Claude again to write %d open-ended answer(s)", len(missed))
        out.update(_ask(llm, profile, resume_text, posting, description, missed, retry=True))
    return out


def _ask(llm: LLM, profile: Profile, resume_text: str, posting: dict, description: str,
         short: dict[str, dict], retry: bool) -> dict[str, dict]:
    system = cached_system(prompts.load("answer", resume=resume_text))
    listing = []
    for sid, f in short.items():
        item = {"id": sid, **{k: f.get(k) for k in ("label", "type", "options", "required", "max_length")
                              if f.get(k) not in (None, [], "")}}
        if is_open_ended(f):
            item["open_ended"] = True
        listing.append(item)
    note = ("" if not retry else
            "\n\nThese are open-ended questions. Write each one as an essay from the resume and the job "
            "description - what the posting says about the company, its product and the role is enough "
            "for 'why this company'. Do not answer needs_user.")
    user = (f"Company: {posting.get('company')}\nRole: {posting.get('title')}\n\n"
            f"<job_description>\n{description[:30000]}\n</job_description>\n\n"
            f"Blank fields:\n{json.dumps(listing, indent=1)}{note}")
    res = llm.call("answer", profile.llm.write_model, system, user, schema=SCHEMA,
                   posting_key=posting.get("posting_key"), max_tokens=12000, effort="medium")
    out: dict[str, dict] = {}
    for v in res.data.get("fields", []):
        f = short.get((v.get("id") or "").strip())
        if f is None:
            continue
        out[f["id"]] = validate(v, f, resume_text)
    for f in short.values():
        out.setdefault(f["id"], {"kind": "needs_user", "answer": None, "evidence": None,
                                 "facts_used": [], "needs_user_reason": "not answered"})
    return out


def validate(v: dict, field: dict, resume: str) -> dict:
    v = dict(v)
    kind = v.get("kind")
    ans = (v.get("answer") or "").strip()
    options = field.get("options") or []

    def to_user(reason: str) -> dict:
        return {"kind": "needs_user", "answer": None, "evidence": v.get("evidence"),
                "facts_used": [], "needs_user_reason": reason}

    if kind == "needs_user":
        return to_user(v.get("needs_user_reason") or "needs the candidate")
    if not ans:
        return to_user("no answer produced")
    if field.get("type") in ("checkbox", "consent", "file"):
        return to_user("checkboxes and files are never answered by Claude")
    if kind == "factual_from_resume" and not evidence_in_resume(v.get("evidence"), resume):
        return to_user("no verbatim resume evidence")
    if options:
        hit = match_option(ans, options)
        if hit is None:
            return to_user("answer is not one of the options")
        if kind == "essay":
            # picking an option is a factual claim, never prose
            if not evidence_in_resume(v.get("evidence"), resume):
                return to_user("choice needs resume evidence")
            kind = "factual_from_resume"
        ans = hit
    max_len = field.get("max_length")
    if max_len and len(ans) > int(max_len):
        ans = ans[: int(max_len)].rsplit(" ", 1)[0]
    v.update(kind=kind, answer=ans)
    v.setdefault("facts_used", [])
    return v
