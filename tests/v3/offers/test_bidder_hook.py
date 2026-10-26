"""The bidder's two opt-in hooks: off, the bids are the v2 bids; on, the offer cost and the price show up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import (
    Channel,
    ChannelStats,
    CustomerState,
    DecisionContext,
    OfferKind,
    OfferOption,
)
from autonomy.decide import BetaResponseModel, GoalBidder, ValueModel
from autonomy.decide.pacing import BudgetPrice
from autonomy.decide.planner import basket_offer_cost
from autonomy.fakes.samples import sample_brief, sample_content

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
PRIORS = BetaResponseModel.priors_from_rates(
    {"treated/*/email|*|*": 0.10, "treated/*/email|pct10|*": 0.30, "control/*": 0.05}, strength=1e6
)


def customer() -> CustomerState:
    return CustomerState.model_validate(
        {
            "customer_id": "c1",
            "as_of": NOW,
            "purchases": 2,
            "revenue": 200.0,
            "margin": 60.0,
            "last_purchase_at": NOW - timedelta(days=90),
            "channels": {Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
        }
    )


def ctx() -> DecisionContext:
    return DecisionContext.model_validate({"now": NOW, "briefs": (sample_brief(NOW),)})


def bidder(**kw: object) -> GoalBidder:
    return GoalBidder(
        sample_brief(NOW),
        sample_content(NOW),
        BetaResponseModel(PRIORS),
        ValueModel(),
        {Channel.EMAIL: 0.5},
        **kw,  # type: ignore[arg-type]
    )


def by_offer(b: GoalBidder) -> dict[str, object]:
    return {x.action.offer_id + "/" + str(x.action.content_id): x for x in b.bids(customer(), ctx())}


def dumps(b: GoalBidder) -> list[dict[str, object]]:
    return [x.model_dump(mode="json") for x in b.bids(customer(), ctx())]


def test_off_the_bids_are_identical():
    base = dumps(bidder())
    assert dumps(bidder(offer_cost_fn=None, budget_price=None)) == base
    assert dumps(bidder(budget_price=lambda: 0.0)) == base  # a price of zero changes nothing
    assert dumps(bidder(budget_price=BudgetPrice(1000.0, 10, initial=0.0001))) == base  # unbound: reads 0
    assert dumps(bidder(offer_cost_fn=lambda o, s: o.cost_per_redemption)) == base


class _Harm:
    """A stand-in for ContactHarm: each message costs this customer a little of their organic chance."""

    def expected_loss(self, customer_id: str, organic: float) -> float:
        return 0.002 if customer_id.endswith("h") else 0.0


def _state(customer_id: str, revenue: float, scores: dict[str, float]) -> CustomerState:
    return customer().model_copy(
        update={"customer_id": customer_id, "revenue": revenue, "margin": 0.3 * revenue, "scores": scores}
    )


def test_off_the_bids_are_identical_across_customers_with_refill_boost_and_harm():
    refill = {"refill_window_open": 1.0, "refill_p14": 0.4}
    states = [
        _state("a", 200.0, {}),
        _state("bh", 900.0, {}),  # harmed by contact
        _state("c", 80.0, refill),  # refill window open: the boost path
        _state("dh", 400.0, refill),  # both
    ]

    def build(**kw: object) -> GoalBidder:
        return GoalBidder(
            sample_brief(NOW),
            sample_content(NOW),
            BetaResponseModel(PRIORS),
            ValueModel(),
            {Channel.EMAIL: 0.5},
            refill=True,
            item_lookup=lambda s: "sku_1",
            harm=_Harm(),  # type: ignore[arg-type]
            **kw,  # type: ignore[arg-type]
        )

    plain, hooked = build(), build(offer_cost_fn=None, budget_price=lambda: 0.0)
    for state in states:
        assert [b.model_dump() for b in hooked.bids(state, ctx())] == [
            b.model_dump() for b in plain.bids(state, ctx())
        ]
    # The cases are the ones meant: one customer's offer is boosted, one is harmed.
    boosted = {b.action.offer_id: b for b in plain.bids(states[2], ctx())}
    flat = {b.action.offer_id: b for b in plain.bids(_state("c", 80.0, {}), ctx())}
    assert boosted["pct10"].uplift > flat["pct10"].uplift


def test_off_with_pruning_on_the_bids_are_identical_too():
    plain = GoalBidder(sample_brief(NOW), sample_content(NOW), BetaResponseModel(PRIORS), prune_z=1.0)
    hooked = GoalBidder(
        sample_brief(NOW),
        sample_content(NOW),
        BetaResponseModel(PRIORS),
        prune_z=1.0,
        offer_cost_fn=None,
        budget_price=lambda: 0.0,
    )
    assert [b.model_dump() for b in hooked.bids(customer(), ctx())] == [
        b.model_dump() for b in plain.bids(customer(), ctx())
    ]


def test_the_budget_price_is_charged_on_top_of_the_offer_cost_and_nothing_else():
    base = by_offer(bidder())
    priced = by_offer(bidder(budget_price=lambda: 2.0))
    off, off_p = base["pct10/ci_1"], priced["pct10/ci_1"]
    assert off_p.offer_cost == pytest.approx(off.offer_cost)  # type: ignore[attr-defined]
    assert off.expected_net - off_p.expected_net == pytest.approx(2.0 * off.offer_cost)  # type: ignore[attr-defined]
    plain, plain_p = base["none/ci_0"], priced["none/ci_0"]
    assert plain_p.expected_net == plain.expected_net  # type: ignore[attr-defined]


def test_a_high_enough_price_drops_the_offer_a_low_one_keeps_it():
    def kept(price: float) -> bool:
        b = GoalBidder(
            sample_brief(NOW),
            sample_content(NOW),
            BetaResponseModel(PRIORS),
            ValueModel(),
            {Channel.EMAIL: 0.5},
            prune_z=0.0,
            budget_price=lambda: price,
        )
        return any(x.action.offer_id == "pct10" for x in b.bids(customer(), ctx()))

    assert kept(0.0)
    assert kept(1.0)
    assert not kept(50.0)  # the offer's extra profit per unit of cost is far below 50


def test_a_per_customer_cost_function_sets_the_offer_cost():
    seen: list[str] = []

    def cost(offer: OfferOption, state: CustomerState) -> float:
        seen.append(state.customer_id)
        return 12.0 if offer.offer_id == "pct10" else 0.0

    b = by_offer(bidder(offer_cost_fn=cost))
    assert b["pct10/ci_1"].offer_cost == pytest.approx(12.0 * 0.30, abs=1e-3)  # type: ignore[attr-defined]
    assert set(seen) == {"c1"}


def test_new_offer_kinds_are_priced_by_their_own_model():
    # Free shipping with a configured shipping cost and no cost_per_redemption: the offer states the
    # shipping cost as its cost, so the v2 bidder charges it too, and the v3 cost function agrees.
    free_ship = OfferOption(offer_id="pct10", label="x", kind=OfferKind.FREE_SHIPPING, shipping_cost=20.0)
    brief = sample_brief(NOW)
    env = brief.envelope.model_copy(update={"offers": (brief.envelope.offers[0], free_ship)})
    brief = brief.model_copy(update={"envelope": env})
    v2 = GoalBidder(brief, sample_content(NOW), BetaResponseModel(PRIORS)).bids(customer(), ctx())
    v3 = GoalBidder(
        brief, sample_content(NOW), BetaResponseModel(PRIORS), offer_cost_fn=basket_offer_cost
    ).bids(customer(), ctx())
    for bids in (v2, v3):
        cost = [b.offer_cost for b in bids if b.action.offer_id == "pct10"]
        assert cost and cost[0] == pytest.approx(20.0 * 0.30, abs=1e-3)


def test_an_offer_the_cost_function_cannot_price_is_not_bid():
    b = by_offer(bidder(offer_cost_fn=lambda o, s: None if o.offer_id == "pct10" else 0.0))
    assert not any(k.startswith("pct10/") for k in b)
    assert any(k.startswith("none/") for k in b)
