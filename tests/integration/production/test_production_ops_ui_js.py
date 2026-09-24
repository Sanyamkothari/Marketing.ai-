"""The M48 privacy and M49 monitoring screens exercised in jsdom against REAL API responses.

`test_production_ui_js.py` does this for sign-in, users and the audit log (M46/M47); this module
does it for the screens added on top of them - consent import and lookup, erasure, access export,
retention's dry run and apply, schedules, firing history with missed slots, alerts and outcomes.

As there, what the node tests replay is what the app answered, never a hand-written copy:

* the monitoring world is `schedules_support.build_api`'s (a client with real tables and a training
  recipe, sign-in on, a fake clock the app shares): a drift schedule the local scheduler finds five
  slots late (missed firings, one catch-up, a `schedule_missed` alert), a score schedule fired by an
  Analyst whose run is then given outcomes the model ranked backwards (an outcome report, the
  incrementality input and a `performance_drop` alert), and a Viewer's refusal;
* the privacy world is `test_privacy_routes.World`'s for consent and retention (a clean and a refused
  consent file, a lookup, a dry run with something due of every kind, its apply and the refused second
  apply), and `sentinel_store.plant`'s for the erasure and the access export - one id planted in every
  store, so the erasure outcome carries real counts per store and real flagged models.

The node tests are in `ui/ops/` (their own directory, so `test_production_ui_js.py`'s run of
`ui/*.test.mjs` with the M46/M47 fixtures never picks them up) and share `ui/harness.mjs`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest

from api.routes.schedules import app_request, get_scheduler
from engine.config import ResolvedConfig
from engine.contracts import RunState
from engine.runs import update_run
from engine.scheduling.scheduler import LocalScheduler
from engine.storage import run_key
from tests.fixtures.node import skip_without_jsdom
from tests.integration.production.schedules_support import Api, build_api
from tests.integration.production.test_monitoring_api import MATURE, backwards_outcomes
from tests.integration.production.test_privacy_erasure_api import Api as ErasureApi
from tests.integration.production.test_privacy_routes import PERSON, World, build_store, consent_csv
from tests.unit.production.scheduling_support import USE_CASE, register_champion
from tests.unit.production.sentinel_store import CLIENT, SENTINEL, plant
from tests.unit.production.test_firing import training_frame

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "ui"
"""The node package holding jsdom (shared with the M46/M47 tests)."""

OPS_DIR: Final[Path] = NODE_DIR / "ops"
"""The M48/M49 jsdom tests."""

LOOKED_UP: Final[str] = "C-00001"
"""A principal of `consent_csv` with a grant and a later withdrawal: two ledger rows."""


def _write(directory: Path, name: str, body: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def _missed_slots(api: Api) -> dict[str, Any]:
    """A drift schedule nobody ticked for five hours: five missed slots, one catch-up, one alert."""
    api.app.state.settings = api.app.state.settings.model_copy(update={"scheduler_backend": "local"})
    made = _ok(
        api.client.post(
            "/schedules",
            json={"use_case_id": USE_CASE, "kind": "drift_check", "cadence": "0 * * * *", "timezone": "UTC"},
            headers=api.as_("analyst"),
        ),
        201,
    )
    scheduler = get_scheduler(app_request(api.app))
    assert isinstance(scheduler, LocalScheduler) and not scheduler.running
    api.world.clock.advance(hours=5, minutes=30)
    scheduler.tick()
    return dict(made)


def _scored_run(api: Api) -> tuple[dict[str, Any], str, pd.DataFrame]:
    """`test_monitoring_api.scored`: a score schedule fired by hand and its run finished with known scores."""
    register_champion(api.world, training_frame(api.world))
    made = _ok(
        api.client.post(
            "/schedules",
            json={
                "use_case_id": USE_CASE,
                "kind": "score",
                "cadence": "monthly",
                "parameters": {"onboarding_spec_id": api.world.spec.spec_id},
            },
            headers=api.as_("analyst"),
        ),
        201,
    )
    firing = _ok(api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst")), 201)
    assert firing["status"] == "running", firing
    run_id = firing["run_id"]
    config = api.world.storage.read_model(run_key(run_id, "run_config.json"), ResolvedConfig).config
    rng = np.random.default_rng(7)
    keys = [f"QZUI{i:06d}" for i in range(1000)]
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
    api.world.clock.now = max(api.world.clock(), MATURE)  # never backwards: the missed slots came first
    return dict(made), run_id, frame.rename(columns={config.actions.score_field: "score"})


def write_monitoring_fixtures(out: Path, root: Path, config_root: Path) -> None:
    api = build_api(root, config_root)
    for role in ("viewer", "analyst", "admin"):
        _write(out, f"me_{role}", _ok(api.client.get("/auth/me", headers=api.as_(role))))
    drift = _missed_slots(api)
    schedule, run_id, scores = _scored_run(api)
    viewer, analyst = api.as_("viewer"), api.as_("analyst")

    _write(out, "schedules", _ok(api.client.get("/schedules", headers=viewer)))
    none = _ok(api.client.get("/schedules", params={"client_id": "c_nobody_here"}, headers=viewer))
    assert none["schedules"] == [], none
    _write(out, "schedules_none", none)
    _write(
        out, "schedule_score", _ok(api.client.get(f"/schedules/{schedule['schedule_id']}", headers=viewer))
    )
    _write(out, "schedule_drift", _ok(api.client.get(f"/schedules/{drift['schedule_id']}", headers=viewer)))
    _write(
        out,
        "firings_drift",
        _ok(api.client.get(f"/schedules/{drift['schedule_id']}/firings", headers=viewer)),
    )
    _write(out, "missed", _ok(api.client.get("/monitoring/missed-firings", headers=viewer)))
    refused = api.client.post("/schedules", json={"use_case_id": USE_CASE}, headers=viewer)
    assert refused.status_code == 403, refused.text
    _write(out, "schedule_refused", refused.json())
    paused = _ok(api.client.post(f"/schedules/{drift['schedule_id']}/disable", headers=analyst))
    _write(out, "schedule_paused", paused)
    fired = _ok(api.client.post(f"/schedules/{drift['schedule_id']}/fire", headers=analyst), 201)
    _write(out, "fired", fired)
    edited = _ok(
        api.client.patch(
            f"/schedules/{schedule['schedule_id']}",
            json={"cadence": "0 6 1 * *", "timezone": "Asia/Kolkata", "parameters": schedule["parameters"]},
            headers=analyst,
        )
    )
    _write(out, "schedule_edited", edited)
    _write(out, "retraining_sync", _ok(api.client.post("/schedules/retraining/sync", headers=analyst)))

    _write(out, "industries", _ok(api.client.get("/industries", headers=viewer)))
    _write(out, "runs", _ok(api.client.get("/runs", params={"mode": "score"}, headers=viewer)))
    _write(out, "run", _ok(api.client.get(f"/runs/{run_id}", headers=viewer)))
    no_report = api.client.get(f"/runs/{run_id}/outcomes", headers=viewer)
    assert no_report.status_code == 404, no_report.text
    _write(out, "outcomes_missing", no_report.json())
    no_input = api.client.get(f"/runs/{run_id}/incrementality-input", headers=viewer)
    assert no_input.status_code == 404, no_input.text
    _write(out, "incrementality_missing", no_input.json())
    not_gated = api.client.get(f"/privacy/runs/{run_id}/consent-report", headers=viewer)
    assert not_gated.status_code == 404, not_gated.text
    _write(out, "consent_report_missing", not_gated.json())
    report = _ok(
        api.client.post(
            f"/runs/{run_id}/outcomes",
            files={"file": ("outcomes.csv", backwards_outcomes(scores), "text/csv")},
            headers=analyst,
        ),
        201,
    )
    _write(out, "outcome_report", report)
    _write(out, "incrementality", _ok(api.client.get(f"/runs/{run_id}/incrementality-input", headers=viewer)))
    _write(
        out,
        "alerts_open",
        _ok(api.client.get("/monitoring/alerts", params={"unacknowledged_only": True}, headers=viewer)),
    )
    _write(
        out,
        "alert_acknowledged",
        _ok(api.client.post(f"/monitoring/alerts/{report['alert_id']}/acknowledge", headers=analyst)),
    )
    _write(out, "alerts_all", _ok(api.client.get("/monitoring/alerts", headers=viewer)))
    # the same run measured on its Campaign results page (ui/modules/uplift): 404 before, the report after
    no_campaign = api.client.get(f"/runs/{run_id}/campaign-results", headers=viewer)
    assert no_campaign.status_code == 404, no_campaign.text
    _write(out, "campaign_results_missing", no_campaign.json())
    campaign_upload = _ok(
        api.client.post(
            "/uploads",
            files={"file": ("outcomes.csv", backwards_outcomes(scores), "text/csv")},
            data={"use_case": USE_CASE, "mode": "score"},
            headers=analyst,
        ),
        201,
    )
    _ok(
        api.client.post(
            f"/runs/{run_id}/campaign-results",
            json={"upload_id": campaign_upload["upload_id"], "outcome_column": "churn_next_60d"},
            headers=analyst,
        )
    )
    _write(out, "campaign_results", _ok(api.client.get(f"/runs/{run_id}/campaign-results", headers=viewer)))
    _write(
        out,
        "ids",
        {"run_id": run_id, "score_schedule": schedule["schedule_id"], "drift_schedule": drift["schedule_id"]},
    )


def write_privacy_fixtures(out: Path, root: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(root / "consent")
    admin = world.admin
    _write(out, "purposes", _ok(world.client.get("/privacy/purposes", headers=admin)))
    files = {"file": ("consent.csv", consent_csv(), "text/csv")}
    _write(
        out, "import_ok", _ok(world.client.post("/privacy/consent/imports", files=files, headers=admin), 201)
    )
    broken = {"file": ("consent.csv", consent_csv(broken_row=3), "text/csv")}
    refused = world.client.post("/privacy/consent/imports", files=broken, headers=admin)
    assert refused.status_code == 422, refused.text
    _write(out, "import_refused", refused.json())
    lookup = _ok(
        world.client.post("/privacy/consent/lookup", json={"principal_id": LOOKED_UP}, headers=admin)
    )
    assert LOOKED_UP not in json.dumps(lookup)
    _write(out, "lookup", lookup)
    viewer_refused = world.client.post(
        "/privacy/erasure", json={"principal_id": PERSON}, headers=world.viewer
    )
    assert viewer_refused.status_code == 403, viewer_refused.text

    build_store(world.root)
    plan = _ok(world.client.get("/privacy/retention/plan", headers=admin))
    _write(out, "retention_plan", plan)
    body = {
        "plan_id": plan["plan"]["plan_id"],
        "planned_at": plan["plan"]["planned_at"],
        "plan_hash": plan["plan_hash"],
    }
    _write(
        out, "retention_applied", _ok(world.client.post("/privacy/retention/apply", json=body, headers=admin))
    )
    again = world.client.post("/privacy/retention/apply", json=body, headers=admin)
    assert again.status_code == 409, again.text
    _write(out, "retention_changed", again.json())

    planted = plant(root / "planted", config_root, monkeypatch)
    erasing = ErasureApi(planted)
    access = erasing.client.post(
        "/privacy/access-requests",
        json={"principal_id": SENTINEL, "client_id": CLIENT},
        headers=erasing.admin,
    )
    assert access.status_code == 200, access.text
    _write(
        out,
        "access_export",
        {"disposition": access.headers["content-disposition"], "size": len(access.content)},
    )
    # Plan D (DEC-863): a background job. First a request whose uploads store keeps failing - its
    # accepted answer, its failed progress and record - then its retry, which finishes it.
    storage = erasing.flaky("uploads/", failures=10_000)
    failed_accepted = _ok(
        erasing.client.post(
            "/privacy/erasure", json={"principal_id": SENTINEL, "client_id": CLIENT}, headers=erasing.admin
        ),
        202,
    )
    request_id = failed_accepted["request_id"]
    failed_record = erasing.finish(request_id)
    assert failed_record["error_code"] == "ERASURE_STORE_FAILED", failed_record
    _write(out, "erasure_accepted", failed_accepted)
    _write(out, "erasure_failed", failed_record)
    _write(
        out,
        "erasure_failed_progress",
        _ok(erasing.client.get(f"/privacy/erasure/{request_id}/progress", headers=erasing.admin)),
    )
    storage.failures = 0
    retried = _ok(
        erasing.client.post(
            f"/privacy/erasure/{request_id}/retry", json={"principal_id": SENTINEL}, headers=erasing.admin
        ),
        202,
    )
    _write(out, "erasure_retry_accepted", retried)
    outcome = erasing.finish(request_id)
    _write(
        out,
        "erasure_progress",
        _ok(erasing.client.get(f"/privacy/erasure/{request_id}/progress", headers=erasing.admin)),
    )
    assert SENTINEL not in json.dumps([failed_accepted, failed_record, retried, outcome])
    _write(out, "erasure", outcome)
    _write(out, "erasures", _ok(erasing.client.get("/privacy/erasure", headers=erasing.admin)))
    _write(out, "retrain_flags", _ok(erasing.client.get("/privacy/retrain-flags", headers=erasing.admin)))
    audit = erasing.client.get(
        "/audit/events",
        params={"action": "privacy.erasure", "object_id": outcome["request_id"]},
        headers=erasing.admin,
    )
    _write(out, "audit_erasure", _ok(audit))

    bare = root / "configs"
    shutil.copytree(config_root, bare)
    (bare / "privacy.yaml").unlink()
    unconfigured = World(root / "unconfigured")
    unconfigured.app.state.config_root = bare
    missing = unconfigured.client.get("/privacy/purposes", headers=unconfigured.admin)
    assert missing.status_code == 409, missing.text
    _write(out, "privacy_not_configured", missing.json())
    _write(out, "typed", {"looked_up": LOOKED_UP, "erased": SENTINEL, "person": PERSON})


def write_ops_fixtures(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "fixtures"
    out.mkdir()
    write_monitoring_fixtures(out, tmp_path / "monitoring", config_root)
    write_privacy_fixtures(out, tmp_path / "privacy", config_root, monkeypatch)
    return out


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_the_ops_fixtures_are_what_the_ui_expects(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs without node: the shapes and sentences the jsdom tests rely on are really the API's."""
    out = write_ops_fixtures(tmp_path, config_root, monkeypatch)
    viewer = {(p["method"], p["path"]): p for p in _read(out, "me_viewer")["permissions"]}
    assert viewer[("POST", "/schedules")]["allowed"] is False
    assert viewer[("POST", "/schedules")]["reason"] == _read(out, "schedule_refused")["detail"]["message"]
    assert viewer[("GET", "/schedules")]["allowed"] is True
    assert viewer[("GET", "/privacy/purposes")]["allowed"] is False
    admin = {(p["method"], p["path"]): p for p in _read(out, "me_admin")["permissions"]}
    assert admin[("POST", "/privacy/erasure")]["allowed"] is True
    assert (
        admin[("POST", "/schedules/{schedule_id}/fire")]["allowed"] is False
    ), "Admin is not Analyst (DEC-703)"

    missed = _read(out, "missed")["firings"]
    assert len(missed) == 5 and all(f["status"] == "missed" for f in missed)
    report = _read(out, "outcome_report")
    assert report["alert_raised"] is True and report["relative_drop_pct"] > report["alert_threshold_pct"]
    assert _read(out, "incrementality")["by_band"]
    assert _read(out, "outcomes_missing")["detail"]["code"] == "OUTCOME_REPORT_NOT_FOUND"
    assert _read(out, "incrementality_missing")["detail"]["code"] == "INCREMENTALITY_INPUT_NOT_FOUND"
    assert _read(out, "consent_report_missing")["detail"]["code"] == "CONSENT_REPORT_NOT_FOUND"
    assert _read(out, "campaign_results_missing")["detail"]["code"] == "CAMPAIGN_RESULTS_NOT_FOUND"
    assert _read(out, "campaign_results")["run_id"] == _read(out, "ids")["run_id"]
    kinds = {alert["kind"] for alert in _read(out, "alerts_open")["alerts"]}
    assert {"performance_drop", "schedule_missed"} <= kinds

    assert _read(out, "import_refused")["imported"] is False and _read(out, "import_refused")["errors"]
    assert _read(out, "retention_plan")["plan"]["items"]
    assert _read(out, "retention_changed")["detail"]["code"] == "RETENTION_PLAN_CHANGED"
    erasure = _read(out, "erasure")
    assert erasure["status"] == "completed" and erasure["store_counts"] and erasure["models_flagged"]
    assert "access_export_ar_" in _read(out, "access_export")["disposition"]
    assert _read(out, "audit_erasure")["total"] == 1
    assert _read(out, "privacy_not_configured")["detail"]["code"] == "PRIVACY_NOT_CONFIGURED"


def test_the_privacy_and_monitoring_screens_in_jsdom(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = skip_without_jsdom(NODE_DIR)
    out = write_ops_fixtures(tmp_path, config_root, monkeypatch)
    tests = sorted(str(p) for p in OPS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
