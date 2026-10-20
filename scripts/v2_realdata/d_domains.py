"""Family D, stage 1: can Autonomy represent a non-retail business? Doctor, events, state, goal, Shadow day.

For each of six non-retail files (bank telemarketing, telco churn, health-insurance cross-sell,
hotel bookings, credit-card default, KDD Cup 98 donations), the steps a business would take:

1. **Data Doctor** on the file as published: the real route (`POST /v2/engines/doctor/profile`
   through FastAPI's TestClient, so the 20 MB cap and the parser are the product's) and the same
   profiler in-process (`read_csv_bytes` + `profile_frame`) with the dataset's own target and key,
   so the leak check runs.
2. **Events** from `d_adapters` (each adapter is the customer's code and lists what it invented).
3. **State** with the production `StateFolder` (`rebuild_states`): segments, purchases, revenue,
   who is contactable.
4. **The business's own goal** (retention, conversion, cross-sell, donation) as a `Goal`, turned
   into a brief by the Strategist's deterministic template (`template_brief`), and whether the
   template's audience picks the right people: the dataset's own outcome inside vs outside it.
5. **Catalogue** (`Catalogue.infer`) on the purchases: what products and "refill" cycles it sees.
6. **Goal suggestions** (`api.v2.deps.suggest_opportunities`, served at `/v2/opportunities`).
7. **One Shadow day** of the real runtime on that goal, with consent invented (one e-mail
   `consent_granted` per customer, labelled), on a seeded sample of at most `--max-customers`.

Nothing here can show that a message would cause anything: none of these files is randomised.

Run (from the worktree root):

    python3 -P scripts/v2_realdata/d_domains.py --all
    python3 -P scripts/v2_realdata/d_domains.py telco_churn --max-customers 5000

Results: /home/user/datasets/_results/d/domains/<dataset>.json
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from b_shadow import SPLIT_SALT, policy_view, summarise, with_consent  # noqa: E402
from d_adapters import FAMILY_D, DAdapted, adapt, describe_events  # noqa: E402
from d_common import (  # noqa: E402
    Clock,
    command_line,
    doctor_client,
    env_info,
    peak_rss_mb,
    post_file,
    write_result,
)

from api.v2.engines.doctor import profile_frame, read_csv_bytes  # noqa: E402
from autonomy.agents.strategist import BusinessFacts, template_brief  # noqa: E402
from autonomy.catalog.products import Catalogue  # noqa: E402
from autonomy.contracts import AutonomyLevel, Channel, CustomerState, EventType, GoalStatus  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.decide.audience import matches  # noqa: E402
from autonomy.fakes import (  # noqa: E402
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_content  # noqa: E402
from autonomy.proof.split import group_assigner  # noqa: E402
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig  # noqa: E402
from autonomy.runtime.settings import workspace_policy_factory  # noqa: E402


def doctor(a: DAdapted) -> dict[str, Any]:
    """The real route as the file is published, then the profiler in-process with target and key."""
    data = a.doctor.read()
    out: dict[str, Any] = {"file": a.doctor.label, "megabytes": round(len(data) / 2**20, 2)}
    fields = {"delimiter": a.doctor.delimiter, "use_v1": "false", "snapshot": a.now.isoformat()}
    if a.doctor.target:
        fields["target"] = a.doctor.target
    if a.doctor.primary_key:
        fields["primary_key"] = a.doctor.primary_key
    t0 = time.perf_counter()
    out["route_as_published"] = post_file(doctor_client(), "profile", data, a.doctor.label, fields)
    out["route_seconds"] = round(time.perf_counter() - t0, 2)
    # In-process (no size cap): what the route would say if the file fitted.
    t0 = time.perf_counter()
    frame = read_csv_bytes(data, delimiter=a.doctor.delimiter)
    rep = profile_frame(
        frame, snapshot=a.now, primary_key=a.doctor.primary_key, target=a.doctor.target, use_v1=False
    )
    out["in_process"] = {
        "seconds": round(time.perf_counter() - t0, 2),
        "readiness_score": rep.readiness_score,
        "rows": rep.rows,
        "columns": rep.columns,
        "problems": [f"{p.severity}:{p.code}:{p.column or ''}:{p.message[:100]}" for p in rep.problems],
        "leak_suspects": [f"{s.column} ({s.reason}, {s.score})" for s in rep.leak_check.suspects],
        "date_columns_found": [d.column for d in rep.temporal_integrity.date_columns],
        "pii": [f"{f.column}:{f.type}:{f.detected_by}" for f in rep.pii_detected],
        "kinds": dict(Counter(c.kind for c in rep.column_profiles)),
    }
    return out


def states_of(events: list[Any], now: Any) -> list[CustomerState]:
    store = InMemoryEventStore()
    store.append(events)
    states = InMemoryStateStore()
    rebuild_states(store, states, StateFolder(), now)
    return list(states.scan())


def state_summary(states: list[CustomerState]) -> dict[str, Any]:
    rev = np.array([s.revenue for s in states])
    pur = np.array([s.purchases for s in states])
    return {
        "customers": len(states),
        "segments": dict(Counter(s.segment for s in states).most_common()),
        "purchases_p10_p50_p90": [float(np.quantile(pur, q)) for q in (0.1, 0.5, 0.9)],
        "revenue_p10_p50_p90": [round(float(np.quantile(rev, q)), 2) for q in (0.1, 0.5, 0.9)],
        "contactable": sum(1 for s in states if s.contactable_channels()),
        "timezones": dict(Counter(s.timezone for s in states).most_common(3)),
        "with_sends": sum(1 for s in states if any(c.sends for c in s.channels.values())),
    }


def outcome_split(a: DAdapted, states: list[CustomerState], inside: set[str]) -> dict[str, Any]:
    """The dataset's own outcome among the audience and the rest (descriptive, not causal)."""
    y = a.outcome
    ids = {s.customer_id for s in states}
    y = y[y.index.isin(ids)]
    m = y.index.isin(inside)
    return {
        "outcome": a.outcome_name,
        "audience": int(m.sum()),
        "rest": int((~m).sum()),
        "rate_in_audience": round(float(y[m].mean()), 4) if m.any() else None,
        "rate_in_rest": round(float(y[~m].mean()), 4) if (~m).any() else None,
    }


