"""The guards bite and feed the screen: a stop blocks Approve until a person says yes, a changed plan takes the yes
away, the screen's ledger carries the names the plan screen reads, and a replay stops where the plan would."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import JsonValue

from engine.access.roles import Principal, Role
from engine.agent.contracts import (
    AgentConfidence,
    DataRecipe,
    DecidedBy,
    ProposalSource,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
    recipe_hash,
)
from engine.agent.dataset_recipe import run_saved_recipe
from engine.agent.recipe import RecipeError
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case, CaseError, CaseItem, CaseStore, VisitState, payload_digest
from engine.doctor.protocol import item_sentence
from engine.doctor.screen import build_screen
from engine.doctor.visit import VisitDeps
from engine.doctor.visit_contracts import PlanCheck
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, T0

MEERA = Principal(user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST}), kind="user")
LOSES = "step.loses_half"
STEP = RecipeStep(
    order=1,
    kind=RecipeStepKind.REMOVE_ROWS,
    column="marketing_opt_in",
    params={"operator": "equals", "values": [0]},
    reason="Leave out customers who did not opt in.",
    decided_by=DecidedBy.USER,
)


def _at_approve(tmp_path: Path) -> tuple[LocalStorage, VisitDeps, Case]:
    """A real case, walked to Approve with its key and outcome answered and a goal chosen."""
    storage = LocalStorage(tmp_path)
    w = vs.world()
    deps = vs.deps_for(storage, w)
    case = vs.open_case(storage, w)
    for _name, _op, act in vs.SCRIPT[:4]:
        case = act(vs.deps_for(storage, w), CaseStore(storage).get(CLIENT, case.case_id))
    assert case.state is VisitState.APPROVE
    return storage, deps, case


def _add_losing_step(storage: LocalStorage, deps: VisitDeps, case: Case, *, ticked: bool = True) -> Case:
    """A person's own step that leaves out about half the customers, ticked; the visit then counts it again."""
    payload: dict[str, JsonValue] = {"upload_id": FIRST, "step": STEP.model_dump(mode="json")}
    item = CaseItem(
        item_id=LOSES,
        kind="recipe_step",
        title=STEP.reason,
        source=ProposalSource.PERSON,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED if ticked else ProposalState.REJECTED,
        upload_id=FIRST,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=T0,
    )
    CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update={"items": (*c.items, item), "state": VisitState.TREAT}), []),
    )
    return visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)


def _with_own_yes(deps: VisitDeps, case: Case) -> Case:
    """Leaving rows out needs its own yes (the protocol's rule); the stops are a separate gate after it."""
    item = next(i for i in case.items if i.item_id == LOSES)
    return visit.confirm(
        CLIENT,
        case.case_id,
        LOSES,
        deps,
        principal=MEERA,
        sentence=item_sentence(item.title, item.payload),
        expected_version=case.version,
    )


def _approve(deps: VisitDeps, case: Case) -> Case:
    return visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)


def _ledger(storage: LocalStorage, case: Case) -> dict[str, Any]:
    view = build_screen(storage, CLIENT, case.case_id, config_root=None).view
    assert view.ledger is not None
    return view.ledger


def test_an_unacknowledged_stop_blocks_approve_and_a_yes_by_its_key_lets_it_through(tmp_path: Path) -> None:
    storage, deps, case = _at_approve(tmp_path)
    case = _with_own_yes(deps, _add_losing_step(storage, deps, case))
    assert case.state is VisitState.APPROVE

    ledger = _ledger(storage, case)
    stops = [f for f in ledger["findings"] if f["level"] == "stop"]
    assert stops and all(f["kind"] == "guard" and f["key"] for f in stops)
    with pytest.raises(CaseError) as refused:
        _approve(deps, case)
    assert refused.value.code == "APPROVAL_STOP_OPEN"

    for stop in stops:
        case = visit.acknowledge_stop(
            CLIENT,
            case.case_id,
            stop["key"],
            deps,
            expected_version=case.version,
            actor=ANALYST,
            reason="Test accounts, on purpose.",
        )
    again = _ledger(storage, case)
    assert [f["level"] for f in again["findings"] if f["key"] in {s["key"] for s in stops}] == ["flag"] * len(
        stops
    )
    assert all(f["acknowledged"] for f in again["findings"] if f["key"] in {s["key"] for s in stops})
    assert case.visit is not None and {a.key for a in case.visit.acknowledged} == {s["key"] for s in stops}
    assert (
        case.visit.acknowledged[0].reason == "Test accounts, on purpose."
        and case.visit.acknowledged[0].by == ANALYST
    )
    events = [
        e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.kind.value == "stop.acknowledged"
    ]
    assert len(events) == len(stops) and events[0].details["reason"] == "Test accounts, on purpose."

    approved = _approve(deps, case)
    assert approved.state is not VisitState.APPROVE and approved.approvals


def test_a_yes_needs_a_real_stop_and_the_plan_as_it_stands(tmp_path: Path) -> None:
    storage, deps, case = _at_approve(tmp_path)
    case = _with_own_yes(deps, _add_losing_step(storage, deps, case))
    with pytest.raises(CaseError) as unknown:
        visit.acknowledge_stop(
            CLIENT, case.case_id, "0" * 16, deps, expected_version=case.version, actor=ANALYST
        )
    assert unknown.value.code == "VISIT_UNKNOWN_CHOICE"


