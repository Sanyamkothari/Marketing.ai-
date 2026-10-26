"""Family B, stage 3: one Shadow-mode day of the real runtime on the rebuilt customer states.

What a business would see on day one if it loaded its order history and switched the agent on in
Shadow mode (decide and log, never send). Per dataset:

1. The order-grain events from `b_adapters` go into an event store; the runtime's first tick
   rebuilds every state from it (production `StateFolder`).
2. The goals are the ones Autonomy itself suggests for this data (`b_ingest`'s two suggestion
   paths: the API's `suggest_opportunities` and the Analyst's `opportunities()`), each turned into a
   brief by the Strategist's deterministic template (`template_brief`, what it falls back to with
   no LLM) and approved, with the sample e-mail content library (`autonomy.fakes.samples`). Every goal
   is ACTIVE at autonomy level SHADOW.
3. The runtime is built the way `autonomy.runtime.settings.build_runtime` builds it (agent policy
   from the workspace, 10% holdout / 10% baseline split by salted hash, the business's time zone),
   on in-memory stores, and ticks once at 10:00 local on the day after the last order (the sweep).
4. What it decided: decisions per goal, contact vs leave alone, by group, the reasons and the hard
   rules that blocked options, and what the agent's warm start learned (segment cuts, organic
   rates, history sends).

Two variants, both reported:

- **as_is**: the data as published. No dataset has consent events, so nobody is contactable.
- **consent_invented**: the adapter adds one `consent_granted` (e-mail) per customer at their first
  event, as the repo's sample loaders do. This is NOT in the data; it is labelled everywhere.

Run:

    python3 -P scripts/v2_realdata/b_shadow.py superstore
    python3 -P scripts/v2_realdata/b_shadow.py --all
    python3 -P scripts/v2_realdata/b_shadow.py olist --max-customers 20000   # a sample, labelled

Results: /home/user/datasets/_results/b/shadow/<dataset>.json
"""

from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from b_adapters import FAMILY_B, adapt  # noqa: E402
from b_common import Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402
from b_ingest import decision_moment  # noqa: E402

