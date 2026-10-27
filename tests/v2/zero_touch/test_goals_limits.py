"""Goal proposals sized in money and limits learned from history (docs/v2/ZERO_TOUCH.md items 1 and 2).

`shopify_shop()` builds a Shopify-shaped shop: order and customer records in the shapes
`autonomy.connectors.shopify.normalize` produces, Klaviyo profiles and events in the shapes the
Klaviyo reader returns, all through the real connector mappings, folded into state by the
production `StateFolder`. 18 months of a US outdoor-coffee shop: refillable products (beans,
filters, wax) and one-off gear, discount codes on about a quarter of orders, a weekly newsletter,
customers across US, UK and Australian time zones, and cart events.
"""

from __future__ import annotations

import dataclasses
import math
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.v2.app import create_app
from api.v2.deps import Services, build_demo_services, suggest_opportunities
from api.v2.onboarding import OnboardingDesk, attach_onboarding
from autonomy.connectors.base import map_with
from autonomy.connectors.klaviyo.mapping import KLAVIYO_MAPPING, event_rows, profile_rows
from autonomy.connectors.shopify.mapping import map_customers, map_orders
from autonomy.contracts import AutonomyLevel, Channel, CustomerState, Event, EventType, GoalKind, GoalStatus
from autonomy.data import StateFolder
from autonomy.data.rebuild import rebuild_states
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore
from autonomy.mandate import MandateStatus, SignaturePolicy, SignerRole
from autonomy.mandate.signing import HmacSigner, KeyRing, verify_mandate
from autonomy.onboarding import (
    SINGLE_OWNER_NOTE,
    SINGLE_OWNER_POLICY,
    ShopHistory,
    learn_limits,
    mandate_from_limits,
    scan_opportunities,
    sign_as_owner,
)
from autonomy.onboarding.opportunities import MESSAGES_COSTED
from autonomy.onboarding.priors import MESSAGE_LIFT, MESSAGE_LIFT_BASE, lift_for, rate_over, refill_factor

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
SHOP = "acme-outdoors.myshopify.com"
KLAVIYO_ACCOUNT = "kl_acme"

PRODUCTS: tuple[tuple[str, str, float, float | None], ...] = (
    # product id, type, price, refill cycle in days (None: bought once)
    ("p_beans", "coffee", 18.0, 28.0),
    ("p_filters", "coffee", 9.0, 45.0),
    ("p_wax", "care", 14.0, 75.0),
    ("p_tent", "gear", 240.0, None),
    ("p_jacket", "apparel", 180.0, None),
    ("p_mug", "gear", 22.0, None),
    ("p_stove", "gear", 95.0, None),
    ("p_socks", "apparel", 16.0, None),
)
ZONES: tuple[tuple[str, float], ...] = (
    ("America/New_York", 0.4),
    ("America/Los_Angeles", 0.2),
    ("America/Chicago", 0.15),
    ("Europe/London", 0.15),
    ("Australia/Sydney", 0.1),
)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _zone(rng: random.Random) -> str:
    x, acc = rng.random(), 0.0
    for name, share in ZONES:
        acc += share
        if x <= acc:
            return name
    return ZONES[-1][0]


def _order(
    rng: random.Random, n: int, customer: str, at: datetime, product: tuple[str, str, float, float | None]
) -> dict[str, Any]:
    pid, kind, price, _ = product
    qty = 1 + (rng.random() < 0.3)
    subtotal = round(price * qty, 2)
    code = rng.random() < 0.25
    discount = round(subtotal * rng.choice((0.10, 0.10, 0.15, 0.20)), 2) if code else 0.0
    return {
        "id": f"gid://shopify/Order/{1000 + n}",
        "name": f"#{1000 + n}",
        "customer_id": customer,
        "processed_at": _iso(at),
        "created_at": _iso(at),
        "total": round(subtotal - discount, 2),
        "total_discounts": discount,
        "discount_codes": [rng.choice(("WELCOME10", "SAVE15"))] if code else [],
        "currency": "USD",
        "financial_status": "PAID",
        "test": False,
        "line_items": [
            {"id": f"li_{n}", "product_id": pid, "product_type": kind, "total": subtotal, "quantity": qty}
        ],
        "refunds": [],
    }


