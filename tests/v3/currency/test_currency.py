"""Money in the shop's currency: an INR shop is priced in rupees end to end, and a goal that cannot be priced does not run."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from autonomy.contracts import (
    NO_OFFER,
    Alert,
    Audience,
    AutonomyLevel,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Envelope,
    Event,
    EventType,
    Goal,
    GoalKind,
    GoalStatus,
    Objective,
    Theme,
    Tone,
)
from autonomy.decide.bidders import DEFAULT_CHANNEL_COSTS
from autonomy.decide.journeys import JourneyConfig, JourneyRunner, load_journeys
from autonomy.decide.models import REFERENCE_ORDER_VALUE
from autonomy.fakes import ContactEveryonePolicy, FakeChannelAdapter
from autonomy.mandate import Mandate, MandateStatus
from autonomy.mandate.catalogue import ActionCatalogue, CatalogueContent, ChannelRate
from autonomy.runtime.agent import COMPLAINT_COST, build_agent_policy
from autonomy.runtime.prices import (
    COMPLAINT_SHARE_OF_ORDER,
    MissingShopMoneyError,
    ShopPrices,
    money_prefix,
    parse_channel_costs,
)
from autonomy.runtime.service import AutonomyRuntime, MandateBook, SpendLedger
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.runtime.v3 import _runner
from autonomy.store import memory_engine
from tests.v3.experiments.helpers import GOAL as AGENT_GOAL
from tests.v3.experiments.helpers import NOW as AGENT_NOW
from tests.v3.experiments.helpers import brief as agent_brief
from tests.v3.experiments.helpers import ctx, library, state

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
SWEEP = T0.replace(hour=10)
GOAL_ID = "goal_inr"
CUSTOMERS = ("c1", "c2", "c3")
INR_ENV = {
    "AUTONOMY_DATABASE_URL": "sqlite://",
    "AUTONOMY_CURRENCY": "INR",
    "AUTONOMY_CHANNEL_COSTS": "email=0.12, whatsapp=0.85",
    "AUTONOMY_TYPICAL_ORDER_VALUE": "1800",
}


def inr_goal(
    channels: tuple[Channel, ...] = (Channel.WHATSAPP,), currency: str = "INR"
) -> tuple[Goal, Brief]:
    start = T0 - timedelta(days=1)
    goal = Goal(
        goal_id=GOAL_ID,
        business_id="shop",
        text="Win back lapsed customers, 50000 rupees this month",
        kind=GoalKind.WIN_BACK,
        budget=50_000,
        currency=currency,
        starts_at=start,
        created_at=start,
        autonomy=AutonomyLevel.AUTOPILOT,
        status=GoalStatus.ACTIVE,
    )
    brief = Brief(
        brief_id="brief_inr_1",
        goal_id=GOAL_ID,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="everyone"),
        envelope=Envelope(channels=channels, offers=(NO_OFFER,), budget=50_000, max_contacts_per_week=3),
        created_at=start,
        approved_by="founder",
        approved_at=start,
    )
    return goal, brief


def content(channel: Channel = Channel.WHATSAPP) -> list[ContentItem]:
    return [
        ContentItem(
            content_id=f"ci_{channel.value}",
            goal_id=GOAL_ID,
            channel=channel,
            theme=Theme.REMINDER,
            tone=Tone.FRIENDLY,
            offer_id="none",
            subject="We miss you" if channel is Channel.EMAIL else None,
            body="Hi {{first_name}}, something new for you.",
            status=ContentStatus.APPROVED,
            created_at=T0 - timedelta(days=1),
        )
    ]


def history() -> list[Event]:
    out: list[Event] = []
    for cid in CUSTOMERS:
        out.append(
            Event(
                event_id=f"consent_{cid}",
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=T0 - timedelta(days=200),
                source="test",
                channel=Channel.WHATSAPP,
            )
        )
        out.append(
            Event(
                event_id=f"buy_{cid}",
                customer_id=cid,
                type=EventType.PURCHASE,
                ts=T0 - timedelta(days=90),
                source="test",
                value=1800.0,
                category="saree",
            )
        )
    return out


def runtime_for(
    env: dict[str, str],
    *,
    adapter: FakeChannelAdapter | None = None,
    catalogue: ActionCatalogue | None = None,
    agent: bool = False,
    mandates: MandateBook | None = None,
    engine: Engine | None = None,
) -> AutonomyRuntime:
    """The production runtime from `env`; `agent` keeps its own agent policy (built with the shop's prices)."""
    return build_runtime(
        RuntimeSettings.from_env(env),
        engine=engine or memory_engine(),
        policy_factory=None if agent else lambda briefs, items: ContactEveryonePolicy(),
        adapters={Channel.WHATSAPP: adapter or FakeChannelAdapter(Channel.WHATSAPP)},
        address_book=lambda cid, ch: f"+91{cid}",
        catalogue=catalogue,
        mandates=mandates,
    )


def seed(runtime: AutonomyRuntime, goal: Goal, brief: Brief) -> None:
    runtime.workspace.put_goal(goal)
    runtime.workspace.put_brief(brief)
    runtime.workspace.put_content(content())
    runtime.events.append(history())


def whatsapp_sends(runtime: AutonomyRuntime) -> list[Event]:
    return [e for e in runtime.events.scan() if e.type is EventType.SEND and e.channel is Channel.WHATSAPP]


# --- an INR goal end to end -------------------------------------------------------------------------


def test_an_inr_goal_records_whatsapp_spend_as_sends_times_the_inr_price() -> None:
    adapter = FakeChannelAdapter(Channel.WHATSAPP)
    runtime = runtime_for(INR_ENV, adapter=adapter)
    goal, brief = inr_goal()
    seed(runtime, goal, brief)

    runtime.tick(SWEEP)
    sent = whatsapp_sends(runtime)

    assert (
        len(sent)
        == len(adapter.sent)
        == len(CUSTOMERS) - len([d for d in runtime.decisions.scan() if d.group.value == "holdout"])
    )
    assert sent, "the goal sent nothing, so there is nothing to price"
    assert runtime.spend_ledger is not None
    assert runtime.spend_ledger.spent(GOAL_ID, SWEEP + timedelta(hours=1)) == pytest.approx(len(sent) * 0.85)
    # What the old dollar table would have recorded: the bug this closes.
    assert len(sent) * DEFAULT_CHANNEL_COSTS[Channel.WHATSAPP] < len(sent) * 0.85
    assert runtime.channel_costs[Channel.WHATSAPP] == 0.85 and runtime.channel_costs[Channel.EMAIL] == 0.12


def test_the_overspend_breaker_and_the_budget_use_the_same_inr_price() -> None:
    runtime = runtime_for(INR_ENV)
    goal, brief = inr_goal()
    seed(runtime, goal, brief)
    runtime.tick(SWEEP)
    sent = len(whatsapp_sends(runtime))
    # The breakers were given the runtime's own ledger, so they read the rupee spend, not a dollar one.
    assert runtime.spend_fn is runtime.spend_ledger
    assert runtime.spend_fn(GOAL_ID, SWEEP + timedelta(hours=1)) == pytest.approx(sent * 0.85)


# --- a goal that cannot be priced does not run --------------------------------------------------------


def test_a_goal_without_prices_refuses_to_start() -> None:
    env = {
        "AUTONOMY_DATABASE_URL": "sqlite://",
        "AUTONOMY_CURRENCY": "INR",
        "AUTONOMY_TYPICAL_ORDER_VALUE": "1800",
    }
    engine = memory_engine()
    first = runtime_for(env, engine=engine)
    goal, brief = inr_goal()
    seed(first, goal, brief)
    # An old dollar goal that ended but was never marked done is not live, and blocks nothing.
    ended = goal.model_copy(update={"goal_id": "goal_old_usd", "currency": "USD", "ends_at": T0})
    first.workspace.put_goal(ended)

    # The process still starts with an unpriced live goal (the API must still pause or fix it and take
    # webhooks); the goal itself does not run.
    runtime = runtime_for(env, engine=engine)
    report = runtime.tick(SWEEP)
    assert report.decisions == [] and whatsapp_sends(runtime) == []
    (alert,) = [a for a in report.alerts if a.code == "prices.missing"]
    assert alert.goal_id == GOAL_ID and "no INR price for whatsapp" in alert.message
    with pytest.raises(MissingShopMoneyError, match="no INR price for whatsapp"):
        runtime.check_goal_prices(GOAL_ID)

    from api.v2.deps import ConflictError, build_services_from_runtime

    services = build_services_from_runtime(runtime, clock=lambda: T0, business_id="shop")
    paused = services.pause(GOAL_ID)
    assert paused.status is GoalStatus.PAUSED
    with pytest.raises(ConflictError, match="no INR price for whatsapp"):
        services.resume(GOAL_ID)


def test_channel_costs_given_with_no_currency_are_refused_not_dropped() -> None:
    plain = RuntimeSettings(database_url="sqlite://")
    with pytest.raises(ValueError, match="set currency"):
        build_runtime(plain, engine=memory_engine(), channel_costs={Channel.WHATSAPP: 0.85})
    with pytest.raises(ValueError, match="set currency"):
        plain.shop_prices(channel_costs={Channel.WHATSAPP: 0.85})
    # With a catalogue the currency may come from it, so the costs are kept.
    cat, _ = catalogue_with(0.78)
    assert plain.shop_prices(cat, channel_costs={Channel.EMAIL: 0.12}).costs()[Channel.EMAIL] == 0.12


def test_a_goal_approved_later_without_prices_does_not_decide_and_says_why() -> None:
    env = {"AUTONOMY_DATABASE_URL": "sqlite://"}
    runtime = runtime_for(env)
    goal, brief = inr_goal()
    seed(runtime, goal, brief)

    report = runtime.tick(SWEEP)

    assert report.decisions == [] and whatsapp_sends(runtime) == []
    (alert,) = [a for a in report.alerts if a.code == "prices.missing"]
    assert isinstance(alert, Alert) and alert.severity == "critical" and alert.goal_id == GOAL_ID
    assert "AUTONOMY_CURRENCY" in alert.message and "AUTONOMY_TYPICAL_ORDER_VALUE" in alert.message
    with pytest.raises(MissingShopMoneyError):
        runtime.check_goal_prices(GOAL_ID)


def test_a_goal_in_another_currency_than_the_shops_is_refused() -> None:
    runtime = runtime_for(INR_ENV)
    goal, brief = inr_goal(currency="USD")  # the old default currency, on a rupee shop
    seed(runtime, goal, brief)
    report = runtime.tick(SWEEP)
    assert report.decisions == []
    (alert,) = [a for a in report.alerts if a.code == "prices.missing"]
    assert "USD" in alert.message and "INR" in alert.message


def test_only_the_channels_a_goal_may_use_need_a_price() -> None:
    env = {**INR_ENV, "AUTONOMY_CHANNEL_COSTS": "whatsapp=0.85"}  # no email price
    goal, brief = inr_goal(channels=(Channel.WHATSAPP,))
    ShopPrices(currency="INR", channel_costs={Channel.WHATSAPP: 0.85}, typical_order_value=1800).check_goal(
        goal, brief
    )
    _, both = inr_goal(channels=(Channel.EMAIL, Channel.WHATSAPP))
    with pytest.raises(MissingShopMoneyError, match="email"):
        RuntimeSettings.from_env(env).shop_prices().check_goal(goal, both)


def test_free_channels_cost_nothing_unless_priced() -> None:
    table = ShopPrices(currency="INR", channel_costs={Channel.SMS: 0.2}).costs()
    assert table[Channel.PUSH] == table[Channel.WEB] == table[Channel.WEBHOOK] == 0.0
    assert table[Channel.SMS] == 0.2 and Channel.WHATSAPP not in table  # never defaulted


# --- the mandate catalogue's provider prices ------------------------------------------------------------


def catalogue_with(
    whatsapp: float, *, other_currency: float | None = None
) -> tuple[ActionCatalogue, datetime]:
    rates = [ChannelRate(channel=Channel.WHATSAPP, unit_cost=whatsapp, currency="INR", provider="gupshup")]
    if other_currency is not None:
        rates.append(
            ChannelRate(channel=Channel.WHATSAPP, unit_cost=other_currency, currency="USD", provider="twilio")
        )
    cat = ActionCatalogue()
    cat.publish(CatalogueContent(channels=tuple(rates)), created_by="ops", now=T0 - timedelta(days=2))
    return cat, T0


def test_catalogue_prices_win_over_the_settings_and_are_read_again_every_tick() -> None:
    cat, _ = catalogue_with(0.78, other_currency=0.01)
    runtime = runtime_for(INR_ENV, catalogue=cat)
    goal, brief = inr_goal()
    seed(runtime, goal, brief)
    assert (
        runtime.channel_costs[Channel.WHATSAPP] == 0.78
    )  # gupshup's INR rate, not the cheaper USD one, not 0.85

    runtime.tick(SWEEP)
    first = len(whatsapp_sends(runtime))
    assert runtime.spend_ledger is not None
    assert runtime.spend_ledger.spent(GOAL_ID, SWEEP + timedelta(hours=1)) == pytest.approx(first * 0.78)

    cat.publish(
        CatalogueContent(
            channels=(
                ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.95, currency="INR", provider="gupshup"),
            )
        ),
        created_by="ops",
        now=SWEEP + timedelta(hours=2),
    )
    runtime.tick(SWEEP + timedelta(days=1))
    assert runtime.channel_costs[Channel.WHATSAPP] == 0.95
    assert runtime.spend_ledger.channel_costs[Channel.WHATSAPP] == 0.95


