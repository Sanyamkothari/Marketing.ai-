"""The Approver's screen's API (Plan D M54, DEC-862, DEC-864): the head-to-head, separation of duties,
approving and rejecting with a reason, and who is refused.

A champion and a challenger are registered in the app's own registry file; the challenger's training
run gets the three artefacts the train flow writes and this feature reads - `run.json` (who started
it), `evaluation.json` (the challenger on its test split) and `run_manifest.json` (the champion
re-scored on that same split, `champion_<metric>`). Nothing is trained: the numbers are what a train
flow would have recorded, and the assertions are about what the API does with them.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.approvals import SEPARATION_MESSAGE, decisions_for
from engine.audit.events import AuditQuery
from engine.config import Metric
from engine.contracts import (
    CostEstimate,
    DatasetFingerprint,
    EvaluationReport,
    MetricValue,
    ModelStatus,
    ModelVersion,
    ProblemType,
    RunManifest,
    RunMode,
    RunRecord,
    RunState,
    ThresholdMode,
)
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
CHAMPION, CHALLENGER = "m_champ", "m_chall"
CHALLENGER_RUN = "r_20260920_chall"

CHALLENGER_METRICS = {Metric.ROC_AUC: 0.8123, Metric.PR_AUC: 0.4100, Metric.F1: 0.4000}
CHAMPION_METRICS = {Metric.ROC_AUC: 0.7801, Metric.PR_AUC: 0.4300, Metric.F1: 0.4000}
GREATER_IS_BETTER = {Metric.ROC_AUC: True, Metric.PR_AUC: True, Metric.F1: True}


def version(
    model_id: str, number: int, *, run_id: str, status: ModelStatus, measured: str | None = None
) -> ModelVersion:
    return ModelVersion(
        model_id=model_id,
        use_case_id=USE_CASE,
        version=number,
        run_id=run_id,
        created_at=utc_now() + timedelta(minutes=number),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=CHALLENGER_METRICS[Metric.ROC_AUC] if model_id == CHALLENGER else 0.79,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        improvement_pct=4.13 if model_id == CHALLENGER else None,
        measured_against_champion_id=measured,
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


def write_run(storage: LocalStorage, run_id: str, requested_by: str | None, *, champion: bool = True) -> None:
    now = utc_now()
    storage.write_model(
        run_key(run_id, "run.json"),
        RunRecord(
            run_id=run_id,
            use_case_id=USE_CASE,
            use_case_name="Targeted Advertisement",
            mode=RunMode.TRAIN,
            state=RunState.DONE,
            created_at=now,
            upload_id="u_1",
            file_name="train.csv",
            primary_key="entity_key",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            model_choice="automl",
            engine_version="0.1.0",
            requested_by=requested_by,
        ),
    )
    storage.write_model(
        run_key(run_id, "evaluation.json"),
        EvaluationReport(
            run_id=run_id,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            rows_evaluated=1200,
            positive_rate=0.2,
            primary_metric=Metric.ROC_AUC,
            primary_metric_label="ROC-AUC",
            headline_score=CHALLENGER_METRICS[Metric.ROC_AUC],
            metrics=tuple(
                MetricValue(
                    id=metric, label=metric.value, value=value, greater_is_better=GREATER_IS_BETTER[metric]
                )
                for metric, value in CHALLENGER_METRICS.items()
            ),
            threshold=0.5,
            threshold_mode=ThresholdMode.FIXED,
            threshold_detail="fixed at 0.5",
            calibration=None,
            evaluated_at=now,
        ),
    )
    storage.write_model(
        run_key(run_id, "run_manifest.json"),
        RunManifest(
            run_id=run_id,
            primary_key="entity_key",
            dataset_fingerprint=DatasetFingerprint(
                hash="0" * 64, algorithm="sha256", n_rows=6000, columns=()
            ),
            seed=1,
            duration_s=1.0,
            cost_estimate=CostEstimate(compute_seconds=1.0, basis="local run, nothing billed"),
            created_at=now,
            metrics={f"champion_{m.value}": v for m, v in CHAMPION_METRICS.items()} if champion else {},
        ),
    )


class World:
    def __init__(self, root: Path, **settings: Any) -> None:
        self.root = root
        self.app: FastAPI = local_app(root, **settings)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.trainer_id = make_user(self.app, "trainer", [Role.ANALYST, Role.APPROVER])
        self.trainer = bearer(self.app, self.trainer_id)
        self.approver_id = make_user(self.app, "approver", [Role.APPROVER])
        self.approver = bearer(self.app, self.approver_id)
        self.analyst = bearer(self.app, make_user(self.app, "analyst", [Role.ANALYST]))
        self.viewer = bearer(self.app, make_user(self.app, "viewer", [Role.VIEWER]))
        self.registry = LocalModelRegistry(root / REGISTRY_FILENAME)
        self.storage = LocalStorage(root)
        self.registry.register(version(CHAMPION, 1, run_id="r_20260901_champ", status=ModelStatus.CANDIDATE))
        self.registry.promote(CHAMPION, by="tests", note="the incumbent")
        self.registry.register(
            version(
                CHALLENGER, 2, run_id=CHALLENGER_RUN, status=ModelStatus.PENDING_APPROVAL, measured=CHAMPION
            )
        )
        write_run(self.storage, CHALLENGER_RUN, requested_by=self.trainer_id)

    def approvals(self, headers: dict[str, str]) -> dict[str, Any]:
        response = self.client.get("/approvals", headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def events(self, action: str) -> list[Any]:
        return list(audit_log_at(self.root).query(AuditQuery(action=action)))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_the_head_to_head_is_both_models_on_the_same_rows_with_the_differences(world: World) -> None:
    body = world.approvals(world.approver)
    assert body["separation_enforced"] is True
    (item,) = body["items"]
    assert item["version"]["model_id"] == CHALLENGER and item["trained_by"] == world.trainer_id
    h2h = item["head_to_head"]
    assert (h2h["measured_against_champion_id"], h2h["current_champion_id"], h2h["stale"]) == (
        CHAMPION,
        CHAMPION,
        False,
    )
    assert (h2h["rows_evaluated"], h2h["improvement_pct"], h2h["note"]) == (1200, 4.13, None)
    rows = {row["metric"]: row for row in h2h["metrics"]}
    assert set(rows) == {m.value for m in CHALLENGER_METRICS}
    roc = rows["roc_auc"]
    assert (roc["primary"], roc["challenger"], roc["champion"], roc["better"]) == (
        True,
        0.8123,
        0.7801,
        "challenger",
    )
    assert roc["difference"] == pytest.approx(0.0322)
    assert rows["pr_auc"]["better"] == "champion", "a metric the challenger is worse on is called out"
    assert rows["f1"]["better"] == "equal"
    assert (item["can_decide"], item["blocked_reason"]) == (True, None)


def test_the_trainer_sees_why_they_cannot_decide_and_is_refused_by_the_server(world: World) -> None:
    (item,) = world.approvals(world.trainer)["items"]
    assert (item["can_decide"], item["blocked_reason"]) == (False, SEPARATION_MESSAGE)
    for path, body in (
        (f"/models/{CHALLENGER}/approve", {"approved_by": "trainer", "reason": "looks good"}),
        (f"/models/{CHALLENGER}/promote", {"promoted_by": "trainer", "reason": "ship it"}),
    ):
        response = world.client.post(path, json=body, headers=world.trainer)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == {
            "code": "SEPARATION_OF_DUTIES",
            "message": SEPARATION_MESSAGE,
            "path": None,
        }
    assert world.registry.get(CHALLENGER).status is ModelStatus.PENDING_APPROVAL
    (event,) = world.events("models.approve")
    assert (event.outcome, event.actor_id, event.details["reason_code"]) == (
        "denied",
        world.trainer_id,
        "SEPARATION_OF_DUTIES",
    )


def test_another_approver_approves_with_a_reason_and_the_record_names_them(world: World) -> None:
    response = world.client.post(
        f"/models/{CHALLENGER}/approve",
        json={"approved_by": "somebody else entirely", "reason": "beats the champion on ROC-AUC"},
        headers=world.approver,
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_champion"] is True
    stored = world.registry.get(CHALLENGER)
    assert (stored.status, stored.approved_by) == (
        ModelStatus.CHAMPION,
        "approver",
    ), "the signed-in person, not the body"
    (decision,) = decisions_for(sqlite_engine(world.root / PLATFORM_DB_FILENAME), [CHALLENGER])[CHALLENGER]
    assert (decision.decision, decision.decided_by, decision.reason, decision.champion_id) == (
        "approved",
        world.approver_id,
        "beats the champion on ROC-AUC",
        CHAMPION,
    )
    assert world.approvals(world.approver)["items"] == [], "nothing is waiting any more"
    (event,) = world.events("models.approve")
    assert (event.outcome, event.actor_id, event.object_id) == ("success", world.approver_id, CHALLENGER)


def test_a_rejection_needs_a_reason_archives_the_challenger_and_is_recorded(world: World) -> None:
    missing = world.client.post(f"/models/{CHALLENGER}/reject", json={}, headers=world.approver)
    assert missing.status_code == 422
    response = world.client.post(
        f"/models/{CHALLENGER}/reject",
        json={"reason": "worse on PR-AUC, the metric sales watches"},
        headers=world.approver,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["version"]["status"], body["is_champion"]) == ("archived", False)
    assert (body["decision"]["decision"], body["decision"]["decided_by"]) == ("rejected", world.approver_id)
    assert world.registry.get_champion(USE_CASE).model_id == CHAMPION, "the champion is untouched"
    again = world.client.post(
        f"/models/{CHALLENGER}/reject", json={"reason": "again please"}, headers=world.approver
    )
    assert again.status_code == 409 and again.json()["detail"]["code"] == "INVALID_TRANSITION"
    champion = world.client.post(
        f"/models/{CHAMPION}/reject", json={"reason": "no champion"}, headers=world.approver
    )
    assert champion.status_code == 409, "a champion is replaced, never rejected"
    (event,) = [e for e in world.events("models.reject") if e.outcome == "success"]
    assert (event.object_id, event.actor_id) == (CHALLENGER, world.approver_id)


class ApprovedMeanwhile(LocalModelRegistry):
    """A registry on which another Approver approves `CHALLENGER` just after the reject route read it.

    The route reads the version (`pending_approval`), then the champion, then archives. The approval
    is slipped in at the champion read - the window a concurrent `POST /approve` has in production.
    """

    def get_champion(self, use_case_id: str) -> ModelVersion | None:
        champion = super().get_champion(use_case_id)
        if self.get(CHALLENGER).status is ModelStatus.PENDING_APPROVAL:
            self.approve(CHALLENGER, by="another approver")
        return champion


def test_a_reject_that_loses_the_race_to_an_approval_leaves_the_new_champion_alone(world: World) -> None:
    """DEC-873: the status is checked again under the registry's lock; the champion is not archived."""
    world.app.state.registry = ApprovedMeanwhile(world.root / REGISTRY_FILENAME)
    response = world.client.post(
        f"/models/{CHALLENGER}/reject", json={"reason": "worse on PR-AUC"}, headers=world.approver
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "INVALID_TRANSITION"
    champion = world.registry.get_champion(USE_CASE)
    assert champion is not None and champion.model_id == CHALLENGER, "the use case still has a champion"
    assert decisions_for(sqlite_engine(world.root / PLATFORM_DB_FILENAME), [CHALLENGER]) in (
        {},
        {CHALLENGER: []},
    ), "no rejection is recorded for a model that was not rejected"
    (event,) = world.events("models.reject")
    assert (event.outcome, event.details["reason_code"]) == ("failed", "INVALID_TRANSITION")


def test_the_trainer_may_withdraw_their_own_challenger(world: World) -> None:
    response = world.client.post(
        f"/models/{CHALLENGER}/reject", json={"reason": "I found a leak"}, headers=world.trainer
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("method", "path", "body", "who", "message"),
    [
        (
            "POST",
            f"/models/{CHALLENGER}/reject",
            {"reason": "not mine to say"},
            "analyst",
            "Only an Approver can reject a challenger.",
        ),
        (
            "POST",
            f"/models/{CHALLENGER}/reject",
            {"reason": "not mine to say"},
            "viewer",
            "Only an Approver can reject a challenger.",
        ),
        (
            "POST",
            f"/models/{CHALLENGER}/approve",
            {"approved_by": "x"},
            "analyst",
            "Only an Approver can approve a champion.",
        ),
    ],
)
def test_a_role_without_approver_is_refused(
    world: World, method: str, path: str, body: dict[str, str], who: str, message: str
) -> None:
    response = world.client.request(method, path, json=body, headers=getattr(world, who))
    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "ROLE_REQUIRED", "message": message, "path": None}
    assert world.registry.get(CHALLENGER).status is ModelStatus.PENDING_APPROVAL


