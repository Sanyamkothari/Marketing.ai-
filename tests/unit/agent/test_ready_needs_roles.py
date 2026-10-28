"""A Guided-setup session is never ready while its ID or its outcome is unsettled (DEC-1821; DEC-1804's promise).

M248's eval harness drove two held-out Indian exports through Guided setup the way a hurried person would: tick what
the helper is sure of, reject the rest. The helper's ID and outcome suggestions there are "please check" (a header
read from Hinglish, or a synonym), so they were rejected; nothing was then left to decide, `status_of` said `ready`,
and Approve failed with `VALIDATION_FAILED` for want of an ID and an outcome. A required role nobody has chosen now
keeps the session in review and is asked for by a blocking question.

The frames here are small synthetic ones shaped like those two exports (a romanised-Hindi ID beside a unique mobile
number, a Hinglish or English outcome with two values, other two-valued flags beside it). They are written here;
the held-out generator is never imported outside the harness.
"""

from __future__ import annotations

import random
from typing import Any

import pandas as pd
import pytest

from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET
from engine.agent.contracts import AgentSession, ProposalKind, ProposalState, Question, SessionStatus
from engine.agent.session import (
    accept_recommended,
    answer,
    decide,
    roles_of,
    start_session,
    status_of,
    unsettled_roles,
)
from engine.agent.tools import AgentContext
from engine.config import RunMode
from tests.unit.agent.helpers import context_for, header_help_context

ROWS = 1_200


def in_01_shaped() -> pd.DataFrame:
    """Romanised-Hindi headers; the ID beside a unique mobile, so the ID is read from its header ("check")."""
    rng = random.Random(1)
    return pd.DataFrame(
        {
            "Grahak ID": [f"G{100_001 + 3 * i:06d}" for i in range(ROWS)],
            "Mobile No": [f"98{20_000_000 + 7 * i}" for i in range(ROWS)],
            "Shahar": [rng.choice(("Mumbai", "Pune", "Nagpur")) for _ in range(ROWS)],
            "Visits (7 din)": [rng.randrange(0, 15) for _ in range(ROWS)],
            "Kitne mahine se": [rng.randrange(1, 96) for _ in range(ROWS)],
            "Yojana": [rng.choice(("Basic", "Gold", "Platinum")) for _ in range(ROWS)],
            "Sakriya": [rng.choice(("haan", "nahi")) for _ in range(ROWS)],
            "SMS ki anumati": [rng.choice(("haan", "haan", "nahi")) for _ in range(ROWS)],
            "Kharida (30 din)": [rng.choice(("1", "0", "0")) for _ in range(ROWS)],
        }
    )


def in_02_shaped() -> pd.DataFrame:
    """An ID the helper is sure of; an outcome found by synonym ("check"); other yes/no flags beside it."""
    rng = random.Random(2)
    return pd.DataFrame(
        {
            "grahak_sankhya": [f"C{100_002 + 3 * i:06d}" for i in range(ROWS)],
            "City": [rng.choice(("Mumbai", "Pune", "Nagpur")) for _ in range(ROWS)],
            "Hafte ki visits": [rng.randrange(0, 15) for _ in range(ROWS)],
            "Tenure (months)": [rng.randrange(1, 96) for _ in range(ROWS)],
            "Plan": [rng.choice(("Silver", "Gold", "Diamond")) for _ in range(ROWS)],
            "Active?": [rng.choice(("Haan", "Nahin")) for _ in range(ROWS)],
            "Marketing OK": [rng.choice(("Haan", "Haan", "Nahin")) for _ in range(ROWS)],
            "Bought in 30 days": [rng.choice(("haan", "nahi", "nahi")) for _ in range(ROWS)],
        }
    )


def _hurried(session: AgentSession, ctx: AgentContext) -> AgentSession:
    """The harness's person: tick what the helper is sure of, then reject every suggestion still pending."""
    session = accept_recommended(session, ctx)
    pending = [p.proposal_id for p in session.proposals if p.state is ProposalState.PENDING]
    return decide(session, ctx, [(pid, ProposalState.REJECTED, None) for pid in pending])


def _role_question(session: AgentSession, path: str) -> Question:
    """The one open, blocking question whose answers choose the column for `path`."""
    found = [
        q
        for q in session.questions
        if q.answer is None
        and q.blocking
        and any(o.proposal is not None and o.proposal.path == path for o in q.options)
    ]
    assert len(found) == 1, [q.text for q in session.questions]
    return found[0]


def _pick(session: AgentSession, ctx: AgentContext, path: str, column: str) -> AgentSession:
    question = _role_question(session, path)
    option = next(o for o in question.options if o.proposal is not None and o.proposal.value == column)
    return answer(session, ctx, question.question_id, option.option_id)


def _role_states(session: AgentSession, path: str) -> list[tuple[Any, ProposalState]]:
    return [(p.value, p.state) for p in session.proposals if p.kind is ProposalKind.ROLE and p.path == path]


