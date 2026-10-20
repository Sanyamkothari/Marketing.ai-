"""Shopify writes: one single-use discount code per decision, whatever happens; idempotent customer tags."""

from __future__ import annotations

from datetime import timedelta

import pytest

from autonomy.connectors import NotSupported, idempotency_key
from autonomy.connectors.shopify import (
    CREATE_DISCOUNT,
    REMOVE_TAGS,
    TAG_CUSTOMER,
    ShopifyConnector,
    discount_code,
)

from .shopify_support import T0, connect_token, make_shopify_rig

KEY = idempotency_key("dec_42", CREATE_DISCOUNT)
OFFER = {"percentage": 0.15, "customer_id": "7", "minimum_subtotal": 30}


def setup():  # type: ignore[no-untyped-def]
    rig, fake = make_shopify_rig()
    connection = connect_token(rig)
    return rig.engine.context(connection, T0), fake, ShopifyConnector()


def test_the_same_decision_never_creates_two_codes() -> None:
    ctx, fake, connector = setup()
    first = connector.write(ctx, CREATE_DISCOUNT, OFFER, idempotency_key=KEY)
    again = connector.write(ctx, CREATE_DISCOUNT, OFFER, idempotency_key=KEY)
    assert first.accepted and again.accepted
    assert first.detail == again.detail == discount_code(KEY) and first.provider_id == again.provider_id
    assert first.detail is not None and first.detail.startswith("AUTO-") and len(first.detail) == 15
    assert fake.operations.count("AutonomyDiscountCreate") == 1 and len(fake.discounts) == 1
    spec = fake.discounts[first.detail]["input"]
    assert spec["usageLimit"] == 1 and spec["appliesOncePerCustomer"] is True
    assert spec["context"] == {"customers": {"add": ["gid://shopify/Customer/7"]}}
    assert spec["customerGets"]["value"] == {"percentage": 0.15}
    assert spec["minimumRequirement"] == {"subtotal": {"greaterThanOrEqualToSubtotal": 30.0}}
    assert (spec["startsAt"], spec["endsAt"]) == ("2026-10-01T12:00:00Z", "2026-10-15T12:00:00Z")
    assert spec["combinesWith"] == {
        "orderDiscounts": False,
        "productDiscounts": False,
        "shippingDiscounts": True,
    }

    other = connector.write(
        ctx, CREATE_DISCOUNT, OFFER, idempotency_key=idempotency_key("dec_43", CREATE_DISCOUNT)
    )
    assert other.detail != first.detail and len(fake.discounts) == 2


def test_a_create_whose_answer_was_lost_is_retried_and_finds_the_code_it_made() -> None:
    ctx, fake, connector = setup()
    fake.lose_answer_to = {"AutonomyDiscountCreate"}
    result = connector.write(ctx, CREATE_DISCOUNT, {"amount": 5, "valid_days": 3}, idempotency_key=KEY)
    assert result.accepted and len(fake.discounts) == 1
    assert fake.operations.count("AutonomyDiscountCreate") == 2  # the retry hit TAKEN, then the lookup
    spec = fake.discounts[discount_code(KEY)]["input"]
    assert spec["context"] == {"all": "ALL"}
    assert spec["customerGets"]["value"] == {"discountAmount": {"amount": 5.0, "appliesOnEachItem": False}}
    assert spec["endsAt"] == "2026-10-04T12:00:00Z"


@pytest.mark.parametrize(
    "payload",
    [
        {"percentage": 0.1, "amount": 5},
        {},
        {"percentage": 1.5},
        {"amount": -1},
        {"amount": 5, "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-04T00:00:00Z"},
    ],
)
def test_a_bad_offer_is_refused_in_plain_words_before_any_call(payload: dict[str, object]) -> None:
    ctx, fake, connector = setup()
    before = len(fake.calls)
    result = connector.write(ctx, CREATE_DISCOUNT, payload, idempotency_key=KEY)
    assert not result.accepted and result.detail
    assert len(fake.calls) == before


def test_tags_are_added_and_removed_idempotently() -> None:
    ctx, fake, connector = setup()
    key = idempotency_key("holdout", "7")
    for _ in range(2):
        result = connector.write(
            ctx, TAG_CUSTOMER, {"customer_id": "7", "tags": ["autonomy-holdout"]}, idempotency_key=key
        )
        assert result.accepted
    assert fake.tags["gid://shopify/Customer/7"] == {"autonomy-holdout"}
    removed = connector.write(
        ctx, REMOVE_TAGS, {"customer_id": "7", "tags": "autonomy-holdout"}, idempotency_key=key
    )
    assert removed.accepted and fake.tags["gid://shopify/Customer/7"] == set()
    assert not connector.write(ctx, TAG_CUSTOMER, {"tags": ["x"]}, idempotency_key=key).accepted
    assert not connector.write(
        ctx, TAG_CUSTOMER, {"customer_id": "7", "tags": ["a,b"]}, idempotency_key=key
    ).accepted
    with pytest.raises(NotSupported):
        connector.write(ctx, "send_email", {}, idempotency_key=key)
    assert T0 + timedelta(0) == ctx.now
