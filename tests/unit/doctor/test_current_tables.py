"""The case's current tables (`engine.doctor.current_tables`): what the plan leaves is what the rules, the detectors
and the AI's looks read; a table a query builds is a table with its columns, key and time column, and the upload stays
available as it came in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.tools import AgentContext, call_tool
from engine.config import RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.case import Case, CaseStore
from engine.doctor.cleaning import cleaning_detector
from engine.doctor.current_tables import BUILT_PREFIX, current_tables, is_built
from engine.doctor.diagnose import LoadedFile, Offer
from engine.doctor.visit import active_section
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
SQL = (
    "SELECT customer_id, MAX(CAST(order_date AS DATE)) AS last_order, "
    "CASE WHEN SUM(amount) > 260 THEN 1 ELSE 0 END AS ordered_again_90d "
    "FROM t_customers_oct GROUP BY customer_id"
)
STEP: dict[str, Any] = {
    "order": 1,
    "kind": "sql_query",
    "column": "customer_training",
    "reason": "One row per customer, with whether they placed another order in the 90 days after the snapshot",
    "params": {
        "sql": SQL,
        "inputs": ["customers_oct"],
        "output_name": "customer_training",
        "description": "One row per customer",
    },
}


def _log() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    ids = [f"C{1000 + i % 150}" for i in range(600)]
    return pd.DataFrame(
        {
            "customer_id": ids,
            "order_date": ["2026-09-01"] * 600,
            "amount": rng.integers(10, 120, 600),
        }
    )


@pytest.fixture
def world() -> World:
    config = load_use_case(GOAL)
    file = vs.loaded(FIRST, "customers_oct.csv", _log(), config)
    return World(files={FIRST: file}, offers=(Offer(config=config, stage="Retain"),))


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _query_item(w: World, *, state: ProposalState = ProposalState.PENDING) -> Any:
    item = visit._item(
        "recipe_step",
        {"step": STEP, "stage": "post_build"},  # no recorded column names: the table is the one that is built
        title="One row per customer",
        upload_id=None,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=w.clock(),
        ticked=False,
        source=ProposalSource.AI,
    )
    return item.model_copy(update={"state": state})


def _case(storage: LocalStorage, w: World) -> Case:
    return open_case(storage, w)


def test_a_table_a_query_builds_is_a_table_with_its_columns_key_and_time(
    storage: LocalStorage, world: World
) -> None:
    case = _case(storage, world)
    config = world.offers[0].config
    files = tuple(world.files.values())
    item = _query_item(world)
    found = current_tables(case, [item], files, config=config)
    (table,) = found.built
    assert table.name == "customer_training" and is_built(table.loaded.upload_id)
    assert table.loaded.upload_id == BUILT_PREFIX + "customer_training"
    assert set(table.columns) == {"customer_id", "last_order", "ordered_again_90d"}
    assert table.key == "customer_id" and table.time_column == "last_order"
    assert "ordered_again_90d" in table.two_valued
    assert table.origin.upload_id == FIRST
    assert len(table.loaded.frame) == 150, "one row per customer, built from the rows read"
    assert found.raw == files, "the upload stays available as it came in"
    assert found.files == files, "no accepted fix: the very same files"
    again = current_tables(case, [item], files, config=config)
    assert again is found, "cached by the plan's digest"
    assert current_tables(case, [item], files, config=config, raw=True).built == ()


def test_describe_outcome_works_on_a_column_the_ai_built(storage: LocalStorage, world: World) -> None:
    case = _case(storage, world)
    config = world.offers[0].config
    files = tuple(world.files.values())
    (table,) = current_tables(case, [_query_item(world)], files, config=config).built
    ctx = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=None,
        upload_id=table.loaded.upload_id,
        mode=RunMode.TRAIN,
        profile=table.loaded.profile,
        frame=table.loaded.frame,
    )
    out = call_tool(ctx, "describe_outcome", {"column": "ordered_again_90d"}, evidence_id="t1").result
    assert out["rows"] == 150 and out["two_valued"] is True
    assert out["positives"] + out["negatives"] == 150


def test_a_query_the_person_left_out_builds_no_table(storage: LocalStorage, world: World) -> None:
    case = _case(storage, world)
    item = _query_item(world, state=ProposalState.REJECTED)
    found = current_tables(case, [item], tuple(world.files.values()), config=world.offers[0].config)
    assert found.built == ()


def test_the_goal_check_sees_a_built_outcome_without_the_columns_a_step_recorded(
    storage: LocalStorage, world: World
) -> None:
    from engine.doctor.visit import ConverseResult

    def port(case: Case, message: str, files: tuple[LoadedFile, ...]) -> ConverseResult:
        return ConverseResult(reply="I built a table.", items=(_query_item(world),), model_calls=1)

    case = visit.resume(CLIENT, open_case(storage, world).case_id, deps_for(storage, world), actor=ANALYST)
    case = visit.chat(
        CLIENT, case.case_id, "Build me a training table.", deps_for(storage, world, converse=port),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    case = visit.set_goal(
        CLIENT, case.case_id, GOAL, deps_for(storage, world), expected_version=case.version, actor=ANALYST
    )
    q = question(case, "OUTCOME_COLUMN")
    assert [o.label for o in q.options] == ["ordered_again_90d", "None of these"]
    assert not [r for r in case.questions if r.code.startswith("DATA_REQ")]
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "ordered_again_90d"), deps_for(storage, world),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    (outcome,) = (i for i in case.items if i.kind == "outcome_column")
    assert outcome.payload == {"upload_id": FIRST, "column": "ordered_again_90d"}
    assert not any(d.doubt_id.startswith("d.outcome_gone") for d in case.doubts)


def test_a_detector_on_the_current_table_does_not_find_again_what_an_accepted_fix_fixed(
    storage: LocalStorage,
) -> None:
    config = load_use_case(vs.GOAL_A)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(300)],
            "spend": [f"₹{1200 + 3 * i:,}" for i in range(300)],
            "age": np.arange(300) % 60 + 18,
        }
    )
    world = World(
        files={FIRST: vs.loaded(FIRST, "spend.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = CaseStore(storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=[vs.case_file(FIRST, world)]
    )
    files = tuple(world.files.values())
    found = cleaning_detector(world.offers)(case, files, None)
    fix = next(
        i for i in found.items if i.payload.get("step", {}).get("column") == "spend" and i.payload.get("step")
    )
    accepted = fix.model_copy(update={"state": ProposalState.ACCEPTED})
    current = current_tables(case, [accepted], files, config=config)
    assert current.files[0] is not files[0] and current.raw == files
    (table,) = current.files
    assert pd.api.types.is_numeric_dtype(table.frame["spend"]), "the money is read"
    assert files[0].frame["spend"].dtype == object, "the upload is untouched"
    case = case.model_copy(update={"items": (accepted,)})
    again = cleaning_detector(world.offers)(case, current.files, None)
    assert not [i for i in again.items if i.payload.get("step", {}).get("column") == "spend"]
    assert active_section(case)[0] == (accepted,)


def test_the_ai_sees_a_built_table_next_to_the_files_looks_at_it_and_cannot_change_it(
    storage: LocalStorage, world: World
) -> None:
    from tests.unit.doctor.test_agent_review import Script, _guard, _review

    client = Script(
        [
            {"action": "get_profile", "file": "T1", "args": {}},
            {"action": "describe_outcome", "file": "T1", "args": {"column": "ordered_again_90d"}},
            {
                "action": "propose_step",
                "file": "T1",
                "args": {"kind": "drop_column", "column": "last_order", "params": {}, "reason": "x"},
            },
            {"action": "finish", "text": "I looked at the built table."},
        ]
    )
    case = open_case(storage, world)
    case = case.model_copy(update={"items": (_query_item(world, state=ProposalState.ACCEPTED),)})
    found = _review(_guard(client))(case, tuple(world.files.values()), None)
    seen = [c for c in client.calls if c.kind == "complete"]
    assert any('"file": "T1"' in c.prompt for c in seen), "the AI state lists the built table next to F1"
    tools = {r.tool: r for r in found.evidence}
    assert tools["describe_outcome"].output["rows"] == 150, "a look works on the built column"
    assert tools["describe_outcome"].input["upload_id"] == BUILT_PREFIX + "customer_training"
    assert not found.items, "a change goes to a file, never to a table a query built"
    assert any("AGENT_TABLE_BUILT" in c.prompt for c in seen[-1:])
