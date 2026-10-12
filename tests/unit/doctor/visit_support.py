"""Shared set-up for the visit's tests (Plan O M235): files, a store, deps, and a script that walks every state.

The files are in memory (`LoadedFile`, as the Doctor reads an upload); the case store is a real one on disk. The
script drives one case through all thirteen states of the visit with no AI service:

    INTAKE -> UNDERSTAND -> RECALL -> TREAT -> ASK (which column names each customer?) -> TREAT -> PREVIEW
    -> APPROVE -> GOAL_BRIEF (targeted advertising) -> TREAT -> ASK (which column is the outcome?) -> ...
    -> CONVERSE (no AI) -> TREAT -> ... -> APPROVE -> PRESCRIBE -> VERIFY -> HAND_OFF -> FOLLOW_UP
    -> INTAKE (next month's file) -> ... -> APPROVE

`python -m tests.unit.doctor.visit_support <data dir> <step> <kill-at>` runs the script in a child process that
dies with `os._exit` at its `kill-at`-th write of `case.json` (after the write, before the timeline): the
real-process half of "kill the process at every state".
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from engine.config import UseCaseConfig, load_use_case
from engine.doctor import visit
from engine.doctor.case import Case, CaseFile, CaseReadinessRef, CaseStore
from engine.doctor.diagnose import LoadedFile, Offer
from engine.doctor.goal_brief import GoalBrief
from engine.doctor.visit import VerifyResult, VisitDeps
from engine.stages import ingest
from engine.storage import LocalStorage

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
CLIENT = "default"
ANALYST = "u-analyst"
GOAL_A = "targeted-advertisement"
GOAL_B = "telco-churn"
FIRST = "up-customers-oct"
SECOND = "up-customers-nov"


def customers(rows: int = 1_200, seed: int = 0) -> pd.DataFrame:
    """One row per customer, with two columns that each hold a different value on every row (so which one names
    the customer is a question), an outcome for each of two goals under a synonym, a date and consent."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "customer_id": [f"C{100000 + i}" for i in range(rows)],
            "account_no": [f"AC{900000 - 7 * i}" for i in range(rows)],
            "age": rng.integers(18, 80, rows),
            "monthly_spend": np.round(rng.gamma(2.0, 400.0, rows), 2),
            "visits_90d": rng.integers(0, 30, rows),
            "converted": rng.integers(0, 2, rows),
            "churned": rng.integers(0, 2, rows),
            "snapshot_date": ["2026-09-30"] * rows,
            "marketing_opt_in": rng.integers(0, 2, rows),
        }
    )


def loaded(upload_id: str, file_name: str, frame: pd.DataFrame, config: UseCaseConfig) -> LoadedFile:
    profile = ingest.profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name=file_name,
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )
    return LoadedFile(upload_id=upload_id, file_name=file_name, frame=frame, profile=profile)


@dataclass
class World:
    """The files on offer, the offers, a clock that moves one second a call."""

    files: dict[str, LoadedFile]
    offers: tuple[Offer, ...]
    verdict: str = "ready"
    ticks: int = 0

    def clock(self) -> datetime:
        self.ticks += 1
        return T0 + timedelta(seconds=self.ticks)

    def load(self, case: Case) -> Sequence[LoadedFile]:
        return tuple(self.files[upload_id] for upload_id in case.upload_ids())

    def verify(self, case: Case, brief: GoalBrief | None, files: tuple[LoadedFile, ...]) -> VerifyResult:
        assert brief is not None
        ref = CaseReadinessRef(
            goal_id=brief.goal_id, report_hash="r" * 64, verdict=self.verdict, created_at=T0
        )
        return VerifyResult(verdict=self.verdict, reason="as the test says", ref=ref)


_WORLD: dict[str, World] = {}


def world() -> World:
    """One set of files per process (profiling them is the slow part)."""
    if "w" not in _WORLD:
        a = load_use_case(GOAL_A)
        b = load_use_case(GOAL_B)
        _WORLD["w"] = World(
            files={
                FIRST: loaded(FIRST, "customers_oct.csv", customers(seed=1), a),
                SECOND: loaded(SECOND, "customers_nov.csv", customers(seed=2), a),
            },
            offers=(Offer(config=a, stage="Acquire"), Offer(config=b, stage="Retain")),
        )
    w = _WORLD["w"]
    w.ticks = 0
    w.verdict = "ready"
    return w


def deps_for(storage: LocalStorage, w: World, **extra: Any) -> VisitDeps:
    return VisitDeps(
        storage=storage,
        load_files=w.load,
        offers=w.offers,
        verify=w.verify,
        clock=w.clock,
        store=CaseStore(storage, clock=w.clock, lock_stale_seconds=extra.pop("stale", 30.0)),
        **extra,
    )


def case_file(upload_id: str, w: World) -> CaseFile:
    return CaseFile(
        upload_id=upload_id, file_name=w.files[upload_id].file_name, added_at=T0, added_by=ANALYST
    )


def open_case(storage: LocalStorage, w: World) -> Case:
    return CaseStore(storage, clock=w.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=[case_file(FIRST, w)]
    )


def question(case: Case, code: str) -> Any:
    return next(q for q in case.questions if q.code == code and q.answer is None)


