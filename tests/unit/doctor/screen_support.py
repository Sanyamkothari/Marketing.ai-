"""Real cases in every shape the Data Doctor screen draws (Plan O M247, DEC-1817).

Each scenario drives a real case through the real visit and protocol (M235, M246) on a store on disk, with the
fake detectors of M246's tests (in the shape M245's will have), and returns `engine.doctor.screen.build_screen`'s
answer: exactly what `GET /cases/{case_id}/screen` serves. `tests/unit/doctor/test_screen.py` checks the view; the
jsdom suites of the screen (`tests/integration/datadoctor/test_data_doctor_ui.py`) draw these answers, so a shape
the screen draws is one the engine really produces.

    empty           a case with no file yet
    question        the key question (with "I don't know"), a detector's yes/no question with what each answer
                    changes, a doubt in three parts and a request for more data
    assumed         a small ambiguity decided by the rules, with the assumption stated and revocable
    approve         everything answered: waiting at Approve, with the exact sentence
    approve_class   a goal chosen; a change that leaves rows out waits for its own yes ("I agree to this sentence")
    approve_class_own
                    the same, read by the analyst who answered: the consent change needs the Approver role and the
                    rows change someone else (separation), so neither offers "I agree" (`can_agree` false)
    ready           its own yes given, approved, checked ready: what the hand-off starts with (the consent change
                    left out, never agreed)
    ready_with_warnings
                    the same, checked ready with warnings: the plain note "Ready, with notes" always shows
    not_ready       approved, and the readiness check said not ready
    standing        next month's case: the earlier answer applied, with its date and Revoke
    revoked         the same, after Revoke: the question is asked again
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.access.roles import Principal, Role
from engine.doctor import visit
from engine.doctor.case import Case, CaseStore
from engine.doctor.protocol import DataRequest, express_doubt
from engine.doctor.screen import CaseScreen, build_screen
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_protocol_visit import (
    ASHA,
    _answer_key,
    _boolean_ask,
    _by_code,
    _deps,
    _evidence,
    _found,
    _remove_rows_ask,
)
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, World, option, question

__all__ = ["MEERA", "SCENARIOS", "build_all", "scenario"]

MEERA = Principal(user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST}), kind="user")
"""A second analyst: separation of duties needs someone other than who answered."""

SCENARIOS = (
    "empty",
    "question",
    "assumed",
    "approve",
    "approve_class",
    "approve_class_own",
    "ready",
    "ready_with_warnings",
    "not_ready",
    "standing",
    "revoked",
)


def _request() -> DataRequest:
    return DataRequest(
        kind="more_data",
        what="the date of each customer's last order",
        why="Win back needs it.",
        columns=("customer_id", "last_order_date"),
        ask_whom="your billing team",
        upload_id=FIRST,
    )


def _doubt() -> tuple[Any, Any]:
    result = _evidence({"rows": 312, "near": 2, "median": 2_000, "mean": 1_880, "mean_if_thousands": 2_090})
    doubt = express_doubt(
        "UNIT_MISMATCH",
        because="“monthly_spend” is near 2,000 for most rows and near 2 for 312 rows, which could be thousands.",
        would_do="leave these 312 values as written and flag the column.",
        if_not="if they are thousands, the average rises from 1,880 to 2,090.",
        evidence={result.evidence_id: result.output},
        upload_id=FIRST,
    )
    return doubt, result


def _open(storage: LocalStorage, w: World, detector: Any) -> Case:
    created = CaseStore(storage, clock=w.clock).create(
        client_id=CLIENT, created_by=ANALYST, title="October customers", files=[vs.case_file(FIRST, w)]
    )
    return visit.resume(CLIENT, created.case_id, _deps(storage, w, detector), actor=ANALYST)


def _screen(
    storage: LocalStorage, case: Case, root: Path | None, principal: Principal | None = None
) -> CaseScreen:
    return build_screen(storage, case.client_id, case.case_id, config_root=root, principal=principal)


def scenario(name: str, storage: LocalStorage, w: World, root: Path | None = None) -> CaseScreen:
    """One scenario's screen answer, on a fresh store."""
    if name == "empty":
        empty = CaseStore(storage, clock=w.clock).create(client_id=CLIENT, created_by=ANALYST)
        empty = visit.resume(CLIENT, empty.case_id, _deps(storage, w, _found()), actor=ANALYST)
        return _screen(storage, empty, root)
    if name in {"question", "approve"}:
        doubt, result = _doubt()
        detector = _found(_boolean_ask(), doubts=(doubt,), evidence=(result,), requests=(_request(),))
        case = _open(storage, w, detector)
        if name == "approve":
            case = _answer_key(storage, w, case, detector)
            asked = _by_code(case, "UNKNOWN_BOOLEAN")
            case = visit.answer(
                CLIENT, case.case_id, asked.question_id, "c0", _deps(storage, w, detector),
                expected_version=case.version, actor=ANALYST,
            )  # fmt: skip
        return _screen(storage, case, root)
    if name == "assumed":
        detector = _found(_boolean_ask(rows_disagree=3))
        return _screen(storage, _answer_key(storage, w, _open(storage, w, detector), detector), root)
    if name in {"approve_class", "approve_class_own", "ready", "ready_with_warnings", "not_ready"}:
        detector = _found(_remove_rows_ask())
        deps = _deps(storage, w, detector)
        case = _answer_key(storage, w, _open(storage, w, detector), detector)
        case = visit.set_goal(
            CLIENT, case.case_id, GOAL_A, deps, expected_version=case.version, actor=ANALYST
        )
        q = question(case, "OUTCOME_COLUMN")
        case = visit.answer(
            CLIENT, case.case_id, q.question_id, option(q, "converted"), deps,
            expected_version=case.version, actor=ANALYST,
        )  # fmt: skip
        asked = _by_code(case, "IMPOSSIBLE_AGE")
        case = visit.answer(
            CLIENT, case.case_id, asked.question_id, "c0", deps, expected_version=case.version, actor=ANALYST
        )
        if name == "approve_class":
            return _screen(storage, case, root)
        if name == "approve_class_own":
            return _screen(storage, case, root, principal=ASHA)  # who answered: ANALYST, as ASHA
        item = next(i for i in case.items if i.payload.get("step", {}).get("kind") == "remove_rows")  # type: ignore[union-attr]
        case = visit.confirm(
            CLIENT, case.case_id, item.item_id, deps, principal=MEERA, sentence=item.title,
            expected_version=case.version,
        )  # fmt: skip
        w.verdict = name if name in {"ready", "ready_with_warnings"} else "not_ready"
        case = visit.approve(
            CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST,
            sentence=visit.approval_sentence(case),
        )  # fmt: skip
        return _screen(storage, case, root)
    if name in {"standing", "revoked"}:
        detector = _found(_boolean_ask())
        first = _open(storage, w, detector)
        asked = _by_code(first, "UNKNOWN_BOOLEAN")
        visit.answer(
            CLIENT, first.case_id, asked.question_id, "c0", _deps(storage, w, detector),
            expected_version=first.version, actor=ANALYST,
        )  # fmt: skip
        second = _open(storage, w, detector)
        if name == "revoked":
            used = second.standing_answers_used[0]
            second = visit.revoke_standing(
                CLIENT, second.case_id, used.answer_id, _deps(storage, w, detector),
                expected_version=second.version, actor=ANALYST,
            )  # fmt: skip
        return _screen(storage, second, root)
    raise KeyError(name)


def build_all(base: Path, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every scenario on its own store under `base`, as the JSON `GET /cases/{case_id}/screen` answers."""
    out: dict[str, dict[str, Any]] = {}
    for name in SCENARIOS:
        w = vs.world()
        storage = LocalStorage(base / name)
        out[name] = scenario(name, storage, w, root).model_dump(mode="json")
    return out
