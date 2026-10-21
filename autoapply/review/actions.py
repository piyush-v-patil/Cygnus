"""Review-queue actions that do not need the browser (§10): answers, reject, give up."""
from __future__ import annotations

from ..config import AnswerBank
from ..ledger import NEEDS_INPUT, READY, REJECTED_BY_USER, Ledger
from ..llm.answer import match_option
from ..normalize import normalize_question
from ..writeback import skip_change


def _compatible(missing: dict, kind: str, answer: str) -> str | None:
    """The value to use for `missing` given a saved (kind, answer), or None."""
    mk = missing.get("answer_kind")
    if mk != kind and {mk, kind} not in ({"yes_no", "choice"}, {"short_text", "long_text"}):
        return None
    if missing.get("options"):
        return match_option(answer, missing["options"])
    return answer


def save_answers(led: Ledger, bank: AnswerBank, posting_key: str, answers: dict[str, str]) -> dict:
    """Store answers for one application, learn them, and unblock matching items.

    `answers` maps a missing field's key to the user's value. Returns
    {"ready": [...posting keys moved to Ready], "still_missing": n,
     "added": {posting_key: [plan entries added]}} - the entries a live tab still needs.
    """
    app = led.application(posting_key)
    if app is None:
        raise KeyError(posting_key)
    learned: list[tuple[str, str, str]] = []
    added: dict[str, list[dict]] = {}
    plan, still = list(app["fill_plan"] or []), []
    for m in app["missing"]:
        val = (answers.get(m["key"]) or "").strip()
        if not val:
            still.append(m)
            continue
        if m.get("options") and m.get("kind") != "checkbox_group":
            hit = match_option(val, m["options"])
            if hit is None:
                still.append(m)
                continue
            val = hit
        entry = {"key": m["key"], "label": m["label"], "kind": m["kind"], "value": val,
                 "source": "user", "required": m.get("required", True), "options": m.get("options", [])}
        plan.append(entry)
        added.setdefault(posting_key, []).append(entry)
        if m.get("kind") != "file":
            bank.add(m["label"], m["answer_kind"], val, source="user")
            learned.append((normalize_question(m["label"]), m["answer_kind"], val))
    led.upsert_application(posting_key, fill_plan=plan, missing=still)
    moved = []
    if not still and led.get(posting_key).state == NEEDS_INPUT:
        led.set_state(posting_key, READY, None, "answered in review")
        moved.append(posting_key)
    moved += propagate(led, learned, skip=posting_key, added=added)
    return {"ready": moved, "still_missing": len(still), "added": added}


def propagate(led: Ledger, learned: list[tuple[str, str, str]], skip: str | None = None,
              added: dict | None = None) -> list[str]:
    """Apply newly learned answers to every other queued Needs-input application."""
    if not learned:
        return []
    added = added if added is not None else {}
    moved = []
    for app in led.applications_in_lane(NEEDS_INPUT):
        key = app["posting_key"]
        if key == skip:
            continue
        plan, still, changed = list(app["fill_plan"] or []), [], False
        for m in app["missing"]:
            hit = None
            for q, kind, ans in learned:
                if m.get("question", normalize_question(m["label"])) == q:
                    hit = _compatible(m, kind, ans)
                    if hit is not None:
                        break
            if hit is None:
                still.append(m)
                continue
            changed = True
            entry = {"key": m["key"], "label": m["label"], "kind": m["kind"], "value": hit,
                     "source": "user", "required": m.get("required", True), "options": m.get("options", [])}
            plan.append(entry)
            added.setdefault(key, []).append(entry)
        if changed:
            led.upsert_application(key, fill_plan=plan, missing=still)
            if not still:
                led.set_state(key, READY, None, "answered via another application")
                moved.append(key)
    return moved


def reject(ctx, posting_key: str) -> None:
    ctx.ledger.set_state(posting_key, REJECTED_BY_USER, "user_rejected")
    if ctx.writeback is not None:
        ctx.writeback.queue(posting_key, skip_change("user_rejected"))


def give_up(ctx, posting_key: str) -> None:
    # not written back: the user may still apply by hand later
    ctx.ledger.set_state(posting_key, REJECTED_BY_USER, "gave_up")
