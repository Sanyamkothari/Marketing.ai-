"""The runtime's and the gateway's failure fallbacks, beyond the rows of the failure-isolation table.

`tests/v2/failure/test_failure_isolation.py` checks each row's headline promise. These check the
edges of the same fixes: what is cached and when it is dropped, what an outage on a cold start
does, and that each alert is raised once a day rather than every tick. The fakes and the world
builder are the failure tests' own, so both files fail the same engines the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

import pytest

from autonomy.channels.breakers import BreakerResult
from autonomy.channels.dispatcher import DispatchStatus
from autonomy.channels.gateway import TelemetryEvent
from autonomy.contracts import (
    AutonomyLevel,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Reward,
)
from autonomy.data.rebuild import rebuild_states
from autonomy.fakes import ContactEveryonePolicy, InMemoryEventStore, SimpleStateFolder
from autonomy.interfaces import EventStore, Policy
from autonomy.mandate import MandateStatus
from autonomy.runtime.service import LEFT_ALONE, AutonomyRuntime, RuntimeConfig
from tests.v2.failure.test_failure_isolation import (
    CUSTOMERS,
    DAY,
    GOAL,
    SWEEP,
    T0,
    Carrier,
    FlakyFeed,
    FlakyMandates,
    RaisingPolicy,
    _expand_budget,
    _mandate,
    build,
    gateway_world,
    history,
)


class BudgetSpy:
    """Contacts everyone, and records the budget the runtime handed it at each tick."""

    version = "spy-v0"

    def __init__(self) -> None:
        self.seen: dict[datetime, float] = {}
        self.inner = ContactEveryonePolicy()

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        self.seen.setdefault(ctx.now, ctx.budget_left.get(GOAL, float("inf")))
        return self.inner.decide(state, ctx)

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


def _flaky() -> tuple[dict[str, FlakyFeed], Callable[[InMemoryEventStore], EventStore]]:
    feed: dict[str, FlakyFeed] = {}

    def wrap(log: InMemoryEventStore) -> EventStore:
        feed["store"] = FlakyFeed(log)
        return feed["store"]

    return feed, wrap


# --- P1 ingest -----------------------------------------------------------------------------------


def test_feed_down_alert_is_raised_once_a_day_and_the_missed_events_fold_when_it_is_back() -> None:
    feed, wrap = _flaky()
    w = build(autonomy=AutonomyLevel.SHADOW, wrap=wrap)
    w.runtime.tick(T0)
    feed["store"].down = True
    first = w.runtime.tick(SWEEP)
    again = w.runtime.tick(SWEEP + timedelta(minutes=5))
    assert [a.code for a in first.alerts if a.code.startswith("ingest.")] == ["ingest.unavailable"]
    assert not any(a.code.startswith("ingest.") for a in again.alerts)  # told once, not every tick

    # An event appended while the feed was down is folded once it is back (the watermark held).
    w.log.append([Event(event_id="late_visit", customer_id="c1", type=EventType.VISIT, ts=SWEEP, source="t")])
    feed["store"].down = False
    back = w.runtime.tick(SWEEP + timedelta(minutes=10))
    assert back.events_folded == 1


def test_a_failed_nightly_rebuild_keeps_the_spend_the_runtime_knew() -> None:
    """A rebuild that fails half way must not leave an empty spend ledger (the budget would reset)."""
    feed, wrap = _flaky()
    spy = BudgetSpy()
    w = build(policy=spy, wrap=wrap, budget=10.0, channel_costs={Channel.EMAIL: 1.0})
    w.runtime.tick(SWEEP)
    assert spy.seen[SWEEP] == pytest.approx(10.0) and len(w.sends()) == len(CUSTOMERS)
    feed["store"].down = True
    day2 = SWEEP + DAY  # the nightly rebuild is due, and fails
    report = w.runtime.tick(day2)
    assert any(a.code == "ingest.unavailable" for a in report.alerts)
    assert spy.seen[day2] == pytest.approx(10.0 - len(CUSTOMERS))


def test_a_cold_start_with_the_feed_down_spends_nothing() -> None:
    """No ledger was ever built from the log, so spend is unknown: the policy is handed no budget."""
    feed, wrap = _flaky()
    spy = BudgetSpy()
    w = build(policy=spy, wrap=wrap)
    rebuild_states(w.log, w.states, SimpleStateFolder(), T0)  # what an earlier process left
    feed["store"].down = True
    report = w.runtime.tick(SWEEP)
    assert {a.code for a in report.alerts} >= {"ingest.unavailable", "spend.unavailable"}
    assert spy.seen[SWEEP] == 0.0


def test_an_old_history_is_not_a_stale_feed_until_the_runtime_has_watched_it_stay_quiet() -> None:
    w = build()  # every customer event is 90 days or more old
    first = w.runtime.tick(SWEEP)
    assert not any(a.code == "ingest.stale" for a in first.alerts)
    later = w.runtime.tick(SWEEP + 3 * DAY)
    assert [a.code for a in later.alerts].count("ingest.stale") == 1
    assert not any(
        a.code == "ingest.stale" for a in w.runtime.tick(SWEEP + 3 * DAY + timedelta(hours=1)).alerts
    )


def test_the_staleness_check_can_be_turned_off_and_a_live_feed_never_trips_it() -> None:
    off = build(seed_events=history(seen_at=T0), config=RuntimeConfig(ingest_stale_after=None))
    off.runtime.tick(T0)
    assert not any(a.code == "ingest.stale" for a in off.runtime.tick(SWEEP + 5 * DAY).alerts)

    live = build(seed_events=history(seen_at=T0))
    live.runtime.tick(T0)
    fresh = Event(event_id="fresh", customer_id="c1", type=EventType.VISIT, ts=SWEEP + 2 * DAY, source="t")
    live.log.append([fresh])
    assert not any(a.code == "ingest.stale" for a in live.runtime.tick(SWEEP + 3 * DAY).alerts)
    with pytest.raises(ValueError):
        RuntimeConfig(ingest_stale_after=timedelta(0))


def test_breakers_that_cannot_read_are_an_alert_and_the_sweep_still_sends() -> None:
    def breakers(briefs: Sequence[Brief], recent: Sequence[Event], now: datetime) -> BreakerResult:
        raise ConnectionError("the spend ledger is unreachable")

    w = build(breakers_fn=breakers)
    report = w.runtime.tick(SWEEP)
    assert any(a.code == "breakers.error" for a in report.alerts)
    assert len(report.sent) == len(CUSTOMERS)


# --- P2 mandate ----------------------------------------------------------------------------------


def test_a_mandate_the_store_revoked_is_never_held_over_an_outage() -> None:
    """Only a store that cannot answer lets the last good mandate stand in; a 'revoked' clears it."""
    store = FlakyMandates(_mandate(3.0, until=SWEEP + 5 * DAY))
    w = build(channel_costs={Channel.EMAIL: 1.0}, mandates=store)
    w.runtime.tick(SWEEP)
    store.mandates = [m.model_copy(update={"status": MandateStatus.REVOKED}) for m in store.mandates]
    w.runtime.tick(SWEEP + DAY)  # the store answers: revoked
    store.down = True
    report = w.runtime.tick(SWEEP + 2 * DAY)
    assert GOAL in report.halted_goals
    assert any(a.code == "mandate.error" for a in report.alerts)


def test_the_send_gate_uses_the_held_mandate_and_raises_without_one() -> None:
    store = FlakyMandates(_mandate(3.0, until=SWEEP + 5 * DAY))
    w = build(channel_costs={Channel.EMAIL: 1.0}, mandates=store)
    w.runtime.tick(SWEEP)
    sent = next(d for d in w.decisions.scan() if d.kind is DecisionKind.ACT)
    store.down = True
    assert w.runtime.send_gate(sent, SWEEP + DAY) is not None  # the held ceiling (3 spent of 3) binds
    with pytest.raises(ConnectionError):
        w.runtime.send_gate(sent, SWEEP + 6 * DAY)  # expired: nothing stands in, the job retries


# --- P3 decision hub -----------------------------------------------------------------------------


def test_a_left_alone_decision_names_the_failure_and_a_policy_that_cannot_be_built_alerts_once_a_day() -> (
    None
):
    w = build(policy=RaisingPolicy(only=("c2",)))
    report = w.runtime.tick(SWEEP)
    [left] = [d for d in report.decisions if d.customer_id == "c2"]
    assert left.kind is DecisionKind.NOTHING and left.reason == LEFT_ALONE and left.probability == 1.0

    calls: list[int] = []

    def factory(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        calls.append(1)
        raise RuntimeError("the model bundle is corrupt")

    broken = build(policy_factory=factory)
    first = broken.runtime.tick(SWEEP)
    again = broken.runtime.tick(SWEEP + timedelta(minutes=5))
    assert len(calls) >= 2  # tried again each tick (and by the nightly run)
    assert [a.code for a in first.alerts if a.goal_id == GOAL] == ["decide.error"]
    assert not any(a.goal_id == GOAL for a in again.alerts)
    assert first.decisions == [] and broken.sends() == []


# --- P6 spend and telemetry ------------------------------------------------------------------------


def test_spend_down_subtracts_our_own_sends_since_the_last_good_read_and_never_honours_a_raise() -> None:
    feed = {"down": False}

    def spend(goal_id: str, now: datetime) -> float:
        if feed["down"]:
            raise ConnectionError("the spend ledger is unreachable")
        return 0.0

    spy = BudgetSpy()
    w = build(policy=spy, budget=10.0, spend_fn=spend, channel_costs={Channel.EMAIL: 1.0})
    w.runtime.tick(SWEEP)
    assert spy.seen[SWEEP] == pytest.approx(10.0)
    feed["down"] = True
    _expand_budget(w, 50_000.0)  # the goal's and the brief's budget both go up while blind
    report = w.runtime.tick(SWEEP + DAY)
    assert spy.seen[SWEEP + DAY] == pytest.approx(10.0 - len(CUSTOMERS))  # four sends at 1.0 since
    assert [a.code for a in report.alerts].count("spend.unavailable") == 1


def test_spend_down_counts_a_send_at_this_very_moment_and_one_that_names_only_its_decision() -> None:
    """A queued send that ran earlier in this tick lands at `now`, and a gateway SEND carries only
    its decision id: both are this goal's spend and come off the budget."""
    feed = {"down": False}

    def spend(goal_id: str, now: datetime) -> float:
        if feed["down"]:
            raise ConnectionError("the spend ledger is unreachable")
        return 0.0

    spy = BudgetSpy()
    w = build(policy=spy, budget=10.0, spend_fn=spend, channel_costs={Channel.EMAIL: 1.0})
    w.runtime.tick(SWEEP)
    feed["down"] = True
    day2 = SWEEP + DAY
    acted = next(d for d in w.decisions.scan() if d.kind is DecisionKind.ACT)
    w.log.append(
        [
            Event(
                event_id="gateway_send_now",
                customer_id=acted.customer_id,
                type=EventType.SEND,
                ts=day2,
                source="gateway",
                channel=Channel.EMAIL,
                decision_id=acted.decision_id,
                attrs={"message_id": "m_now"},
            )
        ]
    )
    w.runtime.tick(day2)
    assert spy.seen[day2] == pytest.approx(10.0 - len(CUSTOMERS) - 1.0)


