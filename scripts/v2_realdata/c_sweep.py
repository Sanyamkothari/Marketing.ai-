"""Family C, scale 2 and 3: one daily sweep of the real runtime, then nightly learning, at N customers.

**Customers.** No real dataset here has 1M customers with purchase histories *and* amounts
(RetailRocket has 1.4M visitors but no amounts and 11,719 buyers; X5's 400k need 19 GB in memory, see
B-transactions.md). So the customers are **synthetic, drawn from a real distribution**: each one copies
a random real customer of UCI Online Retail II (5,881 customers with an id and a positive order:
order count, revenue, first and last order dates, country), with dates shifted so the real data's
last day is yesterday and a +-3 day jitter. Everything else is invented and labelled:

* e-mail consent for everyone (no dataset has consent);
* margin = 35% of revenue (Online Retail has no margin; at margin 0 every bid is negative and the
  policy short-circuits, which would under-measure the work of deciding);
* the agent / baseline / holdout group by the production salted hash (10% / 10%).

**What runs.** The production `AutonomyRuntime` (in-memory stores by default, `--backend sqlite`
for SQLite state, decision and event stores), three goals from the Strategist's deterministic
templates (win-back, repeat purchase, cross-sell; e-mail; no offer, 10% off, free shipping), the
sample content library, Shadow mode. One `tick` at 10:00 UTC: this first tick of the day also runs
the nightly rebuild (empty log here) and the nightly learning on what was just decided, as
production does.

**Nightly learning with outcomes** (`--nightly`): after the sweep, synthetic outcome events are
appended (a send + deliver for every agent-group ACT decision, a click for 3% of them, and
purchases over the next 15 days at each customer's own real order rate, +20% after a click). These are **invented for
timing only**: nothing about the agent's quality can be read from them. Then the runtime's own
nightly step (`AutonomyRuntime._nightly`: `AgentPolicy.nightly` -> `update_models` ->
`learn_contact_harm` -> `ContactHarm.refit`, plus the personal-effects refit) runs 15 days after the
sweep, at 03:00 UTC.

`--profile` runs the sweep (and nightly) under cProfile and stores the top functions, which is how
the bottleneck is named. Profiled timings are slower than plain ones; plain runs give the timings.

Run:

    python3 -P scripts/v2_realdata/c_sweep.py --customers 10000 --nightly
    python3 -P scripts/v2_realdata/c_sweep.py --customers 10000 --nightly --profile
    python3 -P scripts/v2_realdata/c_sweep.py --curve 10000,100000,1000000 --nightly
    python3 -P scripts/v2_realdata/c_sweep.py --customers 100000 --backend sqlite

Results: /home/user/datasets/_results/c/sweep/sweep_<N>_<backend>[_profile].json
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from c_common import (  # noqa: E402
    DATASETS,
    RESULTS,
    TMP,
    Clock,
    command_line,
    current_rss_mb,
    env_info,
    file_mb,
    peak_rss_mb,
    sqlite_engine,
    write_result,
)

from autonomy.agents.strategist import BusinessFacts, template_brief  # noqa: E402
from autonomy.contracts import (  # noqa: E402
    AutonomyLevel,
    Channel,
    ChannelStats,
    CustomerState,
    DecisionKind,
    Event,
    EventType,
    Goal,
    GoalKind,
    GoalStatus,
    Group,
    stable_id,
)
from autonomy.data.folder import StateFolder  # noqa: E402
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

ORII = DATASETS / "online_retail_ii" / "online_retail_II.csv"
POOL = RESULTS / "pool_online_retail_ii.csv"
NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
"""The sweep moment (10:00 UTC, the default sweep hour). Arbitrary; the pool's dates are shifted to it."""
MARGIN_RATE = 0.35
SPLIT_SALT = "autonomy-split"
GOAL_KINDS = (GoalKind.WIN_BACK, GoalKind.REPEAT_PURCHASE, GoalKind.CROSS_SELL)


