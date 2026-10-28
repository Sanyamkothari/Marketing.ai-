"""The check of the whole file runs as a background job: Approve waits for it, the screen says where it stands,
and the same plan is counted once."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from engine.doctor import visit
from engine.doctor.case import Case, CaseError, CaseStore, VisitState
from engine.doctor.full_count import FullCountRunner, full_count_of
from engine.doctor.screen import build_screen
from engine.doctor.visit import VisitDeps, active_section, plan_basis
from engine.jobs import ThreadJobRunner
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT


class _Gate:
    """A whole-file read that waits to be released, and counts how often it was asked."""

    def __init__(self, loader: Any) -> None:
        self.loader = loader
        self.release = threading.Event()
        self.reads = 0

    def __call__(self, case: Case) -> Any:
        self.reads += 1
        assert self.release.wait(10), "the test never released the read"
        return self.loader(case)


def _walk(tmp_path: Path) -> tuple[LocalStorage, VisitDeps, Case, ThreadJobRunner, _Gate]:
    storage = LocalStorage(tmp_path)
    w = vs.world()
    jobs = ThreadJobRunner(max_workers=1)
    gate = _Gate(w.load)
    deps = vs.deps_for(storage, w, full_count=FullCountRunner(storage, jobs), load_full=gate)
    case = vs.open_case(storage, w)
    for _name, _op, act in vs.SCRIPT[:4]:
        case = act(
            vs.deps_for(storage, w, full_count=deps.full_count, load_full=gate),
            CaseStore(storage).get(CLIENT, case.case_id),
        )
    return storage, deps, case, jobs, gate


def _check_of(storage: LocalStorage, case: Case) -> Any:
    return build_screen(storage, CLIENT, case.case_id, config_root=None).view.full_check


def test_approve_waits_for_the_background_check_and_takes_its_result(tmp_path: Path) -> None:
    storage, deps, case, jobs, gate = _walk(tmp_path)
    try:
        assert case.state is VisitState.APPROVE
        # the visit rests at Approve at once, with the sample's count; the whole-file count is a job
        assert case.visit is not None and case.visit.plan_check is not None
        assert case.visit.plan_check.basis is None
        shown = _check_of(storage, case)
        assert shown is not None and shown.state in {"pending", "running"}
        with pytest.raises(CaseError) as refused:
            visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
        assert refused.value.code == "FULL_CHECK_RUNNING"

        gate.release.set()
        items, _ = active_section(case)
        record = full_count_of(storage, case, plan_basis(case, items))
        assert record is not None
        jobs.wait(record.job_id, 10)
        assert _check_of(storage, case).state == "running"  # done, but not yet taken into the case

        case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
        assert case.visit is not None and case.visit.plan_check is not None
        assert case.visit.plan_check.basis == plan_basis(case, items)
        shown = _check_of(storage, case)
        assert shown is not None and shown.state == "done"
        reads = gate.reads  # (a plan that changed on the way to Approve was counted for itself too)
        again = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
        assert again.version == case.version and gate.reads == reads  # the same plan is counted once

        approved = visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
        assert approved.approvals
    finally:
        gate.release.set()
        jobs.shutdown()


def test_a_failed_check_is_said_and_approve_tries_it_again(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    w = vs.world()
    jobs = ThreadJobRunner(max_workers=1)
    broken = {"on": True}

    def load_full(case: Case) -> Any:
        if broken["on"]:
            raise RuntimeError("disk gone")
        return w.load(case)

    deps = vs.deps_for(storage, w, full_count=FullCountRunner(storage, jobs), load_full=load_full)
    try:
        case = vs.open_case(storage, w)
        for _name, _op, act in vs.SCRIPT[:4]:
            case = act(deps, CaseStore(storage).get(CLIENT, case.case_id))
        items, _ = active_section(case)
        first = full_count_of(storage, case, plan_basis(case, items))
        assert first is not None
        jobs.wait(first.job_id, 10)
        shown = _check_of(storage, case)
        assert shown is not None and shown.state == "failed"
        broken["on"] = False
        with pytest.raises(CaseError) as refused:
            visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
        assert refused.value.code == "FULL_CHECK_RUNNING"  # approving started it again
        retry = full_count_of(storage, case, plan_basis(case, items))
        assert retry is not None and retry.job_id != first.job_id
        jobs.wait(retry.job_id, 10)
        case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
        assert case.visit is not None and case.visit.plan_check is not None
        assert case.visit.plan_check.basis is not None
    finally:
        jobs.shutdown()


class _Jobs:
    """A job runner that records submits and cancels and runs nothing (thread-safe enough for the test)."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancelled: list[str] = []

    def submit(self, job_id: str, fn: Any) -> None:
        self.submitted.append(job_id)

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    def status(self, job_id: str) -> Any:
        from types import SimpleNamespace

        from engine.contracts import RunState

        if job_id in self.submitted:
            return SimpleNamespace(state=RunState.RUNNING)
        raise KeyError(job_id)