def catalogue(events: list[Any]) -> dict[str, Any]:
    buys = [e for e in events if e.type is EventType.PURCHASE and e.item_id]
    if not buys:
        return {"items": 0}
    cat = Catalogue.infer(buys)
    return {
        "items": len(cat),
        "products": [
            {
                "item_id": p.item_id,
                "replenishable": p.replenishable,
                "typical_cycle_days": p.typical_cycle_days,
            }
            for p in list(cat)[:8]
        ],
    }


def shadow_day(a: DAdapted, events: list[Any], max_customers: int, seed: int) -> dict[str, Any]:
    ids = sorted({e.customer_id for e in events})
    sampled = None
    if len(ids) > max_customers:
        keep = set(random.Random(seed).sample(ids, max_customers))
        events = [e for e in events if e.customer_id in keep]
        sampled = {"customers_in_data": len(ids), "customers_kept": max_customers, "seed": seed}
    events = with_consent(events)
    # The sweep runs once the business's local clock passes `sweep_hour`: decide at 10:00 local.
    local = a.now.astimezone(ZoneInfo(a.timezone))
    now = local.replace(hour=10, minute=0) if local.hour < 10 else local
    if now < a.now:
        now = a.now
    goal = a.goal(now - timedelta(hours=1)).model_copy(
        update={"status": GoalStatus.ACTIVE, "autonomy": AutonomyLevel.SHADOW}
    )
    workspace = InMemoryWorkspace()
    facts = BusinessFacts(business_id="realdata", channels=(Channel.EMAIL,), offers=(TEN_OFF, FREE_SHIP))
    brief = template_brief(goal, facts, now - timedelta(hours=1), kind=a.goal_kind)
    brief = brief.model_copy(
        update={"approved_by": "realdata-runner", "approved_at": now - timedelta(hours=1)}
    )
    workspace.put_goal(goal)
    workspace.put_brief(brief)
    workspace.put_content(
        [
            c.model_copy(update={"content_id": f"{goal.goal_id}_{c.content_id}"})
            for c in sample_content(now - timedelta(hours=1), goal.goal_id)
            if c.channel is Channel.EMAIL
        ]
    )
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
    secs = time.perf_counter() - t0
    states = list(runtime.states.scan())
    out = summarise(report, runtime, [goal], states)
    acted = {d.customer_id for d in report.decisions if d.action is not None}
    out.update(
        variant="consent_invented",
        sampled=sampled,
        goal={"kind": goal.kind.value, "objective": goal.objective.value, "text": goal.text},
        brief={
            "audience": brief.audience.description,
            "offers": [o.offer_id for o in brief.envelope.offers],
            "horizon_days": brief.horizon_days,
        },
        content_used=[c.body[:70] for c in sample_content(now, goal.goal_id) if c.channel is Channel.EMAIL],
        tick_seconds=round(secs, 2),
        decided_at=now,
        outcome_of_would_contact=outcome_split(a, states, acted),
        policies=policy_view(runtime),
    )
    return out


