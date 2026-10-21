"""GoalBidder reads an uplift score per customer and action, blended with the response model."""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from autonomy.contracts import CustomerState, DecisionContext, DecisionKind
from autonomy.decide.bidders import GoalBidder
from autonomy.decide.models import BetaResponseModel
from autonomy.uplift import UpliftScore, attach_uplift, blend, detach_uplift, uplift_of, warm_start_uplift
from tests.v2.realfix.test_uplift_support import NOW, brief, campaign, content, reachable

SCORED = "email|none|quality"


class TierScorer:
    """A stand-in uplift model: tier a gains 0.10, tier b nothing; scores one key only."""

    def __init__(self, sd: float = 0.002) -> None:
        self.sd = sd
        self.calls = 0

    def score(self, state: CustomerState, action_key: str, now: datetime) -> UpliftScore | None:
        self.calls += 1
        if action_key != SCORED:
            return None
        return UpliftScore(mean=0.10 if state.preferences.get("tier") == "a" else 0.0, sd=self.sd, n=1000)


class NoScores:
    def score(self, state: CustomerState, action_key: str, now: datetime) -> UpliftScore | None:
        return None


def _ctx() -> DecisionContext:
    return DecisionContext(now=NOW, briefs=(brief(),), content=tuple(content()))


def _bids(bidder: GoalBidder, state: CustomerState) -> dict[str, tuple[float, float, float]]:
    out = {}
    for b in bidder.quote(state, _ctx(), prune=False):
        out[b.action.content_id or ""] = (b.uplift, b.expected_net, b.uncertainty)
    return out


# --- the blend ------------------------------------------------------------------------------------


def test_blend_agreeing_readings_both_count() -> None:
    mean, sd = blend(0.05, 0.01, UpliftScore(0.05, 0.01))
    assert mean == pytest.approx(0.05)
    assert sd == pytest.approx(0.01 / math.sqrt(2))


def test_blend_far_off_model_loses_its_weight() -> None:
    """A click model ten times too keen is corrected by the campaign, not averaged with it."""
    score = UpliftScore(0.006, 0.001)
    mean, sd = blend(0.09, 0.005, score)
    assert abs(mean - score.mean) <= score.sd
    assert sd <= score.sd


@pytest.mark.parametrize("gap", [0.0, 0.0005, 0.001, 0.003, 0.05, 1.0])
def test_blend_never_strays_more_than_one_score_sd(gap: float) -> None:
    score = UpliftScore(0.02, 0.001)
    mean, _ = blend(0.02 + gap, 0.0, score)
    assert abs(mean - score.mean) <= score.sd + 1e-12


def test_blend_handles_degenerate_spreads() -> None:
    assert blend(0.03, 0.0, UpliftScore(0.01, 0.0)) == (0.01, 0.0)
    assert blend(float("nan"), 0.1, UpliftScore(0.01, 0.002)) == (0.01, 0.002)


# --- the bidder -----------------------------------------------------------------------------------


def test_without_scores_bids_are_unchanged() -> None:
    model = BetaResponseModel()
    plain = GoalBidder(brief(), content(), model, own_spread=True)
    with_none = GoalBidder(brief(), content(), model, own_spread=True, uplift=NoScores())
    for tier in "ab":
        st = reachable(f"x{tier}", tier)
        assert _bids(plain, st) == _bids(with_none, st)


def test_score_moves_the_scored_option_by_customer() -> None:
    model = BetaResponseModel()
    bidder = GoalBidder(brief(), content(), model, own_spread=True, uplift=TierScorer())
    plain = GoalBidder(brief(), content(), model, own_spread=True)
    a, b = _bids(bidder, reachable("xa", "a")), _bids(bidder, reachable("xb", "b"))
    assert a["c_quality"][0] > b["c_quality"][0] + 0.05
    assert a["c_quality"][1] > b["c_quality"][1]
    # The option the scorer does not score is the response model's alone.
    assert a["c_novelty"] == _bids(plain, reachable("xa", "a"))["c_novelty"]
    assert b["c_novelty"] == _bids(plain, reachable("xb", "b"))["c_novelty"]


