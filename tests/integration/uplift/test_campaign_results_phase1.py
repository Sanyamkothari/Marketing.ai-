"""Campaign results for a Phase 1 scoring run: a propensity model's campaign, not an uplift one.

Plan B §1 item 5 promises an incrementality report "for any campaign" with a Phase 1 control group,
not only for uplift runs. Here the scoring run is Phase 1's own shape - `run.json` with a binary
classification problem type, and `scores.parquet` written by Phase 1's `apply_actions` and
`write_scores` from a propensity score - so `scores` carries bands, actions, the suppression reason
and the control group, and no `intended_treatment` or `segment` column. It is written straight into
the run store rather than trained and scored, because nothing here depends on how the propensity was
estimated (the audit's probe trained a real model and got the same answer); that keeps the test fast.

`POST /runs/{id}/campaign-results` must then compare every eligible treated row with the control
group (intent to treat), or only the named bands, and the lift's interval must cover the effect the
generator planted.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine import __version__
from engine.config import ProblemType, RunMode, UseCaseConfig, load_use_case
from engine.contracts import RunRecord, RunState
from engine.runs import RUN_FILENAME
from engine.stages import export
from engine.stages.actions import apply_actions
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus
from tests.fixtures.make_uplift_data import UpliftDataset, make_winback_campaign, outcomes_for

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
PRIMARY_KEY = "customer_id"
TARGET = "reactivated_90d"
RUN_ID = "r_20260923_1a000001"
SENT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
ROWS = 12_000


@dataclass(frozen=True)
class Phase1Campaign:
    client: TestClient
    storage: LocalStorage
    config: UseCaseConfig
    campaign: UpliftDataset
    scores: pd.DataFrame
    outcomes_upload: str


def _record() -> RunRecord:
    """`run.json` of a finished Phase 1 scoring run of a propensity model."""
    return RunRecord(
        run_id=RUN_ID,
        use_case_id=USE_CASE,
        use_case_name="Win-back Campaign",
        mode=RunMode.SCORE,
        state=RunState.DONE,
        created_at=SENT - timedelta(minutes=5),
        started_at=SENT - timedelta(minutes=4),
        finished_at=SENT,
        upload_id="u_phase1_campaign",
        file_name="winback_campaign.csv",
        row_count=ROWS,
        primary_key=PRIMARY_KEY,
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="auto",
        model_version_id="m_win-back-campaign_1",
        best_model="LightGBM",
        engine_version=__version__,
    )


@pytest.fixture(scope="module")
def phase1(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Phase1Campaign]:
    data_dir = tmp_path_factory.mktemp("phase1-campaign") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    config = load_use_case(USE_CASE, config_root)
    campaign = make_winback_campaign(ROWS, seed=11)
    frame = campaign.frame.drop(columns=[TARGET, "treatment", "treatment_date"])
    frame["marketing_opt_in"] = [index % 10 != 3 for index in range(ROWS)]
    # Any propensity will do: the report measures what the contact changed, whoever was ranked high.
    frame[config.actions.score_field] = campaign.truth["p_treated"].to_numpy()
    scored = apply_actions(frame, config, run_id=RUN_ID, primary_key=PRIMARY_KEY, now=SENT)
    export.write_scores(scored, config, run_id=RUN_ID, primary_key=PRIMARY_KEY, storage=storage)
    storage.write_model(run_key(RUN_ID, RUN_FILENAME), _record())
    scores = pd.read_parquet(storage.local_path(run_key(RUN_ID, export.SCORES_PARQUET)))

    eligible = scores["suppressed_reason"].isna()
    control = scores["control_group"].astype(bool)
    contacted = set(scores.loc[eligible & ~control, PRIMARY_KEY])
    outcomes = outcomes_for(campaign, treated_keys=contacted, seed=5)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        payload = outcomes.to_csv(index=False, lineterminator="\n").encode()
        response = client.post(
            "/uploads",
            files={"file": ("outcomes.csv", payload, "text/csv")},
            data={"use_case": USE_CASE, "mode": "score"},
        )
        assert response.status_code == 201, response.text
        yield Phase1Campaign(
            client=client,
            storage=storage,
            config=config,
            campaign=campaign,
            scores=scores,
            outcomes_upload=str(response.json()["upload_id"]),
        )


def _measure(phase1: Phase1Campaign, **extra: object) -> IncrementalityReport:
    response = phase1.client.post(
        f"/runs/{RUN_ID}/campaign-results",
        json={
            "upload_id": phase1.outcomes_upload,
            "outcome_column": TARGET,
            "outcome_window_days": 90,
            "as_of": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return IncrementalityReport.model_validate(response.json())


def test_the_scores_are_phase_1s_own() -> None:
    columns = export.scores_csv_columns(load_use_case(USE_CASE), PRIMARY_KEY)
    assert "intended_treatment" not in columns and "segment" not in columns


def test_a_phase_1_campaign_is_measured_with_a_lift_and_an_interval(phase1: Phase1Campaign) -> None:
    scores = phase1.scores
    assert tuple(scores.columns) == export.scores_csv_columns(phase1.config, PRIMARY_KEY)
    report = _measure(phase1)
    assert report.status is IncrementalityStatus.MATURE and report.causal is True
    eligible = scores["suppressed_reason"].isna()
    control = scores["control_group"].astype(bool)
    # Intent to treat: every eligible row is compared, treated against the random control group.
    assert report.treated_rows == int((eligible & ~control).sum())
    assert report.control_rows == int((eligible & control).sum()) > 1_000
    lift = report.absolute_lift
    assert lift is not None and lift.ci_low is not None and lift.ci_high is not None
    truth = phase1.campaign.truth.set_index(PRIMARY_KEY)
    keys = scores.loc[eligible, PRIMARY_KEY]
    planted = float(np.mean(truth.loc[keys, "p_treated"] - truth.loc[keys, "p_control"]))
    # Contacting everyone eligible mixes persuadables with sleeping dogs, so the planted average is
    # small (about +2 points); the interval must cover it, whatever side of zero its ends fall.
    assert lift.ci_low <= planted <= lift.ci_high, (lift, planted)
    assert report.p_value is not None
    assert report.summary.startswith("Treated customers converted at")
    stored = phase1.client.get(f"/runs/{RUN_ID}/campaign-results")
    assert stored.status_code == 200 and IncrementalityReport.model_validate(stored.json()) == report


def test_a_phase_1_campaign_can_be_measured_within_its_bands(phase1: Phase1Campaign) -> None:
    scores = phase1.scores
    report = _measure(phase1, bands=["High", "Medium"])
    assert report.status is IncrementalityStatus.MATURE
    inside = scores["band"].isin(["High", "Medium"]) & scores["suppressed_reason"].isna()
    control = scores["control_group"].astype(bool)
    assert report.treated_rows == int((inside & ~control).sum()) > 0
    assert report.control_rows == int((inside & control).sum()) > 0
    assert report.absolute_lift is not None and report.absolute_lift.ci_low is not None
