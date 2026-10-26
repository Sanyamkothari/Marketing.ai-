"""A small synthetic business to run the daily sweep on, and a fingerprint of everything a run did.

Three goals at the three autonomy levels (shadow, approve, autopilot), customers who differ in
consent, history and cart activity, a policy that sometimes passes, sometimes raises, and a mandate
with a daily cap on the autopilot goal. The same scenario runs with any `sweep_batch`; the
fingerprint covers the tick reports, the decision log, the sends, the held batches, the alerts, the
stored states and what the policy was handed, so two runs that match here made the same decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from autonomy.channels.dispatcher import Dispatcher
from autonomy.contracts import (
    AudienceFilter,
    AutonomyLevel,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    GoalStatus,
    Group,
)
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeChannelAdapter,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
    InMemoryWorkspace,
    NothingPolicy,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.mandate import Mandate, MandateStatus
from autonomy.runtime.service import AutonomyRuntime, MandateBook, RuntimeConfig, TickReport
from autonomy.store import SqlDecisionLog, SqlStateStore, create_all, memory_engine

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a Monday, before the 10:00 sweep
SWEEP = T0.replace(hour=10)
START = T0 - timedelta(days=1)

SHADOW_GOAL = "goal_shadow"
APPROVE_GOAL = "goal_approve"
AUTO_GOAL = "goal_auto"

UNSET: Any = object()
"""`sweep_batch` left out of the config (the code from before the setting existed)."""


def _bucket(cid: str) -> int:
    return int(hashlib.sha256(cid.encode()).hexdigest()[:8], 16) % 10


class MixedPolicy(ContactEveryonePolicy):
    """Contacts most customers, passes on some, raises for a few; remembers `prepare_day`'s input."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.version = f"mixed-{name}-v1"
        self.prepared: list[dict[str, Any]] = []
        self.decided = 0

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        ids = [s.customer_id for s in states]
        self.prepared.append(
            {
                "n": len(ids),
                "first": ids[:1],
                "last": ids[-1:],
                "sorted": ids == sorted(ids),
                "purchases": sum(s.purchases for s in states),
                "again": [s.customer_id for s in states][:3],
            }
        )

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        self.decided += 1
        b = _bucket(state.customer_id)
        if b == 0:
            raise RuntimeError("the policy fell over for this customer")
        if b in (1, 2):
            return NothingPolicy().decide(state, ctx)
        decision = super().decide(state, ctx)
        if decision.kind is DecisionKind.ACT:
            return decision.model_copy(update={"expected_net": round(0.5 + b / 10, 2)})
        return decision


@dataclass
class Scenario:
    runtime: AutonomyRuntime
    events: InMemoryEventStore
    workspace: InMemoryWorkspace
    decisions: Any
    states: Any
    adapter: FakeChannelAdapter
    policies: dict[str, MixedPolicy]
    enriched_calls: list[int] = field(default_factory=list)


def customers(n: int) -> list[str]:
    return [f"c{i:06d}" for i in range(n)]


def history(n: int) -> list[Event]:
    """Consent and a last purchase 20 to 140 days before T0; some with a cart in the day before the sweep."""
    out: list[Event] = []
    for i, cid in enumerate(customers(n)):
        if i % 7 != 3:
            out.append(
                Event(
                    event_id=f"consent_{cid}",
                    customer_id=cid,
                    type=EventType.CONSENT_GRANTED,
                    ts=T0 - timedelta(days=200),
                    source="test",
                    channel=Channel.EMAIL,
                )
            )
        if i % 5 == 0:
            out.append(
                Event(
                    event_id=f"consent_wa_{cid}",
                    customer_id=cid,
                    type=EventType.CONSENT_GRANTED,
                    ts=T0 - timedelta(days=200),
                    source="test",
                    channel=Channel.WHATSAPP,
                )
            )
        if i % 11 != 4:
            # Some purchases fall just short of 60 days at the sweep and cross it a few hours later.
            ago = timedelta(days=20 + (i * 13) % 120)
            if i % 17 == 0:
                ago = timedelta(days=60) - timedelta(hours=1 + i % 3)
            out.append(
                Event(
                    event_id=f"buy_{cid}",
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=T0 - ago,
                    source="test",
                    value=20.0 + (i % 9) * 5,
                    category="shoes" if i % 2 else "bags",
                )
            )
        if i % 6 == 1:
            out.append(
                Event(
                    event_id=f"cart_{cid}",
                    customer_id=cid,
                    type=EventType.CART,
                    ts=T0 + timedelta(hours=1, minutes=i % 50),
                    source="test",
                    item_id="sku1",
                )
            )
    return out