def test_a_catalogue_alone_can_price_a_goal_when_it_names_one_currency() -> None:
    cat, _ = catalogue_with(0.78)
    env = {"AUTONOMY_DATABASE_URL": "sqlite://", "AUTONOMY_TYPICAL_ORDER_VALUE": "1800"}
    prices = RuntimeSettings.from_env(env).shop_prices(cat)
    assert prices.shop_currency() == "INR" and prices.costs()[Channel.WHATSAPP] == 0.78
    goal, brief = inr_goal()
    prices.check_goal(goal, brief)
    mixed, _ = catalogue_with(0.78, other_currency=0.01)
    assert RuntimeSettings.from_env(env).shop_prices(mixed).shop_currency() is None


# --- the settings --------------------------------------------------------------------------------------


def test_channel_costs_are_a_typed_setting_read_from_the_environment() -> None:
    s = RuntimeSettings.from_env(INR_ENV)
    assert s.currency == "INR" and s.typical_order_value == 1800.0
    assert s.channel_costs == {Channel.EMAIL: 0.12, Channel.WHATSAPP: 0.85}
    assert parse_channel_costs("sms=0.2") == {Channel.SMS: 0.2}
    for bad in ("whatsapp", "carrier_pigeon=1", "whatsapp=-1", "whatsapp=nan", "whatsapp=x"):
        with pytest.raises(ValueError):
            parse_channel_costs(bad)
    with pytest.raises(ValueError, match="set currency"):
        RuntimeSettings.from_env({"AUTONOMY_CHANNEL_COSTS": "whatsapp=0.85"})
    with pytest.raises(ValueError):
        RuntimeSettings(currency="rupees")
    # Nothing is defaulted: the plain settings carry no money at all.
    plain = RuntimeSettings()
    assert plain.currency is None and plain.channel_costs == {} and plain.typical_order_value is None


