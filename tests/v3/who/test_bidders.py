"""The bidders with the who-to-contact switch: urgency from leaving risk, the cost ceiling from 90-day
value, and the trait-pooled leave-alone. Default off changes nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.contracts import Bid, Channel, ChannelStats, CustomerState, DecisionContext
from autonomy.decide.bidders import GoalBidder, leave_alone_retry
from autonomy.decide.models import BetaResponseModel, ValueModel
from autonomy.decide.slow_models import CHURN_RISK, CLV_90D
from autonomy.decide.who import WhoBook, WhoConfig
from autonomy.fakes.samples import sample_brief, sample_content
from tests.v3.who.test_trait_harm import BACKGROUND, population

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True)
OFF = WhoConfig(models=False, risk_urgency=0.0, value_cost_share=None, trait_harm=False)


def state(customer_id: str = "c1", **scores: float) -> CustomerState:
    return CustomerState.model_validate(
        {
            "customer_id": customer_id,
            "as_of": NOW,
            "purchases": 2,
            "revenue": 200.0,
            "margin": 60.0,
            "last_purchase_at": NOW - timedelta(days=90),
            "channels": {Channel.EMAIL: REACHABLE},
            "scores": scores,
        }
    )


def model() -> BetaResponseModel:
    return BetaResponseModel(
        BetaResponseModel.priors_from_rates({"treated/*/email|*|*": 0.22, "control/*": 0.20}, strength=1e6)
    )


def bidder(who: WhoBook | None = None, prune_z: float | None = None) -> GoalBidder:
    return GoalBidder(
        sample_brief(NOW),
        sample_content(NOW),
        model(),
        ValueModel(),
        {Channel.EMAIL: 0.001},
        prune_z=prune_z,
        who=who,
    )


def bids_of(b: GoalBidder, s: CustomerState, at: datetime = NOW) -> list[Bid]:
    return b.bids(s, DecisionContext(now=at, briefs=(sample_brief(at),)))


def dump(bids: list[Bid]) -> list[dict[str, object]]:
    return [b.model_dump() for b in bids]


# --- default off -----------------------------------------------------------------------------------


def test_without_a_book_scores_in_the_state_change_nothing():
    plain = dump(bids_of(bidder(), state()))
    assert plain
    scored = dump(bids_of(bidder(), state(**{CHURN_RISK: 0.9, CLV_90D: 0.0001})))
    assert scored == plain


def test_a_book_with_everything_off_is_the_same_as_no_book():
    plain = dump(bids_of(bidder(prune_z=2.0), state()))
    scored = state(**{CHURN_RISK: 0.9, CLV_90D: 0.0001})
    assert dump(bids_of(bidder(WhoBook(OFF), prune_z=2.0), scored)) == dump(
        bids_of(bidder(prune_z=2.0), scored)
    )
    assert dump(bids_of(bidder(WhoBook(OFF), prune_z=2.0), state())) == plain


def test_a_book_changes_nothing_for_a_customer_without_scores_or_a_group():
    plain = dump(bids_of(bidder(prune_z=2.0), state()))
    assert dump(bids_of(bidder(WhoBook(), prune_z=2.0), state())) == plain


# --- leaving risk is urgency -----------------------------------------------------------------------


def test_leaving_risk_raises_urgency_and_scales_the_whole_net():
    base = {(b.action.channel, b.action.offer_id, b.action.content_id): b for b in bids_of(bidder(), state())}
    book = WhoBook(WhoConfig(risk_urgency=0.5, value_cost_share=None, trait_harm=False))
    risky = bids_of(bidder(book), state(**{CHURN_RISK: 1.0}))
    assert len(risky) == len(base)
    for b in risky:
        ref = base[(b.action.channel, b.action.offer_id, b.action.content_id)]
        assert b.urgency == 1.5 * ref.urgency
        assert abs(b.expected_net - 1.5 * ref.expected_net) < 1e-12
        assert abs(b.uncertainty - 1.5 * ref.uncertainty) < 1e-12
    half = bids_of(bidder(book), state(**{CHURN_RISK: 0.4}))
    assert abs(half[0].urgency - 1.2) < 1e-12
    # no risk score, no change
    assert dump(bids_of(bidder(book), state())) == dump(list(bids_of(bidder(), state())))
    assert book.urgency_factor(state(**{CHURN_RISK: float("nan")})) == 1.0


# --- 90-day value caps what an action may cost ------------------------------------------------------


def test_an_option_costing_more_than_a_share_of_90_day_value_is_not_bid():
    book = WhoBook(WhoConfig(risk_urgency=0.0, value_cost_share=0.25, trait_harm=False))
    plain = bids_of(bidder(), state())
    rich = bids_of(bidder(book), state(**{CLV_90D: 10_000.0}))
    assert dump(rich) == dump(plain)
    spend = {id(b): b.offer_cost + b.channel_cost for b in plain}
    ceiling = 0.25 * 0.05
    poor = bids_of(bidder(book), state(**{CLV_90D: 0.05}))
    assert 0 < len(poor) < len(plain)
    assert all(b.offer_cost + b.channel_cost <= ceiling for b in poor)
    assert any(c > ceiling for c in spend.values())
    assert book.cost_ceiling(state(**{CLV_90D: 8.0})) == 2.0
    assert book.cost_ceiling(state()) is None
    assert WhoBook(WhoConfig(value_cost_share=None)).cost_ceiling(state(**{CLV_90D: 8.0})) is None


# --- sleeping dogs by traits ------------------------------------------------------------------------


def book_with_dogs() -> WhoBook:
    book = WhoBook(WhoConfig(models=False, risk_urgency=0.0, value_cost_share=None))
    records, keys = population({**BACKGROUND, "dogs|heavy": (60, 0.4), "neutral|heavy": (60, 1.0)})
    book.traits.fit(records, keys)
    book._keys.update({"dog": "dogs|heavy", "fine": "neutral|heavy"})
    return book


def test_a_customer_in_a_group_that_is_credibly_put_off_is_left_alone_except_in_a_retry_week():
    b = bidder(book_with_dogs(), prune_z=2.0)
    assert bids_of(bidder(prune_z=2.0), state("dog"))  # nothing else holds them back
    days = [NOW + timedelta(days=7 * w) for w in range(150)]
    got = {t: bids_of(b, state("dog"), t) for t in days}
    retry = [t for t in days if leave_alone_retry("dog", t)]
    assert retry and len(retry) < 0.1 * len(days)
    assert all(got[t] for t in retry)
    assert all(not got[t] for t in days if t not in retry)


def test_a_customer_in_a_neutral_group_or_no_group_is_bid_for():
    b = bidder(book_with_dogs(), prune_z=2.0)
    assert bids_of(b, state("fine"))
    assert bids_of(b, state("nobody"))
    # not pruned (explaining a decision): nothing is left out for the dogs either
    assert b.quote(state("dog"), DecisionContext(now=NOW, briefs=(sample_brief(NOW),)), prune=False)


def test_turning_trait_harm_off_leaves_the_dogs_in():
    book = book_with_dogs()
    book.config = WhoConfig(models=False, risk_urgency=0.0, value_cost_share=None, trait_harm=False)
    assert bids_of(bidder(book, prune_z=2.0), state("dog"))
