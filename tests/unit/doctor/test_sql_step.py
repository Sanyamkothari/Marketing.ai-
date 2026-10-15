"""The sandboxed SQL step: a query over the case's tables that is run, refused, stopped and replayed by our code."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.advisor import ordered_steps
from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.recipe import RecipeError, run_recipe
from engine.agent.sql_step import SqlStepError, check_sql, run_sql, table_alias
from engine.doctor.diagnose import LoadedFile
from engine.doctor.prescription import FilePart, Provenance, make_prescription
from engine.doctor.prescription_replay import ReplayFile, replay_prescription
from engine.doctor.registry import DataToolContext, DataToolError, data_registry

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
LEVELS = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
TOTALS = (
    "SELECT c.customer_id, c.plan, coalesce(sum(b.amount), 0) AS total, count(b.customer_id) AS bills "
    "FROM t_customers c LEFT JOIN t_bills b ON b.customer_id = c.customer_id "
    "GROUP BY ALL ORDER BY c.customer_id"
)


def customers(ids: tuple[str, ...] = ("C1", "C2", "C3")) -> pd.DataFrame:
    return pd.DataFrame({"customer_id": list(ids), "plan": ["basic", "premium", "basic"][: len(ids)]})


def bills(
    rows: tuple[tuple[str, float], ...] = (("C1", 10.0), ("C1", 5.0), ("C2", 7.0), ("C9", 1.0))
) -> pd.DataFrame:
    return pd.DataFrame({"customer_id": [r[0] for r in rows], "amount": [r[1] for r in rows]})


def query(sql: str = TOTALS, inputs: tuple[str, ...] = ("customers", "bills"), **extra: Any) -> RecipeStep:
    params: dict[str, Any] = {
        "sql": sql,
        "inputs": list(inputs),
        "output_name": "customer_totals",
        "description": "Add up each customer's bills.",
        **extra,
    }
    return RecipeStep(order=1, kind=RecipeStepKind.SQL_QUERY, column="customer_totals", params=params)


def recipe_of(*steps: RecipeStep) -> DataRecipe:
    ordered = ordered_steps(steps)
    return DataRecipe(
        recipe_id="rc-sql",
        use_case_id="data-doctor",
        source_fingerprint="sha256:f",
        steps=ordered,
        recipe_hash=recipe_hash(ordered),
        primary_key=None,
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
        created_at=NOW,
    )


def prescription_of(step: RecipeStep) -> Any:
    return make_prescription(
        files=(
            FilePart(slot="customers", file_names=("customers.csv",)),
            FilePart(slot="bills", file_names=("bills.csv",)),
        ),
        post_build=recipe_of(step),
        provenance=Provenance(created_at=NOW),
    )


def test_a_join_and_total_makes_a_new_table_and_counts_every_table() -> None:
    cust, bill = customers(), bills()
    before = (cust.copy(), bill.copy())
    run = run_recipe(
        cust,
        [query(TOTALS.replace("t_customers", "t_this"), inputs=("this", "bills"))],
        upload_id="u1",
        primary_key=None,
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
        tables={"bills": bill},
    )
    assert run.frame.to_dict("list") == {
        "customer_id": ["C1", "C2", "C3"],
        "plan": ["basic", "premium", "basic"],
        "total": [15.0, 7.0, 0.0],
        "bills": [2, 1, 0],
    }
    (receipt,) = run.receipt.steps
    assert receipt.changed == 3 and receipt.rows == 3 + 4
    assert receipt.note is not None and "this (3 rows)" in receipt.note and "bills (4 rows)" in receipt.note
    assert "3 rows and 4 columns" in receipt.note
    pd.testing.assert_frame_equal(cust, before[0])
    pd.testing.assert_frame_equal(bill, before[1])


def test_the_step_is_replayed_on_next_months_files() -> None:
    saved = prescription_of(query())
    june = replay_prescription(
        saved,
        [ReplayFile("customers", customers()), ReplayFile("bills", bills())],
        mode="score",
    )
    july = replay_prescription(
        saved,
        [
            ReplayFile("customers", customers(("C7", "C8", "C9"))),
            ReplayFile("bills", bills((("C7", 2.5), ("C9", 4.0), ("C9", 1.0)))),
        ],
        mode="score",
    )
    assert june.frame is not None and july.frame is not None
    assert june.frame["total"].tolist() == [15.0, 7.0, 0.0]
    assert july.frame["customer_id"].tolist() == ["C7", "C8", "C9"]
    assert july.frame["total"].tolist() == [2.5, 0.0, 5.0]
    assert july.receipt.rows_out == 3
    (receipt,) = july.receipt.recipe_receipts[-1].steps
    assert receipt.note is not None and "bills (3 rows)" in receipt.note


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DROP TABLE t_customers", "SQL_ONLY_SELECT"),
        ("INSERT INTO t_customers VALUES ('C4', 'x')", "SQL_ONLY_SELECT"),
        ("COPY t_customers TO 'out.csv'", "SQL_ONLY_SELECT"),
        ("ATTACH 'other.db'", "SQL_ONLY_SELECT"),
        ("INSTALL httpfs", "SQL_ONLY_SELECT"),
        ("LOAD httpfs", "SQL_ONLY_SELECT"),
        ("PRAGMA table_info('t_customers')", "SQL_NOT_VALID"),
        ("SET threads = 8", "SQL_ONLY_SELECT"),
        ("EXPORT DATABASE 'dir'", "SQL_ONLY_SELECT"),
        ("WITH x AS (SELECT 1) INSERT INTO t_customers SELECT * FROM x", "SQL_ONLY_SELECT"),
        ("SELECT 1; SELECT 2", "SQL_ONE_STATEMENT"),
        ("SELECT * FROM read_csv('/etc/passwd')", "SQL_READS_OUTSIDE"),
        ("SELECT * FROM read_parquet('x.parquet')", "SQL_READS_OUTSIDE"),
        ("SELECT * FROM read_json('x.json')", "SQL_READS_OUTSIDE"),
        ("SELECT * FROM glob('*')", "SQL_READS_OUTSIDE"),
        ("SELECT getenv('HOME')", "SQL_READS_OUTSIDE"),
        ("SELECT * FROM range(1000000)", "SQL_READS_OUTSIDE"),
        ("SELECT * FROM 'data.csv'", "SQL_TABLE_NOT_ALLOWED"),
        ("SELECT * FROM t_other", "SQL_TABLE_NOT_ALLOWED"),
        ("SELECT * FROM information_schema.tables", "SQL_TABLE_NOT_ALLOWED"),
        ("SELECT random() FROM t_customers", "SQL_NOT_REPEATABLE"),
        ("SELECT now()", "SQL_NOT_REPEATABLE"),
        ("SELECT current_date", "SQL_NOT_REPEATABLE"),
        ("SELECT * FROM t_customers LIMIT 2", "SQL_NOT_REPEATABLE"),
        ("SELECT * FROM t_customers TABLESAMPLE 10%", "SQL_NOT_REPEATABLE"),
    ],
)
def test_forbidden_queries_are_refused_before_anything_runs(sql: str, code: str) -> None:
    with pytest.raises(SqlStepError) as caught:
        check_sql(sql, ["t_customers"], as_of=date(2026, 10, 1))
    assert caught.value.code == code


def test_a_forbidden_step_is_refused_by_the_recipe_check() -> None:
    with pytest.raises(RecipeError) as caught:
        run_recipe(
            customers(),
            [query("SELECT * FROM read_csv('x.csv')", inputs=("this",))],
            upload_id="u1",
            primary_key=None,
            target=None,
            levels=LEVELS,
            max_failure_pct=5.0,
        )
    assert caught.value.code == "SQL_READS_OUTSIDE"


def test_limit_with_an_order_by_is_allowed() -> None:
    check_sql("SELECT * FROM t_customers ORDER BY customer_id LIMIT 2", ["t_customers"])


def test_the_date_comes_in_as_a_parameter() -> None:
    sql = "SELECT customer_id, date_diff('day', DATE '2026-01-01', $as_of) AS days FROM t_this ORDER BY 1"
    step = query(sql, inputs=("this",))
    with pytest.raises(RecipeError) as caught:
        run_recipe(
            customers(),
            [step],
            upload_id="u",
            primary_key=None,
            target=None,
            levels=LEVELS,
            max_failure_pct=5.0,
        )
    assert caught.value.code == "SQL_NEEDS_AS_OF"
    frozen = query(sql, inputs=("this",), as_of="2026-01-11")
    run = run_recipe(
        customers(),
        [frozen],
        upload_id="u",
        primary_key=None,
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
    )
    assert run.frame["days"].tolist() == [10, 10, 10]
    later = run_recipe(
        customers(),
        [frozen],
        upload_id="u",
        primary_key=None,
        target=None,
        levels=LEVELS,
        max_failure_pct=5.0,
        as_of=date(2026, 2, 1),
    )
    assert later.frame["days"].tolist() == [31, 31, 31]


def test_a_slow_query_is_stopped() -> None:
    big = pd.DataFrame({"n": range(5000)})
    with pytest.raises(SqlStepError) as caught:
        run_sql(
            "SELECT count(*) FROM t_big a, t_big b, t_big c, t_big d",
            {"big": big},
            output_name="slow",
            timeout=0.4,
        )
    assert caught.value.code == "SQL_TIMEOUT"


def test_the_input_tables_are_never_changed_and_the_query_cannot_write_to_them() -> None:
    cust = customers()
    before = cust.copy()
    run_sql(
        "SELECT upper(plan) AS plan FROM t_customers ORDER BY 1", {"customers": cust}, output_name="plans"
    )
    pd.testing.assert_frame_equal(cust, before)
    with pytest.raises(SqlStepError):
        run_sql("UPDATE t_customers SET plan = 'x'", {"customers": cust}, output_name="plans")
    pd.testing.assert_frame_equal(cust, before)


def test_a_missing_table_stops_a_replay_with_its_own_code() -> None:
    with pytest.raises(RecipeError) as caught:
        run_recipe(
            customers(),
            [query()],
            upload_id="u",
            primary_key=None,
            target=None,
            levels=LEVELS,
            max_failure_pct=5.0,
        )
    assert caught.value.code == "SQL_INPUT_MISSING"


def test_a_failing_query_does_not_repeat_a_cell_value() -> None:
    frame = pd.DataFrame({"name": ["Asha Rao"]})
    with pytest.raises(SqlStepError) as caught:
        run_sql("SELECT CAST(name AS INTEGER) FROM t_f", {"f": frame}, output_name="x")
    assert caught.value.code == "SQL_FAILED" and "Asha" not in caught.value.message


def test_table_names_are_safe_aliases() -> None:
    assert table_alias("Customers 2026.csv") == "t_customers_2026_csv"


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------
def _ctx(caller: str = "rules") -> DataToolContext:
    files = tuple(
        LoadedFile(upload_id=uid, file_name=name, frame=frame, profile=cast(Any, None))
        for uid, name, frame in (("u1", "customers.csv", customers()), ("u2", "bills.csv", bills()))
    )
    principal = Principal(user_id="u-analyst", username="u", roles=frozenset({Role.ANALYST}), kind="user")
    return DataToolContext(files=files, caller=cast(Any, caller), principal=principal)


GIVEN: dict[str, Any] = {
    "description": "Add up each customer's bills.",
    "sql": TOTALS,
    "output_name": "customer_totals",
    "upload_ids": ["u1", "u2"],
}


@pytest.mark.parametrize("caller", ["rules", "ai"])
def test_propose_sql_step_tries_the_query_and_proposes_a_step(caller: str) -> None:
    result = data_registry().call("propose_sql_step", GIVEN, _ctx(caller))
    out = result.output
    assert out["source"] == ("ai" if caller == "ai" else "rules")
    assert out["state"] == "pending" and out["ticked"] is False and out["never_ticked"] is True
    assert [(t["name"], t["rows"]) for t in out["inputs"]] == [("customers", 3), ("bills", 4)]
    assert (out["rows_in"], out["rows_out"], out["columns_out"]) == (3, 3, 4)
    assert out["title"] == "Add up each customer's bills"
    assert out["sentence"].startswith("Add up each customer's bills.")
    assert "customers.csv (3 rows)" in out["sentence"] and "bills.csv (4 rows)" in out["sentence"]
    assert "SELECT" not in out["sentence"]
    step = out["step"]
    assert step["kind"] == "sql_query" and step["params"]["inputs"] == ["customers", "bills"]
    # the proposed step runs on the same files, as a prescription would replay it
    again = replay_prescription(
        prescription_of(RecipeStep.model_validate(step)),
        [ReplayFile("customers", customers()), ReplayFile("bills", bills())],
        mode="score",
    )
    assert again.frame is not None and again.frame["total"].tolist() == [15.0, 7.0, 0.0]


def test_propose_sql_step_refuses_a_forbidden_query_with_its_code() -> None:
    with pytest.raises(DataToolError) as caught:
        data_registry().call("propose_sql_step", {**GIVEN, "sql": "SELECT * FROM read_csv('x.csv')"}, _ctx())
    assert caught.value.code == "SQL_READS_OUTSIDE"
    with pytest.raises(DataToolError) as unknown:
        data_registry().call("propose_sql_step", {**GIVEN, "upload_ids": ["nope"]}, _ctx())
    assert unknown.value.code == "TOOL_INPUT_INVALID"
