"""Which changes the Doctor's AI may apply for the person, and why the others wait for their tick."""

from __future__ import annotations

from typing import Any

import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.agent_apply import AGENT_APPLIED_STEPS, agent_applies, waits_because
from engine.doctor.case import CaseItem
from engine.doctor.protocol import AGENT
from engine.doctor.visit import _item
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import FIRST


def item(
    payload: dict[str, Any], *, kind: str = "recipe_step", source: ProposalSource = ProposalSource.AI
) -> CaseItem:
    return _item(
        kind,
        payload,
        title="A change",
        upload_id=FIRST,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=vs.T0,
        source=source,
    )


def step(kind: str, column: str = "channel", **params: Any) -> dict[str, Any]:
    return {"upload_id": FIRST, "step": {"order": 1, "kind": kind, "column": column, "params": params}}


PLAIN_PARAMS: dict[str, dict[str, Any]] = {
    "parse_number": {},
    "normalise_text": {"strip": True},
    "split_column": {"separator": "-", "part": 0},
    "rename_column": {},
    "remove_rows": {"operator": "starts_with", "values": ["999"]},
}


def test_the_kinds_the_agent_may_apply_are_exactly_these() -> None:
    assert set(AGENT_APPLIED_STEPS) == set(PLAIN_PARAMS)


@pytest.mark.parametrize("kind", sorted(PLAIN_PARAMS))
def test_the_kinds_the_agent_may_apply_wait_for_nothing(kind: str) -> None:
    assert waits_because(item(step(kind, **PLAIN_PARAMS[kind]))) is None


def test_a_parameter_left_empty_or_at_its_default_decides_nothing() -> None:
    assert waits_because(item(step("parse_number", decimal=".", indian_scale_words=False))) is None
    assert waits_because(item(step("normalise_text", strip=True, merge={}))) is None


@pytest.mark.parametrize(
    "kind",
    [
        "parse_date",
        "map_values",
        "map_boolean",
        "convert_units",
        "fill_missing",
        "set_missing",
        "set_missing_text",
        "valid_range",
        "drop_column",
    ],
)
def test_a_change_of_meaning_or_of_how_a_value_is_read_waits_for_the_persons_tick(kind: str) -> None:
    """The day/month order, merged spellings, yes/no words, units, blanks, ranges and hiding a column are the person's."""
    assert waits_because(item(step(kind))) == "this kind of change is yours to tick"


@pytest.mark.parametrize(
    ("kind", "params"),
    [
        ("parse_number", {"decimal": ","}),
        ("parse_number", {"indian_scale_words": True}),
        ("normalise_text", {"strip": True, "merge": {"email ": "Email"}}),
    ],
)
def test_a_kind_the_agent_may_apply_waits_when_it_decides_how_the_values_are_read(
    kind: str, params: dict[str, Any]
) -> None:
    assert waits_because(item(step(kind, **params))) == "how it reads the values is yours to confirm"


def test_a_warning_a_yes_of_its_own_or_a_person_s_item_waits() -> None:
    assert "many" in str(waits_because(item({**step("parse_number"), "warnings": ["AI_STEP_CHANGES_MANY"]})))
    assert waits_because(item({**step("parse_number"), "needs_yes": True})) == "it needs your own yes"
    assert waits_because(item(step("parse_number"), source=ProposalSource.PERSON)) is not None
    ticked = item(step("parse_number")).model_copy(update={"state": ProposalState.ACCEPTED})
    assert waits_because(ticked) is not None, "only a new, waiting change is the agent's to apply"


def test_a_query_a_setting_and_a_role_are_never_applied_by_the_agent() -> None:
    assert waits_because(item({**step("sql_query"), "receipt": {}})) is not None
    assert waits_because(item({"path": "x", "value": 1}, kind="setting")) is not None
    assert waits_because(item({"column": "age"}, kind="role")) is not None


def _combine(**more: Any) -> CaseItem:
    payload = {
        "upload_ids": ["a", "b"],
        "receipt": {"rows_in": 10},
        "findings": [],
        "needs_yes": False,
        **more,
    }
    return item(payload, kind="stack_files")


def test_a_stack_that_was_counted_and_found_clean_may_be_applied() -> None:
    assert waits_because(_combine(rows_in=100, rows_out=100)) is None


@pytest.mark.parametrize(
    ("more", "says"),
    [
        ({"findings": [{"level": "flag", "code": "X"}]}, "something to look at"),
        ({"findings": [{"level": "stop", "code": "X"}]}, "something to look at"),
        ({"receipt": None}, "could not be counted"),
        ({"needs_yes": True}, "needs your own yes"),
        ({"rows_in": 100, "rows_out": 90}, "needs your own yes"),  # leaves out more than 2%: APPROVE-class
    ],
)
def test_a_stack_that_was_not_counted_flagged_or_leaves_out_too_much_waits(
    more: dict[str, Any], says: str
) -> None:
    assert says in str(waits_because(_combine(**more)))


def test_the_guard_run_decides_and_the_next_change_is_judged_with_the_first_in_place() -> None:
    world = cs.orders_world()
    files = tuple(world.files.values())
    filter_ids = item(step("remove_rows", "customer_id", operator="starts_with", values=["999"]))
    stop = item(step("remove_rows", "order_date", operator="before", values=["2026-05-15"]))
    tidy = item(step("normalise_text", "channel", strip=True))
    decided = agent_applies(files, [], [], [filter_ids, stop, tidy], vs.T0)
    (a, a_waits), (b, b_waits), (c, c_waits) = decided
    assert (
        a_waits is None
        and a.state is ProposalState.ACCEPTED
        and a.decided_by == AGENT
        and a.decided_at == vs.T0
    )
    assert (
        b_waits is not None
        and "more than 5%" in b_waits
        and b.state is ProposalState.PENDING
        and b.decided_by is None
    )
    assert c_waits is None and c.state is ProposalState.ACCEPTED