def test_the_complaint_cost_is_a_share_of_the_usual_order_unless_set() -> None:
    assert ShopPrices(currency="INR", typical_order_value=1800).complaint_cost() == pytest.approx(
        COMPLAINT_SHARE_OF_ORDER * 1800
    )
    assert ShopPrices(currency="USD", typical_order_value=50).complaint_cost() == pytest.approx(
        COMPLAINT_COST
    )
    assert (
        ShopPrices(currency="INR", typical_order_value=1800, complaint_cost_override=40).complaint_cost()
        == 40
    )
    assert ShopPrices(currency="INR").complaint_cost() is None
    assert money_prefix("INR") == "₹" and money_prefix("usd") == "$" and money_prefix("CHF") == "CHF "


# --- the agent is priced in the shop's currency ------------------------------------------------------------


def test_the_agent_prices_whatsapp_at_the_inr_cost_and_a_default_agent_does_not() -> None:
    inr = build_agent_policy(
        [agent_brief()],
        list(library()),
        costs={Channel.EMAIL: 0.12, Channel.WHATSAPP: 0.85},
        complaint_cost=72.0,
        default_order_value=1800.0,
        currency="₹",
    )
    plain = build_agent_policy([agent_brief()], list(library()))

    def whatsapp_cost(agent: object) -> set[float]:
        made = agent.decide(  # type: ignore[attr-defined]
            state("c1", as_of=AGENT_NOW, purchases=2, revenue=3600.0), ctx(seed=1)
        )
        return {b.channel_cost for b in made.bids if b.action.channel is Channel.WHATSAPP}

    assert whatsapp_cost(inr) == {0.85}
    assert whatsapp_cost(plain) == {DEFAULT_CHANNEL_COSTS[Channel.WHATSAPP]}
    assert inr.contact_price.complaint_cost == 72.0 and plain.contact_price.complaint_cost == COMPLAINT_COST
    # A customer with no orders is valued at the shop's usual order, not at 50 rupees.
    assert inr.bundle.value.default_order_value == 1800.0
    assert AGENT_GOAL in inr.briefs


