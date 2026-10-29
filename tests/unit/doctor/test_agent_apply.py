"""Which changes the Doctor's AI may apply for the person, and why the others wait for their tick."""

from __future__ import annotations

from typing import Any

import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import load_use_case
from engine.doctor import agent_apply
from engine.doctor.agent_apply import (
    AGENT_APPLIED_STEPS,
    Roles,
    agent_applies,
    meaning_key,
    twin_of,
    waits_because,
)
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


def test_a_warning_or_a_yes_of_its_own_waits() -> None:
    assert "many" in str(waits_because(item({**step("parse_number"), "warnings": ["AI_STEP_CHANGES_MANY"]})))
    assert waits_because(item({**step("parse_number"), "needs_yes": True})) == "it needs your own yes"
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


# ---------------------------------------------------------------------------
# Columns that hold a meaning are the person's (review of v2a-engine-chat)
# ---------------------------------------------------------------------------
ROLES = Roles(
    protected=frozenset({"marketing_opt_in", "converted", "snapshot_date"}), ids=frozenset({"customer_id"})
)


@pytest.mark.parametrize(
    ("kind", "params"),
    [
        ("rename_column", {}),
        ("split_column", {"separator": "-", "part": 0}),
        ("parse_number", {}),
        ("normalise_text", {"strip": True}),
    ],
)
@pytest.mark.parametrize("column", ["marketing_opt_in", "converted", "snapshot_date"])
def test_a_change_to_a_consent_outcome_or_date_column_waits_for_the_persons_tick(
    kind: str, params: dict[str, Any], column: str
) -> None:
    why = waits_because(item(step(kind, column, **params)), ROLES)
    assert why is not None and "who may be contacted or what the model learns" in why
    assert (
        waits_because(item(step(kind, "channel", **params)), ROLES) is None
    ), "other columns are not held back"


def test_a_column_renamed_to_a_protected_name_or_named_like_consent_waits_too() -> None:
    to_protected = step("rename_column", "channel")
    to_protected["step"]["new_column"] = "converted"
    assert waits_because(item(to_protected), ROLES) is not None
    # Without any goal or key decided, a name that reads as consent is still not the agent's to change.
    for name in ("marketing_opt_in", "Opt Out", "unsubscribed", "sms_consent", "DND"):
        assert waits_because(item(step("rename_column", name))) is not None, name


def test_leaving_rows_out_by_the_id_is_the_one_step_on_the_id_the_agent_may_apply() -> None:
    assert (
        waits_because(item(step("remove_rows", "customer_id", operator="starts_with", values=["999"])), ROLES)
        is None
    )
    assert waits_because(item(step("normalise_text", "customer_id", strip=True)), ROLES) is not None
    assert (
        waits_because(item(step("remove_rows", "converted", operator="equals", values=["0"])), ROLES)
        is not None
    )
    assert (
        waits_because(item(step("remove_rows", "marketing_opt_in", operator="equals", values=["0"])))
        is not None
    )


def test_what_counts_as_the_same_change_ignores_the_models_wording_and_the_trial_counts() -> None:
    plain = step("remove_rows", "customer_id", operator="starts_with", values=["999"])
    worded = {**plain, "step": {**plain["step"], "reason": "Test ids.", "order": 3}}
    first, again = item(plain), item({**worded, "tried": {"rows": 1000, "changed": 31, "failed": 0}})
    assert meaning_key(first) == meaning_key(again)
    assert twin_of([first], again) is first
    other = item(step("remove_rows", "customer_id", operator="starts_with", values=["998"]))
    assert twin_of([first], other) is None
    # What a trial on the first rows counted is not part of what the change is: one step, one id.
    assert first.item_id == item({**plain, "tried": {"rows": 1000, "changed": 31, "failed": 0}}).item_id


def test_a_rule_that_hits_few_of_the_first_rows_but_many_of_the_file_waits() -> None:
    """The guard run sees the first 1000 rows; the rule is also counted on the whole file."""
    world = cs.orders_world()
    files = tuple(world.files.values())
    tail = item(step("remove_rows", "customer_id", operator="starts_with", values=["101"]))  # rows 1000-1199
    ((_, why),) = agent_applies(files, [], [], [tail], vs.T0)
    assert why is not None and "of the rows of the whole file, more than 5%" in why


