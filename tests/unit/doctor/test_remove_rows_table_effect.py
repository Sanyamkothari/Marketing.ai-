"""Leaving rows out by a column (`remove_rows` on Country) and the table the goal's build makes from what is left.

The build reads the rows after the step, so the table it makes changes by more than the share of rows left out; the
plan line says so, with the whole file's count (a sample's table is a sample's and is left unsaid).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.config import load_use_case
from engine.doctor.buildable import buildable_outcome, compile_build
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.conversation import _counts, ledger_steps
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, materialize_plan
from engine.stages.ingest import profile_dataset
from scripts.doctor_scenario_data import generate_order_log
from tests.unit.doctor.test_sql_trial_state import item

GOAL = "retail-win-back"
CONFIG_ROOT = Path("configs")


def _built(window_days: int) -> tuple[LoadedFile, Any, Any]:
    config = load_use_case(GOAL, CONFIG_ROOT)
    frame = generate_order_log()
    profile = profile_dataset(
        frame,
        config,
        upload_id="up-1",
        file_name="o.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    loaded = LoadedFile(upload_id="up-1", file_name="o.csv", frame=frame, profile=profile)
    built = buildable_outcome([loaded], config, CONFIG_ROOT, window_days=window_days)
    assert built is not None and built.snapshot is not None
    return loaded, config, built


def _run(
    loaded: LoadedFile, config: Any, built: Any, remove: dict[str, Any] | None
) -> tuple[pd.DataFrame, Any]:
    sql, description = compile_build(built, config, "t_o", next(a for a in built.audiences if a.enough))
    steps: list[RecipeStep] = []
    if remove is not None:
        steps.append(
            RecipeStep(order=1, kind=RecipeStepKind.REMOVE_ROWS, column="Country", params=remove, reason="r")
        )
    steps.append(
        RecipeStep(
            order=len(steps) + 1,
            kind=RecipeStepKind.SQL_QUERY,
            column="training",
            params={
                "sql": sql,
                "inputs": ["o"],
                "output_name": "training",
                "description": description,
                "reads_future": True,
                "outcome_window_days": built.window_days,
                "as_of": built.snapshot.isoformat(),
            },
            reason="r",
        )
    )
    items = [
        item(f"i{n}", {"step": s.model_dump(mode="json")}, ProposalState.ACCEPTED, upload_id="up-1")
        for n, s in enumerate(steps)
    ]
    files = [
        CaseFile(upload_id="up-1", file_name="o.csv", added_at=datetime(2026, 1, 1, tzinfo=UTC), added_by="u")
    ]
    run = materialize_plan(
        files, items, [loaded], Full(), as_of=built.snapshot, training_as_of=built.snapshot
    )
    assert not run.failures
    return run.tables["training"], (items, run)


def test_leaving_the_other_country_out_changes_the_table_by_more_than_its_share_of_rows() -> None:
    """Measured on the scenario's order log (60-day window): 14.6% of the rows go, and the table made from the rest
    loses the shoppers who only ever ordered abroad and, of those who stay, the orders that made them a yes.
    """
    loaded, config, built = _built(60)
    before, _ = _run(loaded, config, built, None)
    after, (items, run) = _run(loaded, config, built, {"operator": "in", "values": ["Germany"]})
    assert (len(before), int(before["reactivated"].sum())) == (1759, 473)
    assert (len(after), int(after["reactivated"].sum())) == (1649, 387)
    removed = next(r for _, r in run.receipts if r.kind == "remove_rows")
    assert removed.rows_in == {"this": 12008} and removed.rows_out == 10237
    assert int(after["CustomerID"].nunique()) == len(after)  # still one row per shopper
    # the same step, as the plan check keeps it: the line says what the build makes from what is left
    ledger: dict[str, Any] = {
        "steps": [
            {"item_id": "i0", "rows_in": 12008, "rows_out": 10237, "customers_lost": 85},
            {"item_id": "i1", "rows_in": 10237, "rows_out": len(after), "customers_lost": 101},
        ]
    }
    steps = ledger_steps(ledger, items)
    said = _counts(items[0], steps["i0"])
    assert "10,237 kept, 1,771 left out" in said
    assert "the table the plan builds from the rows left has 1,649 rows" in said
    assert "table_after" not in steps["i1"]


def test_a_samples_table_is_not_said_as_the_table() -> None:
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="Country",
        params={"operator": "in", "values": ["Germany"]},
        reason="r",
    )
    query = RecipeStep(
        order=2,
        kind=RecipeStepKind.SQL_QUERY,
        column="t",
        params={"sql": "SELECT 1", "inputs": ["o"], "output_name": "t", "description": "d"},
        reason="r",
    )
    items: list[CaseItem] = [
        item("i0", {"step": step.model_dump(mode="json")}, ProposalState.ACCEPTED, upload_id="up-1"),
        item("i1", {"step": query.model_dump(mode="json")}, ProposalState.ACCEPTED, upload_id="up-1"),
    ]
    rows = [
        {"item_id": "i0", "rows_in": 1000, "rows_out": 854},
        {"item_id": "i1", "rows_in": 854, "rows_out": 448},
    ]
    sample = ledger_steps({"sampled_from": 12008, "steps": rows}, items)
    assert "table_after" not in sample["i0"]
    whole = ledger_steps({"steps": rows}, items)
    assert whole["i0"]["table_after"] == 448
    # a step that left nothing out has no effect to tell
    assert "the table the plan builds" not in _counts(items[0], {**whole["i0"], "rows_out": 1000})
