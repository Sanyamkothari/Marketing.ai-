"""Plan M final integration (DEC-1611 (p)): a contact list made with a model that is not approved is not sent.

M181 part C lets a campaign's list be built with a new model while it still waits for an Approver (the Approver
approves it in the same click as the plan). The wizard and the campaign page hold such a list; the server now holds
it for every client, Plan J's own page and the raw routes included:

* `POST /campaigns/{id}/plan` and, for a run a campaign record names, the contact list itself
  (`GET /runs/{id}/treat_list.csv`, the generic artefact route's `treat_list.*`) answer `409 MODEL_NOT_APPROVED`
  while the run's model is `pending_approval`, or was turned down (`archived` and never the champion), and its run's
  settings require approval;
* a run no campaign names is served as before: plan.md section 11's Phase 1 journey trains, scores the second upload
  with the model it just made and downloads the list with nothing approved in between;
* nothing else changes: `scores.csv` and the other analyst files stay downloadable (DEC-050: a model named
  explicitly may be scored at any status, to try a candidate before approving it), and so does the list's summary;
  an approved model, the champion, a demoted former champion and a run whose settings do not require approval are
  served as before.

DEC-1620 (M190): an arbitrated list (Plan J M101) is built from several goals' scoring runs, and a campaign record
names only its own first run, so the hold checks every run the list was built from (the arbitration summary's
`run_ids`): the arbitrated campaign's plan and the arbitrated list are refused when any of those runs' models waits
for approval or was turned down, and the refusal names that run and its goal.

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
from engine.contracts import ModelStatus, ModelVersion, RunRecord
from engine.decide.arbitrate import ARBITRATED_TREAT_LIST_CSV, ARBITRATED_TREAT_LIST_PARQUET
from engine.decide.treat_list import TREAT_LIST_CSV, TREAT_LIST_SUMMARY_FILENAME, ensure_treat_list
from engine.measurement.campaign import InMemoryCampaignStore
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.decide.treat_runs import USE_CASE, write_run

pytestmark = pytest.mark.integration

RUN = "r_20261011_9a000001"
MODEL = f"m_{USE_CASE}_1"
LATER = f"m_{USE_CASE}_2"


def _version(model_id: str, version: int, use_case_id: str = USE_CASE) -> ModelVersion:
    run_id = f"r_20261011_9b00000{version}"
    return ModelVersion(
        model_id=model_id,
        use_case_id=use_case_id,
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

    def campaign(self) -> str:
        """Record the campaign this run's list went out as: from here on the run is a campaign's list."""
        created = self.client.post("/campaigns", json={"run_id": RUN})
        assert created.status_code == 201, created.text
        return str(created.json()["campaign"]["campaign_id"])

    def plan(self, campaign_id: str | None = None) -> Any:
        body = {
            "metric": "came back within 90 days",
            "outcome_column": self.target,
            "analysis_date": "2027-03-01",
        }
        return self.client.post(f"/campaigns/{campaign_id or self.campaign()}/plan", json=body)


def _refused(response: Any) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_APPROVED", response.text


def test_a_campaigns_list_of_a_model_waiting_for_approval_is_held_and_its_scores_are_not(
    tmp_path: Path, config_root: Path
) -> None:
    world = World(tmp_path, config_root)
    world.register()
    # No campaign names the run: plan.md section 11's Phase 1 journey (train, score, download, nothing approved in
    # between) is unchanged.
    assert world.treat_list().status_code == 200
    assert world.get(TREAT_LIST_CSV).status_code == 200
    # Once a campaign names it, the list waits for the approval on every route, and so does the campaign's plan.
    campaign_id = world.campaign()
    _refused(world.treat_list())
    _refused(world.get(TREAT_LIST_CSV))
    _refused(world.get("treat_list.parquet"))
    _refused(world.plan(campaign_id))
    assert "waiting for an Approver" in world.treat_list().json()["detail"]["message"]
    # DEC-050: trying a candidate before approving it stays possible; the list's counts stay readable.
    scores = world.client.get(f"/runs/{RUN}/scores.csv")
    assert scores.status_code == 200, scores.text
    assert world.get(TREAT_LIST_SUMMARY_FILENAME).status_code == 200

    # Approved: the same list and the same campaign's plan go through.
    world.registry.approve(MODEL, by="approver")
    assert world.treat_list().status_code == 200
    assert world.treat_list().content == world.storage.read_bytes(run_key(RUN, TREAT_LIST_CSV))
    assert world.plan(campaign_id).status_code == 201


def test_a_campaigns_list_of_a_model_that_was_turned_down_is_held(tmp_path: Path, config_root: Path) -> None:
    world = World(tmp_path, config_root)
    world.register()
    world.registry.archive(MODEL, expected_status=ModelStatus.PENDING_APPROVAL)
    campaign_id = world.campaign()
    _refused(world.treat_list())
    _refused(world.plan(campaign_id))
    assert "turned down" in world.treat_list().json()["detail"]["message"]


def test_a_former_champion_that_was_replaced_still_serves_its_list(tmp_path: Path, config_root: Path) -> None:
    world = World(tmp_path, config_root)
    world.register()
    world.registry.approve(MODEL, by="approver")
    world.register(LATER, 2)
    world.registry.approve(LATER, by="approver")
    assert world.registry.get(MODEL).status is ModelStatus.ARCHIVED, "the list's model was demoted"
    campaign_id = world.campaign()
    assert world.treat_list().status_code == 200
    assert world.plan(campaign_id).status_code == 201


def test_without_approval_required_a_waiting_model_changes_nothing(tmp_path: Path, config_root: Path) -> None:
    world = World(tmp_path, config_root, overrides={"governance.approval_required": False})
    world.register()
    campaign_id = world.campaign()
    assert world.treat_list().status_code == 200
    assert world.plan(campaign_id).status_code == 201


def test_a_model_the_registry_does_not_know_changes_nothing(tmp_path: Path, config_root: Path) -> None:
    """Runs written before the registry held their model (and the other suites' fixtures) are served as before."""
    world = World(tmp_path, config_root)
    campaign_id = world.campaign()
    assert world.treat_list().status_code == 200
    assert world.plan(campaign_id).status_code == 201


# ---------------------------------------------------------------------------
# DEC-1620 (M190): an arbitrated list is held when ANY run it was built from used a model that is not approved
# ---------------------------------------------------------------------------
OTHER_RUN = "r_20261011_9a000002"
OTHER_GOAL = "cross-sell-goal"
OTHER_MODEL = f"m_{OTHER_GOAL}_1"


class ArbitratedWorld:
    """Two goals' scoring runs arbitrated into one list (Plan J M101, `POST /decide/arbitrate`).

    The first run (the win-back goal, its model approved) gets the cycle's campaign. The second (another goal, scored
    with `OTHER_MODEL`, registered waiting for approval) wins customers of its own but holds nobody back, so no
    campaign names it: a campaign record names only its own first run, so before DEC-1620 nothing checked the second
    run's model.
    """

    def __init__(
        self, tmp_path: Path, config_root: Path, *, other_overrides: dict[str, Any] | None = None
    ) -> None:
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir()
        self.storage = LocalStorage(self.data_dir)
        self.run = write_run(self.storage, RUN, kind="propensity", rows=400)
        overrides = {"actions.control_group_fraction": 0.0} | (other_overrides or {})
        write_run(self.storage, OTHER_RUN, kind="uplift", rows=400, value=True, config_overrides=overrides)
        record = self.storage.read_model(run_key(OTHER_RUN, RUN_FILENAME), RunRecord)
        self.storage.write_model(
            run_key(OTHER_RUN, RUN_FILENAME),
            record.model_copy(
                update={
                    "use_case_id": OTHER_GOAL,
                    "use_case_name": "Cross-sell",
                    "model_version_id": OTHER_MODEL,
                }
            ),
        )
        for run_id in (RUN, OTHER_RUN):
            ensure_treat_list(self.storage, run_id)
        self.registry = LocalModelRegistry(self.data_dir / REGISTRY_FILENAME)
        self.registry.register(_version(MODEL, 1))
        self.registry.approve(MODEL, by="approver")
        self.registry.register(_version(OTHER_MODEL, 2, OTHER_GOAL))
        self.app = create_app(config_root=config_root, data_dir=self.data_dir)
        self.client = TestClient(self.app)

    def arbitrate(self) -> str:
        """Arbitrate the two runs; the campaign the cycle made (the first run's: the second holds nobody back)."""
        response = self.client.post("/decide/arbitrate", json={"run_ids": [RUN, OTHER_RUN]})
        assert response.status_code == 200, response.text
        made = {c["run_id"]: c for c in response.json()["campaigns"]}
        assert made[OTHER_RUN]["outcome"] == "skipped", made
        assert (
            response.json()["summary"]["winning_by_use_case"][OTHER_GOAL] > 0
        ), "the second run is in the list"
        return str(made[RUN]["campaign_id"])

    def plan(self, campaign_id: str) -> Any:
        body = {
            "metric": "came back within 90 days",
            "outcome_column": self.run.config.target.column,
            "analysis_date": "2027-03-01",
        }
        return self.client.post(f"/campaigns/{campaign_id}/plan", json=body)

    def arbitrated_lists(self) -> list[Any]:
        """Every route the arbitrated list is downloaded from."""
        return [
            self.client.get("/decide/arbitrated-treat-list.csv"),
            self.client.get("/decide/arbitrated-treat-list.parquet"),
            self.client.get(f"/runs/{RUN}/artefacts/{ARBITRATED_TREAT_LIST_CSV}"),
            self.client.get(f"/runs/{RUN}/artefacts/{ARBITRATED_TREAT_LIST_PARQUET}"),
            self.client.get(f"/runs/{OTHER_RUN}/artefacts/{ARBITRATED_TREAT_LIST_CSV}"),
        ]


def test_an_arbitrated_campaign_whose_second_run_used_a_waiting_model_is_held(
    tmp_path: Path, config_root: Path
) -> None:
    world = ArbitratedWorld(tmp_path, config_root)
    campaign_id = world.arbitrate()

    refused = world.plan(campaign_id)
    _refused(refused)
    message = refused.json()["detail"]["message"]
    assert OTHER_RUN in message and "Cross-sell" in message and "waiting for an Approver" in message, message
    for response in world.arbitrated_lists():
        _refused(response)
        assert OTHER_RUN in response.json()["detail"]["message"]
    # Unchanged: each run's own list (the first run's model is approved; no campaign names the second run, plan.md
    # section 11), the second run's scores (DEC-050) and the arbitration's counts.
    assert world.client.get(f"/runs/{RUN}/treat_list.csv").status_code == 200
    assert world.client.get(f"/runs/{OTHER_RUN}/treat_list.csv").status_code == 200
    assert world.client.get(f"/runs/{OTHER_RUN}/scores.csv").status_code == 200
    assert world.client.get("/decide/arbitrate").status_code == 200

    # Every model approved: the same list and the same campaign's plan go through.
    world.registry.approve(OTHER_MODEL, by="approver")
    for response in world.arbitrated_lists():
        assert response.status_code == 200, response.text
    assert world.plan(campaign_id).status_code == 201


def test_an_arbitrated_campaign_whose_second_run_used_a_turned_down_model_is_held(
    tmp_path: Path, config_root: Path
) -> None:
    world = ArbitratedWorld(tmp_path, config_root)
    world.registry.archive(OTHER_MODEL, expected_status=ModelStatus.PENDING_APPROVAL)
    campaign_id = world.arbitrate()
    _refused(world.plan(campaign_id))
    for response in world.arbitrated_lists():
        _refused(response)
        assert "turned down" in response.json()["detail"]["message"]


def test_an_arbitrated_campaign_is_served_when_every_model_is_approved(
    tmp_path: Path, config_root: Path
) -> None:
    world = ArbitratedWorld(tmp_path, config_root)
    world.registry.approve(OTHER_MODEL, by="approver")
    campaign_id = world.arbitrate()
    for response in world.arbitrated_lists():
        assert response.status_code == 200, response.text
    assert world.plan(campaign_id).status_code == 201


def test_an_arbitrated_campaign_is_served_when_the_waiting_models_run_does_not_require_approval(
    tmp_path: Path, config_root: Path
) -> None:
    world = ArbitratedWorld(tmp_path, config_root, other_overrides={"governance.approval_required": False})
    campaign_id = world.arbitrate()
    for response in world.arbitrated_lists():
        assert response.status_code == 200, response.text
    assert world.plan(campaign_id).status_code == 201


def test_an_arbitrated_list_no_campaign_names_is_served(tmp_path: Path, config_root: Path) -> None:
    """An arbitration whose runs no campaign record names is not a campaign's list (plan.md section 11): served."""
    world = ArbitratedWorld(tmp_path, config_root)
    world.arbitrate()
    world.app.state.campaign_store = InMemoryCampaignStore()  # no campaign recorded
    for response in world.arbitrated_lists():
        assert response.status_code == 200, response.text