def test_a_date_rule_over_dates_whose_order_nobody_settled_waits() -> None:
    frame = cs.orders_frame()
    frame["order_date"] = ["03/04/2026", "05/06/2026", "01/02/2026"] * (len(frame) // 3)
    config = load_use_case(vs.GOAL_A)
    loaded = vs.loaded(FIRST, "orders.csv", frame, config)
    before = item(step("remove_rows", "order_date", operator="before", values=["2026-01-01"]))
    ((_, why),) = agent_applies((loaded,), [], [], [before], vs.T0)
    assert why is not None and "day first or month first" in why


def test_a_new_stop_on_another_step_holds_the_change_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.doctor.visit_contracts import PlanCheck, PlanFinding

    stop = PlanFinding(
        key="k", level="stop", code="PLAN_STEP_CANNOT_RUN", text="Another step breaks.", item_id="x.1"
    )
    calls: list[int] = []

    def fake(
        files: object, case_files: object, items: list[CaseItem], as_of: object = None, **_: object
    ) -> PlanCheck:
        calls.append(len(items))
        return PlanCheck(digest="d", findings=(stop,) if len(items) > 1 else ())

    monkeypatch.setattr(agent_apply, "check_plan", fake)
    tidy = item(step("normalise_text", "channel", strip=True))
    other = tidy.model_copy(update={"item_id": "x.1"})
    world = cs.orders_world()
    ((_, why),) = agent_applies(tuple(world.files.values()), [], [other], [tidy], vs.T0)
    assert why == "Another step breaks."


def test_the_check_binds_the_decided_as_of_date_to_a_query_already_in_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the date the accepted query fails in both runs of the check, the file is never prepared, and the new
    step would be ticked unguarded."""
    from datetime import date

    from engine.doctor.plan_check import check_plan
    from tests.unit.doctor.test_decisions import log_file, query_item

    accepted = query_item().model_copy(update={"state": ProposalState.ACCEPTED})
    tidy = _item(
        "recipe_step",
        {
            "upload_id": "up-1",
            "step": {"order": 1, "kind": "normalise_text", "column": "Customer", "params": {"strip": True}},
        },
        title="A change",
        upload_id="up-1",
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=vs.T0,
        source=ProposalSource.AI,
    )
    files = (log_file(),)
    given: list[date | None] = []

    def spy(files: Any, case_files: Any, items: Any, as_of: date | None = None, **_: Any) -> Any:
        given.append(as_of)
        return check_plan(files, case_files, items, as_of=as_of)

    monkeypatch.setattr(agent_apply, "check_plan", spy)
    day = date(2025, 1, 31)
    agent_applies(files, [], [accepted], [tidy], vs.T0, as_of=day)
    assert given == [day, day]
    # and the query really runs with it: no step is reported as unable to run
    assert not [f for f in check_plan(files, [], [accepted], as_of=day).findings if f.level == "stop"]


def test_whoever_proposes_it_a_change_is_placed_by_the_same_rule() -> None:
    """The rule is about the change, not the author: the AI's and the default author's are placed alike."""
    for source in (ProposalSource.AI, ProposalSource.RULES):
        assert waits_because(item(step("parse_number"), source=source)) is None
        assert (
            waits_because(item(step("parse_date"), source=source)) == "this kind of change is yours to tick"
        )


def test_what_the_values_prove_and_the_goals_own_build_go_into_the_plan() -> None:
    sure = item(step("parse_date")).model_copy(update={"confidence": AgentConfidence.SURE})
    assert waits_because(sure) is None
    build = {**step("sql_query"), "needs_yes": True, "buildable": {"goal_id": "g"}}
    assert waits_because(item(build)) is None, "its own yes comes at Approve"
    assert waits_because(item({**build, "buildable": None})) == "it needs your own yes"
    assert waits_because(sure.model_copy(update={"payload": {**sure.payload, "needs_yes": True}})) == (
        "it needs your own yes"
    )
