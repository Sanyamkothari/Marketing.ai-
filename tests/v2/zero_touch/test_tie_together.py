"""The zero-touch pieces joined: the worker runs the inbox, one voice approval gives a goal its messages,
and a proposal this deployment cannot reach is never the one preselected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from autonomy.agents.critic import BrandRules, check_rules
from autonomy.brand import SqlBrandKitStore, approve, save_read
from autonomy.brand.samples import goal_library
from autonomy.channels.breakers import DEFAULT_GUARDS, evaluate
from autonomy.contracts import Channel, ContentStatus, Event, EventType, StopMetric, StopRule
from autonomy.inbox.install import ZeroTouch
from autonomy.onboarding import scan_opportunities
from autonomy.runtime.service import worker_for_runtime
from autonomy.runtime.settings import _guards_for
from autonomy.runtime.worker import tick_job
from tests.v2.zero_touch.test_brand_kit import _client, read_kit, seeded_shop
from tests.v2.zero_touch.test_goals_limits import NOW, shopify_shop
from tests.v2.zero_touch.wiring_support import BUSINESS, GOAL, SWEEP, make_shop


def _zero_touch(runtime: Any) -> ZeroTouch | None:
    return next(
        (h.__self__ for h in runtime.before_tick if isinstance(getattr(h, "__self__", None), ZeroTouch)), None
    )


def test_the_worker_runs_the_inbox_guard_and_watchdog_on_its_own_ticks() -> None:
    shop = make_shop()
    worker = worker_for_runtime(shop.runtime, wiring=shop.wiring, db=shop.db)
    zt = _zero_touch(shop.runtime)
    assert zt is not None, "the worker must keep the inbox and the Autopilot guard current"
    assert shop.engine.monitor.watchdog is zt.watchdog
    shop.at(SWEEP)
    worker.queue.enqueue(tick_job(SWEEP))
    outcomes = worker.drain(SWEEP)
    assert outcomes and all(o.error is None for o in outcomes), outcomes
    assert not [a for a in shop.alerts.list_alerts() if a.code == "runtime.hook_error"]


def test_without_connectors_the_worker_installs_nothing_extra() -> None:
    shop = make_shop()
    before = list(shop.runtime.before_tick)
    worker_for_runtime(shop.runtime)
    assert shop.runtime.before_tick == before


def test_one_voice_approval_gives_a_running_goal_messages_that_pass_the_kit() -> None:
    shop = seeded_shop()
    ws = shop.runtime.workspace
    # The running goal has no approved message yet (as after the one-click accept).
    ws.put_content([c.model_copy(update={"status": ContentStatus.RETIRED}) for c in ws.content(goal_id=GOAL)])
    assert ws.content(goal_id=GOAL) == []
    client, brand = _client(shop)
    with client:
        page = client.post("/v2/onboarding/brand/read").json()
        done = client.post("/v2/onboarding/brand/approve", json={"fingerprint": page["fingerprint"]}).json()
    assert done["status"] == "approved"
    written = ws.content(goal_id=GOAL)
    assert written, done["notes"]
    kit = brand.store.get(BUSINESS)
    assert kit is not None
    brief = max(ws.briefs(GOAL), key=lambda b: b.version)
    rules = BrandRules.from_brief(brief, brand_voice="", assets=brand.assets.find(), kit=kit)
    offers = {o.offer_id for o in brief.envelope.offers}
    for item in written:
        assert item.status is ContentStatus.APPROVED
        assert check_rules(item, rules) == [], item
        assert item.offer_id in offers  # never an offer the signed envelope does not allow


def test_a_goal_that_has_approved_messages_keeps_them() -> None:
    shop = seeded_shop()
    ws = shop.runtime.workspace
    before = sorted(c.content_id for c in ws.content(goal_id=GOAL))
    client, _ = _client(shop)
    with client:
        page = client.post("/v2/onboarding/brand/read").json()
        client.post("/v2/onboarding/brand/approve", json={"fingerprint": page["fingerprint"]})
    assert sorted(c.content_id for c in ws.content(goal_id=GOAL)) == before


def test_goal_library_needs_the_approval_and_writes_in_the_voice() -> None:
    shop = seeded_shop()
    store = SqlBrandKitStore(shop.db)
    kit = save_read(store, read_kit(shop))
    brief = max(shop.runtime.workspace.briefs(GOAL), key=lambda b: b.version)
    try:
        goal_library(kit, brief, now=shop.clock.now())
    except ValueError:
        pass
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("an unapproved kit must not write a library")
    kit = approve(store, BUSINESS, approved_by="Dana", now=shop.clock.now(), fingerprint=kit.fingerprint())
    library = goal_library(kit, brief, now=shop.clock.now())
    assert library.approved > 0 and library.goal_id == GOAL
    greeting = kit.style.greeting
    if greeting:
        word = greeting.split()[0]
        assert any(word in (i.body or "") for i in library.items if i.status is ContentStatus.APPROVED)


def test_a_proposal_this_server_cannot_reach_is_shown_but_never_preselected() -> None:
    events, states = shopify_shop()
    free = scan_opportunities(events.scan(), states.scan(), NOW)
    blocked_id = free.preselected_id
    assert blocked_id is not None
    reason = "This server cannot reach that audience yet."
    held = scan_opportunities(events.scan(), states.scan(), NOW, unreachable={blocked_id: reason})
    assert held.preselected_id not in (None, blocked_id)
    assert any(p.opportunity_id == blocked_id and p.monthly_profit is not None for p in held.proposals)
    assert reason in held.notes


def _mail(n: int, kind: EventType, at: datetime, start: int = 0) -> list[Event]:
    return [
        Event(
            event_id=f"ev_{kind.value}_{i}",
            customer_id=f"c{i}",
            type=kind,
            ts=at + timedelta(minutes=1 if kind is EventType.DELIVER else 0),
            source="klaviyo" if kind is EventType.DELIVER else "dispatcher",
            channel=Channel.EMAIL,
            decision_id=f"dec_{i}",
        )
        for i in range(start, start + n)
    ]


def test_an_evening_batch_klaviyo_has_not_read_back_yet_is_not_a_delivery_failure() -> None:
    # The rehearsal's case: 121 earlier messages delivered, 25 handed to Klaviyo at 20:51 and checked at
    # 21:53, before the hourly engagement read brought their deliveries back.
    now = datetime(2026, 11, 21, 21, 53, tzinfo=UTC)
    events = _mail(121, EventType.SEND, now - timedelta(days=2)) + _mail(
        121, EventType.DELIVER, now - timedelta(days=2)
    )
    events += _mail(25, EventType.SEND, now - timedelta(minutes=62), start=121)
    rules = [StopRule(metric=StopMetric.DELIVERY_RATE_BELOW, threshold=0.95, action="pause_channel")]
    hasty = evaluate(rules, events, {}, 0, 0, now, goal_id=GOAL)
    assert hasty.paused_channels == {Channel.EMAIL}  # the old one-hour grace: a false safety stop
    shop = make_shop()
    patient = evaluate(rules, events, {}, 0, 0, now, goal_id=GOAL, guards=_guards_for(shop.wiring))
    assert patient.alerts == [] and patient.paused_channels == set()
    # Messages that really never arrive still trip it once Klaviyo's window has passed.
    late = now + timedelta(hours=6)
    assert evaluate(
        rules, events, {}, 0, 0, late, goal_id=GOAL, guards=_guards_for(shop.wiring)
    ).paused_channels
    assert _guards_for(None) == DEFAULT_GUARDS
