"""One findings ledger and one approval (root-cause plan, Step 5: F3 rest and F6).

* Every source of a stop writes the one ledger (`engine.doctor.ledger`): the plan check's guards and readiness, a
  query trial's findings in its item's payload, doubts, the refusal that stopped the visit. The screen, the approval
  gate and the ready card read the same list; no route leaves a stop invisible to the screen.
* One approval (`engine.doctor.approval_model`): `needs_yes` lists every yes; one call gives them all when each is shown
  and checks them all before writing any; separation of duties holds; who asked is recorded on the item.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.approval_model import GivenYes, approval_of, needs_yes
from engine.doctor.case import Case, CaseChatEntry, CaseDoubt, CaseError, CaseItem, CaseStore, VisitState
from engine.doctor.case import payload_digest as digest_of
from engine.doctor.ledger import blocking, findings_of
from engine.doctor.protocol import item_sentence
from engine.doctor.screen import build_screen
from engine.doctor.visit_contracts import CaseVisit, VisitStop
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_verify_ledger import (
    HALF,
    LOSES,
    MEERA,
    READY,
    Scripted,
    _at_approve,
    _found,
    _not_ready,
    _step_item,
)
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, T0

ASHA = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")


def _edit(storage: LocalStorage, case: Case, **update: Any) -> Case:
    return CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update=update), []),
    )


def _view(storage: LocalStorage, case: Case, principal: Principal | None = None) -> Any:
    return build_screen(storage, CLIENT, case.case_id, config_root=None, principal=principal).view


def _whole_file_world(tmp_path: Path) -> tuple[LocalStorage, Any, Case, CaseItem]:
    """The rows the Doctor holds show no loss, the whole file does (a stop before approve), and the step that leaves
    rows out needs its own yes: one stop and one confirmation on the same plan."""
    config = load_use_case(GOAL_A)
    whole = vs.customers(seed=1)
    held = whole.assign(marketing_opt_in=1)
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
    deps = dataclasses.replace(vs.deps_for(storage, w_held, load_full=w_whole.load), verify=Scripted(READY))
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
    return storage, deps, case, item


# ---- every source is in the one ledger -------------------------------------------------------------------------------
def test_a_stop_in_a_trials_payload_is_in_the_ledger_and_blocks_until_the_plan_is_counted(
    tmp_path: Path,
) -> None:
    storage, _deps, case = _at_approve(tmp_path)
    payload: dict[str, Any] = {
        "step": {"kind": "sql_query", "order": 1, "params": {}},
        "findings": [
            {
                "level": "stop",
                "code": "PLAN_LOSES_TOO_MUCH",
                "message": "Loses 22.9% of customers.",
                "count": 343,
            },
            {"level": "flag", "code": "STEP_NOT_MEASURED", "message": "Not measured."},
        ],
    }
    item = CaseItem(
        item_id="sql.base.q1",
        kind="recipe_step",
        title="Total per customer",
        source=ProposalSource.AI,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        upload_id=FIRST,
        payload=payload,
        payload_digest=digest_of(payload),
        proposed_at=T0,
    )
    case = _edit(storage, case, items=(*case.items, item), visit=None)  # the plan is not counted yet
    found = [f for f in findings_of(case) if f.source == "sql_trial"]
    assert [(f.code, f.level, f.count, f.subject) for f in found] == [
        ("PLAN_LOSES_TOO_MUCH", "stop", 343, "sql.base.q1")
    ]
    assert found[0].measured == {"count": 343} and found[0].acknowledgeable
    assert {r.kind for r in found[0].resolutions} >= {"acknowledge", "fix", "ai_may_fix"}
    assert [f.key for f in blocking(case)] == [found[0].key]
    shown = _view(storage, case).ledger
    assert shown is not None and [f["key"] for f in shown["findings"]] == [found[0].key], "the screen sees it"
    assert _view(storage, case).conversation is not None


def test_the_refusal_that_stopped_the_visit_is_a_finding_the_screen_shows(tmp_path: Path) -> None:
    storage, _deps, case = _at_approve(tmp_path)
    stopped = (case.visit or CaseVisit()).model_copy(
        update={"stopped": VisitStop(code="AGENT_ITEM_UNKNOWN", message="No such change.", state="treat")}
    )
    case = _edit(storage, case, visit=stopped)
    (found,) = [f for f in findings_of(case) if f.source == "visit"]
    assert found.code == "AGENT_ITEM_UNKNOWN" and found.level == "stop" and not found.acknowledgeable
    assert {r.kind for r in found.resolutions} == {"retry", "fix"}
    rows = _view(storage, case).ledger["findings"]
    assert [r["code"] for r in rows if r["key"] == found.key] == ["AGENT_ITEM_UNKNOWN"]
    assert found.key in [f.key for f in blocking(case)]


def test_a_doubt_is_a_flag_with_a_status_and_never_blocks(tmp_path: Path) -> None:
    storage, _deps, case = _at_approve(tmp_path)
    doubt = CaseDoubt(
        doubt_id="doubt.1",
        because="two columns look like dates",
        would_do="use the first",
        if_not="the second",
    )
    case = _edit(storage, case, doubts=(doubt,))
    (found,) = [f for f in findings_of(case) if f.source == "doubt"]
    assert found.level == "flag" and not found.acknowledged and found.subject == "doubt.1"
    assert not [f for f in blocking(case) if f.source == "doubt"]
    assert all(r["code"] != "DOUBT" for r in (_view(storage, case).ledger or {}).get("findings", []))


def test_plan_stops_are_findings_with_the_levels_hard_stop_and_flag(tmp_path: Path) -> None:
    hard = _found("TARGET_TOO_FEW_POSITIVES", None, hard=True, subject="TARGET_TOO_FEW_POSITIVES:converted")
    soft = _found(count=343)
    storage, deps, case = _at_approve(tmp_path, verify=Scripted(_not_ready(soft, hard)))
    case = visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
    levels = {f.key: f.level for f in findings_of(case)}
    assert levels[hard.key] == "hard" and levels[soft.key] == "stop"
    (row,) = [r for r in _view(storage, case).ledger["findings"] if r["key"] == soft.key]
    assert row["subject"] == "plan" and row["measured"] == {"count": 343}
    assert row["resolutions"][0]["kind"] == "acknowledge"
    (hard_row,) = [r for r in _view(storage, case).ledger["findings"] if r["key"] == hard.key]
    assert "acknowledge" not in [r["kind"] for r in hard_row["resolutions"]], "a hard rule offers no yes"


# ---- one approval ----------------------------------------------------------------------------------------------------
def test_needs_yes_lists_the_stop_and_the_change_and_one_call_gives_both(tmp_path: Path) -> None:
    storage, deps, case, item = _whole_file_world(tmp_path)
    shown = _view(storage, case, MEERA).approval
    assert shown is not None
    stop_keys = [f.key for f in blocking(case) if f.acknowledgeable]
    assert stop_keys and set(shown.subjects) == {*(f"stop:{k}" for k in stop_keys), f"confirm:{LOSES}"}
    assert (
        len(shown.digests) == len(shown.subjects) and shown.sentence == _view(storage, case).approval_sentence
    )
    assert not shown.hard
    yeses = [
        *(GivenYes(kind="acknowledge", key=k, reason="On purpose.") for k in stop_keys),
        GivenYes(kind="confirm", item_id=LOSES, sentence=item_sentence(item.title, item.payload)),
    ]
    done = visit.approve(
        CLIENT,
        case.case_id,
        deps,
        expected_version=case.version,
        actor=MEERA.user_id,
        yeses=yeses,
        principal=MEERA,
        digest=shown.digest,
    )
    assert done.state in {VisitState.HAND_OFF, VisitState.FOLLOW_UP}
    assert [a.key for a in done.visit.acknowledged] == stop_keys  # type: ignore[union-attr]
    assert any(a.approve_class and a.item_ids == (LOSES,) for a in done.approvals)
    assert any(not a.approve_class for a in done.approvals)


def test_without_the_yeses_the_old_refusals_stand(tmp_path: Path) -> None:
    _storage, deps, case, _item = _whole_file_world(tmp_path)
    with pytest.raises(CaseError) as missing:
        visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=MEERA.user_id)
    assert missing.value.code == "APPROVAL_CONFIRMATION_MISSING"
    stop_keys = [f.key for f in blocking(case) if f.acknowledgeable]
    with pytest.raises(CaseError) as only_stops:  # the change's own yes is not given: nothing else is either
        visit.approve(
            CLIENT,
            case.case_id,
            deps,
            expected_version=case.version,
            actor=MEERA.user_id,
            yeses=[GivenYes(kind="acknowledge", key=k) for k in stop_keys],
            principal=MEERA,
        )
    assert only_stops.value.code == "APPROVAL_CONFIRMATION_MISSING"


def test_every_yes_is_checked_before_any_is_written(tmp_path: Path) -> None:
    storage, deps, case, _item = _whole_file_world(tmp_path)
    stop_keys = [f.key for f in blocking(case) if f.acknowledgeable]
    with pytest.raises(CaseError) as changed:
        visit.approve(
            CLIENT,
            case.case_id,
            deps,
            expected_version=case.version,
            actor=MEERA.user_id,
            yeses=[
                *(GivenYes(kind="acknowledge", key=k) for k in stop_keys),
                GivenYes(kind="confirm", item_id=LOSES, sentence="Leave out some rows."),
            ],
            principal=MEERA,
        )
    assert changed.value.code == "APPROVAL_SENTENCE_CHANGED"
    assert CaseStore(storage).get(CLIENT, case.case_id).version == case.version, "nothing was written"
    shown = approval_of(case, "x", principal=MEERA)
    with pytest.raises(CaseError) as stale:
        visit.approve(
            CLIENT,
            case.case_id,
            deps,
            expected_version=case.version,
            actor=MEERA.user_id,
            digest="0" * 32,
            principal=MEERA,
        )
    assert stale.value.code == "APPROVAL_SENTENCE_CHANGED" and shown.digest != "0" * 32


def test_a_yes_to_something_not_shown_is_not_given_and_a_hard_rule_is_never_covered(tmp_path: Path) -> None:
    hard = _found("TARGET_TOO_FEW_POSITIVES", None, hard=True, subject="TARGET_TOO_FEW_POSITIVES:converted")
    storage, deps, case = _at_approve(tmp_path, verify=Scripted(_not_ready(hard)))
    case = visit.approve(CLIENT, case.case_id, deps, expected_version=case.version, actor=ANALYST)
    assert needs_yes(case) == (), "a hard rule is on no list of yeses"
    assert approval_of(case, "s").hard == (hard.key,)
    with pytest.raises(CaseError) as blocked:
        visit.approve(
            CLIENT,
            case.case_id,
            deps,
            expected_version=case.version,
            actor=ANALYST,
            yeses=[GivenYes(kind="acknowledge", key=hard.key)],
            principal=MEERA,
        )
    assert blocked.value.code == "APPROVAL_STOP_OPEN"
    assert CaseStore(storage).get(CLIENT, case.case_id).visit.acknowledged == ()  # type: ignore[union-attr]


def test_a_retried_approval_call_changes_nothing_more(tmp_path: Path) -> None:
    _storage, deps, case, item = _whole_file_world(tmp_path)
    stop_keys = [f.key for f in blocking(case) if f.acknowledgeable]
    yeses = [
        *(GivenYes(kind="acknowledge", key=k) for k in stop_keys),
        GivenYes(kind="confirm", item_id=LOSES, sentence=item_sentence(item.title, item.payload)),
    ]
    args: dict[str, Any] = {
        "expected_version": case.version,
        "actor": MEERA.user_id,
        "yeses": yeses,
        "principal": MEERA,
        "op_id": "one-click-1",
    }
    first = visit.approve(CLIENT, case.case_id, deps, **args)
    again = visit.approve(CLIENT, case.case_id, deps, **args)
    assert again.version == first.version and len(again.approvals) == len(first.approvals)


# ---- separation of duties, and who asked --------------------------------------------------------------------------
def _with_rows_change(tmp_path: Path, **item_update: Any) -> tuple[LocalStorage, Any, Case, CaseItem]:
    storage, deps, case = _at_approve(tmp_path)
    item = _step_item(LOSES, HALF).model_copy(update=item_update)
    case = _edit(storage, case, items=(*case.items, item), state=VisitState.TREAT)
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    assert case.state is VisitState.APPROVE
    return storage, deps, case, item


def test_separation_of_duties_still_refuses_the_one_who_asked_in_one_call(tmp_path: Path) -> None:
    _storage, deps, case, item = _with_rows_change(tmp_path, requested_by=ASHA.user_id)
    yes = GivenYes(kind="confirm", item_id=LOSES, sentence=item_sentence(item.title, item.payload))
    stops = [GivenYes(kind="acknowledge", key=f.key) for f in blocking(case) if f.acknowledgeable]
    shown = {y.subject: y for y in needs_yes(case, separate=True, principal=ASHA)}
    assert (shown[f"confirm:{LOSES}"].can_agree, shown[f"confirm:{LOSES}"].cannot_agree) == (
        False,
        "separation",
    )
    with pytest.raises(CaseError) as own:
        visit.approve(
            CLIENT, case.case_id, deps, expected_version=case.version, actor=ASHA.user_id, yeses=[yes],
            principal=ASHA,
        )  # fmt: skip
    assert own.value.code == "POLICY_ROLE_SEPARATION"
    done = visit.approve(
        CLIENT, case.case_id, deps, expected_version=case.version, actor=MEERA.user_id,
        yeses=[*stops, yes], principal=MEERA,
    )  # fmt: skip
    assert done.state is not VisitState.APPROVE


def test_who_asked_is_the_one_recorded_not_the_one_the_chat_order_suggests(tmp_path: Path) -> None:
    """Asha's message comes right before the agent's reply that holds the change, but Meera asked for it (recorded):
    Asha may give the yes, Meera may not."""
    _storage, deps, case, item = _with_rows_change(tmp_path, requested_by=MEERA.user_id)
    chat = (
        CaseChatEntry(role="user", text="hello", at=T0, by=ASHA.user_id),
        CaseChatEntry(role="agent", text="Done.", at=T0, item_ids=(LOSES,)),
    )
    case = _edit(_storage, case, chat=chat)
    assert visit.item_proposers(case, item.model_copy(update={"requested_by": MEERA.user_id})) == (
        MEERA.user_id,
    )
    # without a record the order of the chat is read (a case kept before it was recorded)
    assert visit.item_proposers(case, item.model_copy(update={"requested_by": None})) == (ASHA.user_id,)
    yes = GivenYes(kind="confirm", item_id=LOSES, sentence=item_sentence(item.title, item.payload))
    stops = [GivenYes(kind="acknowledge", key=f.key) for f in blocking(case) if f.acknowledgeable]
    done = visit.approve(
        CLIENT, case.case_id, deps, expected_version=case.version, actor=ASHA.user_id, yeses=[*stops, yes],
        principal=ASHA,
    )  # fmt: skip
    assert done.state is not VisitState.APPROVE


def test_a_chat_request_records_who_asked_on_the_item(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        "upload_id": FIRST,
        "step": {"kind": "remove_rows", "column": "age", "params": {"operator": "equals", "values": [18]}},
    }

    def converse(case: Case, message: str, files: Any) -> visit.ConverseResult:
        del case, message, files
        item = CaseItem(
            item_id="recipe_step.base.ai1",
            kind="recipe_step",
            title="Leave out the youngest",
            source=ProposalSource.AI,
            confidence=AgentConfidence.CHECK,
            upload_id=FIRST,
            payload=payload,
            payload_digest=digest_of(payload),
            proposed_at=T0,
        )
        return visit.ConverseResult(reply="Here.", items=(item,))

    storage, deps, case = _at_approve(tmp_path)
    deps = dataclasses.replace(deps, converse=converse)
    case = visit.chat(
        CLIENT,
        case.case_id,
        "Leave out 18-year-olds",
        deps,
        expected_version=case.version,
        actor=ASHA.user_id,
    )
    (made,) = [i for i in case.items if i.item_id == "recipe_step.base.ai1"]
    assert made.requested_by == ASHA.user_id
    assert made.source is ProposalSource.AI
    del storage
