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
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Envelope,
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
from autonomy.runtime.agent import AgentPolicy, build_agent_policy
from autonomy.runtime.service import SpendLedger

from .shopify_support import T0, money, order_node

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


def brief(*offers: OfferOption, goal_id: str = GOAL, version: int = 1) -> Brief:
    base = sample_brief(T0, goal_id)
    env = base.envelope.model_copy(update={"offers": (NO_OFFER, *offers)})
    return base.model_copy(update={"envelope": env, "version": version})


def shopify_purchase(
    oid: int, *codes: str, hours: float = 24.0, total: float = 60.0, discount: float = 0.0
) -> Event:
    """The purchase event the Shopify connector makes for an order that used `codes` and took
    `discount` off."""
    node = order_node(
        oid,
        customer=7,
        total=total,
        created=T0 + timedelta(hours=hours),
        codes=codes,
        totalDiscountsSet=money(discount),
    )
    mapped = map_orders([order_from_graphql(node)], "acme.myshopify.com").events
    assert len(mapped) == 1 and mapped[0].type is EventType.PURCHASE
    return mapped[0]


def our_send(customer_id: str, offer_id: str, decision_id: str = "d1", goal_id: str = GOAL) -> Event:
    return Event(
        event_id=f"send_{decision_id}",
        customer_id=customer_id,
        type=EventType.SEND,
        ts=T0,
        source="test",
        channel=Channel.EMAIL,
        decision_id=decision_id,
        attrs={"goal_id": goal_id, "offer_id": offer_id},
    )


def ledger_for(*briefs_or_offers: OfferOption | Brief) -> SpendLedger:
    """A ledger over one brief of these offers, or over the briefs given."""
    workspace = InMemoryWorkspace()
    given = [b for b in briefs_or_offers if isinstance(b, Brief)]
    for b in given or [brief(*(o for o in briefs_or_offers if isinstance(o, OfferOption)))]:
        workspace.put_brief(b)
    return SpendLedger(InMemoryDecisionLog(), workspace, channel_costs={Channel.EMAIL: 0.0})


def agent_send(offer_id: str, decision_id: str = "d1", goal_id: str = GOAL) -> Decision:
    return Decision(
        decision_id=decision_id,
        customer_id="7",
        made_at=T0,
        kind=DecisionKind.ACT,
        action=Action(goal_id=goal_id, channel=Channel.EMAIL, offer_id=offer_id, send_at=T0),
        probability=1.0,
        reason="test",
        policy_version="t",
        group=Group.AGENT,
    )


def started_agent(*briefs: Brief) -> AgentPolicy:
    """The plain agent with offer pricing on, after its first decision (history before it is not spend)."""
    policy = build_agent_policy(
        list(briefs),
        sample_content(T0),
        offer_pricing=OfferPricing(days=10, initial=0.5, default_basket=50.0),
    )
    ctx = DecisionContext(now=T0, briefs=tuple(briefs), content=tuple(sample_content(T0)))
    policy.decide(CustomerState(customer_id="someone_else", as_of=T0), ctx)
    return policy


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
    policy = started_agent(brief(PCT10, FREESHIP))
    price = policy.offer_prices[GOAL]
    policy.nightly(
        [agent_send("pct10")],
        {"7": [shopify_purchase(1, "COMEBACK10"), shopify_purchase(2, "NOPE50", hours=30.0, discount=5.0)]},
        T0 + timedelta(hours=40),
    )
    assert price.spent == pytest.approx(6.0)  # pct10's cost, not 0
    [row] = policy.unattributed_discounts()
    assert (row.code, row.orders, row.discount) == ("NOPE50", 1, 5.0)


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


# --- review fixes: several codes on one order, per-goal tables, refresh, validation ---------------


def test_the_unattributed_row_carries_the_money_the_shop_took_off() -> None:
    ledger = ledger_for(PCT10)
    ledger.observe(
        [
            shopify_purchase(1, "FRIEND20", discount=12.0),
            shopify_purchase(2, "friend20", hours=30.0, discount=8.5),
        ]
    )
    [row] = ledger.unattributed()
    assert (row.code, row.orders, row.discount) == ("FRIEND20", 2, 20.5)


def test_an_unknown_code_next_to_a_known_one_is_still_listed() -> None:
    """COMEBACK10 pays pct10; FRIEND20 on the same order is listed with its half of the order's discount."""
    ledger = ledger_for(PCT10)
    ledger.observe([our_send("7", "pct10"), shopify_purchase(1, "COMEBACK10", "FRIEND20", discount=12.0)])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)
    [row] = ledger.unattributed()
    assert (row.code, row.orders, row.discount) == ("FRIEND20", 1, 6.0)

    policy = started_agent(brief(PCT10))
    policy.nightly(
        [agent_send("pct10")],
        {"7": [shopify_purchase(1, "COMEBACK10", "FRIEND20", discount=12.0)]},
        T0 + timedelta(hours=40),
    )
    assert policy.offer_prices[GOAL].spent == pytest.approx(6.0)
    assert [(r.code, r.orders, r.discount) for r in policy.unattributed_discounts()] == [("FRIEND20", 1, 6.0)]


