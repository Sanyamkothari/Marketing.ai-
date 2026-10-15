"""The shared guards: receipts, join cardinality, change measure, the plan ledger, expectations, run twice."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.doctor import guards, policy
from engine.doctor.change_share import NotMeasurableError, measure_step, unmeasured_verdict
from engine.doctor.diagnose import LoadedFile
from engine.doctor.guards import (
    ExpectationBook,
    Expectations,
    Finding,
    acknowledge,
    cell_change,
    make_receipt,
    run_guarded,
    triage,
)
from engine.doctor.registry import DataToolContext, data_registry

LEVELS = tuple(AgentLevel)
RUN: dict[str, Any] = {
    "upload_id": "t",
    "levels": LEVELS,
    "max_failure_pct": 100.0,
}


def sql(text: str, *, inputs: tuple[str, ...] = ("this",), order: int = 1) -> RecipeStep:
    return RecipeStep(
        order=order,
        kind=RecipeStepKind.SQL_QUERY,
        column="out",
        params={"sql": text, "inputs": list(inputs), "output_name": "out", "description": "Test query."},
    )


def customers(n: int = 20_000) -> pd.DataFrame:
    return pd.DataFrame({"id": range(n), "amount": [10.0] * n, "y": [i % 2 for i in range(n)]})


def codes(found: Any, level: str | None = None) -> list[str]:
    return [f.code for f in found.findings if level is None or f.level == level]


# ---------------------------------------------------------------------------
# Receipts: lost and repeated customers are never netted
# ---------------------------------------------------------------------------
def test_a_join_that_repeats_4000_customers_and_a_filter_that_drops_4000_report_both_and_stop() -> None:
    orders = pd.DataFrame({"id": [*range(4000, 8000), *range(4000, 8000)], "paid": 1.0})
    query = (
        "SELECT c.id, c.amount, c.y FROM t_this c LEFT JOIN t_orders o ON o.id = c.id "
        "WHERE c.id >= 4000 ORDER BY c.id"
    )
    result = run_guarded(
        customers(),
        [sql(query, inputs=("this", "orders"))],
        primary_key="id",
        target="y",
        tables={"orders": orders},
        **RUN,
    )
    receipt = result.receipts[0]
    assert (
        receipt.rows_in == {"this": 20_000, "orders": 8_000} and receipt.rows_out == 20_000
    )  # rows net to 0
    assert (receipt.keys_in, receipt.keys_out) == (20_000, 16_000)
    assert (receipt.keys_lost, receipt.keys_repeated, receipt.extra_rows) == (4_000, 4_000, 4_000)
    assert receipt.lost_by_outcome == {"0": 2_000, "1": 2_000}
    assert result.stopped
    assert "JOIN_REPEATS_CUSTOMERS" in codes(result, "stop") and "STEP_LOSES_CUSTOMERS" in codes(
        result, "flag"
    )
    stop = next(f for f in result.findings if f.code == "JOIN_REPEATS_CUSTOMERS")
    assert stop.count == 4_000 and "4,000" in stop.message


def test_totals_of_number_columns_are_kept_before_and_after() -> None:
    before = pd.DataFrame({"id": [1, 2, 3], "spend": [10.0, 20.0, 30.0]})
    after = pd.DataFrame({"id": [1, 2, 2, 3], "spend": [10.0, 20.0, 20.0, 30.0]})
    receipt = make_receipt(1, "sql_query", {"a": before}, after, key="id")
    (total,) = receipt.totals
    assert (total.before, total.after, total.kept) == (60.0, 80.0, False)
    found = guards.receipt_findings(receipt)
    assert {f.code for f in found} == {"JOIN_REPEATS_CUSTOMERS", "STEP_TOTALS_CHANGED"}


def test_a_stack_may_repeat_a_customer_and_its_totals_are_of_all_files() -> None:
    jan = pd.DataFrame({"id": [1, 2], "spend": [1.0, 2.0]})
    feb = pd.DataFrame({"id": [1, 2], "spend": [3.0, 4.0]})
    receipt = make_receipt(
        1, "stack_files", {"jan": jan, "feb": feb}, pd.concat([jan, feb]), key="id", stacking=True
    )
    assert receipt.keys_repeated == 2 and receipt.totals[0].kept
    assert guards.receipt_findings(receipt, one_row_per_key=False) == []


def test_a_step_with_no_id_to_count_by_needs_a_yes() -> None:
    receipt = make_receipt(1, "sql_query", {"a": pd.DataFrame({"x": [1, 2]})}, pd.DataFrame({"z": [1]}))
    assert receipt.keys_in is None and receipt.lost == 1
    assert [f.code for f in guards.receipt_findings(receipt)] == ["STEP_NOT_COUNTED"]


# ---------------------------------------------------------------------------
# An empty result stops
# ---------------------------------------------------------------------------
def test_a_step_that_leaves_no_rows_stops_on_its_own() -> None:
    result = run_guarded(
        customers(100), [sql("SELECT * FROM t_this WHERE 1 = 0")], primary_key="id", target="y", **RUN
    )
    assert codes(result, "stop")[0] == "STEP_ZERO_ROWS"


def test_a_column_that_comes_out_empty_stops() -> None:
    result = run_guarded(
        customers(100),
        [sql("SELECT id, y, CAST(NULL AS DOUBLE) AS score FROM t_this ORDER BY id")],
        primary_key="id",
        target="y",
        **RUN,
    )
    assert "STEP_EMPTY_COLUMN" in codes(result, "stop")


# ---------------------------------------------------------------------------
# The running ledger
# ---------------------------------------------------------------------------
def drop_batch(k: int) -> RecipeStep:
    return RecipeStep(
        order=k + 1,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="batch",
        params={"operator": "equals", "values": [k]},
    )


def test_25_small_drops_add_up_and_trip_the_plan_gate_naming_the_steps() -> None:
    n = 5_000  # 25 batches of 200 customers = 4% each of nothing, 1% of the original... here 4%/step is too big
    frame = pd.DataFrame(
        {
            "id": range(n),
            "batch": [i // 200 if i < 5_000 else 99 for i in range(n)],
            "y": [1 if (i // 200) % 5 == 0 else 0 for i in range(n)],
        }
    )
    frame = frame.assign(batch=[b if b < 25 else 99 for b in frame["batch"]])
    # each step drops 200 of 5,000 (4%), none alone is over 5%; together they lose 100% of what they touch
    steps = [drop_batch(k) for k in range(5)]  # 5 steps x 4% = 20%
    result = run_guarded(frame, steps, primary_key="id", target="y", **RUN)
    assert all(r.keys_lost == 200 for r in result.receipts)
    assert all(f.level != "stop" for f in result.findings if f.order is not None)
    ledger = result.ledger
    assert ledger is not None and (ledger.lost_total, ledger.start, ledger.end) == (1_000, 5_000, 4_000)
    assert ledger.lost_pct == pytest.approx(20.0) and ledger.unit == "customers"
    gate = next(f for f in result.findings if f.code == "PLAN_LOSES_TOO_MUCH")
    assert gate.level == "stop" and "step 1" in gate.message and "1,000" in gate.message
    assert ledger.lost_by_outcome == {"1": 200 * 5 - 0} or ledger.lost_by_outcome["1"] >= 200


def test_the_outcome_rate_moving_more_than_a_point_trips_the_gate_even_with_small_loss() -> None:
    # 1,000 customers, 100 positive; a step drops 40 of the positives (4% of customers): rate 10% -> 6.25%
    frame = pd.DataFrame({"id": range(1_000), "y": [1] * 100 + [0] * 900, "drop": [1] * 40 + [0] * 960})
    step = RecipeStep(
        order=1, kind=RecipeStepKind.REMOVE_ROWS, column="drop", params={"operator": "equals", "values": [1]}
    )
    result = run_guarded(frame, [step], primary_key="id", target="y", **RUN)
    assert result.ledger is not None and result.ledger.lost_pct == pytest.approx(4.0)
    assert codes(result, "stop") == ["PLAN_MOVES_OUTCOME_RATE"]
    assert result.ledger.rate_move == pytest.approx(-3.75, abs=0.01)


def test_a_person_can_say_yes_to_a_stop_by_its_key() -> None:
    stop = Finding(level="stop", code="PLAN_LOSES_TOO_MUCH", count=1000, message="Lost 20%.")
    other = Finding(level="stop", code="PLAN_LOSES_TOO_MUCH", count=1001, message="Lost 20%.")
    kept = acknowledge([stop, other], {stop.key})
    assert [f.level for f in kept] == ["flag", "stop"]


def test_the_interruptions_are_kept_to_a_budget() -> None:
    many = [Finding(level="stop", code=f"S{i}", message="x") for i in range(8)]
    result = triage(
        [*many, Finding(level="flag", code="F", message="x"), Finding(level="note", code="N", message="x")]
    )
    assert (
        len(result.stops) == 5 and result.overflow == 3 and len(result.flags) == 1 and len(result.notes) == 1
    )


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------
def test_failed_expectations_come_back_together_with_the_retry_count_and_then_ask() -> None:
    frame = customers(100)
    step = sql("SELECT id, amount, y FROM t_this WHERE id < 90 ORDER BY id")
    expect = Expectations(
        rows_out_equals=100, keys_unique=True, max_keys_lost_pct=1.0, totals_kept=("amount",)
    )
    book = ExpectationBook()
    seen = []
    for _ in range(3):
        result = run_guarded(
            frame, [step], primary_key="id", target="y", expectations={1: expect}, book=book, **RUN
        )
        seen.append(result.outcomes[0])
    first = seen[0]
    assert (
        len(first.failures) == 3
    )  # rows, customers lost, total of amount: all at once (customers are unique)
    assert first.retries_left == 2 and not first.must_ask
    assert "try 1" in first.findings[0].message and "2 retries left" in first.findings[0].message
    assert seen[1].retries_left == 1 and not seen[1].must_ask
    assert seen[2].must_ask and "a person must say what to do" in seen[2].findings[0].message


def test_an_expectation_changed_after_a_failure_is_flagged_for_the_person() -> None:
    receipt = make_receipt(1, "sql_query", {"a": customers(10)}, customers(10).head(5), key="id")
    book = ExpectationBook()
    assert book.declare(1, Expectations(rows_out_equals=10)) == []
    assert book.declare(1, Expectations(rows_out_equals=10)) == []  # the same, locked
    assert book.declare(1, Expectations(rows_out_equals=9)) == []  # changed before it ever ran: no problem
    assert book.check(1, receipt).failures
    (flag,) = book.declare(1, Expectations(rows_out_equals=5))
    assert flag.level == "flag" and flag.code == "EXPECTATION_CHANGED_AFTER_FAILURE"
    assert not book.check(1, receipt).failures  # the new one passes, and the person has been told


# ---------------------------------------------------------------------------
# Run twice
# ---------------------------------------------------------------------------
def test_a_plan_that_gives_different_tables_each_time_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake(frame: pd.DataFrame, steps: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        out = frame.assign(stamp=calls["n"])
        kwargs["observe"](steps[0], frame, out)
        return SimpleNamespace(frame=out, receipt=None)

    monkeypatch.setattr(guards, "run_recipe", fake)
    result = run_guarded(customers(5), [sql("SELECT 1")], primary_key="id", target="y", **RUN)
    assert calls["n"] == 2
    assert "RUN_NOT_REPEATABLE" in codes(result, "stop")
    steady = run_guarded(customers(5), [sql("SELECT 1")], primary_key="id", target="y", twice=False, **RUN)
    assert "RUN_NOT_REPEATABLE" not in codes(steady) and calls["n"] == 3


def test_the_same_plan_twice_gives_the_same_hash() -> None:
    step = sql("SELECT id, amount, y FROM t_this ORDER BY id")
    result = run_guarded(customers(50), [step], primary_key="id", target="y", **RUN)
    assert not result.stopped and guards.frame_hash(result.run.frame) == guards.frame_hash(customers(50))


# ---------------------------------------------------------------------------
# The change measure
# ---------------------------------------------------------------------------
def test_a_query_that_rewrites_a_column_is_not_measured_as_0_percent() -> None:
    frame = customers(10)
    rewrite = sql("SELECT id, amount * 2 AS amount, y FROM t_this ORDER BY id")
    assert measure_step(frame, rewrite, primary_key="id", target="y") == (10, 20)
    untouched = sql("SELECT id, amount, y FROM t_this ORDER BY id")
    assert measure_step(frame, untouched, primary_key="id", target="y") == (0, 20)


def test_a_query_with_no_id_cannot_be_measured_and_needs_a_yes() -> None:
    frame = pd.DataFrame({"a": [1, 2, 3]})
    total = sql("SELECT sum(a) AS a FROM t_this")
    with pytest.raises(NotMeasurableError):
        measure_step(frame, total, primary_key=None, target=None)
    with pytest.raises(NotMeasurableError):
        cell_change(frame, pd.DataFrame({"b": [1]}), key=None)
    verdict = unmeasured_verdict("Not measured.")
    assert (verdict.level, verdict.code) == ("warn", "STEP_NOT_MEASURED")


def test_the_policy_never_reads_a_query_as_0_percent() -> None:
    effect = policy.effect_of(sql("SELECT 1"), customers(5), changed=0, failed=0)
    assert effect.measured is False


def test_a_reshape_step_is_measured_by_the_customers_it_loses() -> None:
    frame = pd.DataFrame(
        {"id": [1, 1, 2, 2, 3], "t": ["2026-01-01", "2026-01-02"] * 2 + ["2026-01-01"], "v": [1, 2, 3, 4, 5]}
    )
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.KEEP_LATEST_PER_ID,
        column="id",
        params={"time_column": "t"},
    )
    changed, out_of = measure_step(frame, step, primary_key="id", target=None)
    assert (changed, out_of) == (0, 3)  # 2 rows left out, but every customer is kept


# ---------------------------------------------------------------------------
# The proposal carries the numbers
# ---------------------------------------------------------------------------
def test_propose_sql_step_carries_the_receipt_and_the_findings() -> None:
    cust = pd.DataFrame({"customer_id": ["C1", "C2", "C3"], "plan": ["a", "b", "a"]})
    bills = pd.DataFrame({"customer_id": ["C1", "C1", "C2"], "amount": [1.0, 2.0, 3.0]})
    files = tuple(
        LoadedFile(upload_id=uid, file_name=name, frame=frame, profile=cast(Any, None))
        for uid, name, frame in (("u1", "customers.csv", cust), ("u2", "bills.csv", bills))
    )
    principal = Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user")
    ctx = DataToolContext(files=files, caller="rules", principal=principal)
    given = {
        "description": "Join the bills to each customer.",
        "sql": "SELECT c.customer_id, c.plan, b.amount FROM t_customers c LEFT JOIN t_bills b "
        "ON b.customer_id = c.customer_id ORDER BY c.customer_id",
        "output_name": "joined",
        "upload_ids": ["u1", "u2"],
    }
    out = data_registry().call("propose_sql_step", given, ctx).output
    assert (out["receipt"]["keys_in"], out["receipt"]["keys_repeated"]) == (3, 1)
    assert "JOIN_REPEATS_CUSTOMERS" in [f["code"] for f in out["findings"]]
    assert out["needs_yes"] is True and "1 repeated" in out["sentence"]


def test_a_prescription_learns_under_the_guards_and_hands_back_the_receipts() -> None:
    from datetime import UTC, datetime

    from engine.agent.advisor import ordered_steps
    from engine.agent.contracts import DataRecipe, recipe_hash
    from engine.doctor.prescription import _learn

    steps = ordered_steps([sql("SELECT id, amount, y FROM t_this WHERE id < 90 ORDER BY id")])
    recipe = DataRecipe(
        recipe_id="rc-guard",
        use_case_id="data-doctor",
        source_fingerprint="sha256:f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key="id",
        target="y",
        levels=LEVELS,
        max_failure_pct=5.0,
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    sink: list[guards.GuardedRun] = []
    frame, contract = _learn(customers(100), recipe, primary_key="id", target="y", guards=sink)
    assert frame is not None and contract is not None and len(frame) == 90
    (run,) = sink
    assert run.receipts[0].keys_lost == 10 and run.ledger is not None and run.ledger.lost_pct == 10.0
    assert "PLAN_LOSES_TOO_MUCH" in codes(run, "stop")
