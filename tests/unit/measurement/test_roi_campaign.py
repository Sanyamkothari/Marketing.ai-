"""The value view reads a campaign's report first (Plan J M94, DEC-1304 (i)).

A scoring run's list that went out as a campaign is measured with its real treatment start and its
registered test plan, under `campaigns/<id>/`. `engine.pilot.roi.compute_roi` is keyed by a run or a
campaign of it: the named campaign's final report, else the run's latest measured campaign, else the
run's own report as before. An early look is never priced.
"""

from __future__ import annotations

from datetime import timedelta

from engine.measurement.campaign import (
    CAMPAIGN_FILENAME,
    REPORT_FILENAME,
    campaign_key,
)
from engine.pilot.roi import compute_roi
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import INCREMENTALITY_FILENAME, ConfidenceValue
from tests.unit.pilot.test_roi import INPUTS, NOW, RUN, mature_report, storage

_ = storage  # the pilot tests' run fixture: a finished telco-churn scoring run


def _campaign(
    store: LocalStorage, campaign_id: str, *, low: float, high: float, early: bool = False, minutes: int = 0
) -> None:
    store.write_text(
        campaign_key(campaign_id, CAMPAIGN_FILENAME),
        f'{{"campaign_id": "{campaign_id}", "run_ids": ["{RUN}"]}}',
    )
    report = mature_report(low=low, high=high).model_copy(
        update={
            "campaign_id": campaign_id,
            "incremental_conversions": ConfidenceValue(value=(low + high) / 2, ci_low=low, ci_high=high),
            "early_look": early,
            "computed_at": NOW + timedelta(minutes=minutes),
        }
    )
    store.write_model(campaign_key(campaign_id, REPORT_FILENAME), report)


def test_the_runs_own_report_is_read_when_no_campaign_was_measured(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report(low=30.0, high=90.0))
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.campaign_id is None and view.incremental is not None and view.incremental.low == 30.0


def test_a_campaigns_final_report_wins_over_the_runs(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report(low=30.0, high=90.0))
    _campaign(storage, "c_20261007_00000001", low=10.0, high=50.0, minutes=0)
    _campaign(storage, "c_20261007_00000002", low=20.0, high=60.0, minutes=5)
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.status == "measured" and view.source == "incrementality_report"
    assert view.campaign_id == "c_20261007_00000002", "the latest measured campaign of the run"
    assert view.incremental is not None and (view.incremental.low, view.incremental.high) == (20.0, 60.0)
    named = compute_roi(storage, RUN, inputs=INPUTS, campaign_id="c_20261007_00000001")
    assert named.campaign_id == "c_20261007_00000001"
    assert named.incremental is not None and named.incremental.low == 10.0


def test_an_early_look_is_never_priced(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report(low=30.0, high=90.0))
    _campaign(storage, "c_20261007_00000003", low=1.0, high=2.0, early=True)
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.campaign_id is None and view.incremental is not None and view.incremental.low == 30.0
    alone = compute_roi(storage, RUN, inputs=INPUTS, campaign_id="c_20261007_00000003")
    assert alone.campaign_id is None, "the named campaign has only an early look: the run's report is read"


def test_another_runs_campaign_is_not_read(storage: LocalStorage) -> None:
    storage.write_text(
        campaign_key("c_20261007_00000009", CAMPAIGN_FILENAME),
        '{"campaign_id": "c_20261007_00000009", "run_ids": ["r_other"]}',
    )
    storage.write_model(
        campaign_key("c_20261007_00000009", REPORT_FILENAME),
        mature_report().model_copy(update={"campaign_id": "c_20261007_00000009"}),
    )
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.campaign_id is None and view.status == "not_measured"
