"""Through the API: a session that reaches 'ready' on the helper's suggestions is approved, never
refused by Approve's own checks (the review's repros for 422 TEMPLATE_TIME_MISSING and 409
TIME_COLUMN_MISSING / TIME_COLUMN_UNPARSEABLE)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from engine.jobs import CancelToken
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = pytest.mark.integration


@pytest.fixture
def client(config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _approve(client: TestClient, frame: pd.DataFrame, use_case: str) -> tuple[dict[str, Any], Any]:
    """Upload, start, tick every suggestion, answer each question with its first option, approve."""
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": use_case, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    base = f"/uploads/{upload_id}/agent-session"
    session = client.post(base, json={"use_case": use_case}).json()["session"]
    for question in session["questions"]:
        answered = client.post(
            f"{base}/answers",
            json={"question_id": question["question_id"], "option_id": question["options"][0]["option_id"]},
        )
        assert answered.status_code == 200, answered.text
        session = answered.json()["session"]
    pending = [p["proposal_id"] for p in session["proposals"] if p["state"] == "pending"]
    decided = client.post(
        f"{base}/decisions",
        json={"decisions": [{"proposal_id": pid, "state": "accepted"} for pid in pending]},
    )
    assert decided.status_code == 200, decided.text
    session = decided.json()["session"]
    return session, client.post(f"{base}/apply")


def _renamed(use_case: str) -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id=use_case, rows=2_000)).rename(
        columns={"snapshot_date": "reading_date"}
    )


def _unparseable() -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="fault-prediction", rows=2_000))
    rows = np.random.default_rng(0).choice(len(frame), len(frame) // 3, replace=False)
    frame["snapshot_date"] = frame["snapshot_date"].astype(str)
    frame.loc[rows, "snapshot_date"] = "not a date"
    return frame


def _drifting() -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=2_000))
    dates = pd.to_datetime(frame["snapshot_date"])
    late = dates >= sorted(dates.unique())[-max(1, dates.nunique() // 6)]
    return frame.assign(converted_30d=late.astype(int))


@pytest.mark.parametrize(
    ("name", "use_case"),
    [
        ("renamed", "targeted-advertisement"),
        ("renamed", "fault-prediction"),
        ("unparseable", "fault-prediction"),
        ("drifting", "targeted-advertisement"),
    ],
)
def test_a_ready_session_on_the_helpers_suggestions_is_approved(
    client: TestClient, name: str, use_case: str
) -> None:
    frame = {"renamed": lambda: _renamed(use_case), "unparseable": _unparseable, "drifting": _drifting}[
        name
    ]()
    session, applied = _approve(client, frame, use_case)
    assert session["status"] == "ready"
    assert applied.status_code == 200, applied.text


def _few_dates() -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="fault-prediction", rows=2_000))
    frame["snapshot_date"] = np.where(np.arange(len(frame)) % 2 == 0, "2026-08-01", "2026-08-02")
    return frame


def test_a_date_column_with_too_few_dates_is_split_at_random_and_approved(client: TestClient) -> None:
    session, applied = _approve(client, _few_dates(), "fault-prediction")
    split = next(p for p in session["proposals"] if p["path"] == "split.type")
    assert "fewer than three different dates" in split["reason"]
    assert session["status"] == "ready"
    assert applied.status_code == 200, applied.text


def test_preview_with_a_split_by_date_but_not_its_column_is_refused_not_left_for_approve(
    client: TestClient,
) -> None:
    """Ticking 'Test on the newest rows' but not its date column: 422 on Preview, never 'ready'
    followed by 409 TIME_COLUMN_MISSING on Approve."""
    use_case = "targeted-advertisement"
    frame = generate(GenerationSpec(use_case_id=use_case, rows=2_000))
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": use_case, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    base = f"/uploads/{response.json()['upload_id']}/agent-session"
    session = client.post(base, json={"use_case": use_case}).json()["session"]
    assert any(p["path"] == "split.type" for p in session["proposals"])
    boxes = [
        {
            "proposal_id": p["proposal_id"],
            "state": "rejected" if p["path"] == "split.time_column" else "accepted",
        }
        for p in session["proposals"]
        if p["state"] == "pending"
    ]
    refused = client.post(f"{base}/decisions", json={"decisions": boxes})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "TIME_COLUMN_MISSING"
    assert client.get(base).json()["session"]["status"] != "ready"
