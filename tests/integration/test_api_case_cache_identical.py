"""Data Doctor's caches change how fast a step is, never what it says.

The same file is taken through a visit and a goal twice in one process: once with every cache empty and once with
every cache full (the first run filled them). The two cases must be the same JSON, cell for cell: the same verdicts,
counts, items and question ids. The random ids are made a counter so the two runs can be told apart by content only.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
import secrets
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.content_cache import clear_content_caches
from tests.integration.test_api_case_buildable_goal import GOAL, order_log

_CLOCK = re.compile(r'"\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d)"')


def _stable(case: dict[str, Any]) -> str:
    return _CLOCK.sub('""', json.dumps(case, indent=1, sort_keys=True))


def _two_steps(config_root: Path, data_dir: Path, csv: bytes, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    counter = itertools.count()

    def token_hex(nbytes: int | None = None) -> str:
        size = 32 if nbytes is None else nbytes
        return hashlib.sha256(str(next(counter)).encode()).hexdigest()[: 2 * size]

    monkeypatch.setattr(secrets, "token_hex", token_hex)
    steps: list[str] = []
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        upload = client.post(
            "/uploads",
            files={"file": ("orders.csv", csv, "text/csv")},
            data={"use_case": GOAL, "mode": "train"},
        )
        assert upload.status_code == 201, upload.text
        case_id = client.post("/cases", json={"upload_ids": [upload.json()["upload_id"]]}).json()["case_id"]
        client.post(f"/cases/{case_id}/ai-consent", json={})
        visit = client.post(f"/cases/{case_id}/visit", json={})
        assert visit.status_code == 200, visit.text
        steps.append(_stable(visit.json()))
        goal = client.post(
            f"/cases/{case_id}/goal",
            json={"version": visit.json()["version"], "goal_id": GOAL, "op_id": "g1"},
        )
        assert goal.status_code == 200, goal.text
        steps.append(_stable(goal.json()))
    return steps


def test_two_steps_on_one_file_are_the_same_case_with_the_caches_cold_and_warm(
    config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.StringIO()
    pd.DataFrame(order_log()).to_csv(buffer, index=False)
    csv = buffer.getvalue().encode()

    clear_content_caches()
    cold = _two_steps(config_root, tmp_path / "cold", csv, monkeypatch)
    warm = _two_steps(
        config_root, tmp_path / "warm", csv, monkeypatch
    )  # the caches hold what the first run read

    assert len(cold) == len(warm) == 2
    for step, (before, after) in zip(("visit", "goal"), zip(cold, warm, strict=True), strict=True):
        assert before == after, f"the {step} case differs between a cold and a warm run"
