"""Chunks rendered in worker processes hash to exactly the in-process fingerprint (Plan A M37, DEC-097).

The fingerprint is the identity of every upload and every dataset, so the parallel path is held to
equality, never to a tolerance: the same frame must give the same `sha256:v1:` string whether its
chunks were rendered here or in the pool, whether a worker failed half way, and whether the machine
has one core or many.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future

import numpy as np
import pandas as pd
import pytest

from engine.stages import ingest

ROWS = 5_000
CHUNK = 1_000  # five chunks, so the pool is used from the second on


def _mixed_frame() -> pd.DataFrame:
    """Every kind of cell the canonicaliser formats: floats, integers, nullable integers, dates,
    text with commas and quotes, booleans and nulls in each."""
    rng = np.random.default_rng(20260923)
    frame = pd.DataFrame(
        {
            "account_id": [f"{i:07d}" for i in range(ROWS)],
            "spend": rng.normal(100.0, 40.0, ROWS),
            "visits": rng.integers(0, 50, ROWS),
            "complaints": pd.array(rng.integers(0, 5, ROWS), dtype="Int64"),
            "joined": pd.Timestamp("2024-01-01") + pd.to_timedelta(rng.integers(0, 900, ROWS), unit="D"),
            "note": [f'said "hello, world" #{i}' if i % 7 else None for i in range(ROWS)],
            "opted_in": rng.integers(0, 2, ROWS).astype(bool),
        }
    )
    frame.loc[frame.index % 11 == 0, "spend"] = np.nan
    frame.loc[frame.index % 13 == 0, "complaints"] = pd.NA
    return frame


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force every chunk to be rendered here, the path every fingerprint was computed on before M37."""
    monkeypatch.setattr(ingest, "_render_pool", lambda: None)
    yield


def _fingerprint(frame: pd.DataFrame) -> str:
    return ingest.dataset_fingerprint(frame, chunk_rows=CHUNK).hash


def test_the_pool_gives_the_in_process_fingerprint_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = _mixed_frame()
    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_render_pool", lambda: None)
        expected = _fingerprint(frame)
    if ingest._render_workers() < 2:
        pytest.skip("this machine has fewer than three cores, so the pool is never started")
    assert ingest._render_pool() is not None
    assert _fingerprint(frame) == expected


def test_a_failed_worker_costs_time_and_never_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, in_process: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A pool whose every future fails: each chunk falls back to the in-process render, the pool is
    retired so no later chunk is sent to it, and the failure is logged once, not once per chunk."""
    frame = _mixed_frame()
    expected = _fingerprint(frame)

    class BrokenPool:
        submitted = 0

        def submit(self, fn: object, chunk: pd.DataFrame) -> Future[bytes]:
            BrokenPool.submitted += 1
            future: Future[bytes] = Future()
            future.set_exception(RuntimeError("a worker died"))
            return future

    broken = BrokenPool()
    retired: list[bool] = []

    def retire() -> None:
        retired.append(True)
        monkeypatch.setattr(ingest, "_pool_disabled", True)

    monkeypatch.setattr(ingest, "_pool_disabled", False)
    monkeypatch.setattr(ingest, "_render_pool", lambda: None if retired else broken)
    monkeypatch.setattr(ingest, "_disable_render_pool", retire)
    monkeypatch.setattr(ingest, "_render_workers", lambda: 3)

    with caplog.at_level("WARNING"):
        assert _fingerprint(frame) == expected
    assert retired, "a failed worker must retire the pool"
    assert BrokenPool.submitted >= 2, "the test needs several chunks in flight when the pool fails"
    warnings = [r for r in caplog.records if "render worker failed" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_one_core_never_starts_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_pool", None)
    monkeypatch.setattr(ingest, "_pool_disabled", False)
    monkeypatch.setattr(ingest.os, "cpu_count", lambda: 1)
    assert ingest._render_pool() is None


def test_a_single_chunk_is_rendered_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame of one chunk - every test fixture and most uploads - never reaches for the pool."""

    def must_not_start() -> None:
        raise AssertionError("a one-chunk digest started the pool")

    monkeypatch.setattr(ingest, "_render_pool", must_not_start)
    ingest.dataset_fingerprint(_mixed_frame().head(CHUNK), chunk_rows=CHUNK)
