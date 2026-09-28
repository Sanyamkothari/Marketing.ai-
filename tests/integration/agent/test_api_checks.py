"""`POST /uploads/{id}/checks` (Plan G M71): the Run button's checks, with nothing started or written."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.storage import LocalStorage
from tests.fixtures.make_data import LEAKY_COLUMN, GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
KEY = "customer_id"
TARGET = "converted_30d"


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _upload(client: TestClient, variant: str, *, rows: int = 1_500, mode: str = "train") -> str:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, variant=variant))
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _checks(report: dict[str, Any]) -> list[tuple[str, str, str | None, bool]]:
    return [(c["code"], c["severity"], c["column"], c["acknowledged"]) for c in report["checks"]]


@pytest.mark.parametrize(
    "variant", ["duplicate_keys", "leaky_column", "too_few_positives", "constant_target"]
)
def test_a_dry_run_says_what_the_run_button_says(client: TestClient, variant: str) -> None:
    upload_id = _upload(client, variant)
    body = {"use_case": USE_CASE, "primary_key": KEY, "target": TARGET}
    dry = client.post(f"/uploads/{upload_id}/checks", json=body)
    assert dry.status_code == 200, dry.text
    run = client.post("/runs", json={**body, "mode": "train", "upload_id": upload_id})
    assert run.status_code == 409, run.text
    assert dry.json()["passed"] is False
    assert _checks(dry.json()) == _checks(run.json()["validation"])


def test_a_dry_run_honours_the_same_overrides(client: TestClient) -> None:
    upload_id = _upload(client, "leaky_column")
    body = {
        "use_case": USE_CASE,
        "primary_key": KEY,
        "target": TARGET,
        "overrides": {"prepare.exclude_columns": [LEAKY_COLUMN]},
    }
    report = client.post(f"/uploads/{upload_id}/checks", json=body).json()
    assert not any(c["code"] == "LEAKAGE_SUSPECTED" and c["severity"] == "error" for c in report["checks"])


def test_a_dry_run_starts_nothing_and_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    upload_id = _upload(client, "clean", rows=3_000)  # 12% positives: above the 200 minimum
    storage = LocalStorage(tmp_path / "data")
    before = sorted(storage.list_keys(""))
    response = client.post(
        f"/uploads/{upload_id}/checks", json={"use_case": USE_CASE, "primary_key": KEY, "target": TARGET}
    )
    assert response.status_code == 200
    assert response.json()["passed"] is True
    assert response.json()["run_id"] is None
    assert sorted(storage.list_keys("")) == before
    assert client.get("/runs").json()["runs"] == []


def test_a_bad_override_is_refused_as_post_runs_refuses_it(client: TestClient) -> None:
    upload_id = _upload(client, "clean", rows=300)
    body = {"use_case": USE_CASE, "primary_key": KEY, "target": TARGET, "overrides": {"agent.enabled": False}}
    dry = client.post(f"/uploads/{upload_id}/checks", json=body)
    run = client.post("/runs", json={**body, "mode": "train", "upload_id": upload_id})
    assert dry.status_code == run.status_code == 422
    assert dry.json()["detail"]["code"] == run.json()["detail"]["code"] == "OVERRIDE_UNKNOWN_PATH"


def test_unknown_upload_and_use_case_are_404(client: TestClient) -> None:
    upload_id = _upload(client, "clean", rows=300)
    assert client.post("/uploads/nope/checks", json={"use_case": USE_CASE}).status_code == 404
    assert (
        client.post(f"/uploads/{upload_id}/checks", json={"use_case": "no-such-use-case"}).status_code == 404
    )


def test_a_model_version_is_refused_for_a_training_file(client: TestClient) -> None:
    upload_id = _upload(client, "clean", rows=300)
    response = client.post(
        f"/uploads/{upload_id}/checks", json={"use_case": USE_CASE, "model_version_id": "m1"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UPLOAD_MODE_MISMATCH"