@pytest.mark.parametrize("make", [context_for, header_help_context], ids=["default_root", "agentic_root"])
def test_rejecting_the_outcome_asks_for_it_instead_of_saying_ready(make: Any) -> None:
    """in_02's shape: the ID is ticked, the outcome suggestion ("check") is rejected. Before DEC-1821: ready."""
    ctx = make(in_02_shaped())
    started = start_session(ctx, session_id="s-in02")
    assert started.stop_reason is None, started.stop_reason
    assert _role_states(started, ROLE_TARGET) == [("Bought in 30 days", ProposalState.PENDING)]

    session = _hurried(started, ctx)

    assert _role_states(session, ROLE_PRIMARY_KEY) == [("grahak_sankhya", ProposalState.ACCEPTED)]
    assert _role_states(session, ROLE_TARGET) == [("Bought in 30 days", ProposalState.REJECTED)]
    assert session.status is not SessionStatus.READY
    assert session.status is SessionStatus.NEEDS_REVIEW
    assert unsettled_roles(session) == (ROLE_TARGET,)
    question = _role_question(session, ROLE_TARGET)
    offered = [o.proposal.value for o in question.options if o.proposal is not None]
    assert "Bought in 30 days" in offered and "grahak_sankhya" not in offered
    assert offered[-1] == "Bought in 30 days"  # the rejected suggestion is offered, last
    assert question.question_id in session.undecided

    chosen = _pick(session, ctx, ROLE_TARGET, "Bought in 30 days")
    chosen = _hurried(chosen, ctx)
    assert chosen.status is SessionStatus.READY
    assert roles_of(chosen) == ("grahak_sankhya", "Bought in 30 days")
    assert unsettled_roles(chosen) == ()


def test_rejecting_both_header_read_roles_asks_for_both() -> None:
    """in_01's shape with the header reading on: ID and outcome are both read from Hinglish headers ("check",
    never ticked), so the hurried person rejects both. Before DEC-1821 the session said ready with neither."""
    ctx = header_help_context(in_01_shaped())
    started = start_session(ctx, session_id="s-in01")
    assert started.stop_reason is None, started.stop_reason
    assert [(p.value, p.from_headers) for p in started.proposals if p.kind is ProposalKind.ROLE] == [
        ("Grahak ID", True),
        ("Kharida (30 din)", True),
    ]

    session = _hurried(started, ctx)

    assert session.status is SessionStatus.NEEDS_REVIEW
    assert unsettled_roles(session) == (ROLE_PRIMARY_KEY, ROLE_TARGET)
    ids = [o.proposal.value for o in _role_question(session, ROLE_PRIMARY_KEY).options if o.proposal]
    assert ids[-1] == "Grahak ID" and "Mobile No" in ids
    _role_question(session, ROLE_TARGET)

    session = _pick(session, ctx, ROLE_PRIMARY_KEY, "Grahak ID")
    assert roles_of(session)[0] == "Grahak ID"
    assert session.status is not SessionStatus.READY  # the outcome is still unchosen
    assert unsettled_roles(session) == (ROLE_TARGET,)
    session = _pick(session, ctx, ROLE_TARGET, "Kharida (30 din)")
    session = _hurried(session, ctx)
    assert session.stop_reason is None, session.stop_reason
    assert session.status is SessionStatus.READY
    assert roles_of(session) == ("Grahak ID", "Kharida (30 din)")


def test_switch_off_in_01_shape_never_says_ready_without_an_id() -> None:
    """The same file with the switch off: the outcome is a question, and the ID suggestion ("check") rejected."""
    ctx = context_for(in_01_shaped())
    session = _hurried(start_session(ctx, session_id="s-in01-off"), ctx)
    assert _role_states(session, ROLE_PRIMARY_KEY) == [("Grahak ID", ProposalState.REJECTED)]
    assert session.status is SessionStatus.NEEDS_REVIEW
    assert ROLE_PRIMARY_KEY in unsettled_roles(session)
    _role_question(session, ROLE_PRIMARY_KEY)


def test_rejecting_the_role_an_answer_chose_opens_the_question_again() -> None:
    ctx = context_for(in_02_shaped())
    session = _pick(
        _hurried(start_session(ctx, session_id="s-again"), ctx), ctx, ROLE_TARGET, "Bought in 30 days"
    )
    asked = next(
        q for q in session.questions if q.answer is not None and q.question_id.endswith("role-outcome")
    )
    chosen = next(p for p in session.proposals if p.path == ROLE_TARGET and p.state is ProposalState.ACCEPTED)

    again = decide(session, ctx, [(chosen.proposal_id, ProposalState.REJECTED, None)])

    assert again.status is SessionStatus.NEEDS_REVIEW
    reopened = _role_question(again, ROLE_TARGET)
    assert reopened.question_id == asked.question_id
    assert len({q.question_id for q in again.questions}) == len(again.questions)


def test_status_of_alone_never_says_ready_without_the_roles() -> None:
    """The rule itself, whatever built the session: no pending suggestion and no open question is not enough."""
    ctx = context_for(in_02_shaped())
    ready = _hurried(
        _pick(_hurried(start_session(ctx, session_id="s-rule"), ctx), ctx, ROLE_TARGET, "Bought in 30 days"),
        ctx,
    )
    assert status_of(ready) is SessionStatus.READY

    def without(session: AgentSession, path: str) -> AgentSession:
        return session.model_copy(
            update={
                "proposals": tuple(
                    (
                        p.model_copy(update={"state": ProposalState.REJECTED})
                        if p.kind is ProposalKind.ROLE and p.path == path
                        else p
                    )
                    for p in session.proposals
                )
            }
        )

    for path in (ROLE_PRIMARY_KEY, ROLE_TARGET):
        broken = without(ready, path)
        assert broken.undecided == ()
        assert status_of(broken) is SessionStatus.NEEDS_REVIEW, path
    # Scoring needs the ID only; an Ask-your-data session needs no role.
    scoring = without(ready, ROLE_TARGET).model_copy(update={"mode": RunMode.SCORE.value})
    assert unsettled_roles(scoring) == ()
    assert status_of(scoring) is SessionStatus.READY
    assert unsettled_roles(
        without(ready, ROLE_PRIMARY_KEY).model_copy(update={"mode": RunMode.SCORE.value})
    ) == (ROLE_PRIMARY_KEY,)