def test_the_journeys_value_bar_scales_with_the_shops_order_value() -> None:
    # Loaded with no shop, the bars stay multiples of the order value: no fixed number is applied by default.
    loose = load_journeys()
    assert loose.unresolved() == ("C", "D") and loose.get("C").value_bar is None  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="for_shop"):
        JourneyRunner(loose)
    reference = load_journeys(order_value=REFERENCE_ORDER_VALUE)
    assert reference.get("C").value_bar == 50.0 and reference.get("D").value_bar == 50.0  # type: ignore[union-attr]
    assert reference.get("A").value_bar is None  # type: ignore[union-attr]
    rupees = load_journeys(order_value=1800.0)
    assert rupees.get("C").value_bar == 1800.0  # type: ignore[union-attr]
    # A config loaded and passed on gets the shop's bar, not 50 rupees.
    runner = _runner(loose, 1800.0)
    assert runner is not None and runner.config.get("C").value_bar == 1800.0  # type: ignore[union-attr]
    agent = build_agent_policy(
        [agent_brief()], list(library()), journeys=load_journeys(), default_order_value=1800.0
    )
    assert agent.policy.journeys is not None
    assert agent.policy.journeys.config.get("D").value_bar == 1800.0  # type: ignore[union-attr]
    # None is "no bar", and stays so whatever the shop's order value.
    unbarred = loose.with_value_bar("C", None)
    assert unbarred.unresolved() == ("D",)
    gone = _runner(unbarred, 1800.0)
    assert gone is not None and gone.config.get("C").value_bar is None  # type: ignore[union-attr]
    # An explicit bar keeps winning over the multiple, and a multiple nobody resolved is refused, not guessed.
    pinned = rupees.with_value_bar("C", 500.0).for_shop(9999.0)
    assert pinned.get("C").value_bar == 500.0  # type: ignore[union-attr]
    raw = JourneyConfig.model_validate(
        {
            "journeys": [
                {
                    "journey_id": "X",
                    "name": "x",
                    "value_bar_order_multiple": 2.0,
                    "steps": [{"channel": "whatsapp"}],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="for_shop"):
        JourneyRunner(raw)
    assert JourneyRunner(raw.for_shop(1000.0)).config.get("X").value_bar == 2000.0  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        raw.for_shop(0.0)


def test_the_v3_preset_sets_its_journeys_bar_from_the_shops_order_value() -> None:
    agent = build_agent_policy([agent_brief()], list(library()), v3=True, default_order_value=1800.0)
    runner = agent.policy.journeys
    assert runner is not None and runner.config.get("C").value_bar == 1800.0  # type: ignore[union-attr]
    default = build_agent_policy([agent_brief()], list(library()), v3=True)
    assert default.policy.journeys is not None
    assert default.policy.journeys.config.get("C").value_bar == 50.0  # type: ignore[union-attr]


def test_the_runtime_builds_each_goals_agent_with_the_shops_money() -> None:
    from autonomy.runtime.settings import agent_policy_factory

    prices = RuntimeSettings.from_env(INR_ENV).shop_prices()
    _, brief = inr_goal()
    agent = agent_policy_factory([brief], content(), prices=prices)
    assert agent.contact_price.complaint_cost == pytest.approx(COMPLAINT_SHARE_OF_ORDER * 1800)  # type: ignore[attr-defined]
    assert agent.costs[Channel.WHATSAPP] == 0.85  # type: ignore[attr-defined]
    assert agent.policy.currency == "₹"  # type: ignore[attr-defined]


def test_the_ledger_default_stays_dollar_sized_for_the_gym_and_the_demo() -> None:
    from autonomy.fakes import InMemoryDecisionLog, InMemoryWorkspace

    ledger = SpendLedger(InMemoryDecisionLog(), InMemoryWorkspace())
    assert ledger.channel_costs == dict(DEFAULT_CHANNEL_COSTS)


# --- the API ------------------------------------------------------------------------------------------


def test_the_api_refuses_to_approve_a_goal_the_shop_cannot_price_and_defaults_to_the_shops_currency() -> None:
    from api.v2.deps import ConflictError, build_services_from_runtime

    runtime = runtime_for({"AUTONOMY_DATABASE_URL": "sqlite://"})
    services = build_services_from_runtime(runtime, clock=lambda: T0, business_id="shop")
    goal, brief = inr_goal()
    draft = goal.model_copy(update={"status": GoalStatus.DRAFT, "autonomy": AutonomyLevel.SHADOW})
    runtime.workspace.put_goal(draft)
    runtime.workspace.put_brief(brief.model_copy(update={"approved_at": None, "approved_by": None}))
    with pytest.raises(ConflictError, match="cannot run until the shop's money is set"):
        services.approve(GOAL_ID, AutonomyLevel.APPROVE)
    assert runtime.workspace.goals()[0].status is GoalStatus.DRAFT

    priced = runtime_for(INR_ENV)
    shop = build_services_from_runtime(priced, clock=lambda: T0, business_id="shop")
    created, _ = shop.create_goal("win back lapsed customers", 50_000)
    assert created.currency == "INR"  # not the old USD default
    approved, _ = shop.approve(created.goal_id, AutonomyLevel.APPROVE)
    assert approved.status is GoalStatus.ACTIVE


# --- review fixes: the agent's bids, the mandate, the dearest provider, onboarding -----------------------


def inr_mandate(**kw: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": "m_inr",
        "business_id": "shop",
        "goal_ids": (GOAL_ID,),
        "budget_ceiling": 50_000.0,
        "currency": "INR",
        "channels": (Channel.WHATSAPP,),
        "approved_template_ids": tuple(c.content_id for c in content()),
        "valid_from": T0 - timedelta(days=10),
        "valid_until": T0 + timedelta(days=30),
        "status": MandateStatus.ACTIVE,
    }
    base.update(kw)
    return Mandate.model_validate(base)


def test_an_inr_mandate_cap_stops_sending_once_the_rupee_spend_reaches_it() -> None:
    # A daily cap of 4 rupees: the pacer lets a quarter (1.00) go at once. At 0.85 a WhatsApp message one
    # send fits; at the old dollar table's 0.03 all of them would have.
    runtime = runtime_for(INR_ENV, mandates=MandateBook([inr_mandate(max_daily_spend=4.0)]))
    goal, brief = inr_goal()
    seed(runtime, goal, brief)
    report = runtime.tick(SWEEP)
    acted = [d for d in report.decisions if d.action is not None and d.group.value != "holdout"]
    assert len(acted) >= 2 and len(acted) * DEFAULT_CHANNEL_COSTS[Channel.WHATSAPP] < 1.0
    assert len(whatsapp_sends(runtime)) == 1 and len(report.mandate_blocked) == len(acted) - 1
    assert "mandate.paced" in {a.code for a in report.alerts}
    assert runtime.spend_fn(GOAL_ID, SWEEP + timedelta(hours=1)) == pytest.approx(0.85)


def test_a_mandate_in_another_currency_does_not_authorise_the_goal() -> None:
    runtime = runtime_for(INR_ENV, mandates=MandateBook([inr_mandate(currency="USD")]))
    goal, brief = inr_goal()
    seed(runtime, goal, brief)
    report = runtime.tick(SWEEP)
    assert whatsapp_sends(runtime) == [] and GOAL_ID in report.halted_goals
    (alert,) = [a for a in report.alerts if a.code == "mandate.not_active"]
    assert "USD" in alert.message and "INR" in alert.message


def test_the_catalogue_charges_the_dearest_provider_in_the_shops_currency() -> None:
    cat = ActionCatalogue()
    rates = (
        ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.78, currency="INR", provider="gupshup"),
        ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.92, currency="INR", provider="karix"),
        ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.01, currency="USD", provider="twilio"),
    )
    cat.publish(CatalogueContent(channels=rates), created_by="ops", now=T0 - timedelta(days=2))
    # The gateway may fail over to either INR provider: the budget holds the dearer one.
    assert RuntimeSettings.from_env(INR_ENV).shop_prices(cat).costs()[Channel.WHATSAPP] == 0.92


