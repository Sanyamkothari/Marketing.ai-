"""A Telco file trained through the API scores a second file of the same format (DEC-956).

The Telco Customer Churn template spells its flags `Yes`/`No` (`Partner`, `PhoneService`,
`PaperlessBilling`, ...). `register` records a fitted column's type from the dtype the model saw -
text - so `schema.json` says `string`; the score-time SCHEMA_MISMATCH check types the uploaded file
with ingest's inference, which reads two boolean tokens as `boolean`. Before DEC-956 the two
vocabularies had no bridge, so `POST /runs` in score mode answered 409 "Changed type: Partner (was
string, now boolean) ..." for the very file format the model was trained on.

This module walks that journey as a user does - upload, train, upload, score - through `TestClient`
and the real local job runner. The search is the smallest real one (`test_jobs_as_sagemaker.py`'s
settings: a one-minute budget, one model family, no ensemble), which keeps it in the fast suite: it
is about the schema check, not the model.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.contracts import FeatureSchema, RunRecord, RunState
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage
from tests.fixtures.make_data import CLEAN, SCORING, GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "telco-churn"
PRIMARY_KEY = "customerID"
TARGET = "Churn"
TRAIN_ROWS = 2_500
"""Enough rows to pass `min_rows` and `min_positive` on this use case, and no more."""

SCORE_ROWS = 300
TRAIN_SEED = 20260924
SCORE_SEED = 20260925
RUN_TIMEOUT_S = 600.0

#: The template's two-valued Yes/No flags: text to the model, `boolean` to ingest's inference.
YES_NO_COLUMNS = ("Partner", "PhoneService", "PaperlessBilling")

OVERRIDES: dict[str, Any] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.candidates": ["LogisticRegression"],
    "model_search.ensemble": False,
}
"""The smallest real search there is. This module tests the schema check, not the model."""


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("yes-no-schema")


@pytest.fixture(scope="module")
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def upload(client: TestClient, *, variant: str, rows: int, seed: int, mode: str) -> str:
    frame = generate(GenerationSpec(USE_CASE, rows=rows, variant=variant, seed=seed))
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": (f"{mode}.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def finish(client: TestClient, run_id: str) -> RunRecord:
    """Poll the run until the local job runner has taken it to a terminal state."""
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        record = RunRecord.model_validate(response.json()["run"])
        if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
            return record
        assert time.monotonic() < deadline, f"run {run_id} still {record.state} after {RUN_TIMEOUT_S}s"
        time.sleep(0.5)


@pytest.fixture(scope="module")
def trained(client: TestClient) -> RunRecord:
    upload_id = upload(client, variant=CLEAN, rows=TRAIN_ROWS, seed=TRAIN_SEED, mode="train")
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": PRIMARY_KEY,
            "target": TARGET,
            "overrides": OVERRIDES,
        },
    )
    assert response.status_code == 202, response.text
    record = finish(client, str(response.json()["run_id"]))
    assert record.state is RunState.DONE, record.error
    assert record.model_version_id is not None
    return record


def test_a_telco_model_scores_a_second_file_of_the_same_format(
    client: TestClient, trained: RunRecord
) -> None:
    upload_id = upload(client, variant=SCORING, rows=SCORE_ROWS, seed=SCORE_SEED, mode="score")
    response = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "score",
            "upload_id": upload_id,
            "primary_key": PRIMARY_KEY,
            "model_version_id": trained.model_version_id,
            "overrides": OVERRIDES,
        },
    )

    assert response.status_code == 202, response.text
    record = finish(client, str(response.json()["run_id"]))
    assert record.state is RunState.DONE, record.error
    assert record.model_version_id == trained.model_version_id


def test_the_yes_no_flags_are_recorded_as_the_text_the_model_was_fitted_on(
    trained: RunRecord, data_dir: Path
) -> None:
    """The schema itself is unchanged by DEC-956: a model trained before the fix records the same.

    That is what makes the check-side bridge the backward-compatible fix - every `schema.json`
    already on disk says `string` for these columns, and it is right to: the predictor was fitted on
    the text `Yes`/`No`, not on a boolean.
    """
    version = LocalModelRegistry(data_dir / REGISTRY_FILENAME).get(trained.model_version_id or "")
    assert version is not None
    schema = LocalStorage(data_dir).read_model(version.schema_key, FeatureSchema)
    recorded = {column.name: column for column in schema.columns}

    for name in YES_NO_COLUMNS:
        assert recorded[name].inferred_type.value == "string", name
        assert set(recorded[name].categories) == {"No", "Yes"}, name