def test_a_changed_plan_takes_the_yes_away(tmp_path: Path) -> None:
    storage, deps, case = _at_approve(tmp_path)
    case = _with_own_yes(deps, _add_losing_step(storage, deps, case))
    stops = [f for f in _ledger(storage, case)["findings"] if f["level"] == "stop"]
    for stop in stops:
        case = visit.acknowledge_stop(
            CLIENT, case.case_id, stop["key"], deps, expected_version=case.version, actor=ANALYST
        )
    assert case.visit is not None and case.visit.acknowledged

    # The person unticks the step and ticks it again: it is another plan each time it changes.
    case = visit.decide(
        CLIENT, case.case_id, LOSES, False, deps, expected_version=case.version, actor=ANALYST
    )
    assert case.visit is not None and case.visit.acknowledged == ()
    case = visit.decide(CLIENT, case.case_id, LOSES, True, deps, expected_version=case.version, actor=ANALYST)
    assert case.state is VisitState.APPROVE and case.visit is not None and case.visit.acknowledged == ()
    with pytest.raises(CaseError) as refused:
        _approve(deps, case)
    assert refused.value.code == "APPROVAL_STOP_OPEN"


def test_counts_are_not_trusted_for_another_plan(tmp_path: Path) -> None:
    """Counts kept for another plan (or none) are never an all-clear: Approve counts the plan it is given."""
    storage, deps, case = _at_approve(tmp_path)
    case = _with_own_yes(deps, _add_losing_step(storage, deps, case))
    assert case.visit is not None
    stale = case.visit.model_copy(update={"plan_check": PlanCheck(digest="another-plan")})
    case = CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update={"visit": stale}), []),
    )
    with pytest.raises(CaseError) as refused:
        _approve(deps, case)
    assert refused.value.code == "APPROVAL_STOP_OPEN"


def test_the_screen_view_carries_the_ledger_fields_the_plan_screen_reads(tmp_path: Path) -> None:
    """The exact names `ui/modules/datadoctor/plan.js` documents: whole-plan funnel, per-step receipt, findings."""
    storage, deps, case = _at_approve(tmp_path)
    case = _with_own_yes(deps, _add_losing_step(storage, deps, case))
    ledger = _ledger(storage, case)

    for name in (
        "rows_in",
        "rows_out",
        "customers_in",
        "customers_out",
        "positive_rate_in",
        "positive_rate_out",
        "steps",
        "findings",
    ):
        assert name in ledger, name
    assert ledger["rows_in"] > ledger["rows_out"] > 0
    assert ledger["customers_in"] > ledger["customers_out"] > 0
    assert 0 <= ledger["positive_rate_in"] <= 100 and 0 <= ledger["positive_rate_out"] <= 100

    step = next(s for s in ledger["steps"] if s["item_id"] == LOSES)
    for name in (
        "rows_in",
        "rows_out",
        "customers_in",
        "customers_out",
        "customers_lost",
        "customers_duplicated",
        "totals",
        "level",
    ):
        assert name in step, name
    assert (
        step["rows_out"] < step["rows_in"]
        and step["customers_lost"] > 0
        and step["level"] in {"stop", "flag"}
    )
    assert all({"column", "before", "after"} <= set(t) for t in step["totals"])
    for finding in ledger["findings"]:
        assert finding["level"] in {"stop", "flag", "note"} and finding["kind"] == "guard"
        assert finding["text"] and finding["key"] and "item_id" in finding
    # Counted once for this plan and kept on the case: the numbers come from the stored count, not a new run.
    assert case.visit is not None and case.visit.plan_check is not None
    assert ledger["steps"] == _ledger(storage, case)["steps"]


def test_a_plan_with_no_steps_has_nothing_to_stop(tmp_path: Path) -> None:
    _storage, deps, case = _at_approve(tmp_path)
    assert case.visit is not None and case.visit.plan_check is not None
    assert not [f for f in case.visit.plan_check.findings if f.level == "stop"]
    assert _approve(deps, case).approvals


# ---- the replay (next month, scoring) ---------------------------------------------------------------------------
def _recipe(*, acknowledged: tuple[str, ...] | None = ()) -> DataRecipe:
    steps = (STEP,)
    return DataRecipe(
        recipe_id="rc-0000000000000001",
        use_case_id=GOAL_A,
        source_fingerprint="f" * 64,
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key="customer_id",
        target="converted",
        acknowledged_stops=acknowledged,
        created_at=T0,
    )


def _new_month() -> pd.DataFrame:
    return vs.customers(rows=400, seed=7)


def test_a_replay_stops_on_a_stop_instead_of_scoring_silently() -> None:
    config = load_use_case(GOAL_A)
    with pytest.raises(RecipeError) as stopped:
        run_saved_recipe(_new_month(), _recipe(), config, upload_id="up-next")
    assert stopped.value.code == "RECIPE_GUARD_STOP"
    assert "customers" in stopped.value.message


def test_a_replay_goes_on_where_the_person_said_yes_to_that_kind_of_stop() -> None:
    config = load_use_case(GOAL_A)
    first = run_saved_recipe(_new_month(), _recipe(acknowledged=None), config, upload_id="up-next")
    codes = ("PLAN_LOSES_TOO_MUCH", "PLAN_MOVES_OUTCOME_RATE")
    again = run_saved_recipe(_new_month(), _recipe(acknowledged=codes), config, upload_id="up-next")
    assert len(again.frame) == len(first.frame) < 400


def test_a_recipe_approved_without_the_guards_replays_as_it_was_approved() -> None:
    """Guided setup never counted its intended losses, so a replay cannot tell them from new ones: it is not stopped."""
    run = run_saved_recipe(
        _new_month(), _recipe(acknowledged=None), load_use_case(GOAL_A), upload_id="up-next"
    )
    assert 0 < len(run.frame) < 400
