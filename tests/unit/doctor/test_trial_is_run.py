"""Trial = run (root-cause plan, Step 1): the plan is materialised by ONE function, so what a query trial, the plan
check and the run (full file) make agree on schema and on error class, and a trial's receipt is the run's on the
same sample.

The cases are the ones the separate implementations used to drift on: a date parsed and then read by a SQL step that
uses `$as_of`; a SQL step that replaces an earlier pending one; a join of two files; and a step that cannot run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.doctor.case import CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, Prepared, Sample, materialize_plan
from engine.doctor.registry import DataToolContext, DataToolError, data_registry

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ROWS = 400
SAMPLE = 120
PRINCIPAL = Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user")


def orders() -> pd.DataFrame:
    days = pd.date_range("2025-01-01", periods=ROWS, freq="D")
    return pd.DataFrame(
        {
            "Customer": [f"C{n % 25}" for n in range(ROWS)],
            "InvoiceDate": [d.strftime("%m/%d/%Y") for d in days],
            "Amount": [float(n % 17) for n in range(ROWS)],
        }
    )


def customers() -> pd.DataFrame:
    return pd.DataFrame(
        {"Customer": [f"C{n}" for n in range(25)], "Region": [f"R{n % 3}" for n in range(25)]}
    )


def loaded(upload_id: str, name: str, frame: pd.DataFrame) -> LoadedFile:
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=cast(Any, None))


def item(item_id: str, step: RecipeStep, state: ProposalState, upload_id: str | None) -> CaseItem:
    return CaseItem.model_validate(
        {
            "item_id": item_id,
            "kind": "recipe_step",
            "source": "rules",
            "confidence": "sure",
            "state": state.value,
            "upload_id": upload_id,
            "payload": {"step": step.model_dump(mode="json")},
            "payload_digest": item_id,
            "proposed_at": NOW,
        }
    )


def parse_date(state: ProposalState = ProposalState.ACCEPTED) -> CaseItem:
    step = RecipeStep(
        order=1, kind=RecipeStepKind.PARSE_DATE, column="InvoiceDate", params={"dayfirst": False}, reason="d"
    )
    return item("parse", step, state, "up-1")


def propose(
    files: tuple[LoadedFile, ...],
    items: tuple[CaseItem, ...],
    sql: str,
    upload_ids: list[str],
    *,
    replacing: tuple[str, ...] = (),
    **more: Any,
) -> dict[str, Any]:
    ctx = DataToolContext(
        files=files, caller="ai", principal=PRINCIPAL, case_items=items, replacing=replacing
    )
    given = {"description": "A query.", "sql": sql, "output_name": "made", "upload_ids": upload_ids, **more}
    return dict(data_registry().call("propose_sql_step", given, ctx).output)


def query_item(
    proposal: dict[str, Any], item_id: str = "query", state: ProposalState = ProposalState.ACCEPTED
) -> CaseItem:
    return item(item_id, RecipeStep.model_validate(proposal["step"]), state, "up-1")


def schema(prepared: Prepared) -> list[tuple[str, str]]:
    frame = prepared.tables[prepared.by_upload["up-1"]]
    return [(str(c), str(t)) for c, t in zip(frame.columns, frame.dtypes, strict=True)]


def codes(prepared: Prepared) -> list[str]:
    return sorted(f.code for f in prepared.findings if f.level == "stop")


def agree(items: list[CaseItem], files: tuple[LoadedFile, ...], proposal: dict[str, Any]) -> None:
    """Sample and Full give the same schema and error class, and the trial's receipt is the sample run's."""
    sample = materialize_plan((), items, files, Sample(SAMPLE))
    full = materialize_plan((), items, files, Full())
    assert schema(sample) == schema(full)
    assert codes(sample) == codes(full)
    assert not sample.failed and not full.failed
    ran = next(r for item_id, r in sample.receipts if item_id == "query")
    tried = proposal["receipt"]
    for field in ("rows_in", "rows_out", "keys_in", "keys_out", "keys_lost", "keys_repeated"):
        assert tried[field] == getattr(ran, field), field
    assert proposal["rows_out"] == ran.rows_out
    assert proposal["columns_out"] == len(schema(sample))


@pytest.fixture(autouse=True)
def _trial_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("engine.doctor.tools_sql.DRY_RUN_ROWS", SAMPLE)


def test_a_parsed_date_read_by_a_query_with_an_as_of_agrees() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    sql = (
        "SELECT Customer, count(*) AS n, max(InvoiceDate) AS last_seen FROM t_orders "
        "WHERE InvoiceDate <= $as_of GROUP BY ALL"
    )
    waiting = parse_date(ProposalState.PENDING)  # not yet ticked: the trial counts it as accepted
    proposal = propose(files, (waiting,), sql, ["up-1"], as_of="2025-12-31")
    agree([parse_date(), query_item(proposal)], files, proposal)


def test_a_query_that_replaces_an_earlier_pending_query_agrees() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    first = propose(files, (), "SELECT Customer, count(*) AS n FROM t_orders GROUP BY ALL", ["up-1"])
    earlier = query_item(first, "earlier", ProposalState.PENDING)
    sql = "SELECT Customer, sum(Amount) AS spent FROM t_orders GROUP BY ALL"
    proposal = propose(files, (earlier,), sql, ["up-1"], replacing=("earlier",))
    assert list(proposal["columns"]) == ["Customer", "spent"]
    agree([query_item(proposal)], files, proposal)


def test_a_join_of_two_files_agrees() -> None:
    files = (loaded("up-1", "orders.csv", orders()), loaded("up-2", "customers.csv", customers()))
    sql = (
        "SELECT o.Customer, c.Region, sum(o.Amount) AS spent FROM t_orders o "
        "JOIN t_customers c ON c.Customer = o.Customer GROUP BY ALL"
    )
    proposal = propose(files, (), sql, ["up-1", "up-2"])
    assert proposal["inputs"][1]["file_name"] == "customers.csv"
    agree([query_item(proposal)], files, proposal)


def test_a_step_that_cannot_run_is_the_same_error_class_on_a_sample_and_on_the_file() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    proposal = propose(files, (), "SELECT Customer, count(*) AS n FROM t_orders GROUP BY ALL", ["up-1"])
    # Tried on text dates, the query now runs after a parse that makes them times, so it cannot read them as text.
    params = {
        **proposal["step"]["params"],
        "sql": "SELECT Customer, strptime(InvoiceDate, '%m/%d/%Y') AS d FROM t_orders",
    }
    broken = RecipeStep.model_validate(proposal["step"]).model_copy(update={"params": params})
    items = [parse_date(), item("query", broken, ProposalState.ACCEPTED, "up-1")]
    sample = materialize_plan((), items, files, Sample(SAMPLE))
    full = materialize_plan((), items, files, Full())
    assert codes(sample) == codes(full) == ["PLAN_STEP_CANNOT_RUN"]
    assert sample.failed == full.failed == ("up-1",)
    assert [f.code for f in sample.failures] == [f.code for f in full.failures]
    assert all(f.hard for f in (*sample.findings, *full.findings) if f.code == "PLAN_STEP_CANNOT_RUN")


def test_the_trial_refuses_a_query_the_run_could_not_run() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    with pytest.raises(DataToolError) as caught:
        propose(
            files, (parse_date(),), "SELECT strptime(InvoiceDate, '%m/%d/%Y') AS d FROM t_orders", ["up-1"]
        )
    assert caught.value.status == 422
    assert not materialize_plan((), [parse_date()], files, Sample(SAMPLE)).failed  # the plan alone runs


def test_a_query_that_makes_a_new_table_is_tried_without_the_files_old_id_and_held_to_it_after_the_yes() -> (
    None
):
    files = (loaded("up-1", "orders.csv", orders()),)
    key = CaseItem.model_validate(
        {
            "item_id": "key",
            "kind": "key",
            "source": "rules",
            "confidence": "sure",
            "state": "accepted",
            "upload_id": "up-1",
            "payload": {"column": "Customer"},
            "payload_digest": "key",
            "proposed_at": NOW,
        }
    )
    proposal = propose(files, (key,), "SELECT count(*) AS n FROM t_orders", ["up-1"])
    assert proposal["rows_out"] == 1  # the trial shows the table; the old ID is not what it is held to yet
    # Said yes to without a new ID, the plan keeps the file's ID, which the table no longer has: a step that cannot run.
    after = materialize_plan((), [key, query_item(proposal)], files, Sample(SAMPLE))
    assert codes(after) == ["PLAN_STEP_CANNOT_RUN"]