def mandate(kind: str) -> Mandate:
    """`paced`: a small daily cap, so the pacer holds some back. `violation`: some customers excluded."""
    base: dict[str, Any] = {
        "mandate_id": f"m_{kind}",
        "business_id": "demo",
        "goal_ids": (AUTO_GOAL,),
        "budget_ceiling": 100000.0,
        "channels": (Channel.EMAIL, Channel.WHATSAPP),
        "approved_template_ids": tuple(c.content_id for c in sample_content(START, AUTO_GOAL)),
        "valid_from": T0 - timedelta(days=10),
        "valid_until": T0 + timedelta(days=30),
        "status": MandateStatus.ACTIVE,
    }
    if kind == "paced":
        base["max_daily_spend"] = 0.06
    if kind == "violation":
        base["cohort_exclusions"] = (AudienceFilter(field="purchases", op="gte", value=1),)
    return Mandate.model_validate(base)


def assign_group(state: CustomerState) -> CustomerState:
    """A tenth of the customers are a holdout, set when states are (re)built."""
    group = Group.HOLDOUT if _bucket(state.customer_id) == 3 else Group.AGENT
    return state if state.group is group else state.model_copy(update={"group": group})


def build(
    n: int,
    *,
    sweep_batch: Any = UNSET,
    one_contact: bool = True,
    mandate_kind: str = "none",
    enrich: bool = False,
    groups: bool = False,
    sql: bool = False,
    report_decisions: Any = UNSET,
    seed_events: Sequence[Event] | None = None,
) -> Scenario:
    events = InMemoryEventStore()
    events.append(history(n) if seed_events is None else seed_events)
    workspace = InMemoryWorkspace()
    levels = {
        SHADOW_GOAL: (AutonomyLevel.SHADOW, 60),
        APPROVE_GOAL: (AutonomyLevel.APPROVE, 90),
        AUTO_GOAL: (AutonomyLevel.AUTOPILOT, 45),
    }
    for gid, (level, days) in levels.items():
        workspace.put_goal(
            sample_goal(START, gid).model_copy(update={"autonomy": level, "status": GoalStatus.ACTIVE})
        )
        brief = sample_brief(START, gid)
        audience = brief.audience.model_copy(
            update={
                "filters": (
                    AudienceFilter(field="purchases", op="gte", value=1),
                    AudienceFilter(field="last_purchase_at", op="days_since_gt", value=days),
                )
            }
        )
        workspace.put_brief(brief.model_copy(update={"audience": audience}))
        workspace.put_content(sample_content(START, gid))
    decisions: Any = InMemoryDecisionLog()
    states: Any = InMemoryStateStore()
    if sql:
        engine = memory_engine()
        create_all(engine)
        decisions, states = SqlDecisionLog(engine, batch_size=10), SqlStateStore(engine, batch_size=10)
    adapter = FakeChannelAdapter(Channel.EMAIL)
    queue = InMemoryJobQueue()
    dispatcher = Dispatcher(
        {Channel.EMAIL: adapter}, lambda cid, ch: f"{cid}@example.com", workspace, events, queue
    )
    policies: dict[str, MixedPolicy] = {}

    def factory(briefs: Sequence[Any], content: Sequence[Any]) -> MixedPolicy:
        gid = briefs[0].goal_id
        return policies.setdefault(gid, MixedPolicy(gid))

    scenario_calls: list[int] = []

    def enricher(
        batch: list[CustomerState], events_by: Mapping[str, list[Event]], now: datetime
    ) -> list[CustomerState]:
        scenario_calls.append(len(batch))
        return [
            s.model_copy(update={"scores": {**s.scores, "enriched": float(_bucket(s.customer_id))}})
            for s in batch
        ]

    config_kw: dict[str, Any] = {"one_contact_per_tick": one_contact}
    if sweep_batch is not UNSET:
        config_kw["sweep_batch"] = sweep_batch
    if report_decisions is not UNSET:
        config_kw["report_decisions"] = report_decisions
    extra: dict[str, Any] = {}
    if mandate_kind != "none":
        extra["mandates"] = MandateBook([mandate(mandate_kind)])
    if enrich:
        extra["state_enricher"] = enricher
    if groups:
        extra["assign_group"] = assign_group
    runtime = AutonomyRuntime(
        events=events,
        states=states,
        decisions=decisions,
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=factory,
        queue=queue,
        dispatcher=dispatcher,
        config=RuntimeConfig(**config_kw),
        **extra,
    )
    return Scenario(runtime, events, workspace, decisions, states, adapter, policies, scenario_calls)