def build_pool() -> pd.DataFrame:
    """Per real Online Retail II customer: orders, revenue, first / last order, country. Cached."""
    if POOL.exists():
        return pd.read_csv(POOL, parse_dates=["first", "last"])
    raw = pd.read_csv(
        ORII,
        usecols=["Invoice", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"],
        dtype={"Invoice": str, "Country": str},
    )
    raw = raw.dropna(subset=["Customer ID"])
    raw = raw[(raw["Quantity"] > 0) & (raw["Price"] > 0) & ~raw["Invoice"].str.startswith("C")]
    raw["amount"] = raw["Quantity"] * raw["Price"]
    raw["InvoiceDate"] = pd.to_datetime(raw["InvoiceDate"])
    orders = raw.groupby(["Customer ID", "Invoice"]).agg(
        ts=("InvoiceDate", "min"), amount=("amount", "sum"), country=("Country", "first")
    )
    per = orders.groupby(level=0).agg(
        orders=("amount", "size"),
        revenue=("amount", "sum"),
        first=("ts", "min"),
        last=("ts", "max"),
        country=("country", "first"),
    )
    per = per.reset_index(drop=True)
    POOL.parent.mkdir(parents=True, exist_ok=True)
    per.to_csv(POOL, index=False)
    return pd.read_csv(POOL, parse_dates=["first", "last"])


def synth_states(n: int, now: datetime, seed: int) -> list[CustomerState]:
    """`n` customers, each a copy of a random real Online Retail II customer (see the docstring)."""
    pool = build_pool()
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(pool), size=n)
    data_end = pool["last"].max()
    shift = (now - timedelta(days=1)).replace(tzinfo=None) - data_end.to_pydatetime()
    jitter = rng.integers(-3, 4, size=n)
    folder = StateFolder()
    email = ChannelStats(address_known=True, consent=True)
    first = pool["first"].to_numpy()
    last = pool["last"].to_numpy()
    orders = pool["orders"].to_numpy()
    revenue = pool["revenue"].to_numpy()
    out: list[CustomerState] = []
    for i in range(n):
        j = int(pick[i])
        d = shift + timedelta(days=int(jitter[i]))
        f = (pd.Timestamp(first[j]).to_pydatetime() + d).replace(tzinfo=UTC)
        la = min((pd.Timestamp(last[j]).to_pydatetime() + d).replace(tzinfo=UTC), now - timedelta(hours=1))
        f = min(f, la)
        s = CustomerState(
            customer_id=f"s{i:07d}",
            as_of=la,
            version=int(orders[j]) + 1,
            first_seen_at=f,
            last_seen_at=la,
            last_purchase_at=la,
            purchases=int(orders[j]),
            revenue=float(revenue[j]),
            margin=float(revenue[j]) * MARGIN_RATE,
            channels={Channel.EMAIL: email},
            offer_sensitivity=0.3,
        )
        out.append(folder.refresh(s, now))
    return out


def make_goals(workspace: InMemoryWorkspace, now: datetime) -> list[Goal]:
    facts = BusinessFacts(business_id="c-scale", channels=(Channel.EMAIL,), offers=(TEN_OFF, FREE_SHIP))
    goals = []
    for kind in GOAL_KINDS:
        g = Goal(
            goal_id=f"goal_{kind.value}",
            business_id="c-scale",
            text=f"{kind.value.replace('_', ' ')} (scale test)",
            kind=kind,
            budget=1_000_000.0,
            currency="GBP",
            starts_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
            autonomy=AutonomyLevel.SHADOW,
            status=GoalStatus.ACTIVE,
        )
        b = template_brief(g, facts, now - timedelta(hours=1), kind=kind)
        b = b.model_copy(update={"approved_by": "c-scale-runner", "approved_at": now - timedelta(hours=1)})
        workspace.put_goal(g)
        workspace.put_brief(b)
        workspace.put_content(
            [
                c.model_copy(update={"content_id": f"{g.goal_id}_{c.content_id}"})
                for c in sample_content(now - timedelta(hours=1), g.goal_id)
                if c.channel is Channel.EMAIL
            ]
        )
        goals.append(g)
    return goals


