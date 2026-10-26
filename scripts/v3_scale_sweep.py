"""Time and memory of the runtime's daily sweep on N synthetic customers in a SQL state store (plan-v3 section 7).

REAL_DATA_RESULTS failure 11: a 1,000,000-customer sweep was OOM-killed because the sweep kept every
Decision and a copy of every state for the whole tick. The sweep now works in batches
(`RuntimeConfig.sweep_batch`); this script measures it. It writes N customers into a SQLite file
(states only: the process starts warm, as a worker does after its first tick), runs one Shadow sweep
of `--goals` goals, and prints one JSON line: seconds, decisions per second, and the process's peak
resident memory before and after the sweep. Run each setting in its own process so each peak is its
own:

    python -m scripts.v3_scale_sweep --customers 1000000 --batch 5000
    python -m scripts.v3_scale_sweep --customers 1000000 --batch 0       # the whole audience in one batch

`--policy fake` (the default) is a policy that costs nothing per customer, so the number is the
sweep's own memory. `--policy agent` is the production policy (`workspace_policy_factory`); it keeps
a few small records per customer of its own, so its peak grows with the population even when the
sweep's does not. `--log sql` records every decision in the SQLite file too (slow, and a few KB a
decision on disk); the default counts them. `--report-decisions` defaults to 1,000 for a batched
run (the deployed worker's setting) and to every decision for `--batch 0`, as the old sweep kept
them. `--enrich` writes one recent visit per customer into a SQL event store and runs a state
enricher, so each batch reads its customers' recent events. `--lapse` runs a second tick an hour on
with a time filter on every audience, so every goal does a lapse check (a scan of the store each).
Nothing is estimated: every figure is read from this run.
"""

from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from autonomy.contracts import (
    AudienceFilter,
    AutonomyLevel,
    Brief,
    Channel,
    ChannelStats,
    ContentItem,
    CustomerState,
    Decision,
    Event,
    EventType,
    GoalStatus,
)
from autonomy.data.folder import StateFolder
from autonomy.fakes import ContactEveryonePolicy, InMemoryEventStore, InMemoryWorkspace
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.interfaces import DecisionLog, EventStore
from autonomy.proof.split import group_assigner
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig
from autonomy.runtime.settings import workspace_policy_factory
from autonomy.store import SqlDecisionLog, SqlEventStore, SqlStateStore, create_all

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
WRITE_PAGE = 10_000


def rss_mb() -> float:
    """Peak resident memory of this process so far, in MB (Linux reports KB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


class CountingLog:
    """A decision log that counts what it is given and keeps nothing."""

    def __init__(self) -> None:
        self.recorded = 0

    def record(self, decisions: Iterable[Decision]) -> None:
        self.recorded += sum(1 for _ in decisions)

    def get(self, decision_id: str) -> Decision | None:  # noqa: ARG002
        return None

    def scan(
        self,
        *,
        since: datetime | None = None,  # noqa: ARG002
        until: datetime | None = None,  # noqa: ARG002
    ) -> Iterator[Decision]:
        return iter(())

    def for_customer(self, customer_id: str) -> list[Decision]:  # noqa: ARG002
        return []


def customer(i: int) -> CustomerState:
    """One customer: e-mail consent, one to five orders, the last 30 to 180 days ago."""
    bought = NOW - timedelta(days=30 + i % 150)
    return CustomerState(
        customer_id=f"c{i:08d}",
        as_of=NOW,
        first_seen_at=NOW - timedelta(days=400),
        last_seen_at=bought,
        last_purchase_at=bought,
        purchases=1 + i % 5,
        revenue=30.0 * (1 + i % 5),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )


def visit(i: int) -> Event:
    """One visit 3 days before the sweep: inside the enricher's 30-day lookback, outside the fold window."""
    return Event(
        event_id=f"v{i:08d}",
        customer_id=f"c{i:08d}",
        type=EventType.VISIT,
        ts=NOW - timedelta(days=3, hours=i % 24),
        source="v3-scale",
    )


