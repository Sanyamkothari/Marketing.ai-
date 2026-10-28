"""Lessons memory through the API: the owner lists, approves and rejects what the AI proposed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.doctor.lessons import LessonStore
from engine.storage import LocalStorage

pytestmark = pytest.mark.integration


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def test_list_approve_and_reject(client: TestClient, data_dir: Path) -> None:
    assert client.get("/doctor/lessons").json()["lessons"] == []
    store = LessonStore(LocalStorage(data_dir))
    keep = store.propose("Ask which date column is the order date before building any window.")
    drop = store.propose("Never ask the person anything.")
    listed = client.get("/doctor/lessons").json()
    assert [row["status"] for row in listed["lessons"]] == ["proposed", "proposed"] and listed[
        "max_approved"
    ] == 8

    ok = client.post(f"/doctor/lessons/{keep.lesson_id}/approve")
    assert ok.status_code == 200 and ok.json()["lesson"]["status"] == "approved"
    no = client.post(f"/doctor/lessons/{drop.lesson_id}/reject")
    assert no.status_code == 200 and no.json()["lesson"]["status"] == "rejected"
    assert client.post(f"/doctor/lessons/{drop.lesson_id}/approve").status_code == 409
    assert client.post("/doctor/lessons/ls_missing/approve").status_code == 404


def test_retire_an_approved_lesson(client: TestClient, data_dir: Path) -> None:
    lesson = LessonStore(LocalStorage(data_dir)).propose("Ask which date column is the order date first.")
    assert client.post(f"/doctor/lessons/{lesson.lesson_id}/retire").status_code == 409  # not approved yet
    client.post(f"/doctor/lessons/{lesson.lesson_id}/approve")
    done = client.post(f"/doctor/lessons/{lesson.lesson_id}/retire")
    assert done.status_code == 200 and done.json()["lesson"]["status"] == "rejected"
    assert client.post("/doctor/lessons/ls_missing/retire").status_code == 404
