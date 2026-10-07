"""Scored runs written straight into a store, for the M94 campaign tests (Plan J).

Two shapes, as `tests/integration/uplift/test_campaign_results_phase1.py` writes them: a Phase 1
propensity run (`apply_actions` + `write_scores` over a win-back campaign with a planted effect) and
an uplift run (`apply_uplift_actions` over the same customers, the planted uplift as the prediction),
each with its `run.json` and the outcomes its campaign produced. Nothing is trained: what is measured
is what the contact changed, whoever was ranked high.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pandas as pd
from fastapi.testclient import TestClient

from engine import __version__
from engine.config import ProblemType, RunMode, load_use_case
from engine.contracts import RunRecord, RunState
from engine.runs import RUN_FILENAME
from engine.stages import export
from engine.stages.actions import apply_actions
from engine.storage import LocalStorage, run_key
from engine.uplift.actions import TREAT_ACTION, apply_uplift_actions
from engine.uplift.contracts import SegmentThresholds
from tests.fixtures.make_uplift_data import make_winback_campaign, outcomes_for

USE_CASE: Final[str] = "win-back-campaign"
PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "reactivated_90d"
SENT: Final[datetime] = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
MATURE: Final[datetime] = datetime(2026, 9, 1, tzinfo=UTC)
"""Every 90-day window from SENT (or a week after it) has elapsed by then."""
THRESHOLDS: Final[SegmentThresholds] = SegmentThresholds(
    persuadable_min_uplift=0.02,
    sleeping_dog_max_uplift=-0.01,
    sure_thing_min_probability=0.40,
    sure_thing_from_base_rate=False,
)


@dataclass(frozen=True)
class ScoredRun:
    run_id: str
    scores: pd.DataFrame
    outcomes: pd.DataFrame


def record(run_id: str, *, problem_type: ProblemType, rows: int) -> RunRecord:
    """`run.json` of a finished scoring run that finished at SENT."""
    return RunRecord(
        run_id=run_id,
        use_case_id=USE_CASE,
        use_case_name="Win-back Campaign",
        mode=RunMode.SCORE,
        state=RunState.DONE,
        created_at=SENT - timedelta(minutes=5),
        started_at=SENT - timedelta(minutes=4),
        finished_at=SENT,
        upload_id=f"u_{run_id[-8:]}",
        file_name="winback_campaign.csv",
        row_count=rows,
        primary_key=PRIMARY_KEY,
        problem_type=problem_type,
        model_choice="auto",
        model_version_id=f"m_{USE_CASE}_1",
        best_model="LightGBM",
        engine_version=__version__,
    )


def _customers(rows: int, seed: int) -> tuple[Any, pd.DataFrame]:
    campaign = make_winback_campaign(rows, seed=seed)
    frame = campaign.frame.drop(columns=[TARGET, "treatment", "treatment_date"])
    frame["marketing_opt_in"] = [index % 10 != 3 for index in range(rows)]
    return campaign, frame


def propensity_run(storage: LocalStorage, run_id: str, *, rows: int = 12_000, seed: int = 11) -> ScoredRun:
    """A Phase 1 scoring run: bands, actions, suppression and the random control group."""
    config = load_use_case(USE_CASE)
    campaign, frame = _customers(rows, seed)
    frame[config.actions.score_field] = campaign.truth["p_treated"].to_numpy()
    scored = apply_actions(frame, config, run_id=run_id, primary_key=PRIMARY_KEY, now=SENT)
    export.write_scores(scored, config, run_id=run_id, primary_key=PRIMARY_KEY, storage=storage)
    storage.write_model(
        run_key(run_id, RUN_FILENAME),
        record(run_id, problem_type=ProblemType.BINARY_CLASSIFICATION, rows=rows),
    )
    scores = pd.read_parquet(storage.local_path(run_key(run_id, export.SCORES_PARQUET)))
    contacted = set(scores.loc[scores["suppressed_reason"].isna() & ~scores["control_group"], PRIMARY_KEY])
    return ScoredRun(run_id, scores, outcomes_for(campaign, treated_keys=contacted, seed=5))


def uplift_run(storage: LocalStorage, run_id: str, *, rows: int = 12_000, seed: int = 13) -> ScoredRun:
    """An uplift scoring run: segments, `intended_treatment`, Phase 1's suppression and control group."""
    config = load_use_case(USE_CASE)
    campaign, frame = _customers(rows, seed)
    truth = campaign.truth
    frame["p_treated"] = truth["p_treated"].to_numpy()
    frame["p_control"] = truth["p_control"].to_numpy()
    frame["uplift"] = (truth["p_treated"] - truth["p_control"]).to_numpy()
    scored, _ = apply_uplift_actions(
        frame,
        config,
        run_id=run_id,
        primary_key=PRIMARY_KEY,
        causal=True,
        thresholds=THRESHOLDS,
        now=SENT,
    )
    keep = [PRIMARY_KEY, "uplift", "p_treated", "p_control", "segment", "band", "action"]
    keep += ["intended_treatment", "suppressed_reason", "control_group"]
    scores = scored[keep].reset_index(drop=True)
    buffer = io.BytesIO()
    scores.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, export.SCORES_PARQUET), buffer.getvalue())
    storage.write_model(
        run_key(run_id, RUN_FILENAME), record(run_id, problem_type=ProblemType.UPLIFT, rows=rows)
    )
    treated = scores["action"] == TREAT_ACTION
    contacted = set(scores.loc[treated, PRIMARY_KEY])
    return ScoredRun(run_id, scores, outcomes_for(campaign, treated_keys=contacted, seed=7))


def upload(client: TestClient, frame: pd.DataFrame, *, name: str = "outcomes.csv") -> str:
    """POST /uploads of a frame as CSV; the upload id."""
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": (name, payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": "score"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()
