"""One quoting style in what Guided setup says (UI audit §8.4 item 16).

A reason used to read `("'Basic' → 'BASIC'", "'PREMIUM' → 'premium'")`: pairs already quoted by
`formats.py` were wrapped in a second pair of double quotes by the advisor. Every value a person reads
is now quoted one way, `'value'` (`engine.agent.untrusted.quoted`), and no message shows Python syntax.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from engine.agent.advisor import advise
from engine.agent.contracts import ProposalState
from engine.agent.grounding import numbers_in
from engine.agent.session import SessionError, decide, start_session
from engine.agent.untrusted import plain_value, quoted
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.unit.agent.helpers import context_for

PYTHON_SYNTAX = re.compile(r"\(\"|\"'|'\"|\[\'|\'\]|\bTrue\b|\bFalse\b|\bNone\b")


@pytest.fixture(scope="module")
def advice() -> Any:
    return advise(context_for(messy_frame()))


def test_values_are_quoted_one_way() -> None:
    assert quoted("Basic") == "'Basic'"
    assert quoted("O'Brien") == "'O'Brien'", "an apostrophe does not switch to double quotes"
    assert quoted(" Delhi\t") == "' Delhi\\t'", "spaces kept, a tab shown as its escape"
    assert quoted("a\\b") == "'a\\b'", "a backslash is not doubled"
    assert plain_value(["weekly", "monthly"]) == "'weekly', 'monthly'"
    assert plain_value(True) == "true" and plain_value(10) == "10" and plain_value(None) == "an empty value"
    assert plain_value({"Basic": "BASIC"}) == "'Basic' → 'BASIC'"


def test_a_merge_reason_lists_its_pairs_plainly(advice: Any) -> None:
    merge = next(p for p in advice.proposals if p.title == "Merge different spellings in 'plan_tier'")
    assert "'Basic' → 'basic'" in merge.reason
    assert re.search(r"\('[^']+' → '[^']+'(, '[^']+' → '[^']+')*\)", merge.reason), merge.reason


def test_no_reason_title_or_question_shows_python_syntax(advice: Any) -> None:
    texts = [p.title for p in advice.proposals] + [p.reason for p in advice.proposals]
    texts += [q.text for q in advice.questions]
    assert texts
    for text in texts:
        assert not PYTHON_SYNTAX.search(text), text
    numbers = next(p for p in advice.proposals if p.title == "Turn 'ad_ctr_90d' into numbers")
    assert re.search(r"stored as text \('[^']+'(, '[^']+')*\)", numbers.reason), numbers.reason
    boolean = next(p for p in advice.proposals if "'is_premium'" in p.title)
    assert re.search(r"spelled several ways: '[^']+' \(\d+\)", boolean.reason), boolean.reason


def test_quoted_values_are_still_numbers_to_the_grounding_check(advice: Any) -> None:
    """Only a quoted column name is skipped as a number; a quoted value stays a claim to ground."""
    numbers = next(p for p in advice.proposals if p.title == "Turn 'ad_ctr_90d' into numbers")
    assert numbers_in(numbers.reason, ["ad_ctr_90d"])
    assert numbers_in(numbers.title, ["ad_ctr_90d"]) == []


def test_a_refused_value_is_named_without_python_syntax() -> None:
    ctx = context_for(messy_frame())
    session = start_session(ctx, session_id="s1")
    split = next(p for p in session.proposals if p.path == "split.type")
    with pytest.raises(SessionError) as refused:
        decide(session, ctx, [(split.proposal_id, ProposalState.ACCEPTED, ["a", "b"])])
    assert refused.value.code == "AGENT_VALUE_NOT_ALLOWED"
    assert refused.value.message.startswith("'a', 'b' is not a value"), refused.value.message
    with pytest.raises(SessionError) as unknown:
        decide(session, ctx, [("nope", ProposalState.ACCEPTED, None)])
    assert "'nope'" in unknown.value.message
