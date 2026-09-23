"""The M49 monitoring routes through the app: outcomes -> report -> performance-drop alert -> acknowledgement.

The scoring run is a real one, started by "fire now" on a score schedule through the API (the
recipe rebuilt against the client's tables, the champion pinned); its `scores.parquet` is written
by the test in the actions stage's shape, as `tests/unit/production/test_outcomes.py` does, so the
drop the report measures is known to be far beyond the alert level. The outcome file's customer ids
must reach neither the audit trail nor any artefact but the aggregates (DEC-771).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import ResolvedConfig
from engine.contracts import RunState
from engine.platform_db import PLATFORM_DB_FILENAME
from engine.runs import update_run
from engine.storage import run_key
from tests.integration.production.schedules_support import Api, build_api, code_of
from tests.unit.production.scheduling_support import USE_CASE, register_champion
from tests.unit.production.test_firing import training_frame

pytestmark = pytest.mark.integration

MATURE = datetime(2026, 9, 1, tzinfo=UTC)
KEY_PREFIX = "QZOUT"
"""Every outcome row's customer id starts with this, so a leak is one substring search."""


@pytest.fixture
def scored(tmp_path: Path, config_root: Path) -> tuple[Api, str, pd.DataFrame]:
    """An app, and a finished scoring run of the champion with a known `scores.parquet`."""
    api = build_api(tmp_path, config_root)
    register_champion(api.world, training_frame(api.world))
    made = api.client.post(
        "/schedules",
        json={
            "use_case_id": USE_CASE,
            "kind": "score",
            "cadence": "monthly",
            "parameters": {"onboarding_spec_id": api.world.spec.spec_id},
        },
        headers=api.as_("analyst"),
    ).json()
    firing = api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst")).json()
    assert firing["status"] == "running", firing
    run_id = firing["run_id"]
    config = api.world.storage.read_model(run_key(run_id, "run_config.json"), ResolvedConfig).config
    rng = np.random.default_rng(7)
    keys = [f"{KEY_PREFIX}{i:06d}" for i in range(1000)]
    score = np.round(rng.uniform(0.0, 1.0, size=len(keys)), 6)
    frame = pd.DataFrame(
        {
            "entity_key": keys,
            config.actions.score_field: score,
            "band": ["High" if s >= 0.8 else "Medium" if s >= 0.5 else "Low" for s in score],
            "action": "Act now",
            "suppressed_reason": [("opted_out" if i % 25 == 1 else None) for i in range(len(keys))],
            "control_group": [i % 10 == 0 for i in range(len(keys))],
        }
    )
    api.world.storage.write_bytes(run_key(run_id, "scores.parquet"), frame.to_parquet(index=False))
    update_run(api.world.storage, run_id, state=RunState.DONE, finished_at=datetime(2026, 6, 3, tzinfo=UTC))
    api.world.clock.now = MATURE
    return api, run_id, frame.rename(columns={config.actions.score_field: "score"})


def backwards_outcomes(scores: pd.DataFrame) -> bytes:
    """Outcomes the model ranked exactly backwards: a real-world AUC of 0 against a test score of 0.8."""
    churned = np.where(scores["score"] < 0.5, "yes", "no")
    return (
        pd.DataFrame({"entity_key": scores["entity_key"], "churn_next_60d": churned})
        .to_csv(index=False)
        .encode()
    )


def upload(api: Api, run_id: str, payload: bytes, *, name: str = "outcomes.csv", who: str = "analyst") -> Any:
    return api.client.post(
        f"/runs/{run_id}/outcomes",
        files={"file": (name, payload, "text/csv")},
        headers=api.as_(who),
    )