def stores(backend: str, n: int) -> tuple[Any, Any, Any, Any, Path | None]:
    if backend == "memory":
        return InMemoryEventStore(), InMemoryStateStore(), InMemoryDecisionLog(), InMemoryWorkspace(), None
    from autonomy.store import create_all
    from autonomy.store.sql import SqlDecisionLog, SqlEventStore, SqlStateStore

    db = TMP / f"sweep_{n}.sqlite"
    engine = sqlite_engine(db)
    create_all(engine)
    # The workspace stays in memory: three goals, 15 content items; the per-customer tables are SQL.
    return SqlEventStore(engine), SqlStateStore(engine), SqlDecisionLog(engine), InMemoryWorkspace(), db


def top_functions(prof: cProfile.Profile, n: int = 30) -> dict[str, list[dict[str, Any]]]:
    st = pstats.Stats(prof)
    rows = []
    for (file, line, fn), (_cc, nc, tt, ct, _) in st.stats.items():  # type: ignore[attr-defined]
        short = file.split("/realdata/")[-1] if "/realdata/" in file else file.split("/")[-1]
        rows.append(
            {
                "function": f"{short}:{line}:{fn}",
                "calls": nc,
                "tottime": round(tt, 3),
                "cumtime": round(ct, 3),
            }
        )
    by_cum = sorted(rows, key=lambda r: -r["cumtime"])[:n]
    by_tot = sorted(rows, key=lambda r: -r["tottime"])[:n]
    return {"by_cumtime": by_cum, "by_tottime": by_tot}


def summarise(report: TickReport) -> dict[str, Any]:
    ds = report.decisions
    acts = [d for d in ds if d.kind is DecisionKind.ACT and d.action is not None]
    return {
        "decisions": len(ds),
        "customers_decided": len({d.customer_id for d in ds}),
        "by_group_kind": dict(Counter(f"{d.group.value}/{d.kind.value}" for d in ds)),
        "act_by_goal": dict(Counter(d.action.goal_id for d in acts if d.action is not None)),
        "act_offer": dict(Counter(d.action.offer_id for d in acts if d.action is not None)),
        "blocked_by": dict(Counter(c for d in ds for c in d.blocked_by).most_common(10)),
        "probability_all_set": all(0 < d.probability <= 1 for d in ds),
        "alerts": [f"{a.code}: {a.message[:160]}" for a in report.alerts],
        "swept_goals": report.swept_goals,
        "nightly_ran": report.nightly_ran,
        "rebuilt": report.rebuilt,
    }


def outcome_events(
    report: TickReport, states: dict[str, CustomerState], now: datetime, seed: int
) -> list[Event]:
    """Invented outcomes over 15 days, for timing the learner only (see the module docstring)."""
    rng = np.random.default_rng(seed + 1)
    out: list[Event] = []
    clicked: set[str] = set()
    for d in report.decisions:
        if d.kind is not DecisionKind.ACT or d.action is None or d.group is not Group.AGENT:
            continue
        a = d.action
        send_at = max(a.send_at, now)
        base = {
            "customer_id": d.customer_id,
            "source": "c-scale-synthetic",
            "channel": a.channel,
            "decision_id": d.decision_id,
        }
        out.append(
            Event(event_id=stable_id("evt", "send", d.decision_id), type=EventType.SEND, ts=send_at, **base)
        )
        out.append(
            Event(
                event_id=stable_id("evt", "dlv", d.decision_id),
                type=EventType.DELIVER,
                ts=send_at + timedelta(minutes=1),
                **base,
            )
        )
        if rng.random() < 0.03:
            clicked.add(d.customer_id)
            out.append(
                Event(
                    event_id=stable_id("evt", "clk", d.decision_id),
                    type=EventType.CLICK,
                    ts=send_at + timedelta(hours=float(rng.uniform(0.1, 30))),
                    **base,
                )
            )
    for cid, s in states.items():
        days = (
            max((s.last_purchase_at - s.first_seen_at).days, 30)
            if s.last_purchase_at and s.first_seen_at
            else 365
        )
        rate = s.purchases / days * (1.2 if cid in clicked else 1.0)
        k = int(rng.poisson(rate * 15))
        for j in range(k):
            ts = now + timedelta(days=float(rng.uniform(0, 15)))
            value = s.revenue / max(s.purchases, 1)
            out.append(
                Event(
                    event_id=stable_id("evt", "buy", cid, j),
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=ts,
                    source="c-scale-synthetic",
                    value=round(value, 2),
                    margin=round(value * MARGIN_RATE, 2),
                )
            )
    return out


