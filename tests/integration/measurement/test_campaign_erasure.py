"""Erasing a customer does not move a campaign's goalposts (Plan J M94, DEC-1304 (h), (j)).

Erasure removes the principal's row from `campaigns/<id>/assignment.parquet` (DEC-741), so the
population the next measurement reads is the registered plan's less that customer. That is a privacy
request honoured, not a different test: the campaign is still measured against the plan in force,
with no amendment that nobody made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.access.roles import LOCAL_OPERATOR
from engine.measurement.campaign import ASSIGNMENT_FILENAME, INTENDED_COLUMN, campaign_key, read_frame
from engine.measurement.plan import TestPlan
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.erasure import erase
from engine.storage import LocalStorage
from engine.uplift.contracts import IncrementalityReport
from tests.integration.measurement.support import MATURE, PRIMARY_KEY, TARGET, ok, propensity_run, upload

pytestmark = pytest.mark.integration

RUN_ID = "r_20261007_94000011"
SALT = "campaign-erasure-salt-0001"


def test_an_erased_customer_does_not_change_the_registered_plan(config_root: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    run = propensity_run(storage, RUN_ID, rows=2_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        outcomes = upload(client, run.outcomes)
        campaign_id = ok(client.post("/campaigns", json={"run_id": RUN_ID}), 201)["campaign"]["campaign_id"]
        ok(client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": outcomes}))
        body = {
            "metric": "reactivated within 90 days",
            "outcome_column": TARGET,
            "analysis_date": "2026-08-15",
        }
        plan = TestPlan.model_validate(ok(client.post(f"/campaigns/{campaign_id}/plan", json=body), 201))

        # one held-back customer of the measured population asks to be erased
        assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
        held_back = assignment[(assignment["arm"] == "holdout") & assignment[INTENDED_COLUMN]]
        principal = str(held_back[PRIMARY_KEY].iloc[0])
        engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
        outcome = erase(
            storage,
            principal,
            engine=engine,
            principal=LOCAL_OPERATOR,
            salt=SALT,
            client_id="cl_1",
            config_root=config_root,
        )
        engine.dispose()
        assert outcome.status == "completed"
        after = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
        assert principal not in set(after[PRIMARY_KEY]) and len(after.index) == len(assignment.index) - 1

        measured = client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()})
        view = ok(measured)
        report = IncrementalityReport.model_validate(view["report"])
        assert report.test_plan_hash == plan.plan_hash and report.early_look is False
        assert view["verdict"] is not None and view["campaign"]["status"] == "measured"
        assert ok(client.get(f"/campaigns/{campaign_id}/plan"))["plan"]["version"] == 1, "no amendment"
