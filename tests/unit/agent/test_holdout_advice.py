"""Plan J M109: Guided setup proposes how many customers to hold back, and an explore share, from the planner.

A campaign that holds back too few customers cannot show an effect of 1 to 3 points, and reads as a
failure when it worked. When a use case contacts customers, the advisor now sizes the held-back share
with `engine.measurement.planner` (the closed form M93 pinned) and proposes it as a **check**
suggestion of `actions.control_group_fraction`, the one run setting that holds the share. The explore
share (`actions.explore_fraction`) and a persistent holdout are deployment settings that no run can
override, so those are recorded as assumptions in plain words, never as a setting that would do nothing.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from engine.agent.advisor import Advice, advise
from engine.agent.contracts import AgentConfidence, Proposal, ProposalKind
from engine.config import load_use_case, overridable_paths
from engine.holdout.spec import HoldoutConfig
from engine.measurement.planner import arm_sizes, mde_two_proportions
from engine.pilot.plain import jargon_in
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for

PATH = "actions.control_group_fraction"
UC = "targeted-advertisement"


def _frame(rows: int, positive_rate: float = 0.12) -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id=UC, rows=rows, variant="clean", positive_rate=positive_rate))


def _proposal(advice: Advice, path: str = PATH) -> Proposal | None:
    return next((p for p in advice.proposals if p.kind is ProposalKind.SETTING and p.path == path), None)


def test_a_file_too_small_for_ten_percent_gets_a_bigger_share_sized_by_the_planner() -> None:
    advice = advise(context_for(_frame(20_000)))
    proposal = _proposal(advice)
    assert proposal is not None, [p.path for p in advice.proposals]
    value = proposal.value
    assert isinstance(value, float) and 0.10 < value <= 0.50
    assert proposal.confidence is AgentConfidence.CHECK  # a person decides how many customers go without
    assert PATH in overridable_paths(load_use_case(UC))
    assert "points" in proposal.reason and "1 to 3 points" in proposal.reason
    assert not jargon_in(proposal.title) and not jargon_in(proposal.reason), proposal.reason


def test_the_proposed_share_is_the_smallest_whole_percent_that_sees_two_points() -> None:
    frame = _frame(20_000)
    rate = float(frame["converted_30d"].mean())
    proposal = _proposal(advise(context_for(frame)))
    assert proposal is not None
    assert isinstance(proposal.value, float)
    share = proposal.value
    seen = mde_two_proportions(*arm_sizes(len(frame), share), rate)
    assert seen.absolute is not None and seen.absolute <= 0.02 + 1e-9
    one_less = mde_two_proportions(*arm_sizes(len(frame), round(share - 0.01, 2)), rate)
    assert one_less.absolute is None or one_less.absolute > 0.02


def test_a_file_that_already_sees_two_points_is_left_alone() -> None:
    advice = advise(context_for(_frame(200_000)))
    assert _proposal(advice) is None
    assert not any("hold back" in text.lower() for text in advice.assumptions)


def test_a_file_too_small_even_for_half_says_so_and_proposes_nothing() -> None:
    advice = advise(context_for(_frame(3_000)))
    assert _proposal(advice) is None
    notes = [text for text in advice.assumptions if "held back" in text]
    assert len(notes) == 1, advice.assumptions
    assert "Even holding back half" in notes[0] and "2 points" in notes[0] and "detectable effect" in notes[0]
    assert not jargon_in(notes[0]), notes[0]


def test_an_operational_use_case_that_contacts_nobody_gets_neither() -> None:
    config = load_use_case("order-fulfillment")
    assert not config.actions.contacts_customers
    frame = generate(GenerationSpec(use_case_id="order-fulfillment", rows=20_000, variant="clean"))
    advice = advise(context_for(frame, "order-fulfillment"))
    assert _proposal(advice) is None
    assert not any("held back" in text for text in advice.assumptions)


def test_a_persistent_holdout_is_the_administrators_so_the_run_is_not_asked_to_change_it() -> None:
    ctx = context_for(_frame(20_000))
    config = ctx.config.model_copy(
        update={
            "actions": ctx.config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=0.05)}
            )
        }
    )
    advice = advise(dataclasses.replace(ctx, config=config))
    assert _proposal(advice) is None  # `actions.control_group_fraction` is not read under a persistent scope
    notes = [text for text in advice.assumptions if "administrator" in text and "held back" in text]
    assert len(notes) == 1, advice.assumptions
    assert "5%" in notes[0] and not jargon_in(notes[0]), notes[0]


def test_an_explore_share_is_an_assumption_sized_from_the_uplift_floor_never_a_setting() -> None:
    config = load_use_case(UC)
    floor = config.uplift.min_arm_rows
    advice = advise(context_for(_frame(20_000)))
    assert not any(p.path == "actions.explore_fraction" for p in advice.proposals)
    notes = [text for text in advice.assumptions if "explore" in text.lower()]
    assert len(notes) == 1, advice.assumptions
    assert f"{floor:,}" in notes[0] and "5%" in notes[0]  # 1,000 of 20,000 customers
    assert "administrator" in notes[0] and not jargon_in(notes[0]), notes[0]
    # a file where even the most allowed (10%) falls short of the floor says nothing about explore
    small = advise(context_for(_frame(5_000)))
    assert not any("explore" in text.lower() for text in small.assumptions)


@pytest.mark.parametrize("rows", [1_500, 12_000, 20_000, 60_000])
def test_every_proposal_the_rule_makes_is_legal_and_resolves(rows: int) -> None:
    advice = advise(context_for(_frame(rows)))
    legal = overridable_paths(load_use_case(UC))
    for proposal in advice.proposals:
        if proposal.kind is ProposalKind.SETTING:
            assert proposal.path in legal


def test_the_advice_is_computed_from_counts_alone_so_a_million_rows_cost_nothing() -> None:
    import time

    from engine.agent.recommend import DataFacts, holdout_advice

    facts = DataFacts(
        rows=1_000_000, positive_rate=0.05, time_columns=(), consent_candidates=(), evidence_ids=("e1",)
    )
    started = time.perf_counter()
    advice = holdout_advice(load_use_case(UC), facts)
    assert time.perf_counter() - started < 1.0
    assert advice is not None and advice.enough and advice.recommended_fraction is None


def test_without_a_known_rate_nothing_is_guessed() -> None:
    from engine.agent.recommend import DataFacts, holdout_advice

    for rate in (None, 0.0, 1.0):
        facts = DataFacts(
            rows=20_000, positive_rate=rate, time_columns=(), consent_candidates=(), evidence_ids=()
        )
        assert holdout_advice(load_use_case(UC), facts) is None
