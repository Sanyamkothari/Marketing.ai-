"""Plan M M189 (DEC-1619): the estimate moves while the train stage runs, and the heartbeat stops writing.

The train stage is a fake that "runs for" half an hour on a fake clock: it moves the clock on in steps and,
after each, waits for the pipeline's heartbeat to rewrite `status.json`. Nothing in the train stage writes
a status itself (train.py is frozen), so every reading taken while it runs comes from the heartbeat. The
stage stubs, the recording store and the context are `test_run_train.py`'s.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from engine import run_progress
from engine.contracts import RunState, RunStatus, StageKey
from engine.jobs import CancelToken, JobCancelledError
from engine.runs import cancel_run
from engine.stages import train
from tests.unit.run_progress_support import (  # noqa: F401 - fixtures, by name
    FakeClock,
    clock_fixture,
    next_heartbeat,
    registry_fixture,
    resolved_fixture,
    storage_fixture,
    wait_for,
)
from tests.unit.test_run_train import (
    RUN_ID,
    RecordingStorage,
    StageStubs,
    make_context,
    pipeline_for,
    read_run,
    read_status,
)

STEPS = 5


def _slow_train(
    stubs: StageStubs, store: RecordingStorage, clock: FakeClock, budget: float, readings: list[RunStatus]
) -> Callable[..., Any]:
    def slow(recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> Any:
        for _ in range(STEPS):
            clock.now += budget / (STEPS + 1)
            readings.append(next_heartbeat(store))
        return stubs.train(recipe, parts, evaluation, **kwargs)

    return slow


def test_estimated_pct_rises_during_the_train_stage_while_progress_pct_stays(
    monkeypatch, resolved, storage, registry, clock
) -> None:
    stubs = StageStubs().install(monkeypatch)
    budget = resolved.config.model_search.time_limit_minutes * 60.0
    readings: list[RunStatus] = []
    monkeypatch.setattr(train, "train", _slow_train(stubs, storage, clock, budget, readings))
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry))

    assert len(readings) == STEPS
    assert all(status.current_stage is StageKey.TRAIN for status in readings)
    # progress_pct keeps its meaning: four of eight stages done, the whole time.
    assert {status.progress_pct for status in readings} == {50}
    estimates = [status.estimated_pct for status in readings]
    assert all(value is not None for value in estimates)
    assert estimates == sorted(estimates) and len(set(estimates)) == STEPS, estimates
    assert estimates[0] > 5 and estimates[-1] < 99
    assert all(status.estimate_basis == "time_budget" for status in readings)
    etas = [status.eta_seconds for status in readings]
    assert all(eta is not None for eta in etas) and etas == sorted(etas, reverse=True)
    fractions = [
        next(row.fraction for row in status.stages if row.key is StageKey.TRAIN) for status in readings
    ]
    assert fractions == sorted(fractions) and 0 < fractions[0] < fractions[-1] < 1

    final = read_status(storage)
    assert final.state is RunState.DONE
    assert (final.progress_pct, final.estimated_pct, final.eta_seconds) == (100, 100, None)
    # Every status write is a monotonic reading.
    written = [status.estimated_pct for status in storage.statuses() if status.estimated_pct is not None]
    assert written == sorted(written)


def test_the_heartbeat_stops_writing_once_the_run_is_done(
    monkeypatch, resolved, storage, registry, clock
) -> None:
    stubs = StageStubs().install(monkeypatch)
    readings: list[RunStatus] = []
    monkeypatch.setattr(train, "train", _slow_train(stubs, storage, clock, 1800.0, readings))
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry))
    count = len(storage.statuses())
    time.sleep(0.2)  # twenty heartbeat intervals
    assert len(storage.statuses()) == count
    assert read_status(storage).state is RunState.DONE
    assert not [thread for thread in threading.enumerate() if thread.name.startswith(f"progress-{RUN_ID}")]


def test_the_heartbeat_never_writes_after_a_cancel(monkeypatch, resolved, storage, registry, clock) -> None:
    StageStubs().install(monkeypatch)
    cancel = CancelToken()
    after_cancel: list[int] = []

    def cancelled_while_training(recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> Any:
        clock.now += 600.0
        next_heartbeat(storage)
        cancel.cancel()
        after_cancel.append(len(storage.statuses()))
        time.sleep(0.2)  # the heartbeat had twenty chances to write meanwhile
        after_cancel.append(len(storage.statuses()))
        raise JobCancelledError("The run was cancelled.")

    monkeypatch.setattr(train, "train", cancelled_while_training)
    with pytest.raises(JobCancelledError):
        pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, cancel=cancel))
    # at most one write was already on its way when the token flipped; none after that
    assert after_cancel[1] - after_cancel[0] <= 1
    final = read_status(storage)
    assert final.state is RunState.CANCELLED
    assert final.eta_seconds is None
    assert all(row.fraction is None for row in final.stages)
    count = len(storage.statuses())
    time.sleep(0.1)
    assert len(storage.statuses()) == count


def test_the_heartbeat_never_overwrites_a_cancel_written_from_outside_the_flow(
    monkeypatch, resolved, storage, registry, clock
) -> None:
    """The cancel route, the cost cap and SageMaker's reconcile call `runs.cancel_run`, which writes CANCELLED
    to status.json without the flow's lock, while the flow's own token is set only later (on SageMaker, at the
    SIGTERM that follows StopTrainingJob). The heartbeat must leave that ending alone in the meantime."""
    StageStubs().install(monkeypatch)
    seen: dict[str, Any] = {}

    def cancelled_from_outside(recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> Any:
        clock.now += 600.0
        last = next_heartbeat(storage)
        # the recording store notes a write before it reaches the file: let that one land first
        wait_for(lambda: read_status(storage).updated_at >= last.updated_at, "the heartbeat's write to land")
        cancel_run(storage, RUN_ID)  # the token is NOT set: the container has not heard yet
        for _ in range(4):  # the heartbeat had many chances to write while the clock moved on
            clock.now += 60.0
            time.sleep(0.05)
        seen["written"] = list(storage.statuses())
        seen["stored"] = read_status(storage)
        raise JobCancelledError("The run was cancelled.")

    monkeypatch.setattr(train, "train", cancelled_from_outside)
    with pytest.raises(JobCancelledError):
        pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry))
    written: list[RunStatus] = seen["written"]
    cancelled_at = next(i for i, status in enumerate(written) if status.state is RunState.CANCELLED)
    later = [status.state for status in written[cancelled_at + 1 :]]
    assert RunState.RUNNING not in later, f"status.json went back to running after the cancel: {later}"
    assert seen["stored"].state is RunState.CANCELLED
    assert read_status(storage).state is RunState.CANCELLED


def test_a_heartbeat_write_already_on_its_way_when_the_cancel_lands_is_put_right_by_the_next_beat(
    monkeypatch, resolved, storage, registry, clock
) -> None:
    """No lock is shared across processes, so a heartbeat write can be in flight while `cancel_run` writes.
    Made deterministic here: `cancel_run` runs inside a heartbeat's own write, before it reaches the file.
    The next beat finds run.json cancelled under a status.json that says running, and puts the ending back."""
    StageStubs().install(monkeypatch)
    armed = threading.Event()
    fired = threading.Event()
    record = storage.write_model

    def write_model(key: str, model: Any) -> None:
        if (
            armed.is_set()
            and threading.current_thread().name.startswith("progress-")
            and key.endswith("status.json")
        ):
            armed.clear()
            cancel_run(storage, RUN_ID)  # lands first; the heartbeat's RUNNING then lands on top of it
            fired.set()
        record(key, model)

    monkeypatch.setattr(storage, "write_model", write_model)
    seen: dict[str, Any] = {}

    def cancelled_mid_write(recipe: Any, parts: Any, evaluation: Any, **kwargs: Any) -> Any:
        clock.now += 600.0
        next_heartbeat(storage)
        armed.set()
        wait_for(fired.is_set, "the cancel inside a heartbeat write")
        wait_for(
            lambda: read_status(storage).state is RunState.CANCELLED, "the next beat to put the ending back"
        )
        time.sleep(0.1)  # and ten more chances to write
        seen["written"] = list(storage.statuses())
        seen["stored"] = read_status(storage)
        raise JobCancelledError("The run was cancelled.")

    monkeypatch.setattr(train, "train", cancelled_mid_write)
    with pytest.raises(JobCancelledError):
        pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry))
    written: list[RunStatus] = seen["written"]
    cancelled_at = next(i for i, status in enumerate(written) if status.state is RunState.CANCELLED)
    after = [status.state for status in written[cancelled_at + 1 :]]
    assert after == [RunState.RUNNING, RunState.CANCELLED], "one stray write, then the ending put back"
    assert seen["stored"].state is RunState.CANCELLED and seen["stored"].eta_seconds is None
    assert read_run(storage).state is RunState.CANCELLED


def test_a_training_run_with_no_basis_carries_no_estimate_and_no_heartbeat(
    monkeypatch, resolved, storage, registry, clock
) -> None:
    StageStubs().install(monkeypatch)
    monkeypatch.setattr(run_progress, "train_budget_seconds", lambda config: None)
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry))
    assert all(status.estimated_pct is None for status in storage.statuses())
    assert read_status(storage).estimate_basis is None


def test_the_estimate_is_installed_on_both_flows() -> None:
    from engine.pipeline import _ScoreFlow, _TrainFlow

    assert _TrainFlow.__dict__.get("_run_progress_installed") is True
    assert _ScoreFlow.__dict__.get("_run_progress_installed") is True