from autonomy.agents.analyst import Analyst, SegmentSummary  # noqa: E402
from autonomy.agents.strategist import BusinessFacts, template_brief  # noqa: E402
from autonomy.contracts import (  # noqa: E402
    AutonomyLevel,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    DecisionKind,
    Event,
    EventType,
    Goal,
    GoalStatus,
    stable_id,
)
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import (  # noqa: E402
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_content  # noqa: E402
from autonomy.proof.split import group_assigner  # noqa: E402
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, TickReport  # noqa: E402
from autonomy.runtime.settings import workspace_policy_factory  # noqa: E402

SPLIT_SALT = "autonomy-split"


def with_consent(events: list[Event]) -> list[Event]:
    """One invented e-mail consent per customer, a minute before their first event."""
    first: dict[str, Event] = {}
    for e in events:
        if e.customer_id not in first or e.ts < first[e.customer_id].ts:
            first[e.customer_id] = e
    extra = [
        Event(
            event_id=stable_id("evt", "invented-consent", cid),
            customer_id=cid,
            type=EventType.CONSENT_GRANTED,
            ts=e.ts - timedelta(minutes=1),
            source="adapter-invented",
            channel=Channel.EMAIL,
            attrs={"invented": True},
        )
        for cid, e in first.items()
    ]
    return extra + events


def suggested_goals(states: list[CustomerState], now: datetime, currency: str) -> list[Goal]:
    """Both suggestion paths, contactable treated as unknown (else nothing is suggested at all)."""
    from api.v2.deps import suggest_opportunities

    goals: dict[str, Goal] = {}
    for g in suggest_opportunities(states, "realdata", now):
        goals[g.kind.value] = g.model_copy(update={"currency": currency})
    by_seg: dict[str, list[CustomerState]] = {}
    for s in states:
        by_seg.setdefault(s.segment or "none", []).append(s)
    summary = {
        seg: SegmentSummary(customers=len(rows), avg_value=sum(r.revenue for r in rows) / len(rows))
        for seg, rows in by_seg.items()
    }
    for g in Analyst(llm=None).opportunities(summary, now, business_id="realdata", currency=currency):  # type: ignore[arg-type]
        goals.setdefault(g.kind.value, g)
    return [
        g.model_copy(
            update={
                "status": GoalStatus.ACTIVE,
                "autonomy": AutonomyLevel.SHADOW,
                "starts_at": now - timedelta(hours=1),
            }
        )
        for g in goals.values()
    ]


_NUM = re.compile(r"-?[$£€]?\d[\d,]*(\.\d+)?%?( ?(BRL|TWD|GBP|USD))?")


def reason_key(text: str) -> str:
    return _NUM.sub("#", text)[:140]


def summarise(
    report: TickReport, runtime: AutonomyRuntime, goals: list[Goal], states: list[CustomerState]
) -> dict[str, Any]:
    from autonomy.runtime.service import Trigger, _decision_id

    decisions = report.decisions
    out: dict[str, Any] = {"decisions": len(decisions)}
    day = report.now.astimezone(runtime._tz).date().isoformat()
    owner = {
        _decision_id(g.goal_id, s.customer_id, Trigger.SWEEP, day): g.goal_id for g in goals for s in states
    }
    per_goal: dict[str, Counter[str]] = {}
    for d in decisions:
        gid = owner.get(d.decision_id, "?")
        per_goal.setdefault(gid, Counter())[f"{d.group.value}/{d.kind.value}"] += 1
    out["per_goal"] = {g: dict(c) for g, c in per_goal.items()}
    kinds = Counter(d.kind.value for d in decisions)
    out["kinds"] = dict(kinds)
    out["by_group"] = {
        f"{grp}/{kind}": n
        for (grp, kind), n in Counter((d.group.value, d.kind.value) for d in decisions).items()
    }
    out["customers_decided"] = len({d.customer_id for d in decisions})
    out["customers_with_any_act"] = len({d.customer_id for d in decisions if d.kind is DecisionKind.ACT})
    acts = [d for d in decisions if d.kind is DecisionKind.ACT and d.action is not None]
    actions = [d.action for d in acts if d.action is not None]
    out["act_by_goal"] = dict(Counter(a.goal_id for a in actions))
    out["act_channel_offer"] = dict(Counter(f"{a.channel.value}|{a.offer_id}" for a in actions))
    out["act_send_hour_local"] = dict(
        sorted(Counter(a.send_at.astimezone(runtime._tz).hour for a in actions).items())
    )
    nets = [d.expected_net for d in acts]
    if nets:
        out["act_expected_net"] = {
            "p10": float(np.quantile(nets, 0.1)),
            "p50": float(np.median(nets)),
            "p90": float(np.quantile(nets, 0.9)),
            "sum": float(np.sum(nets)),
        }
    out["probability_logged"] = {
        "min": float(min(d.probability for d in decisions)) if decisions else None,
        "all_set": all(0 < d.probability <= 1 for d in decisions),
    }
    out["blocked_by"] = dict(Counter(code for d in decisions for code in d.blocked_by).most_common())
    out["reasons_nothing"] = dict(
        Counter(reason_key(d.reason) for d in decisions if d.kind is DecisionKind.NOTHING).most_common(12)
    )
    out["reasons_act"] = dict(Counter(reason_key(d.reason) for d in acts).most_common(8))
    out["alerts"] = [{"code": a.code, "severity": a.severity, "message": a.message} for a in report.alerts]
    out["sent"] = len(report.sent)
    out["batches_created"] = len(report.batches_created)
    groups = Counter(s.group.value for s in runtime.states.scan())
    out["state_groups_after_tick"] = dict(groups)
    return out


def policy_view(runtime: AutonomyRuntime) -> dict[str, Any]:
    """What each goal's agent policy learned before / while deciding (its warm start and priors)."""
    out: dict[str, Any] = {}
    for gid, (_, policy) in runtime._policies.items():
        seg = getattr(policy, "segmenter", None)
        organic = getattr(policy, "organic", None)
        value = getattr(policy, "value", None)
        out[gid] = {
            "type": type(policy).__name__,
            "segmenter_fitted": getattr(seg, "fitted", None),
            "segment_cuts_per_30d": getattr(seg, "cuts", None),
            "data_start": getattr(seg, "data_start", None),
            "organic_overall_per_day": getattr(organic, "overall", None),
            "organic_segment_rates": getattr(organic, "segment_rate", None),
            "history_sends_used": getattr(policy, "history_used", None),
            "value_fallback_order": value._fallback_order() if value is not None else None,
            "value_fallback_margin_rate": value._fallback_margin() if value is not None else None,
        }
    return out


def fitted_view(states: list[CustomerState], now: datetime) -> dict[str, Any]:
    """What `prepare_day` would have fitted (the runtime never calls it): segment cuts and organic rates."""
    from autonomy.runtime.agent import build_agent_policy

    p = build_agent_policy([], [])
    p.segmenter.fit(states, now)
    p.organic.fit(states, now)
    labels = Counter(p.segmenter.label(s, now) for s in states)
    return {
        "segment_cuts_per_30d": p.segmenter.cuts,
        "organic_overall_per_day": p.organic.overall,
        "organic_segment_rates": p.organic.segment_rate,
        "agent_segments": dict(labels),
    }


def shadow(name: str, *, consent: bool, max_customers: int | None, seed: int) -> dict[str, Any]:
    clock = Clock()
    a = adapt(name, clock)
    if a.order_events is None:
        return {"skipped": "no events: " + " ".join(a.notes[:1])}
    events = list(a.order_events)
    sampled = None
    if max_customers is not None:
        ids = sorted({e.customer_id for e in events})
        if len(ids) > max_customers:
            keep = set(random.Random(seed).sample(ids, max_customers))
            events = [e for e in events if e.customer_id in keep]
            sampled = {"customers_in_data": len(ids), "customers_kept": max_customers, "seed": seed}
    if consent:
        events = with_consent(events)
    now = decision_moment(a.order_events, a.timezone)

    # Goals come from the states as they are before the runtime touches them.
    pre_store = InMemoryEventStore()
    pre_store.append(events)
    pre_states = InMemoryStateStore()
    rebuild_states(pre_store, pre_states, StateFolder(), now)
    states = list(pre_states.scan())
    goals = suggested_goals(states, now, a.currency)

    workspace = InMemoryWorkspace()
    facts = BusinessFacts(business_id="realdata", channels=(Channel.EMAIL,), offers=(TEN_OFF, FREE_SHIP))
    briefs = []
    for g in goals:
        workspace.put_goal(g)
        b = template_brief(g, facts, now - timedelta(hours=1))
        b = b.model_copy(update={"approved_by": "realdata-runner", "approved_at": now - timedelta(hours=1)})
        workspace.put_brief(b)
        briefs.append(b)
        # The sample library reuses the ids ci_0..ci_4 for every goal; the workspace keys content by
        # id, so a second goal's library would replace the first's. Prefix them per goal.
        content: list[ContentItem] = [
            c.model_copy(update={"content_id": f"{g.goal_id}_{c.content_id}"})
            for c in sample_content(now - timedelta(hours=1), g.goal_id)
            if c.channel is Channel.EMAIL
        ]
        workspace.put_content(content)

    store = InMemoryEventStore()
    store.append(events)
    runtime = AutonomyRuntime(
        events=store,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=workspace,
        folder=StateFolder(),
        policy_factory=workspace_policy_factory(workspace),
        assign_group=group_assigner(SPLIT_SALT, holdout_share=0.1, baseline_share=0.1),
        config=RuntimeConfig(timezone=a.timezone, holdout_share=0.1, baseline_share=0.1, run_send_jobs=False),
    )
    t0 = time.perf_counter()
    report = runtime.tick(now)
    tick_s = time.perf_counter() - t0
    result = summarise(report, runtime, goals, states)
    result.update(
        variant="consent_invented" if consent else "as_is",
        now=now,
        sampled=sampled,
        goals=[
            {
                "goal_id": g.goal_id,
                "kind": g.kind.value,
                "text": g.text,
                "budget": g.budget,
                "currency": g.currency,
            }
            for g in goals
        ],
        briefs=[
            {
                "goal_id": b.goal_id,
                "audience": b.audience.description,
                "channels": [c.value for c in b.envelope.channels],
                "offers": [o.offer_id for o in b.envelope.offers],
                "max_contacts_per_week": b.envelope.max_contacts_per_week,
            }
            for b in briefs
        ],
        audience_sizes={b.goal_id: sum(1 for s in states if _in(s, b, now)) for b in briefs},
        # The first tick also rebuilds every state from the event store (`rebuilt` below), so this
        # rate is decisions over the whole tick, not the decide step alone.
        tick_seconds=round(tick_s, 2),
        decisions_per_second=round(len(report.decisions) / max(tick_s, 1e-9), 1),
        policies=policy_view(runtime),
        what_prepare_day_would_fit=fitted_view(states, now),
        rebuilt=report.rebuilt,
        events_folded=report.events_folded,
    )
    return result


def _in(state: CustomerState, brief: Brief, now: datetime) -> bool:
    from autonomy.decide.audience import matches

    return matches(state, brief.audience, now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datasets", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--max-customers", type=int, default=None, help="decide for a random sample (labelled)"
    )
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    if args.all:
        code = 0
        extra = ["--max-customers", str(args.max_customers)] if args.max_customers else []
        for name in FAMILY_B:
            print(f"== {name}", flush=True)
            code |= subprocess.call([sys.executable, "-P", __file__, name, *extra])
        return code
    for name in args.datasets:
        t0 = time.perf_counter()
        payload: dict[str, Any] = {"dataset": name, "command": command_line(), "env": env_info()}
        for consent in (False, True):
            key = "consent_invented" if consent else "as_is"
            payload[key] = shadow(name, consent=consent, max_customers=args.max_customers, seed=args.seed)
            print(
                f"  {name} {key}: {payload[key].get('kinds')} in {payload[key].get('tick_seconds')} s",
                flush=True,
            )
        payload["total_seconds"] = round(time.perf_counter() - t0, 2)
        payload["peak_rss_mb"] = round(peak_rss_mb(), 1)
        path = write_result("shadow", name, payload)
        print(f"{name}: wrote {path} ({payload['total_seconds']} s, peak RSS {payload['peak_rss_mb']} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
