"""The goal check reads the tables the AI's own query steps make, not only the raw upload (replay blocker).

An invoice log has no outcome; the AI's SQL step builds one row per customer with `ordered_again_90d`. The visit
offers that column as the outcome (the person's yes decides it) instead of asking for more data, and the readiness
check then reads it as the target.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import AgentConfidence, ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case
from engine.doctor.diagnose import LoadedFile, Offer
from engine.doctor.goal_brief import load_goal_brief
from engine.doctor.visit import PrepareError, VisitDeps
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import (
    ANALYST,
    CLIENT,
    FIRST,
    World,
    deps_for,
    open_case,
    option,
    question,
)

GOAL = "retail-win-back"
STEP: dict[str, Any] = {
    "order": 1,
    "kind": "sql_query",
    "column": "customer_training",
    "reason": "One row per customer, with whether they placed another order in the 90 days after the snapshot",
    "params": {
        "sql": "SELECT customer_id, 1 AS ordered_again_90d FROM invoices",
        "inputs": ["customers_oct"],
        "output_name": "customer_training",
        "description": "One row per customer",
    },
}


def _invoices() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(600)],
            "invoice_date": ["2026-09-01"] * 600,
            "amount": np.round(rng.gamma(2.0, 40.0, 600), 2),
        }
    )


@pytest.fixture
def world() -> World:
    config = load_use_case(GOAL)
    file = vs.loaded(FIRST, "customers_oct.csv", _invoices(), config)
    return World(files={FIRST: file}, offers=(Offer(config=config, stage="Retain"),))


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _deps(storage: LocalStorage, w: World, items: tuple[Any, ...] = ()) -> VisitDeps:
    def port(case: Case, message: str, files: tuple[LoadedFile, ...]) -> visit.ConverseResult:
        return visit.ConverseResult(reply="I built a table.", items=items, model_calls=1)

    return deps_for(storage, w, converse=port)


def _sql_item(w: World, columns: list[str], *, ticked: bool = False) -> Any:
    return visit._item(
        "recipe_step",
        {"step": STEP, "stage": "post_build", "columns": columns},
        title="One row per customer",
        upload_id=None,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=w.clock(),
        ticked=ticked,
    )


def _asked(storage: LocalStorage, w: World, columns: list[str]) -> Case:
    case = visit.resume(CLIENT, open_case(storage, w).case_id, _deps(storage, w), actor=ANALYST)
    case = visit.chat(
        CLIENT, case.case_id, "Build me a training table.", _deps(storage, w, (_sql_item(w, columns),)),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    return visit.set_goal(
        CLIENT, case.case_id, GOAL, _deps(storage, w), expected_version=case.version, actor=ANALYST
    )


def test_the_column_an_ai_query_makes_is_offered_as_the_outcome_not_asked_for_as_data(
    storage: LocalStorage, world: World
) -> None:
    case = _asked(storage, world, ["customer_id", "ordered_again_90d", "recency_days"])
    q = question(case, "OUTCOME_COLUMN")
    assert [o.label for o in q.options] == ["ordered_again_90d", "None of these"]
    assert not [r for r in case.questions if r.code.startswith("DATA_REQ")]
    assert not any(i.kind == "outcome_column" for i in case.items), "never decided silently"
    assert "outcome_missing" not in " ".join(d.doubt_id for d in case.doubts)
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "ordered_again_90d"), _deps(storage, world),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    (outcome,) = (i for i in case.items if i.kind == "outcome_column")
    assert outcome.state is ProposalState.ACCEPTED and outcome.payload["column"] == "ordered_again_90d"
    assert outcome.payload["upload_id"] == FIRST


def test_a_query_column_that_does_not_read_as_the_outcome_is_not_offered(
    storage: LocalStorage, world: World
) -> None:
    case = _asked(storage, world, ["customer_id", "recency_days", "spend"])
    assert not [q for q in case.questions if q.code == "OUTCOME_COLUMN" and q.answer is None]
    assert any(d.doubt_id.startswith("d.outcome_missing") for d in case.doubts)


def test_readiness_runs_on_the_prepared_table_with_the_decided_column_as_the_target(
    storage: LocalStorage, world: World
) -> None:
    case = _asked(storage, world, ["customer_id", "ordered_again_90d"])
    q = question(case, "OUTCOME_COLUMN")
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "ordered_again_90d"), _deps(storage, world),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    seen: list[tuple[str, str]] = []

    def prepare(config: Any, upload_id: str, key: Any, target: str) -> str:
        seen.append((upload_id, target))
        raise PrepareError("stop here")

    brief = load_goal_brief(GOAL)
    result = visit.readiness_verify(storage, None, prepare)(case, brief, ())
    assert seen == [(FIRST, "ordered_again_90d")], "the run reads the prepared table with this target"
    assert "VISIT_NO_OUTCOME" not in result.reason
