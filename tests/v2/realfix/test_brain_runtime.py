"""History reaches the agent, daily preparation runs, and the production baseline group is contacted
(failures 2 and 9), plus the consent explanation, the shop's own VIP bar and the contact-harm start
(failures 7, 8, 9). Inline fixtures, a few seconds in all."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.contracts import (
    AutonomyLevel,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    GoalStatus,
    Group,
    Reward,
)
from autonomy.data.folder import SegmentRules, StateFolder
from autonomy.decide import rules
from autonomy.decide.bidders import contact_harm_of
from autonomy.fakes import (
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    InMemoryWorkspace,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.learning import build_bundle, learn_contact_harm
from autonomy.runtime import service
from autonomy.runtime.agent import build_agent_policy
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig
from autonomy.runtime.settings import agent_policy_factory

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
SWEEP = T0.replace(hour=10)


def purchase(cid: str, at: datetime, value: float = 40.0, n: int = 0) -> Event:
    return Event(
        event_id=f"buy_{cid}_{n}", customer_id=cid, type=EventType.PURCHASE, ts=at, source="t", value=value
    )


def consent(cid: str, at: datetime) -> Event:
    return Event(
        event_id=f"consent_{cid}",
        customer_id=cid,
        type=EventType.CONSENT_GRANTED,
        ts=at,
        source="t",
        channel=Channel.EMAIL,
    )


@dataclass
class Recorder:
    """A policy that records what the runtime hands it."""

    version: str = "recorder-v1"
    needs_history: bool = True
    calls: list[tuple[int, set[str], datetime | None]] = field(default_factory=list)
    prepared: list[tuple[datetime, int]] = field(default_factory=list)

    def nightly(
        self, decisions: Iterable[Decision], events: Mapping[str, Sequence[Event]], now: datetime
    ) -> list[str]:
        evs = [e for v in events.values() for e in v]
        self.calls.append((len(list(decisions)), set(events), min((e.ts for e in evs), default=None)))
        return []

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        self.prepared.append((ctx.now, len(states)))

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        self.needs_history = False
        return Decision(
            decision_id=f"d_{state.customer_id}_{ctx.now.isoformat()}",
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.NOTHING,
            group=state.group,
            probability=1.0,
            reason="recorder",
            policy_version=self.version,
        )

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:
        return None


def runtime_with(policy: Recorder, events: list[Event], *, batch: int) -> AutonomyRuntime:
    store = InMemoryEventStore()
    store.append(events)
    workspace = InMemoryWorkspace()
    start = T0 - timedelta(days=1)
    workspace.put_goal(
        sample_goal(start).model_copy(update={"autonomy": AutonomyLevel.SHADOW, "status": GoalStatus.ACTIVE})
    )
    workspace.put_brief(sample_brief(start))
    workspace.put_content(sample_content(start))
    return AutonomyRuntime(
        events=store,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: policy,
        config=RuntimeConfig(history_batch=batch),
    )


def test_a_new_policy_gets_every_customers_whole_history_in_slices_and_prepares_daily() -> None:
    customers = [f"c{i}" for i in range(7)]
    events: list[Event] = []
    for i, cid in enumerate(customers):
        events.append(consent(cid, T0 - timedelta(days=700)))
        events.append(purchase(cid, T0 - timedelta(days=400 + i)))  # far older than 30 days
    policy = Recorder()
    rt = runtime_with(policy, events, batch=3)
    report = rt.tick(SWEEP)
    history_calls = [c for c in policy.calls if c[0] == 0 and c[1]]
    assert [len(c[1]) for c in history_calls[:3]] == [3, 3, 1]  # slices of `history_batch` customers
    fed = set().union(*(c[1] for c in history_calls[:3]))
    assert fed == set(customers)  # never decided on, and still handed over
    assert min(c[2] for c in history_calls[:3] if c[2] is not None) <= T0 - timedelta(days=400)
    assert report.history_fed_goals and report.prepared_goals
    assert policy.prepared and policy.prepared[0][1] == len(customers)
    # once a day: a second tick the same day does not prepare again, the next day does
    rt.tick(SWEEP + timedelta(hours=1))
    assert len(policy.prepared) == 1
    rt.tick(SWEEP + timedelta(days=1))
    assert len(policy.prepared) == 2
    # the policy has decided: a later rebuild of the same object is not fed the history again
    n_calls = len(policy.calls)
    rt._feed_history([], SWEEP + timedelta(days=1), report)
    assert len(policy.calls) == n_calls


def test_the_agent_streams_history_and_warm_starts_from_it() -> None:
    """The real agent: history handed in two slices is the same as in one, and is then dropped."""
    events_by: dict[str, list[Event]] = {}
    for i in range(40):
        cid = f"c{i}"
        events_by[cid] = [consent(cid, T0 - timedelta(days=300)), purchase(cid, T0 - timedelta(days=100 + i))]
    brief = sample_brief(T0 - timedelta(days=1))
    one = build_agent_policy([brief], sample_content(T0))
    two = build_agent_policy([brief], sample_content(T0))
    assert one.needs_history
    one.nightly([], events_by, T0)
    keys = sorted(events_by)
    two.nightly([], {k: events_by[k] for k in keys[20:]}, T0)
    two.nightly([], {k: events_by[k] for k in keys[:20]}, T0)
    assert one.segmenter.data_start == two.segmenter.data_start == T0 - timedelta(days=139)
    assert one.digest.data_start == two.digest.data_start
    ctx = DecisionContext(now=T0, briefs=(brief,), content=tuple(sample_content(T0)), seed=1)
    one.prepare_day([], ctx)
    assert one.digest.purchases == {}  # the digest is freed after the warm start
    assert not two.needs_history or two.digest.purchases  # the other has not decided or prepared yet


def test_the_production_baseline_group_is_contacted_by_the_simple_rule() -> None:
    brief = sample_brief(T0 - timedelta(days=1))
    policy = agent_policy_factory([brief], sample_content(T0))
    lapsed = CustomerState(
        customer_id="b1",
        as_of=SWEEP,
        group=Group.BASELINE,
        purchases=2,
        revenue=80.0,
        last_purchase_at=SWEEP - timedelta(days=90),
        first_seen_at=SWEEP - timedelta(days=400),
        channels={Channel.EMAIL: ChannelStats(consent=True, address_known=True)},
    )
    ctx = DecisionContext(now=SWEEP, briefs=(brief,), content=tuple(sample_content(T0)), seed=1)
    d = policy.decide(lapsed, ctx)
    assert d.kind is DecisionKind.ACT and d.group is Group.BASELINE
    assert "no baseline rule" not in d.reason


def test_missing_consent_is_named_in_blocked_by() -> None:
    brief = sample_brief(T0 - timedelta(days=1))
    policy = build_agent_policy([brief], sample_content(T0))
    nobody = CustomerState(
        customer_id="n1",
        as_of=SWEEP,
        group=Group.AGENT,
        purchases=1,
        revenue=40.0,
        last_purchase_at=SWEEP - timedelta(days=50),
        first_seen_at=SWEEP - timedelta(days=200),
        channels={Channel.EMAIL: ChannelStats(consent=False, address_known=True)},
    )
    ctx = DecisionContext(now=SWEEP, briefs=(brief,), content=tuple(sample_content(T0)), seed=1)
    d = policy.decide(nobody, ctx)
    assert d.kind is DecisionKind.NOTHING
    assert rules.NO_CONSENT in d.blocked_by
    assert "anything to offer" not in d.reason


def test_the_vip_bar_follows_the_shops_own_buyers_and_currency() -> None:
    folder = StateFolder()
    rouble_shop = [
        CustomerState(
            customer_id=f"r{i}",
            as_of=SWEEP,
            purchases=20 + i % 10,
            revenue=8_000.0 + 50.0 * i,  # every client spends far more than 500 roubles
            last_purchase_at=SWEEP - timedelta(days=3),
            first_seen_at=SWEEP - timedelta(days=300),
        )
        for i in range(200)
    ]
    fixed = sum(SegmentRules().label(s, SWEEP) == "vip" for s in rouble_shop)
    assert fixed == 200  # a flat 500 makes everyone VIP
    rules_now = folder.fit_segments(rouble_shop)
    vip = sum(folder.refresh(s, SWEEP).segment == "vip" for s in rouble_shop)
    assert 0.05 * 200 <= vip <= 0.15 * 200
    assert rules_now.vip_min_revenue > 500.0
    # too few buyers to say: the defaults stay
    assert folder.fit_segments(rouble_shop[:10]) == SegmentRules()
    # bars someone set by hand are theirs: not refitted
    chosen = SegmentRules(vip_min_revenue=12_000.0)
    assert StateFolder(segments=chosen).fit_segments(rouble_shop) == chosen


def test_contact_harm_records_never_start_after_the_agents_start() -> None:
    """A win-back goal: its customers bought nothing in the window before the start. The first pass
    sees no purchase at all; the next sees one a minute after the start. The evidence must still
    start no later than the start, so the start's messages count."""
    start = SWEEP
    bundle = build_bundle()
    sends = {
        f"w{i}": [
            Event(
                event_id=f"s_w{i}",
                customer_id=f"w{i}",
                type=EventType.SEND,
                ts=start,
                source="t",
                channel=Channel.EMAIL,
                decision_id=f"d_w{i}",
            )
        ]
        for i in range(5)
    }
    learn_contact_harm(bundle, sends, {}, start + timedelta(hours=1), 7, split_at=start)
    later = {k: [*v, purchase(k, start + timedelta(minutes=1))] for k, v in sends.items()}
    learn_contact_harm(bundle, later, {}, start + timedelta(days=1), 7, split_at=start)
    harm = contact_harm_of(bundle.response)
    assert harm is not None and harm.data_start is not None and harm.data_start <= start
    # the full history's start, when given, wins over a later first purchase in the slice
    fresh = build_bundle()
    early = start - timedelta(days=300)
    learn_contact_harm(fresh, later, {}, start + timedelta(days=1), 7, split_at=start, data_start=early)
    found = contact_harm_of(fresh.response)
    assert found is not None and found.data_start == early


def test_a_policy_rebuilt_the_same_day_is_prepared_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime remembers which policy object it prepared, not its `id()`: a rebuilt policy can
    be allocated where a freed one was, and must still get its first day's `prepare_day`."""
    first, second = Recorder(), Recorder()
    rt = runtime_with(first, [consent("c1", T0 - timedelta(days=10))], batch=10)
    report = rt.tick(SWEEP)
    assert len(first.prepared) == 1
    goal_id, (prepared, day) = next(iter(rt._prepared.items()))
    assert prepared is first
    run = next(r for r in rt._running(SWEEP, report) if r.goal.goal_id == goal_id)
    assert not rt._prepare_due(run, day)
    rebuilt = replace(run, policy=second)
    # every object at the same address, as when the new policy reuses the freed one's memory
    monkeypatch.setattr(service, "id", lambda _obj: 1, raising=False)
    assert rt._prepare_due(rebuilt, day)
