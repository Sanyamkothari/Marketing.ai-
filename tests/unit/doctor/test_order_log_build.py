"""`plan_build` and `drive_builder` on a raw order log (v3 phase 0, B4): the list is derived, the sources are the roll-up,
the plan hash covers the counts, and a plan without the explicit argument is exactly what it was."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.config import LabelDefinition, get_roles, load_use_case
from engine.doctor.build_driver import (
    BuildDriveError,
    BuildPlan,
    OrderLogInput,
    approval_for,
    drive_builder,
    order_log_files,
    plan_build,
    replay_build,
)
from engine.doctor.contracts import Diagnosis
from engine.doctor.diagnose import diagnose_file
from engine.doctor.order_roll_up import LogColumns, find_log_columns, list_matches_log, roll_up
from tests.fixtures.order_log import order_log_frame
from tests.unit.doctor.m237_support import RecordingBuilder
from tests.unit.doctor.order_log_support import USE_CASE, loaded

NOW = datetime(2026, 10, 26, tzinfo=UTC)
LOG = "u_log"
NAME = "online-retail.csv"


@pytest.fixture
def world(config_root: Path):  # type: ignore[no-untyped-def]
    config = load_use_case(USE_CASE, config_root)
    log = order_log_frame()
    diagnosis = Diagnosis(
        files=(diagnose_file(loaded(log, NAME, config_root, LOG), config, get_roles(config_root)),),
        use_cases=(),
        links=(),
        stacks=(),
    )
    columns = find_log_columns(log, customer="CustomerID", order="InvoiceNo", date="InvoiceDate")
    return config, log, diagnosis, OrderLogInput(LOG, columns)


def plan_of(world) -> tuple[BuildPlan, dict[str, pd.DataFrame]]:  # type: ignore[no-untyped-def]
    config, log, diagnosis, order_log = world
    plan = plan_build(
        diagnosis,
        {LOG: log},
        use_case=USE_CASE,
        label_spec=config.label,
        order_log=order_log,
        suggested=config.suggested_features,
    )
    return plan, {LOG: log}


def test_the_plan_derives_the_list_and_rolls_the_log_up_into_sources(world) -> None:  # type: ignore[no-untyped-def]
    plan, _ = plan_of(world)
    assert (plan.state, plan.confidence, plan.never_ticked) == ("pending", "check", True)
    assert plan.entity.role == "entity" and plan.entity.source_name == "online-retail.customers.csv"
    assert plan.entity.key_column == "CustomerID" and plan.entity.key_rule.changes_nothing
    assert [(m.column, m.cast) for m in plan.entity.mapped] == [("signup_date", "date")]
    assert [(e.role, e.source_name) for e in plan.events] == [
        ("orders", "online-retail.orders.csv"),
        ("order_lines", "online-retail.order_lines.csv"),
        ("returns", "online-retail.returns.csv"),
    ]
    orders = plan.events[0]
    assert orders.date_column == "event_time"
    assert [m.column for m in orders.mapped] == [
        "order_value",
        "quantity",
        "order_month",
        "days_since_previous_order",
        "country",
    ]
    assert plan.blocking == () and plan.questions == ()
    assert set(plan.approval_reasons) == {"BUILD_DEFINES_OUTCOME", "JOIN_DROPS_ROWS"}
    spec = plan.order_log
    assert spec is not None and spec.skipped_features == () and spec.customer_column == "CustomerID"
    assert [f.name for f in plan.features] == [
        "recency_days",
        "tenure_days",
        "orders_total",
        "spend_total",
        "order_value_mean",
        "order_value_max",
        "active_months",
        "days_between_orders_mean",
        "country",
        "items_total",
        "distinct_products",
        "returned_orders",
        "returned_items",
    ]
    assert plan.label_spec is not None and plan.label_spec.role == "orders"


def test_the_sentence_says_what_is_left_out_and_what_is_a_return(world) -> None:  # type: ignore[no-untyped-def]
    plan, _ = plan_of(world)
    spec = plan.order_log
    assert spec is not None
    counts = roll_up(world[1], world[3].columns).counts
    assert plan.approval_sentence == (
        f"Build the table from online-retail.csv: one shopper for each different 'CustomerID' ({counts.shoppers:,}), "
        f"with their orders, order lines and returns rolled up from {counts.lines:,} lines, using only what happened "
        f"on or before each month-end. {counts.lines_without_shopper:,} lines with no 'CustomerID' are left out. "
        f"{counts.return_lines:,} lines have a quantity below zero ({counts.return_orders:,} order numbers, "
        f"{counts.cancelled_return_orders:,} of them starting with 'C'); they are counted as returns, not orders, "
        f"and {counts.return_lines_without_c:,} of those lines are on numbers that do not start with 'C'. "
        f"{counts.unpriced_lines:,} order lines have a 'UnitPrice' of zero or less and add nothing to spend. "
        "Description is not used. The outcome is 'reactivated_90d'."
    )
    assert counts.return_lines_without_c == 3 and counts.lines_without_shopper > 0


def test_the_hash_covers_the_roll_up_and_a_different_log_is_not_the_approved_one(world) -> None:  # type: ignore[no-untyped-def]
    plan, frames = plan_of(world)
    again, _ = plan_of(world)
    assert plan.plan_hash == again.plan_hash
    assert plan.model_copy(update={"summary": "other words"}).plan_hash == plan.plan_hash
    assert plan.order_log is not None
    changed = plan.model_copy(
        update={"order_log": plan.order_log.model_copy(update={"orders": plan.order_log.orders + 1})}
    )
    assert changed.plan_hash != plan.plan_hash
    approval = approval_for(plan, user_id="u-analyst", at=NOW)
    port = RecordingBuilder()
    shorter = {LOG: frames[LOG].iloc[:-3]}
    with pytest.raises(BuildDriveError) as raised:
        drive_builder(plan, shorter, port, approval)
    assert (
        raised.value.code == "BUILD_PLAN_NOT_APPROVED"
        and "not the log that was approved" in raised.value.message
    )
    assert port.calls == []


def test_an_approved_plan_materialises_the_list_as_an_entity_source_and_builds(world) -> None:  # type: ignore[no-untyped-def]
    plan, frames = plan_of(world)
    port = RecordingBuilder()
    result = drive_builder(plan, frames, port, approval_for(plan, user_id="u-analyst", at=NOW))
    added = [call for name, call in port.calls if name == "add_source"]
    assert [(c["file_name"], c["role"]) for c in added] == [
        ("online-retail.customers.csv", "entity"),
        ("online-retail.orders.csv", "orders"),
        ("online-retail.order_lines.csv", "order_lines"),
        ("online-retail.returns.csv", "returns"),
    ]
    assert result.dataset_id == "ds_1" and result.approved_by == "u-analyst"
    customers = port.data["s1"]
    assert list(customers.columns) == ["CustomerID", "signup_date"]
    assert list_matches_log(
        customers, frames[LOG], "CustomerID"
    ), "the list is exactly the log's distinct IDs"
    maps = {
        tuple(c["source"] for c in call["columns"]): call
        for name, call in port.calls
        if name == "save_mapping"
    }
    entity_map = next(call for cols, call in maps.items() if "signup_date" in cols)
    standards = {c["source"]: (c["standard"], c.get("transform")) for c in entity_map["columns"]}
    assert standards["CustomerID"] == ("entity_key", None)
    assert standards["signup_date"] == ("signup_date", {"kind": "cast", "to_type": "date"})
    orders_map = next(call for cols, call in maps.items() if "order_value" in cols)
    assert {c["source"]: c["standard"] for c in orders_map["columns"]}.items() >= {
        "CustomerID": "entity_key",
        "event_time": "event_time",
        "order_value": "order_value",
        "days_since_previous_order": "days_since_previous_order",
    }.items()
    spec = dict(port.calls)["save_spec"]
    assert spec["label_spec"]["role"] == "orders" and spec["label_spec"]["where"]["column"] == "quantity"
    assert len(spec["event_source_ids"]) == 3 and spec["entity_source_id"] == "s1"
    assert port.records["spec_1"].plan == plan


def test_next_months_whole_log_replays_through_the_frozen_rules(world) -> None:  # type: ignore[no-untyped-def]
    plan, frames = plan_of(world)
    port = RecordingBuilder()
    drive_builder(plan, frames, port, approval_for(plan, user_id="u-analyst", at=NOW))
    record = port.records["spec_1"]
    bigger = order_log_frame(customers=170)
    again = RecordingBuilder()
    result = replay_build(record, {NAME: [bigger]}, again, mode="score")
    assert result.dataset_id == "ds_1"
    assert (
        len(again.data["s1"]) == bigger["CustomerID"].nunique() > 150
    ), "the counts may change, the meaning may not"
    with pytest.raises(BuildDriveError) as raised:
        replay_build(record, {}, RecordingBuilder())
    assert raised.value.code == "BUILD_REPLAY_FILES"


def test_a_plan_without_the_explicit_argument_is_exactly_what_it_was(world) -> None:  # type: ignore[no-untyped-def]
    config, log, diagnosis, _ = world
    with pytest.raises(BuildDriveError) as raised:
        plan_build(diagnosis, {LOG: log}, use_case=USE_CASE, label_spec=config.label)
    assert (
        raised.value.code == "BUILD_PLAN_INCOMPLETE" and "No file is a customer list" in raised.value.message
    )
    from tests.unit.doctor.m237_support import diagnosis as many_diagnosis
    from tests.unit.doctor.m237_support import frames as many_frames

    loaded_files = many_frames()
    label = LabelDefinition.model_validate(
        {"name": "churn_next_60d", "type": "event_absence", "role": "bills", "horizon_days": 60}
    )
    plan = plan_build(many_diagnosis(loaded_files), loaded_files, use_case="rca", label_spec=label)
    dump = plan.model_dump(mode="json")
    assert "order_log" not in dump and all(
        "mapped" not in source for source in [dump["entity"], *dump["events"]]
    )


def test_features_that_read_a_column_the_log_does_not_have_are_left_out_and_named(config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    log = order_log_frame().drop(columns=["UnitPrice", "Country", "StockCode"])
    diagnosis = Diagnosis(
        files=(diagnose_file(loaded(log, NAME, config_root, LOG), config, get_roles(config_root)),),
        use_cases=(),
        links=(),
        stacks=(),
    )
    columns = find_log_columns(log, customer="CustomerID", order="InvoiceNo", date="InvoiceDate")
    plan = plan_build(
        diagnosis,
        {LOG: log},
        use_case=USE_CASE,
        label_spec=config.label,
        order_log=OrderLogInput(LOG, columns),
        suggested=config.suggested_features,
    )
    assert plan.order_log is not None
    assert set(plan.order_log.skipped_features) == {
        "spend_total",
        "order_value_mean",
        "order_value_max",
        "country",
        "distinct_products",
    }
    assert "Not built, the log has no column they read" in plan.approval_sentence
    assert "recency_days" in {f.name for f in plan.features} and "spend_total" not in {
        f.name for f in plan.features
    }


def test_a_log_with_no_order_number_or_two_shoppers_on_one_order_cannot_be_planned(world) -> None:  # type: ignore[no-untyped-def]
    config, log, diagnosis, order_log = world
    no_order = OrderLogInput(LOG, LogColumns(customer="CustomerID", order=None, date="InvoiceDate"))
    with pytest.raises(BuildDriveError) as raised:
        plan_build(diagnosis, {LOG: log}, use_case=USE_CASE, label_spec=config.label, order_log=no_order)
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE"
    twice = log.copy()
    order = twice.loc[twice["CustomerID"].notna(), "InvoiceNo"].iloc[0]
    twice.loc[twice.index[twice["InvoiceNo"] == order][0], "CustomerID"] = 99999.0
    with pytest.raises(BuildDriveError) as raised:
        plan_build(diagnosis, {LOG: twice}, use_case=USE_CASE, label_spec=config.label, order_log=order_log)
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE"
    with pytest.raises(BuildDriveError):
        plan_build(diagnosis, {}, use_case=USE_CASE, label_spec=config.label, order_log=order_log)


def test_the_files_of_a_plan_are_the_roll_up_row_for_row(world) -> None:  # type: ignore[no-untyped-def]
    plan, frames = plan_of(world)
    files = order_log_files(plan, frames[LOG])
    rolled = roll_up(frames[LOG], world[3].columns)
    pd.testing.assert_frame_equal(files["online-retail.orders.csv"][0], rolled.orders)
    pd.testing.assert_frame_equal(files["online-retail.customers.csv"][0], rolled.customers)
    with pytest.raises(BuildDriveError):
        order_log_files(plan.model_copy(update={"order_log": None}), frames[LOG])
