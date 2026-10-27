"""Trial = run (root-cause plan, Step 1): the plan is materialised by ONE function, so what a query trial, the plan
check and the run (full file) make agree on schema and on error class, and a trial's receipt is the run's on the
same sample.

The cases are the ones the separate implementations used to drift on: a date parsed and then read by a SQL step that
uses `$as_of`; a SQL step that replaces an earlier pending one; a join of two files; and a step that cannot run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, Prepared, Sample, materialize_plan
from engine.doctor.plan_check import check_plan
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
    as_of_of_case: date | None = None,
    primary_key: str | None = None,
    **more: Any,
) -> dict[str, Any]:
    ctx = DataToolContext(
        files=files,
        caller="ai",
        principal=PRINCIPAL,
        case_items=items,
        replacing=replacing,
        as_of=as_of_of_case,
        primary_key=primary_key,
    )
    given = {"description": "A query.", "sql": sql, "output_name": "made", "upload_ids": upload_ids, **more}
    return dict(data_registry().call("propose_sql_step", given, ctx).output)


def query_item(
    proposal: dict[str, Any], item_id: str = "query", state: ProposalState = ProposalState.ACCEPTED
) -> CaseItem:
    made = item(item_id, RecipeStep.model_validate(proposal["step"]), state, "up-1")
    made.payload["columns"] = list(proposal["columns"])  # what a yes records of the table
    return made


def schema(prepared: Prepared) -> list[tuple[str, str]]:
    frame = prepared.tables[prepared.by_upload["up-1"]]
    return [(str(c), str(t)) for c, t in zip(frame.columns, frame.dtypes, strict=True)]


def codes(prepared: Prepared) -> list[str]:
    return sorted(f.code for f in prepared.findings if f.level == "stop")


def agree(
    items: list[CaseItem],
    files: tuple[LoadedFile, ...],
    proposal: dict[str, Any],
    *,
    n: int = SAMPLE,
    as_of: date | None = None,
) -> None:
    """Sample and Full give the same schema and error class, and the trial's receipt is the sample run's."""
    sample = materialize_plan((), items, files, Sample(n), as_of=as_of)
    full = materialize_plan((), items, files, Full(), as_of=as_of)
    assert schema(sample) == schema(full)
    assert codes(sample) == codes(full)
    assert not sample.failed and not full.failed
    ran = next(r for item_id, r in sample.receipts if item_id == "query")
    tried = proposal["receipt"]
    for field in ("rows_in", "rows_out", "keys_in", "keys_out", "keys_lost", "keys_repeated"):
        assert tried[field] == getattr(ran, field), field
    assert proposal["rows_out"] == ran.rows_out
    assert proposal["columns_out"] == len(schema(sample))


def _dry_run_rows(monkeypatch: pytest.MonkeyPatch, rows: int) -> None:
    from engine.doctor.config import DataDoctorLimits, TrialLimits

    monkeypatch.setattr(
        "engine.doctor.limits._ACTIVE", [DataDoctorLimits(trial=TrialLimits(dry_run_rows=rows))]
    )


@pytest.fixture(autouse=True)
def _trial_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _dry_run_rows(monkeypatch, SAMPLE)


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


def accepted(item_id: str, kind: str, column: str, upload_id: str | None, **more: Any) -> CaseItem:
    return CaseItem.model_validate(
        {
            "item_id": item_id,
            "kind": kind,
            "source": "rules",
            "confidence": "sure",
            "state": "accepted",
            "upload_id": upload_id,
            "payload": {"column": column, **more},
            "payload_digest": item_id,
            "proposed_at": NOW,
        }
    )


def test_a_query_that_drops_the_files_id_is_the_stop_the_run_gives() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    key = accepted("key", "key", "Customer", "up-1")
    with pytest.raises(DataToolError) as caught:
        propose(files, (key,), "SELECT count(*) AS n FROM t_orders", ["up-1"])
    assert caught.value.status == 422
    # the same step, said yes to, is the same hard stop (no second world where it passes)
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="made",
        params={
            "sql": "SELECT count(*) AS n FROM t_orders",
            "inputs": ["orders"],
            "output_name": "made",
            "description": "d",
        },
        reason="r",
    )
    after = materialize_plan(
        (), [key, item("query", step, ProposalState.ACCEPTED, "up-1")], files, Sample(SAMPLE)
    )
    assert codes(after) == ["PLAN_STEP_CANNOT_RUN"]
    assert all(f.hard for f in after.findings if f.code == "PLAN_STEP_CANNOT_RUN")
    assert [f.code for f in after.failures] == [caught.value.code], "the trial gave the run's own error"


def test_an_id_that_follows_the_outcome_to_the_queries_table_is_the_same_in_the_trial_and_the_run() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    key = accepted("key", "key", "Customer", "up-1")
    outcome = accepted("outcome", "outcome_column", "repeat", None)
    sql = "SELECT Customer AS customer_id, count(*) > 10 AS repeat FROM t_orders GROUP BY ALL"
    proposal = propose(files, (key, outcome), sql, ["up-1"])
    after = materialize_plan((), [key, outcome, query_item(proposal)], files, Sample(SAMPLE))
    assert not after.failed and "PLAN_STEP_CANNOT_RUN" not in codes(after)
    (done,) = after.files
    assert (done.key, done.target) == ("customer_id", "repeat")
    ran = next(r for item_id, r in done.receipts if item_id == "query")
    assert proposal["receipt"]["keys_in"] == ran.keys_in and proposal["receipt"]["keys_out"] == ran.keys_out


def test_an_id_follows_a_queries_table_before_the_outcome_is_decided_in_the_trial_and_the_run() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    key = accepted("key", "key", "Customer", "up-1")
    sql = "SELECT Customer AS customer_id, sum(Amount) AS spent FROM t_orders GROUP BY ALL"
    proposal = propose(
        files, (key,), sql, ["up-1"]
    )  # the person has said yes to the ID, not yet to an outcome
    after = materialize_plan((), [key, query_item(proposal)], files, Sample(SAMPLE))
    assert not after.failed and "PLAN_STEP_CANNOT_RUN" not in codes(after)
    (done,) = after.files
    assert done.key == "customer_id"
    ran = next(r for item_id, r in done.receipts if item_id == "query")
    assert proposal["receipt"]["keys_in"] == ran.keys_in and proposal["receipt"]["keys_out"] == ran.keys_out


def test_the_trial_runs_with_the_cases_as_of_date() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    sql = "SELECT Customer, InvoiceDate FROM t_orders WHERE InvoiceDate <= $as_of"
    given = date(2025, 3, 31)
    waiting = parse_date(ProposalState.PENDING)
    proposal = propose(files, (waiting,), sql, ["up-1"], as_of="2025-12-31", as_of_of_case=given)
    own = propose(files, (waiting,), sql, ["up-1"], as_of="2025-12-31")
    assert proposal["rows_out"] == own["rows_out"], "the query's own date is never overwritten by the case's"
    agree([parse_date(), query_item(proposal)], files, proposal, as_of=given)
    bare = propose(files, (waiting,), sql, ["up-1"], as_of_of_case=given)
    assert bare["rows_out"] < own["rows_out"], "a query with no date of its own reads the case's"


def test_the_trial_counts_by_the_plans_id_only_as_the_run_does() -> None:
    files = (loaded("up-1", "orders.csv", orders()),)
    sql = "SELECT Customer, sum(Amount) AS spent FROM t_orders GROUP BY ALL"
    # an ID the case has not decided is not the trial's to count by: the run has none either
    proposal = propose(files, (), sql, ["up-1"], primary_key="Amount")
    assert proposal["receipt"]["keys_in"] is None
    agree([query_item(proposal)], files, proposal)


def test_a_trial_on_a_bigger_file_reads_the_rows_a_run_samples_not_the_first_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = 3_000
    big = pd.DataFrame(
        {"Customer": [f"C{n // 100}" for n in range(rows)], "Amount": [float(n % 7) for n in range(rows)]}
    )  # the first 1,000 rows hold 10 of the 30 customers
    files = (loaded("up-1", "orders.csv", big),)
    _dry_run_rows(monkeypatch, 1_000)
    proposal = propose(files, (), "SELECT Customer, count(*) AS n FROM t_orders GROUP BY ALL", ["up-1"])
    assert proposal["rows_out"] > 10
    agree([query_item(proposal)], files, proposal, n=1_000)


def leaking_query(upload: str = "up-1") -> list[CaseItem]:
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="made",
        params={
            "sql": "SELECT Customer, sum(Amount) AS spend, count(*) AS total FROM t_orders GROUP BY ALL",
            "inputs": ["orders"],
            "output_name": "made",
            "description": "d",
            "as_of": "2025-06-30",
            "reads_future": True,
            "outcome_window_days": 30,
        },
        reason="r",
    )
    return [parse_date(), item("query", step, ProposalState.ACCEPTED, upload)]


def leaks(items: list[CaseItem]) -> list[Any]:
    files = [loaded("up-1", "orders.csv", orders())]
    case_files = [CaseFile(upload_id="up-1", file_name="orders.csv", added_at=NOW, added_by="u")]
    return [f for f in check_plan(files, case_files, items).findings if f.code == "PLAN_STEP_CANNOT_RUN"]


def test_a_query_reading_the_future_with_a_leaking_feature_is_a_hard_stop_whether_or_not_the_outcome_is_decided() -> (
    None
):
    undecided = leaks(leaking_query())
    assert undecided and all(f.hard and f.level == "stop" for f in undecided)
    assert "dated after" in undecided[0].text
    elsewhere = leaks([*leaking_query(), accepted("outcome", "outcome_column", "total", "up-2")])
    assert elsewhere and all(f.hard for f in elsewhere)
    named = leaks([*leaking_query(), accepted("outcome", "outcome_column", "total", "up-1")])
    assert named, "`spend` reads later rows and is not the outcome"


def test_a_failure_in_a_file_the_query_does_not_read_is_not_the_querys_refusal() -> None:
    files = (loaded("up-1", "orders.csv", orders()), loaded("up-2", "customers.csv", customers()))
    broken = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="bad",
        params={
            "sql": "SELECT nope FROM t_customers",
            "inputs": ["customers"],
            "output_name": "bad",
            "description": "d",
        },
        reason="r",
    )
    items = (item("bad", broken, ProposalState.ACCEPTED, "up-2"),)
    sql = "SELECT Customer, count(*) AS n FROM t_orders GROUP BY ALL"
    assert propose(files, items, sql, ["up-1"])["rows_out"] == 25  # the other file's stop is the plan's own
    with pytest.raises(DataToolError) as caught:
        propose(
            files,
            items,
            "SELECT o.Customer FROM t_orders o JOIN t_customers c ON c.Customer = o.Customer",
            ["up-1", "up-2"],
        )
    assert caught.value.status == 409 and "customers.csv" in caught.value.message
