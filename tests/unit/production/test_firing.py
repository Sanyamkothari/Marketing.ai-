"""What a firing does: score, retrain, check drift - through the engine's own run path (DEC-766..768).

Every test builds real datasets from a real client store with the real `build_dataset`, and every
run is created by the same engine calls `POST /runs` makes. The one thing stood in for is the *job*:
`RecordingJobs` records what was submitted and runs nothing, so no model is fitted here, and a run's
ending is written with `engine.runs.update_run`/`fail_run` - the same functions a job body ends
with. The register stage's decision on a retrain is exercised with the stage's own
`build_model_version`, on the configuration the firing wrote. The end-to-end version that trains
for real is `tests/integration/production/test_scheduling_flow.py` (slow).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.access.roles import SYSTEM_SCHEDULER
from engine.audit.events import DETAIL_KEYS
from engine.config import ResolvedConfig, RunMode, load_use_case
from engine.contracts import (
    BestModel,
    DriftReport,
    DriftStatus,
    FeatureDrift,
    JobSpec,
    ModelStatus,
    RunError,
    RunRecord,
    RunState,
)
from engine.jobs import CancelToken
from engine.onboarding.datasets import dataset_key
from engine.onboarding.specs import DatasetManifest
from engine.pipeline import StageContext
from engine.runs import fail_run, update_run
from engine.scheduling.alerts import AlertKind, AlertQuery
from engine.scheduling.firing import (
    DriftCheckReport,
    ScheduleFirer,
    build_dataset_from_spec,
    latest_recipe_inputs,
)
from engine.scheduling.schedules import FiringStatus, ScheduleKind, ScheduleParameters
from engine.stages.register import ChampionScore, build_model_version, feature_schema, store_model_version
from engine.storage import run_key
from tests.unit.production.scheduling_support import (
    USE_CASE,
    MemoryFlags,
    World,
    make_world,
    register_champion,
    tables,
)


@pytest.fixture
def world(tmp_path: Path, config_root: Path) -> World:
    return make_world(tmp_path, config_root)


def training_frame(world: World) -> pd.DataFrame:
    config = load_use_case(USE_CASE)
    manifest = build_dataset_from_spec(
        world.spec, config=config, mode=RunMode.TRAIN, client_store=world.client_store, storage=world.storage
    )
    return pd.read_parquet(world.storage.local_path(dataset_key(manifest.dataset_id, "dataset.parquet")))


def run_record(world: World, run_id: str) -> RunRecord:
    return world.storage.read_model(run_key(run_id, "run.json"), RunRecord)


def manifest_of(world: World, dataset_id: str) -> DatasetManifest:
    return world.storage.read_model(dataset_key(dataset_id, "dataset_manifest.json"), DatasetManifest)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
def test_a_scheduled_score_rebuilds_the_recipe_and_starts_a_run_against_the_champion(world: World) -> None:
    champion = register_champion(world, training_frame(world))
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert firing is not None
    assert firing.status is FiringStatus.RUNNING
    assert firing.result_code == "SCORING_STARTED"
    assert firing.run_id is not None and firing.dataset_id is not None
    assert world.jobs.submitted == [firing.run_id]

    record = run_record(world, firing.run_id)
    assert record.mode is RunMode.SCORE
    assert record.state is RunState.PENDING
    assert record.dataset_id == firing.dataset_id
    assert record.client_id == world.client_id
    assert (
        record.model_version_id == champion.model_id
    ), "the champion is pinned on run.json, as POST /runs does"
    spec = world.storage.read_model(run_key(firing.run_id, "job_spec.json"), JobSpec)
    assert spec.entrypoint.value == "score"
    manifest = manifest_of(world, firing.dataset_id)
    assert manifest.target is None, "a scoring build derives no label"
    assert world.client_store.get_dataset(firing.dataset_id).dataset_id == firing.dataset_id


def test_every_firing_writes_one_audit_event_as_the_scheduler_with_ids_only(world: World) -> None:
    register_champion(world, training_frame(world))
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert firing is not None
    (event,) = world.audit.events
    assert event.actor_id == SYSTEM_SCHEDULER.user_id
    assert event.actor_kind == "system"
    assert event.action == "schedules.fire"
    assert (event.object_type, event.object_id) == ("schedule", "sch_score")
    assert set(event.details) <= DETAIL_KEYS
    assert event.details["run_id"] == firing.run_id
    assert event.details["trigger"] == "manual"
    dumped = event.model_dump_json()
    assert "C000" not in dumped, "no customer id reaches the audit log"


def test_a_firing_started_through_the_api_writes_no_event_of_its_own(world: World) -> None:
    """The API's middleware writes the one event that request is allowed (M47)."""
    ScheduleFirer(world.services(audit_log=None)).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert world.audit.events == []


