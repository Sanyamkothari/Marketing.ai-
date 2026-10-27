"""The case's own as-of date is `$as_of` for the AI's queries (AI-first A1; found by the Online Retail replay).

The person left out the unfinished last month ("Use 2011-11-30 as the as-of date") and answered the as-of question,
yet every query the AI wrote with `$as_of` was refused with SQL_NEEDS_AS_OF, and one with the date typed in with
SQL_FIXED_DATE: nothing turned the answers into a date.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import duckdb

from engine.agent.contracts import ProposalState
from engine.agent.sql_step import _error_kind
from engine.doctor.case import Case
from engine.doctor.decisions import decisions


def case_as_of(case: Case) -> str | None:
    return decisions(case).as_of_iso


def _case(items: list[Any] = (), questions: list[Any] = ()) -> Case:  # type: ignore[assignment]
    return cast(Case, SimpleNamespace(items=list(items), questions=list(questions), goals=[]))


def _item(payload: dict[str, Any], state: ProposalState = ProposalState.ACCEPTED) -> Any:
    return SimpleNamespace(
        payload=payload,
        state=state,
        kind="step",
        item_id="i1",
        decided_by=None,
        decided_at=None,
        upload_id=None,
        goal_id=None,
        proposed_at=None,
    )


def _as_of_question(answer: str | None, typed: str | None = None) -> Any:
    options = [
        SimpleNamespace(option_id="c0", label="The latest date in the file: 2011-12-09", sets="as_of"),
        SimpleNamespace(option_id="c1", label="I will type a date", sets="as_of"),
    ]
    return SimpleNamespace(
        code="Q08_AS_OF",
        question_id="q1",
        answer=answer,
        answer_value=typed,
        options=options,
        answered_by="u1",
        answered_at=None,
        upload_id=None,
        goal_id=None,
    )


def test_leaving_out_the_unfinished_month_sets_the_as_of_date() -> None:
    assert case_as_of(_case([_item({"as_of": "2011-11-30"})])) == "2011-11-30"


def test_a_pending_or_rejected_item_does_not() -> None:
    assert case_as_of(_case([_item({"as_of": "2011-11-30"}, ProposalState.PENDING)])) is None


def test_the_as_of_answer_gives_the_date_its_option_names() -> None:
    assert case_as_of(_case(questions=[_as_of_question("c0")])) == "2011-12-09"
    assert case_as_of(_case(questions=[_as_of_question("c1")])) is None  # a typed date with no value yet
    assert case_as_of(_case(questions=[_as_of_question(None)])) is None


def test_a_typed_date_is_the_as_of_date_and_beats_the_label() -> None:
    assert case_as_of(_case(questions=[_as_of_question("c1", "2011-11-30")])) == "2011-11-30"
    assert case_as_of(_case(questions=[_as_of_question("c0", "2011-11-30")])) == "2011-11-30"
    assert case_as_of(_case(questions=[_as_of_question("c1", "not a date")])) is None


def test_an_accepted_step_wins_over_a_typed_date() -> None:
    case = _case([_item({"as_of": "2011-10-31"})], [_as_of_question("c1", "2011-11-30")])
    assert case_as_of(case) == "2011-10-31"


def test_an_accepted_step_wins_over_the_question() -> None:
    case = _case([_item({"as_of": "2011-11-30"})], [_as_of_question("c0")])
    assert case_as_of(case) == "2011-11-30"


def test_a_conversion_error_says_what_it_wanted_but_never_the_cell() -> None:
    for sql, wanted in (
        ("SELECT CAST('12/1/2010 8:34' AS DATE)", "a date YYYY-MM-DD"),
        ("SELECT CAST('C536379' AS INTEGER)", "INT32"),
    ):
        try:
            duckdb.sql(sql).fetchall()
        except duckdb.Error as exc:
            said = _error_kind(exc)
        assert said.startswith("Conversion Error: ") and wanted in said
        assert "12/1/2010" not in said and "C536379" not in said
