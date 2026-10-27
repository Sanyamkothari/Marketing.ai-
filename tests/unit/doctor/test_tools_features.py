"""`propose_features` (Step 7): behaviour features from the point-in-time library, as one proposed query step.

Point-in-time (rows after the as-of date never move a feature), the `rfm` preset against a hand-computed result, a
feature that reads the outcome is refused, the leak probe fires on a crafted future-reading query, and the trial is
the run (the same step through `materialize_plan`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import ProposalState, RecipeStep
from engine.agent.sql_step import future_rows_moved, run_sql
from engine.doctor.case import CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, Sample, materialize_plan
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from engine.doctor.tools_features import ProposeFeaturesArgs, compile_features, parse_query_output
from engine.onboarding.features import FeatureError, assert_point_in_time

AS_OF = date(2025, 6, 30)
PRINCIPAL = Principal(user_id="u", username="u", roles=frozenset({Role.ANALYST}), kind="user")
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ROWS = [
    ("C1", "A1", "2025-06-20", 60.0, "p1"),
    ("C1", "A1", "2025-06-20", 40.0, "p2"),
    ("C1", "A2", "2025-03-01", 50.0, "p1"),
    ("C1", "A3", "2024-08-01", 30.0, "p3"),
    ("C1", "A4", "2025-07-15", 999.0, "p9"),  # after the as-of date
    ("C2", "B1", "2025-06-01", 20.0, "p1"),
    ("C3", "D1", "2025-08-01", 5.0, "p1"),  # a customer who first appears after the as-of date
]


def invoices(rows: list[tuple[Any, ...]] | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        rows if rows is not None else ROWS,
        columns=["Customer", "Invoice", "InvoiceDate", "Amount", "Product"],
    )


def loaded(frame: pd.DataFrame) -> LoadedFile:
    return LoadedFile(upload_id="up-1", file_name="invoices.csv", frame=frame, profile=cast(Any, None))


def ask(frame: pd.DataFrame | None = None, **more: Any) -> dict[str, Any]:
    ctx = DataToolContext(
        files=(loaded(invoices() if frame is None else frame),), caller="ai", principal=PRINCIPAL
    )
    given = {
        "upload_id": "up-1",
        "entity_key": "Customer",
        "time_column": "InvoiceDate",
        "as_of": AS_OF.isoformat(),
        "amount_column": "Amount",
        "order_column": "Invoice",
        **more,
    }
    return dict(data_registry().call("propose_features", given, ctx).output)


def made(frame: pd.DataFrame, **more: Any) -> pd.DataFrame:
    args = ProposeFeaturesArgs.model_validate(
        {
            "upload_id": "up-1",
            "entity_key": "Customer",
            "time_column": "InvoiceDate",
            "amount_column": "Amount",
            "order_column": "Invoice",
            **more,
        }
    )
    sql = compile_features(args, "t_invoices").sql
    return run_sql(sql, {"invoices": frame}, output_name="f", as_of=AS_OF).frame.set_index("Customer")


def test_rfm_equals_the_hand_computed_result() -> None:
    out = made(invoices(), preset="rfm")
    assert list(out.index) == ["C1", "C2"]  # C3 has nothing on or before the as-of date
    c1, c2 = out.loc["C1"], out.loc["C2"]
    assert (c1.days_since_last_order, c1.orders_90d, c1.orders_365d) == (10, 1, 3)
    assert (c1.spend_90d, c1.spend_365d, c1.avg_order_value) == (100.0, 180.0, 60.0)
    assert (c2.days_since_last_order, c2.orders_90d, c2.orders_365d) == (29, 1, 1)
    assert (c2.spend_90d, c2.spend_365d, c2.avg_order_value) == (20.0, 20.0, 20.0)


def test_rfm_plus_adds_its_features_point_in_time() -> None:
    out = made(invoices(), preset="rfm_plus", product_column="Product")
    c1 = out.loc["C1"]
    assert c1.distinct_products == 3 and c1.tenure_days == (AS_OF - date(2024, 8, 1)).days
    assert c1.active_months == 3 and c1.return_rate == 0
    assert (
        c1.spend_trend == 100.0 - 50.0
    )  # last 90 days minus the 90 before (2025-03-01 is in the earlier 90)
    assert c1.days_between_orders == (date(2025, 6, 20) - date(2024, 8, 1)).days / 2


def test_adding_or_removing_rows_after_the_as_of_date_never_changes_a_feature() -> None:
    base = made(invoices(), preset="rfm_plus", product_column="Product")
    more = invoices([*ROWS, ("C1", "A5", "2025-09-09", 12345.0, "pz"), ("C2", "B2", "2025-07-01", 7.0, "pq")])
    fewer = invoices([r for r in ROWS if r[2] <= AS_OF.isoformat()])
    for other in (more, fewer):
        again = made(other, preset="rfm_plus", product_column="Product")
        pd.testing.assert_frame_equal(base, again.loc[base.index])


def test_a_feature_that_reads_the_outcome_is_refused() -> None:
    for more in (
        {"features": [{"name": "sum_amount", "agg": "sum", "column": "Amount"}], "outcome_column": "Amount"},
        {
            "features": [
                {"name": "n", "agg": "count", "where": {"column": "Amount", "op": "gt", "value": 1}}
            ],
            "outcome_column": "amount",
        },
        {"preset": "rfm", "outcome_column": "Invoice"},
    ):
        with pytest.raises(DataToolError) as caught:
            ask(**more)
        assert caught.value.code == "FEATURE_READS_OUTCOME"


def test_the_leak_probe_fires_on_a_crafted_future_reading_feature() -> None:
    frame = invoices()
    leaky = "SELECT Customer, sum(Amount) AS total_ever FROM t_invoices GROUP BY ALL"  # ignores $as_of: reads rows after it
    assert (future_rows_moved(leaky, {"invoices": frame}, output_name="f", as_of=AS_OF) or 0) > 0
    honest = compile_features(
        ProposeFeaturesArgs.model_validate(
            {
                "upload_id": "up-1",
                "entity_key": "Customer",
                "time_column": "InvoiceDate",
                "preset": "rfm",
                "amount_column": "Amount",
            }
        ),
        "t_invoices",
    ).sql
    assert future_rows_moved(honest, {"invoices": frame}, output_name="f", as_of=AS_OF) == 0
    with pytest.raises(FeatureError):  # a join that lost the guard is refused before it leaves the compiler
        assert_point_in_time(honest.replace("AND e.event_time <= s.snapshot_date", "AND TRUE"))


def test_a_constant_or_blank_feature_is_reported() -> None:
    out = ask(
        features=[
            {"name": "never", "agg": "count", "where": {"column": "Amount", "op": "gt", "value": 100000}},
            {
                "name": "ghost",
                "agg": "latest",
                "column": "Product",
                "where": {"column": "Amount", "op": "gt", "value": 100000},
            },
            {"name": "orders", "agg": "nunique", "column": "Invoice"},
        ]
    )
    state = {f["name"]: f["state"] for f in out["features"]}
    assert state == {"never": "constant", "ghost": "mostly_blank", "orders": "ok"}
    codes = {f["code"] for f in out["findings"]}
    assert {"FEATURE_CONSTANT", "FEATURE_MOSTLY_BLANK"} <= codes


def test_the_proposal_is_pending_never_ticked_and_says_the_features_in_plain_words() -> None:
    out = ask(preset="rfm")
    assert out["state"] == "pending" and out["ticked"] is False and out["never_ticked"] is True
    assert out["step"]["kind"] == "sql_query"
    for words in ("days since the last order", "spend in the last year", "100% filled", "2025-06-30"):
        assert words in out["sentence"]
    assert out["features_as_of"] == "2025-06-30"
    assert parse_query_output(out).payload()["features_as_of"] == "2025-06-30"
    assert "as_of" not in parse_query_output(out).payload()  # that key would set the case's date


def test_no_as_of_date_is_asked_for_not_guessed() -> None:
    ctx = DataToolContext(files=(loaded(invoices()),), caller="ai", principal=PRINCIPAL)
    given = {
        "upload_id": "up-1",
        "entity_key": "Customer",
        "time_column": "InvoiceDate",
        "preset": "rfm",
        "amount_column": "Amount",
    }
    with pytest.raises(DataToolError) as caught:
        data_registry().call("propose_features", given, ctx)
    assert caught.value.code == "FEATURE_NEEDS_AS_OF"
    ctx = DataToolContext(files=(loaded(invoices()),), caller="ai", principal=PRINCIPAL, as_of=AS_OF)
    assert data_registry().call("propose_features", given, ctx).output["rows_out"] == 2  # the case's date


def test_the_trial_is_the_run() -> None:
    files = (loaded(invoices()),)
    out = ask(preset="rfm")
    step = RecipeStep.model_validate(out["step"])
    item = CaseItem.model_validate(
        {
            "item_id": "feat",
            "kind": "recipe_step",
            "source": "ai",
            "confidence": "check",
            "state": ProposalState.ACCEPTED.value,
            "upload_id": None,
            "payload": {"step": step.model_dump(mode="json")},
            "payload_digest": "feat",
            "proposed_at": NOW,
        }
    )
    sample = materialize_plan((), [item], files, Sample(100))
    full = materialize_plan((), [item], files, Full())
    assert not sample.failed and not full.failed
    table = full.by_upload["up-1"]
    assert list(sample.tables[table].columns) == list(full.tables[table].columns)
    assert out["rows_out"] == len(full.tables[table]) == 2
    expected = made(invoices(), preset="rfm").reset_index()
    pd.testing.assert_frame_equal(full.tables[table].reset_index(drop=True), expected)
    ran = next(r for item_id, r in full.receipts if item_id == "feat")
    assert out["receipt"]["rows_out"] == ran.rows_out


def test_the_one_materialisation_refuses_a_feature_step_that_reads_the_future() -> None:
    """The probe is in every scope: the same step with a query that ignores the as-of date fails the run."""
    files = (loaded(invoices()),)
    out = ask(preset="rfm")
    leaky = RecipeStep.model_validate(out["step"])
    leaky = leaky.model_copy(
        update={
            "params": {
                **leaky.params,
                "sql": "SELECT Customer, sum(Amount) AS spend_ever FROM t_invoices GROUP BY ALL",
            }
        }
    )
    item = CaseItem.model_validate(
        {
            "item_id": "leaky",
            "kind": "recipe_step",
            "source": "ai",
            "confidence": "check",
            "state": ProposalState.ACCEPTED.value,
            "upload_id": None,
            "payload": {"step": leaky.model_dump(mode="json")},
            "payload_digest": "leaky",
            "proposed_at": NOW,
        }
    )
    for scope in (Sample(100), Full()):
        prepared = materialize_plan((), [item], files, scope)
        assert [f.code for f in prepared.failures] == ["FUTURE_EVENTS_LEAKED"]