def test_the_recipe_reads_the_newest_table_of_each_role(world: World) -> None:
    later = datetime(2026, 7, 2, tzinfo=UTC)
    fresh = tables(seed=7, end=datetime(2026, 7, 1).date())["activity"]
    world.add_source("activity_july", fresh, role="activity", created_at=later)
    world.add_source(
        "activity_narrow", fresh[["CUST_ID"]], role="activity", created_at=later + timedelta(days=1)
    )
    inputs = latest_recipe_inputs(world.client_store, world.spec)
    assert inputs.rebound == ("s_activity",)
    assert inputs.spec.event_source_ids == ("s_activity_july",), "a table missing a mapped column is not used"
    assert inputs.spec.entity_source_id == "s_customers"
    (activity,) = [mapping for mapping in inputs.mappings if mapping.role == "activity"]
    assert activity.source_id == "s_activity_july"
    assert activity.mapping_id == "m_s_activity", "the saved mapping, re-pointed"

    register_champion(world, training_frame(world))
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert firing is not None and firing.dataset_id is not None
    assert set(manifest_of(world, firing.dataset_id).source_fingerprints) == {
        "s_customers",
        "s_activity_july",
    }


def test_settle_moves_a_running_firing_from_its_run(world: World) -> None:
    register_champion(world, training_frame(world))
    firer = ScheduleFirer(world.services())
    schedule = world.schedule(ScheduleKind.SCORE)
    first = firer.fire(schedule)
    assert first is not None and first.run_id is not None
    assert firer.settle() == (), "a pending run leaves the firing running"
    update_run(world.storage, first.run_id, state=RunState.DONE)
    (settled,) = firer.settle()
    assert settled.status is FiringStatus.SUCCEEDED
    assert settled.result_code == "SCORED"
    assert settled.finished_at is not None

    second = firer.fire(schedule)
    assert second is not None and second.run_id is not None
    fail_run(world.storage, second.run_id, RunError(code="MODEL_NOT_LOADABLE", message="x", stage=None))
    (failed,) = firer.settle()
    assert failed.status is FiringStatus.FAILED
    assert failed.error_code == "MODEL_NOT_LOADABLE"
    (alert,) = world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULED_JOB_FAILED))
    assert alert.run_id == second.run_id
    assert "did not finish" in alert.message


def test_a_score_with_no_champion_fails_with_the_engine_code_and_alerts(world: World) -> None:
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert firing is not None
    assert firing.status is FiringStatus.FAILED
    assert firing.error_code == "CHAMPION_NOT_FOUND"
    assert world.jobs.submitted == []
    (alert,) = world.alerts.store.query(AlertQuery())
    assert alert.kind is AlertKind.SCHEDULED_JOB_FAILED
    assert alert.severity == "critical"
    (event,) = world.audit.events
    assert event.outcome == "failed"
    assert event.details["reason_code"] == "CHAMPION_NOT_FOUND"


def test_a_fixed_dataset_is_refused_with_the_code_post_runs_uses(world: World) -> None:
    register_champion(world, training_frame(world))
    schedule = world.schedule(
        ScheduleKind.SCORE, schedule_id="sch_fixed", parameters=ScheduleParameters(dataset_id="ds_nothing")
    )
    firing = ScheduleFirer(world.services()).fire(schedule)
    assert firing is not None and firing.error_code == "DATASET_NOT_FOUND"