def history_events(states: list[CustomerState], seed: int) -> list[Event]:
    """Consent at first sight and each customer's orders between their first and last order.

    Online Retail II gives order count, revenue and first / last dates per customer, not every
    order's date, so the orders in between are placed uniformly at random (labelled synthetic)."""
    rng = np.random.default_rng(seed + 2)
    out: list[Event] = []
    for s in states:
        assert s.first_seen_at is not None and s.last_purchase_at is not None
        cid, f, la = s.customer_id, s.first_seen_at, s.last_purchase_at
        out.append(
            Event(
                event_id=stable_id("evt", "consent", cid),
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=f - timedelta(minutes=1),
                source="c-scale-synthetic",
                channel=Channel.EMAIL,
            )
        )
        n = max(s.purchases, 1)
        span = (la - f).total_seconds()
        times = [f, la] if n >= 2 else [la]
        times += [f + timedelta(seconds=float(x)) for x in rng.uniform(0, max(span, 1), size=max(n - 2, 0))]
        value = s.revenue / n
        for j, ts in enumerate(sorted(times)):
            out.append(
                Event(
                    event_id=stable_id("evt", "hist", cid, j),
                    customer_id=cid,
                    type=EventType.PURCHASE,
                    ts=ts,
                    source="c-scale-synthetic",
                    value=round(value, 2),
                    margin=round(value * MARGIN_RATE, 2),
                )
            )
    return out


