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
    replay_build,
)
from engine.doctor.contracts import Diagnosis
from tests.unit.doctor.m237_support import CUSTOMERS, RecordingBuilder, diagnosis, file, frames

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
    bill_rows = sum(len(frame) for name, frame in frames().items() if name in {"u_jan", "u_feb", "u_mar"})
    assert (bills.rows, bills.rows_dropped, bills.dropped_share) == (bill_rows, 0, 0.0)
    assert entity.rows is None and entity.dropped_share is None
    assert "customers.csv keeps its IDs as they are" in plan.approval_sentence
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
        "save_record",
        "build",
    ]
    assert result.approved_by == "u-analyst" and result.dataset_id == "ds_1" and result.spec_id == "spec_1"
    customers, bills = port.data["s1"], port.data["s2"]
    assert list(customers["customer_id"]) == list(
        loaded["u_list"]["customer_id"]
    ), "the list's IDs as written"
    assert list(bills["customer_id"][:3]) == [
        "CUST-00001",
        "CUST-00001",
        "CUST-00002",
    ], "the bills' IDs as the list writes them"
    assert set(bills["customer_id"]) == set(customers["customer_id"])
    assert len(bills) == 3 * 2 * CUSTOMERS, "each bill once"
    assert list(bills.columns) == ["customer_id", "invoice_date", "amount"]
    (key_receipt,) = result.key_receipts  # the bills' own step (zeros); the list has none
    assert key_receipt.steps[0].kind == "normalise_key" and key_receipt.steps[0].changed == 0
    (matched,) = result.match_receipts
    assert (matched.rows, matched.rows_matched, matched.rows_rewritten) == (6 * CUSTOMERS,) * 3
    assert matched.rows_not_on_list == matched.rows_ambiguous == matched.rows_without_key == 0
    assert result.record_key == "records/spec_1" and port.records["spec_1"].plan == plan
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


def _log_with_strangers(strangers: int) -> tuple[Diagnosis, dict[str, pd.DataFrame]]:
    """A list of 100 customers `C-001`... and a log of 100 rows keyed `1`..., `strangers` of them for customers
    the list does not have."""
    listed = pd.DataFrame({"customer_id": [f"C-{n:03d}" for n in range(1, 101)]})
    log = pd.DataFrame(
        {
            "customer_id": [str(n) for n in range(1, 101 - strangers)]
            + [str(500 + n) for n in range(strangers)],
            "event_date": ["2025-01-05"] * 100,
        }
    )
    files = (
        file("c", "c.csv", listed, "entity", id_column="customer_id"),
        file("e", "e.csv", log, "bills", id_column="customer_id", date_columns=("event_date",)),
    )
    return Diagnosis(files=files, use_cases=(), links=(), stacks=()), {"c": listed, "e": log}


def test_a_file_with_no_name_is_named_by_its_upload_id_never_left_blank() -> None:
    """M237 probe: the sentence read "... customers.csv and . ..." for a file whose stored name was empty."""
    found, loaded = _log_with_strangers(15)
    blank = tuple(
        item.model_copy(update={"file_name": "  " if item.upload_id == "e" else item.file_name})
        for item in found.files
    )
    plan = plan_build(found.model_copy(update={"files": blank}), loaded, use_case="rca", label_spec=None)
    (log,) = plan.events
    assert log.source_name == "e"
    assert plan.approval_sentence.startswith("Build the table from c.csv and e.")
    assert "Joining e to c.csv leaves out 15 of 100 rows" in plan.approval_sentence
    assert " and ." not in plan.approval_sentence and "Joining  to" not in plan.approval_sentence


def test_a_join_that_drops_more_than_two_percent_of_a_log_needs_approval() -> None:
    """M237 review: 15 of 100 log rows name customers the list lacks; the plan says so and asks."""
    found, loaded = _log_with_strangers(15)
    plan = plan_build(found, loaded, use_case="rca", label_spec=None)
    (log,) = plan.events
    assert log.fixed_coverage == 0.85
    assert (log.rows, log.rows_dropped, log.dropped_share) == (100, 15, 0.15)
    assert "JOIN_DROPS_ROWS" in plan.approval_reasons
    assert "Joining e.csv to c.csv leaves out 15 of 100 rows (15.0%)" in plan.approval_sentence
    few, loaded_few = _log_with_strangers(2)
    assert (
        "JOIN_DROPS_ROWS" not in plan_build(few, loaded_few, use_case="rca", label_spec=None).approval_reasons
    )


