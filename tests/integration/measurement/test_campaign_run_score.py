"""Plan K M135 (DEC-1324) through the product's own API, on a real trained and scored run.

The journey a person takes: upload a Telco file, train, upload a second file, score it (the real local job
runner, the smallest real model search), record the campaign that list went out as, register a test plan that
adjusts by the scoring run's own prediction (`covariate_source: run_score`), upload the outcomes and measure.
The outcomes are drawn from the run's own predicted chance of churn (so the prediction carries real
information, as it does in a real campaign) and a known effect of the contact. The report's adjusted figures
must equal `adjusted_difference` computed by hand on arrays joined by a pandas merge on the key; the same
outcomes in another row order must give the same report; the Value Proof Pack and the value view read the
adjusted estimate with plain words and verified figures.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import create_app
from engine.contracts import RunRecord, RunState
from engine.measurement.campaign import ASSIGNMENT_FILENAME, REPORT_FILENAME, campaign_key, read_frame
from engine.measurement.continuous import adjusted_difference
from engine.pilot.plain import jargon_in
from engine.pilot.proof import build_proof, figures_of, proof_document, verify_provenance
from engine.pilot.roi import RoiInputs, compute_roi
from engine.stages import export
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import IncrementalityReport
from engine.utils.time import utc_now
from tests.fixtures.make_data import CLEAN, SCORING, GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "telco-churn"
PRIMARY_KEY = "customerID"
SCORE_FIELD = "churn_prob"
"""`actions.score_field` of `configs/use_cases/telco_churn.yaml`: the run's predicted chance of churn."""
OUTCOME = "churned"
TRAIN_ROWS = 2_500
SCORE_ROWS = 6_000
RUN_TIMEOUT_S = 600.0
EFFECT = -0.06
"""A contact lowers the chance of churning by six points."""
OVERRIDES: dict[str, Any] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.candidates": ["LogisticRegression"],
    "model_search.ensemble": False,
}
"""The smallest real search there is (`tests/integration/test_score_yes_no_columns.py`)."""


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    run: RunRecord
    scores: pd.DataFrame
    outcomes: pd.DataFrame
    as_of: str
    """One moment every measurement reads the outcomes as of, after the run finished."""


def _upload(client: TestClient, frame: pd.DataFrame, *, name: str, mode: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads", files={"file": (name, payload, "text/csv")}, data={"use_case": USE_CASE, "mode": mode}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _finish(client: TestClient, run_id: str) -> RunRecord:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        record = RunRecord.model_validate(response.json()["run"])
        if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
            return record
        assert time.monotonic() < deadline, f"run {run_id} still {record.state}"
        time.sleep(0.5)


def _run(client: TestClient, body: dict[str, Any]) -> RunRecord:
    response = client.post("/runs", json={"use_case": USE_CASE, "primary_key": PRIMARY_KEY, **body})
    assert response.status_code == 202, response.text
    record = _finish(client, str(response.json()["run_id"]))
    assert record.state is RunState.DONE, record.error
    return record


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("run-score")
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        train = generate(GenerationSpec(USE_CASE, rows=TRAIN_ROWS, variant=CLEAN, seed=1351))
        trained = _run(
            client,
            {
                "mode": "train",
                "upload_id": _upload(client, train, name="train.csv", mode="train"),
                "target": "Churn",
                "overrides": OVERRIDES,
            },
        )
        score = generate(GenerationSpec(USE_CASE, rows=SCORE_ROWS, variant=SCORING, seed=1352))
        scored = _run(
            client,
            {
                "mode": "score",
                "upload_id": _upload(client, score, name="score.csv", mode="score"),
                "model_version_id": trained.model_version_id,
                "overrides": OVERRIDES,
            },
        )
        storage = LocalStorage(data_dir)
        scores = pd.read_parquet(storage.local_path(run_key(scored.run_id, export.SCORES_PARQUET)))
        # What happened after the list went out: each customer churned with the chance the run predicted
        # (the prediction is informative, as a real one is), six points less when contacted.
        contacted = scores["suppressed_reason"].isna() & ~scores["control_group"].astype(bool)
        chance = np.clip(scores[SCORE_FIELD].to_numpy() + np.where(contacted, EFFECT, 0.0), 0.0, 1.0)
        churned = np.random.default_rng(1353).random(len(scores)) < chance
        outcomes = pd.DataFrame({PRIMARY_KEY: scores[PRIMARY_KEY], OUTCOME: churned.astype(int)})
        yield World(client, storage, scored, scores, outcomes, utc_now().isoformat())


def _campaign(world: World, outcomes: pd.DataFrame, **plan: Any) -> str:
    # Every outcome is in as soon as the list goes out (window 0), so the result can be read today.
    created = _ok(
        world.client.post("/campaigns", json={"run_id": world.run.run_id, "outcome_window_days": 0}), 201
    )
    campaign_id = str(created["campaign"]["campaign_id"])
    body = {
        "metric": "churned after the campaign",
        "outcome_column": OUTCOME,
        "analysis_date": utc_now().date().isoformat(),
        **plan,
    }
    if plan.get("register", True):
        body.pop("register", None)
        _ok(world.client.post(f"/campaigns/{campaign_id}/plan", json=body), 201)
    upload_id = _upload(world.client, outcomes, name="outcomes.csv", mode="score")
    outcome_body: dict[str, Any] = {"upload_id": upload_id, "outcome_column": OUTCOME}
    if "treatment_date" in outcomes.columns:
        outcome_body["treatment_date_column"] = "treatment_date"
    _ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json=outcome_body))
    return campaign_id


