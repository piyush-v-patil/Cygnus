"""Ledger state machine, question normalisation and the answer bank (§8.6, §11)."""
import pytest

from autoapply.config import AnswerBank
from autoapply.ledger import (CLOSED, DISCOVERED, ENRICHED, ERROR, FILTERED_OUT, IllegalTransition, Ledger,
                              READY, SCORED, SKIPPED, SUBMITTED, FILLING)
from autoapply.normalize import html_to_text, normalize_question


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "l.sqlite")


def test_unique_posting_key_and_refs(led):
    assert led.upsert_discovered("greenhouse:a:1", {"source_name": "s", "source_ref": "a:1", "company": "A"})
    assert not led.upsert_discovered("greenhouse:a:1", {"source_name": "s", "source_ref": "a:2", "title": "T"})
    row = led.get("greenhouse:a:1")
    assert row.state == DISCOVERED and row["title"] == "T" and row["company"] == "A"
    assert sorted(led.refs_for("greenhouse:a:1")) == [("s", "a:1"), ("s", "a:2")]
    assert led.db.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1


def test_happy_path_transitions(led):
    led.upsert_discovered("k", {"source_name": "s"})
    for st in (ENRICHED, SCORED, FILLING, READY, SUBMITTED):
        led.set_state("k", st)
    assert led.get("k").state == SUBMITTED
    with pytest.raises(IllegalTransition):
        led.set_state("k", READY)          # submitted is final: never apply twice


def test_illegal_and_recheckable(led):
    led.upsert_discovered("k", {"source_name": "s"})
    with pytest.raises(IllegalTransition):
        led.set_state("k", READY)
    led.set_state("k", FILTERED_OUT, "tier")
    led.set_state("k", DISCOVERED)         # rule outcomes are re-checked
    led.set_state("k", ENRICHED)
    led.set_state("k", SKIPPED, "low_fit")
    with pytest.raises(IllegalTransition):
        led.set_state("k", DISCOVERED)     # a paid-for skip is final


def test_error_counter(led):
    led.upsert_discovered("k", {"source_name": "s"})
    led.set_state("k", ERROR, "enrich: x")
    led.set_state("k", ERROR, "enrich: y")
    assert led.get("k")["error_count"] == 2
    led.set_state("k", CLOSED)


def test_spend(led):
    led.record_usage("score", "m", {"input_tokens": 10, "output_tokens": 5}, 0.25, "k")
    led.record_usage("score", "m", {"input_tokens": 10, "output_tokens": 5}, 0.5, "k")
    assert led.month_spend() == pytest.approx(0.75)


@pytest.mark.parametrize("raw,norm", [
    ("Are you willing to work onsite 5 days a week?", "are you willing to work onsite 5 days a week"),
    ("Pronouns *", "pronouns"),
    ("First Name*", "first name"),
    ("Email ✱", "email"),
    ("Why  Acme?\n(required)", "why acme"),
    ("Ｗｈｙ　us？", "why us"),
])
def test_normalize_question(raw, norm):
    assert normalize_question(raw) == norm


def test_answer_bank_exact_match_and_kinds(tmp_path):
    bank = AnswerBank(tmp_path / "answers.yaml")
    bank.add("Are you willing to work onsite 5 days a week?", "yes_no", "Yes")
    assert bank.lookup("Are you willing to work onsite 5 days a week? *", "yes_no") == "Yes"
    # a choice field: the saved answer must be one of the options
    assert bank.lookup("are you willing to work onsite 5 days a week", "choice", ["Yes", "No"]) == "Yes"
    assert bank.lookup("are you willing to work onsite 5 days a week", "choice", ["Maybe", "Never"]) is None
    assert bank.lookup("Willing to work onsite?", "yes_no") is None          # v1: exact only
    assert bank.lookup("are you willing to work onsite 5 days a week", "long_text") is None
    again = AnswerBank(tmp_path / "answers.yaml")
    assert again.items[0].question == "are you willing to work onsite 5 days a week"
    assert again.items[0].source == "user"


def test_html_to_text_handles_greenhouse_double_escaping():
    html = "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    t = html_to_text(html)
    assert "Build & ship" in t and "- Python" in t and "<" not in t