def test_every_unknown_code_of_an_order_is_listed_with_an_equal_share() -> None:
    ledger = ledger_for(PCT10)
    ledger.observe([shopify_purchase(1, "FRIEND20", "VIP5", discount=10.0)])
    rows = {r.code: (r.orders, r.discount) for r in ledger.unattributed()}
    assert rows == {"FRIEND20": (1, 5.0), "VIP5": (1, 5.0)}


ONLY_A = OfferOption(
    offer_id="pct10",
    label="10% off",
    kind=OfferKind.PERCENT_OFF,
    cost_per_redemption=6.0,
    codes=("COMEBACK10",),
)
ONLY_B = OfferOption(
    offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, cost_per_redemption=9.0, codes=("VIP10",)
)


def test_a_code_is_read_with_the_table_of_the_goal_that_sent_the_offer() -> None:
    """Two goals both send pct10 but name different codes: a code pays only the goal whose offer names it."""
    a, b = brief(ONLY_A, goal_id="goal_a"), brief(ONLY_B, goal_id="goal_b")
    ledger = ledger_for(a, b)
    ledger.observe([our_send("7", "pct10", "db", goal_id="goal_b"), shopify_purchase(1, "COMEBACK10")])
    later = T0 + timedelta(days=3)
    assert ledger("goal_a", later) == 0.0 and ledger("goal_b", later) == 0.0  # goal_b's pct10 is VIP10
    assert ledger.unattributed() == []  # goal_a's offer names it
    ledger.observe([shopify_purchase(2, "VIP10", hours=30.0)])
    assert ledger("goal_b", later) == pytest.approx(9.0)

    policy = started_agent(a, b)
    policy.nightly(
        [agent_send("pct10", "da", goal_id="goal_a"), agent_send("pct10", "db", goal_id="goal_b")],
        {"7": [shopify_purchase(1, "COMEBACK10")]},
        T0 + timedelta(hours=40),
    )
    # goal_b sent last, but COMEBACK10 is goal_a's code: goal_a pays.
    assert policy.offer_prices["goal_a"].spent == pytest.approx(6.0)
    assert policy.offer_prices["goal_b"].spent == 0.0


def test_refresh_reads_codes_a_new_brief_version_names() -> None:
    plain = PCT10.model_copy(update={"codes": (), "code_prefixes": ()})
    workspace = InMemoryWorkspace()
    workspace.put_brief(brief(plain))
    ledger = SpendLedger(InMemoryDecisionLog(), workspace, channel_costs={Channel.EMAIL: 0.0})
    ledger.observe([shopify_purchase(1, "COMEBACK10")])
    assert [r.code for r in ledger.unattributed()] == ["COMEBACK10"]
    workspace.put_brief(brief(PCT10, version=2))
    ledger.refresh()
    assert ledger.unattributed() == []
    ledger.observe([our_send("7", "pct10"), shopify_purchase(2, "COMEBACK10", hours=30.0)])
    assert ledger(GOAL, T0 + timedelta(days=3)) == pytest.approx(6.0)


def test_the_agent_forgets_unattributed_orders_past_its_memory() -> None:
    policy = started_agent(brief(PCT10))
    policy.nightly([], {"7": [shopify_purchase(1, "NOPE50")]}, T0 + timedelta(hours=40))
    assert [r.code for r in policy.unattributed_discounts()] == ["NOPE50"]
    policy.nightly([], {}, T0 + timedelta(days=policy.learn_horizon_days + 60))
    assert policy.unattributed_discounts() == []


def test_codes_that_could_be_read_two_ways_are_refused() -> None:
    for bad in ("A,B", "none", "NONE"):
        with pytest.raises(ValueError):
            OfferOption(offer_id="x", label="x", kind=OfferKind.PERCENT_OFF, codes=(bad,))
    with pytest.raises(ValueError, match="comma"):
        OfferOption(offer_id="x", label="x", kind=OfferKind.PERCENT_OFF, code_prefixes=("WB,",))

    def envelope(*offers: OfferOption) -> Envelope:
        return Envelope(channels=(Channel.EMAIL,), offers=(NO_OFFER, *offers), budget=100.0)

    def offer(offer_id: str, codes: tuple[str, ...] = (), prefixes: tuple[str, ...] = ()) -> OfferOption:
        return OfferOption(
            offer_id=offer_id, label=offer_id, kind=OfferKind.PERCENT_OFF, codes=codes, code_prefixes=prefixes
        )

    with pytest.raises(ValueError, match="named by offers"):
        envelope(offer("a", ("X",)), offer("b", ("x",)))  # one code, any case, on two offers
    with pytest.raises(ValueError, match="named by offers"):
        envelope(offer("a", prefixes=("WB-",)), offer("b", prefixes=("wb-",)))
    with pytest.raises(ValueError, match="is the id of offer"):
        envelope(offer("pct10"), offer("g", ("PCT10",)))
    assert envelope(PCT10, FREESHIP).offers[1] == PCT10  # distinct codes are fine