def option(q: Any, column: str) -> str:
    return str(next(o.option_id for o in q.options if o.label.split(" (")[0] == column))


Action = Callable[[VisitDeps, Case], Case]


def _answer(code: str, column: str, op: str) -> Action:
    def act(deps: VisitDeps, case: Case) -> Case:
        q = question(case, code)
        return visit.answer(
            CLIENT, case.case_id, q.question_id, option(q, column), deps,
            expected_version=case.version, actor=ANALYST, op_id=op,
        )  # fmt: skip

    return act


def _goal(goal_id: str | None, op: str) -> Action:
    return lambda deps, case: visit.set_goal(
        CLIENT, case.case_id, goal_id, deps, expected_version=case.version, actor=ANALYST, op_id=op
    )


def _approve(op: str) -> Action:
    return lambda deps, case: visit.approve(
        CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST, op_id=op
    )


def _chat(text: str, op: str) -> Action:
    return lambda deps, case: visit.chat(
        CLIENT, case.case_id, text, deps, expected_version=case.version, actor=ANALYST, op_id=op
    )


def _next_month(op: str) -> Action:
    return lambda deps, case: visit.add_files(
        CLIENT, case.case_id, [case_file(SECOND, world_now())], deps,
        expected_version=case.version, actor=ANALYST, op_id=op,
    )  # fmt: skip


def world_now() -> World:
    return _WORLD["w"]


def _resume(deps: VisitDeps, case: Case) -> Case:
    return visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)


SCRIPT: tuple[tuple[str, str | None, Action], ...] = (
    ("resume", None, _resume),
    ("answer key", "op-1", _answer("KEY_AMBIGUOUS", "customer_id", "op-1")),
    ("goal A", "op-2", _goal(GOAL_A, "op-2")),
    ("answer outcome", "op-3", _answer("OUTCOME_COLUMN", "converted", "op-3")),
    ("chat", "op-4", _chat("What is in this file? Call me on 98765 43210.", "op-4")),
    ("approve", "op-5", _approve("op-5")),
    ("next month", "op-6", _next_month("op-6")),
    ("answer key again", "op-7", _answer("KEY_AMBIGUOUS", "customer_id", "op-7")),
)
"""Every state of the visit, with no AI service connected. Each person's action has its idempotency key."""


def run_script(storage: LocalStorage, w: World, case_id: str, *, stale: float = 30.0) -> Case:
    """Run the script; an action already written (its key is in the case) is not sent again, as a client that
    reloads the case after a restart would not."""
    case = CaseStore(storage).get(CLIENT, case_id)
    for _name, op, act in SCRIPT:
        case = CaseStore(storage).get(CLIENT, case_id)
        if op is not None and case.visit is not None and op in case.visit.ops:
            continue
        case = act(deps_for(storage, w, stale=stale), case)
    return case


def summary(case: Case) -> dict[str, Any]:
    """What must be equal between a run that was killed and one that was not: everything but the clock."""
    return {
        "state": case.state.value,
        "version": case.version,
        "files": [f.model_dump(exclude={"added_at"}) for f in case.files],
        "goals": [g.model_dump(exclude={"set_at"}) for g in case.goals],
        "items": [i.model_dump(exclude={"proposed_at", "decided_at"}) for i in case.items],
        "questions": [q.model_dump(exclude={"answered_at"}) for q in case.questions],
        "doubts": [d.model_dump() for d in case.doubts],
        "approvals": [a.model_dump(exclude={"approved_at"}) for a in case.approvals],
        "evidence": sorted(e.evidence_id for e in case.evidence),
        "prescriptions": [p.prescription_hash for p in case.prescriptions],
        "readiness": [r.model_dump(exclude={"created_at"}) for r in case.readiness],
        "chat": [(c.role, c.text) for c in case.chat],
        "diagnosis": case.diagnosis,
        "visit": None if case.visit is None else case.visit.model_dump(),
        "counters": case.counters.model_dump(exclude={"tool_calls"}),
    }


def timeline_summary(storage: LocalStorage, case: Case) -> list[tuple[Any, ...]]:
    return [
        (e.version, e.kind.value, e.item_id, e.upload_id, tuple(sorted(e.details.items())))
        for e in CaseStore(storage).timeline(CLIENT, case.case_id)
    ]


class _Dying(LocalStorage):
    """A store whose process dies (`os._exit`) right after its `kill_at`-th write of `case.json`."""

    kill_at = 0
    writes = 0

    def write_bytes(self, key: str, data: bytes) -> None:
        super().write_bytes(key, data)
        if key.endswith("/case.json"):
            type(self).writes += 1
            if type(self).writes == type(self).kill_at:
                os._exit(17)


def main(argv: list[str]) -> int:
    """Child process: open (or reload, resume and continue) the case, dying at its `kill_at`-th write."""
    data, kill_at = Path(argv[0]), int(argv[1])
    _Dying.kill_at = kill_at
    storage = _Dying(data)
    w = world()
    found = CaseStore(storage).list(CLIENT)
    case = found[0] if found else open_case(storage, w)
    if found:  # a restart: the lock the dead process left is broken at once (`stale`), then the visit resumes
        visit.resume(CLIENT, case.case_id, deps_for(storage, w, stale=0.0), actor=ANALYST)
    print(run_script(storage, w, case.case_id, stale=0.0).state.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