def _measure(world: World, campaign_id: str, **body: Any) -> Any:
    return world.client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": world.as_of, **body})


def _by_hand(world: World, campaign_id: str) -> tuple[float, float, float, float]:
    """`adjusted_difference` on the measured customers, joined by a pandas merge on the key."""
    assignment = read_frame(world.storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    measured = assignment[assignment["intended"] & (assignment["arm"] != "suppressed")]
    joined = measured.merge(world.outcomes, on=PRIMARY_KEY).merge(
        world.scores[[PRIMARY_KEY, SCORE_FIELD]], on=PRIMARY_KEY
    )
    y = joined[OUTCOME].to_numpy(dtype=float)
    x = joined[SCORE_FIELD].to_numpy(dtype=float)
    treated = (joined["arm"] == "treated").to_numpy()
    result = adjusted_difference(y[treated], x[treated], y[~treated], x[~treated])
    assert result is not None
    return result.difference, result.low, result.high, result.variance_reduction


def test_the_journey_measures_a_yes_no_campaign_adjusted_by_the_runs_prediction(world: World) -> None:
    campaign_id = _campaign(world, world.outcomes, covariate_source="run_score")
    measured = _ok(_measure(world, campaign_id))
    report = IncrementalityReport.model_validate(measured["report"])
    assert report.covariate_source == "run_score" and report.covariate_column is None
    assert report.rows_covariate_missing == 0  # counted: every measured customer was scored by the run
    assert report.adjusted_interval is not None and report.adjusted_lift is not None
    assert report.absolute_lift is not None, "the unadjusted estimate is always there"
    value, low, high, reduction = _by_hand(world, campaign_id)
    assert report.adjusted_lift == pytest.approx(value, abs=1e-12)
    assert report.adjusted_interval.ci_low == pytest.approx(low, abs=1e-12)
    assert report.adjusted_interval.ci_high == pytest.approx(high, abs=1e-12)
    assert report.variance_reduction == pytest.approx(reduction, abs=1e-12)
    assert "the scoring run predicted" in report.summary and jargon_in(report.summary) == ()
    verdict = measured["verdict"]
    assert verdict is not None and "the scoring run predicted" in verdict["detail"]
    stored = world.storage.read_model(campaign_key(campaign_id, REPORT_FILENAME), IncrementalityReport)
    assert stored.adjusted_interval == report.adjusted_interval

    # The same outcomes in another row order: the same report (the prediction is joined on the key).
    shuffled = world.outcomes.sample(frac=1.0, random_state=7).reset_index(drop=True)
    other_id = _campaign(world, shuffled, covariate_source="run_score")
    again = IncrementalityReport.model_validate(_ok(_measure(world, other_id))["report"])
    skip = {"campaign_id", "computed_at", "test_plan_hash"}
    assert again.model_dump(exclude=skip) == report.model_dump(exclude=skip)

    # The Value Proof Pack and the value view read the adjusted estimate, in plain words, every figure traced.
    view = build_proof(world.storage, campaign_id)
    verify_provenance(view, world.storage)
    labels = [line.label for section in view.sections for line in section.lines]
    assert "Difference (adjusted for the predicted chance registered in the plan)" in labels
    document = proof_document(view)
    assert jargon_in(document.model_dump_json(exclude={"schema_version", "generated_at", "footer"})) == ()
    assert any(
        source.field.startswith("adjusted_interval.") for f in figures_of(view) for source in f.sources
    )
    roi = compute_roi(
        world.storage,
        world.run.run_id,
        campaign_id=campaign_id,
        inputs=RoiInputs(value_per_outcome=100.0, contact_cost=1.0),
    )
    assert roi.adjusted is True and roi.incremental is not None and roi.value_note is not None
    assert roi.incremental.value == pytest.approx(report.adjusted_lift * report.treated_rows)
    assert jargon_in(roi.value_note) == ()


def test_a_customer_contacted_before_the_run_finished_is_refused(world: World) -> None:
    assert world.run.finished_at is not None
    early = world.outcomes.assign(
        treatment_date=(world.run.finished_at.date() - pd.Timedelta(days=1)).isoformat()
    )
    campaign_id = _campaign(world, early, covariate_source="run_score")
    refused = _measure(world, campaign_id)
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "COVARIATE_NOT_BEFORE_CAMPAIGN"
    assert "made after the campaign began" in detail["message"]
    assert not world.storage.exists(campaign_key(campaign_id, REPORT_FILENAME))


def test_a_plan_without_run_score_is_measured_as_before_and_refuses_it(world: World) -> None:
    campaign_id = _campaign(world, world.outcomes)
    refused = _measure(world, campaign_id, covariate_source="run_score")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "TEST_PLAN_CHANGED"
    assert "covariate_source" in refused.json()["detail"]["message"]
    report = _ok(_measure(world, campaign_id))["report"]
    assert not {"covariate_source", "adjusted_interval", "adjusted_lift", "rows_covariate_missing"} & set(
        report
    )
    unplanned = _campaign(world, world.outcomes, register=False)
    without = _measure(world, unplanned, covariate_source="run_score")
    assert without.status_code == 409 and without.json()["detail"]["code"] == "TEST_PLAN_CHANGED"


def test_run_score_is_amended_like_any_field(world: World) -> None:
    campaign_id = _campaign(world, world.outcomes)
    amended = _ok(
        world.client.post(
            f"/campaigns/{campaign_id}/plan/amendments",
            json={
                "metric": "churned after the campaign",
                "outcome_column": OUTCOME,
                "analysis_date": utc_now().date().isoformat(),
                "covariate_source": "run_score",
                "reason": "Adjust by the run's own prediction",
            },
        ),
        201,
    )
    assert amended["version"] == 2 and amended["covariate_source"] == "run_score"
    report = IncrementalityReport.model_validate(_ok(_measure(world, campaign_id))["report"])
    assert report.covariate_source == "run_score" and report.test_plan_hash == amended["plan_hash"]


def test_run_score_needs_one_scoring_run() -> None:
    from datetime import UTC, datetime

    from api.routes.campaigns import _resolved
    from engine.measurement.campaign import AssignmentCounts, Campaign, CampaignKind, CampaignStatus
    from engine.measurement.plan import TestPlanInput

    moment = datetime(2026, 3, 1, tzinfo=UTC)
    counts = AssignmentCounts(
        rows=1, suppressed=0, treated=1, holdout=0, intended=1, intended_treated=1, intended_holdout=0
    )
    audited = Campaign(
        campaign_id="c_20260301_00000000",
        kind=CampaignKind.EXTERNAL,
        name="Audit",
        primary_key=PRIMARY_KEY,
        treatment_start=moment,
        treatment_start_source="entered",
        population="eligible",
        causal=True,
        causal_basis="declared_random",
        counts=counts,
        status=CampaignStatus.LIVE,
        created_at=moment,
        created_by="u_1",
    )
    plan = TestPlanInput(
        metric="churned", outcome_column=OUTCOME, analysis_date=moment.date(), covariate_source="run_score"
    )
    with pytest.raises(HTTPException) as raised:
        _resolved(audited, plan)
    assert raised.value.status_code == 422 and "not made from one of our scoring runs" in str(
        raised.value.detail
    )
    several = audited.model_copy(update={"kind": CampaignKind.SCORED, "run_ids": ("r_1", "r_2")})
    with pytest.raises(HTTPException, match="several scoring runs"):
        _resolved(several, plan)
