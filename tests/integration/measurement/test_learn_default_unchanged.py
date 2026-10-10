"""Plan J M106 is opt-in: a scoring run that did not engage the holdout service learns exactly as before.

The run is step 4's own world (`tests/integration/uplift/test_measure_campaign.py`: a Phase 1 win-back run with
the per-run control group and no explore share, so no `holdout_assignment.json`). Learning from it must build
the very frame `engine.uplift.measure.build_experiment_frame` builds - every column, every row, every value -
write no learn record, keep step 4's view and the Approver's items free of the new keys, and never be refused
for overlap. Passes on b5ea557 too: it is the guard that the default path did not move.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.uplift import _read_all
from api.routes.uploads import load_upload
from engine.runs import job_spec_key, read_job_spec
from engine.stages import export
from engine.storage import LocalStorage, run_key
from engine.uplift.measure import build_experiment_frame
from tests.integration.uplift.test_measure_campaign import (
    FAST,
    PRIMARY_KEY,
    TARGET,
    _finish,
    _upload,
    seed_scored_run,
)

pytestmark = pytest.mark.integration

RUN_ID = "r_20261009_6d000001"
LEARNED: dict[str, Any] = {**FAST, "governance": {"approval_required": True}}


@pytest.fixture(scope="module")
def client(
    config_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TestClient, LocalStorage]]:
    data_dir = tmp_path_factory.mktemp("learn-default") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client, storage


def test_a_default_run_learns_from_the_same_frame_and_writes_nothing_new(
    client: tuple[TestClient, LocalStorage],
) -> None:
    test_client, storage = client
    seeded = seed_scored_run(test_client, storage, run_id=RUN_ID)
    assert not storage.exists(run_key(RUN_ID, "holdout_assignment.json"))
    outcomes_upload = _upload(test_client, seeded.outcomes, name="outcomes.csv")
    measured = test_client.post(
        f"/runs/{RUN_ID}/measure", json={"upload_id": outcomes_upload, "as_of": "2026-09-01T00:00:00Z"}
    )
    assert measured.status_code == 200, measured.text
    before = measured.json()
    assert "learned" not in before and before["learn"]["ready"] is True

    response = test_client.post(f"/runs/{RUN_ID}/measure/learn", json={"overrides": LEARNED})
    assert response.status_code == 202, response.text
    record = _finish(test_client, str(response.json()["run_id"]))
    assert record.upload_id is not None
    experiment = pd.read_parquet(storage.local_path(load_upload(storage, record.upload_id).source_key))

    spec = read_job_spec(storage, job_spec_key(RUN_ID))
    inputs = _read_all(storage, spec.upload_key, spec.upload_format)
    scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(RUN_ID, export.SCORES_PARQUET))))
    expected = build_experiment_frame(
        inputs,
        scores,
        seeded.outcomes,
        primary_key=PRIMARY_KEY,
        outcome_column=TARGET,
        positive_label=None,
        target_column=TARGET,
        treatment_column="contacted",
    )
    pd.testing.assert_frame_equal(experiment, expected, check_dtype=False)

    assert not storage.exists(run_key(record.run_id, "learned_from.json"))
    after = test_client.get(f"/runs/{RUN_ID}/measure").json()
    assert "learned" not in after
    moved = {"uplift_run", "outcomes"}  # the run learned from, and the record of it, as before M106
    assert {key: value for key, value in after.items() if key not in moved} == {
        key: value for key, value in before.items() if key not in moved
    }
    for item in test_client.get("/approvals").json()["items"]:
        assert "live_calibration" not in item
