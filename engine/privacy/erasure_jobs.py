"""Erasure as a background job (Plan D M54, DEC-863).

Finding a person means reading every artefact in the store, and erasing them means rewriting every
file they are in: minutes on a real deployment, far longer than an HTTP request should be held open.
So `POST /privacy/erasure` writes the request row as `queued`, hands the work to `ErasureJobs`, and
answers `202` with the request id; `GET /privacy/erasure/{id}` shows the per-store progress and, at
the end, the completion report.

**One worker, and the store rewrite lock.** Erasure and retention both rewrite the artefact store,
and two rewrites at once could each read a file the other is about to rewrite (DEC-749); the job
takes the same process-wide lock the retention routes take. One worker means requests are erased in
the order they were made.

**Retries.** Within a job, a store whose files cannot be written is tried again `max_attempts` times
with a growing wait (`backoff`), and the other stores carry on meanwhile; a store still failing ends
the request `failed` with `ERASURE_STORE_FAILED` (`engine.privacy.erasure.erase`). An Admin can then
retry the request: the id is given again in the body (it is never stored - only its salted hash, which
the retry is checked against) and the job re-finds whatever still holds the person, which is exactly
what the failed stores left behind. The route claims the request first, moving it from `failed` to
`queued` in one conditional statement, so two retries at once start one job (DEC-869).

**A restart.** The jobs live in this process's memory, so a request still `queued` or `in_progress`
when the API starts belonged to a process that stopped: `fail_interrupted` marks it `failed` with
`ERASURE_INTERRUPTED` at start-up, and it can be retried (DEC-869).

**Audited at start and at end.** The start is the audit middleware's event for the `POST`; the end is
a `privacy.erasure.complete` event this job appends with the outcome's counts - or its failure code -
under the same request id, by the Admin who asked. Neither holds the id.

**The id lives only in the job's memory**, from the request until the job ends, exactly as long as the
synchronous version held it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Final

from sqlalchemy.engine import Engine

from engine.access.roles import Principal
from engine.audit.events import AuditLog
from engine.privacy.erasure import erase, fail_if_unfinished
from engine.privacy.errors import PrivacyError
from engine.storage import Storage
from engine.utils.logging import get_logger

__all__ = ["COMPLETE_ACTION", "ErasureJobs", "default_backoff"]

_LOGGER = get_logger(__name__)

COMPLETE_ACTION: Final[str] = "privacy.erasure.complete"
"""The audit action of the event a finished job appends (the start is the route's `privacy.erasure`)."""

DEFAULT_MAX_ATTEMPTS: Final[int] = 3


def default_backoff(attempt: int) -> float:
    """Seconds to wait after failed attempt `attempt` of a store: 2, 8, 30, 30, …"""
    return float(min(30, 2 * 4 ** (attempt - 1)))


class ErasureJobs:
    """Runs erasure requests one at a time on a background thread."""

    def __init__(
        self,
        *,
        rewrite_lock: threading.Lock,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff: Callable[[int], float] = default_backoff,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lock = rewrite_lock
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._sleep = sleep
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="erasure")
        self._futures: dict[str, Future[None]] = {}
        self._guard = threading.Lock()

    def submit(
        self,
        *,
        request_id: str,
        storage: Storage,
        principal_id: str,
        engine: Engine,
        principal: Principal,
        salt: str,
        client_id: str | None,
        config_root: Path | None,
        audit_log: AuditLog | None,
        history_all_clients: bool,
    ) -> None:
        """Queue the erasure of an already-`queued` request row (a retry re-queues it first, DEC-869)."""

        def run() -> None:
            try:
                with self._lock:
                    erase(
                        storage,
                        principal_id,
                        engine=engine,
                        principal=principal,
                        salt=salt,
                        client_id=client_id,
                        config_root=config_root,
                        audit_log=audit_log,
                        request_id=request_id,
                        history_all_clients=history_all_clients,
                        max_attempts=self._max_attempts,
                        wait=lambda attempt: self._sleep(self._backoff(attempt)),
                        resume=True,
                        audit_action=COMPLETE_ACTION,
                    )
            except PrivacyError as exc:  # recorded on the request row and in the audit trail by `erase`
                _LOGGER.error("privacy.erasure job request=%s code=%s", request_id, exc.code)
                if exc.code != "ERASURE_NOT_RETRYABLE":  # not queued: the row is another job's to finish
                    fail_if_unfinished(engine, request_id, exc.code)
            except Exception as exc:
                _LOGGER.error("privacy.erasure job request=%s error=%s", request_id, type(exc).__name__)
                fail_if_unfinished(engine, request_id, "ERASURE_FAILED")

        with self._guard:
            self._futures[request_id] = self._executor.submit(run)

    def wait(self, request_id: str, timeout: float | None = None) -> None:
        """Block until the job for `request_id` has finished (tests, scripts). Unknown ids return at once."""
        with self._guard:
            future = self._futures.get(request_id)
        if future is not None:
            future.result(timeout=timeout)

    def shutdown(self) -> None:
        """Finish the queued jobs and stop the worker."""
        self._executor.shutdown(wait=True)