def test_the_strangers_rows_are_counted_and_kept_as_written() -> None:
    found, loaded = _log_with_strangers(15)
    plan = plan_build(found, loaded, use_case="rca", label_spec=None)
    port = RecordingBuilder()
    result = drive_builder(plan, loaded, port, approval_for(plan, user_id="u", at=NOW))
    (matched,) = result.match_receipts
    assert (matched.rows_matched, matched.rows_not_on_list, matched.dropped_share) == (85, 15, 0.15)
    log = port.data["s2"]["customer_id"]
    assert list(log[:2]) == ["C-001", "C-002"] and list(log[-2:]) == ["513", "514"]


def test_an_id_two_customers_share_under_the_rule_is_never_given_to_one() -> None:
    listed = pd.DataFrame({"customer_id": ["C-007", "C-0007", *[f"C-{n:03d}" for n in range(10, 60)]]})
    log = pd.DataFrame(
        {"customer_id": ["7", *[str(n) for n in range(10, 60)]], "event_date": ["2025-01-05"] * 51}
    )
    files = (
        file("c", "c.csv", listed, "entity", id_column="customer_id"),
        file("e", "e.csv", log, "bills", id_column="customer_id", date_columns=("event_date",)),
    )
    frames_ = {"c": listed, "e": log}
    plan = plan_build(
        Diagnosis(files=files, use_cases=(), links=(), stacks=()), frames_, use_case="rca", label_spec=None
    )
    (event,) = plan.events
    assert event.rows_dropped == 1
    port = RecordingBuilder()
    result = drive_builder(plan, frames_, port, approval_for(plan, user_id="u", at=NOW))
    (matched,) = result.match_receipts
    assert matched.rows_ambiguous == 1 and matched.rows_matched == 50
    assert port.data["s2"]["customer_id"].iloc[0] == "7", "left as written: it names no one customer"


def test_next_months_files_replay_the_approved_rules_and_keep_the_lists_ids() -> None:
    """M237 review: the rule and the stack are kept with the recipe, and a replay applies them."""
    plan, loaded = _plan()
    port = RecordingBuilder()
    drive_builder(plan, loaded, port, approval_for(plan, user_id="u-analyst", at=NOW))
    record = port.records["spec_1"]
    april = loaded["u_mar"].rename(columns={"bill_amount": "amount"})
    april["invoice_date"] = (
        april["invoice_date"].str.replace("2025-03", "2025-04").str.replace("2025-02", "2025-03")
    )
    later = RecordingBuilder()
    replayed = replay_build(
        record,
        {"customers.csv": [loaded["u_list"]], "bills_stacked.csv": [loaded["u_mar"], april]},
        later,
        mode="score",
    )
    assert later.calls[-1] == ("build", {"spec_id": "spec_1", "mode": "score"})
    bills = later.data["s2"]
    assert set(bills["customer_id"]) == set(
        loaded["u_list"]["customer_id"]
    ), "CUST- IDs, as the list writes them"
    (stacked,) = replayed.stack_receipts
    assert stacked.duplicates_removed == CUSTOMERS, "the overlapping day counted once, by the frozen stack"
    (matched,) = replayed.match_receipts
    assert matched.rows_rewritten == matched.rows == len(bills)
    with pytest.raises(BuildDriveError) as raised:
        replay_build(record, {"customers.csv": [loaded["u_list"]]}, RecordingBuilder())
    assert raised.value.code == "BUILD_REPLAY_FILES"
    tampered = record.model_copy(update={"plan": record.plan.model_copy(update={"use_case": "other"})})
    with pytest.raises(BuildDriveError) as raised:
        replay_build(tampered, {"customers.csv": [loaded["u_list"]]}, RecordingBuilder())
    assert raised.value.code == "BUILD_PLAN_NOT_APPROVED"