def test_a_cold_start_with_the_feed_down_keeps_no_spend_pacer() -> None:
    """A pacer built while the ledger was never filled would hand out a whole fresh burst later."""
    feed, wrap = _flaky()
    paced = _mandate(100.0, until=SWEEP + 5 * DAY).model_copy(update={"max_daily_spend": 4.0})
    w = build(wrap=wrap, channel_costs={Channel.EMAIL: 1.0}, mandates=FlakyMandates(paced))
    rebuild_states(w.log, w.states, SimpleStateFolder(), T0)
    feed["store"].down = True
    w.runtime.tick(SWEEP)
    assert w.sends() == [] and w.runtime._buckets == {}
    feed["store"].down = False
    w.runtime.tick(SWEEP + DAY)  # the next sweep, with the ledger built from the log
    assert w.runtime._buckets and w.sends()


def test_a_worker_that_has_not_ticked_gates_a_queued_send_on_the_spend_in_the_log() -> None:
    """The gate in a fresh worker reads the log into its ledger, rather than refusing for good."""
    feed, wrap = _flaky()
    store = FlakyMandates(_mandate(100.0, until=SWEEP + 5 * DAY))
    w = build(wrap=wrap, channel_costs={Channel.EMAIL: 1.0}, mandates=store)
    w.runtime.tick(SWEEP)
    acted = next(d for d in w.decisions.scan() if d.kind is DecisionKind.ACT)

    def fresh_worker(ceiling: float) -> AutonomyRuntime:
        return AutonomyRuntime(
            events=w.events,
            states=w.states,
            decisions=w.decisions,
            workspace=w.workspace,
            folder=SimpleStateFolder(),
            policy_factory=lambda briefs, content: ContactEveryonePolicy(),
            channel_costs={Channel.EMAIL: 1.0},
            mandates=FlakyMandates(_mandate(ceiling, until=SWEEP + 5 * DAY)),
        )

    later = SWEEP + timedelta(hours=1)
    assert fresh_worker(100.0).send_gate(acted, later) is None  # 4 of 100 spent: it may go
    assert fresh_worker(len(CUSTOMERS)).send_gate(acted, later) == "mandate_budget_ceiling"
    feed["store"].down = True
    with pytest.raises(ConnectionError):  # the log cannot be read: the job retries, nothing is sent
        fresh_worker(100.0).send_gate(acted, later)


def test_a_failing_telemetry_sink_is_counted_and_the_event_kept() -> None:
    def sink(event: TelemetryEvent) -> None:
        raise ConnectionError("the telemetry collector is unreachable")

    now = SWEEP
    carrier = Carrier()
    carrier.up = True
    g = gateway_world(carrier, at=now, telemetry=sink)
    [result] = g.gateway.dispatch([g.decision], {"c1": g.state}, now).results
    assert result.status is DispatchStatus.SENT
    assert g.gateway.telemetry_errors == len(g.gateway.telemetry_log) > 0