def test_outcomes_make_a_report_an_incrementality_input_and_a_drop_alert_that_is_acknowledged(
    scored: tuple[Api, str, pd.DataFrame],
) -> None:
    api, run_id, scores = scored
    response = upload(api, run_id, backwards_outcomes(scores))
    assert response.status_code == 201, response.text
    report = response.json()
    assert (report["run_id"], report["metric"], report["metric_basis"]) == (
        run_id,
        "roc_auc",
        "control_group",
    )
    assert report["relative_drop_pct"] > report["alert_threshold_pct"]
    assert report["alert_raised"] is True and report["alert_id"]
    assert report["rows_matched"] == 1000 and report["rows_in_file"] == 1000

    read = api.client.get(f"/runs/{run_id}/outcomes", headers=api.as_("viewer"))
    assert read.status_code == 200 and read.json() == report
    incrementality = api.client.get(f"/runs/{run_id}/incrementality-input", headers=api.as_("viewer"))
    assert incrementality.status_code == 200, incrementality.text
    body = incrementality.json()
    assert body["schema_version"] == 1 and body["run_id"] == run_id
    assert body["control"]["rows"] + body["treated"]["rows"] + body["suppressed_rows_excluded"] == 1000
    assert [band["band"] for band in body["by_band"]]

    alerts = api.client.get(
        "/monitoring/alerts", params={"kind": "performance_drop"}, headers=api.as_("viewer")
    ).json()["alerts"]
    assert [alert["alert_id"] for alert in alerts] == [report["alert_id"]]
    assert alerts[0]["run_id"] == run_id and alerts[0]["acknowledged_at"] is None

    acknowledged = api.client.post(
        f"/monitoring/alerts/{report['alert_id']}/acknowledge", headers=api.as_("analyst")
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["acknowledged_by"] == api.user_ids["analyst"]
    assert acknowledged.json()["acknowledged_at"].startswith("2026-09-01")
    again = api.client.post(
        f"/monitoring/alerts/{report['alert_id']}/acknowledge", headers=api.as_("analyst2")
    )
    assert again.json()["acknowledged_by"] == api.user_ids["analyst"], "the first acknowledgement stands"
    still_open = api.client.get(
        "/monitoring/alerts", params={"unacknowledged_only": True}, headers=api.as_("viewer")
    ).json()["alerts"]
    assert report["alert_id"] not in {alert["alert_id"] for alert in still_open}

    (uploaded,) = api.events("monitoring.outcomes_upload")
    assert (uploaded.actor_id, uploaded.object_type, uploaded.object_id) == (
        api.user_ids["analyst"],
        "run",
        run_id,
    )
    assert uploaded.details["count"] == 1000 and uploaded.details["reason_code"] == "PERFORMANCE_DROP"
    assert uploaded.after_hash is not None
    acks = api.events("monitoring.alert_acknowledge")
    assert len(acks) == 2 and all(event.object_id == report["alert_id"] for event in acks)
    assert acks[-1].before_hash is not None


def test_no_outcome_value_is_kept_anywhere_but_the_aggregates(scored: tuple[Api, str, pd.DataFrame]) -> None:
    api, run_id, scores = scored
    assert upload(api, run_id, backwards_outcomes(scores), name=f"{KEY_PREFIX}-file.csv").status_code == 201
    trail = json.dumps([event.model_dump(mode="json") for event in api.events()])
    assert KEY_PREFIX not in trail
    assert KEY_PREFIX.encode() not in (api.data_dir / PLATFORM_DB_FILENAME).read_bytes()
    for name in ("outcome_report.json", "incrementality_input.json"):
        assert KEY_PREFIX not in api.world.storage.read_bytes(run_key(run_id, name)).decode()
    stored = [path for path in api.data_dir.rglob("*") if path.is_file() and KEY_PREFIX in path.name]
    assert stored == [], "the uploaded file was not written"


def test_a_parquet_file_is_read_too(scored: tuple[Api, str, pd.DataFrame]) -> None:
    api, run_id, scores = scored
    frame = pd.read_csv(io.BytesIO(backwards_outcomes(scores)), dtype=str)
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    assert upload(api, run_id, buffer.getvalue(), name="outcomes.parquet").status_code == 201


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("immature", 409, "OUTCOME_WINDOW_NOT_MATURED"),
        ("format", 415, "OUTCOME_FILE_FORMAT_UNSUPPORTED"),
        ("column", 422, "OUTCOME_COLUMN_MISSING"),
        ("run", 404, "OUTCOME_RUN_NOT_FOUND"),
    ],
)
def test_outcomes_that_cannot_be_measured_are_refused_with_the_engines_code(
    scored: tuple[Api, str, pd.DataFrame], change: str, status: int, code: str
) -> None:
    api, run_id, scores = scored
    payload, name, target = backwards_outcomes(scores), "outcomes.csv", run_id
    if change == "immature":
        api.world.clock.now = datetime(2026, 7, 1, tzinfo=UTC)
    elif change == "format":
        name = "outcomes.xlsx"
    elif change == "column":
        payload = pd.DataFrame({"entity_key": scores["entity_key"]}).to_csv(index=False).encode()
    else:
        target = "r_20990101_deadbeef"
    response = upload(api, target, payload, name=name)
    assert response.status_code == status, response.text
    assert code_of(response) == code
    if change == "immature":
        assert "2026-08-02" in response.json()["detail"]["message"], "the refusal names the date"
    (event,) = api.events("monitoring.outcomes_upload")
    assert event.outcome == "failed"
    if change != "format":
        assert event.details["reason_code"] == code


def test_reports_that_do_not_exist_yet_are_404(scored: tuple[Api, str, pd.DataFrame]) -> None:
    api, run_id, _ = scored
    missing = api.client.get(f"/runs/{run_id}/outcomes", headers=api.as_("viewer"))
    assert missing.status_code == 404 and code_of(missing) == "OUTCOME_REPORT_NOT_FOUND"
    none = api.client.get(f"/runs/{run_id}/incrementality-input", headers=api.as_("viewer"))
    assert none.status_code == 404 and code_of(none) == "INCREMENTALITY_INPUT_NOT_FOUND"
    unknown = api.client.get("/runs/r_20990101_deadbeef/outcomes", headers=api.as_("viewer"))
    assert unknown.status_code == 404 and code_of(unknown) == "RUN_NOT_FOUND"
    alert = api.client.post("/monitoring/alerts/al_missing/acknowledge", headers=api.as_("analyst"))
    assert alert.status_code == 404 and code_of(alert) == "ALERT_NOT_FOUND"


@pytest.mark.parametrize("who", ["viewer", "approver", "admin"])
def test_only_an_analyst_uploads_outcomes_or_acknowledges_an_alert(
    scored: tuple[Api, str, pd.DataFrame], who: str
) -> None:
    api, run_id, scores = scored
    alert_id = upload(api, run_id, backwards_outcomes(scores)).json()["alert_id"]
    refused = upload(api, run_id, backwards_outcomes(scores), who=who)
    assert refused.status_code == 403 and code_of(refused) == "ROLE_REQUIRED"
    assert refused.json()["detail"]["message"] == "Only an Analyst can add a run's real outcomes."
    ack = api.client.post(f"/monitoring/alerts/{alert_id}/acknowledge", headers=api.as_(who))
    assert ack.status_code == 403
    assert ack.json()["detail"]["message"] == "Only an Analyst can acknowledge an alert."
    for path in (f"/runs/{run_id}/outcomes", "/monitoring/alerts", "/monitoring/missed-firings"):
        assert api.client.get(path, headers=api.as_(who)).status_code == 200, path
