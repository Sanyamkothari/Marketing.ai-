"""The two agent ablations of docs/v4/METHOD.md: `agent_greedy` and `agent_uplift_only`.

The default agent is unchanged (checked in tests/v3/diagnose/test_variants.py: its switches are the plain
build's); here is what each ablation does when it is on.
"""

from __future__ import annotations

from autonomy.contracts import Channel, DecisionKind
from autonomy.decide.bidders import GoalBidder
from autonomy.decide.models import BetaResponseModel
from autonomy.gym import default_brief, default_content
from autonomy.runtime import ablation
from autonomy.uplift import attach_uplift
from autonomy.uplift.score import score_only, set_score_only
from tests.v2.decide.test_policy import bid, ctx, policy, state
from tests.v2.realfix.test_uplift_bidder import TierScorer, _bids, brief, content, reachable


def test_greedy_takes_the_best_net_every_time_where_the_plain_policy_draws() -> None:
    options = (bid(1.0, sd=4.0), bid(0.9, sd=4.0, channel=Channel.WHATSAPP, channel_cost=0.03))
    plain, greedy = policy(*options, floor=0.0), policy(*options, floor=0.0, greedy=True)
    drawn = {d.action.channel for d in (plain.decide(state(), ctx(seed)) for seed in range(60)) if d.action}
    chosen = [greedy.decide(state(), ctx(seed)) for seed in range(60)]
    assert drawn == {Channel.EMAIL, Channel.WHATSAPP}  # the Thompson draw picks both, whatever the means
    assert {d.action.channel for d in chosen if d.action} == {Channel.EMAIL}
    assert all(d.kind is DecisionKind.ACT and d.probability == 1.0 for d in chosen)


def test_greedy_still_rests_when_no_option_beats_nothing() -> None:
    greedy = policy(bid(-0.5, sd=4.0), floor=0.0, greedy=True)
    assert {greedy.decide(state(), ctx(seed)).kind for seed in range(30)} == {DecisionKind.NOTHING}


def test_agent_greedy_is_the_agent_with_no_floor_and_no_draw() -> None:
    built = ablation.build_variant("agent_greedy", [default_brief()], default_content())
    assert built.policy.greedy is True and built.policy.exploration_floor == 0.0
    plain = ablation.build_variant("agent", [default_brief()], default_content())
    assert plain.policy.greedy is False and plain.policy.exploration_floor is None


def test_uplift_only_prices_a_scored_option_from_the_score_alone() -> None:
    model = BetaResponseModel()
    bidder = GoalBidder(brief(), content(), model, own_spread=True)
    scorer = TierScorer(sd=0.05)  # a wide score: the blend would pull towards the response model
    assert attach_uplift(model, scorer)
    st = reachable("xa", "a")
    blended = _bids(bidder, st)
    assert set_score_only(model, True) and score_only(model)
    alone = _bids(bidder, st)
    assert alone["c_quality"][0] == 0.10  # the score's own mean, exactly
    assert blended["c_quality"][0] != alone["c_quality"][0]
    # an option the scorer does not score is still the response model's reading
    assert alone["c_novelty"] == blended["c_novelty"]
    assert set_score_only(model, False) and not score_only(model)
    assert _bids(bidder, st) == blended


def test_uplift_only_follows_the_scorer_for_each_customer() -> None:
    model = BetaResponseModel()
    bidder = GoalBidder(brief(), content(), model, own_spread=True)
    attach_uplift(model, TierScorer(sd=0.05))
    set_score_only(model, True)
    a, b = _bids(bidder, reachable("xa", "a")), _bids(bidder, reachable("xb", "b"))
    assert a["c_quality"][0] == 0.10 and b["c_quality"][0] == 0.0


def test_agent_uplift_only_detaches_the_click_model_on_its_own_bundle_only() -> None:
    built = ablation.build_variant("agent_uplift_only", [default_brief()], default_content())
    other = ablation.build_variant("agent", [default_brief()], default_content())
    assert score_only(built.bundle.response) and not score_only(other.bundle.response)
    assert ablation.switches(built)["click_model"] is False
    assert ablation.switches(other)["click_model"] is True