def run(name: str, *, max_customers: int, seed: int) -> dict[str, Any]:
    clock = Clock()
    with clock.step("adapt") as s:
        a = adapt(name, clock)
        s.extra = {"rows_in": a.rows_in}
    result: dict[str, Any] = {
        "dataset": name,
        "domain": a.domain,
        "command": command_line(),
        "env": env_info(),
        "now": a.now,
        "invented": a.invented,
        "notes": a.notes,
        "mapping_problems": a.problems,
        "events": describe_events(a.events),
        "extra": {k: v for k, v in a.extra.items() if k != "raw"},
    }
    with clock.step("doctor"):
        result["doctor"] = doctor(a)
    with clock.step("rebuild_states"):
        states = states_of(a.events, a.now)
    result["state"] = state_summary(states)
    goal = a.goal(a.now)
    facts = BusinessFacts(business_id="realdata", channels=(Channel.EMAIL,), offers=(TEN_OFF, FREE_SHIP))
    brief = template_brief(goal, facts, a.now, kind=a.goal_kind)
    inside = {s.customer_id for s in states if matches(s, brief.audience, a.now)}
    result["goal"] = {
        "kind": a.goal_kind.value,
        "objective": a.objective.value,
        "text": a.goal_text,
        "template_audience": brief.audience.description,
        "offers_left": [o.offer_id for o in brief.envelope.offers],
        "outcome_split": outcome_split(a, states, inside),
    }
    with clock.step("catalogue"):
        result["catalogue"] = catalogue(a.events)
    from api.v2.deps import suggest_opportunities

    result["suggested_goals"] = [
        {"kind": g.kind.value, "text": g.text, "budget": g.budget, "currency": g.currency}
        for g in suggest_opportunities(states, "realdata", a.now)
    ]
    with clock.step("shadow_day"):
        result["shadow"] = shadow_day(a, a.events, max_customers, seed)
    result["steps"] = clock.as_list()
    result["peak_rss_mb"] = round(peak_rss_mb(), 1)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("datasets", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-customers", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    names = list(FAMILY_D) if args.all else args.datasets
    for n in names:
        t0 = time.perf_counter()
        res = run(n, max_customers=args.max_customers, seed=args.seed)
        path = write_result("domains", n, res)
        print(f"{n}: {time.perf_counter() - t0:.1f}s -> {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