def test_a_newly_published_catalogue_rate_reaches_the_agents_bids() -> None:
    cat, _ = catalogue_with(0.78)
    runtime = runtime_for(INR_ENV, catalogue=cat, agent=True)
    goal, brief = inr_goal()
    seed(runtime, goal, brief)

    def whatsapp_bid_costs(since: datetime) -> set[float]:
        return {
            b.channel_cost
            for d in runtime.decisions.scan(since=since)
            for b in d.bids
            if b.action.channel is Channel.WHATSAPP
        }

    runtime.tick(SWEEP)
    assert whatsapp_bid_costs(SWEEP) == {0.78}

    cat.publish(
        CatalogueContent(
            channels=(
                ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.95, currency="INR", provider="gupshup"),
            )
        ),
        created_by="ops",
        now=SWEEP + timedelta(hours=2),
    )
    later = SWEEP + timedelta(days=1)
    runtime.tick(later)
    # The goal's agent was built again with the new price: its bids and the ledger agree.
    assert whatsapp_bid_costs(later) == {0.95}
    assert runtime.channel_costs[Channel.WHATSAPP] == 0.95


class _PricedRuntime:
    """The two things the API reads from a production runtime about money."""

    def __init__(self, prices: ShopPrices, workspace: Any) -> None:
        self.prices = prices
        self.workspace = workspace

    def check_goal_prices(self, goal: Goal) -> None:
        briefs = self.workspace.briefs(goal.goal_id)
        self.prices.check_goal(goal, max(briefs, key=lambda b: b.version) if briefs else None)