def test_two_requests_start_one_count(tmp_path: Path) -> None:
    storage, _deps, case, jobs, gate = _walk(tmp_path)
    gate.release.set()
    jobs.shutdown()
    items, _ = active_section(case)
    fake = _Jobs()
    barrier = threading.Barrier(2)

    def ensure() -> None:
        barrier.wait()
        # a new runner per request, as the route builds it
        FullCountRunner(storage, fake).ensure(case, items, "basis-two", load_full=gate)  # type: ignore[arg-type]

    threads = [threading.Thread(target=ensure) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(fake.submitted) == 1


def test_a_pending_record_just_written_is_alive_but_a_running_unknown_job_is_failed(tmp_path: Path) -> None:
    from engine.doctor.full_count import FullCount
    from engine.utils.time import utc_now

    storage, _deps, case, jobs, gate = _walk(tmp_path)
    gate.release.set()
    jobs.shutdown()
    runner = FullCountRunner(storage, _Jobs())  # type: ignore[arg-type]
    young = FullCount(basis="b-young", job_id="unknown-1", state="pending", started_at=utc_now())
    runner._write(case, young)
    assert runner.current(case, "b-young").state == "pending"  # type: ignore[union-attr]
    lost = FullCount(basis="b-lost", job_id="unknown-2", state="running", started_at=utc_now())
    runner._write(case, lost)
    gone = runner.current(case, "b-lost")
    assert gone is not None and gone.state == "failed"
    assert full_count_of(storage, case, "b-lost").state == "failed"  # type: ignore[union-attr]


def test_a_changed_plan_cancels_the_old_count(tmp_path: Path) -> None:
    storage, _deps, case, jobs, gate = _walk(tmp_path)
    gate.release.set()
    jobs.shutdown()
    items, _ = active_section(case)
    fake = _Jobs()
    runner = FullCountRunner(storage, fake)  # type: ignore[arg-type]
    old = runner.ensure(case, items, "plan-old", load_full=gate)
    new = runner.ensure(case, items, "plan-new", load_full=gate)
    assert fake.cancelled == [old.job_id] and new.job_id in fake.submitted
    assert full_count_of(storage, case, "plan-new").basis == "plan-new"  # type: ignore[union-attr]


def test_steps_done_grows_to_steps_total(tmp_path: Path) -> None:
    storage, _deps, case, jobs, gate = _walk(tmp_path)
    gate.release.set()
    items, _ = active_section(case)
    template = next(i for i in items if i.kind == "key")
    items = (
        *items,
        *(
            template.model_copy(
                update={
                    "item_id": f"recipe_step.{column}",
                    "kind": "recipe_step",
                    "payload": {"step": {"order": 1, "kind": kind, "column": column, "params": {}}},
                }
            )
            for kind, column in (("normalise_text", "account_no"), ("drop_column", "visits_90d"))
        ),
    )
    seen: list[tuple[str, int, int]] = []

    class _Watched(FullCountRunner):
        def _write(self, case: Case, count: Any) -> None:
            seen.append((count.state, count.steps_done, count.steps_total))
            super()._write(case, count)

    try:
        record = _Watched(storage, jobs).ensure(case, items, "plan-steps", load_full=gate)
        jobs.wait(record.job_id, 10)
    finally:
        jobs.shutdown()
    total = record.steps_total
    assert total == 2
    done = [n for state, n, _ in seen if state == "running"]
    assert done == sorted(done) and seen[-1] == ("done", total, total)
    assert max(done) == total
