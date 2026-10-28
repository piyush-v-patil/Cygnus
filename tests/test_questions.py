"""Answers from what you already said: near-match suggestions and work preferences.

What can go wrong here is a wrong answer sent in your name: a saved answer
reused for a question that only looks alike ("worked for Google" vs "work at
Google"), or anything at all said about visa, legal or demographic questions.
"""
from datetime import date

import pytest

from autoapply.config import Answer, WorkCfg
from autoapply.questions import (as_value, near_answer, on_resume, protected, similarity,
                                 standing_answer, standing_value)


def saved(original, answer, kind="yes_no", added=date(2026, 10, 1)):
    return Answer(original=original, answer=answer, kind=kind, added=added)


# ---- near matches ----------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Are you willing to relocate?", "Open to relocation?"),
    ("Are you willing to relocate?", "Would you consider relocating for this role?"),
    ("Are you willing to move to Austin, TX?", "Are you willing to relocate to Austin, TX?"),
    ("Are you open to working on-site?", "Are you comfortable working in the office?"),
    ("Are you willing to work hybrid?", "Are you open to a hybrid schedule?"),
    ("What are your salary expectations?", "Desired compensation"),
    ("How did you hear about us?", "How did you hear about this position?"),
])
def test_the_same_question_worded_differently_is_near(a, b):
    assert similarity(a, b) >= 0.6


@pytest.mark.parametrize("a,b", [
    ("Are you willing to relocate?", "Are you willing to travel?"),
    ("Have you previously worked for Google?", "Are you willing to work at Google?"),    # past vs future
    ("How many years of Python experience?", "How many years of experience with Java?"),
    ("Are you willing to relocate to Austin?", "Are you willing to relocate to Seattle?"),
    ("Are you willing to work weekends?", "Are you not willing to work weekends?"),      # negated
])
def test_questions_that_only_look_alike_are_not(a, b):
    assert similarity(a, b) < 0.6


def test_the_closest_saved_answer_is_suggested_with_its_question():
    items = [saved("Are you willing to travel?", "No"), saved("Are you willing to relocate?", "Yes")]
    assert near_answer(items, "Open to relocation?", "yes_no") == ("Yes", "Are you willing to relocate?")


def test_a_suggestion_must_be_one_of_the_options():
    items = [saved("Are you willing to relocate?", "Yes")]
    assert near_answer(items, "Open to relocation?", "choice", ["Yes, anywhere", "No"]) is None
    assert near_answer(items, "Open to relocation?", "choice", ["yes", "no"]) == ("yes", "Are you willing to relocate?")


def test_a_suggestion_must_be_of_a_compatible_kind():
    items = [saved("Years of experience with Python", "3", kind="number")]
    assert near_answer(items, "How many years of Python experience?", "number") == ("3", "Years of experience with Python")
    assert near_answer(items, "How many years of Python experience?", "long_text") is None


def test_the_newest_of_equally_close_answers_wins():
    items = [saved("Open to relocation?", "No", added=date(2026, 1, 1)),
             saved("Willing to relocate?", "Yes", added=date(2026, 9, 1))]
    assert near_answer(items, "Are you willing to relocate?", "yes_no")[0] == "Yes"


@pytest.mark.parametrize("label", [
    "Will you now or in the future require visa sponsorship?",
    "Are you legally authorized to work in the United States?",
    "Are you a U.S. citizen?", "Do you hold an active security clearance?",
    "Have you ever been convicted of a felony?", "Are you a protected veteran?",
    "What is your gender?", "Do you have a disability?",
])
def test_legal_visa_and_demographic_questions_are_never_suggested_or_answered(label):
    assert protected(label)
    items = [saved(label, "No"), saved(label.replace("?", " now?"), "No")]
    assert near_answer(items, label + " ", "yes_no") is None
    work = WorkCfg(relocate=True, anywhere_in_us=True, onsite=True, hybrid=True, remote=True,
                   relocation_assistance=False, only_resume_employers=True)
    assert standing_answer(label, work, "Acme", "") is None


def test_a_saved_legal_answer_never_answers_another_question():
    items = [saved("Will you require sponsorship to relocate?", "No")]
    assert near_answer(items, "Are you willing to relocate?", "yes_no") is None


# ---- work preferences ------------------------------------------------------

YOU = WorkCfg(relocate=True, anywhere_in_us=True, onsite=True, hybrid=True, remote=True,
              relocation_assistance=False, only_resume_employers=True)


@pytest.mark.parametrize("label,want", [
    ("Are you willing to relocate?", "yes"),
    ("Would you be willing to move to Austin?", "yes"),
    ("Do you live within commuting distance or are you willing to relocate?", "yes"),
    ("Will you require relocation assistance?", "no"),
    ("Are you able to work on-site 5 days a week?", "yes"),
    ("Are you comfortable working in a hybrid environment (3 days in office)?", "yes"),
    ("Are you open to working remotely?", "yes"),
    ("Are you open to working in any of our US office locations?", "yes"),
    ("Have you ever worked for Capital One or any of its subsidiaries?", "no"),
    ("Are you a current or former employee of Capital One?", "no"),
    ("Have you previously been employed by us?", "no"),
])
def test_your_work_preferences_answer(label, want):
    assert standing_answer(label, YOU, "Capital One", "Software Engineer Intern, Amazon")[0] == want


@pytest.mark.parametrize("label", [
    "Do you currently live in Austin, TX?",                 # a fact about you, not a preference
    "Is this role remote?",
    "Are you willing to work in any of the following locations?",
    "Do you have any relatives who work for Capital One?",
    "Have you ever worked for the federal government?",
    "Have you previously applied to Capital One?",
    "Please list your previous employers",
])
def test_questions_that_are_not_preferences_are_left_alone(label):
    assert standing_answer(label, YOU, "Capital One", "") is None


def test_an_employer_on_your_resume_is_yours_to_answer():
    assert standing_answer("Have you ever worked for Amazon?", YOU, "Amazon",
                           "Software Engineer Intern, Amazon Web Services") is None
    assert on_resume("Amazon.com Services LLC", "Intern at Amazon")


def test_unset_preferences_answer_nothing():
    unset = WorkCfg()
    for label in ("Are you willing to relocate?", "Are you able to work on-site?",
                  "Have you ever worked for Acme?"):
        assert standing_answer(label, unset, "Acme", "") is None


def test_a_preference_of_no_answers_no():
    assert standing_answer("Are you open to working remotely?", WorkCfg(remote=False), "", "")[0] == "no"


def test_the_answer_takes_the_form_of_the_field():
    assert as_value("yes", "yes_no", None) == "Yes"
    assert as_value("yes", "choice", ["Yes, I am", "No, I am not"]) == "Yes, I am"
    assert as_value("yes", "choice", ["Yes, at my own expense", "Yes, with assistance", "No"]) is None
    assert as_value("no", "number", None) is None
    assert standing_value("Are you willing to relocate?", "choice", ["Yes", "No"], YOU)[0] == "Yes"
