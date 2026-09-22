"""Background jobs: the `JobRunner` protocol and a thread-pool implementation.

Cancellation is cooperative (DEC-017): every job function receives a `CancelToken` and calls
`raise_if_cancelled()` between steps. A job that has not started yet is cancelled outright.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from engine.contracts import RunState
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

_LOGGER = get_logger(__name__)

_TERMINAL_STATES: frozenset[RunState] = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


class JobInfo(BaseModel):
    """What the API can tell a caller about a submitted job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    state: RunState
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


class JobCancelledError(Exception):
    """Raised inside a job function by `CancelToken.raise_if_cancelled()` once cancellation is requested."""


# This exception was first named `JobCancelled`; ruff's N818 wants the `Error` suffix (erratum E2),
# so the suffixed name is canonical and this alias keeps the shorter name importable for callers
# that already spell it that way.
JobCancelled = JobCancelledError


class CancelToken:
    """One per job; stages poll it between steps so a run stops at a clean boundary."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Ask the job to stop; returns immediately, the job stops at its next poll."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once `cancel()` has been called."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise `JobCancelledError` when cancellation has been requested, else do nothing."""
        if self._event.is_set():
            raise JobCancelledError("The run was cancelled.")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancellation is requested or `timeout` elapses; True when cancelled."""
        return self._event.wait(timeout)


JobFn = Callable[[CancelToken], None]


@runtime_checkable
class JobRunner(Protocol):
    """Runs pipeline work off the request thread; a SageMaker-backed implementation lands in Phase 4."""

    def submit(self, job_id: str, fn: JobFn) -> JobInfo: ...

    def status(self, job_id: str) -> JobInfo: ...

    def cancel(self, job_id: str) -> bool: ...

    def shutdown(self, *, wait: bool = True) -> None: ...


class ThreadJobRunner:
    """`concurrent.futures.ThreadPoolExecutor` plus one `CancelToken` per job.

    A pending job that is cancelled never starts and reports `cancelled`; a running job is asked to
    stop and reports `cancelled` once `JobCancelledError` propagates; any other exception leaves the
    job `failed` with `error=str(exc)`. All state lives behind one lock and `status()` never blocks
    on the job itself.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="marketing-ai-job")
        self._lock = threading.Lock()
        self._jobs: dict[str, JobInfo] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._done: dict[str, threading.Event] = {}

    def submit(self, job_id: str, fn: JobFn) -> JobInfo:
        """Queue `fn`; raises `ValueError` when `job_id` was submitted before."""
        token = CancelToken()
        finished = threading.Event()
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"Job {job_id!r} has already been submitted.")
            info = JobInfo(job_id=job_id, state=RunState.PENDING, submitted_at=utc_now())
            self._jobs[job_id] = info
            self._tokens[job_id] = token
            self._done[job_id] = finished
        future: Future[None] = self._executor.submit(self._run, job_id, fn, token)
        with self._lock:
            self._futures[job_id] = future
        return info

    def status(self, job_id: str) -> JobInfo:
        """The job's current state; raises `KeyError` for an unknown id."""
        with self._lock:
            return self._jobs[job_id]

    def cancel(self, job_id: str) -> bool:
        """True iff the job was pending or running; False for a finished or unknown id. Never raises."""
        with self._lock:
            info = self._jobs.get(job_id)
            if info is None or info.state in _TERMINAL_STATES:
                return False
            token = self._tokens[job_id]
            future = self._futures.get(job_id)
        token.cancel()
        if future is not None and future.cancel():
            self._finish(job_id, RunState.CANCELLED)
        return True

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work; with `wait=True` block until every running job has finished."""
        self._executor.shutdown(wait=wait)

    def wait(self, job_id: str, timeout: float | None = None) -> JobInfo:
        """Block until the job reaches a terminal state and return it (test helper, not in the Protocol)."""
        with self._lock:
            finished = self._done[job_id]
        if not finished.wait(timeout):
            raise TimeoutError(f"Job {job_id!r} did not finish within {timeout} seconds.")
        return self.status(job_id)

    def _run(self, job_id: str, fn: JobFn, token: CancelToken) -> None:
        if token.cancelled:
            self._finish(job_id, RunState.CANCELLED)
            return
        self._update(job_id, state=RunState.RUNNING, started_at=utc_now())
        try:
            fn(token)
        except JobCancelledError:
            self._finish(job_id, RunState.CANCELLED)
        except Exception as exc:  # the state machine records every failure; the traceback goes to the log
            _LOGGER.exception("job %s failed", job_id)
            self._finish(job_id, RunState.FAILED, error=str(exc))
        else:
            self._finish(job_id, RunState.DONE)

    def _update(self, job_id: str, **changes: Any) -> JobInfo:
        with self._lock:
            info = self._jobs[job_id].model_copy(update=changes)
            self._jobs[job_id] = info
            return info

    def _finish(self, job_id: str, state: RunState, *, error: str | None = None) -> None:
        changes: Mapping[str, Any] = {"state": state, "ended_at": utc_now(), "error": error}
        with self._lock:
            current = self._jobs[job_id]
            if current.state in _TERMINAL_STATES:
                return
            self._jobs[job_id] = current.model_copy(update=dict(changes))
            self._done[job_id].set()
