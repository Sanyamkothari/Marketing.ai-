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
