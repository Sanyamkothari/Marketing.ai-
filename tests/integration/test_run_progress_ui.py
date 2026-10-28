"""Plan M M189 (DEC-1619): the running bar in jsdom, drawn from statuses a real training run wrote.

`tests/integration/production/ui/run_progress/run_progress.test.mjs` draws the goal's Running card, the uplift
workbench's running screen and the New campaign wizard's learning step through `ui/run_progress.js`. The
statuses it reads are not hand-written: this file runs a training run through the real pipeline (the stage
bodies are `test_run_train.py`'s fakes) with its train stage held on a fake clock, and keeps what the
pipeline's own heartbeat wrote three quarters of the way through the time budget, then at twice the budget,
then at the end. The "before M189" status is the mid-run one with the estimate taken off, which is exactly
what a status.json written before this milestone holds.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from engine.contracts import RunStatus
from engine.registry import LocalModelRegistry
from engine.stages import train
from tests.fixtures.node import skip_without_jsdom
from tests.unit.run_progress_support import (  # noqa: F401 - fixtures, by name
    FakeClock,
    clock_fixture,
    next_heartbeat,
    registry_fixture,
    resolved_fixture,
    storage_fixture,
)
from tests.unit.test_run_train import RecordingStorage, StageStubs, make_context, pipeline_for, read_status

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
SUITE: Final[Path] = NODE_DIR / "run_progress" / "run_progress.test.mjs"
PAGE_SUITE: Final[Path] = NODE_DIR / "run_progress" / "run_page.test.mjs"
"""The campaign progress page moving on to the review by itself (no fixtures needed)."""


def _dump(status: RunStatus, *, without_estimate: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(status.model_dump_json())
    if without_estimate:
        for name in ("estimated_pct", "eta_seconds", "estimate_basis"):
            body.pop(name)
        for row in body["stages"]:
            row.pop("fraction")
    return body


def write_fixtures(
    out: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved: Any,
    store: RecordingStorage,
    models: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    stubs = StageStubs().install(monkeypatch)
    budget = resolved.config.model_search.time_limit_minutes * 60.0
    kept: dict[str, RunStatus] = {}

    def held_train(recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> Any:
        for name, elapsed in (("status_mid", 0.75 * budget), ("status_overrun", 2.0 * budget)):
            clock.now = elapsed
            kept[name] = next_heartbeat(store)
        return stubs.train(recipe, parts, evaluation, **kwargs)

    monkeypatch.setattr(train, "train", held_train)
    pipeline_for(store, models).run_train(make_context(resolved, store, models))
    out.mkdir(parents=True)
    for name, status in kept.items():
        (out / f"{name}.json").write_text(json.dumps(_dump(status), indent=1), encoding="utf-8")
    (out / "status_legacy.json").write_text(
        json.dumps(_dump(kept["status_mid"], without_estimate=True), indent=1), encoding="utf-8"
    )
    (out / "status_done.json").write_text(json.dumps(_dump(read_status(store)), indent=1), encoding="utf-8")


def test_the_running_bar_in_jsdom(monkeypatch, resolved, storage, registry, clock, tmp_path: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)
    out = tmp_path / "fixtures"
    write_fixtures(out, monkeypatch, resolved, storage, registry, clock)
    result = subprocess.run(
        [node, "--test", "--test-concurrency=1", str(SUITE), str(PAGE_SUITE)],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]