def onboarding_client(prices: ShopPrices) -> tuple[Any, Any, Any]:
    import dataclasses

    from fastapi.testclient import TestClient

    from api.v2.app import create_app
    from api.v2.deps import build_demo_services
    from api.v2.onboarding import OnboardingDesk, attach_onboarding
    from tests.v2.zero_touch.test_goals_limits import NOW as SHOP_NOW
    from tests.v2.zero_touch.test_goals_limits import shopify_shop

    events, states = shopify_shop(n_customers=250, days=300)
    base = build_demo_services(SHOP_NOW, n_customers=20, days=1)
    for g in base.workspace.goals():  # a fresh shop: nothing is running yet
        base.workspace.put_goal(g.model_copy(update={"status": GoalStatus.DONE}))
    services = dataclasses.replace(
        base,
        events=events,
        states=states,
        clock=lambda: SHOP_NOW,
        runtime=_PricedRuntime(prices, base.workspace),  # type: ignore[arg-type]
    )
    desk = OnboardingDesk(secret=b"test-secret", persistent_key=True)
    app = create_app(services)
    attach_onboarding(app, desk)
    return TestClient(app), services, desk


def test_an_inr_shops_accepted_proposal_is_budgeted_at_the_inr_price() -> None:
    from autonomy.onboarding.opportunities import MESSAGES_COSTED

    prices = ShopPrices(
        currency="INR",
        channel_costs={Channel.EMAIL: 0.12, Channel.SMS: 0.2, Channel.WHATSAPP: 0.85},
        typical_order_value=1800,
    )
    client, services, _ = onboarding_client(prices)
    with client:
        proposals = client.get("/v2/onboarding/proposals").json()["proposals"]
        win_back = next(p for p in proposals if p["opportunity_id"] == "opp_win_back")
        assert win_back["currency"] == "INR" and win_back["message_cost"] is not None
        r = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back", "margin_pct": 40})
    assert r.status_code == 200, r.text
    out = r.json()
    reached = out["proposal"]["reached_in_month"]
    # Everyone in this shop is reached by e-mail: the budget is reached x messages x the INR e-mail price,
    # where the dollar table (0.001) gives the 1-rupee floor.
    expected = max(1, math.ceil(round(reached * MESSAGES_COSTED * 0.12, 2)))
    assert out["goal"]["currency"] == "INR" and out["goal"]["budget"] == expected
    assert expected > max(1, math.ceil(reached * MESSAGES_COSTED * DEFAULT_CHANNEL_COSTS[Channel.EMAIL]))
    assert out["mandate"]["mandate"]["budget_ceiling"] == expected
    assert out["mandate"]["mandate"]["currency"] == "INR"
    assert out["goal"]["goal_id"] in {g.goal_id for g in services.workspace.goals()}


def test_onboarding_without_the_shops_prices_quotes_no_money_and_writes_nothing() -> None:
    prices = ShopPrices(currency="INR", typical_order_value=1800)  # no price for any paid channel
    client, services, desk = onboarding_client(prices)
    before = {g.goal_id for g in services.workspace.goals()}
    head = desk.ledger.head
    with client:
        proposals = client.get("/v2/onboarding/proposals").json()["proposals"]
        win_back = next(p for p in proposals if p["opportunity_id"] == "opp_win_back")
        assert win_back["budget"] == 0.0 and win_back["message_cost"] is None
        assert "No INR price per message for email" in win_back["not_enough"]
        r = client.post("/v2/onboarding/accept", json={"opportunity_id": "opp_win_back", "margin_pct": 40})
    assert r.status_code == 409 and "no INR price for email" in r.json()["detail"]
    assert {g.goal_id for g in services.workspace.goals()} == before
    assert desk.signed == {} and desk.ledger.head == head
    assert services.margin_rate is None and desk.margin_rate is None  # a refused accept keeps no margin
