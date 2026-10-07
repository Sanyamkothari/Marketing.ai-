"""A campaign records its run's holdout epoch and is measured only within it (M92 x M94, DEC-1304 (l)).

Through the product's own API, over a Phase 1 propensity run written into the store with the
`holdout_assignment.json` an engaged run writes (a persistent `use_case` holdout, epoch 1):

* `POST /campaigns` records the run's holdout scope, key and epoch; a default run records `run`;
* `POST /campaigns/{id}/measure` answers `409 CAMPAIGN_EPOCH_MISMATCH`, stores nothing and audits the
  refusal when the holdout was redrawn (a new epoch in the platform database's ledger, the one
  `GET /holdout` reads) before the outcomes were all in, when the run's epoch no longer matches the
  record, and when the campaign's runs span two epochs; a redraw after the outcomes were in is fine.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.audit.events import AuditQuery
from engine.audit.store import SqlAuditLog
from engine.holdout.salt import HoldoutLedger
from engine.holdout.spec import HoldoutAssignmentReport, HoldoutLedgerEntry, HoldoutSpec
from engine.measurement.campaign import REPORT_FILENAME, Campaign, SqlCampaignStore, campaign_key
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.storage import LocalStorage, run_key
from tests.integration.measurement.support import MATURE, SENT, USE_CASE, ok, propensity_run, upload

pytestmark = pytest.mark.integration

ENGAGED_RUN = "r_20261007_94e00001"
SECOND_RUN = "r_20261007_94e00002"
DEFAULT_RUN = "r_20261007_94e00003"
OUTCOMES_IN = SENT + timedelta(days=90)
"""The use case's 90-day window from the day the list went out."""


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path
    outcomes: str


def _engaged(storage: LocalStorage, run_id: str, epoch: int) -> None:
    """The aggregate file an engaged run writes beside its scores (DEC-1302 (e))."""
    storage.write_model(
        run_key(run_id, "holdout_assignment.json"),
        HoldoutAssignmentReport(
            run_id=run_id,
            use_case_id=USE_CASE,
            spec=HoldoutSpec(
                scope="use_case", fraction=0.1, salt_id="0123456789abcdef", epoch=epoch, scope_key=USE_CASE
            ),
            rows=4_000,
            holdout_members=400,
            control_rows=360,
            explore_candidates=0,
            explore_rows=0,
            created_at=SENT,
        ),
    )


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("campaign-epochs") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    seeded = propensity_run(storage, ENGAGED_RUN, rows=4_000)
    propensity_run(storage, SECOND_RUN, rows=4_000)
    propensity_run(storage, DEFAULT_RUN, rows=4_000)
    _engaged(storage, ENGAGED_RUN, epoch=1)
    _engaged(storage, SECOND_RUN, epoch=2)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield World(client, storage, data_dir, upload(client, seeded.outcomes))


def _ledger(world: World, epoch: int, started: datetime) -> None:
    HoldoutLedger(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME)).put_entry(
        HoldoutLedgerEntry(
            scope="use_case",
            scope_key=USE_CASE,
            epoch=epoch,
            fraction=0.1,
            salt_id="0123456789abcdef",
            started_at=started,
            updated_at=started,
        )
    )


def _campaign(world: World, run_id: str) -> str:
    created = ok(world.client.post("/campaigns", json={"run_id": run_id}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    ok(world.client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": world.outcomes}))
    return campaign_id


def _measure(world: World, campaign_id: str) -> Any:
    return world.client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()})


def _refused(world: World, campaign_id: str) -> str:
    response = _measure(world, campaign_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CAMPAIGN_EPOCH_MISMATCH"
    assert not world.storage.exists(campaign_key(campaign_id, REPORT_FILENAME)), "nothing is stored"
    return str(detail["message"])


def test_a_campaign_records_its_runs_holdout(world: World) -> None:
    engaged = ok(world.client.get(f"/campaigns/{_campaign(world, ENGAGED_RUN)}"))["campaign"]
    assert (engaged["holdout_scope"], engaged["holdout_scope_key"], engaged["holdout_epoch"]) == (
        "use_case",
        USE_CASE,
        1,
    )
    default = ok(world.client.get(f"/campaigns/{_campaign(world, DEFAULT_RUN)}"))["campaign"]
    assert (default["holdout_scope"], default["holdout_scope_key"], default["holdout_epoch"]) == (
        "run",
        None,
        None,
    )


def test_a_redraw_before_the_outcomes_were_in_is_refused_and_after_them_is_not(world: World) -> None:
    campaign_id = _campaign(world, ENGAGED_RUN)
    _ledger(world, 1, SENT - timedelta(days=30))
    ok(_measure(world, campaign_id))  # the ledger still holds the campaign's epoch

    campaign_id = _campaign(world, ENGAGED_RUN)
    _ledger(world, 2, OUTCOMES_IN - timedelta(days=10))
    message = _refused(world, campaign_id)
    assert OUTCOMES_IN.date().isoformat() in message
    log = SqlAuditLog(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME))
    refusals = [
        event
        for event in log.query(AuditQuery(action="campaigns.measure", limit=1000))
        if event.object_id == campaign_id
    ]
    assert [event.details.get("reason_code") for event in refusals] == ["CAMPAIGN_EPOCH_MISMATCH"]

    _ledger(world, 2, OUTCOMES_IN + timedelta(days=1))
    assert ok(_measure(world, campaign_id))["report"]["campaign_id"] == campaign_id


def test_a_run_whose_epoch_no_longer_matches_is_refused(world: World) -> None:
    _ledger(world, 1, SENT - timedelta(days=30))
    campaign_id = _campaign(world, ENGAGED_RUN)
    _engaged(world.storage, ENGAGED_RUN, epoch=3)
    try:
        assert "no longer" in _refused(world, campaign_id)
    finally:
        _engaged(world.storage, ENGAGED_RUN, epoch=1)


def test_a_campaign_whose_runs_span_two_epochs_is_refused(world: World) -> None:
    _ledger(world, 1, SENT - timedelta(days=30))
    campaign_id = _campaign(world, ENGAGED_RUN)
    store = SqlCampaignStore(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME))
    record = store.get(campaign_id)
    assert isinstance(record, Campaign)
    store.save(record.model_copy(update={"run_ids": (ENGAGED_RUN, SECOND_RUN)}))
    message = _refused(world, campaign_id)
    assert "epoch 1" in message and "epoch 2" in message