def play(s: Scenario) -> list[TickReport]:
    """Four ticks: the sweep, a repeat, the lapse check three hours on, the next morning (the held batch approved)."""
    reports = [s.runtime.tick(SWEEP)]
    reports.append(s.runtime.tick(SWEEP + timedelta(minutes=5)))
    reports.append(s.runtime.tick(SWEEP + timedelta(hours=3)))
    for batch in s.runtime.list_batches():
        s.runtime.approve_batch(batch.batch_id, by="founder", now=SWEEP + timedelta(hours=4))
    reports.append(s.runtime.tick(SWEEP + timedelta(days=1)))
    return reports


def _dump(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(obj, default=str, sort_keys=True))
    return out


def fingerprint(s: Scenario, reports: Sequence[TickReport]) -> dict[str, Any]:
    """Everything the run did, as plain data, to compare two runs."""
    return _dump(
        {
            "summaries": [r.summary() for r in reports],
            "report_decisions": [[d.model_dump(mode="json") for d in r.decisions] for r in reports],
            "report_dispatch": [
                [(x.decision_id, x.customer_id, x.status.value, x.reason) for x in r.dispatch.results]
                for r in reports
            ],
            "report_mandate_blocked": [list(r.mandate_blocked) for r in reports],
            "report_lists": [
                {
                    "swept": r.swept_goals,
                    "prepared": r.prepared_goals,
                    "halted": r.halted_goals,
                    "folded": r.events_folded,
                    "batches_sent": [b.batch_id for b in r.batches_sent],
                }
                for r in reports
            ],
            "log": [
                d.model_dump(mode="json") for d in sorted(s.decisions.scan(), key=lambda d: d.decision_id)
            ],
            "events": [e.model_dump(mode="json") for e in sorted(s.events.scan(), key=lambda e: e.event_id)],
            "batches": [b.model_dump(mode="json") for b in s.runtime.list_batches()],
            "alerts": [
                a.model_dump(mode="json")
                for a in sorted(s.runtime.alert_store.list_alerts(), key=lambda a: a.alert_id)
            ],
            "states": [st.model_dump(mode="json") for st in s.states.scan()],
            "prepared": {gid: p.prepared for gid, p in sorted(s.policies.items())},
            "decided": {gid: p.decided for gid, p in sorted(s.policies.items())},
            "sent_messages": sorted(m.decision_id for m in s.adapter.sent),
            "cursor": s.runtime.cursor.model_dump(mode="json"),
        }
    )
