"""The whole-file count of a plan, as a background job (roadmap 2.3).

Counting the accepted steps on every row of the files takes seconds on a big file (about eight on the 541,909-row
Online Retail file, see docs/plans/DATA_DOCTOR_SCALE.md), too long for a request. :class:`FullCountRunner` runs it on
the job runner (:mod:`engine.jobs`) and keeps what it makes next to the case (`cases/<client>/<case>/full_count/`), by the plan's `basis` (the plan digest
with the as-of date it is counted under, `PlanCheck.basis`): the same plan is never counted twice, a changed plan is a
new count, and a count that is still running for an old plan is simply never read.

Until the count is done the visit rests with the count of a sample (`PlanCheck.basis` None) and the screen says the
whole-file check is pending, running or done (:class:`FullCount`, `view.full_check`). Approve waits for the result:
it is refused (`FULL_CHECK_RUNNING`) until the count of the whole file is there. The stored record holds counts, codes
and column names only, never a cell value.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.contracts import RunState
from engine.doctor.case import Case, CaseItem, case_dir
from engine.doctor.diagnose import LoadedFile
from engine.doctor.guards import Receipt
from engine.doctor.materialize import Full, materialize
from engine.doctor.plan_check import plan_check_of, recipe_steps_of
from engine.doctor.visit_contracts import PlanCheck, PlanFinding
from engine.jobs import CancelToken, JobRunner
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = ["FullCount", "FullCountRunner", "full_count_key", "full_count_of"]

_LOGGER = get_logger(__name__)
FULL_COUNT_DIRNAME: Final[str] = "full_count"
RECEIPTS_DIRNAME: Final[str] = "receipts"
FullCountState = Literal["pending", "running", "done", "failed"]


class FullCount(StrictBase):
    """Where the whole-file count of one plan stands: the last one asked for in the case."""

    basis: str = Field(description="The plan it counts (`PlanCheck.basis`).")
    job_id: str
    state: FullCountState
    steps_done: int = Field(default=0, description="Steps counted so far, of `steps_total`.")
    steps_total: int = 0
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    error: str | None = Field(default=None, description="Why it failed, in words (no cell value).")
    check: PlanCheck | None = Field(default=None, description="The count, once `state` is done.")


def full_count_key(client_id: str, case_id: str, basis: str) -> str:
    """One record per plan (`basis`), so a count still running for an old plan never writes over the new one's."""
    return f"{case_dir(client_id, case_id)}{FULL_COUNT_DIRNAME}/{basis}.json"


def full_count_of(storage: Storage, case: Case, basis: str) -> FullCount | None:
    """The stored count of the case's plan `basis`, or None when none was asked for (or it cannot be read)."""
    key = full_count_key(case.client_id, case.case_id, basis)
    try:
        return storage.read_model(key, FullCount) if storage.exists(key) else None
    except (StorageError, ValueError):
        return None


class StoredReceipts:
    """The receipts of the steps counted on the whole file, kept next to the case by what each step read
    (:class:`engine.doctor.guards.ReceiptMemo`): a plan that changed one step counts only that step and the ones
    after it again. Counts, codes and column names only. A record that cannot be read is simply counted again.
    """

    def __init__(self, storage: Storage, case: Case) -> None:
        self._storage = storage
        self._prefix = f"{case_dir(case.client_id, case.case_id)}{FULL_COUNT_DIRNAME}/{RECEIPTS_DIRNAME}/"

    def get(self, key: str) -> Receipt | None:
        name = f"{self._prefix}{key}.json"
        try:
            return self._storage.read_model(name, Receipt) if self._storage.exists(name) else None
        except (StorageError, ValueError):
            return None

    def put(self, key: str, receipt: Receipt) -> None:
        try:
            self._storage.write_model(f"{self._prefix}{key}.json", receipt)
        except StorageError:
            _LOGGER.warning("a step receipt could not be kept; it is counted again next time")


Extra = Callable[[Case, Sequence[LoadedFile]], tuple[PlanFinding, ...]]
"""Flags read on the whole files beside the plan's count, added to its findings."""


