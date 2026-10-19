"""The question catalogue: every question renders, its answers are c-indexed, its recommendation is as the
catalogue says, and "I don't know" always leads to who would know with a ready message and a safe default."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.doctor.protocol import DONT_KNOW, Ask, settle
from engine.doctor.questions import (
    CATALOGUE,
    QuestionError,
    build_ask,
    catalogue_lines,
    question,
    ready_message,
    render_options,
    required_fields,
    who_would_know,
)
from engine.doctor.visit_contracts import OptionEffect

SAMPLE: dict[str, object] = {
    "file": "customers.csv",
    "column": "cust_status",
    "columns": "last_order_value, orders_90d",
    "count": 412,
    "kind": "sheets",
    "hint": "values between 100 and 5000000 (about 100 times larger than the other file)",
    "latest_date": "30 Sep 2026",
    "name_date": "31 Aug 2026",
    "back_date": "28 Sep 2026",
    "as_of": "30 Sep 2026",
    "value": "bilkul",
    "date_column": "updated_at",
    "campaign": "Diwali SMS",
    "client": "Acme Retail",
    "question": "Which column is the main customer ID in customers.csv?",
    "id": "order_id",
    "detail": "The order total is the same on every row.",
    "reason": "were never active",
    "positives": 30,
    "outcome": "churn",
}
CHOICES = ("customer_id", "account_no", "phone")


def _ask(qid: str) -> Ask:
    return build_ask(qid, SAMPLE, rows=3000, choices=CHOICES, recommended_choice=1)


def test_the_catalogue_holds_q1_to_q24() -> None:
    assert list(CATALOGUE) == [f"Q{n}" for n in range(1, 25)]
    codes = [spec.code for spec in CATALOGUE.values()]
    assert len(set(codes)) == 24


@pytest.mark.parametrize("qid", list(CATALOGUE))
def test_every_question_renders_with_sample_values(qid: str) -> None:
    spec = question(qid)
    assert required_fields(spec) <= set(SAMPLE), f"{qid} needs {sorted(required_fields(spec) - set(SAMPLE))}"
    ask = _ask(qid)
    assert ask.text.strip() and ask.why.strip()
    assert "{" not in ask.text and "{" not in ask.why
    assert all("{" not in option.label for option in ask.options)


@pytest.mark.parametrize("qid", list(CATALOGUE))
def test_the_answers_are_c_indexed_and_end_with_i_dont_know(qid: str) -> None:
    ask = _ask(qid)
    ids = [option.effect.option_id for option in ask.options]
    assert ids == [f"c{n}" for n in range(len(ids))]
    last = ask.options[-1]
    assert last.label == DONT_KNOW and last.safe
    assert sum(1 for option in ask.options if option.safe) == 1
    assert len(ask.options) >= 3 or qid == "Q18"  # at least two answers beside "I don't know" (Q18 has two)


@pytest.mark.parametrize("qid", list(CATALOGUE))
def test_the_recommendation_is_exactly_one_where_the_catalogue_says_so(qid: str) -> None:
    spec = question(qid)
    ask = _ask(qid)
    ids = {option.effect.option_id for option in ask.options}
    if spec.recommends:
        assert ask.recommended in ids and ask.recommended != ask.options[-1].effect.option_id
    else:
        assert ask.recommended is None
    if spec.meaning_bearing:
        assert ask.recommended is None and ask.meaning_bearing


def test_the_recommended_answers_are_the_ones_the_study_names() -> None:
    labels = {
        qid: {o.effect.option_id: o.label for o in _ask(qid).options}[str(_ask(qid).recommended)]
        for qid in ("Q2", "Q5", "Q10", "Q13", "Q15", "Q18")
    }
    assert labels["Q2"] == "Merge when the phone number and date of birth are the same"
    assert labels["Q5"] == "Not known"
    assert labels["Q10"] == "Bought in the last 12 months"
    assert labels["Q13"] == "No"
    assert labels["Q15"] == "In the fairness report only"
    assert labels["Q18"].startswith("India time")
    # A recommendation taken from the data points at the caller's pick among the choices.
    assert _ask("Q1").recommended == "c1"
    assert _ask("Q6").recommended == "c1"


def test_meaning_bearing_questions_are_the_ones_about_meaning() -> None:
    meaning = {qid for qid, spec in CATALOGUE.items() if spec.meaning_bearing}
    assert {"Q7", "Q8", "Q9", "Q11", "Q12", "Q14", "Q17", "Q20", "Q24"} == meaning
    assert not any(spec.recommends for spec in CATALOGUE.values() if spec.meaning_bearing)


def test_the_blocking_questions_are_few_and_decide_the_table_or_the_contact_list() -> None:
    assert {qid for qid, spec in CATALOGUE.items() if spec.blocking} == {
        "Q1",
        "Q8",
        "Q10",
        "Q12",
        "Q14",
        "Q24",
    }


def test_an_optional_answer_is_left_out_when_its_value_is_missing() -> None:
    values = {key: value for key, value in SAMPLE.items() if key not in {"name_date", "back_date"}}
    labels = render_options(question("Q8"), values)
    assert labels == ("The latest date in the file: 30 Sep 2026", "I will type a date")
    ask = build_ask("Q8", values, rows=3000)
    assert [o.effect.option_id for o in ask.options] == ["c0", "c1", "c2"]


def test_a_missing_value_or_unknown_question_is_a_coded_mistake_of_the_caller() -> None:
    with pytest.raises(QuestionError, match="no value for file"):
        build_ask("Q20", {}, rows=10)
    with pytest.raises(QuestionError, match="unknown question"):
        question("Q99")
    with pytest.raises(QuestionError, match="needs `choices`"):
        build_ask("Q1", SAMPLE, rows=10)


def test_counts_effects_and_rates_follow_the_answers() -> None:
    effects = [
        OptionEffect(option_id="x", kind="define_outcome", title="t", payload={"n": n}) for n in range(3)
    ]
    ask = build_ask(
        "Q10",
        SAMPLE,
        rows=3000,
        rows_changed=[0, 400, 900],
        label_rates=[4.0, 6.5, 9.0],
        effects=effects,
    )
    assert [o.rows_changed for o in ask.options] == [0, 400, 900, None]
    assert [o.label_rate for o in ask.options] == [4.0, 6.5, 9.0, None]
    assert [o.effect.option_id for o in ask.options] == ["c0", "c1", "c2", "c3"]
    assert ask.options[1].effect.kind == "define_outcome" and ask.options[3].effect.kind is None
    with pytest.raises(QuestionError, match="rows_changed has 2 entries"):
        build_ask("Q10", SAMPLE, rows=3000, rows_changed=[1, 2])


def test_a_catalogue_ask_goes_through_the_protocol_like_any_other() -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    differing = build_ask("Q10", SAMPLE, rows=3000, rows_changed=[0, 400, 900])
    settled = settle(differing, now=now)
    assert settled.how == "ask" and settled.question is not None
    assert settled.question.blocking and settled.question.recommended == "c0"
    assert [o.option_id for o in settled.question.options] == ["c0", "c1", "c2", "c3"]
    tiny = build_ask("Q10", SAMPLE, rows=3000, rows_changed=[0, 1, 2])
    assumed = settle(tiny, now=now)
    assert assumed.how == "assume" and assumed.question is not None and assumed.question.answer == "c0"
    # A question about meaning is never answered by the rules, even when the choices barely differ.
    meaning = build_ask("Q12", SAMPLE, rows=3000, rows_changed=[0, 1, 2])
    safe = settle(meaning, now=now)
    assert safe.question is not None and safe.question.answer == "c3" and safe.question.recommended is None


def test_the_subject_makes_per_value_questions_distinct() -> None:
    first = build_ask("Q12", SAMPLE, rows=10)
    second = build_ask("Q12", {**SAMPLE, "value": "nahi"}, rows=10)
    assert first.subject != second.subject


# ---------------------------------------------------------------------------
# "I don't know" -> who would know
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("qid", list(CATALOGUE))
def test_i_dont_know_gives_roles_a_ready_message_and_a_safe_default(qid: str) -> None:
    known = who_would_know(qid, SAMPLE, choices=CHOICES, file_name="customers.csv")
    assert known.roles and known.waiting_for == f"Waiting for the {known.roles[0]}"
    assert known.safe_default.startswith("Until then I will ") and known.safe_default.endswith(".")
    assert known.safe_default != "Until then I will ."
    assert "customers.csv" in known.message
    assert f"the {known.roles[0]}" in known.message
    assert _ask(qid).text in known.message
    for label in render_options(question(qid), SAMPLE, CHOICES):
        assert f"- {label}" in known.message
    assert "{" not in known.message


def test_the_ready_message_is_addressed_to_the_role_asked_for() -> None:
    known = who_would_know("Q14", SAMPLE, role="compliance officer")
    assert known.waiting_for == "Waiting for the compliance officer"
    assert "only the compliance officer can give" in known.message
    other = who_would_know("Q14", SAMPLE, role="plumber")
    assert other.waiting_for == f"Waiting for the {question('Q14').roles[0]}"
    assert ready_message("Q14", SAMPLE, role="CRM admin").startswith("Hello,")


def test_consent_is_never_assumed_yes_while_waiting() -> None:
    assert "not agreed" in who_would_know("Q14", SAMPLE).safe_default
    assert "random holdout" in who_would_know("Q13", SAMPLE).safe_default


def test_the_agent_gets_one_line_per_question() -> None:
    lines = catalogue_lines()
    assert len(lines) == 23 and lines[0].startswith("Q1 ") and "(blocking)" in lines[0]
    assert "choices" in lines[0] and "file" in lines[0]


def test_what_counts_as_a_yes_is_a_question_only_the_chat_asks_and_it_never_recommends() -> None:
    """Q24: the person says in their own words what a sale is; the options are those words. Meaning: blocking, no
    recommendation, and the review's prompt does not list it."""
    spec = question("Q24")
    assert spec.chat_only and spec.meaning_bearing and spec.blocking and not spec.recommends
    assert len(catalogue_lines(chat=True)) == 24 and "Q24" not in "".join(catalogue_lines())
    ask = build_ask(
        "Q24", {"file": "orders.csv"}, rows=1200, choices=("An order over 500 is a sale", "Any order")
    )
    assert [o.label for o in ask.options] == ["An order over 500 is a sale", "Any order", DONT_KNOW]
    assert ask.recommended is None and ask.blocking and ask.meaning_bearing


def test_a_recommended_choice_outside_the_choices_is_refused() -> None:
    with pytest.raises(QuestionError):
        build_ask("Q1", SAMPLE, rows=3000, choices=CHOICES, recommended_choice=3)


@pytest.mark.parametrize("qid", list(CATALOGUE))
def test_an_answer_claims_an_outcome_only_when_it_carries_a_step(qid: str) -> None:
    from engine.doctor.protocol import NO_STEP, _case_question

    spec = question(qid)
    assert all(not o.effect for o in spec.options if o.repeat), "a repeated answer is already a sentence"
    ask = _ask(qid)
    shown = _case_question(ask, "q", ask.options).options
    assert all(o.effect == NO_STEP for o in shown), "no step: the screen claims nothing happens"
    step = OptionEffect(option_id="c0", kind="doctor.step", title="Fix it")
    with_step = build_ask(qid, SAMPLE, rows=3000, choices=CHOICES, effects=[step] * (len(ask.options) - 1))
    first = _case_question(with_step, "q", with_step.options).options[0].effect
    assert first != NO_STEP and "{" not in first and "“customer_id” rows" not in first