def test_one_slot_fires_once_even_when_asked_twice(world: World) -> None:
    firer = ScheduleFirer(world.services())
    schedule = world.schedule(ScheduleKind.DRIFT_CHECK)
    slot = datetime(2026, 9, 1, 20, 30, tzinfo=UTC)
    assert firer.fire_scheduled(schedule.schedule_id, scheduled_for=slot) is not None
    assert firer.fire_scheduled(schedule.schedule_id, scheduled_for=slot) is None
    assert world.store.get(schedule.schedule_id).next_due_at == slot + timedelta(days=1)


# ---------------------------------------------------------------------------
# retrain
# ---------------------------------------------------------------------------
class NoApprovals:
    """A registry wrapper that fails the test if anything approves or promotes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in ("approve", "promote"):
            raise AssertionError(f"a scheduled firing called registry.{name}")
        return getattr(self._inner, name)


def test_a_retrain_starts_a_normal_training_run_and_records_the_flags_it_answers(world: World) -> None:
    world.flags = MemoryFlags(f"m_{USE_CASE}_1", "m_fault-prediction_3")
    firing = ScheduleFirer(world.services(registry=NoApprovals(world.registry))).fire(
        world.schedule(ScheduleKind.RETRAIN)
    )
    assert firing is not None and firing.run_id is not None
    assert firing.status is FiringStatus.RUNNING
    assert firing.result_code == "TRAINING_STARTED"
    assert firing.flagged_models == (f"m_{USE_CASE}_1",), "only this use case's flags"
    record = run_record(world, firing.run_id)
    assert record.mode is RunMode.TRAIN
    assert record.target == "churn_next_60d"
    assert record.model_version_id is None
    assert manifest_of(world, record.dataset_id or "").spec_id == world.spec.spec_id
    assert world.flags.cleared == [], "a flag is not cleared when a retrain merely starts"


def _register_as_the_train_flow_would(world: World, run_id: str, *, beat_champion: bool) -> str:
    """The register stage's own decision, on the run and the configuration the firing wrote."""
    resolved = world.storage.read_model(run_key(run_id, "run_config.json"), ResolvedConfig)
    ctx = StageContext(
        run_id=run_id,
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=world.storage,
        registry=world.registry,
        cancel=CancelToken(),
        primary_key="entity_key",
        target="churn_next_60d",
        upload_key="unused",
        model_version_id=None,
    )
    model_id = f"m_{USE_CASE}_{world.registry.next_version(USE_CASE)}"
    metric = resolved.config.model_search.metric
    best = BestModel(
        run_id=run_id,
        model_name="WeightedEnsemble_L2",
        family=None,
        is_ensemble=True,
        display_name="Ensemble",
        metric=metric,
        metric_label="ROC AUC",
        validation_score=0.9,
        test_score=0.95,
        hyperparameters_summary="-",
        training_rows=100,
        feature_count=5,
        fit_time_seconds=1.0,
        predictor_key=run_key(run_id, "model"),
        trained_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    frame = training_frame(world)
    schema = feature_schema(
        frame, resolved.config, primary_key="entity_key", target="churn_next_60d", model_version_id=model_id
    )
    champion = world.registry.get_champion(USE_CASE)
    score = None
    if champion is not None:
        score = ChampionScore(
            model_id=champion.model_id, metric=metric, test_score=0.5 if beat_champion else 0.99
        )
    version = store_model_version(
        world.registry, build_model_version(ctx, best, schema, champion_score=score)
    )
    update_run(world.storage, run_id, state=RunState.DONE, model_version_id=version.model_id)
    return version.model_id


@pytest.mark.parametrize("with_champion", [False, True])
def test_retraining_yields_a_challenger_never_an_automatic_champion(
    world: World, with_champion: bool
) -> None:
    """governance.approval_required is on for this use case: the challenger waits for an Approver."""
    champion = register_champion(world, training_frame(world)) if with_champion else None
    world.flags = MemoryFlags(f"m_{USE_CASE}_1")
    firer = ScheduleFirer(world.services(registry=NoApprovals(world.registry)))
    firing = firer.fire(world.schedule(ScheduleKind.RETRAIN))
    assert firing is not None and firing.run_id is not None
    resolved = world.storage.read_model(run_key(firing.run_id, "run_config.json"), ResolvedConfig)
    assert resolved.config.governance.approval_required is True

    new_id = _register_as_the_train_flow_would(world, firing.run_id, beat_champion=True)
    challenger = world.registry.get(new_id)
    assert challenger.status is ModelStatus.PENDING_APPROVAL
    current = world.registry.get_champion(USE_CASE)
    assert (current.model_id if current else None) == (champion.model_id if champion else None)

    (settled,) = firer.settle()
    assert settled.status is FiringStatus.SUCCEEDED
    assert settled.result_code == "MODEL_PENDING_APPROVAL"
    assert world.flags.cleared == [f"m_{USE_CASE}_1"], "cleared once the retrain registered a version"


def test_a_failed_retrain_keeps_its_flags(world: World) -> None:
    world.flags = MemoryFlags(f"m_{USE_CASE}_1")
    firer = ScheduleFirer(world.services())
    firing = firer.fire(world.schedule(ScheduleKind.RETRAIN))
    assert firing is not None and firing.run_id is not None
    fail_run(world.storage, firing.run_id, RunError(code="TRAINING_FAILED", message="x", stage=None))
    (settled,) = firer.settle()
    assert settled.status is FiringStatus.FAILED
    assert world.flags.cleared == []
    assert world.flags.flagged() == (f"m_{USE_CASE}_1",)


def test_a_retrain_prefers_the_recipe_the_champion_was_trained_from(world: World) -> None:
    firer = ScheduleFirer(world.services())
    first = firer.fire(world.schedule(ScheduleKind.RETRAIN, schedule_id="sch_first"))
    assert first is not None and first.run_id is not None
    _register_as_the_train_flow_would(world, first.run_id, beat_champion=True)
    world.registry.approve(f"m_{USE_CASE}_1", by="an approver")  # a person, not the scheduler
    newer = world.spec.model_copy(
        update={"spec_id": "sp_newer", "created_at": datetime(2026, 8, 1, tzinfo=UTC)}
    )
    world.client_store.save_spec(newer.with_hash())
    second = firer.fire(world.schedule(ScheduleKind.RETRAIN, schedule_id="sch_second"))
    assert second is not None and second.dataset_id is not None
    assert manifest_of(world, second.dataset_id).spec_id == world.spec.spec_id


def test_a_retrain_with_no_recipe_anywhere_fails_by_name(world: World) -> None:
    other = world.client_store.create_client("Empty Co", "telecom")
    firing = ScheduleFirer(world.services()).fire(
        world.schedule(ScheduleKind.RETRAIN, schedule_id="sch_empty", client_id=other.client_id)
    )
    assert firing is not None
    assert firing.error_code == "NO_TRAINING_DATA"


# ---------------------------------------------------------------------------
# drift_check
# ---------------------------------------------------------------------------
def _scored(world: World, *, drifted: bool) -> tuple[str, str]:
    """A finished scoring run of the champion with a drift.json saying drifted or stable."""
    champion = register_champion(world, training_frame(world))
    firing = ScheduleFirer(world.services()).fire(
        world.schedule(ScheduleKind.SCORE, schedule_id="sch_scorer")
    )
    assert firing is not None and firing.run_id is not None
    psi = 0.5 if drifted else 0.01
    status = DriftStatus.DRIFTED if drifted else DriftStatus.STABLE
    world.storage.write_model(
        run_key(firing.run_id, "drift.json"),
        DriftReport(
            run_id=firing.run_id,
            baseline_run_id=champion.run_id,
            model_version_id=champion.model_id,
            threshold=0.2,
            features=(
                FeatureDrift(
                    feature="events_30d", psi=psi, status=status, null_rate_baseline=0, null_rate_current=0
                ),
            ),
            max_psi=psi,
            drifted_features=("events_30d",) if drifted else (),
            status=status,
            summary=f"PSI {psi:.2f}, {status.value}",
            computed_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )
    update_run(world.storage, firing.run_id, state=RunState.DONE)
    world.jobs.submitted.clear()
    return firing.run_id, champion.model_id


def test_a_drift_check_with_nothing_to_check_succeeds_and_says_why(world: World) -> None:
    firer = ScheduleFirer(world.services())
    firing = firer.fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None and (firing.status, firing.result_code) == (
        FiringStatus.SUCCEEDED,
        "NO_CHAMPION",
    )
    register_champion(world, training_frame(world))
    again = firer.fire(world.store.get("sch_drift_check"))
    assert again is not None and again.result_code == "NO_SCORED_DATA"


def test_stable_data_raises_nothing(world: World) -> None:
    run_id, _ = _scored(world, drifted=False)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None
    assert (firing.status, firing.result_code) == (FiringStatus.SUCCEEDED, "DRIFT_OK")
    assert world.alerts.store.query(AlertQuery()) == ()
    report = world.storage.read_model(
        run_key(run_id, f"drift_checks/{firing.firing_id}.json"), DriftCheckReport
    )
    assert report.reused_run_drift is True
    assert report.above_threshold is False


def test_drift_above_the_threshold_alerts_and_retrains_under_on_drift(world: World) -> None:
    run_id, champion_id = _scored(world, drifted=True)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None and firing.run_id is not None
    assert firing.status is FiringStatus.RUNNING
    assert firing.result_code == "DRIFT_ABOVE_THRESHOLD"
    assert run_record(world, firing.run_id).mode is RunMode.TRAIN
    assert world.jobs.submitted == [firing.run_id]
    (alert,) = world.alerts.store.query(AlertQuery())
    assert alert.kind is AlertKind.DRIFT_ABOVE_THRESHOLD
    assert (alert.run_id, alert.model_id) == (run_id, champion_id)
    assert "1 of 1 features" in alert.message
    assert "0.5" not in alert.message, "no statistic or value in the message"
    report = world.storage.read_model(
        run_key(run_id, f"drift_checks/{firing.firing_id}.json"), DriftCheckReport
    )
    assert report.retrain_run_id == firing.run_id


def test_an_erasure_flag_retrains_at_the_drift_cycle_even_without_drift(world: World) -> None:
    _, champion_id = _scored(world, drifted=False)
    world.flags = MemoryFlags(champion_id)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None
    assert firing.result_code == "RETRAIN_FOR_ERASURE"
    assert firing.flagged_models == (champion_id,)
    assert world.alerts.store.query(AlertQuery()) == ()


def test_drift_under_manual_retraining_alerts_only(tmp_path: Path, config_root: Path) -> None:
    patched = tmp_path / "configs"
    shutil.copytree(config_root, patched)
    use_case = patched / "use_cases" / "telco_churn.yaml"
    use_case.write_text(use_case.read_text() + "\nmonitoring:\n  retraining: manual\n")
    world = make_world(tmp_path / "w", patched)
    _scored(world, drifted=True)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None
    assert (firing.status, firing.result_code, firing.run_id) == (
        FiringStatus.SUCCEEDED,
        "DRIFT_ABOVE_THRESHOLD",
        None,
    )
    assert world.jobs.submitted == []
    assert len(world.alerts.store.query(AlertQuery(kind=AlertKind.DRIFT_ABOVE_THRESHOLD))) == 1


def test_drift_that_cannot_be_measured_against_a_new_champion_is_said_to_be_so(world: World) -> None:
    """The run was scored by an older champion; re-measuring needs the new one's preparation record."""
    _scored(world, drifted=True)
    register_champion(world, training_frame(world), model_number=2)  # no prepare.json: never trained
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    assert firing is not None
    assert (firing.status, firing.result_code) == (FiringStatus.SUCCEEDED, "DRIFT_NOT_MEASURED")


def test_nothing_the_scheduler_writes_holds_a_customer_id(world: World) -> None:
    _scored(world, drifted=True)
    ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.DRIFT_CHECK))
    written = json.dumps(
        {
            "alerts": [alert.model_dump(mode="json") for alert in world.alerts.store.query(AlertQuery())],
            "firings": [firing.model_dump(mode="json") for firing in world.store.list_firings()],
            "audit": [event.model_dump(mode="json") for event in world.audit.events],
        }
    )
    assert "C000" not in written
