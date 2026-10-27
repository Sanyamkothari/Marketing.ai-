"""A typed Shopify discount code is traced to the agent's offer, through the real mapping path.

Shopify reports `COMEBACK10`; the agent sent `pct10`. These tests build the purchase with
`map_orders` (never by writing the agent's id onto it, as the Gym does) and check that the spend
ledger, the agent's offer-spend tracking and the proof's rewards charge the right offer, and that a
code no offer names is counted as unattributed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from autonomy.connectors.shopify import map_orders
from autonomy.connectors.shopify.normalize import order_from_graphql
from autonomy.contracts import (
    NO_OFFER,
    Action,
    Brief,
    Channel,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Group,
    Objective,
    OfferKind,
    OfferOption,
)
from autonomy.contracts.offer_codes import OfferCodeTable, redemption_tokens
from autonomy.decide.pacing import OfferPricing
from autonomy.fakes import InMemoryDecisionLog, InMemoryWorkspace
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.proof.rewards import Costs, build_rewards
from autonomy.runtime.agent import build_agent_policy
from autonomy.runtime.service import SpendLedger

from .shopify_support import T0, order_node

GOAL = "goal_winback"
PCT10 = OfferOption(
    offer_id="pct10",
    label="10% off",
    kind=OfferKind.PERCENT_OFF,
    amount=10,
    cost_per_redemption=6.0,
    codes=("COMEBACK10",),
    code_prefixes=("WB10-",),
)
FREESHIP = OfferOption(
    offer_id="freeship",
    label="Free shipping",
    kind=OfferKind.FREE_SHIPPING,
    cost_per_redemption=4.0,
    codes=("SHIPFREE",),
)


def brief(*offers: OfferOption) -> Brief:
    base = sample_brief(T0, GOAL)
    env = base.envelope.model_copy(update={"offers": (NO_OFFER, *offers)})
    return base.model_copy(update={"envelope": env})


def shopify_purchase(oid: int, *codes: str, hours: float = 24.0, total: float = 60.0) -> Event:
    """The purchase event the Shopify connector makes for an order that used `codes`."""
    node = order_node(oid, customer=7, total=total, created=T0 + timedelta(hours=hours), codes=codes)
    mapped = map_orders([order_from_graphql(node)], "acme.myshopify.com").events
    assert len(mapped) == 1 and mapped[0].type is EventType.PURCHASE
    return mapped[0]


def our_send(customer_id: str, offer_id: str, decision_id: str = "d1") -> Event:
    return Event(
        event_id=f"send_{decision_id}",
        customer_id=customer_id,
        type=EventType.SEND,
        ts=T0,
        source="test",
        channel=Channel.EMAIL,
        decision_id=decision_id,
        attrs={"goal_id": GOAL, "offer_id": offer_id},
    )


def ledger_for(*offers: OfferOption) -> SpendLedger:
    workspace = InMemoryWorkspace()
    workspace.put_brief(brief(*offers))
    return SpendLedger(InMemoryDecisionLog(), workspace, channel_costs={Channel.EMAIL: 0.0})


def test_the_connector_reports_the_typed_code_not_our_offer_id() -> None:
    p = shopify_purchase(1, "COMEBACK10")
    assert p.attrs["offer_id"] == "COMEBACK10" and p.attrs["discount_codes"] == "COMEBACK10"
    assert redemption_tokens(p.attrs) == ("COMEBACK10",)
    assert redemption_tokens({"offer_id": "none"}) == ()
    assert redemption_tokens({"offer_id": "A", "discount_codes": "A, B,"}) == ("A", "B")


def test_a_code_table_reads_ids_codes_and_prefixes_in_any_case() -> None:
    table = OfferCodeTable((NO_OFFER, PCT10, FREESHIP))
    assert table.offer_for("pct10") == "pct10"  # the agent's own id, as the Gym reports it
    assert table.offer_for("comeback10") == "pct10"
    assert table.offer_for("WB10-A7K2") == "pct10"  # a per-send unique code
    assert table.offer_for("none") is None and table.offer_for("SUMMER") is None
    assert table.offers_for({"offer_id": "SUMMER", "discount_codes": "SUMMER,SHIPFREE"}) == ("freeship",)
    assert table.unmatched({"offer_id": "SUMMER", "discount_codes": "SUMMER,SHIPFREE"}) == ("SUMMER",)
    longer = OfferOption(offer_id="big", label="big", kind=OfferKind.PERCENT_OFF, code_prefixes=("WB10-VIP",))
    assert OfferCodeTable((PCT10, longer)).offer_for("WB10-VIP1") == "big"  # the longest prefix wins


def test_an_offer_without_codes_dumps_as_it_did_before() -> None:
    plain = OfferOption(offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10)
    assert "codes" not in plain.model_dump() and "code_prefixes" not in plain.model_dump()
    assert OfferOption.model_validate(PCT10.model_dump()) == PCT10
    with pytest.raises(ValueError):
        OfferOption(offer_id="x", label="x", kind=OfferKind.PERCENT_OFF, codes=("  ",))


def test_the_ledger_charges_the_offer_a_typed_code_redeems() -> None:
    ledger = ledger_for(PCT10, FREESHIP)
    ledger.observe([our_send("7", "pct10")])
    ledger.observe([shopify_purchase(1, "COMEBACK10")])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)  # pct10's cost, charged once
    ledger.observe([shopify_purchase(2, "COMEBACK10")])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)  # the decision pays its offer once
    assert ledger.unattributed() == []


def test_the_ledger_matches_a_per_send_unique_code_by_prefix() -> None:
    ledger = ledger_for(PCT10)
    ledger.observe([our_send("7", "pct10"), shopify_purchase(1, "wb10-a7k2")])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)


def test_a_code_of_another_offer_does_not_pay_this_one() -> None:
    ledger = ledger_for(PCT10, FREESHIP)
    ledger.observe([our_send("7", "pct10"), shopify_purchase(1, "SHIPFREE")])
    assert ledger(GOAL, T0 + timedelta(days=3)) == 0.0  # known code, but not the offer that was sent
    assert ledger.unattributed() == []  # and not "unattributed": an offer names it


def test_a_late_send_is_still_matched_to_the_code() -> None:
    ledger = ledger_for(PCT10)
    ledger.observe([shopify_purchase(1, "COMEBACK10")])
    assert ledger(GOAL, T0 + timedelta(days=3)) == 0.0
    ledger.observe([our_send("7", "pct10")])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)


def test_an_unknown_code_is_counted_as_unattributed_never_dropped() -> None:
    ledger = ledger_for(PCT10)
    ledger.observe([our_send("7", "pct10")])
    ledger.observe([shopify_purchase(1, "FRIEND20"), shopify_purchase(2, "friend20", hours=30.0)])
    ledger.observe([shopify_purchase(3, "COMEBACK10", hours=40.0)])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)  # only the known code is charged
    [row] = ledger.unattributed()
    assert (row.code, row.orders) == ("FRIEND20", 2)
    ledger.reset()
    assert ledger.unattributed() == []


def test_the_ledger_still_reads_the_agents_own_offer_id() -> None:
    """The Gym writes our id onto the purchase; that keeps working with or without a code table."""
    ledger = ledger_for(PCT10)
    own = Event(
        event_id="p_own",
        customer_id="7",
        type=EventType.PURCHASE,
        ts=T0 + timedelta(hours=5),
        source="test",
        value=60.0,
        attrs={"offer_id": "pct10"},
    )
    ledger.observe([our_send("7", "pct10"), own])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)


def test_the_agent_charges_the_goals_budget_for_a_typed_code() -> None:
    b = brief(PCT10, FREESHIP)
    policy = build_agent_policy(
        [b], sample_content(T0), offer_pricing=OfferPricing(days=10, initial=0.5, default_basket=50.0)
    )
    sent = Decision(
        decision_id="d1",
        customer_id="7",
        made_at=T0,
        kind=DecisionKind.ACT,
        action=Action(goal_id=GOAL, channel=Channel.EMAIL, offer_id="pct10", send_at=T0),
        probability=1.0,
        reason="test",
        policy_version="t",
        group=Group.AGENT,
    )
    price = policy.offer_prices[GOAL]
    policy._decided = True  # as after the first decision; history before it is not spend
    policy.nightly(
        [sent],
        {"7": [shopify_purchase(1, "COMEBACK10"), shopify_purchase(2, "NOPE50", hours=30.0)]},
        T0 + timedelta(hours=40),
    )
    assert price.spent == pytest.approx(6.0)  # pct10's cost, not 0
    [row] = policy.unattributed_discounts()
    assert (row.code, row.orders) == ("NOPE50", 1)


def test_the_proof_charges_the_offer_a_typed_code_redeems() -> None:
    b = brief(PCT10)
    sent = Decision(
        decision_id="d1",
        customer_id="7",
        made_at=T0,
        kind=DecisionKind.ACT,
        action=Action(goal_id=GOAL, channel=Channel.EMAIL, offer_id="pct10", send_at=T0),
        probability=1.0,
        reason="test",
        policy_version="t",
        group=Group.AGENT,
    )
    costs = Costs.from_brief(b)
    [with_code] = build_rewards(
        [sent],
        {"7": [shopify_purchase(1, "COMEBACK10")]},
        14,
        Objective.GROSS_PROFIT,
        costs,
        now=T0 + timedelta(days=20),
    )
    assert with_code.converted and with_code.cost == pytest.approx(6.0)
    [other_code] = build_rewards(
        [sent],
        {"7": [shopify_purchase(2, "NOPE50")]},
        14,
        Objective.GROSS_PROFIT,
        costs,
        now=T0 + timedelta(days=20),
    )
    assert other_code.converted and other_code.cost == 0.0
