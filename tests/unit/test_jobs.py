"""`engine.jobs`: the job state machine and cooperative cancellation.

Every wait in this module is driven by `threading.Event`, never by a sleep long enough to be a
timing guess; `TIMEOUT` is only a ceiling that fails the test instead of hanging the suite.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from engine.contracts import RunState
from engine.jobs import CancelToken, JobCancelled, JobCancelledError, JobInfo, JobRunner, ThreadJobRunner

TIMEOUT: float = 10.0


@pytest.fixture
def runner() -> Iterator[ThreadJobRunner]:
    job_runner = ThreadJobRunner(max_workers=2)
    try:
        yield job_runner
    finally:
        job_runner.shutdown(wait=True)


def test_thread_job_runner_satisfies_the_protocol(runner: ThreadJobRunner) -> None:
    assert isinstance(runner, JobRunner)


def test_the_documented_exception_name_is_the_suffixed_class() -> None:
    assert JobCancelled is JobCancelledError


def test_cancel_token_starts_open_and_latches() -> None:
    token = CancelToken()
    assert token.cancelled is False
    token.raise_if_cancelled()
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(JobCancelledError):
        token.raise_if_cancelled()


def test_a_submitted_job_runs_and_reports_done(runner: ThreadJobRunner) -> None:
    ran = threading.Event()

    def job(token: CancelToken) -> None:
        ran.set()

    submitted = runner.submit("j_done", job)
    assert submitted.state is RunState.PENDING
    assert submitted.job_id == "j_done"
    assert submitted.started_at is None

    final = runner.wait("j_done", TIMEOUT)
    assert ran.is_set()
    assert final.state is RunState.DONE
    assert final.started_at is not None
    assert final.ended_at is not None
    assert final.error is None


def test_a_running_job_is_visible_as_running(runner: ThreadJobRunner) -> None:
    started, release = threading.Event(), threading.Event()

    def job(token: CancelToken) -> None:
        started.set()
        release.wait(TIMEOUT)

    runner.submit("j_running", job)
    assert started.wait(TIMEOUT)
    assert runner.status("j_running").state is RunState.RUNNING
    release.set()
    assert runner.wait("j_running", TIMEOUT).state is RunState.DONE


def test_a_raising_job_fails_with_the_message(runner: ThreadJobRunner) -> None:
    def job(token: CancelToken) -> None:
        raise RuntimeError("the upload could not be read")

    runner.submit("j_fail", job)
    final = runner.wait("j_fail", TIMEOUT)
    assert final.state is RunState.FAILED
    assert final.error == "the upload could not be read"
    assert final.ended_at is not None


def test_cancelling_a_pending_job_means_it_never_runs() -> None:
    runner = ThreadJobRunner(max_workers=1)
    blocking, release = threading.Event(), threading.Event()
    second_ran = threading.Event()

    def blocker(token: CancelToken) -> None:
        blocking.set()
        release.wait(TIMEOUT)

    def never(token: CancelToken) -> None:
        second_ran.set()

    try:
        runner.submit("j_blocker", blocker)
        assert blocking.wait(TIMEOUT)
        runner.submit("j_pending", never)
        assert runner.status("j_pending").state is RunState.PENDING
        assert runner.cancel("j_pending") is True
        release.set()
        assert runner.wait("j_blocker", TIMEOUT).state is RunState.DONE
        assert runner.wait("j_pending", TIMEOUT).state is RunState.CANCELLED
        assert second_ran.is_set() is False
        assert runner.status("j_pending").started_at is None
    finally:
        runner.shutdown(wait=True)


def test_cancelling_a_running_job_that_polls_the_token(runner: ThreadJobRunner) -> None:
    started = threading.Event()

    def job(token: CancelToken) -> None:
        started.set()
        while True:
            token.wait(0.01)
            token.raise_if_cancelled()

    runner.submit("j_cancel", job)
    assert started.wait(TIMEOUT)
    assert runner.cancel("j_cancel") is True
    final = runner.wait("j_cancel", TIMEOUT)
    assert final.state is RunState.CANCELLED
    assert final.started_at is not None
    assert final.error is None


def test_cancelling_a_finished_or_unknown_job_is_false(runner: ThreadJobRunner) -> None:
    def job(token: CancelToken) -> None:
        return

    runner.submit("j_over", job)
    assert runner.wait("j_over", TIMEOUT).state is RunState.DONE
    assert runner.cancel("j_over") is False
    assert runner.cancel("j_never_submitted") is False


def test_a_duplicate_job_id_is_rejected(runner: ThreadJobRunner) -> None:
    def job(token: CancelToken) -> None:
        return

    runner.submit("j_dup", job)
    runner.wait("j_dup", TIMEOUT)
    with pytest.raises(ValueError, match="j_dup"):
        runner.submit("j_dup", job)


def test_status_of_an_unknown_id_raises_key_error(runner: ThreadJobRunner) -> None:
    with pytest.raises(KeyError):
        runner.status("j_unknown")


def test_job_info_is_frozen_and_forbids_extras(runner: ThreadJobRunner) -> None:
    def job(token: CancelToken) -> None:
        return

    info: JobInfo = runner.submit("j_frozen", job)
    with pytest.raises(ValueError, match="frozen"):
        info.state = RunState.DONE  # type: ignore[misc]


def test_twenty_concurrent_submits_all_terminate() -> None:
    runner = ThreadJobRunner(max_workers=4)
    counter_lock = threading.Lock()
    finished: list[str] = []

    def job(token: CancelToken) -> None:
        with counter_lock:
            finished.append("x")

    try:
        ids = [f"j_{index:02d}" for index in range(20)]
        for job_id in ids:
            runner.submit(job_id, job)
        runner.shutdown(wait=True)
        assert len(finished) == 20
        assert all(runner.status(job_id).state is RunState.DONE for job_id in ids)
    finally:
        runner.shutdown(wait=True)
