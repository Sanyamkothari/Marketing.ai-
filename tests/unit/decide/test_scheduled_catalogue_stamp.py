"""Plan J M100 part B (DEC-1310): a scheduled scoring run is stamped from the root it was checked against.

The first review's major finding: the M99 gap (the catalogue a run is stamped and priced with came from
the run process's own config root, not the one its use case was checked against) was closed only in
`POST /runs`. A scheduled firing resolves and checks the use case against `FiringServices.config_root`
(`create_app(config_root=...)`'s root, through `get_config_root`), and starts the run through
`engine.scheduling.firing.start_dataset_run`, which wrote no stamp: the run's actions stage then stamped
from the process's default root. These tests fire a real schedule (the real recipe rebuild, dataset
checks and `create_run`, as `tests/unit/production/test_firing.py` does) with a config root of their own
and no `MARKETING_AI_CONFIG_DIR`; the first fails on 53af5d5, where the run directory holds no stamp.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml

from engine.config import RunMode, load_use_case
from engine.decide.catalogue import CATALOGUE_STAMP_FILENAME, CatalogueStamp
from engine.onboarding.datasets import dataset_key
from engine.scheduling.firing import ScheduleFirer, build_dataset_from_spec
from engine.scheduling.schedules import FiringStatus, ScheduleKind
from engine.storage import run_key
from tests.unit.production.scheduling_support import USE_CASE, World, make_world, register_champion

CATALOGUE = """\
# A test catalogue: the High band's action, by call then SMS.
actions:
  - action_id: retention_call
    label: Retention call
    channels: [call, sms]
    offer_cost: 150.0
    contact_cost: 12.5
"""


def _root(config_root: Path, target: Path) -> Path:
    shutil.copytree(config_root, target)
    assert not (target / "decide" / "catalogue.yaml").exists(), "the repository ships no catalogue"
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    path = target / "use_cases" / "telco_churn.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["actions"]["bands"][0]["action_id"] = "retention_call"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _fire_score(world: World) -> str:
    config = load_use_case(USE_CASE)
    manifest = build_dataset_from_spec(
        world.spec, config=config, mode=RunMode.TRAIN, client_store=world.client_store, storage=world.storage
    )
    frame = pd.read_parquet(world.storage.local_path(dataset_key(manifest.dataset_id, "dataset.parquet")))
    register_champion(world, frame)
    firing = ScheduleFirer(world.services()).fire(world.schedule(ScheduleKind.SCORE))
    assert firing is not None and firing.status is FiringStatus.RUNNING, firing
    assert firing.run_id is not None
    return firing.run_id


def test_a_scheduled_score_is_stamped_from_the_root_it_was_checked_against(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)  # the services' root is the only one
    world = make_world(tmp_path / "w", _root(config_root, tmp_path / "configs"))
    run_id = _fire_score(world)
    key = run_key(run_id, CATALOGUE_STAMP_FILENAME)
    assert world.storage.exists(key), "stamped when the run was created, before its job starts"
    stamp = world.storage.read_model(key, CatalogueStamp)
    assert stamp.run_id == run_id
    assert stamp.catalogue_sha256 == hashlib.sha256(CATALOGUE.encode("utf-8")).hexdigest()
    assert stamp.planned_channels == {"retention_call": ("call", "sms")}
    action = stamp.actions["retention_call"]
    assert (action.label, action.offer_cost, action.contact_cost) == ("Retention call", 150.0, 12.5)


def test_a_scheduled_score_without_a_catalogue_writes_no_stamp(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default configuration: no `catalogue.yaml` in the services' root, so the run directory is
    exactly what it was."""
    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    world = make_world(tmp_path / "w", config_root)
    run_id = _fire_score(world)
    assert not world.storage.exists(run_key(run_id, CATALOGUE_STAMP_FILENAME))
