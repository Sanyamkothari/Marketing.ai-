"""An answer can carry a typed value, and the as-of answer sets the date (AI-first A1c).

Q8 offers "I will type a date"; the person could not give 2011-11-30 because an answer was only an option id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.doctor import visit
from engine.doctor.agent_review import case_as_of
from engine.doctor.carry import _answers_mark
from engine.doctor.case import Case, CaseError, CaseQuestion, CaseStore, TimelineKind
from engine.doctor.protocol import _case_question, _with_dont_know
from engine.doctor.questions import build_ask
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_visit import _deps, _generic
from tests.unit.doctor.visit_support import ANALYST, CLIENT, World


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _q8() -> CaseQuestion:
    ask = build_ask("Q8", {"latest_date": "2011-12-09"}, rows=100)
    question = _case_question(ask, "q8", _with_dont_know(ask))
    return question.model_copy(update={"code": "Q08_AS_OF"})


def _with_q8(storage: LocalStorage, w: World) -> Case:
    case = _generic(storage, w)

    def change(c: Case) -> tuple[Case, tuple[()]]:
        return c.model_copy(update={"questions": (*c.questions, _q8())}), ()

    return CaseStore(storage).update(
        CLIENT, case.case_id, expected_version=case.version, actor=ANALYST, change=change
    )


def _typed(storage: LocalStorage, w: World, case: Case, option_id: str, value: str | None) -> Case:
    return visit.answer(
        CLIENT, case.case_id, "q8", option_id, _deps(storage, w),
        expected_version=case.version, actor=ANALYST, value=value,
    )  # fmt: skip


def test_the_typed_date_option_says_what_it_does_and_asks_for_a_date() -> None:
    question = _q8()
    typed = next(o for o in question.options if o.label == "I will type a date")
    assert (
        typed.value_kind == "date"
        and typed.effect == "Your date is used as the cut-off for the steps that follow."
    )
    assert all(o.value_kind is None for o in question.options if o is not typed)
    assert "value_kind" not in question.options[0].model_dump(mode="json", exclude_defaults=False)


def test_a_typed_date_is_stored_and_becomes_the_as_of_date(storage: LocalStorage, world: World) -> None:
    case = _with_q8(storage, world)
    typed = next(o for o in case.questions[-1].options if o.value_kind == "date")
    answered = _typed(storage, world, case, typed.option_id, " 2011-11-30 ")
    question = next(q for q in answered.questions if q.question_id == "q8")
    assert question.answer_value == "2011-11-30"
    assert case_as_of(answered) == "2011-11-30"  # the typed value wins over the label
    event = [
        e
        for e in CaseStore(storage).timeline(CLIENT, case.case_id)
        if e.kind is TimelineKind.QUESTION_ANSWERED and e.item_id == "q8"
    ][-1]
    assert event.details["value"] == "2011-11-30"


@pytest.mark.parametrize("bad", [None, "", "30/11/2011", "2011-13-40", "2011-2-3", "tomorrow"])
def test_a_missing_or_bad_date_is_refused(storage: LocalStorage, world: World, bad: str | None) -> None:
    case = _with_q8(storage, world)
    typed = next(o for o in case.questions[-1].options if o.value_kind == "date")
    with pytest.raises(CaseError) as refused:
        _typed(storage, world, case, typed.option_id, bad)
    assert refused.value.code == "VISIT_ANSWER_VALUE_INVALID"


def test_an_option_that_takes_no_value_refuses_one(storage: LocalStorage, world: World) -> None:
    case = _with_q8(storage, world)
    with pytest.raises(CaseError) as refused:
        _typed(storage, world, case, "c0", "2011-11-30")
    assert refused.value.code == "VISIT_ANSWER_VALUE_INVALID"
    plain = _typed(storage, world, case, "c0", None)
    assert next(q for q in plain.questions if q.question_id == "q8").answer_value is None


def test_an_old_case_without_the_new_fields_loads_unchanged() -> None:
    old = {"question_id": "q1", "text": "?", "options": [{"option_id": "c0", "label": "Yes"}]}
    question = CaseQuestion.model_validate(old)
    assert question.answer_value is None and question.options[0].value_kind is None
    assert question.model_dump(mode="json", exclude_defaults=False).get("answer_value") is None
    assert "answer_value" not in question.model_dump(mode="json")
    assert "value_kind" not in question.options[0].model_dump(mode="json")


def test_answering_another_option_clears_the_typed_date(storage: LocalStorage, world: World) -> None:
    case = _with_q8(storage, world)
    typed = next(o for o in case.questions[-1].options if o.value_kind == "date")
    first = _typed(storage, world, case, typed.option_id, "2011-11-30")
    again = _typed(storage, world, first, "c0", None)
    question = next(q for q in again.questions if q.question_id == "q8")
    assert question.answer_value is None
    assert case_as_of(again) == "2011-12-09"  # the label's date, now that nothing is typed


def test_a_changed_typed_date_is_a_changed_answer(storage: LocalStorage, world: World) -> None:
    case = _with_q8(storage, world)
    typed = next(o for o in case.questions[-1].options if o.value_kind == "date")
    nov = _typed(storage, world, case, typed.option_id, "2011-11-30")
    oct_ = _typed(storage, world, nov, typed.option_id, "2011-10-31")
    assert _answers_mark(nov.questions) != _answers_mark(oct_.questions)
    assert visit.prescription_digest(nov, None) != visit.prescription_digest(oct_, None)


def test_a_suggestion_never_points_at_an_answer_that_needs_a_value(
    storage: LocalStorage, world: World
) -> None:
    case = _with_q8(storage, world)
    typed = next(o for o in case.questions[-1].options if o.value_kind == "date")
    step = visit._Step(to=case.state)
    assert visit._suggested(case, step, ("q8", typed.option_id)) is None
    assert visit._suggested(case, step, ("q8", "c0")) is not None
