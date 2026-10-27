"""Guided setup's Approve and the chat with Data Doctor on (Plan O M246, DEC-1816; M233's cross-branch request (2)).

* O10 (acceptance): an AI step changing 70% of a column is refused (`AI_STEP_CHANGES_TOO_MANY`, the code M238 named
  for the plan's `AI_STEP_CHANGE_TOO_LARGE`); at 40% it is offered, pending, and carries `AI_STEP_CHANGES_MANY`,
  which Approve lists.
* M233's approval-time facts at Approve: the preview shown, decision O3's reshape guards, the exact sentence and
  the role (`engine.doctor.plan_g_approval`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.access.roles import LOCAL_OPERATOR, Principal, Role
from engine.agent.contracts import (
    AgentSession,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalState,
    Question,
    QuestionOption,
    RecipeReceipt,
    RecipeStep,
    RecipeStepKind,
    StepReceipt,
    recipe_hash,
)
from engine.agent.edits import add_edit
from engine.agent.session import start_session
from engine.doctor.plan_g_approval import (
    WARNING_MESSAGES,
    PreviewShown,
    approval_of,
    approval_refusal,
    read_preview,
    read_proposers,
    record_preview,
    record_proposers,
)
from engine.storage import LocalStorage
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.unit.agent.helpers import context_for
from tests.unit.doctor.test_policy_runtime import (
    _chat,
    _look,
    _step,
)

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


ASHA = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
VIEWER = Principal(user_id="u-vic", username="vic", roles=frozenset({Role.VIEWER}), kind="user")


@pytest.fixture(scope="module")
def plans() -> pd.DataFrame:
    """A column where 70% of the values are `PP` and one where 40% are."""
    frame = messy_frame()
    frame["plan"] = ["PP" if index % 10 < 7 else "Prepaid" for index in range(len(frame))]
    frame["plan_b"] = ["PP" if index % 10 < 4 else "Prepaid" for index in range(len(frame))]
    return frame


# ---------------------------------------------------------------------------
# O10 through the chat
# ---------------------------------------------------------------------------
def test_an_ai_step_changing_70_percent_is_refused_and_one_at_40_percent_carries_a_warning(
    plans: pd.DataFrame,
) -> None:
    heavy = _step("map_values", "plan", mapping={"PP": "Prepaid"})
    refused = _chat(plans, _look("plan"), heavy, heavy)
    assert refused.proposals == () and refused.blocked_by == "AI_STEP_CHANGES_TOO_MANY"
    light = _step("map_values", "plan_b", mapping={"PP": "Prepaid"})
    turn = _chat(plans, _look("plan_b"), light)
    (made,) = turn.proposals
    assert made.state is ProposalState.PENDING and made.source is ProposalSource.AI
    (step_result,) = [r for r in turn.tool_results if r.evidence_id == made.evidence_ids[0]]
    assert step_result.result["warnings"] == ["AI_STEP_CHANGES_MANY"]


def test_approve_lists_the_warning_of_an_accepted_ai_step(
    plans: pd.DataFrame,
) -> None:
    turn = _chat(plans, _look("plan_b"), _step("map_values", "plan_b", mapping={"PP": "Prepaid"}))
    ctx = context_for(plans)
    session = start_session(ctx, session_id="s1")
    accepted = turn.proposals[0].model_copy(
        update={"state": ProposalState.ACCEPTED, "decided_by": DecidedBy.USER}
    )
    session = session.model_copy(
        update={
            "proposals": (*session.proposals, accepted),
            "tool_results": (*session.tool_results, *turn.tool_results),
        }
    )
    approval = approval_of(
        session, file_name="plans.csv", preview=None, recipe_hash=None, training=True, approval_required=True
    )
    (warning,) = approval.warnings
    assert (
        warning.code == "AI_STEP_CHANGES_MANY" and warning.message == WARNING_MESSAGES["AI_STEP_CHANGES_MANY"]
    )


# ---------------------------------------------------------------------------
# The approval-time facts: preview shown, reshape guards, exact sentence, role
# ---------------------------------------------------------------------------
def _session(frame: pd.DataFrame) -> AgentSession:
    return start_session(context_for(frame), session_id="s1")


def _keep_latest(session: AgentSession, *, offered_ticked: bool, answered: bool) -> AgentSession:
    evidence = session.tool_results[0].evidence_id
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.KEEP_LATEST_PER_ID,
        column="customer_id",
        params={"time_column": "signup_date"},
    )
    proposal = Proposal(
        proposal_id="p-latest",
        kind=ProposalKind.RECIPE_STEP,
        title="Keep the latest row of each customer",
        reason="Several rows per customer.",
        step=step,
        evidence_ids=(evidence,),
        confidence="check",  # type: ignore[arg-type]
        state=ProposalState.ACCEPTED,
        decided_by=DecidedBy.AGENT if offered_ticked else DecidedBy.USER,
    )
    question = Question(
        question_id="q-latest",
        text="Keep only the latest row of each customer?",
        options=(
            QuestionOption(
                option_id="yes",
                label="Yes",
                proposal=proposal.model_copy(update={"state": ProposalState.PENDING, "decided_by": None}),
            ),
            QuestionOption(option_id="no", label="No"),
        ),
        answer="yes" if answered else None,
    )
    return session.model_copy(
        update={"proposals": (*session.proposals, proposal), "questions": (*session.questions, question)}
    )


def _preview(steps: tuple[RecipeStep, ...], *, rows_in: int = 1_000, rows_out: int = 900) -> PreviewShown:
    receipt = RecipeReceipt(
        recipe_hash=recipe_hash(steps),
        upload_id="up-1",
        rows_in=rows_in,
        rows_out=rows_out,
        columns_in=5,
        columns_out=5,
        steps=tuple(
            StepReceipt(
                order=s.order,
                kind=s.kind,
                column=s.column,
                rows=rows_in,
                changed=rows_in - rows_out,
                failed=0,
            )
            for s in steps
        ),
        created_at=NOW,
    )
    return PreviewShown(upload_id="up-1", recipe_hash=receipt.recipe_hash, receipt=receipt, shown_at=NOW)


def _steps(session: AgentSession) -> tuple[RecipeStep, ...]:
    from engine.agent.advisor import recipe_steps

    return recipe_steps(p for p in session.proposals if p.state is ProposalState.ACCEPTED)


def test_a_change_to_the_rows_needs_its_question_its_preview_and_never_a_tick(plans: pd.DataFrame) -> None:
    session = _keep_latest(_session(plans), offered_ticked=True, answered=False)
    steps = _steps(session)
    approval = approval_of(
        session,
        file_name="f.csv",
        preview=None,
        recipe_hash=recipe_hash(steps),
        training=True,
        approval_required=False,
    )
    assert set(approval.reshape_missing) == {
        "its question has not been answered",
        "it was offered already ticked, and a change to the rows never is",
        "its preview (rows in and rows out) has not been shown",
    }
    found = approval_refusal(
        approval, approver=ASHA, sentence_read=approval.sentence, approval_required=False
    )
    assert found is not None and found.code == "RESHAPE_GUARD_MISSING"


def test_a_shown_preview_and_the_exact_sentence_let_a_change_to_the_rows_be_approved(
    plans: pd.DataFrame,
) -> None:
    session = _keep_latest(_session(plans), offered_ticked=False, answered=True)
    steps = _steps(session)
    preview = _preview(steps)
    approval = approval_of(
        session,
        file_name="f.csv",
        preview=preview,
        recipe_hash=recipe_hash(steps),
        training=True,
        approval_required=True,
        proposers={"p-latest": ("u-meera",)},  # Meera answered its question; Asha approves
    )
    assert approval.preview_shown and approval.reshape_missing == ()
    (special,) = approval.approve_class
    assert (
        special.kind == "keep_latest_per_id"
        and "100 of the preview's 1,000 rows are left out" in special.sentence
    )
    assert special.sentence in approval.sentence
    refused = approval_refusal(approval, approver=ASHA, sentence_read=None, approval_required=True)
    assert refused is not None and refused.code == "APPROVAL_SENTENCE_CHANGED"
    role = approval_refusal(
        approval, approver=VIEWER, sentence_read=approval.sentence, approval_required=True
    )
    assert role is not None and role.code == "TOOL_ROLE_REQUIRED"
    assert (
        approval_refusal(approval, approver=ASHA, sentence_read=approval.sentence, approval_required=True)
        is None
    )
    stale = approval_of(
        session,
        file_name="f.csv",
        preview=preview,
        recipe_hash="0" * 64,
        training=True,
        approval_required=True,
    )
    assert not stale.preview_shown, "a preview of other steps does not count"


def test_two_percent_or_less_left_out_needs_no_own_yes(plans: pd.DataFrame) -> None:
    session = _keep_latest(_session(plans), offered_ticked=False, answered=True)
    steps = _steps(session)
    approval = approval_of(
        session,
        file_name="f.csv",
        preview=_preview(steps, rows_out=985),
        recipe_hash=recipe_hash(steps),
        training=True,
        approval_required=True,
    )
    assert approval.approve_class == ()
    assert (
        approval_refusal(approval, approver=LOCAL_OPERATOR, sentence_read=None, approval_required=True)
        is None
    )


def test_a_persons_remove_rows_needs_the_sentence_read_back(plans: pd.DataFrame, tmp_path: Path) -> None:
    ctx = context_for(plans)
    storage = LocalStorage(tmp_path)
    before = _session(plans)
    session = add_edit(
        before,
        ctx,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="region",
        params={"operator": "equals", "values": ["TEST"]},
    )
    record_proposers(storage, session.upload_id, before, session, "u-meera")  # Meera added it by hand
    edit = next(
        p for p in session.proposals if p.step is not None and p.step.kind is RecipeStepKind.REMOVE_ROWS
    )
    approval = approval_of(
        session,
        file_name="f.csv",
        preview=None,
        recipe_hash=None,
        training=True,
        approval_required=True,
        proposers=read_proposers(storage, session.upload_id),
    )
    (special,) = approval.approve_class
    assert special.proposal_id == edit.proposal_id and special.role is Role.ANALYST
    assert approval.sentence.startswith("Prepare f.csv with") and approval.sentence.endswith(
        "Nothing else changes."
    )
    assert (
        approval_refusal(approval, approver=ASHA, sentence_read="Prepare it.", approval_required=True)
        is not None
    )
    assert (
        approval_refusal(approval, approver=ASHA, sentence_read=approval.sentence, approval_required=True)
        is None
    )


def test_the_preview_shown_is_recorded_beside_the_session(tmp_path: object) -> None:
    storage = LocalStorage(Path(str(tmp_path)))
    assert read_preview(storage, "up-1") is None
    steps = (RecipeStep(order=1, kind=RecipeStepKind.FILL_MISSING, column="age", params={"value": 0}),)
    shown = _preview(steps)
    record_preview(
        storage, "up-1", recipe_hash=shown.recipe_hash, receipt=shown.receipt, actor="u-asha", at=NOW
    )
    again = read_preview(storage, "up-1")
    assert again is not None and again.recipe_hash == recipe_hash(steps) and again.shown_by == "u-asha"


# ---------------------------------------------------------------------------
# Separation of duties at Guided setup's Approve (O17; review fix)
# ---------------------------------------------------------------------------
MEERA = Principal(user_id="u-meera", username="meera", roles=frozenset({Role.ANALYST}), kind="user")


def _remove_rows_session(plans: pd.DataFrame) -> tuple[AgentSession, AgentSession]:
    before = _session(plans)
    after = add_edit(
        before,
        context_for(plans),
        kind=RecipeStepKind.REMOVE_ROWS,
        column="region",
        params={"operator": "equals", "values": ["TEST"]},
    )
    return before, after


def test_the_person_who_chose_an_approve_class_step_cannot_approve_it_when_separation_is_on(
    plans: pd.DataFrame, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path)
    before, session = _remove_rows_session(plans)
    record_proposers(storage, session.upload_id, before, session, "u-asha")
    assert set(read_proposers(storage, session.upload_id).values()) == {("u-asha",)}

    def refusal(approver: Principal, required: bool) -> str | None:
        approval = approval_of(
            session,
            file_name="f.csv",
            preview=None,
            recipe_hash=None,
            training=True,
            approval_required=required,
            proposers=read_proposers(storage, session.upload_id),
        )
        found = approval_refusal(
            approval, approver=approver, sentence_read=approval.sentence, approval_required=required
        )
        return None if found is None else found.code

    assert refusal(ASHA, True) == "POLICY_ROLE_SEPARATION", "Asha added it, so someone else approves it"
    assert refusal(MEERA, True) is None
    assert refusal(ASHA, False) is None, "without approval_required the proposer may approve"
    assert refusal(LOCAL_OPERATOR, True) is None, "the local operator (sign-in off) is everyone at once"


def test_an_approve_class_step_nobody_recorded_choosing_is_refused_when_separation_is_on(
    plans: pd.DataFrame,
) -> None:
    _before, session = _remove_rows_session(plans)
    approval = approval_of(
        session, file_name="f.csv", preview=None, recipe_hash=None, training=True, approval_required=True
    )
    found = approval_refusal(
        approval, approver=MEERA, sentence_read=approval.sentence, approval_required=True
    )
    assert found is not None and found.code == "POLICY_ROLE_SEPARATION"
    assert "not recorded" in str(found.detail["reason"])


def test_only_what_the_persons_action_ticked_or_added_is_recorded_as_theirs(
    plans: pd.DataFrame, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path)
    before, session = _remove_rows_session(plans)
    record_proposers(storage, session.upload_id, before, session, None)
    assert read_proposers(storage, session.upload_id) == {}, "no signed-in person: nothing recorded"
    record_proposers(storage, session.upload_id, before, session, "u-asha")
    record_proposers(storage, session.upload_id, session, session, "u-meera")  # changed nothing
    record_proposers(storage, session.upload_id, before, session, "u-asha")  # recorded once
    assert list(read_proposers(storage, session.upload_id).values()) == [("u-asha",)]
