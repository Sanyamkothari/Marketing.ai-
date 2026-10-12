"""The `normalise_key` step (Plan O M237, DEC-1807 (b)): one ID written one way, by a frozen rule.

Row-wise and stateless (a cell's output depends on that cell and the rule only), counted on its receipt,
refused on the recipe's ID and outcome, replayed identically - and it never makes two IDs meet by being
similar: `CUST-00123` meets `123` only through an approved rule, and `124` never.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.key_steps import (
    KEY_JOIN_OK,
    KeyRule,
    key_coverage,
    normalise_key_value,
    normalise_keys,
    propose_key_rules,
)
from engine.agent.loop import CHAT_STEP_KINDS
from engine.agent.recipe import STEP_PHASE, RecipeError, check_recipe, run_recipe


def _step(order: int, column: str, **params: Any) -> RecipeStep:
    return RecipeStep(order=order, kind=RecipeStepKind.NORMALISE_KEY, column=column, params=params)


def _run(frame: pd.DataFrame, steps: list[RecipeStep], **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "upload_id": "u1",
        "primary_key": "row_id",
        "target": "churned",
        "levels": [AgentLevel.CLEAN],
        "max_failure_pct": 50.0,
    }
    options.update(kwargs)
    return run_recipe(frame, steps, **options)


# ---------------------------------------------------------------------------
# The rule, value by value
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "rule", "expected"),
    [
        ("CUST-00123", KeyRule(prefix="CUST-", zeros=True), "123"),
        ("CUST-00123", KeyRule(prefix="CUST-"), "00123"),
        ("cust-00123", KeyRule(prefix="CUST-", zeros=True, case=True), "123"),
        ("ACC_77", KeyRule(prefix="CUST-", zeros=True), "ACC_77"),  # no prefix: kept
        ("CUST-", KeyRule(prefix="CUST-"), "CUST-"),  # nothing after the prefix: kept
        ("000", KeyRule(zeros=True), "0"),
        (" C 77 ", KeyRule(spaces=True), "C77"),
        ("C-77", KeyRule(case=True), "c-77"),
        (123.0, KeyRule(zeros=True), "123"),  # a number column with a blank reads 123 as 123.0
        (7, KeyRule(zeros=True), "7"),
    ],
)
def test_the_rule_writes_each_id_one_way(value: Any, rule: KeyRule, expected: str) -> None:
    assert normalise_key_value(value, rule) == (expected, True)


@pytest.mark.parametrize(
    "value",
    [
        "9876543210",
        "098765 43210",
        "+91 98765 43210",
        "91-98765-43210",
        "0091 9876543210",
        "(+91) 98765-43210",
        9876543210,
    ],
)
def test_indian_phone_numbers_become_e164(value: Any) -> None:
    assert normalise_key_value(value, KeyRule(phone=True)) == ("+919876543210", True)


def test_a_foreign_e164_number_is_kept_and_a_non_phone_is_counted_not_guessed() -> None:
    assert normalise_key_value("+44 20 7946 0958", KeyRule(phone=True)) == ("+442079460958", True)
    assert normalise_key_value("12345", KeyRule(phone=True)) == ("12345", False)
    outcome = normalise_keys(pd.Series(["9876543210", "12345", None, "abc"]), KeyRule(phone=True))
    assert list(outcome.values) == ["+919876543210", "12345", None, "abc"]
    assert (outcome.changed, outcome.failed) == (1, 2)
    assert outcome.failed_examples == ("12345", "abc")


def test_empty_cells_stay_empty_and_are_not_counted() -> None:
    outcome = normalise_keys(pd.Series([None, float("nan"), "", "C1"]), KeyRule(case=True))
    assert list(outcome.values)[:3] == [None, None, None]
    assert (outcome.changed, outcome.failed) == (1, 0)


def test_phone_cannot_be_combined_with_prefix_or_zeros() -> None:
    with pytest.raises(ValueError, match="phone"):
        KeyRule(phone=True, zeros=True)


@settings(max_examples=60, deadline=None)
@given(
    values=st.lists(
        st.one_of(st.none(), st.text(alphabet="CUSTcust-0123 ", max_size=12), st.integers(0, 10**6)),
        min_size=1,
        max_size=30,
    ),
    start=st.integers(0, 29),
)
def test_the_step_is_stateless(values: list[Any], start: int) -> None:
    """A row's output depends on that row only: any slice of the column gives the same rows' answers."""
    rule = KeyRule(prefix="CUST-", zeros=True, case=True, spaces=True)
    whole = normalise_keys(pd.Series(values, dtype=object), rule).values
    part = pd.Series(values[start:], index=range(start, len(values)), dtype=object)
    sliced = normalise_keys(part, rule).values
    assert list(sliced) == list(whole[start:])


# ---------------------------------------------------------------------------
# Proposing a rule: exact equality after the rule, never a near match
# ---------------------------------------------------------------------------
def test_cust_00123_meets_123_only_through_the_proposed_rule() -> None:
    customers = pd.Series([f"CUST-{n:05d}" for n in range(1, 201)])
    bills = pd.Series([str(n) for n in range(1, 201)])
    assert key_coverage(bills, customers) == 0.0
    proposal = propose_key_rules(bills, customers)
    assert proposal.base_rule == KeyRule(prefix="CUST-", zeros=True)
    assert proposal.rule == KeyRule(zeros=True)
    assert (proposal.coverage, proposal.fixed_coverage) == (0.0, 1.0)
    assert proposal.needed and proposal.meets


def test_nothing_meets_on_a_near_match() -> None:
    customers = pd.Series([f"CUST-{n:05d}" for n in range(1, 201)])
    near = pd.Series([f"CUST-0O{n:03d}" for n in range(1, 201)])  # a letter O for a zero
    off_by_one = pd.Series([str(n + 1000) for n in range(1, 201)])
    for keys in (near, off_by_one):
        proposal = propose_key_rules(keys, customers)
        assert not proposal.meets
        assert proposal.fixed_coverage < KEY_JOIN_OK


def test_ids_that_already_meet_need_no_rule() -> None:
    keys = pd.Series(["A1", "A2", "A3"])
    proposal = propose_key_rules(keys, pd.Series(["A1", "A2", "A3", "A4"]))
    assert not proposal.needed and proposal.fixed_coverage == 1.0


def test_phone_ids_are_proposed_the_phone_rule() -> None:
    crm = pd.Series([f"98765{n:05d}" for n in range(300)])
    other = pd.Series([f"+91 98765 {n:05d}" for n in range(300)])
    proposal = propose_key_rules(other, crm)
    assert proposal.rule == KeyRule(phone=True) == proposal.base_rule
    assert proposal.fixed_coverage == 1.0


# ---------------------------------------------------------------------------
# In the recipe
# ---------------------------------------------------------------------------
def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4],
            "customer": ["CUST-00123", " cust-0007 ", None, "CUST-00042"],
            "mobile": ["098765 43210", "91-98765-43211", "12", "9876543212"],
            "churned": [0, 1, 0, 1],
        }
    )


def test_the_recipe_runs_the_step_and_counts_it() -> None:
    steps = [
        _step(1, "customer", prefix="CUST-", zeros=True, case=True, spaces=True),
        _step(2, "mobile", phone=True),
    ]
    run = _run(_frame(), steps)
    assert list(run.frame["customer"]) == ["123", "7", None, "42"]
    assert list(run.frame["mobile"]) == ["+919876543210", "+919876543211", "12", "+919876543212"]
    first, second = run.receipt.steps
    assert (first.kind, first.rows, first.changed, first.failed) == (RecipeStepKind.NORMALISE_KEY, 4, 3, 0)
    assert (second.changed, second.failed, second.examples_failed) == (3, 1, ("12",))


def test_too_many_unreadable_phones_stop_the_run() -> None:
    with pytest.raises(RecipeError) as raised:
        _run(_frame(), [_step(1, "mobile", phone=True)], max_failure_pct=5.0)
    assert raised.value.code == "RECIPE_VALUES_UNCONVERTED"


def test_the_step_never_touches_the_id_or_the_outcome() -> None:
    for column in ("row_id", "churned"):
        with pytest.raises(RecipeError) as raised:
            _run(_frame(), [_step(1, column, zeros=True)])
        assert raised.value.code == "RECIPE_STEP_INVALID"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"zeros": False},
        {"zeros": "yes"},
        {"prefix": 7},
        {"phone": True, "zeros": True},
        {"lower": True},
        {"prefix": "X" * 21},
    ],
)
def test_a_rule_that_says_nothing_or_too_much_is_refused(params: dict[str, Any]) -> None:
    with pytest.raises(RecipeError) as raised:
        check_recipe(
            [_step(1, "customer", **params)],
            columns=list(_frame().columns),
            primary_key="row_id",
            target="churned",
            levels=[AgentLevel.CLEAN],
        )
    assert raised.value.code == "RECIPE_STEP_INVALID"


def test_the_step_makes_no_new_column() -> None:
    with pytest.raises(ValueError, match="normalise_key"):
        RecipeStep(
            order=1, kind=RecipeStepKind.NORMALISE_KEY, column="c", new_column="d", params={"zeros": True}
        )


def test_next_months_file_is_written_the_same_way() -> None:
    """Replay: the frozen rule on a new file gives each repeated value the same output."""
    steps = [_step(1, "customer", prefix="CUST-", zeros=True)]
    first = _run(_frame(), steps).frame
    later = _frame().iloc[::-1].reset_index(drop=True)
    again = _run(later, steps).frame
    assert dict(zip(first["row_id"], first["customer"], strict=True)) == dict(
        zip(again["row_id"], again["customer"], strict=True)
    )


def test_the_step_tidies_with_the_other_values_and_is_a_clean_level_step() -> None:
    assert STEP_PHASE[RecipeStepKind.NORMALISE_KEY] == STEP_PHASE[RecipeStepKind.PARSE_NUMBER]
    check_recipe(
        [_step(1, "customer", zeros=True)],
        columns=list(_frame().columns),
        primary_key="row_id",
        target="churned",
        levels=[AgentLevel.CLEAN],
    )


def test_the_ai_cannot_propose_the_step() -> None:
    """Rules propose `normalise_key` (design §5.5 E); the chat's step list does not include it."""
    assert RecipeStepKind.NORMALISE_KEY not in CHAT_STEP_KINDS
