"""Agent contracts (Plan G M70): a suggestion is never a change, and every claim cites evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    DataRecipe,
    DecidedBy,
    Proposal,
    ProposalKind,
    ProposalState,
    Question,
    QuestionOption,
    RecipeStep,
    RecipeStepKind,
    SessionStatus,
    ToolResult,
    recipe_hash,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _evidence(evidence_id: str = "e1") -> ToolResult:
    return ToolResult(evidence_id=evidence_id, tool="get_profile", result={"rows": 10}, created_at=NOW)


def _step(order: int = 1, **changes: Any) -> RecipeStep:
    values: dict[str, Any] = {"order": order, "kind": RecipeStepKind.PARSE_NUMBER, "column": "bill"}
    values.update(changes)
    return RecipeStep(**values)


def _setting(**changes: Any) -> Proposal:
    values: dict[str, Any] = {
        "proposal_id": "p1",
        "kind": ProposalKind.SETTING,
        "title": "Split by date",
        "reason": "Your data has dates.",
        "path": "split.type",
        "value": "time_based",
        "suggested_value": "time_based",
        "evidence_ids": ("e1",),
        "confidence": AgentConfidence.SURE,
    }
    values.update(changes)
    return Proposal(**values)


def _session(**changes: Any) -> AgentSession:
    values: dict[str, Any] = {
        "session_id": "s1",
        "upload_id": "u1",
        "use_case_id": "targeted-advertisement",
        "mode": "train",
        "agent_name": "Targeted Advertisement helper",
        "status": SessionStatus.NEEDS_REVIEW,
        "tool_results": (_evidence(),),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return AgentSession(**values)


def test_the_recipe_hash_covers_logic_not_wording() -> None:
    a = (_step(reason="Stored as text", proposal_id="p1"),)
    b = (_step(reason="Different words", proposal_id="p9", decided_by=DecidedBy.USER),)
    c = (_step(params={"percent": True}),)
    assert recipe_hash(a) == recipe_hash(b)
    assert recipe_hash(a) != recipe_hash(c)


def test_a_recipe_refuses_a_hash_that_does_not_match_its_steps() -> None:
    steps = (_step(1), _step(2, kind=RecipeStepKind.DROP_COLUMN, column="email"))
    recipe = DataRecipe(
        recipe_id="r1",
        use_case_id="uc",
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        created_at=NOW,
    )
    assert recipe.recipe_hash.startswith("sha256:")
    with pytest.raises(ValidationError):
        DataRecipe(
            recipe_id="r1",
            use_case_id="uc",
            source_fingerprint="f",
            steps=steps,
            recipe_hash="sha256:0",
            created_at=NOW,
        )


def test_a_recipe_numbers_its_steps_one_to_n() -> None:
    steps = (_step(1), _step(3))
    with pytest.raises(ValidationError):
        DataRecipe(
            recipe_id="r1",
            use_case_id="uc",
            source_fingerprint="f",
            steps=steps,
            recipe_hash=recipe_hash(steps),
            created_at=NOW,
        )


def test_derive_names_its_new_column_and_drop_creates_none() -> None:
    with pytest.raises(ValidationError):
        _step(kind=RecipeStepKind.DERIVE)
    assert _step(kind=RecipeStepKind.DERIVE, new_column="spend_per_order").new_column == "spend_per_order"
    with pytest.raises(ValidationError):
        _step(kind=RecipeStepKind.DROP_COLUMN, new_column="x")


def test_a_proposal_without_evidence_is_refused() -> None:
    with pytest.raises(ValidationError):
        _setting(evidence_ids=())


def test_a_recipe_proposal_carries_its_step_and_others_carry_a_path() -> None:
    step_proposal = _setting(kind=ProposalKind.RECIPE_STEP, path=None, step=_step())
    assert step_proposal.step is not None
    with pytest.raises(ValidationError):
        _setting(kind=ProposalKind.RECIPE_STEP, path=None)
    with pytest.raises(ValidationError):
        _setting(step=_step())
    with pytest.raises(ValidationError):
        _setting(path=None)


def test_decided_by_is_set_exactly_when_a_proposal_is_decided() -> None:
    assert _setting(state=ProposalState.ACCEPTED, decided_by=DecidedBy.USER).decided_by is DecidedBy.USER
    with pytest.raises(ValidationError):
        _setting(state=ProposalState.ACCEPTED)
    with pytest.raises(ValidationError):
        _setting(decided_by=DecidedBy.AGENT)


def test_a_question_offers_real_choices_and_an_answer_is_one_of_them() -> None:
    options = (QuestionOption(option_id="hide", label="Hide"), QuestionOption(option_id="keep", label="Keep"))
    assert (
        Question(question_id="q1", text="Hide refund_date?", options=options, answer="hide").answer == "hide"
    )
    with pytest.raises(ValidationError):
        Question(question_id="q1", text="?", options=options[:1])
    with pytest.raises(ValidationError):
        Question(question_id="q1", text="?", options=options, answer="maybe")


def test_a_session_refuses_evidence_it_does_not_hold() -> None:
    with pytest.raises(ValidationError):
        _session(proposals=(_setting(evidence_ids=("e404",)),))


def test_undecided_lists_pending_proposals_and_open_blocking_questions() -> None:
    options = (QuestionOption(option_id="a", label="A"), QuestionOption(option_id="b", label="B"))
    session = _session(
        proposals=(
            _setting(),
            _setting(proposal_id="p2", state=ProposalState.REJECTED, decided_by=DecidedBy.USER),
        ),
        questions=(
            Question(question_id="q1", text="?", options=options),
            Question(question_id="q2", text="?", options=options, blocking=False),
            Question(question_id="q3", text="?", options=options, answer="a"),
        ),
    )
    assert session.undecided == ("p1", "q1")


def test_an_applied_session_names_the_upload_it_wrote() -> None:
    with pytest.raises(ValidationError):
        _session(status=SessionStatus.APPLIED)
    assert _session(status=SessionStatus.APPLIED, applied_upload_id="u2").applied_upload_id == "u2"


def test_contracts_round_trip_through_json() -> None:
    session = _session(proposals=(_setting(),))
    assert AgentSession.model_validate_json(session.model_dump_json()) == session
