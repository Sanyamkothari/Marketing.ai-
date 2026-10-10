"""Plan L M153 (DEC-1531): the advisor suggests a range only where a column's name says it, never ticked."""

from __future__ import annotations

import numpy as np

from engine.agent.advisor import NEVER_TICKED_STEPS, advise
from engine.agent.contracts import AgentConfidence, RecipeStepKind
from tests.unit.agent.helpers import context_for, synthetic


def ranges(frame):  # type: ignore[no-untyped-def]
    advice = advise(context_for(frame))
    return [p for p in advice.proposals if p.step is not None and p.step.kind is RecipeStepKind.VALID_RANGE]


def test_an_impossible_age_is_a_check_suggestion_never_ticked() -> None:
    frame = synthetic(rows=2000)
    rng = np.random.default_rng(1)
    frame["customer_age"] = rng.integers(18, 80, size=len(frame))
    frame.loc[[3, 9, 27], "customer_age"] = [212, -3, 999]
    (proposal,) = ranges(frame)
    assert proposal.step is not None
    assert proposal.step.column == "customer_age"
    assert dict(proposal.step.params) == {"minimum": 0, "maximum": 120}
    assert proposal.confidence is AgentConfidence.CHECK
    assert RecipeStepKind.VALID_RANGE in NEVER_TICKED_STEPS
    assert "3 rows" in proposal.reason and "212" in proposal.reason


def test_a_percentage_on_the_0_to_100_scale_gets_its_range() -> None:
    frame = synthetic(rows=2000)
    frame["discount_pct"] = (np.arange(len(frame)) % 60).astype(float)
    frame.loc[[5, 6], "discount_pct"] = [150.0, -20.0]
    (proposal,) = ranges(frame)
    assert dict(proposal.step.params) == {"minimum": 0, "maximum": 100}  # type: ignore[union-attr]


def test_no_suggestion_without_the_name_or_with_too_many_outside() -> None:
    frame = synthetic(rows=2000)
    frame["usage"] = 500.0  # "usage" holds "age" but not as a word
    frame["age_days"] = np.arange(len(frame)) * 10  # most values above 120: the name does not mean years
    frame["share_pct"] = np.linspace(0, 0.9, len(frame))  # a 0-1 share, not 0-100
    frame.loc[0, "share_pct"] = 3.0
    assert ranges(frame) == []
