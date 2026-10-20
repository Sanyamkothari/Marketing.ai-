"""Family B, scale: how much transaction history the ingestion path holds, on X5 RetailHero receipts.

X5's purchase file (45.8M lines, 8.0M receipts, 400k customers) is the largest real transaction log
we have. The file is sorted by client, so its first N clients come with their complete histories.
For N clients this measures, in one fresh process:

1. reading their lines (pandas, chunked) and grouping lines into receipts (one order each);
2. `map_frame` (the product's mapping) to order-grain `Event`s;
3. appending to the in-memory event store and `rebuild_states` with the production `StateFolder`;
4. optionally (`--shadow`) one Shadow sweep of the real runtime for a cross-sell goal over all of them,
   with invented e-mail consent (labelled), to time decisions per second at that size.

Wall time per step and the process's peak RSS are recorded. Larger sizes are run as separate
processes so each peak is its own. Nothing is extrapolated here; the notes say what a straight-line
extrapolation would give and label it as such.

Run:

    python3 -P scripts/v2_realdata/b_scale.py --clients 10000
    python3 -P scripts/v2_realdata/b_scale.py --curve 10000,40000,100000
    python3 -P scripts/v2_realdata/b_scale.py --clients 20000 --shadow

Results: /home/user/datasets/_results/b/scale/x5_<N>[_shadow].json
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
from b_common import DATASETS, Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.contracts import Event, EventType  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.mapping import EventMapping, map_frame, problem_counts  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402

PURCHASES = DATASETS / "x5_retailhero" / "purchases.csv.gz"
COLUMNS = [
    "client_id",
    "transaction_id",
    "transaction_datetime",
    "purchase_sum",
    "regular_points_spent",
    "express_points_spent",
]
MAPPING = EventMapping(
    customer_id="client_id",
    timestamp="transaction_datetime",
    timezone="Europe/Moscow",
    event_type=EventType.PURCHASE,
    value_column="purchase_sum",
    attrs={"invoice_no": "transaction_id", "offer_id": "points_offer"},
    fixed_attrs={"country": "RU", "timezone": "Europe/Moscow"},
    row_key=("transaction_id",),
)


def read_first_clients(n: int, clock: Clock) -> tuple[pd.DataFrame, int]:
    """Receipts of the first `n` clients in file order (complete histories: the file is sorted)."""
    parts: list[pd.DataFrame] = []
    seen: list[str] = []
    lines = 0
    last = None
    with clock.step("read_lines") as s:
        for chunk in pd.read_csv(PURCHASES, usecols=COLUMNS, dtype=str, chunksize=1_000_000):
            ids = chunk["client_id"]
            if last is not None and ids.iloc[0] < last:
                raise SystemExit("purchases.csv.gz is not sorted by client_id; the first-N read is invalid")
            if not ids.is_monotonic_increasing:
                raise SystemExit("a chunk is not sorted by client_id; the first-N read is invalid")
            uniq = ids.drop_duplicates()
            # A client whose lines straddle a chunk boundary was already counted in the last chunk.
            # (The recorded runs, before this fix, counted such a client twice, so N requested gave
            # N minus the number of boundaries crossed: 9,999 / 39,996 / 99,989 clients. Their
            # histories were still complete; the tables report the states actually built.)
            if last is not None and len(uniq) and uniq.iloc[0] == last:
                uniq = uniq.iloc[1:]
            room = n - len(seen)
            if len(uniq) > room:
                cut = uniq.iloc[room]
                chunk = chunk[ids < cut]
                seen += uniq.iloc[:room].tolist()
                parts.append(chunk)
                lines += len(chunk)
                break
            seen += uniq.tolist()
            parts.append(chunk)
            lines += len(chunk)
            last = ids.iloc[-1]
        s.extra = {"lines": lines, "clients": len(seen)}
    with clock.step("lines_to_receipts") as s:
        frame = pd.concat(parts, ignore_index=True)
        receipts = frame.drop_duplicates("transaction_id").copy()
        spent = pd.to_numeric(receipts["regular_points_spent"], errors="coerce").fillna(0) + pd.to_numeric(
            receipts["express_points_spent"], errors="coerce"
        ).fillna(0)
        # Loyalty points spent on a receipt: the closest thing to "bought with an offer" in this log.
        receipts["points_offer"] = ["points" if v != 0 else None for v in spent]
        receipts = receipts.reset_index(drop=True)
        s.extra = {"receipts": len(receipts)}
    return receipts, lines


def run(n: int, *, shadow: bool) -> dict[str, Any]:
    clock = Clock()
    t0 = time.perf_counter()
    receipts, lines = read_first_clients(n, clock)
    with clock.step("map_frame") as s:
        events, problems = map_frame(receipts, MAPPING, "x5")
        s.extra = {"events": len(events), "problems": problem_counts(problems)}
    del receipts
    store = InMemoryEventStore()
    with clock.step("event_store_append") as s:
        s.extra = {"appended": store.append(events)}
    last = max(e.ts for e in events)
    now = (last + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    states = InMemoryStateStore()
    with clock.step("rebuild_states") as s:
        s.extra = {"states": rebuild_states(store, states, StateFolder(), now)}
    out: dict[str, Any] = {
        "clients_requested": n,
        "lines_read": lines,
        "receipts": len(events),
        "now": now,
    }
    if shadow:
        out["shadow"] = shadow_sweep(events, now, clock)
    out["steps"] = clock.as_list()
    out["total_seconds"] = round(time.perf_counter() - t0, 2)
    out["peak_rss_mb"] = round(peak_rss_mb(), 1)
    return out


def shadow_sweep(events: list[Event], now: datetime, clock: Clock) -> dict[str, Any]:
    """One Shadow sweep of a cross-sell goal (template audience: bought in the last 60 days, which on
    X5 is nearly everyone; a win-back audience is empty because every client bought within the
    last 60 days of the log), consent invented (labelled)."""
    from collections import Counter

    from b_shadow import SPLIT_SALT, with_consent

    from autonomy.agents.strategist import BusinessFacts, template_brief
    from autonomy.contracts import AutonomyLevel, Channel, Goal, GoalKind, GoalStatus
    from autonomy.fakes import InMemoryDecisionLog, InMemoryWorkspace
    from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_content
    from autonomy.proof.split import group_assigner
    from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig
    from autonomy.runtime.settings import workspace_policy_factory

    start = now - timedelta(hours=1)
    goal = Goal(
        goal_id="goal_cross_sell",
        business_id="x5",
        text="Cross-sell to recent buyers",
        kind=GoalKind.CROSS_SELL,
        budget=100000,
        currency="RUB",
        starts_at=start,
        ends_at=start + timedelta(days=30),
        autonomy=AutonomyLevel.SHADOW,
        status=GoalStatus.ACTIVE,
        created_at=start,
    )
    facts = BusinessFacts(business_id="x5", channels=(Channel.EMAIL,), offers=(TEN_OFF, FREE_SHIP))
    brief = template_brief(goal, facts, start).model_copy(
        update={"approved_by": "realdata-runner", "approved_at": start}
    )
    ws = InMemoryWorkspace()
    ws.put_goal(goal)
    ws.put_brief(brief)
    ws.put_content([c for c in sample_content(start, goal.goal_id) if c.channel is Channel.EMAIL])
    store = InMemoryEventStore()
    with clock.step("shadow_append_with_invented_consent"):
        store.append(with_consent(list(events)))
    runtime = AutonomyRuntime(
        events=store,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=ws,
        folder=StateFolder(),
        policy_factory=workspace_policy_factory(ws),
        assign_group=group_assigner(SPLIT_SALT, holdout_share=0.1, baseline_share=0.1),
        config=RuntimeConfig(
            timezone="Europe/Moscow", holdout_share=0.1, baseline_share=0.1, run_send_jobs=False
        ),
    )
    local_now = now.astimezone(runtime._tz).replace(hour=10, minute=0)
    with clock.step("shadow_tick") as s:
        report = runtime.tick(local_now)
        s.extra = {"decisions": len(report.decisions)}
    tick_s = clock.steps[-1].seconds
    from autonomy.decide.audience import matches

    audience = sum(1 for st in runtime.states.scan() if matches(st, brief.audience, local_now))
    return {
        "goal": "cross_sell (template: bought in the last 60 days)",
        "audience": audience,
        "decisions": len(report.decisions),
        "kinds": dict(Counter(d.kind.value for d in report.decisions)),
        "tick_seconds": tick_s,
        "decisions_per_second": round(len(report.decisions) / max(tick_s, 1e-9), 1),
        "consent": "invented (one e-mail consent per customer)",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clients", type=int)
    parser.add_argument("--curve", type=str, help="comma-separated client counts, one process each")
    parser.add_argument("--shadow", action="store_true")
    args = parser.parse_args(argv)
    if args.curve:
        code = 0
        for n in [int(x) for x in args.curve.split(",")]:
            extra = ["--shadow"] if args.shadow else []
            code |= subprocess.call([sys.executable, "-P", __file__, "--clients", str(n), *extra])
        return code
    if not args.clients:
        parser.error("give --clients N or --curve N1,N2,...")
    payload = {
        "dataset": "x5_retailhero purchases (receipt grain)",
        "command": command_line(),
        "env": env_info(),
    }
    payload.update(run(args.clients, shadow=args.shadow))
    path = write_result("scale", f"x5_{args.clients}{'_shadow' if args.shadow else ''}", payload)
    print(
        f"x5 {args.clients} clients: {payload['receipts']} receipts, {payload['total_seconds']} s, "
        f"peak RSS {payload['peak_rss_mb']} MB -> {path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
