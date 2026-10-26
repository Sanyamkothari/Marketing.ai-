"""Each offer kind's honest cost, the bundle builder and the catalogue hook."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autonomy.contracts import NO_OFFER, Action, Channel, CustomerState, OfferKind, OfferOption
from autonomy.decide.planner import basket_offer_cost
from autonomy.mandate.catalogue import (
    ActionCatalogue,
    CatalogueContent,
    bundle_offer,
    catalogue_costs,
    catalogue_offer_cost,
    offer_cost,
    priced_offer,
)

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)


def opt(kind: OfferKind, **kw: object) -> OfferOption:
    return OfferOption.model_validate({"offer_id": kind.value, "label": kind.value, "kind": kind, **kw})


def test_no_offer_and_content_cost_nothing():
    assert offer_cost(NO_OFFER) == 0.0
    assert offer_cost(opt(OfferKind.CONTENT)) == 0.0


def test_percent_off_is_its_share_of_the_basket():
    twenty = opt(OfferKind.PERCENT_OFF, amount=20)
    assert offer_cost(twenty, 900.0) == pytest.approx(180.0)
    assert offer_cost(twenty) == 0.0  # basket unknown, nothing configured


def test_amount_off_is_the_amount():
    assert offer_cost(opt(OfferKind.AMOUNT_OFF, amount=75)) == 75.0


def test_free_shipping_is_the_shipping_cost_per_order():
    assert offer_cost(opt(OfferKind.FREE_SHIPPING, shipping_cost=40)) == 40.0


def test_bundle_is_its_discount():
    assert offer_cost(opt(OfferKind.BUNDLE, bundle_discount=55.5, bundle_item_ids=("a", "b"))) == 55.5


def test_loyalty_is_points_times_value_times_redemption_rate():
    pts = opt(OfferKind.LOYALTY, points=500, point_value=0.2, redemption_rate=0.4)
    assert offer_cost(pts) == pytest.approx(40.0)
    never = opt(OfferKind.LOYALTY, points=500, point_value=0.2, redemption_rate=0.0)
    assert offer_cost(never) == 0.0


def test_gift_and_early_access_cost_what_is_configured():
    assert offer_cost(opt(OfferKind.GIFT, cost_per_redemption=25)) == 25.0
    assert offer_cost(opt(OfferKind.EARLY_ACCESS, cost_per_redemption=3)) == 3.0
    assert offer_cost(opt(OfferKind.GIFT)) == 0.0


def test_an_explicit_cost_always_wins():
    shipping = opt(OfferKind.FREE_SHIPPING, shipping_cost=40, cost_per_redemption=4.0)
    assert offer_cost(shipping) == 4.0
    assert offer_cost(opt(OfferKind.PERCENT_OFF, amount=10, cost_per_redemption=6.0), 1000.0) == 6.0


def test_priced_offer_states_the_cost_and_leaves_a_priced_one_alone():
    fs = opt(OfferKind.FREE_SHIPPING, shipping_cost=40)
    assert priced_offer(fs).cost_per_redemption == 40.0
    already = opt(OfferKind.GIFT, cost_per_redemption=9.0)
    assert priced_offer(already) is already


def test_new_fields_are_additive_and_validated():
    plain = OfferOption(offer_id="x", label="x", kind=OfferKind.PERCENT_OFF, amount=10)
    assert plain.shipping_cost == 0 and plain.bundle_item_ids == () and plain.redemption_rate == 1.0
    with pytest.raises(ValueError):
        opt(OfferKind.LOYALTY, redemption_rate=1.5)
    with pytest.raises(ValueError):
        opt(OfferKind.FREE_SHIPPING, shipping_cost=-1)


def test_basket_offer_cost_uses_the_customers_usual_basket():
    ten = opt(OfferKind.PERCENT_OFF, amount=10)

    def customer(purchases: int, revenue: float) -> CustomerState:
        return CustomerState.model_validate(
            {"customer_id": "c", "as_of": NOW, "purchases": purchases, "revenue": revenue}
        )

    assert basket_offer_cost(ten, customer(4, 800.0)) == pytest.approx(20.0)  # 10% of 200
    assert basket_offer_cost(ten, customer(0, 0.0)) == 0.0
    assert basket_offer_cost(opt(OfferKind.AMOUNT_OFF, amount=5), customer(0, 0.0)) == 5.0


# --- bundle from the co-purchase recommender ------------------------------------------------------

PRICES = {"kettle": 1000.0, "mug": 200.0, "tea": 300.0, "free": 0.0}
RELATED = {"kettle": [("tea", 0.9), ("unpriced", 0.8), ("free", 0.7), ("mug", 0.5)]}


def related(item_id: str, k: int) -> list[tuple[str, float]]:
    return RELATED.get(item_id, [])[:k]


def test_bundle_holds_the_anchor_and_its_best_priced_partners():
    b = bundle_offer("b1", "Tea set", "kettle", related=related, prices=PRICES, percent_off=10, size=3)
    assert b is not None
    assert b.kind is OfferKind.BUNDLE
    assert b.bundle_item_ids == ("kettle", "tea", "mug")  # unpriced and zero-price items are skipped
    assert b.bundle_discount == pytest.approx(150.0)  # 10% of 1000 + 300 + 200
    assert offer_cost(b) == pytest.approx(150.0)


def test_bundle_needs_two_priced_items():
    assert bundle_offer("b", "x", "mug", related=related, prices=PRICES, percent_off=10) is None
    assert bundle_offer("b", "x", "ghost", related=related, prices=PRICES, percent_off=10) is None
    with pytest.raises(ValueError):
        bundle_offer("b", "x", "kettle", related=related, prices=PRICES, percent_off=0)


def test_bundle_from_a_fitted_copurchase_model():
    from autonomy.catalog.recommend import CoPurchaseConfig, CoPurchaseModel
    from autonomy.contracts import Event, EventType

    events = [
        Event(
            event_id=f"e{c}_{i}",
            customer_id=f"c{c}",
            type=EventType.PURCHASE,
            ts=datetime(2026, 1, 1 + c, 9, i, tzinfo=UTC),
            item_id=item,
            value=10.0,
            source="test",
        )
        for c in range(6)
        for i, item in enumerate(("kettle", "tea"))
    ]
    model = CoPurchaseModel.fit(events, as_of=NOW, config=CoPurchaseConfig(min_pair_count=2))
    b = bundle_offer(
        "b1", "Kettle and tea", "kettle", related=model.related, prices=PRICES, percent_off=20, size=2
    )
    assert b is not None and b.bundle_item_ids == ("kettle", "tea")
    assert b.bundle_discount == pytest.approx(260.0)


# --- the catalogue --------------------------------------------------------------------------------


def test_catalogue_publishes_new_kinds_and_prices_them():
    fs = opt(OfferKind.FREE_SHIPPING, shipping_cost=40)
    cat = ActionCatalogue()
    v = cat.publish(CatalogueContent(offers=(fs,)), created_by="t", now=NOW)
    assert catalogue_offer_cost(v, "free_shipping") == 40.0
    assert catalogue_offer_cost(v, "missing") == 0.0
    assert cat.verify_history() == []


def test_the_mandates_budget_accounting_still_reads_only_the_configured_cost():
    # A derived v3 cost is opt-in (`offer_cost`); `catalogue_costs` is unchanged.
    fs = opt(OfferKind.FREE_SHIPPING, shipping_cost=40)
    v = ActionCatalogue().publish(CatalogueContent(offers=(fs,)), created_by="t", now=NOW)
    action = Action(goal_id="g", channel=Channel.EMAIL, offer_id="free_shipping", send_at=NOW)
    assert catalogue_costs(v, action)[0] == 0.0
