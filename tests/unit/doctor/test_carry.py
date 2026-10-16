"""One approval however often the plan comes back (`engine.doctor.carry`): after the files are joined the visit
carries on by itself, a goal's new meaning (who agreed to be contacted) is asked once and said in one line, what is
the same stays approved and folded, and a stop or flag, or a change a person made, always asks."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor import visit
from engine.doctor.carry import carries_on, carry_of
from engine.doctor.case import Case, CaseItem, CaseStore, VisitState
from engine.doctor.screen import screen_view
from engine.doctor.visit import active_section, confirmed
from engine.doctor.visit_contracts import PlanCheck, PlanFinding
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_combining import _customers, _Day, _one, _orders


def _with_goal_columns(frame: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return frame.assign(
        converted=rng.integers(0, 2, len(frame)),
        snapshot_date="2026-09-30",
        marketing_opt_in=rng.integers(0, 2, len(frame)),
    )


def _joined(tmp_path: Path) -> tuple[_Day, Case]:
    day = _Day(
        tmp_path,
        {"up-c": ("customers.csv", _with_goal_columns(_customers())), "up-o": ("orders.csv", _orders(0))},
    )
    case = day.open()
    item = _one(case, "build_dataset")
    case = visit.decide(
        vs.CLIENT, case.case_id, item.item_id, True, day.deps, expected_version=case.version, actor=vs.ANALYST
    )
    case = visit.approve(vs.CLIENT, case.case_id, day.deps, expected_version=case.version, actor=vs.ANALYST)
    return day, case


def test_the_goal_adds_what_is_new_in_one_line_and_what_was_approved_is_folded(tmp_path: Path) -> None:
    day, case = _joined(tmp_path)
    assert case.state is VisitState.FOLLOW_UP  # the joined table asked nothing new: carried on by itself

    case = visit.set_goal(
        vs.CLIENT, case.case_id, vs.GOAL_A, day.deps, expected_version=case.version, actor=vs.ANALYST
    )
    q = vs.question(case, "OUTCOME_COLUMN")  # this goal's outcome has to be told
    case = visit.answer(
        vs.CLIENT, case.case_id, q.question_id, vs.option(q, "converted"), day.deps,
        expected_version=case.version, actor=vs.ANALYST,
    )  # fmt: skip
    view = screen_view(day.storage, case, config_root=None)
    assert case.state is VisitState.APPROVE
    assert view.carried is not None
    assert view.carried.line is not None and "who agreed to be contacted" in view.carried.line
    assert view.carried.line.startswith("For this campaign we also need:")
    folded = {i.item_id: i for i in case.items}
    assert view.carried.approved and all(folded[i].goal_id is None for i in view.carried.approved)
    # the joined files' own items are not drawn again, and nothing meaning-bearing is folded
    assert not [i for i in view.carried.approved if folded[i].kind in {"consent_column", "outcome_column"}]


def test_a_stop_or_a_flag_or_a_change_a_person_made_is_never_carried_on(tmp_path: Path) -> None:
    day, case = _joined(tmp_path)
    items, questions = active_section(case)
    assert case.visit is not None
    check = PlanCheck(digest="x")
    assert carries_on(case, items, questions, check, confirmed=confirmed) is not None

    new = items[0].model_copy(
        update={
            "item_id": "recipe_step.base.newone",
            "upload_id": case.files[0].upload_id,
            "kind": "recipe_step",
            "title": "Turn 'spend' into numbers.",
            "payload": {
                "upload_id": case.files[0].upload_id,
                "step": {"kind": "parse_number", "column": "spend"},
            },
            "payload_digest": "d" * 64,
            "source": ProposalSource.RULES,
            "confidence": AgentConfidence.SURE,
            "state": ProposalState.ACCEPTED,
            "decided_by": None,
            "parked_for": None,
        }
    )
    more = (*items, new)
    assert (
        carries_on(case, more, questions, check, confirmed=confirmed) is not None
    )  # a sure fix is carried on

    flagged = PlanCheck(
        digest="x",
        findings=(
            PlanFinding(
                key="k", level="flag", code="STEP_LOSES_CUSTOMERS", text="Loses 30.", item_id=new.item_id
            ),
        ),
    )
    assert carries_on(case, more, questions, flagged, confirmed=confirmed) is None
    touched = new.model_copy(update={"decided_by": vs.ANALYST})
    assert carries_on(case, (*items, touched), questions, check, confirmed=confirmed) is None
    consent = new.model_copy(
        update={
            "item_id": "consent_column.g.x",
            "kind": "consent_column",
            "payload": {"column": "marketing_opt_in"},
        }
    )
    assert carries_on(case, (*items, consent), questions, check, confirmed=confirmed) is None
    assert CaseStore(day.storage).get(vs.CLIENT, case.case_id).state is VisitState.FOLLOW_UP


def _fix(case: Case, items: tuple[CaseItem, ...], **update: object) -> CaseItem:
    return items[0].model_copy(
        update={
            "item_id": "recipe_step.base.newone",
            "upload_id": case.files[0].upload_id,
            "kind": "recipe_step",
            "title": "Turn 'spend' into numbers.",
            "payload": {
                "upload_id": case.files[0].upload_id,
                "step": {"kind": "parse_number", "column": "spend"},
            },
            "payload_digest": "d" * 64,
            "source": ProposalSource.RULES,
            "confidence": AgentConfidence.SURE,
            "state": ProposalState.ACCEPTED,
            "decided_by": None,
            "parked_for": None,
            **update,
        }
    )


def test_an_ai_item_a_changed_answer_or_a_changed_step_is_not_carried_on(tmp_path: Path) -> None:
    day, case = _joined(tmp_path)
    items, questions = active_section(case)
    check = PlanCheck(digest="x")
    ai = _fix(case, items, source=ProposalSource.AI)
    assert carries_on(case, (*items, ai), questions, check, confirmed=confirmed) is None
    assert carries_on(case, (*items, _fix(case, items)), questions, check, confirmed=confirmed) is not None

    answered = vs.question(
        visit.set_goal(
            vs.CLIENT, case.case_id, vs.GOAL_A, day.deps, expected_version=case.version, actor=vs.ANALYST
        ),
        "OUTCOME_COLUMN",
    )
    changed = answered.model_copy(
        update={"answer": "something else", "assumed": False, "standing_answer_id": None}
    )
    assert carries_on(case, items, (*questions, changed), check, confirmed=confirmed) is None

    # the same sentence on a step that does something else is a new step, not the one approved
    other = _fix(
        case,
        items,
        payload={"upload_id": case.files[0].upload_id, "step": {"kind": "parse_number", "column": "age"}},
    )
    approved = case.visit.approved_before if case.visit is not None else ()
    assert approved
    carry = carry_of(case, (_fix(case, items), other), check, confirmed=confirmed)
    assert carry is not None and other.item_id in {i.item_id for i in carry.new}


def test_a_carried_approval_says_so_in_its_record_and_a_new_months_file_gets_a_whole_plan(
    tmp_path: Path,
) -> None:
    day, case = _joined(tmp_path)
    assert case.state is VisitState.FOLLOW_UP and case.visit is not None and case.visit.approved_before
    carried = [
        e
        for e in CaseStore(day.storage).timeline(vs.CLIENT, case.case_id)
        if e.kind.value == "approval" and e.details.get("carried")
    ]
    assert carried and carried[0].details.get("carried_from", "").startswith("approval.")

    day.world.files["up-next"] = vs.loaded(
        "up-next", "next.csv", _with_goal_columns(_customers()), day.config
    )
    case = visit.add_files(
        vs.CLIENT, case.case_id, [vs.case_file("up-next", day.world)], day.deps,
        expected_version=case.version, actor=vs.ANALYST,
    )  # fmt: skip
    assert case.visit is not None and case.visit.approved_before == ()
