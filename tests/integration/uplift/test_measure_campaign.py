"""Step 4 of a use case, "Measure the campaign" (Plan H M83), through the product's own API.

The scoring run is Phase 1's own shape, written straight into the run store as
`test_campaign_results_phase1.py` writes it: `scores.parquet` from `apply_actions` + `write_scores`
over a win-back campaign whose treatment effect per customer is planted (`make_uplift_data`), so the
control group is the engine's own random hold-out. Its input file is a real upload with a real
`job_spec.json`, because "Learn who to contact next time" rebuilds the experiment from it.

What is checked: the one-upload measurement finds the outcome column and returns exactly the report
`measure_incrementality` computes on the same files (and the one `POST /campaign-results` stores);
the plain verdict is read off that report; an outcome window that has not elapsed says so and
offers nothing to learn; an operational use case has no step 4; and learning builds the treatment
column on the server and trains a real uplift model with a treat list.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.uploads import load_upload
from engine import __version__
from engine.config import ProblemType, RunMode, load_use_case
from engine.contracts import RunRecord, RunState
from engine.runs import RUN_FILENAME, job_spec_for, write_job_spec
from engine.stages import export
from engine.stages.actions import CONTROL_ACTION, apply_actions
from engine.storage import LocalStorage, run_key, upload_key
from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus
from engine.uplift.incrementality import measure_incrementality
from engine.uplift.measure import CampaignVerdict, VerdictKind, campaign_verdict
from tests.fixtures.make_uplift_data import UpliftDataset, make_winback_campaign, outcomes_for

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
PRIMARY_KEY = "customer_id"
TARGET = "reactivated_90d"
RUN_ID = "r_20260928_3a000001"
OPS_RUN_ID = "r_20260928_3a000002"
SENT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 1, tzinfo=UTC)
ROWS = 12_000
RUN_TIMEOUT_S = 600.0

FAST: dict[str, Any] = {
    "uplift": {
        "base_model": "lightgbm",
        "bootstrap_samples": 50,
        "min_arm_rows": 200,
        "min_arm_positives": 20,
    },
    "governance": {"approval_required": False},
}
"""Seconds instead of minutes, as `test_uplift_api.py` trains: LightGBM, 50 resamples, small floors."""


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    campaign: UpliftDataset
    scores: pd.DataFrame
    outcomes: pd.DataFrame
    outcomes_upload: str


def _upload(client: TestClient, frame: pd.DataFrame, *, name: str, use_case: str = USE_CASE) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": (name, payload, "text/csv")},
        data={"use_case": use_case, "mode": "score"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _record(run_id: str, use_case: str, upload_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        use_case_id=use_case,
        use_case_name=use_case,
        mode=RunMode.SCORE,
        state=RunState.DONE,
        created_at=SENT - timedelta(minutes=5),
        started_at=SENT - timedelta(minutes=4),
        finished_at=SENT,
        upload_id=upload_id,
        file_name="campaign_customers.csv",
        row_count=ROWS,
        primary_key=PRIMARY_KEY,
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        model_choice="auto",
        model_version_id=f"m_{use_case}_1",
        best_model="LightGBM",
        engine_version=__version__,
    )


@dataclass(frozen=True)
class Seeded:
    campaign: UpliftDataset
    scores: pd.DataFrame
    outcomes: pd.DataFrame


def seed_scored_run(
    client: TestClient, storage: LocalStorage, *, run_id: str, rows: int = ROWS, effect_scale: float = 3.0
) -> Seeded:
    """A finished Phase 1 scoring run of win-back with a real input upload and `job_spec.json`, and the
    outcomes its campaign produced (not uploaded). Shared with `tests/integration/test_measure_ui.py`."""
    config = load_use_case(USE_CASE)
    campaign = make_winback_campaign(rows, seed=11)
    inputs = campaign.frame.drop(columns=[TARGET, "treatment", "treatment_date"])
    inputs["marketing_opt_in"] = [index % 10 != 3 for index in range(rows)]
    input_upload = _upload(client, inputs, name="campaign_customers.csv")
    frame = inputs.copy()
    frame[config.actions.score_field] = campaign.truth["p_treated"].to_numpy()
    scored = apply_actions(frame, config, run_id=run_id, primary_key=PRIMARY_KEY, now=SENT)
    files = export.write_scores(scored, config, run_id=run_id, primary_key=PRIMARY_KEY, storage=storage)
    summary = export.summarise(
        scored,
        config,
        run_id=run_id,
        model_version_id=f"m_{USE_CASE}_1",
        model_display_name="LightGBM",
        primary_key=PRIMARY_KEY,
        drift=None,
        files=files,
        scored_at=SENT,
    )
    storage.write_model(run_key(run_id, "scoring_summary.json"), summary)
    record = _record(run_id, USE_CASE, input_upload).model_copy(update={"row_count": rows})
    storage.write_model(run_key(run_id, RUN_FILENAME), record)
    write_job_spec(storage, job_spec_for(record, upload=load_upload(storage, input_upload)))
    scores = pd.read_parquet(storage.local_path(run_key(run_id, export.SCORES_PARQUET)))
    eligible = scores["suppressed_reason"].isna()
    contacted = set(scores.loc[eligible & ~scores["control_group"].astype(bool), PRIMARY_KEY])
    # A strong planted effect, so the verdict is a clear one; the numbers are the engine's either way.
    outcomes = outcomes_for(campaign, treated_keys=contacted, seed=5, effect_scale=effect_scale)
    return Seeded(campaign=campaign, scores=scores, outcomes=outcomes)


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("measure-campaign") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        seeded = seed_scored_run(client, storage, run_id=RUN_ID)
        yield World(
            client=client,
            storage=storage,
            campaign=seeded.campaign,
            scores=seeded.scores,
            outcomes=seeded.outcomes,
            outcomes_upload=_upload(client, seeded.outcomes, name="outcomes.csv"),
        )


def _measure(world: World, **extra: Any) -> dict[str, Any]:
    response = world.client.post(
        f"/runs/{RUN_ID}/measure", json={"upload_id": world.outcomes_upload, **extra}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_control_group_is_the_action_column_s_hold_out(world: World) -> None:
    held = world.scores["action"] == CONTROL_ACTION
    assert held.equals(world.scores["control_group"].astype(bool))
    assert int(held.sum()) > 1_000


def test_step_4_before_any_outcome_says_who_was_held_back(world: World) -> None:
    response = world.client.get(f"/runs/{RUN_ID}/measure")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["offered"] is True and view["report"] is None and view["verdict"] is None
    assert view["held_back"] == int(world.scores["control_group"].astype(bool).sum())
    assert view["learn"]["ready"] is False and view["learn"]["reason"] == "Measure the campaign first."
    assert view["outcome_is_good"] is True


def test_one_upload_measures_the_campaign_exactly_as_the_engine_does(world: World) -> None:
    view = _measure(world, as_of=LATER.isoformat())
    report = IncrementalityReport.model_validate(view["report"])
    config = load_use_case(USE_CASE)
    # The outcome column was found in the file, and the window is the use case's own label horizon.
    assert view["outcomes"]["outcome_column"] == TARGET
    assert report.outcome_window_days == config.label.horizon_days == 90  # type: ignore[union-attr]
    engine = measure_incrementality(
        world.scores,
        world.outcomes,
        run_id=RUN_ID,
        primary_key=PRIMARY_KEY,
        outcome_column=TARGET,
        treatment_time=SENT,
        outcome_window_days=90,
        as_of=LATER,
    )
    same = ("treated_rows", "treated_conversions", "control_rows", "control_conversions", "p_value")
    assert {k: getattr(report, k) for k in same} == {k: getattr(engine, k) for k in same}
    assert report.incremental_conversions == engine.incremental_conversions
    assert report.absolute_lift == engine.absolute_lift
    # ... and it is the report the Campaign results page reads.
    stored = world.client.get(f"/runs/{RUN_ID}/campaign-results")
    assert IncrementalityReport.model_validate(stored.json()) == report

    verdict = CampaignVerdict.model_validate(view["verdict"])
    assert verdict == campaign_verdict(report, outcome_is_good=True, outcome_label=config.target.definition)
    incremental = report.incremental_conversions
    assert incremental is not None and incremental.ci_low is not None and incremental.ci_high is not None
    assert verdict.kind is VerdictKind.ADDED
    assert verdict.amount == round(incremental.value) > 0
    assert verdict.headline == f"The campaign added about {verdict.amount:,} conversions"
    assert (verdict.likely_low, verdict.likely_high) == (
        round(incremental.ci_low),
        round(incremental.ci_high),
    )
    assert view["learn"]["ready"] is True, view["learn"]


def test_an_outcome_window_not_over_yet_says_so_and_offers_nothing_to_learn(world: World) -> None:
    view = _measure(world, as_of=(SENT + timedelta(days=10)).isoformat())
    assert view["report"]["status"] == IncrementalityStatus.IMMATURE.value
    assert view["report"]["treated_rate"] is None, "no rate is shown before the window is over"
    assert view["verdict"]["kind"] == "too_early"
    assert view["verdict"]["headline"] == "Outcome window not over yet"
    assert "30 Jul 2026" in view["verdict"]["detail"]
    assert view["learn"]["ready"] is False
    refused = world.client.post(f"/runs/{RUN_ID}/measure/learn", json={"overrides": FAST})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MEASURE_NOT_READY"
    _measure(world, as_of=LATER.isoformat())  # back to the mature measurement for the tests below


def test_a_file_with_several_candidate_outcomes_is_refused_in_plain_words(world: World) -> None:
    extra = world.outcomes.assign(order_value=1, region="north")
    upload = _upload(world.client, extra, name="too_many.csv")
    response = world.client.post(f"/runs/{RUN_ID}/measure", json={"upload_id": _upload_without_target(world)})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MEASURE_INVALID"
    assert "Keep only the customer id" in response.json()["detail"]["message"]
    # The use case's own outcome column is found among others.
    assert world.client.post(f"/runs/{RUN_ID}/measure", json={"upload_id": upload}).status_code == 200
    _measure(world, as_of=LATER.isoformat())


def _upload_without_target(world: World) -> str:
    frame = world.outcomes.rename(columns={TARGET: "came_back"}).assign(spend=1)
    return _upload(world.client, frame, name="ambiguous.csv")


def test_too_small_a_campaign_says_how_much_more_it_needs(world: World) -> None:
    response = world.client.post(
        f"/runs/{RUN_ID}/measure/learn", json={"overrides": {"uplift": {"min_arm_rows": 50_000}}}
    )
    assert response.status_code == 409
    message = response.json()["detail"]["message"]
    assert message.startswith("To learn who to contact next time, each group needs at least 50,000 customers")


def test_an_operational_use_case_has_no_step_4(world: World, config_root: Path) -> None:
    record = _record(OPS_RUN_ID, "order-fulfillment", "u_unused")
    world.storage.write_model(run_key(OPS_RUN_ID, RUN_FILENAME), record)
    view = world.client.get(f"/runs/{OPS_RUN_ID}/measure").json()
    assert view["offered"] is False
    assert (
        view["reason"] == "Order Fulfillment does not contact customers, so there is no campaign to measure."
    )
    refused = world.client.post(f"/runs/{OPS_RUN_ID}/measure", json={"upload_id": world.outcomes_upload})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MEASURE_NOT_OFFERED"


def test_learning_builds_the_treatment_on_the_server_and_trains_an_uplift_model(world: World) -> None:
    _measure(
        world, as_of=LATER.isoformat()
    )  # self-contained: under pytest -n the module is split across workers
    response = world.client.post(f"/runs/{RUN_ID}/measure/learn", json={"overrides": FAST})
    assert response.status_code == 202, response.text
    uplift_run = str(response.json()["run_id"])
    record = _finish(world.client, uplift_run)
    assert record.mode is RunMode.TRAIN and record.upload_id is not None

    experiment = pd.read_parquet(
        world.storage.local_path(load_upload(world.storage, record.upload_id).source_key)
    )
    scores = world.scores.set_index(world.scores[PRIMARY_KEY].astype(str))
    keys = experiment[PRIMARY_KEY].astype(str)
    assert scores.loc[keys, "suppressed_reason"].isna().all(), "suppressed customers are in neither arm"
    held = scores.loc[keys, "action"].eq(CONTROL_ACTION).to_numpy()
    assert (experiment["contacted"].to_numpy() == (~held).astype(int)).all()
    truth = world.outcomes.set_index(world.outcomes[PRIMARY_KEY].astype(str))
    assert (experiment[TARGET].to_numpy() == truth.loc[keys, TARGET].to_numpy()).all()
    assert len(experiment.index) == int(scores["suppressed_reason"].isna().sum())

    treat_list = world.client.get(f"/runs/{uplift_run}/uplift/policy_recommendation.json")
    assert treat_list.status_code == 200, treat_list.text
    view = world.client.get(f"/runs/{RUN_ID}/measure").json()
    assert view["uplift_run"] == {"run_id": uplift_run, "state": "done"}


def test_learning_from_a_synthetic_campaign_trains_a_synthetic_uplift_run(world: World) -> None:
    """Plan J M95: the uplift run is trained on the campaign's outcomes, so if the outcomes file was marked
    synthetic the experiment upload and the run learned from it are too (they were once recorded plain)."""
    key = upload_key(world.outcomes_upload, "upload.json")
    stored = json.loads(world.storage.read_bytes(key))
    stored["synthetic"] = True
    world.storage.write_bytes(key, json.dumps(stored).encode())
    # Self-contained: under pytest -n this test can run on a worker where no other test measured the
    # campaign, and learning needs a mature measurement (it failed there with MEASURE_NOT_READY).
    _measure(world, as_of=LATER.isoformat())

    response = world.client.post(f"/runs/{RUN_ID}/measure/learn", json={"overrides": FAST})
    assert response.status_code == 202, response.text
    record = _finish(world.client, str(response.json()["run_id"]))
    assert record.synthetic is True
    assert record.upload_id is not None and load_upload(world.storage, record.upload_id).synthetic is True


def _finish(client: TestClient, run_id: str) -> RunRecord:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        body = client.get(f"/runs/{run_id}").json()
        record = RunRecord.model_validate(body["run"])
        if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
            assert record.state is RunState.DONE, body["status"]
            return record
        assert time.monotonic() < deadline, f"run {run_id} did not finish in {RUN_TIMEOUT_S}s"
        time.sleep(0.2)