def test_score_travels_with_the_response_model() -> None:
    model = BetaResponseModel()
    bidder = GoalBidder(brief(), content(), model, own_spread=True)
    before = _bids(bidder, reachable("xa", "a"))
    scorer = TierScorer()
    assert attach_uplift(model, scorer)
    assert uplift_of(model) is scorer
    after = _bids(bidder, reachable("xa", "a"))
    assert after["c_quality"][0] > before["c_quality"][0]
    detach_uplift(model)
    assert uplift_of(model) is None
    assert _bids(bidder, reachable("xa", "a")) == before


def test_sampling_spread_is_the_options_own() -> None:
    model = BetaResponseModel()

    class Shared(TierScorer):
        def score(self, state: CustomerState, action_key: str, now: datetime) -> UpliftScore | None:
            s = super().score(state, action_key, now)
            return None if s is None else UpliftScore(s.mean, 0.02, 1000, own_sd=0.005)

    own = GoalBidder(brief(), content(), model, own_spread=True, uplift=Shared())
    full = GoalBidder(brief(), content(), model, own_spread=False, uplift=Shared())
    st = reachable("xa", "a")
    assert _bids(own, st)["c_quality"][2] < _bids(full, st)["c_quality"][2]
    assert _bids(own, st)["c_quality"][1] == pytest.approx(_bids(full, st)["c_quality"][1])


# --- the agent, end to end ------------------------------------------------------------------------


def test_agent_targets_the_customers_the_campaign_says_it_moves() -> None:
    """The production agent, warm-started on a past campaign where only tier a responded: its own
    warm start (`AgentPolicy._warm_start`) fits the uplift model from the history it was handed (the
    purchases come back from the compact digest) and attaches it, and at $1 an e-mail and about $20
    a conversion it e-mails tier a (15 points of lift), not tier b (none)."""
    from autonomy.contracts import Channel
    from autonomy.runtime.agent import build_agent_policy

    events, states = campaign(n=3000, lift_a=0.15, lift_b=0.0, base=0.05)
    # Only the message the campaign sent: one it never sent has no score, and the agent explores it
    # on the response model alone (as it should), which is not what this test is about.
    library = [c for c in content() if c.content_id == "c_quality"]
    policy = build_agent_policy([brief()], library, costs={Channel.EMAIL: 1.0})
    policy.nightly([], events, NOW)
    ctx = DecisionContext(now=NOW, briefs=(brief(),), content=tuple(library), seed=1)
    policy.prepare_day(list(states.values()), ctx)
    result = policy.uplift
    assert result is not None and result.attached, result
    assert uplift_of(policy.bundle.response) is result.model
    # The same fit as handing the raw events to warm_start_uplift directly: the digest loses nothing
    # the uplift model reads.
    direct = warm_start_uplift(BetaResponseModel(), states, events, policy.content_by_id, NOW)
    assert direct.model is not None and result.model is not None
    assert direct.model.report.arms[0].ate == pytest.approx(result.model.report.arms[0].ate)
    assert result.model.report.arms[0].slope_used > 0

    acted = {"a": 0, "b": 0}
    nets: dict[str, list[float]] = {"a": [], "b": []}
    for i in range(60):
        for tier in "ab":
            d = policy.decide(reachable(f"new_{tier}_{i}", tier), ctx)
            if d.kind is DecisionKind.ACT:
                acted[tier] += 1
                nets[tier].append(d.expected_net)
    assert acted["a"] >= 50 and acted["b"] <= 10, acted
    if nets["a"] and nets["b"]:
        assert sum(nets["a"]) / len(nets["a"]) > sum(nets["b"]) / len(nets["b"])
