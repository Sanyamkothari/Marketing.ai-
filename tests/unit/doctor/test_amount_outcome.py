"""An amount as the outcome: spend in the N days after the snapshot, point in time (roadmap 2.2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.sql_step import MAX_COMPILED_SQL_CHARS, run_sql
from engine.config import load_use_case
from engine.doctor.amount_outcome import (
    ProposeAmountOutcomeArgs,
    amount_minimums,
    compile_amount_outcome,
    propose_amount_outcome,
)
from engine.doctor.diagnose import LoadedFile
from engine.doctor.registry import DataToolContext, DataToolError

AS_OF = date(2025, 6, 30)
PRINCIPAL = Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user")
ROWS = [
    ("C1", "2025-06-20", 60.0),  # before: history, not the outcome
    ("C1", "2025-07-05", 40.0),  # in the 30-day window
    ("C1", "2025-07-30", 25.0),  # in the window (last day)
    ("C1", "2025-07-31", 999.0),  # after the window
    ("C2", "2025-06-01", 20.0),  # known, spends nothing after
    ("C3", "2025-07-10", 70.0),  # first seen after the snapshot: not a customer yet
    ("C4", "2025-06-30", 10.0),  # on the snapshot day: history
    ("C4", "2025-07-02", -5.0),  # a refund is netted in
]


def orders(rows: list[tuple[Any, ...]] | None = None) -> pd.DataFrame:
    return pd.DataFrame(rows if rows is not None else ROWS, columns=["Customer", "OrderDate", "Amount"])


def args(**more: Any) -> ProposeAmountOutcomeArgs:
    return ProposeAmountOutcomeArgs.model_validate(
        {
            "upload_id": "up-1",
            "entity_key": "Customer",
            "time_column": "OrderDate",
            "amount_column": "Amount",
            "window_days": 30,
            **more,
        }
    )


def spend(frame: pd.DataFrame, **more: Any) -> pd.Series[Any]:
    sql = compile_amount_outcome(args(**more), "t_orders")
    out = run_sql(
        sql, {"orders": frame}, output_name="o", as_of=AS_OF, max_chars=MAX_COMPILED_SQL_CHARS
    ).frame
    return out.set_index("Customer").iloc[:, 0]


def test_spend_is_the_amount_in_the_window_after_the_snapshot() -> None:
    out = spend(orders())
    assert list(out.index) == ["C1", "C2", "C4"]  # C3 first appears after the snapshot
    assert out.to_dict() == {"C1": 65.0, "C2": 0.0, "C4": -5.0}


def test_rows_after_the_window_or_the_snapshot_never_move_the_history_side() -> None:
    # the customers known at the snapshot and their spend do not depend on anything after the window
    extra = orders([*ROWS, ("C1", "2026-01-01", 5000.0), ("C9", "2026-01-01", 1.0)])
    assert spend(extra).to_dict() == spend(orders()).to_dict()


def test_the_window_is_the_one_asked_for() -> None:
    assert spend(orders(), window_days=60).loc["C1"] == 1064.0  # 60 days reaches the 31 July order
    assert spend(orders(), window_days=1).loc["C1"] == 0.0


def context(frame: pd.DataFrame, **more: Any) -> DataToolContext:
    loaded = LoadedFile(upload_id="up-1", file_name="orders.csv", frame=frame, profile=cast(Any, None))
    return DataToolContext(files=(loaded,), **{"caller": "ai", "principal": PRINCIPAL, **more})


def test_the_proposal_is_a_pending_future_reading_step_with_its_window() -> None:
    proposal = propose_amount_outcome(args(as_of=AS_OF), context(orders()))
    params = proposal.step["params"]
    assert proposal.step["kind"] == "sql_query"
    assert params["reads_future"] is True and params["outcome_window_days"] == 30
    assert params["as_of"] == AS_OF.isoformat()
    assert proposal.rows_out == 3 and "spend_next_30d" in proposal.all_columns
    assert not any(f.code == "OUTCOME_WINDOW_PAST_DATA" for f in proposal.findings)


def test_a_snapshot_whose_window_runs_past_the_data_is_stopped() -> None:
    proposal = propose_amount_outcome(args(as_of=AS_OF, window_days=90), context(orders()))
    assert any(f.code == "OUTCOME_WINDOW_PAST_DATA" and f.level == "stop" for f in proposal.findings)
    assert proposal.needs_yes


def test_the_training_date_of_the_case_is_the_snapshot() -> None:
    proposal = propose_amount_outcome(args(), context(orders(), training_as_of=AS_OF))
    assert proposal.rows_out == 3


def test_no_snapshot_date_is_asked_for() -> None:
    with pytest.raises(DataToolError) as caught:
        propose_amount_outcome(args(), context(orders()))
    assert caught.value.code == "OUTCOME_NEEDS_AS_OF"


def test_a_name_that_is_not_a_column_name_is_refused() -> None:
    with pytest.raises(DataToolError):
        compile_amount_outcome(args(entity_key='Customer"; DROP'), "t_orders")


def test_the_floor_is_a_thousand_customers_and_two_hundred_who_spent(config_root: Path) -> None:
    config = load_use_case("retail-win-back", config_root)
    rng = np.random.default_rng(3)
    enough = pd.Series(np.where(rng.random(1500) < 0.3, rng.gamma(2.0, 50.0, 1500), 0.0))
    assert amount_minimums(enough, config).enough
    few_spenders = pd.Series(np.where(rng.random(1500) < 0.05, 40.0, 0.0))
    found = amount_minimums(few_spenders, config)
    assert not found.enough and found.min_non_zero == 200 and "who spent" in found.message
    few_rows = pd.Series(np.where(rng.random(600) < 0.5, 40.0, 0.0))
    assert not amount_minimums(few_rows, config).enough


def spenders(customers: int, share: float) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    spent = rng.random(customers) < share
    return pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(customers)],
            "orders_365d": rng.integers(0, 20, size=customers),
            "spend_next_90d": np.where(spent, rng.gamma(2.0, 80.0, size=customers).round(2), 0.0),
        }
    )


def test_an_amount_goal_is_offered_with_its_spenders_counted(config_root: Path) -> None:
    from api.routes.doctor import offers
    from engine.doctor.diagnose import readiness
    from tests.unit.doctor.test_diagnose import loaded as loaded_file

    def verdict(frame: pd.DataFrame) -> Any:
        found = readiness([loaded_file(frame, "spend.csv", config_root)], offers(config_root), config_root)
        return next(v for v in found if v.use_case_id == "customer-spend")

    good = verdict(spenders(1500, 0.3))
    assert good.status in {"ready", "check"} and good.outcome_column == "spend_next_90d"
    assert good.yes_count and good.yes_count > 200
    thin = verdict(spenders(1500, 0.05))
    assert thin.status == "too_small" and "who spent" in thin.summary


def _history(customers: int = 40) -> pd.DataFrame:
    rows = [
        (f"C{i:03d}", f"2025-0{1 + (i + j) % 6}-1{j}", 10.0 * (j + 1))
        for i in range(customers)
        for j in range(3)
    ]
    return pd.DataFrame(rows, columns=["customer_id", "ordered_on", "order_total"])


def test_the_registry_offers_the_tool_to_the_rules_only() -> None:
    from engine.doctor.registry import data_registry

    assert (
        data_registry().find("propose_amount_outcome", context(orders())) is None
    )  # the AI is not offered it
    tool = data_registry().find("propose_amount_outcome", context(orders(), caller="rules"))
    assert tool is not None and tool.number == 88 and tool.callers == frozenset({"rules"})
    out = data_registry().call(
        "propose_amount_outcome", args(as_of=AS_OF).model_dump(mode="json"), context(orders(), caller="rules")
    )
    assert out.output["rows_out"] == 3


def test_an_order_file_makes_a_missing_spend_goal_offer_its_outcome(config_root: Path) -> None:
    from engine.doctor.diagnose import Offer, readiness

    config = load_use_case("customer-spend", config_root)
    loaded = LoadedFile(upload_id="up-1", file_name="orders.csv", frame=_history(), profile=cast(Any, None))
    found = readiness([loaded], [Offer(config=config, stage="Growth")], config_root)
    verdict = next(v for v in found if v.use_case_id == "customer-spend")
    offer = verdict.amount_outcome
    assert verdict.status == "missing" and verdict.reason_code == "NO_OUTCOME"
    assert offer is not None and verdict.first_fix == offer.sentence
    assert (offer.entity_key, offer.time_column, offer.amount_column) == (
        "customer_id",
        "ordered_on",
        "order_total",
    )
    assert offer.outcome_name == "spend_next_90d" and offer.window_days == 90
    # the offer is exactly the tool's arguments: agreeing to it proposes the pending step
    proposal = propose_amount_outcome(
        ProposeAmountOutcomeArgs(
            upload_id=offer.upload_id,
            entity_key=offer.entity_key,
            time_column=offer.time_column,
            amount_column=offer.amount_column,
            window_days=offer.window_days,
            outcome_name=offer.outcome_name,
            as_of=date(2025, 3, 31),
        ),
        DataToolContext(files=(loaded,), caller="rules", principal=PRINCIPAL),
    )
    assert offer.outcome_name in proposal.all_columns


def test_a_file_without_orders_offers_nothing(config_root: Path) -> None:
    from engine.doctor.amount_outcome import offer_amount_outcome

    config = load_use_case("customer-spend", config_root)
    flat = pd.DataFrame({"customer_id": [f"C{i}" for i in range(50)], "age": range(50)})
    loaded = LoadedFile(upload_id="up-2", file_name="people.csv", frame=flat, profile=cast(Any, None))
    assert offer_amount_outcome([loaded], config) is None
