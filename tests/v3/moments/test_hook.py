"""The bidder hook: off means every v2 number is unchanged; on multiplies the urgency by the live moments."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import Bid, Channel, ChannelStats, CustomerState, DecisionContext, GoalKind, ItemViews
from autonomy.decide import GoalBidder
from autonomy.decide import bidders as bidders_module
from autonomy.decide.bidders import Urgency, use_moments
from autonomy.decide.moments import MomentBook, MomentUrgency
from autonomy.fakes.samples import sample_brief, sample_content

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)  # no festival, no payday
BOOK = MomentBook.load()
REACHABLE = ChannelStats(address_known=True, consent=True)


def state(**kw: object) -> CustomerState:
    base: dict[str, object] = {
        "customer_id": "c1",
        "as_of": NOW,
        "country": "IN",
        "timezone": "Asia/Kolkata",
        "purchases": 2,
        "revenue": 200.0,
        "margin": 60.0,
        "last_purchase_at": NOW - timedelta(days=90),
        "channels": {Channel.EMAIL: REACHABLE},
    }
    base.update(kw)
    return CustomerState.model_validate(base)


def ctx() -> DecisionContext:
    return DecisionContext.model_validate({"now": NOW, "briefs": (sample_brief(NOW),)})


def bidder(**kw: object) -> GoalBidder:
    return GoalBidder(sample_brief(NOW), sample_content(NOW), **kw)  # type: ignore[arg-type]


def summary(bids: list[Bid]) -> list[tuple[object, ...]]:
    return [(b.action.model_dump_json(), b.uplift, b.urgency, b.expected_net, b.uncertainty) for b in bids]


@pytest.fixture(autouse=True)
def moments_off() -> Iterator[None]:
    use_moments(None)
    yield
    use_moments(None)


CART = {"last_cart_at": NOW - timedelta(hours=6)}


def test_off_is_the_default_and_leaves_the_urgency_alone() -> None:
    plain = bidder()
    assert plain.moments is None
    assert plain.urgency is None
    assert bidder(kind=GoalKind.CART_RECOVERY).urgency is not None
    assert not isinstance(bidder(kind=GoalKind.CART_RECOVERY).urgency, MomentUrgency)
    assert plain.moment_reasons(state(**CART), NOW) == ()


def test_off_gives_identical_bids_whatever_the_moment_fields_hold() -> None:
    loaded = state(**CART, viewed_items={"shoe": ItemViews(count=3, last_at=NOW)}, last_return_at=NOW)
    bare = state()
    assert summary(bidder().bids(loaded, ctx())) == summary(bidder().bids(bare, ctx()))
    assert all(b.urgency == 1.0 for b in bidder().bids(loaded, ctx()))


def test_on_with_no_live_moment_gives_identical_bids() -> None:
    quiet = state()
    assert summary(bidder(moments=BOOK).bids(quiet, ctx())) == summary(bidder().bids(quiet, ctx()))
    base = Urgency(half_life_days=1.0)
    cart = state(last_seen_at=NOW - timedelta(days=1), country="US", timezone="UTC")
    on = bidder(moments=BOOK, kind=GoalKind.CART_RECOVERY, urgency=base)
    off = bidder(kind=GoalKind.CART_RECOVERY, urgency=base)
    assert summary(on.bids(cart, ctx())) == summary(off.bids(cart, ctx()))


def test_on_multiplies_the_urgency_and_the_net_by_the_factor() -> None:
    cart = state(**CART)
    factor = BOOK.factor(cart, NOW)
    assert factor > 1.0
    off = {b.action.content_id: b for b in bidder().bids(cart, ctx())}
    on = {b.action.content_id: b for b in bidder(moments=BOOK).bids(cart, ctx())}
    assert off.keys() == on.keys()
    for key, bid in on.items():
        assert bid.urgency == pytest.approx(factor)
        assert bid.expected_net == pytest.approx(off[key].expected_net * factor)
        assert bid.uplift == pytest.approx(off[key].uplift)  # the message is not better, only worth more


def test_a_moment_does_not_reorder_one_customers_options() -> None:
    cart = state(**CART)
    on = [b.action.content_id for b in bidder(moments=BOOK).bids(cart, ctx())]
    off = [b.action.content_id for b in bidder().bids(cart, ctx())]
    assert on == off


def test_use_moments_switches_it_on_for_bidders_built_afterwards() -> None:
    before = bidder()
    use_moments(BOOK)
    after = bidder()
    assert before.moments is None
    assert after.moments is BOOK
    explicit = bidder(moments=MomentBook.load())
    assert explicit.moments is not BOOK
    use_moments(None)
    assert bidder().moments is None
    assert bidders_module._default_moments is None


def test_cart_recovery_keeps_its_own_fade_and_skips_the_cart_moment() -> None:
    cart = state(
        country="US",
        timezone="UTC",
        last_cart_at=NOW - timedelta(hours=6),
        last_seen_at=NOW - timedelta(hours=6),
    )
    on = bidder(moments=BOOK, kind=GoalKind.CART_RECOVERY)
    off = bidder(kind=GoalKind.CART_RECOVERY)
    assert summary(on.bids(cart, ctx())) == summary(off.bids(cart, ctx()))
    viewed = state(
        country="US",
        timezone="UTC",
        last_cart_at=NOW - timedelta(hours=6),
        last_seen_at=NOW - timedelta(hours=6),
        viewed_items={"shoe": ItemViews(count=2, last_at=NOW)},
    )
    assert on.bids(viewed, ctx())[0].urgency > off.bids(viewed, ctx())[0].urgency


def test_a_refill_goal_skips_the_refill_moment_it_already_prices() -> None:
    due = state(country="US", timezone="UTC", scores={"refill_window_open": 1.0, "refill_p14": 0.3})
    refill_on = bidder(moments=BOOK, kind=GoalKind.REPEAT_PURCHASE)
    refill_off = bidder(kind=GoalKind.REPEAT_PURCHASE)
    assert summary(refill_on.bids(due, ctx())) == summary(refill_off.bids(due, ctx()))
    # Any other goal does not price the refill, so there it counts.
    assert bidder(moments=BOOK).bids(due, ctx())[0].urgency > 1.0


def test_the_reasons_name_the_live_moments_strongest_first() -> None:
    both = state(**CART, viewed_items={"shoe": ItemViews(count=2, last_at=NOW)})
    reasons = bidder(moments=BOOK).moment_reasons(both, NOW)
    assert reasons[0].startswith("Left a cart")
    assert reasons[1].startswith("Looked at shoe")
    assert bidder(moments=BOOK).moment_reasons(state(), NOW) == ()
