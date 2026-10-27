"""One materialisation, and the final check's findings reach the person (root-cause plan, Step 1).

* The plan is counted on every row BEFORE the person approves, so a stop of the whole file is known first.
* VERIFY's not-ready findings (a guard's stop on the whole file, a readiness requirement) are written into the plan's
  ledger; the visit rests at APPROVE with them; a stop is said yes to by its key unless it is a hard rule; a yes holds
  while the count stays at or below the one said yes to; the visit never goes round TREAT, APPROVE and VERIFY.
* VERIFY never reads the raw upload when the plan has steps.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import (
    AgentConfidence,
    DecidedBy,
    ProposalSource,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
)
from engine.config import load_use_case
from engine.contracts import Severity
from engine.doctor import visit
from engine.doctor.case import Case, CaseError, CaseFile, CaseItem, CaseStore, VisitState, payload_digest
from engine.doctor.goal_brief import load_goal_brief
from engine.doctor.materialize import Full, Sample, materialize_plan, table_names
from engine.doctor.plan_check import open_stops
from engine.doctor.protocol import item_sentence
from engine.doctor.screen import build_screen
from engine.doctor.visit import VerifyResult, VisitDeps
from engine.doctor.visit_contracts import Acknowledgement, CaseVisit, PlanCheck, PlanFinding
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, T0

MEERA = Principal(user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST}), kind="user")
LOSES = "step.loses_half"
HALF = RecipeStep(
    order=1,
    kind=RecipeStepKind.REMOVE_ROWS,
    column="marketing_opt_in",
    params={"operator": "equals", "values": [0]},
    reason="Leave out customers who did not opt in.",
    decided_by=DecidedBy.USER,
)
FEW = RecipeStep(
    order=1,
    kind=RecipeStepKind.REMOVE_ROWS,
    column="age",
    params={"operator": "equals", "values": [18]},
    reason="Leave out the youngest.",
    decided_by=DecidedBy.USER,
)


def _step_item(item_id: str, step: RecipeStep) -> CaseItem:
    payload: dict[str, Any] = {"upload_id": FIRST, "step": step.model_dump(mode="json")}
    return CaseItem(
        item_id=item_id,
        kind="recipe_step",
        title=step.reason,
        source=ProposalSource.PERSON,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        upload_id=FIRST,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=T0,
    )


def _deps(storage: LocalStorage, w: vs.World, **extra: Any) -> VisitDeps:
    return dataclasses.replace(vs.deps_for(storage, w), **extra)


def _at_approve(
    tmp_path: Path, step: RecipeStep | None = None, **extra: Any
) -> tuple[LocalStorage, VisitDeps, Case]:
    """A real case walked to Approve (key, goal and outcome answered), then the step added and the plan counted."""
    storage = LocalStorage(tmp_path)
    w = vs.world()
    deps = _deps(storage, w, **extra)
    case = vs.open_case(storage, w)
    for _name, _op, act in vs.SCRIPT[:4]:
        case = act(_deps(storage, w, **extra), CaseStore(storage).get(CLIENT, case.case_id))
    assert case.state is VisitState.APPROVE
    if step is not None:
        item = _step_item(LOSES, step)
        CaseStore(storage).update(
            CLIENT,
            case.case_id,
            expected_version=case.version,
            actor=ANALYST,
            change=lambda c: (
                c.model_copy(update={"items": (*c.items, item), "state": VisitState.TREAT}),
                [],
            ),
        )
        case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    return storage, deps, case


def _ledger(storage: LocalStorage, case: Case) -> dict[str, Any]:
    view = build_screen(storage, CLIENT, case.case_id, config_root=None).view
    assert view.ledger is not None
    return view.ledger


def _found(
    code: str = "PLAN_LOSES_TOO_MUCH", count: int | None = 343, *, hard: bool = False, subject: str = "plan"
) -> PlanFinding:
    from engine.agentic.canonical import canonical_bytes, sha256_hex

    return PlanFinding(
        key=sha256_hex(canonical_bytes([code, subject]))[:16],
        level="stop",
        code=code,
        text="Leaving out these rows loses 343 of 1,500 customers (22.9%).",
        count=count,
        scope="full",
        hard=hard,
        source="readiness" if hard else "guard",
    )


class Scripted:
    """A VERIFY port that answers from a list, one result per call (the last one repeats)."""

    def __init__(self, *results: VerifyResult) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self, case: Case, brief: Any, files: tuple[Any, ...]) -> VerifyResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def _not_ready(
    *findings: PlanFinding, reason: str = "Not ready: leave out 343 of 1,500 customers."
) -> VerifyResult:
    return VerifyResult(verdict="not_ready", reason=reason, findings=tuple(findings))


READY = VerifyResult(verdict="ready", reason="Ready.")


def _approve(deps: VisitDeps, case: Case) -> Case:
    return visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)


def _ack(deps: VisitDeps, case: Case, key: str) -> Case:
    return visit.acknowledge_stop(
        CLIENT, case.case_id, key, deps, expected_version=case.version, actor=ANALYST, reason="On purpose."
    )


# ---- the stop of the whole file, found at VERIFY ---------------------------------------------------------------------
def test_a_stop_found_at_verify_rests_at_approve_is_acknowledged_and_verify_then_passes(
    tmp_path: Path,
) -> None:
    stop = _found()
    port = Scripted(_not_ready(stop), READY)
    storage, deps, case = _at_approve(tmp_path, verify=port)

    case = _approve(deps, case)
    assert case.state is VisitState.APPROVE, "it rests at APPROVE with the stop, not back at TREAT"
    assert case.visit is not None and case.visit.plan_check is not None and case.visit.plan_check.verified
    shown = [f for f in _ledger(storage, case)["findings"] if f["key"] == stop.key]
    assert len(shown) == 1
    assert shown[0]["scope"] == "full" and shown[0]["level"] == "stop" and shown[0]["acknowledgeable"] is True
    assert "343 of 1,500 customers" in shown[0]["text"]
    verdict = build_screen(storage, CLIENT, case.case_id, config_root=None).view.verdict
    assert verdict.key == "not_ready" and "343 of 1,500" in " ".join(verdict.reasons)
    with pytest.raises(CaseError) as refused:
        _approve(deps, case)
    assert refused.value.code == "APPROVAL_STOP_OPEN"

    case = _ack(deps, case, stop.key)
    assert case.visit is not None and [a.count for a in case.visit.acknowledged] == [343]
    after = _ledger(storage, case)
    assert [f["level"] for f in after["findings"] if f["key"] == stop.key] == ["flag"]
    assert build_screen(storage, CLIENT, case.case_id, config_root=None).view.verdict.key == "needs_approval"

    case = _approve(deps, case)
    assert case.state in {VisitState.HAND_OFF, VisitState.FOLLOW_UP}, "VERIFY reaches its verdict: no loop"
    assert port.calls == 2
    assert not [
        e
        for e in CaseStore(storage).timeline(CLIENT, case.case_id)
        if e.details.get("from") == "verify" and e.details.get("to") == "treat"
    ]


def test_a_hard_stop_is_shown_and_cannot_be_acknowledged(tmp_path: Path) -> None:
    hard = _found("TARGET_TOO_FEW_POSITIVES", None, hard=True, subject="TARGET_TOO_FEW_POSITIVES:converted")
    storage, deps, case = _at_approve(tmp_path, verify=Scripted(_not_ready(hard)))

    case = _approve(deps, case)
    assert case.state is VisitState.APPROVE
    shown = [f for f in _ledger(storage, case)["findings"] if f["key"] == hard.key]
    assert shown and shown[0]["hard"] is True and shown[0]["acknowledgeable"] is False
    with pytest.raises(CaseError) as refused:
        _ack(deps, case, hard.key)
    assert refused.value.code == "STOP_NOT_ACKNOWLEDGEABLE"
    with pytest.raises(CaseError) as blocked:
        _approve(deps, case)
    assert blocked.value.code == "APPROVAL_STOP_OPEN"
    card = build_screen(storage, CLIENT, case.case_id, config_root=None).view.conversation
    assert card is not None


def test_a_yes_holds_for_the_same_or_a_smaller_count_and_is_asked_again_for_a_bigger_one(
    tmp_path: Path,
) -> None:
    first = _found(count=343)
    port = Scripted(
        _not_ready(first),
        _not_ready(_found(count=300)),  # the same stop, smaller: the yes still holds
        _not_ready(_found(count=500)),  # a bigger loss
    )
    storage, deps, case = _at_approve(tmp_path, verify=port)
    case = _approve(deps, case)
    case = _ack(deps, case, first.key)

    case = _approve(deps, case)  # VERIFY again: the same stop at 300
    assert case.state is VisitState.APPROVE and case.visit is not None
    assert open_stops(case.visit.plan_check, case.visit.acknowledged) == [], "343 covers 300"
    assert [f["level"] for f in _ledger(storage, case)["findings"] if f["key"] == first.key] == ["flag"]

    case = _approve(deps, case)  # VERIFY a third time: 500 customers
    assert case.visit is not None
    (open_one,) = open_stops(case.visit.plan_check, case.visit.acknowledged)
    assert open_one.count == 500, "a bigger loss needs a new yes"
    with pytest.raises(CaseError):
        _approve(deps, case)


def test_the_same_reason_with_the_same_plan_rests_at_approve_and_does_not_loop(tmp_path: Path) -> None:
    """Not ready with nothing to show goes back to TREAT once; the same plan and reason again is a stop at APPROVE."""
    port = Scripted(VerifyResult(verdict="needs_answers", reason="Needs answers: 1 question(s)."))
    _storage, deps, case = _at_approve(tmp_path, verify=port)

    case = _approve(deps, case)
    assert case.state is VisitState.APPROVE, "TREAT -> PREVIEW -> APPROVE again, waiting for the person"
    assert port.calls == 1
    assert case.visit is not None and not case.visit.plan_check.verified  # type: ignore[union-attr]

    case = _approve(deps, case)  # nothing changed in the plan; VERIFY says the same thing
    assert case.state is VisitState.APPROVE and port.calls == 2
    assert case.visit is not None and case.visit.plan_check is not None and case.visit.plan_check.verified
    (stopped,) = open_stops(case.visit.plan_check, case.visit.acknowledged)
    assert stopped.code == "VERIFY_NOT_READY" and stopped.hard
    transitions = case.visit.transitions
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    assert (
        case.state is VisitState.APPROVE and case.visit is not None and case.visit.transitions == transitions
    )


# ---- the plan counted on every row before the person approves --------------------------------------------------------
def test_the_whole_file_is_counted_before_approve_not_after(tmp_path: Path) -> None:
    """The rows the Doctor holds show no loss; the whole file does: the stop is in the ledger before APPROVE."""
    config = load_use_case(GOAL_A)
    whole = vs.customers(seed=1)
    held = whole.assign(marketing_opt_in=1)  # a sample in which nobody opted out
    w_held = vs.World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", held, config)}, offers=vs.world().offers
    )
    w_whole = vs.World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", whole, config)}, offers=w_held.offers
    )
    storage = LocalStorage(tmp_path)
    case = vs.open_case(storage, vs.world())
    for _name, _op, act in vs.SCRIPT[:4]:
        case = act(vs.deps_for(storage, vs.world()), CaseStore(storage).get(CLIENT, case.case_id))
    deps = vs.deps_for(storage, w_held, load_full=w_whole.load)
    item = _step_item(LOSES, HALF)
    CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update={"items": (*c.items, item), "state": VisitState.TREAT}), []),
    )
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)

    assert case.state is VisitState.APPROVE
    stops = [f for f in _ledger(storage, case)["findings"] if f["level"] == "stop"]
    assert stops and all(f["scope"] == "full" for f in stops)
    case = visit.confirm(  # leaving rows out needs its own yes first (the protocol's rule); the stops are the gate after
        CLIENT,
        case.case_id,
        LOSES,
        deps,
        principal=MEERA,
        sentence=item_sentence(item.title, item.payload),
        expected_version=case.version,
    )
    with pytest.raises(CaseError) as refused:
        _approve(deps, case)
    assert refused.value.code == "APPROVAL_STOP_OPEN"


def test_the_plan_is_not_counted_again_for_the_same_plan(tmp_path: Path) -> None:
    storage, deps, case = _at_approve(tmp_path, FEW)
    assert case.visit is not None and case.visit.plan_check is not None
    kept = case.visit.plan_check
    assert kept.basis is not None
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    assert case.visit is not None and case.visit.plan_check == kept
    del storage


# ---- one materialisation ---------------------------------------------------------------------------------------------
def _orders_case(rows: int, lost_share: float) -> tuple[list[Any], list[CaseFile], list[CaseItem]]:
    config = load_use_case(GOAL_A)
    cut = int(rows * lost_share)
    frame = pd.DataFrame(
        {
            "customer_id": range(rows),
            "month": [1] * cut + [0] * (rows - cut),
            "converted": [i % 2 for i in range(rows)],
        }
    )
    file = vs.loaded("up-c", "customers.csv", frame, config)
    case_file = CaseFile(upload_id="up-c", file_name="customers.csv", added_at=T0, added_by=ANALYST)
    gone = RecipeStep(
        order=1, kind=RecipeStepKind.REMOVE_ROWS, column="month", params={"operator": "equals", "values": [1]}
    )
    payload: dict[str, Any] = {"upload_id": "up-c", "step": gone.model_dump(mode="json")}
    item = CaseItem(
        item_id="remove.c",
        kind="recipe_step",
        title="Leave out the unfinished month.",
        source=ProposalSource.PERSON,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        upload_id="up-c",
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=T0,
    )
    return [file], [case_file], [item]


def test_a_sample_is_seeded_and_the_same_for_every_caller_and_a_yes_on_it_matches_the_whole_file() -> None:
    files, case_files, items = _orders_case(5_000, 0.3)
    one = materialize_plan(case_files, items, files, Sample(500, seed=7))
    two = materialize_plan(case_files, items, files, Sample(500, seed=7))
    other = materialize_plan(case_files, items, files, Sample(500, seed=8))
    assert one.receipts[0][1].rows_in == two.receipts[0][1].rows_in
    assert list(one.tables["customers"].index) == list(two.tables["customers"].index)
    assert list(one.tables["customers"].index) != list(other.tables["customers"].index)

    whole = materialize_plan(case_files, items, files, Full())
    on_sample = [f for f in one.findings if f.level == "stop"]
    on_file = [f for f in whole.findings if f.level == "stop"]
    assert on_sample and on_file
    assert {f.key for f in on_sample} == {
        f.key for f in on_file
    }, "the key is the code and the step, never the count"
    assert on_sample[0].scope == "sample" and on_file[0].scope == "full"
    assert (on_file[0].count or 0) > (on_sample[0].count or 0)
    said = Acknowledgement(key=on_sample[0].key, count=on_sample[0].count, by=ANALYST, at=T0)
    assert open_stops(PlanCheck(digest="d", findings=tuple(on_file)), [said]), "a yes on the sample's count"
    big = Acknowledgement(key=on_file[0].key, count=on_file[0].count, by=ANALYST, at=T0)
    assert not open_stops(PlanCheck(digest="d", findings=tuple(on_file)), [big])


def test_a_step_that_cannot_run_is_a_stop_not_a_silence() -> None:
    files, case_files, items = _orders_case(200, 0.0)
    broken = items[0].payload["step"]
    assert isinstance(broken, dict)
    broken["column"] = "no_such_column"
    items[0] = items[0].model_copy(update={"payload": {**items[0].payload, "step": broken}})
    prepared = materialize_plan(case_files, items, files, Full())
    (stop,) = [f for f in prepared.findings if f.level == "stop"]
    assert stop.code == "PLAN_STEP_CANNOT_RUN" and stop.hard and stop.scope == "full"
    assert "customers.csv" in stop.text


def test_one_namer_tells_two_files_of_one_name_apart() -> None:
    config = load_use_case(GOAL_A)
    frame = pd.DataFrame({"a": [1]})
    files = [
        vs.loaded("u1", "orders.csv", frame, config),
        vs.loaded("u2", "orders.csv", frame, config),
        vs.loaded("u3", "My Customers.csv", frame, config),
    ]
    assert table_names(files) == {"u1": "orders", "u2": "orders_2", "u3": "My_Customers"}


# ---- VERIFY never reads the raw upload when the plan has steps -------------------------------------------------------
class _ReachedError(Exception):
    def __init__(self, upload_id: str) -> None:
        super().__init__(upload_id)
        self.upload_id = upload_id


def _verify_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(storage: Any, upload_id: str, **_kw: Any) -> Any:
        raise _ReachedError(upload_id)

    monkeypatch.setattr("engine.doctor.readiness.inputs_for_upload", fake)


def test_verify_checks_the_table_the_plan_makes_never_the_raw_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, _deps, case = _at_approve(tmp_path, FEW)
    brief = load_goal_brief(GOAL_A)
    files = vs.world().load(case)
    _verify_inputs(monkeypatch)
    stored: list[pd.DataFrame] = []

    def store(config: Any, upload_id: str, frame: pd.DataFrame) -> str:
        stored.append(frame)
        return "up-prepared"

    port = visit.readiness_verify(storage, None, lambda *_a: None, store_frame=store)
    with pytest.raises(_ReachedError) as reached:
        port(case, brief, tuple(files))
    assert reached.value.upload_id == "up-prepared", "the readiness check reads the stored plan table"
    assert len(stored) == 1 and len(stored[0]) < len(files[0].frame), "the plan's step was applied"
    assert not (stored[0]["age"] == 18).any()


def test_verify_without_a_way_to_make_the_table_is_a_named_stop_not_the_raw_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, _deps, case = _at_approve(tmp_path, FEW)
    _verify_inputs(monkeypatch)
    for prepare in (None, lambda *_a: None):
        result = visit.readiness_verify(storage, None, prepare)(
            case, load_goal_brief(GOAL_A), tuple(vs.world().load(case))
        )
        assert result.verdict == "not_ready"
        (found,) = [f for f in result.findings if f.code == "VERIFY_NOT_PREPARED"]
        assert found.hard and found.scope == "full"


def test_verify_of_a_plan_that_changes_nothing_checks_the_upload_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, _deps, case = _at_approve(tmp_path)
    _verify_inputs(monkeypatch)
    with pytest.raises(_ReachedError) as reached:
        visit.readiness_verify(storage, None, lambda *_a: None)(
            case, load_goal_brief(GOAL_A), tuple(vs.world().load(case))
        )
    assert reached.value.upload_id == FIRST


def test_verify_stops_on_a_whole_file_stop_nobody_said_yes_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, _deps, case = _at_approve(tmp_path, HALF)
    _verify_inputs(monkeypatch)
    result = visit.readiness_verify(storage, None, lambda *_a: None)(
        case, load_goal_brief(GOAL_A), tuple(vs.world().load(case))
    )
    assert result.verdict == "not_ready"
    assert [f.code for f in result.findings if f.level == "stop"], "the guards' stop comes with the verdict"


# ---- the readiness report as findings ----------------------------------------------------------------------------------
def test_readiness_failures_are_findings_and_a_rule_is_hard() -> None:
    from engine.doctor.visit import _readiness_findings

    def check(code: str, *, acknowledgeable: bool, column: str | None = None) -> Any:
        return SimpleNamespace(
            severity=Severity.ERROR,
            acknowledged=False,
            code=code,
            column=column,
            message=f"{code} message",
            suggestion="",
            acknowledgeable=acknowledgeable,
        )

    report = SimpleNamespace(
        dry_run_report=SimpleNamespace(
            checks=[
                check("TARGET_TOO_FEW_POSITIVES", acknowledgeable=False, column="converted"),
                check("HIGH_NULL_COLUMN", acknowledgeable=True, column="age"),
            ]
        ),
        requirements=[],
        probes=[
            SimpleNamespace(status="fail", code="X_LEAK", column="spend", message="Uses the future.", fix="")
        ],
    )
    found = {f.code: f for f in _readiness_findings(report)}
    assert found["TARGET_TOO_FEW_POSITIVES"].hard and found["X_LEAK"].hard
    assert not found["HIGH_NULL_COLUMN"].hard and found["HIGH_NULL_COLUMN"].column == "age"
    assert all(f.scope == "full" and f.source == "readiness" and f.level == "stop" for f in found.values())


def test_an_older_stored_case_still_loads() -> None:
    old = CaseVisit.model_validate(
        {
            "plan_check": {
                "digest": "d",
                "findings": [{"key": "k" * 16, "level": "stop", "code": "PLAN_LOSES_TOO_MUCH", "text": "x"}],
            },
            "acknowledged": [{"key": "k" * 16, "by": "u", "at": "2026-10-12T09:00:00+00:00"}],
        }
    )
    assert old.plan_check is not None and old.plan_check.basis is None
    assert old.plan_check.findings[0].scope == "sample" and not old.plan_check.findings[0].hard
    assert old.acknowledged[0].count is None
    assert open_stops(old.plan_check, old.acknowledged) == [], "no count on either side: the old yes holds"


# ---- review round: older cases, rules that no check's flag can soften, flags-only verdicts, failed steps --------------
def test_an_older_case_at_approve_is_counted_on_the_whole_file_and_the_count_is_kept(tmp_path: Path) -> None:
    """A case kept with a sample-only check (no basis): the whole-file count is stored, with the old yeses dropped, so
    the stops approval refuses at are the stops the screen shows and a yes to each lets the person approve."""
    config = load_use_case(GOAL_A)
    whole = vs.customers(seed=1)
    w_whole = vs.World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", whole, config)}, offers=vs.world().offers
    )
    storage = LocalStorage(tmp_path)
    case = vs.open_case(storage, vs.world())
    for _name, _op, act in vs.SCRIPT[:4]:
        case = act(vs.deps_for(storage, vs.world()), CaseStore(storage).get(CLIENT, case.case_id))
    deps = vs.deps_for(storage, w_whole, load_full=w_whole.load)
    item = _step_item(LOSES, HALF)
    CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update={"items": (*c.items, item), "state": VisitState.TREAT}), []),
    )
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    case = visit.confirm(
        CLIENT,
        case.case_id,
        LOSES,
        deps,
        principal=MEERA,
        sentence=item_sentence(item.title, item.payload),
        expected_version=case.version,
    )
    assert case.visit is not None and case.visit.plan_check is not None
    digest = case.visit.plan_check.digest
    old = PlanFinding(key="o" * 16, level="stop", code="PLAN_LOSES_TOO_MUCH", text="old", count=1)
    legacy = PlanCheck(digest=digest, findings=(old,))  # no basis: counted on a sample only
    said = Acknowledgement(key=old.key, count=1, by="u", at=T0)
    CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (
            c.model_copy(
                update={
                    "visit": (c.visit or CaseVisit()).model_copy(
                        update={"plan_check": legacy, "acknowledged": (said,)}
                    )
                }
            ),
            [],
        ),
    )

    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    assert case.visit is not None and case.visit.plan_check is not None
    assert case.visit.plan_check.basis is not None and case.visit.acknowledged == ()
    stops = open_stops(case.visit.plan_check, case.visit.acknowledged)
    assert stops and all(f.scope == "full" for f in stops) and old.key not in {f.key for f in stops}
    for stop in [s for s in stops if not s.hard]:
        case = _ack(deps, case, stop.key)
    assert case.visit is not None and not open_stops(case.visit.plan_check, case.visit.acknowledged)
    assert _approve(deps, case).state is not VisitState.APPROVE


def test_leakage_consent_and_the_minimums_are_rules_whatever_the_check_says() -> None:
    from engine.doctor.visit import _readiness_findings

    def check(code: str) -> Any:
        return SimpleNamespace(
            severity=Severity.ERROR,
            acknowledged=False,
            code=code,
            column="c",
            message=code,
            suggestion="",
            acknowledgeable=True,  # the registry would let a run acknowledge it
        )

    codes = [
        "LEAKAGE_SUSPECTED",
        "ROWS_TOO_FEW",
        "TARGET_TOO_FEW_POSITIVES",
        "CONSENT_COLUMN_MISSING",
        "CONSENT_X",
    ]
    report = SimpleNamespace(
        dry_run_report=SimpleNamespace(checks=[check(c) for c in [*codes, "HIGH_NULL_COLUMN"]]),
        requirements=[],
        probes=[],
    )
    found = {f.code: f for f in _readiness_findings(report)}
    assert all(found[c].hard for c in codes)
    assert not found["HIGH_NULL_COLUMN"].hard
    said = [Acknowledgement(key=found["LEAKAGE_SUSPECTED"].key, by="u", at=T0)]
    still = open_stops(PlanCheck(digest="d", findings=tuple(found.values())), said)
    assert "LEAKAGE_SUSPECTED" in {f.code for f in still}, "a yes never lifts it"


def test_a_verdict_with_only_flags_does_not_loop_and_gets_its_stop(tmp_path: Path) -> None:
    flag = PlanFinding(key="f" * 16, level="flag", code="SOME_FLAG", text="Worth a look.", scope="full")
    port = Scripted(
        VerifyResult(verdict="needs_answers", reason="Needs answers: 1 question(s).", findings=(flag,))
    )
    storage, deps, case = _at_approve(tmp_path, verify=port)

    case = _approve(deps, case)
    assert case.state is VisitState.APPROVE and port.calls == 1
    case = _approve(deps, case)
    assert case.state is VisitState.APPROVE and port.calls == 2
    assert case.visit is not None and case.visit.plan_check is not None
    (stopped,) = open_stops(case.visit.plan_check, case.visit.acknowledged)
    assert stopped.code == "VERIFY_NOT_READY"
    shown = build_screen(storage, CLIENT, case.case_id, config_root=None).view.verdict
    assert shown.key == "not_ready" and shown.reasons


def test_the_no_progress_stop_goes_when_a_confirmation_changes_what_the_check_depends_on(
    tmp_path: Path,
) -> None:
    from engine.doctor.case import CaseApproval

    port = Scripted(VerifyResult(verdict="needs_approval", reason="Needs approval: 1 change."))
    _storage, deps, case = _at_approve(tmp_path, verify=port)
    case = _approve(deps, case)
    case = _approve(deps, case)
    assert case.visit is not None and case.visit.plan_check is not None
    assert [f.code for f in open_stops(case.visit.plan_check, case.visit.acknowledged)] == [
        "VERIFY_NOT_READY"
    ]

    yes = CaseApproval(
        approval_id="approval.9", approved_by="u", approved_at=T0, approve_class=True, payload_digest="p" * 16
    )
    CaseStore(_storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update={"approvals": (*c.approvals, yes)}), []),
    )
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    assert case.visit is not None and case.visit.plan_check is not None
    assert not open_stops(case.visit.plan_check, case.visit.acknowledged), "the check may run again"


def test_a_step_that_cannot_run_is_hard_in_every_form_and_its_file_is_recorded_as_failed() -> None:
    files, case_files, items = _orders_case(200, 0.0)
    broken = items[0].payload["step"]
    assert isinstance(broken, dict)
    broken["column"] = "no_such_column"
    items[0] = items[0].model_copy(update={"payload": {**items[0].payload, "step": broken}})
    prepared = materialize_plan(case_files, items, files, Full())
    assert prepared.failed == (files[0].upload_id,) and not prepared.files
    assert all(f.hard for f in prepared.findings if f.level == "stop")
