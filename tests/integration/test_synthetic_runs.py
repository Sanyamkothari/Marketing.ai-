"""A run that reads an upload marked synthetic is recorded as synthetic (Plan J M95).

`POST /uploads` takes an optional `synthetic` form field; the upload record keeps it, and `POST /runs`
copies it onto `run.json`, so every report drawn from the run says the effect is planted. Without the
field nothing changes: the upload and the run are not synthetic, and old `upload.json` files read as
not synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import UploadRecord
from engine.contracts import RunRecord
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key, upload_key
from tests.integration.test_api_runs import (
    CLEAN_CSV,
    DEMO_ID,
    install_ingest_stub,
    install_m2_job_stub,
    install_validate_stub,
    run_body,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app of `test_api_runs`: ingest and validation stood in for, runs moved through the real status
    machine in milliseconds. These tests are about what `POST /uploads` and `POST /runs` record."""
    install_ingest_stub(monkeypatch)
    install_validate_stub(monkeypatch)
    install_m2_job_stub(monkeypatch)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


def upload(client: TestClient, **form: str) -> str:
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", CLEAN_CSV.encode(), "text/csv")},
        data={"use_case": DEMO_ID, "mode": "train", **form},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def run_of(client: TestClient, storage: LocalStorage, upload_id: str) -> RunRecord:
    response = client.post("/runs", json=run_body(upload_id))
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    client.app.state.jobs.wait(run_id, 10.0)
    return storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)


def stored_upload(storage: LocalStorage, upload_id: str) -> UploadRecord:
    return storage.read_model(upload_key(upload_id, "upload.json"), UploadRecord)


def test_a_synthetic_upload_makes_a_synthetic_run(client: TestClient, storage: LocalStorage) -> None:
    upload_id = upload(client, synthetic="true")
    assert stored_upload(storage, upload_id).synthetic is True
    record = run_of(client, storage, upload_id)
    assert record.synthetic is True
    assert client.get(f"/runs/{record.run_id}").json()["run"]["synthetic"] is True


def test_an_ordinary_upload_makes_an_ordinary_run(client: TestClient, storage: LocalStorage) -> None:
    upload_id = upload(client)
    assert stored_upload(storage, upload_id).synthetic is False
    assert run_of(client, storage, upload_id).synthetic is False


def test_an_upload_record_written_before_the_field_existed_is_not_synthetic(
    client: TestClient, storage: LocalStorage
) -> None:
    upload_id = upload(client, synthetic="true")
    key = upload_key(upload_id, "upload.json")
    document: dict[str, Any] = json.loads(storage.read_bytes(key))
    del document["synthetic"]
    storage.write_bytes(key, json.dumps(document).encode())
    assert stored_upload(storage, upload_id).synthetic is False
