"""`build_dataset` proposes, `drive_builder` runs an approved plan (Plan O M237, DEC-1807 (e), (f)).

Closes DEC-1550 and DEC-1578: Data Doctor no longer prints the onboarding calls (`next_steps`) - it plans them,
and once the person approves the plan's hash and its exact sentence, makes them through a `BuilderPort`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from engine.config import LabelDefinition, LabelType
from engine.doctor.build_driver import (
    BuildDriveError,
    BuildPlan,
    approval_for,
    drive_builder,
    mapping_columns,
    plan_build,
)
from tests.unit.doctor.m237_support import CUSTOMERS, RecordingBuilder, diagnosis, frames

LABEL = LabelDefinition(name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="bills", horizon_days=60)
NOW = datetime(2026, 10, 12, tzinfo=UTC)


def _plan() -> tuple[BuildPlan, dict[str, pd.DataFrame]]:
    loaded = frames()
    return plan_build(diagnosis(loaded), loaded, use_case="rca", label_spec=LABEL), loaded


def test_the_plan_stacks_tidies_ids_and_leaves_out_what_does_not_meet() -> None:
    plan, _ = _plan()
    assert (plan.state, plan.confidence, plan.never_ticked) == ("pending", "check", True)
    entity, events = plan.entity, plan.events
    assert entity.role == "entity" and entity.key_rule.prefix == "CUST-" and entity.key_rule.zeros
    (bills,) = events
    assert bills.upload_ids == ("u_jan", "u_feb", "u_mar") and bills.stack is not None
    assert bills.stack.renames[0].after == "bill_amount"
    assert (bills.coverage, bills.fixed_coverage) == (0.0, 1.0)
    (left,) = plan.left_out
    assert (left.upload_id, left.code) == ("u_phones", "JOIN_KEYS_DO_NOT_MEET")
    (receipt,) = plan.stack_receipts
    assert (
        receipt.duplicates_removed == 2 * CUSTOMERS
    ), "February and March each repeat a day of the month before"
    assert set(plan.approval_reasons) == {"BUILD_REWRITES_IDS", "STACK_DROPS_ROWS", "BUILD_DEFINES_OUTCOME"}
    assert [f.name for f in plan.features] == ["bills_90d", "days_since_last_bills"]
    assert plan.blocking == ()


def test_the_plan_hash_is_stable_and_ignores_wording() -> None:
    first, _ = _plan()
    again, _ = _plan()
    assert first.plan_hash == again.plan_hash
    reworded = first.model_copy(update={"summary": "something else"})
    assert reworded.plan_hash == first.plan_hash


def test_an_approved_plan_drives_the_builder_in_the_manual_setup_order() -> None:
    plan, loaded = _plan()
    port = RecordingBuilder()
    result = drive_builder(plan, loaded, port, approval_for(plan, user_id="u-analyst", at=NOW))
    assert [name for name, _ in port.calls] == [
        "add_source",
        "suggest_mapping",
        "save_mapping",
        "add_source",
        "suggest_mapping",
        "save_mapping",
        "save_spec",
        "build",
    ]
    assert result.approved_by == "u-analyst" and result.dataset_id == "ds_1" and result.spec_id == "spec_1"
    customers, bills = port.data["s1"], port.data["s2"]
    assert list(customers["customer_id"][:3]) == ["1", "2", "3"], "the approved normalise_key step ran"
    assert len(bills) == 3 * 2 * CUSTOMERS, "each bill once"
    assert list(bills.columns) == ["customer_id", "invoice_date", "amount"]
    key_receipt, _ = result.key_receipts
    assert key_receipt.steps[0].changed == CUSTOMERS
    spec = dict(port.calls)["save_spec"]
    assert spec["label_spec"]["name"] == "churn_next_60d" and spec["label_spec"]["horizon_days"] == 60
    assert spec["event_source_ids"] == ["s2"] and spec["entity_source_id"] == "s1"
    saved = [call for name, call in port.calls if name == "save_mapping"][1]["columns"]
    assert {(c["source"], c["standard"]) for c in saved} >= {
        ("customer_id", "entity_key"),
        ("invoice_date", "event_time"),
    }


def test_a_plan_not_approved_or_reworded_is_refused_and_nothing_is_called() -> None:
    plan, loaded = _plan()
    approval = approval_for(plan, user_id="u-analyst", at=NOW)
    port = RecordingBuilder()
    for bad, code in (
        (approval.model_copy(update={"plan_hash": "x"}), "BUILD_PLAN_NOT_APPROVED"),
        (approval.model_copy(update={"sentence": approval.sentence + " Also."}), "APPROVAL_SENTENCE_CHANGED"),
    ):
        with pytest.raises(BuildDriveError) as raised:
            drive_builder(plan, loaded, port, bad)
        assert raised.value.code == code
    changed = plan.model_copy(
        update={"snapshot_spec": plan.snapshot_spec.model_copy(update={"max_snapshots": 3})}
    )
    with pytest.raises(BuildDriveError) as raised:
        drive_builder(changed, loaded, port, approval)
    assert raised.value.code == "BUILD_PLAN_NOT_APPROVED"
    assert port.calls == []


def test_conflicting_exports_wait_for_the_answer() -> None:
    loaded = frames()
    feb = loaded["u_feb"].copy()
    feb.loc[0, "amount"] = 999  # the overlapping day, written differently
    loaded["u_feb"] = feb
    plan = plan_build(diagnosis(loaded), loaded, use_case="rca", label_spec=LABEL)
    assert plan.blocking == ("STACK_CONFLICTS_UNDECIDED",) and len(plan.questions) == 1
    with pytest.raises(BuildDriveError) as raised:
        drive_builder(plan, loaded, RecordingBuilder(), approval_for(plan, user_id="u", at=NOW))
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE"
    answered = plan_build(
        diagnosis(loaded), loaded, use_case="rca", label_spec=LABEL, stack_answers={"u_jan": "later_file"}
    )
    assert answered.blocking == () and answered.questions == ()
    (receipt,) = answered.stack_receipts
    assert receipt.conflicts == 1 and receipt.conflict_rows_removed == 1


def test_an_outcome_on_a_log_the_plan_left_out_blocks_it() -> None:
    loaded = frames()
    label = LABEL.model_copy(update={"role": "payments"})
    plan = plan_build(diagnosis(loaded), loaded, use_case="rca", label_spec=label)
    assert "BUILD_PLAN_INCOMPLETE" in plan.blocking


def test_no_customer_list_is_refused() -> None:
    loaded = frames()
    found = diagnosis(loaded)
    without = found.model_copy(update={"files": tuple(f for f in found.files if f.kind != "entity")})
    with pytest.raises(BuildDriveError) as raised:
        plan_build(without, loaded, use_case="rca", label_spec=LABEL)
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE"


def test_the_required_columns_win_over_the_builders_suggestion() -> None:
    columns = mapping_columns(
        [
            {"source": "cust", "standard": "entity_key", "decided_by": "auto"},
            {"source": "region", "standard": "region", "decided_by": "auto"},
            {"source": "invoice_date", "standard": "signup_date", "decided_by": "auto"},
        ],
        {"entity_key": ("customer_id", None), "event_time": ("invoice_date", "date")},
    )
    assert [(c["source"], c["standard"], c["decided_by"]) for c in columns] == [
        ("customer_id", "entity_key", "user"),
        ("invoice_date", "event_time", "user"),
        ("region", "region", "auto"),
    ]
    assert columns[1]["transform"] == {"kind": "cast", "to_type": "date"}


def test_the_plan_holds_no_customer_id_or_cell() -> None:
    plan, loaded = _plan()
    text = plan.model_dump_json()
    for value in list(loaded["u_list"]["customer_id"]) + list(loaded["u_list"]["email"]):
        assert f'"{value}"' not in text
    assert "@example.com" not in text
