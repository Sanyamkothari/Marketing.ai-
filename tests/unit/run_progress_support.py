"""Shared set-up for the Plan M M189 (DEC-1619) progress tests: `test_run_train.py`'s store, registry and
resolved use case as fixtures (imported by name into each test module), a fake clock, and a waiter for the
pipeline's heartbeat writes."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from engine import run_progress
from engine.config import ResolvedConfig, resolve_config
from engine.contracts import RunStatus
from engine.registry import LocalModelRegistry
from tests.unit.test_run_train import UPLOAD_KEY, USE_CASE, RecordingStorage


@pytest.fixture(name="resolved")
def resolved_fixture(config_root: Path) -> ResolvedConfig:
    return resolve_config(USE_CASE, root=config_root)


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> RecordingStorage:
    store = RecordingStorage(tmp_path / "data")
    store.write_bytes(UPLOAD_KEY, b"customer_id,converted_30d\nC-1,1\n")
    return store


@pytest.fixture(name="registry")
def registry_fixture(tmp_path: Path) -> LocalModelRegistry:
    return LocalModelRegistry(tmp_path / "registry.db")


class FakeClock:
    """A monotonic clock a fake train stage moves on, so a half-hour stage takes milliseconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The fake clock installed as the estimate's clock, and a heartbeat every 10 ms instead of 5 s."""
    fake = FakeClock()
    monkeypatch.setattr(run_progress, "CLOCK", fake)
    monkeypatch.setattr(run_progress, "HEARTBEAT_SECONDS", 0.01)
    return fake


def wait_for(condition: Callable[[], bool], what: str = "a heartbeat write", timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


def next_heartbeat(storage: RecordingStorage) -> RunStatus:
    """Wait for two more status writes and return the last: the second certainly read the clock as it is now."""
    seen = len(storage.statuses())
    wait_for(lambda: len(storage.statuses()) >= seen + 2)
    return storage.statuses()[-1]
