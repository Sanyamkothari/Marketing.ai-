"""Plan M M184 (DEC-1614 (b)): a scheduled scoring firing uses only an approved model while approval is required.

Nobody chooses the model when a schedule fires. `POST /runs` honours any version a person names (DEC-050) and is
unchanged; a schedule that names `parameters.model_version_id` is refused with `MODEL_VERSION_NOT_APPROVED` when
that version was never approved (a candidate, or one still waiting for approval) and the use case's
`governance.approval_required` is on. The champion - every firing that names no version - always is approved, and
so is a version that was champion once (re-scoring history with it).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml

from engine.config import RunMode, load_use_case
from engine.contracts import ModelStatus, RunRecord
from engine.model_approval import MODEL_VERSION_NOT_APPROVED, model_version_approved, unapproved_model_message
from engine.onboarding.datasets import dataset_key
from engine.scheduling.alerts import AlertQuery
from engine.scheduling.firing import ScheduleFirer, build_dataset_from_spec
from engine.scheduling.schedules import FiringStatus, ScheduleKind, ScheduleParameters
from engine.storage import run_key
from tests.unit.production.scheduling_support import USE_CASE, World, make_world, register_champion


@pytest.fixture
def world(tmp_path: Path, config_root: Path) -> World:
    return make_world(tmp_path, config_root)


def _frame(world: World) -> pd.DataFrame:
    manifest = build_dataset_from_spec(
        world.spec,
        config=load_use_case(USE_CASE),
        mode=RunMode.TRAIN,
        client_store=world.client_store,
        storage=world.storage,
    )
    return pd.read_parquet(world.storage.local_path(dataset_key(manifest.dataset_id, "dataset.parquet")))


def _fire_named(world: World, model_id: str, schedule_id: str = "sch_named") -> object:
    schedule = world.schedule(
        ScheduleKind.SCORE,
        schedule_id=schedule_id,
        parameters=ScheduleParameters(onboarding_spec_id=world.spec.spec_id, model_version_id=model_id),
    )
    return ScheduleFirer(world.services()).fire(schedule)


def test_a_schedule_naming_a_model_that_was_never_approved_is_refused(world: World) -> None:
    frame = _frame(world)
    register_champion(world, frame)
    candidate = register_champion(world, frame, model_number=2, status=ModelStatus.CANDIDATE)
    assert load_use_case(USE_CASE).governance.approval_required, "the shipped default requires approval"

    firing = _fire_named(world, candidate.model_id)

    assert firing is not None
    assert firing.status is FiringStatus.FAILED  # type: ignore[attr-defined]
    assert firing.error_code == MODEL_VERSION_NOT_APPROVED  # type: ignore[attr-defined]
    assert world.jobs.submitted == [], "nothing was scored"
    (alert,) = world.alerts.store.query(AlertQuery())
    assert MODEL_VERSION_NOT_APPROVED in alert.message
    (event,) = world.audit.events
    assert event.details["reason_code"] == MODEL_VERSION_NOT_APPROVED


def test_the_champion_named_or_not_and_a_former_champion_are_still_scored(world: World) -> None:
    frame = _frame(world)
    first = register_champion(world, frame)
    second = register_champion(world, frame, model_number=2)  # promoting it archives the first
    archived = world.registry.get(first.model_id)
    assert archived.status is ModelStatus.ARCHIVED and model_version_approved(archived)

    for schedule_id, model_id in (("sch_former", first.model_id), ("sch_champion", second.model_id)):
        firing = _fire_named(world, model_id, schedule_id)
        assert firing is not None and firing.status is FiringStatus.RUNNING, firing  # type: ignore[attr-defined]
        record = world.storage.read_model(run_key(firing.run_id, "run.json"), RunRecord)  # type: ignore[attr-defined]
        assert record.model_version_id == model_id
    unnamed = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert unnamed is not None and unnamed.status is FiringStatus.RUNNING


def test_without_approval_required_a_named_candidate_is_scored_as_before(
    tmp_path: Path, config_root: Path
) -> None:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    path = root / "use_cases" / "telco_churn.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("governance", {})["approval_required"] = False
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    world = make_world(tmp_path / "w", root)
    frame = _frame(world)
    register_champion(world, frame)
    candidate = register_champion(world, frame, model_number=2, status=ModelStatus.CANDIDATE)

    firing = _fire_named(world, candidate.model_id)

    assert firing is not None and firing.status is FiringStatus.RUNNING  # type: ignore[attr-defined]


def test_what_counts_as_approved(world: World) -> None:
    frame = _frame(world)
    champion = register_champion(world, frame)
    candidate = register_champion(world, frame, model_number=2, status=ModelStatus.CANDIDATE)
    assert model_version_approved(champion)
    assert not model_version_approved(candidate)
    assert not model_version_approved(candidate.model_copy(update={"status": ModelStatus.PENDING_APPROVAL}))
    rejected = candidate.model_copy(update={"status": ModelStatus.ARCHIVED})
    assert not model_version_approved(rejected), "a challenger archived without ever being champion"
    assert unapproved_model_message(world.registry, champion.model_id) is None
    assert unapproved_model_message(world.registry, "m_nobody") is not None, "an unknown version fails closed"
    assert unapproved_model_message(world.registry, None) is not None