def shopify_records(
    n_customers: int = 600, days: int = 540, seed: int = 11, carts: bool = True
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(Shopify orders, Shopify customers, Klaviyo profiles, Klaviyo events) for one shop."""
    rng = random.Random(seed)
    orders: list[dict[str, Any]] = []
    customers: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    kl_events: list[dict[str, Any]] = []
    start = NOW - timedelta(days=days)
    for i in range(n_customers):
        cid = str(7_000_000 + i)
        created = start + timedelta(days=rng.uniform(0, days - 3), hours=rng.uniform(0, 23))
        subscribed = rng.random() < 0.8
        customers.append(
            {
                "id": cid,
                "created_at": _iso(created),
                "state": "ENABLED",
                "email_consent": {
                    "state": "SUBSCRIBED" if subscribed else "NOT_SUBSCRIBED",
                    "updated_at": _iso(created),
                    "opt_in_level": "SINGLE_OPT_IN",
                },
            }
        )
        zone = _zone(rng)
        profiles.append(
            {
                "id": f"01KL{i:06d}",
                "attributes": {
                    "external_id": cid,
                    "created": _iso(created),
                    "updated": _iso(created),
                    "location": {"timezone": zone, "country": "US" if zone.startswith("America") else None},
                    "subscriptions": {
                        "email": {
                            "marketing": {
                                "consent": "SUBSCRIBED" if subscribed else "NEVER_SUBSCRIBED",
                                "can_receive_email_marketing": subscribed,
                                "last_updated": _iso(created),
                            }
                        }
                    },
                },
            }
        )
        # Purchases: a refill buyer keeps to the product's cycle until they stop; others come back now and then.
        first = created + timedelta(hours=rng.uniform(0.5, 72))
        product = rng.choice(PRODUCTS)
        at = first
        while at < NOW:
            orders.append(_order(rng, len(orders), cid, at, product))
            cycle = product[3]
            if cycle is not None:
                if rng.random() < 0.18:
                    break
                at = at + timedelta(days=cycle * math.exp(rng.gauss(0, 0.25)))
            else:
                if rng.random() < 0.55:
                    break
                at = at + timedelta(days=rng.expovariate(1 / 80))
                product = rng.choice(PRODUCTS)
        # The shop's own newsletter: weekly on Tuesdays for 26 weeks, a second mail to some.
        if subscribed:
            for week in range(26):
                sent = NOW - timedelta(weeks=week, days=(NOW.weekday() - 1) % 7, hours=-4)
                if sent <= created or sent >= NOW:
                    continue
                for extra in (0, 1) if rng.random() < 0.35 else (0,):
                    when = sent + timedelta(days=2 * extra)
                    kl_events.append(
                        _kl_event(f"rcv_{i}_{week}_{extra}", "Received Email", when, profiles[-1])
                    )
                    if rng.random() < 0.22:
                        opened = when + timedelta(hours=rng.uniform(1, 30))
                        if opened < NOW:
                            kl_events.append(
                                _kl_event(f"opn_{i}_{week}_{extra}", "Opened Email", opened, profiles[-1])
                            )
        if carts and rng.random() < 0.12:
            moment = NOW - timedelta(days=rng.uniform(0, 60))
            if moment > created:
                kl_events.append(
                    _kl_event(f"cart_{i}", "Added to Cart", moment, profiles[-1], item=product[0])
                )
    # Two rows Shopify reports but we never count: a test order and a guest checkout.
    orders.append(_order(rng, len(orders), "7000000", NOW - timedelta(days=3), PRODUCTS[0]) | {"test": True})
    orders.append(_order(rng, len(orders), "", NOW - timedelta(days=2), PRODUCTS[1]) | {"customer_id": None})
    return orders, customers, profiles, kl_events


def _kl_event(
    eid: str, metric: str, at: datetime, profile: dict[str, Any], item: str | None = None
) -> dict[str, Any]:
    props: dict[str, Any] = {"$flow": None}
    if item is not None:
        props = {"ProductID": item, "Categories": ["gear"]}
    return {
        "id": eid,
        "attributes": {"datetime": _iso(at), "event_properties": props},
        "_metric": metric,
        "_profile": {"id": profile["id"], **profile["attributes"]},
        "_ours": False,
    }


def shopify_shop(**kwargs: Any) -> tuple[InMemoryEventStore, InMemoryStateStore]:
    """The shop's events through the real connector mappings, and states folded from them."""
    orders, customers, profiles, kl_events = shopify_records(**kwargs)
    events: list[Event] = []
    events.extend(map_orders(orders, SHOP).events)
    events.extend(map_customers(customers, SHOP).events)
    rows = [r for p in profiles for r in profile_rows(p)] + event_rows(kl_events)
    events.extend(map_with(KLAVIYO_MAPPING, rows, source="klaviyo", account=KLAVIYO_ACCOUNT).events)
    store = InMemoryEventStore()
    store.append(sorted(events, key=lambda e: (e.ts, e.event_id)))
    states = InMemoryStateStore()
    rebuild_states(store, states, StateFolder(), NOW)
    return store, states


@pytest.fixture(scope="module")
def shop() -> tuple[InMemoryEventStore, InMemoryStateStore]:
    return shopify_shop()


@pytest.fixture(scope="module")
def scan(shop: tuple[InMemoryEventStore, InMemoryStateStore]) -> Any:
    events, states = shop
    return scan_opportunities(events.scan(), states.scan(), NOW)


def _history(shop: tuple[InMemoryEventStore, InMemoryStateStore]) -> ShopHistory:
    events, states = shop
    return ShopHistory.build(events.scan(), states.scan(), NOW)


# --- proposals ------------------------------------------------------------------------------------


def test_the_fixture_is_shopify_shaped(shop: tuple[InMemoryEventStore, InMemoryStateStore]) -> None:
    events, states = shop
    sources = {e.source for e in events.scan()}
    assert sources == {"shopify", "klaviyo"}
    purchases = [e for e in events.scan() if e.type.value == "purchase"]
    assert purchases and all(e.attrs.get("currency") == "USD" for e in purchases)
    assert any(e.attrs.get("offer_id") for e in purchases)
    zoned = [s for s in states.scan() if s.timezone != "UTC"]
    assert len(zoned) > 400  # Klaviyo's time zones landed on the Shopify customers (external_id = Shopify id)


def test_every_opportunity_is_sized_with_a_range_and_ranked(scan: Any) -> None:
    ids = [p.opportunity_id for p in scan.proposals]
    assert set(ids) == {
        "opp_refill_due",
        "opp_first_to_second",
        "opp_win_back",
        "opp_vip_at_risk",
        "opp_cart",
    }
    assert scan.enough_history and scan.currency == "USD"
    sized = [p for p in scan.proposals if p.monthly_profit is not None]
    # VIP at risk (27 distinct people in its look-backs) and cart (12) are below the 30 needed.
    assert len(sized) >= 3
    assert all(
        p.not_enough and "we need 30" in p.not_enough for p in scan.proposals if p.monthly_profit is None
    )
    for p in sized:
        m = p.monthly_profit
        assert m is not None and m.low <= m.point <= m.high and m.low < m.high
        assert p.extra_orders is not None and p.base_rate is not None and p.base_sample >= 30
        assert p.audience_now >= p.reachable_now >= 0 and p.reached_in_month > 0
        assert p.why.endswith(".") and p.why.count(". ") == 0  # one sentence
        assert f"{p.audience_now:,}" in p.why or f"{p.reached_in_month:,}" in p.goal_text
    points = [p.monthly_profit.point for p in sized if p.monthly_profit is not None]
    assert points == sorted(points, reverse=True)
    assert [p.rank for p in scan.proposals] == list(range(1, len(scan.proposals) + 1))


def test_the_best_proposal_is_preselected(scan: Any) -> None:
    chosen = [p for p in scan.proposals if p.preselected]
    assert len(chosen) == 1 and chosen[0].opportunity_id == scan.preselected_id
    assert chosen[0] is scan.proposals[0] or chosen[0] == scan.proposals[0]


def test_every_number_traces_to_the_shop_or_a_cited_prior(scan: Any) -> None:
    for p in scan.proposals:
        if p.monthly_profit is None:
            continue
        sources = " ".join(b.source for b in p.basis)
        assert "your order history" in sources
        assert MESSAGE_LIFT.source in sources
        assert "DEFAULT_CHANNEL_COSTS" in sources
        margin = next(b for b in p.basis if b.label == "Margin")
        assert margin.assumed  # Shopify orders carry no product cost: the 40% default, flagged
        assert "tell us your margin" in margin.source
    assert any("Margin assumed" in n for n in scan.notes)
    assert scan.margin.rate == pytest.approx(0.4) and not scan.margin.from_data


def test_money_is_linear_in_the_margin_it_was_given(
    shop: tuple[InMemoryEventStore, InMemoryStateStore],
) -> None:
    history = _history(shop)
    low = scan_opportunities((), (), NOW, margin_rate=0.25, history=history)
    high = scan_opportunities((), (), NOW, margin_rate=0.5, history=history)
    for a in low.proposals:
        b = next(p for p in high.proposals if p.opportunity_id == a.opportunity_id)
        if a.monthly_profit is None or b.monthly_profit is None or a.message_cost is None:
            continue
        gross_a = a.monthly_profit.point + a.message_cost
        gross_b = b.monthly_profit.point + a.message_cost
        assert gross_b == pytest.approx(2 * gross_a, abs=0.02)
    assert high.margin.source == "your answer" and high.margin.from_data


def test_the_lift_is_the_cited_prior_capped_for_rare_buyers() -> None:
    assert lift_for(0.25, MESSAGE_LIFT.point) == pytest.approx(MESSAGE_LIFT.point)
    rare = MESSAGE_LIFT_BASE / 2
    assert lift_for(rare, MESSAGE_LIFT.point, base_days=14) == pytest.approx(MESSAGE_LIFT.point / 2)
    assert lift_for(0.0, MESSAGE_LIFT.point) == 0.0
    # A 30-day rate is compared with the test's 14-day base over the same window: a group buying at
    # the test's base over 30 days buys at about half of it over 14, so its lift is capped near half.
    capped = lift_for(MESSAGE_LIFT_BASE, MESSAGE_LIFT.point, base_days=30)
    expected = MESSAGE_LIFT.point * rate_over(MESSAGE_LIFT_BASE, 30, 14) / MESSAGE_LIFT_BASE
    assert capped == pytest.approx(expected) and 0.4 * MESSAGE_LIFT.point < capped < 0.5 * MESSAGE_LIFT.point
    assert refill_factor(0.0) == 1.0 and refill_factor(0.1) == pytest.approx(1.5) and refill_factor(1.0) < 2.0


def test_the_extra_orders_follow_from_reach_and_lift(scan: Any) -> None:
    for p in scan.proposals:
        if p.extra_orders is None or p.base_rate is None:
            continue
        boost = 2.0 if p.opportunity_id == "opp_refill_due" else 1.0
        most = p.reached_in_month * MESSAGE_LIFT.high * boost
        assert 0 < p.extra_orders.point <= most + 0.05
        assert p.message_cost == pytest.approx(p.reached_in_month * MESSAGES_COSTED * 0.001, abs=0.01)
        assert p.budget == max(1, math.ceil(p.message_cost or 0))


def test_a_young_shop_gets_counts_and_not_enough_history() -> None:
    events, states = shopify_shop(n_customers=120, days=40, seed=3)
    found = scan_opportunities(events.scan(), states.scan(), NOW)
    assert not found.enough_history and found.not_enough is not None
    assert found.not_enough.startswith("Not enough history yet")
    assert found.preselected_id is None
    for p in found.proposals:
        assert p.monthly_profit is None and p.extra_orders is None and p.budget == 0.0
        assert p.not_enough is not None and p.not_enough.startswith("Not enough history yet")


def test_cart_follow_up_only_when_cart_or_view_events_exist() -> None:
    events, states = shopify_shop(n_customers=200, days=300, seed=5, carts=False)
    found = scan_opportunities(events.scan(), states.scan(), NOW)
    assert "opp_cart" not in {p.opportunity_id for p in found.proposals}
    assert any(s.startswith("Cart and browse") for s in found.skipped)


def test_a_running_goal_kind_is_not_preselected(shop: tuple[InMemoryEventStore, InMemoryStateStore]) -> None:
    history = _history(shop)
    plain = scan_opportunities((), (), NOW, history=history)
    best = next(p for p in plain.proposals if p.preselected)
    again = scan_opportunities((), (), NOW, taken=frozenset({best.kind}), history=history)
    assert all(not p.preselected for p in again.proposals if p.kind is best.kind)
    assert again.preselected_id != best.opportunity_id


def test_the_scan_is_deterministic(shop: tuple[InMemoryEventStore, InMemoryStateStore], scan: Any) -> None:
    events, states = shop
    assert scan_opportunities(events.scan(), states.scan(), NOW) == scan


def test_suggest_opportunities_with_history_returns_the_sized_goals(
    shop: tuple[InMemoryEventStore, InMemoryStateStore], scan: Any
) -> None:
    events, states = shop
    goals = suggest_opportunities(states.scan(), "biz", NOW, events=events.scan())
    assert [g.goal_id for g in goals] == [p.opportunity_id for p in scan.proposals]
    first = goals[0]
    assert "extra profit a month" in first.text and first.budget == scan.proposals[0].budget
    assert all(g.status is GoalStatus.DRAFT for g in goals)
    taken = suggest_opportunities(
        states.scan(), "biz", NOW, events=events.scan(), taken=frozenset({GoalKind.WIN_BACK})
    )
    assert GoalKind.WIN_BACK not in {g.kind for g in taken}
    # Without history only counts are possible, and the budget is what the messages cost, not a guess.
    plain = suggest_opportunities(states.scan(), "biz", NOW)
    assert plain and all(g.budget < 1000 for g in plain)


def test_a_suggested_budget_without_history_is_costed_on_the_channels_the_shop_can_send_through() -> None:
    """400 one-time buyers, two messages each: $24.00 on a plan that may use WhatsApp (0.03 a message), not the
    $1.00 the email price gave; an email-only shop is costed at the email price (800 x 0.001, so the $1 floor).
    """
    from api.v2.deps import STARTING_PLAN_CHANNELS, budget_channels

    rows = [CustomerState(customer_id=f"c{i}", as_of=NOW, purchases=1) for i in range(400)]
    rows += [CustomerState(customer_id=f"n{i}", as_of=NOW, purchases=0) for i in range(5000)]
    plan = {g.kind: g.budget for g in suggest_opportunities(rows, "biz", NOW)}
    assert plan == {GoalKind.REPEAT_PURCHASE: 24.0, GoalKind.ONBOARDING: 300.0}
    email_only = suggest_opportunities(rows, "biz", NOW, cost_channels=budget_channels({Channel.EMAIL}))
    assert {g.kind: g.budget for g in email_only} == {
        GoalKind.REPEAT_PURCHASE: 1.0,
        GoalKind.ONBOARDING: 10.0,
    }
    # which channels: the starting plan's that have a sender, or the whole plan when none is known
    assert budget_channels({Channel.EMAIL, Channel.SMS}) == (Channel.EMAIL,)
    assert budget_channels({Channel.EMAIL, Channel.WHATSAPP}) == STARTING_PLAN_CHANNELS
    assert budget_channels(()) == STARTING_PLAN_CHANNELS


# --- limits ---------------------------------------------------------------------------------------


def test_limits_are_learned_from_the_shop(
    shop: tuple[InMemoryEventStore, InMemoryStateStore], scan: Any
) -> None:
    best = next(p for p in scan.proposals if p.preselected)
    learned = learn_limits(_history(shop), NOW, proposal=best)
    by = {lim.key: lim for lim in learned.limits}
    assert set(by) == {"cadence", "discount_depth", "discount_frequency", "margin", "quiet_hours", "budget"}
    assert by["cadence"].from_data and "median of 1." in by["cadence"].found
    assert learned.terms.max_touches == 2  # they send fewer than 2 a week, so the starting cap stands
    assert by["discount_depth"].from_data and "Never deeper than 1" in by["discount_depth"].use
    assert learned.terms.max_offer_pct is not None and 0.09 <= learned.terms.max_offer_pct <= 0.15
    assert by["discount_frequency"].from_data and "%" in by["discount_frequency"].found
    assert not by["margin"].from_data and by["margin"].question is not None
    assert learned.questions == (by["margin"].question,)
    assert by["quiet_hours"].from_data and learned.terms.timezone == "America/New_York"
    assert (learned.terms.quiet_start, learned.terms.quiet_end) == (21, 9)
    assert by["budget"].from_data and learned.terms.budget_ceiling == best.budget
    assert learned.terms.margin_floor_pct == pytest.approx(0.4 - learned.terms.max_offer_pct, abs=0.011)
    assert not learned.terms.offers_on
    for lim in learned.limits:
        assert lim.found and lim.use and lim.description and lim.source


def test_offers_on_adds_a_budget_never_above_todays_discounts(
    shop: tuple[InMemoryEventStore, InMemoryStateStore], scan: Any
) -> None:
    best = next(p for p in scan.proposals if p.preselected)
    history = _history(shop)
    off = learn_limits(history, NOW, proposal=best)
    on = learn_limits(history, NOW, proposal=best, offers_on=True)
    assert on.terms.offers_on and on.terms.budget_ceiling > off.terms.budget_ceiling
    recent = [p for p in history.all_purchases() if p.ts > NOW - timedelta(days=90)]
    monthly_discounts = sum(p.discount or 0.0 for p in recent) / 3
    assert on.terms.budget_ceiling <= math.ceil(off.terms.budget_ceiling + monthly_discounts)


def test_missing_data_falls_back_to_flagged_conservative_defaults() -> None:
    state = CustomerState(customer_id="c1", as_of=NOW)
    history = ShopHistory.build([], [state], NOW)
    learned = learn_limits(history, NOW)
    by = {lim.key: lim for lim in learned.limits}
    assert not any(lim.from_data for lim in learned.limits)
    assert learned.terms.max_touches == 2 and learned.terms.timezone == "UTC"
    assert learned.terms.max_offer_pct is None and learned.terms.budget_ceiling == 1.0
    assert "No discounts" in by["discount_depth"].use


# --- the mandate ----------------------------------------------------------------------------------


def _terms(shop: tuple[InMemoryEventStore, InMemoryStateStore], scan: Any) -> Any:
    best = next(p for p in scan.proposals if p.preselected)
    return learn_limits(_history(shop), NOW, proposal=best).terms


def test_single_owner_path_signs_alone_and_says_so(shop: Any, scan: Any) -> None:
    terms = _terms(shop, scan)
    keys = KeyRing()
    keys.add_hmac("k_owner", b"s" * 32, person_id="owner", roles=(SignerRole.MARKETING_LEAD,))
    draft = mandate_from_limits(
        terms, business_id="biz", goal_ids=("g1",), now=NOW, created_by="owner", single_owner=True
    )
    assert draft.frequency_cap.max_touches == terms.max_touches
    assert draft.quiet_hours.timezone == terms.timezone and draft.budget_ceiling == terms.budget_ceiling
    assert draft.note == SINGLE_OWNER_NOTE and draft.approved_offer_ids == ()
    signed, unmet = sign_as_owner(
        draft, HmacSigner("k_owner", b"s" * 32), keys, owner_id="owner", policy=SINGLE_OWNER_POLICY, now=NOW
    )
    assert unmet == [] and signed.status is MandateStatus.ACTIVE
    assert verify_mandate(signed, SINGLE_OWNER_POLICY, keys) == []
    # The two-person rule is still the default everywhere else.
    assert verify_mandate(signed, SignaturePolicy(), keys) == ["two_person_rule"]


def test_single_owner_still_needs_finance_above_the_threshold(shop: Any, scan: Any) -> None:
    terms = _terms(shop, scan).model_copy(update={"budget_ceiling": 12_000.0})
    keys = KeyRing()
    keys.add_hmac("k_owner", b"s" * 32, person_id="owner", roles=(SignerRole.MARKETING_LEAD,))
    draft = mandate_from_limits(
        terms, business_id="biz", goal_ids=("g1",), now=NOW, created_by="owner", single_owner=True
    )
    signed, unmet = sign_as_owner(
        draft, HmacSigner("k_owner", b"s" * 32), keys, owner_id="owner", policy=SINGLE_OWNER_POLICY, now=NOW
    )
    assert signed.status is MandateStatus.PENDING_SIGNATURES and unmet == ["missing_role:finance"]


# --- the API --------------------------------------------------------------------------------------


@pytest.fixture()
def client(shop: tuple[InMemoryEventStore, InMemoryStateStore]) -> Iterator[TestClient]:
    events, states = shop
    base = build_demo_services(NOW, n_customers=20, days=1)
    for goal in base.workspace.goals():  # a fresh shop: nothing is running yet
        base.workspace.put_goal(goal.model_copy(update={"status": GoalStatus.DONE}))
    services: Services = dataclasses.replace(base, events=events, states=states, clock=lambda: NOW)
    app = create_app(services)
    attach_onboarding(app, OnboardingDesk(secret=b"test-secret", persistent_key=True))
    with TestClient(app) as c:
        yield c


def test_api_proposals_limits_and_one_click_accept(client: TestClient) -> None:
    r = client.get("/v2/onboarding/proposals")
    assert r.status_code == 200
    body = r.json()
    best = body["preselected_id"]
    assert best and body["proposals"][0]["opportunity_id"] == best
    r = client.get("/v2/onboarding/limits")
    assert r.status_code == 200 and r.json()["opportunity_id"] == best
    terms = r.json()["terms"]

    r = client.post("/v2/onboarding/accept", json={"margin_pct": 40})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["goal"]["status"] == "active" and out["goal"]["autonomy"] == AutonomyLevel.SHADOW.value
    assert out["proposal"]["opportunity_id"] == best
    assert out["brief"]["audience"] == out["proposal"]["audience"]
    assert (
        out["brief"]["approved_by"]
        and out["brief"]["envelope"]["max_contacts_per_week"] <= terms["max_touches"]
    )
    assert out["brief"]["envelope"]["budget"] == terms["budget_ceiling"]
    mandate = out["mandate"]
    assert mandate["mandate"]["status"] == "active" and mandate["single_owner"] and mandate["unmet"] == []
    assert mandate["mandate"]["goal_ids"] == [out["goal"]["goal_id"]]
    assert "single-owner path" in out["words"] and "sends nothing" in out["words"]
    # One goal per kind: accepting the same kind again is refused.
    again = client.post("/v2/onboarding/accept", json={"opportunity_id": best, "margin_pct": 40})
    assert again.status_code == 409
    listed = client.get("/v2/onboarding/mandates").json()
    assert [m["mandate"]["mandate_id"] for m in listed] == [mandate["mandate"]["mandate_id"]]
    assert listed[0]["ledger_head"] == mandate["ledger_head"]
    goals = client.get("/v2/goals").json()
    assert out["goal"]["goal_id"] in {g["goal_id"] for g in goals}


def test_api_two_person_path_waits_and_keeps_shadow(client: TestClient) -> None:
    proposals = client.get("/v2/onboarding/proposals").json()["proposals"]
    pick = next(p for p in proposals if p["monthly_profit"] is not None and not p["preselected"])
    r = client.post(
        "/v2/onboarding/accept",
        json={
            "opportunity_id": pick["opportunity_id"],
            "single_owner": False,
            "autonomy": "autopilot",
            "margin_pct": 40,
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mandate"]["mandate"]["status"] == "pending_signatures"
    assert out["mandate"]["unmet"] == ["two_person_rule"] and "second person" in out["words"]
    assert out["goal"]["autonomy"] == "shadow" and any(
        "Autopilot needs an active mandate" in n for n in out["notes"]
    )


def test_api_margin_is_asked_once_and_used_after(client: TestClient) -> None:
    before = client.get("/v2/onboarding/proposals").json()
    assert not before["margin"]["from_data"]
    r = client.post("/v2/onboarding/answers", json={"margin_pct": 55})
    assert r.status_code == 200 and r.json()["margin"]["rate"] == pytest.approx(0.55)
    after = client.get("/v2/onboarding/proposals").json()
    assert after["margin"]["source"] == "your answer"
    a, b = before["proposals"][0], after["proposals"][0]
    assert b["monthly_profit"]["point"] > a["monthly_profit"]["point"]
    limits = client.get("/v2/onboarding/limits").json()
    assert limits["questions"] == [] and limits["terms"]["margin_rate"] == pytest.approx(0.55)


def test_api_refuses_unknown_proposals(client: TestClient) -> None:
    assert (
        client.post(
            "/v2/onboarding/accept", json={"opportunity_id": "opp_nope", "margin_pct": 40}
        ).status_code
        == 404
    )
    assert client.post("/v2/onboarding/answers", json={"margin_pct": 120}).status_code == 422


# --- review fixes ---------------------------------------------------------------------------------


def test_the_base_rate_interval_counts_each_person_once(
    shop: tuple[InMemoryEventStore, InMemoryStateStore], scan: Any
) -> None:
    """The look-backs overlap (one lapsed customer is lapsed 30, 60 and 90 days ago), so the sample
    size and the interval are those of the distinct people, not of the person-look-backs."""
    from autonomy.onboarding.opportunities import _SPECS, _base_rate, _Context, _wilson

    ctx = _Context.of(_history(shop))
    for spec in _SPECS:
        rate, people, used = _base_rate(ctx, spec)
        if rate is None:
            continue
        cuts = [NOW - timedelta(days=d) for d in (30, 60, 90)][:used]
        rows = [c for cut in cuts for c in spec.members(ctx, cut)]
        assert people == len(set(rows)) <= len(rows)
        proposal = next((p for p in scan.proposals if p.opportunity_id == spec.opportunity_id), None)
        if proposal is not None:
            assert proposal.base_sample == people
    narrow, wide = _wilson(20, 100), _wilson(20, 100, 40)
    assert narrow.point == wide.point == pytest.approx(0.2)
    assert wide.low < narrow.low and wide.high > narrow.high
    assert _wilson(20, 100, 500) == narrow  # never narrower than the observations allow


def _deliveries(people: int, per_week: float) -> list[Event]:
    """Klaviyo-style deliveries: `per_week` messages a week to each of `people` over 8 weeks."""
    out: list[Event] = []
    n = round(per_week * 8)
    for i in range(people):
        for j in range(n):
            out.append(
                Event(
                    event_id=f"d{i}_{j}",
                    customer_id=f"c{i}",
                    type=EventType.DELIVER,
                    ts=NOW - timedelta(days=1 + j * 55 / max(n, 1)),
                    source="klaviyo",
                    channel=Channel.EMAIL,
                )
            )
    return out


@pytest.mark.parametrize(("per_week", "cap"), [(0.25, 1), (1.0, 1), (1.5, 2), (3.0, 1)])
def test_the_message_cap_follows_the_shops_own_rhythm(per_week: float, cap: int) -> None:
    states = [CustomerState(customer_id=f"c{i}", as_of=NOW) for i in range(40)]
    history = ShopHistory.build(_deliveries(40, per_week), states, NOW)
    learned = learn_limits(history, NOW)
    cadence = next(lim for lim in learned.limits if lim.key == "cadence")
    assert cadence.from_data and learned.terms.max_touches == cap
    assert "on top of yours" in cadence.use and "jumps" not in cadence.use


def test_api_a_draft_goal_of_the_same_kind_does_not_block_accept(
    shop: tuple[InMemoryEventStore, InMemoryStateStore],
) -> None:
    events, states = shop
    base = build_demo_services(NOW, n_customers=20, days=1)
    for goal in base.workspace.goals():
        base.workspace.put_goal(goal.model_copy(update={"status": GoalStatus.DONE}))
    services: Services = dataclasses.replace(base, events=events, states=states, clock=lambda: NOW)
    app = create_app(services)
    attach_onboarding(app, OnboardingDesk(secret=b"test-secret", persistent_key=True))
    with TestClient(app) as c:
        best = c.get("/v2/onboarding/proposals").json()["proposals"][0]
        # Someone typed a goal of the same kind and never approved it: it is not running.
        draft, _ = services.create_goal("a draft nobody approved", 5.0, "USD", GoalKind(best["kind"]))
        assert draft.status is GoalStatus.DRAFT
        listed = c.get("/v2/onboarding/proposals").json()["proposals"]
        assert not next(p for p in listed if p["opportunity_id"] == best["opportunity_id"])["already_running"]
        assert c.post("/v2/onboarding/accept", json={"margin_pct": 40}).status_code == 200
        # A paused goal of that kind does hold it: a new goal must not get around a pause.
        services.pause(draft.goal_id)
        r = c.post("/v2/onboarding/accept", json={"opportunity_id": best["opportunity_id"], "margin_pct": 40})
        assert r.status_code == 409
