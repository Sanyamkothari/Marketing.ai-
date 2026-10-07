"""`engine.measurement.campaign.epoch_mismatch`: a campaign is measured against one holdout epoch (M92, M94).

A persistent holdout (DEC-1302) is drawn per epoch; a lowered share or a new salt starts the next one
and releases or reshuffles held-back customers. So a campaign records the epoch of its run's
assignment, and it is not measured when its runs span two epochs, when the run's epoch is no longer
the recorded one, or when the holdout was redrawn before the outcomes were all in
(`CAMPAIGN_EPOCH_MISMATCH`, DEC-1304 (l)).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.holdout.assign import run_holdout_spec
from engine.holdout.spec import HoldoutAssignmentReport, HoldoutLedgerEntry, HoldoutSpec
from engine.measurement.campaign import (
    AssignmentCounts,
    Campaign,
    CampaignKind,
    CampaignStatus,
    epoch_mismatch,
    holdout_identity,
)
from engine.storage import LocalStorage, run_key

SENT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
OUTCOMES_IN = SENT + timedelta(days=90)
KEY = "win-back-campaign"


def spec(epoch: int | None, *, scope: str = "use_case") -> HoldoutSpec:
    if scope == "run":
        return HoldoutSpec(scope="run", fraction=0.1)
    return HoldoutSpec(scope=scope, fraction=0.1, salt_id="0123456789abcdef", epoch=epoch, scope_key=KEY)


def ledger(epoch: int, started: datetime) -> HoldoutLedgerEntry:
    return HoldoutLedgerEntry(
        scope="use_case",
        scope_key=KEY,
        epoch=epoch,
        fraction=0.1,
        salt_id="0123456789abcdef",
        started_at=started,
        updated_at=started,
    )


def campaign(*, epoch: int | None = 1, scope: str = "use_case", runs: tuple[str, ...] = ("r_1",)) -> Campaign:
    counts = AssignmentCounts(
        rows=8, suppressed=2, treated=3, holdout=3, intended=6, intended_treated=3, intended_holdout=3
    )
    persistent = scope != "run"
    return Campaign(
        campaign_id="c_20260501_00000001",
        kind=CampaignKind.SCORED,
        name="Win-back, sent 1 May 2026",
        use_case_id=KEY,
        run_ids=runs,
        primary_key="customer_id",
        treatment_start=SENT,
        treatment_start_source="run_finished",
        outcome_window_days=90,
        population="eligible",
        causal=True,
        causal_basis="engine_random",
        holdout_scope=scope,
        holdout_scope_key=KEY if persistent else None,
        holdout_epoch=epoch if persistent else None,
        counts=counts,
        status=CampaignStatus.LIVE,
        created_at=SENT,
        created_by="u_1",
    )


def test_a_run_without_a_holdout_file_drew_per_run() -> None:
    assert holdout_identity(None) == ("run", None, None)
    assert holdout_identity(spec(None, scope="run")) == ("run", None, None)
    assert holdout_identity(spec(2)) == ("use_case", KEY, 2)


def test_a_per_run_campaign_is_never_refused() -> None:
    assert epoch_mismatch(campaign(scope="run"), {"r_1": None}, None, outcomes_in_by=OUTCOMES_IN) is None


def test_a_campaign_in_its_runs_epoch_with_the_ledger_unchanged_is_measured() -> None:
    assert epoch_mismatch(campaign(), {"r_1": spec(1)}, ledger(1, SENT), outcomes_in_by=OUTCOMES_IN) is None
    assert epoch_mismatch(campaign(), {"r_1": spec(1)}, None, outcomes_in_by=OUTCOMES_IN) is None


def test_runs_spanning_two_epochs_are_refused() -> None:
    both = {"r_1": spec(1), "r_2": spec(2)}
    reason = epoch_mismatch(campaign(runs=("r_1", "r_2")), both, None, outcomes_in_by=OUTCOMES_IN)
    assert reason is not None and "epoch 1" in reason and "epoch 2" in reason
    mixed = {"r_1": spec(1), "r_2": None}
    assert epoch_mismatch(campaign(runs=("r_1", "r_2")), mixed, None, outcomes_in_by=OUTCOMES_IN) is not None


def test_a_run_whose_epoch_no_longer_matches_the_record_is_refused() -> None:
    reason = epoch_mismatch(campaign(epoch=1), {"r_1": spec(2)}, ledger(2, SENT), outcomes_in_by=OUTCOMES_IN)
    assert reason is not None and "no longer" in reason
    assert (
        epoch_mismatch(campaign(scope="run"), {"r_1": spec(1)}, None, outcomes_in_by=OUTCOMES_IN) is not None
    )


def test_a_new_epoch_before_the_outcomes_were_in_is_refused_and_after_them_is_not() -> None:
    inside = ledger(2, OUTCOMES_IN - timedelta(days=1))
    reason = epoch_mismatch(campaign(), {"r_1": spec(1)}, inside, outcomes_in_by=OUTCOMES_IN)
    assert reason is not None and OUTCOMES_IN.date().isoformat() in reason
    after = ledger(2, OUTCOMES_IN)
    assert epoch_mismatch(campaign(), {"r_1": spec(1)}, after, outcomes_in_by=OUTCOMES_IN) is None


def test_two_epochs_since_cannot_be_dated_and_are_refused() -> None:
    later = ledger(3, OUTCOMES_IN + timedelta(days=30))
    reason = epoch_mismatch(campaign(), {"r_1": spec(1)}, later, outcomes_in_by=OUTCOMES_IN)
    assert reason is not None and "2 times" in reason


def test_a_ledger_behind_the_campaign_is_refused() -> None:
    assert (
        epoch_mismatch(campaign(epoch=2), {"r_1": spec(2)}, ledger(1, SENT), outcomes_in_by=OUTCOMES_IN)
        is not None
    )


@pytest.mark.parametrize("engaged", [False, True])
def test_the_runs_holdout_is_read_from_its_holdout_file(tmp_path: Path, engaged: bool) -> None:
    storage = LocalStorage(tmp_path)
    if engaged:
        storage.write_model(
            run_key("r_1", "holdout_assignment.json"),
            HoldoutAssignmentReport(
                run_id="r_1",
                use_case_id=KEY,
                spec=spec(3),
                rows=10,
                holdout_members=1,
                control_rows=1,
                explore_candidates=0,
                explore_rows=0,
                created_at=SENT,
            ),
        )
    found = run_holdout_spec(storage, "r_1")
    assert holdout_identity(found) == (("use_case", KEY, 3) if engaged else ("run", None, None))
