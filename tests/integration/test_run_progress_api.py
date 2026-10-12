"""Plan M M189 (DEC-1619): `GET /runs/{id}` carries the progress estimate, and `progress_pct` is unchanged.

A training run goes through the real pipeline (its stage bodies are `test_run_train.py`'s fakes) into the
app's data directory on a background thread. Its train stage "runs" on a fake clock and holds until the
test has read the run through the API, so what the Running screen's poll sees mid-training is checked on
the real route: the time-weighted `estimated_pct` beside the unchanged done-stages `progress_pct`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import RunDetailResponse
from engine.contracts import StageKey
from engine.stages import train
from tests.unit.run_progress_support import (  # noqa: F401 - fixtures, by name
    clock_fixture,
    registry_fixture,
    resolved_fixture,
    storage_fixture,
)
from tests.unit.test_run_train import RUN_ID, StageStubs, make_context, pipeline_for

pytestmark = pytest.mark.integration


def test_the_status_route_carries_the_estimate_mid_training_and_progress_pct_keeps_its_meaning(
    monkeypatch: pytest.MonkeyPatch, resolved, storage, registry, clock, config_root: Path, tmp_path: Path
) -> None:
    stubs = StageStubs().install(monkeypatch)
    budget = resolved.config.model_search.time_limit_minutes * 60.0
    training = threading.Event()
    release = threading.Event()

    def held_train(recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> Any:
        clock.now += budget / 2
        training.set()
        assert release.wait(10), "the test never released the train stage"
        return stubs.train(recipe, parts, evaluation, **kwargs)

    monkeypatch.setattr(train, "train", held_train)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry))
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert training.wait(10), "the run never reached its train stage"
        with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
            deadline = time.monotonic() + 5
            while True:
                response = client.get(f"/runs/{RUN_ID}")
                assert response.status_code == 200, response.text
                body = response.json()["status"]
                if body["estimated_pct"] is not None and body["estimated_pct"] >= 40:
                    break  # a heartbeat written after the clock moved
                assert time.monotonic() < deadline, body
                time.sleep(0.01)
            detail = RunDetailResponse.model_validate(response.json())
    finally:
        release.set()
        worker.join(10)
    assert not errors, errors

    for field in ("estimated_pct", "eta_seconds", "estimate_basis"):
        assert field in body
    assert detail.status.current_stage is StageKey.TRAIN
    assert detail.status.progress_pct == 50  # done stages over total: 4 of 8, as before
    assert detail.status.estimate_basis == "time_budget"
    assert detail.status.estimated_pct is not None and 40 <= detail.status.estimated_pct < 99
    assert detail.status.eta_seconds is not None and 0 < detail.status.eta_seconds < budget
    fractions = {row.key: row.fraction for row in detail.status.stages}
    assert fractions.pop(StageKey.TRAIN) == pytest.approx(0.5, abs=0.01)
    assert set(fractions.values()) == {None}

    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as client:
        done = RunDetailResponse.model_validate(client.get(f"/runs/{RUN_ID}").json())
    assert (done.status.progress_pct, done.status.estimated_pct, done.status.eta_seconds) == (100, 100, 0)