class FullCountRunner:
    """Starts, follows and reads the whole-file counts of a store's cases on a :class:`JobRunner`."""

    def __init__(self, storage: Storage, jobs: JobRunner) -> None:
        self._storage = storage
        self._jobs = jobs
        self._lock = threading.Lock()

    def current(self, case: Case, basis: str) -> FullCount | None:
        """The stored count for `basis`; a count recorded as running by a process that is gone reads as failed."""
        found = full_count_of(self._storage, case, basis)
        if found is None:
            return None
        if found.state in {"pending", "running"} and not self._alive(found.job_id):
            found = found.model_copy(
                update={
                    "state": "failed",
                    "ended_at": utc_now(),
                    "error": "The check was interrupted before it finished.",
                }
            )
            self._write(case, found)  # so the screen, which has no runner, says so too
        return found

    def ensure(
        self,
        case: Case,
        items: Sequence[CaseItem],
        basis: str,
        *,
        load_full: Callable[[Case], Sequence[LoadedFile]],
        retry: bool = False,
        extra: Extra | None = None,
    ) -> FullCount:
        """The count for `basis`, started when none is under way or done. A failed one starts again only with
        `retry` (Approve is the retry), so a screen that is read again and again does not loop on a failure.
        `extra` adds flags read on the same whole files to the count (the history check of `engine.doctor.history`).
        """
        with self._lock:
            found = self.current(case, basis)
            if found is not None and (found.state != "failed" or not retry):
                return found
            total = sum(
                len(recipe_steps_of(items, f.upload_id, only_file=len(case.files) == 1)) for f in case.files
            )
            fresh = FullCount(
                basis=basis,
                job_id=f"fullcount-{case.case_id}-{uuid.uuid4().hex[:12]}",
                state="pending",
                steps_total=total,
                started_at=utc_now(),
            )
            self._cancel_others(case, basis)
            self._write(case, fresh)
            self._jobs.submit(fresh.job_id, self._job(case, tuple(items), fresh, load_full, extra))
            return fresh

    def _cancel_others(self, case: Case, basis: str) -> None:
        """The plan changed: a count still under way for another plan is of no use, so it is stopped."""
        prefix = full_count_key(case.client_id, case.case_id, "")
        for key in self._storage.list_keys(prefix):
            other = key.removeprefix(prefix).removesuffix(".json")
            if "/" in other:  # the kept step receipts, not a count
                continue
            found = full_count_of(self._storage, case, other) if other != basis else None
            if found is not None and found.state in {"pending", "running"}:
                self._jobs.cancel(found.job_id)

    def _alive(self, job_id: str) -> bool:
        try:
            return self._jobs.status(job_id).state in {RunState.PENDING, RunState.RUNNING}
        except KeyError:
            return False

    def _write(self, case: Case, count: FullCount) -> None:
        self._storage.write_model(full_count_key(case.client_id, case.case_id, count.basis), count)

    def _job(
        self,
        case: Case,
        items: tuple[CaseItem, ...],
        count: FullCount,
        load_full: Callable[[Case], Sequence[LoadedFile]],
        extra: Extra | None = None,
    ) -> Callable[[CancelToken], None]:
        def run(token: CancelToken) -> None:
            state = count.model_copy(update={"state": "running"})
            self._write(case, state)
            done = 0

            def counted(*_: object) -> None:  # after each step of each file
                nonlocal done, state
                token.raise_if_cancelled()
                done = min(done + 1, state.steps_total)
                state = state.model_copy(update={"steps_done": done})
                self._write(case, state)

            try:
                files = load_full(case)
                prepared = materialize(
                    case,
                    items,
                    files,
                    Full(),
                    watch=counted,
                    memo=StoredReceipts(self._storage, case),
                )
                check = plan_check_of(prepared, basis=count.basis)
                added = extra(case, files) if extra is not None else ()
                if added:
                    check = check.model_copy(update={"findings": (*check.findings, *added)})
            except Exception as exc:  # the record says it failed; the traceback goes to the log
                _LOGGER.exception("whole-file count %s failed", count.job_id)
                self._write(
                    case,
                    state.model_copy(
                        update={"state": "failed", "ended_at": utc_now(), "error": type(exc).__name__}
                    ),
                )
                raise
            self._write(
                case,
                state.model_copy(
                    update={
                        "state": "done",
                        "steps_done": state.steps_total,
                        "ended_at": utc_now(),
                        "check": check,
                    }
                ),
            )

        return run
