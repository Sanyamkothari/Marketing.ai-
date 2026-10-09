"""Plan H M81/M82 in jsdom: the four-page product, against REAL API responses where there are some.

The node tests (`tests/integration/production/ui/simple/*.test.mjs`, sharing that directory's jsdom
install as `agent/`, `ops/` and `approvals/` do) load the page as `ui/index.html` does - every
module in its script order - and check what a person now sees:

* the top bar is exactly Home, Connections, Results and Settings;
* Home is the generic journey, with no industry chooser, no client chooser and, with demo mode off,
  no sample-data chip, no "Sample data ready" tag and no demo client's name anywhere;
* Results lists every run newest first, says "No runs yet" when there is none, and shows one notice
  when models wait for approval; a finished run's results link Model health, the Schedule and its
  report;
* Settings groups the links, hides Admin while sign-in is off, and says sign-in is off;
* a use case's Setup opens on Guided setup;
* the screens left under Settings and Results (the Reports hub `#/pilot`, the data request kit
  `#/pilot/kit`) speak of one company: no Client or Industry column, and "Open the latest report"
  only once a report exists.

Everything the fake API answers with was answered by the app below with sign-in off and demo mode
off (`GET /industries`, `/use-cases/{id}`, `/models`, `/auth/me`, `/pilot/demo`, `/pilot/help`,
`/clients`, `POST /clients/default`, `/healthz`, an empty `GET /runs`, `/datasets` and the data
request). Some bodies are built here instead, because producing them for real means training models:
the run history, which is three `RunRecord`s validated through `RunListResponse` (so its shape is the
API's); the approvals list, of which the page reads only how many items there are; a champion model
validated through `ModelListResponse`; and a campaign's value before and after it is measured,
validated through `RoiView`.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import ModelListResponse, RunListResponse
from engine import __version__
from engine.pilot.roi import RoiView
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "production" / "ui"
"""The node package holding jsdom (shared with the Phase 4b and Plan G screens' tests)."""

TESTS_DIR: Final[Path] = NODE_DIR / "simple"
"""Plan H's jsdom tests."""

USE_CASE: Final[str] = "telco-churn"
"""A predictive use case of the generic journey, whose Setup offers Guided setup."""


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _write(directory: Path, name: str, body: Any) -> None:
    (directory / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def _runs() -> dict[str, Any]:
    """Three runs, newest first, in `GET /runs`'s own shape: a failure, a scoring run, a training run."""
    now = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    common = {
        "use_case_id": USE_CASE,
        "use_case_name": "Telco Customer Churn",
        "file_name": "customers.csv",
        "primary_key": "customer_id",
        "problem_type": "binary_classification",
        "model_choice": "__automl__",
        "engine_version": __version__,
    }
    runs = [
        {
            **common,
            "run_id": "r_20260928_0003",
            "mode": "train",
            "state": "failed",
            "created_at": now.isoformat(),
            "upload_id": "u3",
            "target": "churned",
            "error": {
                "code": "TARGET_SINGLE_CLASS",
                "message": "The outcome has one value only.",
                "stage": "validate",
            },
        },
        {
            **common,
            "run_id": "r_20260928_0002",
            "mode": "score",
            "state": "done",
            "created_at": (now - timedelta(hours=1)).isoformat(),
            "upload_id": "u2",
            "row_count": 1200,
            "model_version_id": "m1",
            "best_model": "Gradient boosting",
        },
        {
            **common,
            "run_id": "r_20260928_0001",
            "mode": "train",
            "state": "done",
            "created_at": (now - timedelta(hours=2)).isoformat(),
            "upload_id": "u1",
            "target": "churned",
            "row_count": 5000,
            "model_version_id": "m1",
            "best_model": "Gradient boosting",
            "headline_metric": "roc_auc",
            "headline_metric_label": "ROC AUC",
            "headline_score": 0.912,
        },
    ]
    return RunListResponse.model_validate({"runs": runs}).model_dump(mode="json")


def _champion() -> dict[str, Any]:
    """`GET /models` with one champion: the version the training run above produced, approved."""
    approved = datetime(2026, 9, 28, 8, 30, tzinfo=UTC)
    version = {
        "model_id": "m1",
        "use_case_id": USE_CASE,
        "version": 1,
        "run_id": "r_20260928_0001",
        "created_at": (approved - timedelta(minutes=10)).isoformat(),
        "status": "champion",
        "metric": "roc_auc",
        "metric_label": "ROC AUC",
        "test_score": 0.912,
        "model_display_name": "Gradient boosting",
        "schema_key": "models/m1/feature_schema.json",
        "run_config_key": "models/m1/run_config.json",
        "predictor_key": "models/m1/predictor",
        "approved_by": "analyst",
        "approved_at": approved.isoformat(),
        "engine_version": __version__,
        "autogluon_version": "1.1.1",
    }
    body = {"versions": [{"version": version, "is_champion": True}]}
    return ModelListResponse.model_validate(body).model_dump(mode="json")


def _roi(status: str) -> dict[str, Any]:
    """`GET /pilot/roi/{run_id}` for the scoring run above, measured or not yet."""
    view = {
        "run_id": "r_20260928_0002",
        "use_case_id": USE_CASE,
        "status": status,
        "source": "outcome_ingestion" if status == "measured" else None,
        "results_available_on": None,
        "causal": status == "measured",
        "outcome_name": "churned",
        "summary": "Measured." if status == "measured" else "Not measured yet.",
    }
    return RoiView.model_validate(view).model_dump(mode="json")


def write_fixtures(root: Path, config_root: Path) -> Path:
    """Every body the node tests replay; see the module docstring for which ones are the app's own."""
    out = root / "fixtures"
    out.mkdir()
    with TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        _write(out, "industries", _ok(client.get("/industries")))
        _write(out, "use_case", _ok(client.get(f"/use-cases/{USE_CASE}")))
        _write(out, "models", _ok(client.get("/models", params={"use_case": USE_CASE})))
        _write(out, "runs_empty", _ok(client.get("/runs", params={"limit": 100})))
        _write(out, "me_off", _ok(client.get("/auth/me")))
        _write(out, "demo", _ok(client.get("/pilot/demo")))
        _write(out, "help", _ok(client.get("/pilot/help")))
        _write(out, "clients_empty", _ok(client.get("/clients")))
        _write(out, "client_default", _ok(client.post("/clients/default")))
        _write(out, "clients", _ok(client.get("/clients")))
        _write(out, "healthz", _ok(client.get("/healthz")))
        _write(out, "datasets_empty", _ok(client.get("/datasets")))
        _write(out, "data_request", _ok(client.get("/pilot/data-request", params={"format": "json"})))
    _write(out, "runs", _runs())
    _write(out, "approvals", {"items": [{}, {}], "separation_enforced": False})
    _write(out, "ids", {"use_case": USE_CASE})
    _write(out, "models_champion", _champion())
    _write(out, "roi_not_measured", _roi("not_measured"))
    _write(out, "roi_measured", _roi("measured"))
    _proof_fixtures(out, root, config_root)
    return out


def _proof_fixtures(out: Path, root: Path, config_root: Path) -> None:
    """Plan J M104: a real campaign's Value Proof Pack - the list, the pack as JSON and as a page, the approval
    of its one suggestion and the pack after it - and the refusal of a campaign not measured yet. The campaign
    is a real Phase 1 scoring run's list with a planted harmful band (`tests/integration/pilot/test_proof_pack.py`),
    in a data directory of its own, so the other fixtures still describe an empty installation."""
    from engine.storage import LocalStorage
    from tests.integration.measurement.support import propensity_run
    from tests.integration.pilot.test_proof_pack import HARMED_BAND, _banded_outcomes, _measured_campaign

    data_dir = root / "proof_data"
    data_dir.mkdir()
    run = propensity_run(LocalStorage(data_dir), "r_20261009_31000009", rows=12_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        campaign_id = _measured_campaign(client, run.run_id, _banded_outcomes(run.scores, seed=21))
        _write(out, "proofs", _ok(client.get("/pilot/proof")))
        _write(out, "proof", _ok(client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"})))
        _write(out, "proof_page", {"html": client.get(f"/pilot/proof/{campaign_id}").text})
        approval = {"dimension": "band", "segment": HARMED_BAND}
        _write(
            out,
            "proof_approval",
            _ok(client.post(f"/pilot/proof/{campaign_id}/suppressions", json=approval), 201),
        )
        _write(
            out, "proof_approved", _ok(client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"}))
        )
        waiting = _ok(client.post("/campaigns", json={"run_id": run.run_id}), 201)["campaign"]["campaign_id"]
        refused = client.get(f"/pilot/proof/{waiting}", params={"format": "json"})
        assert refused.status_code == 409, refused.text
        _write(out, "proof_refused", refused.json())


def test_the_fixtures_are_the_world_the_screens_are_tested_in(tmp_path: Path, config_root: Path) -> None:
    """Runs without node: an empty start, sign-in off, demo off, and the generic journey first."""
    out = write_fixtures(tmp_path, config_root)

    def read(name: str) -> Any:
        return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))

    assert read("runs_empty")["runs"] == [], "a new installation starts empty"
    assert read("me_off")["auth_mode"] == "off"
    demo = read("demo")
    assert demo["demo_mode"] is False and demo["manifest"] is None
    industries = read("industries")
    assert industries["default_industry"] == "generic"
    assert industries["industries"][0]["id"] == "generic"
    assert read("clients_empty")["clients"] == []
    assert [c["client_id"] for c in read("clients")["clients"]] == [read("client_default")["client_id"]]
    assert [run["state"] for run in read("runs")["runs"]] == ["failed", "done", "done"]
    assert read("datasets_empty")["datasets"] == []
    assert read("data_request")["tables"], "the data request names the tables to ask for"
    assert [v["is_champion"] for v in read("models_champion")["versions"]] == [True]
    assert [read(f"roi_{s}")["status"] for s in ("not_measured", "measured")] == ["not_measured", "measured"]
    # Plan J M104: a real pack with one suggestion, approved once, and a refusal with its day
    assert [entry["status"] for entry in read("proofs")["proofs"]] == ["ready"]
    assert [p["status"] for p in read("proof")["proposals"]] == ["proposed"]
    assert [p["status"] for p in read("proof_approved")["proposals"]] == ["approved"]
    assert read("proof_refused")["detail"]["code"] == "PROOF_NOT_MATURE"


def test_the_four_page_product_in_jsdom(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "SP_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]
