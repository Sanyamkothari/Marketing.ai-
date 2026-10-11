"""Plan M final integration (DEC-1611 (p)): a contact list made with a model that is not approved is not sent.

M181 part C lets a campaign's list be built with a new model while it still waits for an Approver (the Approver
approves it in the same click as the plan). The wizard and the campaign page hold such a list; the server now holds
it for every client, Plan J's own page and the raw routes included:

* `POST /campaigns/{id}/plan` and the contact list itself (`GET /runs/{id}/treat_list.csv`, the generic artefact
  route's `treat_list.*`) answer `409 MODEL_NOT_APPROVED` while the run's model is `pending_approval`, or was turned
  down (`archived` and never the champion), and its run's settings require approval;
* nothing else changes: `scores.csv` and the other analyst files stay downloadable (DEC-050: a model named
  explicitly may be scored at any status, to try a candidate before approving it), and so does the list's summary;
  an approved model, the champion, a demoted former champion and a run whose settings do not require approval are
  served as before.

The scoring runs are written straight into the store by the treat-list fixture (`tests/fixtures/decide/treat_runs.py`),
whose `run.json` names the model `m_win-back-campaign_1`; the test registers that version at the status it needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import Metric
from engine.contracts import ModelStatus, ModelVersion
from engine.decide.treat_list import TREAT_LIST_CSV, TREAT_LIST_SUMMARY_FILENAME, ensure_treat_list
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.decide.treat_runs import USE_CASE, write_run

pytestmark = pytest.mark.integration

RUN = "r_20261011_9a000001"
MODEL = f"m_{USE_CASE}_1"
LATER = f"m_{USE_CASE}_2"


def _version(model_id: str, version: int) -> ModelVersion:
    run_id = f"r_20261011_9b00000{version}"
    return ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=version,
        run_id=run_id,
        created_at=utc_now(),
        status=ModelStatus.PENDING_APPROVAL,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8,
        model_display_name="LightGBM",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0",
        autogluon_version="0",
    )


class World:
    def __init__(self, tmp_path: Path, config_root: Path, *, overrides: dict[str, Any] | None = None) -> None:
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir()
        self.storage = LocalStorage(self.data_dir)
        self.run = write_run(self.storage, RUN, kind="propensity", rows=400, config_overrides=overrides)
        ensure_treat_list(self.storage, RUN)
        self.registry = LocalModelRegistry(self.data_dir / REGISTRY_FILENAME)
        self.client = TestClient(create_app(config_root=config_root, data_dir=self.data_dir))
        self.target = self.run.config.target.column

    def register(self, model_id: str = MODEL, version: int = 1) -> None:
        self.registry.register(_version(model_id, version))

    def get(self, name: str) -> Any:
        return self.client.get(f"/runs/{RUN}/artefacts/{name}")

    def treat_list(self) -> Any:
        return self.client.get(f"/runs/{RUN}/treat_list.csv")

    def plan(self) -> Any:
        created = self.client.post("/campaigns", json={"run_id": RUN})
        assert created.status_code == 201, created.text
        campaign_id = created.json()["campaign"]["campaign_id"]
        body = {
            "metric": "came back within 90 days",
            "outcome_column": self.target,
            "analysis_date": "2027-03-01",
        }
        return self.client.post(f"/campaigns/{campaign_id}/plan", json=body)


def _refused(response: Any) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_APPROVED", response.text


def test_a_list_of_a_model_waiting_for_approval_is_held_and_its_scores_are_not(
    tmp_path: Path, config_root: Path
) -> None:
    world = World(tmp_path, config_root)
    world.register()
    _refused(world.treat_list())
    _refused(world.get(TREAT_LIST_CSV))
    _refused(world.get("treat_list.parquet"))
    _refused(world.plan())
    assert "waiting for an Approver" in world.treat_list().json()["detail"]["message"]
    # DEC-050: trying a candidate before approving it stays possible; the list's counts stay readable.
    scores = world.client.get(f"/runs/{RUN}/scores.csv")
    assert scores.status_code == 200, scores.text
    assert world.get(TREAT_LIST_SUMMARY_FILENAME).status_code == 200

    # Approved: the same list and the same plan go through.
    world.registry.approve(MODEL, by="approver")
    assert world.treat_list().status_code == 200
    assert world.treat_list().content == world.storage.read_bytes(run_key(RUN, TREAT_LIST_CSV))
    assert world.plan().status_code == 201


def test_a_list_of_a_model_that_was_turned_down_is_held(tmp_path: Path, config_root: Path) -> None:
    world = World(tmp_path, config_root)
    world.register()
    world.registry.archive(MODEL, expected_status=ModelStatus.PENDING_APPROVAL)
    _refused(world.treat_list())
    _refused(world.plan())
    assert "turned down" in world.treat_list().json()["detail"]["message"]


def test_a_former_champion_that_was_replaced_still_serves_its_list(tmp_path: Path, config_root: Path) -> None:
    world = World(tmp_path, config_root)
    world.register()
    world.registry.approve(MODEL, by="approver")
    world.register(LATER, 2)
    world.registry.approve(LATER, by="approver")
    assert world.registry.get(MODEL).status is ModelStatus.ARCHIVED, "the list's model was demoted"
    assert world.treat_list().status_code == 200
    assert world.plan().status_code == 201


def test_without_approval_required_a_waiting_model_changes_nothing(tmp_path: Path, config_root: Path) -> None:
    world = World(tmp_path, config_root, overrides={"governance.approval_required": False})
    world.register()
    assert world.treat_list().status_code == 200
    assert world.plan().status_code == 201


def test_a_model_the_registry_does_not_know_changes_nothing(tmp_path: Path, config_root: Path) -> None:
    """Runs written before the registry held their model (and the other suites' fixtures) are served as before."""
    world = World(tmp_path, config_root)
    assert world.treat_list().status_code == 200
    assert world.plan().status_code == 201