def test_a_viewer_reads_the_list_but_is_told_they_cannot_decide(world: World) -> None:
    (item,) = world.approvals(world.viewer)["items"]
    assert (item["can_decide"], item["blocked_reason"]) == (False, "Only an Approver can approve a champion.")
    anonymous = world.client.get("/approvals")
    assert anonymous.status_code == 401


def test_a_stale_comparison_and_an_unrecorded_trainer_are_said_plainly(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.registry.register(version("m_newer", 3, run_id="r_20260921_newer", status=ModelStatus.CANDIDATE))
    world.registry.promote("m_newer", by="tests", note="another champion meanwhile")
    write_run(world.storage, CHALLENGER_RUN, requested_by=None, champion=False)
    (item,) = world.approvals(world.approver)["items"]
    h2h = item["head_to_head"]
    assert (h2h["stale"], h2h["current_champion_id"]) == (True, "m_newer")
    assert ("not recorded" in h2h["note"] and "stale" not in h2h["note"]) or "changed" in h2h["note"]
    assert all(
        row["champion"] is None and row["better"] is None for row in h2h["metrics"]
    ), "nothing estimated"
    assert (item["trained_by"], item["can_decide"]) == (None, True)
    refused = world.client.post(
        f"/models/{CHALLENGER}/approve", json={"approved_by": "x", "reason": "stale"}, headers=world.approver
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "CHAMPION_CHANGED"


def test_with_sign_in_off_the_screen_says_separation_is_not_enforced(tmp_path: Path) -> None:
    world = World(tmp_path, auth_mode="off")
    body = world.approvals({})
    assert body["separation_enforced"] is False
    (item,) = body["items"]
    assert item["can_decide"] is True
