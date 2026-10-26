"""Each offer kind's honest cost, the bundle builder and the catalogue hook."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autonomy.contracts import NO_OFFER, Action, Channel, CustomerState, OfferKind, OfferOption
from autonomy.decide.planner import basket_cost_fn, basket_offer_cost
from autonomy.mandate.catalogue import (
    ActionCatalogue,
    CatalogueContent,
    CatalogueVersion,
    bundle_offer,
    catalogue_costs,
    catalogue_offer_cost,
    compute_hash,
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
    assert basket_offer_cost(opt(OfferKind.AMOUNT_OFF, amount=5), customer(0, 0.0)) == 5.0


def test_an_unknown_basket_is_never_costed_as_free():
    ten = opt(OfferKind.PERCENT_OFF, amount=10)
    new = CustomerState.model_validate({"customer_id": "c", "as_of": NOW, "purchases": 0, "revenue": 0.0})
    # No basket of their own and no shop default: the offer cannot be priced, so it is not bid.
    assert basket_offer_cost(ten, new) is None
    # The shop's typical order stands in for a customer with no orders.
    assert basket_offer_cost(ten, new, 300.0) == pytest.approx(30.0)
    assert basket_cost_fn(300.0)(ten, new) == pytest.approx(30.0)
    # A configured cost needs no basket.
    assert basket_offer_cost(opt(OfferKind.PERCENT_OFF, amount=10, cost_per_redemption=7.0), new) == 7.0


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
    assert b.cost_per_redemption == pytest.approx(150.0)  # what every v2 reader of the cost sees


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


@pytest.mark.parametrize(
    "offer",
    [
        opt(OfferKind.FREE_SHIPPING, shipping_cost=40),
        opt(OfferKind.BUNDLE, bundle_discount=40, bundle_item_ids=("a", "b")),
        opt(OfferKind.LOYALTY, points=400, point_value=0.25, redemption_rate=0.4),
    ],
    ids=lambda o: o.kind.value,
)
def test_the_mandates_budget_accounting_sees_the_derived_cost(offer: OfferOption):
    # An offer priced only through the v3 fields states that cost as `cost_per_redemption`, so the
    # budget ledger, the margin floor and the v2 bidder all charge it.
    assert offer.cost_per_redemption == pytest.approx(40.0) == offer_cost(offer)
    v = ActionCatalogue().publish(CatalogueContent(offers=(offer,)), created_by="t", now=NOW)
    action = Action(goal_id="g", channel=Channel.EMAIL, offer_id=offer.offer_id, send_at=NOW)
    assert catalogue_costs(v, action)[0] == pytest.approx(offer_cost(offer))
    # And it survives a save and a reload unchanged.
    again = OfferOption.model_validate_json(offer.model_dump_json())
    assert again == offer


def test_an_offer_that_does_not_use_the_new_fields_dumps_as_in_v2():
    ten = OfferOption(offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10)
    assert set(ten.model_dump(mode="json")) == {
        "offer_id",
        "label",
        "kind",
        "amount",
        "cost_per_redemption",
        "needs_approval",
    }


def test_a_catalogue_version_saved_by_v2_still_verifies():
    # A version file exactly as v2 wrote it (no v3 offer fields), with the hash v2 computed over it.
    v2_file = {
        "version": 1,
        "created_at": "2026-03-02T10:00:00Z",
        "created_by": "ops",
        "offers": [
            {
                "offer_id": "pct10",
                "label": "10% off",
                "kind": "percent_off",
                "amount": 10.0,
                "cost_per_redemption": 6.0,
                "needs_approval": False,
            }
        ],
        "channels": [],
        "templates": [],
        "diff_from_previous": {
            "from_version": None,
            "to_version": 1,
            "changes": [{"kind": "offer", "key": "pct10", "change": "added", "fields": []}],
        },
        "previous_hash": "0" * 64,
    }
    import hashlib

    from autonomy.mandate.signing import canonical_bytes

    v2_hash = hashlib.sha256(canonical_bytes(v2_file)).hexdigest()
    loaded = CatalogueVersion.model_validate({**v2_file, "content_hash": v2_hash})
    assert compute_hash(loaded) == v2_hash