# --- the Gym's path is unchanged ------------------------------------------------------------------


def test_a_gym_purchase_charges_the_same_with_or_without_codes() -> None:
    """The Gym writes our own offer id on a purchase and no discount_codes. The ledger, the proof and
    the agent charge it the same whether or not the offers name codes: the real 'numbers unchanged'
    check for this change (the Gym smoke never builds a code table)."""
    plain = PCT10.model_copy(update={"codes": (), "code_prefixes": ()})
    gym = Event(
        event_id="p_gym",
        customer_id="7",
        type=EventType.PURCHASE,
        ts=T0 + timedelta(hours=5),
        source="gym",
        value=60.0,
        attrs={"offer_id": "pct10"},
    )
    charged = []
    for offer in (plain, PCT10):
        b = brief(offer, FREESHIP)
        ledger = ledger_for(b)
        ledger.observe([our_send("7", "pct10"), gym])
        policy = started_agent(b)
        policy.nightly([agent_send("pct10")], {"7": [gym]}, T0 + timedelta(hours=40))
        rewards = [
            build_rewards(
                [agent_send("pct10")],
                {"7": [gym]},
                14,
                Objective.GROSS_PROFIT,
                costs,
                now=T0 + timedelta(days=20),
            )[0].cost
            for costs in (Costs(channel={}, offer={"pct10": 6.0}), Costs.from_brief(b))
        ]
        charged.append(
            (
                ledger(GOAL, T0 + timedelta(days=3)),
                policy.offer_prices[GOAL].spent,
                *rewards,
                ledger.unattributed(),
            )
        )
    assert charged[0] == charged[1] == (6.0, 6.0, 6.0, 6.0, [])


def test_the_runtime_logs_the_unattributed_codes_each_night(caplog: pytest.LogCaptureFixture) -> None:
    """The nightly step reads the ledger's list, so an unknown code reaches the operator's log."""
    from types import SimpleNamespace

    from autonomy.runtime.service import AutonomyRuntime

    ledger = ledger_for(PCT10)
    ledger.observe([shopify_purchase(1, "FRIEND20", discount=12.0)])
    with caplog.at_level("WARNING", logger="autonomy.runtime.service"):
        AutonomyRuntime._warn_unattributed(SimpleNamespace(spend_ledger=ledger))  # type: ignore[arg-type]
    assert "FRIEND20 (1 orders, 12.00 off)" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="autonomy.runtime.service"):
        AutonomyRuntime._warn_unattributed(SimpleNamespace(spend_ledger=ledger_for(PCT10)))  # type: ignore[arg-type]
    assert caplog.text == ""


# --- the production path that sets the codes ------------------------------------------------------


@pytest.mark.slow
def test_onboarding_accept_puts_the_owners_codes_on_the_plans_offer() -> None:
    """`POST /v2/onboarding/accept` with offers on and the shop's code: the running plan's offer names
    the code, so a Shopify redemption of it maps to that offer (and a code with a comma is refused)."""
    import dataclasses

    from fastapi.testclient import TestClient

    from api.v2.app import create_app
    from api.v2.deps import Services, build_demo_services
    from api.v2.onboarding import OnboardingDesk, attach_onboarding
    from autonomy.contracts import GoalStatus
    from tests.v2.zero_touch.test_goals_limits import NOW, shopify_shop

    events, states = shopify_shop()
    base = build_demo_services(NOW, n_customers=20, days=1)
    for goal in base.workspace.goals():  # a fresh shop: nothing is running yet
        base.workspace.put_goal(goal.model_copy(update={"status": GoalStatus.DONE}))
    services: Services = dataclasses.replace(base, events=events, states=states, clock=lambda: NOW)
    app = create_app(services)
    attach_onboarding(app, OnboardingDesk(secret=b"test-secret", persistent_key=True))
    with TestClient(app) as c:
        bad = c.post("/v2/onboarding/accept", json={"offers": True, "offer_codes": ["A,B"], "margin_pct": 40})
        assert bad.status_code == 422
        r = c.post(
            "/v2/onboarding/accept", json={"offers": True, "offer_codes": ["WELCOME10"], "margin_pct": 40}
        )
        assert r.status_code == 200, r.text
    plan = Brief.model_validate(r.json()["brief"])
    [offer] = [o for o in plan.envelope.offers if o.offer_id != NO_OFFER.offer_id]
    assert offer.codes == ("WELCOME10",)
    assert OfferCodeTable(plan.envelope.offers).offer_for("welcome10") == offer.offer_id
