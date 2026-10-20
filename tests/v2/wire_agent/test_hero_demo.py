"""The hero example end to end: the demo's refill goal, products, image formats and signed mandate.

One small Gym run (300 customers, 10 days, a few seconds) is shared by the module; the late-goal
test runs a second, tiny one. Nothing here reads the Gym's truth.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from autonomy.assets import supports
from autonomy.catalog import REFILL_KEYS
from autonomy.contracts import (
    AutonomyLevel,
    DecisionContext,
    DecisionKind,
    EventType,
    Format,
    GoalKind,
    Group,
    Theme,
)
from autonomy.gym import GYM_OFFERS
from autonomy.mandate import MandateStatus, verify_mandate
from autonomy.runtime.demo import (
    HERO_PRODUCTS,
    REFILL_AUDIENCE,
    DemoResult,
    HeroLayer,
    ProductLayer,
    hero_story,
    run_demo,
)


@pytest.fixture(scope="module")
def demo() -> DemoResult:
    return run_demo(scenario="baseline_shop", days=10, customers=300, seed=0)


def _hero(result: DemoResult) -> HeroLayer:
    assert result.hero is not None
    return result.hero


def _refill_goal(result: DemoResult) -> str:
    return next(g.goal_id for g in result.goals if g.kind is GoalKind.REPEAT_PURCHASE)


def test_the_hero_run_has_a_refill_goal_beside_win_back(demo: DemoResult) -> None:
    kinds = {g.kind: g for g in demo.goals}
    assert kinds[GoalKind.WIN_BACK].autonomy is AutonomyLevel.AUTOPILOT
    assert kinds[GoalKind.REPEAT_PURCHASE].autonomy is AutonomyLevel.APPROVE
    refill_brief = next(b for b in demo.briefs if b.goal_id == _refill_goal(demo))
    assert refill_brief.audience == REFILL_AUDIENCE
    assert demo.violations == []
    # The runtime skips (and only alerts on) customers whose policy raised: the hero wrapper must not.
    assert not [a for a in demo.alerts if a.code == "decide.error"]


def test_purchases_carry_products_and_the_catalogue_comes_from_them(demo: DemoResult) -> None:
    hero = _hero(demo)
    purchases = [e for e in demo.events if e.type is EventType.PURCHASE]
    assert purchases and all(e.item_id in hero.catalogue for e in purchases)
    assert all(hero.catalogue.category_of(str(e.item_id)) == e.category for e in purchases)
    replenishable = {p.item_id for p in HERO_PRODUCTS if p.replenishable}
    assert set(hero.catalogue.replenishable_items()) == replenishable
    # Prices were not stated: they come from the labelled purchases.
    assert all(p.price > 0 for p in hero.catalogue)
    assert hero.cycles is not None and hero.co is not None


def test_the_product_layer_is_deterministic(demo: DemoResult) -> None:
    purchases = [e.model_copy(update={"item_id": None}) for e in demo.events if e.type is EventType.PURCHASE]
    first = ProductLayer(seed=0).label(purchases[:200])
    second = ProductLayer(seed=0).label(purchases[:200])
    assert [e.item_id for e in first] == [e.item_id for e in second]
    by_customer = Counter((e.customer_id, e.category, e.item_id) for e in first)
    usual = ProductLayer(seed=0)
    # Most of a customer's purchases in a category are their usual product.
    assert sum(
        n for (c, cat, item), n in by_customer.items() if item == usual.usual(c, str(cat))
    ) > 0.5 * len(first)


def test_the_demo_makes_refill_decisions_about_a_product(demo: DemoResult) -> None:
    hero = _hero(demo)
    goal = _refill_goal(demo)
    content = {c.content_id: c for c in demo.content}
    # The agent's own decisions; the baseline group follows its fixed rule and names no product.
    refills = [
        d
        for d in demo.decisions
        if d.action is not None and d.action.goal_id == goal and d.group is Group.AGENT
    ]
    assert refills and all(d.action is not None and d.action.item_id for d in refills)
    for d in refills:
        note = hero.note(d)
        assert note is not None and note.item_kind == "refill"
        assert d.action is not None and d.action.item_id == note.refill_item
        assert set(note.refill_scores) <= set(REFILL_KEYS) and "refill_due_days" in note.refill_scores
        assert -28 <= note.refill_scores["refill_due_days"] <= 7  # inside the refill audience
        assert hero.catalogue.is_replenishable(str(d.action.item_id))
    # The refill reminder is always one of the options weighed; which message wins is the agent's call.
    weighed = {content[str(b.action.content_id)].theme for d in refills for b in d.bids}
    assert Theme.REMINDER in weighed and len(weighed) >= 3
    sent = {e.decision_id for e in demo.events if e.type is EventType.SEND}
    assert any(d.decision_id in sent for d in refills)


def test_image_formats_are_chosen_where_the_channel_carries_them(demo: DemoResult) -> None:
    hero = _hero(demo)
    content = {c.content_id: c for c in demo.content}
    assets = {a.asset_id: a for a in hero.assets}
    media = [d for d in demo.decisions if d.action is not None and d.action.format is not Format.TEXT]
    assert any(d.action is not None and d.action.format is Format.TEXT_IMAGE for d in media)
    for d in media:
        a = d.action
        assert a is not None and supports(a.channel, a.format)
        asset = assets[str(a.asset_id)]
        assert asset.is_approved and asset.alt_text.strip() and asset.url.startswith("https://")
        assert asset.url in (content[str(a.content_id)].asset_url or "").split()
    assert {c.format for c in demo.content} >= {Format.TEXT, Format.TEXT_IMAGE}


def test_every_decision_passes_the_signed_mandate(demo: DemoResult) -> None:
    hero = _hero(demo)
    setup = hero.mandates
    mandate = setup.mandate
    assert verify_mandate(mandate, setup.policy, setup.keys) == []
    assert mandate.status is MandateStatus.ACTIVE and len({s.person_id for s in mandate.signers}) == 2
    assert mandate.margin_floor_pct == pytest.approx(0.2)
    assert setup.ledger.verify_chain(keys=setup.keys).ok
    checks = {(c.goal_id, c.customer_id, c.made_at): c for c in hero.checks}
    assert len(checks) == len(demo.decisions)
    offers = {o.offer_id: o for o in GYM_OFFERS}
    states = {s.customer_id: s for s in demo.stores.states.scan()}
    acts = [d for d in demo.decisions if d.kind is DecisionKind.ACT and d.action is not None]
    assert acts
    for d in acts:
        a = d.action
        assert a is not None
        check = checks[(a.goal_id, d.customer_id, d.made_at)]
        assert check.codes == () and check.content_id == a.content_id
        # The same limits, checked independently of `check_action`.
        assert a.content_id in mandate.approved_template_ids and a.channel in mandate.channels
        assert a.offer_id == "none" or a.offer_id in mandate.approved_offer_ids
        local = a.send_at.astimezone(ZoneInfo(states[d.customer_id].timezone or "UTC")).hour
        assert not mandate.quiet_hours.is_quiet(local)
        if a.offer_id != "none":
            note = hero.note(d)
            assert note is not None and note.avg_order_value > 0
            margin = demo.costs.margin_rate * note.avg_order_value - offers[a.offer_id].cost_per_redemption
            assert margin / note.avg_order_value >= mandate.margin_floor_pct - 1e-9
    # Anything the mandate refused was logged as "do nothing" with its codes, never sent.
    sent = {e.decision_id for e in demo.events if e.type is EventType.SEND}
    for c in hero.checks:
        if c.codes:
            # Both goals may decide for a customer at the same moment: find this goal's decision.
            same = [d for d in demo.decisions if (d.customer_id, d.made_at) == (c.customer_id, c.made_at)]
            held = [d for d in same if (n := hero.note(d)) is not None and n.goal_id == c.goal_id]
            assert held and all(d.kind is DecisionKind.NOTHING for d in held)
            assert all(set(c.codes) <= set(d.blocked_by) and d.decision_id not in sent for d in held)


def test_briefs_act_inside_the_mandate_envelope(demo: DemoResult) -> None:
    mandate = _hero(demo).mandates.mandate
    for brief in demo.briefs:
        env = brief.envelope
        assert set(env.channels) <= set(mandate.channels)
        assert {o.offer_id for o in env.offers} <= {"none", *mandate.approved_offer_ids}
        assert env.max_contacts_per_week <= mandate.frequency_cap.max_touches
        assert env.budget <= mandate.budget_ceiling
        assert (env.quiet_start_hour, env.quiet_end_hour) == (21, 9)


def test_decisions_explain_themselves(demo: DemoResult) -> None:
    hero = _hero(demo)
    assert all(d.reason.strip() for d in demo.decisions)
    about = [d for d in demo.decisions if d.action is not None and d.action.item_id is not None]
    assert about
    for d in about:
        assert d.action is not None
        name = hero.names[str(d.action.item_id)]
        assert name in d.reason
    refill_goal = _refill_goal(demo)
    assert all(
        "refill" in d.reason for d in about if d.action is not None and d.action.goal_id == refill_goal
    )
    # Options are logged with each sent decision, so the reason can be checked against them.
    assert all(d.bids for d in about if d.group is Group.AGENT)


def test_the_walk_through_tells_one_customer_story(demo: DemoResult) -> None:
    story = hero_story(demo)
    assert "Refill pick" in story and "The decision" in story and "What happened next" in story
    assert "| do nothing |" in story and "mandate check: passed" in story
    picked = story.split("`")[1]
    assert any(d.customer_id == picked for d in demo.decisions)


def test_states_keep_their_refill_scores_for_whoever_serves_the_run(demo: DemoResult) -> None:
    scored = [s for s in demo.stores.states.scan() if "refill_due_days" in s.scores]
    assert scored and all(s.scores["refill_p14"] >= 0 for s in scored)


def test_a_goal_added_later_brings_a_new_mandate_version() -> None:
    result = run_demo(days=3, customers=60, seed=3, approve_goal_days=1)
    setup = _hero(result).mandates
    v1, v2 = setup.versions
    assert (v1.version, v2.version) == (1, 2)
    late = next(g for g in result.goals if g.kind is GoalKind.REPEAT_PURCHASE)
    assert late.goal_id not in v1.goal_ids and late.goal_id in v2.goal_ids
    assert setup.at(late.starts_at - timedelta(seconds=1)) == v1 and setup.at(late.starts_at) == v2
    kinds = [e.kind for e in setup.ledger.entries()]
    assert kinds.count("activated") == 2 and "superseded" in kinds
    assert setup.ledger.verify_chain(keys=setup.keys).ok
    assert all(not c.codes for c in _hero(result).checks if c.group == Group.AGENT.value)


def test_explain_keeps_every_option_the_agent_weighed() -> None:
    result = run_demo(days=2, customers=150, seed=1, explain=True)
    hero = _hero(result)
    goal = _refill_goal(result)
    acts = [
        d
        for d in result.decisions
        if d.action is not None and d.action.goal_id == goal and d.group is Group.AGENT
    ]
    assert acts
    for d in acts:
        note = hero.note(d)
        assert note is not None and note.options
        assert d.action is not None
        chosen = [o for o in note.options if o.content_id == d.action.content_id]
        assert chosen and chosen[0].blocked_by is None
        # Options the mandate ruled out for her were never offered to the auction.
        assert {o.offer_id for o in note.options} <= set(note.allowed_offers)
    assert "| do nothing | | | | $0.00 | $0.00 | the bar |" in hero_story(result)


def test_an_action_the_mandate_refuses_is_held_as_do_nothing() -> None:
    # The runs above hold nothing from the agent, so the refusal path is driven directly here.
    result = run_demo(days=2, customers=60, seed=2)
    hero = _hero(result)
    act = next(d for d in result.decisions if d.action is not None and d.group is Group.AGENT)
    assert act.action is not None
    state = result.stores.states.get(act.customer_id)
    assert state is not None
    ctx = DecisionContext(now=act.made_at, briefs=tuple(result.briefs), content=tuple(result.content))
    unlisted = act.action.model_copy(update={"content_id": "not_an_approved_template"})
    before = len(hero.checks)
    out = hero.finish(
        act.model_copy(update={"action": unlisted}),
        state,
        ctx,
        unlisted.goal_id,
        frozenset({"none"}),
        fills_items=True,
    )
    assert out.kind is DecisionKind.NOTHING and out.action is None
    assert "mandate_template_not_approved" in out.blocked_by and "signed mandate" in out.reason
    assert len(hero.checks) == before + 1 and "mandate_template_not_approved" in hero.checks[-1].codes
    note = hero.notes[(unlisted.goal_id, act.customer_id, act.made_at)]
    assert note.held and note.item_id is None