def run(n: int, backend: str, nightly: bool, profile: bool, seed: int, keep_db: bool, history: bool) -> Path:
    clock = Clock()
    t_start = time.perf_counter()
    rss0 = current_rss_mb()
    with clock.step("synth_states") as s:
        states = synth_states(n, NOW, seed)
        s.extra["segments"] = dict(Counter(x.segment for x in states).most_common())
    rss1 = current_rss_mb()
    events, state_store, decisions, workspace, db = stores(backend, n)
    if history:
        # The realistic first day: the log holds each customer's consent and orders, and the tick's
        # nightly rebuild folds them into state (states are not written directly).
        with clock.step("history_events") as s:
            hist = history_events(states, seed)
            s.extra["events"] = len(hist)
        with clock.step("append_history") as s:
            for i in range(0, len(hist), 50_000):
                events.append(hist[i : i + 50_000])
            s.extra["events"] = len(hist)
        del hist
    else:
        with clock.step("put_states") as s:
            for i in range(0, len(states), 10_000):
                state_store.put(states[i : i + 10_000])
    make_goals(workspace, NOW)
    del states
    runtime = AutonomyRuntime(
        events=events,
        states=state_store,
        decisions=decisions,
        workspace=workspace,
        folder=StateFolder(),
        policy_factory=workspace_policy_factory(workspace),
        assign_group=group_assigner(SPLIT_SALT, holdout_share=0.1, baseline_share=0.1),
        config=RuntimeConfig(timezone="UTC", holdout_share=0.1, baseline_share=0.1, run_send_jobs=False),
    )
    prof = cProfile.Profile() if profile else None
    with clock.step("tick_sweep") as s:
        if prof:
            prof.enable()
        report = runtime.tick(NOW)
        if prof:
            prof.disable()
        s.extra["decisions"] = len(report.decisions)
    tick_seconds = clock.steps[-1].seconds
    payload: dict[str, Any] = {
        "customers": n,
        "backend": backend,
        "profiled": profile,
        "history_in_log": history,
        "command": command_line(),
        "env": env_info(),
        "synthetic": "customers copied from real Online Retail II customers; consent, margin and outcomes invented",
        "state_mb_per_1000_customers": round((rss1 - rss0) / n * 1000, 2) if n else None,
        "sweep": summarise(report),
        "sweep_decisions_per_second": round(len(report.decisions) / max(tick_seconds, 1e-9), 1),
    }
    if prof:
        payload["sweep_profile"] = top_functions(prof)
    if nightly:
        with clock.step("synth_outcomes") as s:
            # Fresh states with their group (the runtime assigned it during the tick).
            grouped = {x.customer_id: x for x in runtime.states.scan()}
            outs = outcome_events(report, grouped, NOW, seed)
            s.extra["events"] = len(outs)
            s.extra["by_type"] = dict(Counter(e.type.value for e in outs))
        with clock.step("append_outcomes") as s:
            s.extra["new"] = runtime.events.append(outs)
        del outs, grouped
        later = NOW + timedelta(days=15)
        later = later.replace(hour=3)
        runtime.cursor.nightly_on = None
        report2 = TickReport(now=later)
        runtime._start_day(later.date())
        prof2 = cProfile.Profile() if profile else None
        with clock.step("nightly") as s:
            if prof2:
                prof2.enable()
            runtime._nightly(later, later, report2)
            if prof2:
                prof2.disable()
            s.extra["alerts"] = [f"{a.code}: {a.message[:160]}" for a in report2.alerts]
        payload["nightly"] = {"at": later, "seconds": clock.steps[-1].seconds, "alerts": s.extra["alerts"]}
        payload["learned"] = learned_view(runtime)
        if prof2:
            payload["nightly_profile"] = top_functions(prof2)
    payload["steps"] = clock.as_dict()
    payload["total_seconds"] = round(time.perf_counter() - t_start, 2)
    payload["peak_rss_mb"] = round(peak_rss_mb(), 1)
    if db is not None:
        payload["db_file_mb"] = file_mb(db)
        if not keep_db:
            db.unlink(missing_ok=True)
    name = f"sweep_{n}_{backend}" + ("_history" if history else "") + ("_profile" if profile else "")
    path = write_result("sweep", name, payload)
    print(f"wrote {path}: {payload['total_seconds']} s, peak RSS {payload['peak_rss_mb']} MB", flush=True)
    return path


def learned_view(runtime: AutonomyRuntime) -> dict[str, Any]:
    from autonomy.decide.bidders import contact_harm_of

    out: dict[str, Any] = {}
    for gid, (_, policy) in runtime._policies.items():
        bundle = getattr(policy, "bundle", None)
        harm = contact_harm_of(bundle.response, create=False) if bundle is not None else None
        out[gid] = {
            "learned_decisions": len(bundle.learned_decisions) if bundle is not None else None,
            "harm_customers_with_evidence": len(harm.evidence) if harm is not None else None,
            "harm_fitted": harm.fitted if harm is not None else None,
            "harm_describe": harm.describe() if harm is not None else None,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--customers", type=int, default=10_000)
    parser.add_argument("--curve", default=None, help="comma-separated sizes, each in a fresh process")
    parser.add_argument("--backend", default="memory", choices=["memory", "sqlite"])
    parser.add_argument("--nightly", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument(
        "--history", action="store_true", help="put consent + order events in the log, not states"
    )
    args = parser.parse_args(argv)
    if args.curve:
        code = 0
        for size in args.curve.split(","):
            cmd = [
                sys.executable,
                "-P",
                __file__,
                "--customers",
                size,
                "--backend",
                args.backend,
                "--seed",
                str(args.seed),
            ]
            cmd += ["--nightly"] if args.nightly else []
            cmd += ["--profile"] if args.profile else []
            cmd += ["--history"] if args.history else []
            code |= subprocess.call(cmd)
        return code
    print(f"== sweep customers={args.customers} backend={args.backend}", flush=True)
    run(args.customers, args.backend, args.nightly, args.profile, args.seed, args.keep_db, args.history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
