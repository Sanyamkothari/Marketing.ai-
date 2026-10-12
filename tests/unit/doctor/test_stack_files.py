"""`stack_files` (Plan O M237, DEC-1807 (d)): monthly exports stacked, each repeated row counted once.

Plan O §M237's acceptance: three monthly exports that overlap by one week each are stacked; each overlapping
row is counted once, and the receipt lists the duplicates removed. Conflicting duplicates become one question;
a stack that removes more than 2% of the rows is an APPROVE-class item. The builder's bare `pd.concat`
(`engine/onboarding/build.py:463`) counts the overlap twice - the regression these tests pin.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from engine.doctor.contracts import ColumnDrift, ColumnRename, FileStack
from engine.doctor.stack_files import StackError, StackFile, StackSpec, plan_stack, run_stack

CUSTOMERS = 40
START = date(2025, 1, 1)


def bills(first: date, last: date, *, amount: str = "amount") -> pd.DataFrame:
    """One bill per customer every 3 days in [first, last]; the same customer and day is always the same bill."""
    rows = []
    day = START
    while day <= date(2025, 4, 7):
        if first <= day <= last:
            for customer in range(1, CUSTOMERS + 1):
                if (day - START).days % 3 == customer % 3:
                    rows.append(
                        {"customer_id": customer, "bill_date": day.isoformat(), amount: 100 + customer}
                    )
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def exports() -> dict[str, pd.DataFrame]:
    """January to the 7th of February, February to the 7th of March, March to the 7th of April."""
    return {
        "u_jan": bills(date(2025, 1, 1), date(2025, 2, 7)),
        "u_feb": bills(date(2025, 2, 1), date(2025, 3, 7)),
        "u_mar": bills(date(2025, 3, 1), date(2025, 4, 7), amount="bill_amount"),
    }


def stack(**changes: object) -> FileStack:
    base = FileStack(
        upload_ids=("u_jan", "u_feb", "u_mar"),
        file_names=("bills_2025_01.csv", "bills_2025_02.csv", "bills_2025_03.csv"),
        kind="bills",
        columns=("customer_id", "bill_date"),
        rows=0,
        drift=(
            ColumnDrift(
                upload_id="u_mar",
                file_name="bills_2025_03.csv",
                renamed=(ColumnRename(before="amount", after="bill_amount"),),
            ),
        ),
        summary="",
    )
    return base.model_copy(update=changes)


def _week(first: date) -> int:
    """Bills in the 7 days from `first`: what one export repeats from the one before."""
    return len(bills(first, first + timedelta(days=6)))


def test_three_overlapping_monthly_exports_count_each_overlapping_row_once() -> None:
    frames = exports()
    proposal = plan_stack(stack(), frames, key_column="customer_id", date_column="bill_date")
    assert (proposal.state, proposal.confidence, proposal.never_ticked) == ("pending", "check", True)
    assert proposal.question is None and proposal.receipt is not None
    receipt = proposal.receipt
    feb_overlap, mar_overlap = _week(date(2025, 2, 1)), _week(date(2025, 3, 1))
    truth = len(bills(date(2025, 1, 1), date(2025, 4, 7)))
    assert receipt.rows_in == sum(len(frame) for frame in frames.values())
    assert receipt.duplicates_removed == feb_overlap + mar_overlap
    assert receipt.rows_out == truth, "every bill once: no overlap counted twice"
    overlaps = [(o.upload_id, o.with_upload_id, o.rows, o.first_date, o.last_date) for o in receipt.overlaps]
    assert overlaps == [
        ("u_feb", "u_jan", feb_overlap, "2025-02-01", "2025-02-07"),
        ("u_mar", "u_feb", mar_overlap, "2025-03-01", "2025-03-07"),
    ]
    per_file = {f.upload_id: f.duplicates_removed for f in receipt.files}
    assert per_file == {"u_jan": 0, "u_feb": feb_overlap, "u_mar": mar_overlap}
    run = run_stack([frames[u] for u in ("u_jan", "u_feb", "u_mar")], proposal.spec)
    assert list(run.frame.columns) == [
        "customer_id",
        "bill_date",
        "amount",
    ], "the renamed column is read as amount"
    assert not run.frame.duplicated().any()
    assert run.frame["amount"].notna().all()


def test_the_builders_bare_concat_counts_the_overlap_twice() -> None:
    """What `build.py:463` does today: every overlapping bill twice. `stack_files` is the fix."""
    frames = exports()
    concatenated = pd.concat(
        [frames["u_jan"], frames["u_feb"], frames["u_mar"].rename(columns={"bill_amount": "amount"})]
    )
    truth = len(bills(date(2025, 1, 1), date(2025, 4, 7)))
    assert len(concatenated) == truth + _week(date(2025, 2, 1)) + _week(date(2025, 3, 1))


def test_rows_a_file_holds_twice_are_kept_twice() -> None:
    """Counted as a multiset: two identical bills in one export are two bills; only a later copy is removed."""
    first = pd.DataFrame({"customer_id": [1, 1, 2], "bill_date": ["2025-01-05"] * 3, "amount": [10, 10, 7]})
    second = pd.DataFrame(
        {"customer_id": [1, 3], "bill_date": ["2025-01-05", "2025-02-01"], "amount": [10, 9]}
    )
    spec = StackSpec(
        files=(StackFile(upload_id="a", file_name="a.csv"), StackFile(upload_id="b", file_name="b.csv")),
        columns=("customer_id", "bill_date", "amount"),
    )
    run = run_stack([first, second], spec)
    assert run.receipt.duplicates_removed == 1
    assert len(run.frame) == 4
    assert int(((run.frame["customer_id"] == 1) & (run.frame["amount"] == 10)).sum()) == 2


def _conflicting() -> tuple[list[pd.DataFrame], FileStack]:
    first = pd.DataFrame({"customer_id": [1, 2, 3], "bill_date": ["2025-01-31"] * 3, "amount": [10, 20, 30]})
    second = pd.DataFrame({"customer_id": [2, 3, 4], "bill_date": ["2025-01-31"] * 3, "amount": [20, 35, 40]})
    group = FileStack(
        upload_ids=("a", "b"),
        file_names=("jan.csv", "feb.csv"),
        kind="bills",
        columns=("customer_id", "bill_date", "amount"),
        rows=6,
        summary="",
    )
    return [first, second], group


def test_conflicting_duplicates_become_one_question() -> None:
    frames, group = _conflicting()
    proposal = plan_stack(
        group, {"a": frames[0], "b": frames[1]}, key_column="customer_id", date_column="bill_date"
    )
    assert proposal.receipt is None and proposal.conflicts == 1
    assert proposal.question is not None and proposal.question.blocking
    assert {option.option_id for option in proposal.question.options} == {"later_file", "earlier_file"}
    with pytest.raises(StackError) as raised:
        run_stack(frames, proposal.spec)
    assert raised.value.code == "STACK_CONFLICTS_UNDECIDED"


@pytest.mark.parametrize(("rule", "amount"), [("later_file", 35), ("earlier_file", 30)])
def test_the_answer_decides_which_export_wins(rule: str, amount: int) -> None:
    frames, group = _conflicting()
    proposal = plan_stack(
        group,
        {"a": frames[0], "b": frames[1]},
        key_column="customer_id",
        date_column="bill_date",
        on_conflict=rule,  # type: ignore[arg-type]
    )
    assert proposal.receipt is not None and proposal.question is None
    receipt = proposal.receipt
    assert (receipt.duplicates_removed, receipt.conflicts, receipt.conflict_rows_removed) == (1, 1, 1)
    run = run_stack(frames, proposal.spec)
    assert sorted(run.frame["customer_id"]) == [1, 2, 3, 4]
    assert int(run.frame.loc[run.frame["customer_id"] == 3, "amount"].iloc[0]) == amount


def test_a_stack_removing_more_than_two_percent_is_approve_class() -> None:
    frames = exports()
    proposal = plan_stack(stack(), frames, key_column="customer_id", date_column="bill_date")
    assert proposal.receipt is not None
    assert proposal.receipt.removed_share > 0.02
    assert proposal.receipt.approval_reasons == ("STACK_DROPS_ROWS",)
    clean = {
        "u_jan": bills(date(2025, 1, 1), date(2025, 1, 31)),
        "u_feb": bills(date(2025, 2, 1), date(2025, 2, 28)),
        "u_mar": bills(date(2025, 3, 1), date(2025, 3, 31), amount="bill_amount"),
    }
    calm = plan_stack(stack(), clean, key_column="customer_id", date_column="bill_date")
    assert calm.receipt is not None and calm.receipt.duplicates_removed == 0
    assert not calm.receipt.approval_required


def test_a_missing_key_column_is_refused_and_an_extra_column_is_left_out() -> None:
    frames = exports()
    proposal = plan_stack(stack(), frames, key_column="customer_id", date_column="bill_date")
    later = dict(frames)
    later["u_feb"] = frames["u_feb"].assign(channel="app")
    run = run_stack([later[u] for u in ("u_jan", "u_feb", "u_mar")], proposal.spec)
    assert run.receipt.files[1].columns_left_out == ("channel",)
    later["u_feb"] = frames["u_feb"].drop(columns=["customer_id"])
    with pytest.raises(StackError) as raised:
        run_stack([later[u] for u in ("u_jan", "u_feb", "u_mar")], proposal.spec)
    assert raised.value.code == "STACK_COLUMN_MISSING"


def test_the_spec_replays_identically_and_hashes_stably() -> None:
    frames = exports()
    first = plan_stack(stack(), frames, key_column="customer_id", date_column="bill_date")
    again = plan_stack(stack(), frames, key_column="customer_id", date_column="bill_date")
    assert first.spec.spec_hash == again.spec.spec_hash
    ordered = [frames[u] for u in ("u_jan", "u_feb", "u_mar")]
    pd.testing.assert_frame_equal(run_stack(ordered, first.spec).frame, run_stack(ordered, again.spec).frame)