def passthrough(
    states: list[CustomerState],
    events_by: Mapping[str, list[Event]],
    now: datetime,  # noqa: ARG001
) -> list[CustomerState]:
    """A state enricher that reads every customer's recent events and changes nothing."""
    assert all(s.customer_id in events_by for s in states)
    return states


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--customers", type=int, default=1_000_000)
    parser.add_argument(
        "--batch", type=int, default=5_000, help="RuntimeConfig.sweep_batch; 0 is the whole audience"
    )
    parser.add_argument("--goals", type=int, default=3)
    parser.add_argument("--policy", choices=("fake", "agent"), default="fake")
    parser.add_argument("--log", choices=("count", "sql"), default="count")
    parser.add_argument(
        "--report-decisions",
        default=None,
        help="decisions the report keeps in full: a number or 'all' (default: 1000, or all for --batch 0)",
    )
    parser.add_argument("--enrich", action="store_true", help="a state enricher on recent SQL events")
    parser.add_argument("--lapse", action="store_true", help="a lapse-check tick after the sweep")
    parser.add_argument("--db", type=Path, default=None, help="SQLite file (default: a temporary one)")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        path = args.db or Path(tmp) / "sweep.db"
        engine = create_engine(f"sqlite:///{path}")
        create_all(engine)
        states = SqlStateStore(engine)
        assign = group_assigner("v3-scale", holdout_share=0.1, baseline_share=0.1)
        t_write = time.perf_counter()
        for lo in range(0, args.customers, WRITE_PAGE):
            states.put(assign(customer(i)) for i in range(lo, min(lo + WRITE_PAGE, args.customers)))
        events: EventStore = InMemoryEventStore()
        if args.enrich:
            events = SqlEventStore(engine)
            for lo in range(0, args.customers, WRITE_PAGE):
                events.append(visit(i) for i in range(lo, min(lo + WRITE_PAGE, args.customers)))
        write_s = time.perf_counter() - t_write

        start = NOW - timedelta(days=1)
        workspace = InMemoryWorkspace()
        for k in range(args.goals):
            gid = f"goal_{k}"
            workspace.put_goal(
                sample_goal(start, gid).model_copy(
                    update={"autonomy": AutonomyLevel.SHADOW, "status": GoalStatus.ACTIVE}
                )
            )
            brief = sample_brief(start, gid)
            filters = [AudienceFilter(field="purchases", op="gte", value=1)]
            if args.lapse:
                # Customers whose last order is 30 days old at the sweep cross this an hour on.
                filters.append(AudienceFilter(field="last_purchase_at", op="days_since_gt", value=30))
            audience = brief.audience.model_copy(update={"filters": tuple(filters)})
            workspace.put_brief(brief.model_copy(update={"audience": audience}))
            workspace.put_content(sample_content(start, gid))
        log: DecisionLog = SqlDecisionLog(engine) if args.log == "sql" else CountingLog()
        policy = ContactEveryonePolicy()

        def fixed_policy(
            briefs: Sequence[Brief],  # noqa: ARG001
            content: Sequence[ContentItem],  # noqa: ARG001
        ) -> ContactEveryonePolicy:
            return policy

        if args.report_decisions is None:
            keep: int | None = 1_000 if args.batch else None
        else:
            keep = None if args.report_decisions == "all" else int(args.report_decisions)
        runtime = AutonomyRuntime(
            events=events,
            states=states,
            decisions=log,
            workspace=workspace,
            folder=StateFolder(),
            policy_factory=workspace_policy_factory(workspace) if args.policy == "agent" else fixed_policy,
            config=RuntimeConfig(
                sweep_batch=args.batch or None,
                report_decisions=keep,
                run_send_jobs=False,
                nightly_rebuild=False,
            ),
            state_enricher=passthrough if args.enrich else None,
        )
        # Warm, as a worker is after its first tick: the states are in the store, so the tick folds
        # nothing and goes straight to deciding.
        runtime._warm = True
        runtime.cursor.watermark = NOW - timedelta(hours=1)
        before = rss_mb()
        t0 = time.perf_counter()
        report = runtime.tick(NOW)
        seconds = time.perf_counter() - t0
        lapse: dict[str, object] = {}
        if args.lapse:
            t1 = time.perf_counter()
            later = runtime.tick(NOW + timedelta(hours=1, minutes=5))
            lapse = {
                "lapse_decisions": later.decision_count,
                "lapse_seconds": round(time.perf_counter() - t1, 1),
                "peak_rss_mb_after_lapse": round(rss_mb(), 1),
            }
        out = {
            "customers": args.customers,
            "goals": args.goals,
            "batch": args.batch or "whole audience",
            "policy": args.policy,
            "log": args.log,
            "report_decisions": "all" if keep is None else keep,
            "enrich": args.enrich,
            "decisions": report.decision_count,
            "decisions_kept_in_report": len(report.decisions),
            "alerts": [a.code for a in report.alerts],
            "write_states_seconds": round(write_s, 1),
            "sweep_seconds": round(seconds, 1),
            "decisions_per_second": round(report.decision_count / max(seconds, 1e-9), 1),
            "peak_rss_mb_before_sweep": round(before, 1),
            "peak_rss_mb_after_sweep": round(rss_mb(), 1),
            **lapse,
        }
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
