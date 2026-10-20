"""Family B, stage 1: Data Doctor, mapping, customer-state rebuild, goal suggestions, warm-start inputs.

For each transaction dataset this runs, in order, what a customer onboarding Autonomy would hit:

1. **Data Doctor** (`api.v2.engines.doctor.profile_frame`, the function behind
   `POST /v2/engines/doctor/profile`) on each file as a customer would upload it, after the same
   `read_csv_bytes` the route uses. Whether the route itself would accept the upload (CSV only,
   20 MB cap) is recorded beside it; one file per dataset is also posted to the real route through
   FastAPI's TestClient (`--http`), to confirm the in-process numbers are the route's.
2. **Mapping** (`b_adapters`: `map_frame` with an `EventMapping`, or the built-in loader).
3. **State rebuild** (`autonomy.data.rebuild.rebuild_states` with the production `StateFolder`) into
   the in-memory stores (`--sql` also times SQLite through `autonomy.store`).
4. **State summary**: segments, repeat rate, order value, contactable customers, currency.
5. **Goal suggestions**: the API's `suggest_opportunities` (what `/v2/opportunities` serves), the
   Analyst's rule-based `opportunities()` fed a segment summary built from state, and how many
   customers each goal kind's template audience would hold.
6. **Warm-start inputs**: how many past sends / clicks the agent could learn from
   (`autonomy.learning.observations.logged_sends`), and the value prior's scale against the data.

Run (one dataset per process keeps peak memory per dataset honest):

    python3 -P scripts/v2_realdata/b_ingest.py online_retail_i
    python3 -P scripts/v2_realdata/b_ingest.py --all            # every family-B dataset
    python3 -P scripts/v2_realdata/b_ingest.py olist --http --sql

Results: /home/user/datasets/_results/b/ingest/<dataset>.json
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
from b_adapters import FAMILY_B, Adapted, DoctorInput, adapt  # noqa: E402
from b_common import Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.contracts import CustomerState, Event, EventType  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402

MAX_UPLOAD = 20 * 1024 * 1024


def decision_moment(events: list[Event], tz: str) -> datetime:
    """The day after the last event, 10:00 local: the runtime's default sweep hour."""
    last = max(e.ts for e in events).astimezone(ZoneInfo(tz))
    day = (last + timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=ZoneInfo(tz))


# --------------------------------------------------------------------------- Data Doctor


def run_doctor(inp: DoctorInput, snapshot: datetime | None, *, use_v1: bool) -> dict[str, Any]:
    from api.v2.engines.doctor import profile_frame, read_csv_bytes

    out: dict[str, Any] = {"file": inp.label, "as_published": inp.as_published}
    t0 = time.perf_counter()
    try:
        data = inp.read_bytes()
    except Exception as err:
        out["error"] = f"{type(err).__name__}: {err}"
        return out
    out["bytes"] = len(data)
    reasons = []
    if not inp.as_published.startswith("csv") and inp.as_published != "whitespace text":
        reasons.append(f"published as {inp.as_published}, the route reads CSV only")
    if inp.as_published.endswith(".gz") or inp.as_published == "zip":
        reasons.append("compressed: must be unpacked first")
    if len(data) > MAX_UPLOAD:
        reasons.append(f"{len(data) / 1e6:.0f} MB after unpacking, over the 20 MB cap (HTTP 413)")
    out["route_would_accept_as_published"] = not reasons
    out["route_refusal_reasons"] = reasons
    try:
        frame = read_csv_bytes(data, delimiter=inp.delimiter)
    except ValueError as err:
        out["read_error"] = str(err)
        out["seconds"] = round(time.perf_counter() - t0, 2)
        return out
    read_s = time.perf_counter() - t0
    try:
        report = profile_frame(
            frame,
            snapshot=snapshot,
            primary_key=inp.primary_key,
            date_columns=list(inp.date_columns) if inp.date_columns is not None else None,
            use_v1=use_v1,
        )
    except Exception as err:
        out["profile_error"] = f"{type(err).__name__}: {err}"
        out["seconds"] = round(time.perf_counter() - t0, 2)
        return out
    if snapshot is not None:
        # The route's default is no snapshot (the field is optional), and then no date is checked
        # for being in the future. The runner's snapshot is the day after the last *mapped* purchase,
        # which is earlier than the export (it leaves out cancelled orders and every later delivery
        # or ship date), so future-date errors under it are partly the runner's choice.
        bare = profile_frame(
            frame,
            snapshot=None,
            primary_key=inp.primary_key,
            date_columns=list(inp.date_columns) if inp.date_columns is not None else None,
            use_v1=use_v1,
        )
        out["readiness_score_no_snapshot"] = bare.readiness_score
        out["problem_codes_no_snapshot"] = [p.code for p in bare.problems]
    out.update(
        snapshot=snapshot,
        rows=report.rows,
        columns=report.columns,
        readiness_score=report.readiness_score,
        problems=[p.model_dump(mode="json") for p in report.problems],
        pii=[p.model_dump(mode="json") for p in report.pii_detected],
        primary_key=report.primary_key.model_dump(mode="json"),
        date_columns=[d.model_dump(mode="json") for d in report.temporal_integrity.date_columns],
        column_kinds={c.name: c.kind for c in report.column_profiles},
        v1_enrichment=bool(report.v1_enrichment),
        read_seconds=round(read_s, 2),
        seconds=round(time.perf_counter() - t0, 2),
    )
    return out


def run_doctor_http(inp: DoctorInput, snapshot: datetime | None) -> dict[str, Any]:
    """Post the file to the real route (TestClient, no network)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.v2.engines.doctor import router

    app = FastAPI()
    app.include_router(router, prefix="/v2/engines")
    client = TestClient(app)
    data = inp.read_bytes()
    fields = {"delimiter": inp.delimiter, "use_v1": "false"}
    if snapshot is not None:
        fields["snapshot"] = snapshot.isoformat()
    t0 = time.perf_counter()
    resp = client.post(
        "/v2/engines/doctor/profile", files={"file": (inp.label, data, "text/csv")}, data=fields
    )
    out: dict[str, Any] = {
        "file": inp.label,
        "status": resp.status_code,
        "seconds": round(time.perf_counter() - t0, 2),
    }
    body = resp.json()
    if resp.status_code == 200:
        out["readiness_score"] = body["readiness_score"]
        out["problem_codes"] = [p["code"] for p in body["problems"]]
    else:
        out["detail"] = body.get("detail")
    return out


# --------------------------------------------------------------------------- state


def rebuild(
    events: list[Event], now: datetime, clock: Clock, *, sql: bool
) -> tuple[list[CustomerState], dict[str, Any]]:
    timings: dict[str, Any] = {}
    store = InMemoryEventStore()
    with clock.step("event_store_append") as s:
        s.extra = {"appended": store.append(events)}
    states = InMemoryStateStore()
    with clock.step("rebuild_states", trace=True) as s:
        written = rebuild_states(store, states, StateFolder(), now)
        s.extra = {"states": written}
    timings["in_memory"] = {"append_s": clock.steps[-2].seconds, "rebuild_s": clock.steps[-1].seconds}
    if sql:
        from autonomy.store import SqlEventStore, SqlStateStore, create_all, memory_engine

        engine = memory_engine()
        create_all(engine)
        sev, sst = SqlEventStore(engine), SqlStateStore(engine)
        with clock.step("sqlite_append") as s:
            s.extra = {"appended": sev.append(events)}
        with clock.step("sqlite_rebuild") as s:
            s.extra = {"states": rebuild_states(sev, sst, StateFolder(), now)}
        timings["sqlite_memory"] = {"append_s": clock.steps[-2].seconds, "rebuild_s": clock.steps[-1].seconds}
    return list(states.scan()), timings


def _q(values: list[float], qs: tuple[float, ...] = (0.1, 0.5, 0.9)) -> dict[str, float]:
    if not values:
        return {}
    arr = np.asarray(values, dtype=float)
    return {f"p{int(q * 100)}": round(float(np.quantile(arr, q)), 2) for q in qs} | {
        "mean": round(float(arr.mean()), 2)
    }


def summarise_states(states: list[CustomerState], events: list[Event], now: datetime) -> dict[str, Any]:
    n = len(states)
    buyers = [s for s in states if s.purchases > 0]
    purchases = Counter(min(s.purchases, 5) for s in states)
    order_vals = [e.value for e in events if e.type is EventType.PURCHASE and e.value is not None]
    refunds = sum(1 for e in events if e.type is EventType.REFUND)
    revenue = sum(s.revenue for s in states)
    margin = sum(s.margin for s in states)
    gaps: list[float] = []
    by: dict[str, list[datetime]] = {}
    for e in events:
        if e.type is EventType.PURCHASE:
            by.setdefault(e.customer_id, []).append(e.ts)
    for ts in by.values():
        ts.sort()
        gaps += [(b - a).total_seconds() / 86400 for a, b in pairwise(ts)]
    first = min(e.ts for e in events)
    last = max(e.ts for e in events)
    offer = [s.offer_sensitivity for s in buyers if s.offer_sensitivity is not None]
    return {
        "customers": n,
        "buyers": len(buyers),
        "orders_per_customer": {("5+" if k == 5 else str(k)): v for k, v in sorted(purchases.items())},
        "repeat_rate": round(sum(1 for s in buyers if s.purchases >= 2) / max(len(buyers), 1), 4),
        "one_time_buyers": sum(1 for s in states if s.purchases == 1),
        "order_value": _q(order_vals),
        "refund_events": refunds,
        "net_revenue": round(revenue, 2),
        "margin_total": round(margin, 2),
        "customers_with_margin": sum(1 for s in states if s.margin > 0),
        "days_between_orders": _q(gaps),
        "first_event": first,
        "last_event": last,
        "span_days": round((last - first).total_seconds() / 86400, 1),
        "segments": dict(Counter(s.segment for s in states).most_common()),
        "contactable_any_channel": sum(1 for s in states if s.contactable_channels()),
        "countries_top5": dict(Counter(s.country for s in states).most_common(5)),
        "timezones": dict(Counter(s.timezone for s in states).most_common(5)),
        "with_top_categories": sum(1 for s in states if s.top_categories),
        "offer_sensitivity_mean": round(statistics.fmean(offer), 4) if offer else None,
        "offer_sensitivity_distinct_values": len({round(o, 4) for o in offer}),
        "vip_bar_note": "vip needs >= 5 purchases and >= 500 revenue in the business currency",
        "now": now,
    }


def basket_stats(a: Adapted) -> dict[str, Any]:
    if a.baskets is None or a.baskets.empty:
        return {}
    size = a.baskets.groupby("basket_id")["item_id"].nunique()
    return {
        "baskets": int(size.size),
        "distinct_items": int(a.baskets["item_id"].nunique()),
        "items_per_basket": _q(size.astype(float).tolist(), (0.5, 0.9, 0.99)),
        "single_item_basket_share": round(float((size == 1).mean()), 4),
        "time_is_invented": a.invented_time,
    }


# --------------------------------------------------------------------------- goals


def goal_suggestions(states: list[CustomerState], now: datetime, currency: str) -> dict[str, Any]:
    from api.v2.deps import _AUDIENCES, suggest_opportunities
    from autonomy.agents.analyst import Analyst, SegmentSummary
    from autonomy.agents.strategist import TEMPLATES
    from autonomy.decide.audience import matches

    api_goals = suggest_opportunities(states, "realdata", now)
    api_rows = []
    for g in api_goals:
        if g.goal_id == "opp_repeat_purchase":
            size = sum(1 for s in states if s.purchases == 1)
        elif g.goal_id == "opp_onboarding":
            size = sum(1 for s in states if s.purchases == 0)
        else:
            size = sum(1 for s in states if matches(s, _AUDIENCES[g.kind], now))
        api_rows.append(
            {"goal_id": g.goal_id, "kind": g.kind.value, "text": g.text, "budget": g.budget, "audience": size}
        )

    by_seg: dict[str, list[CustomerState]] = {}
    for s in states:
        by_seg.setdefault(s.segment or "none", []).append(s)

    def summary(contactable_known: bool) -> dict[str, SegmentSummary]:
        out = {}
        for seg, rows in by_seg.items():
            rev = sum(r.revenue for r in rows)
            mar = sum(r.margin for r in rows)
            out[seg] = SegmentSummary(
                customers=len(rows),
                contactable=sum(1 for r in rows if r.contactable_channels()) if contactable_known else None,
                avg_value=rev / len(rows) if rows else 0.0,
                margin_rate=min(max(mar / rev, 0.0), 1.0) if rev > 0 and mar > 0 else None,
            )
        return out

    analyst = Analyst(llm=None)  # type: ignore[arg-type]  # opportunities() never calls the model
    analyst_rows: dict[str, Any] = {}
    for label, known in (("contactable_unknown", False), ("contactable_from_state", True)):
        goals = analyst.opportunities(summary(known), now, business_id="realdata", currency=currency)
        analyst_rows[label] = [{"kind": g.kind.value, "text": g.text, "budget": g.budget} for g in goals]

    from autonomy.contracts import Audience

    templates = {
        k.value: sum(
            1 for s in states if matches(s, Audience(description=t.description, filters=t.filters), now)
        )
        for k, t in TEMPLATES.items()
    }
    api_aud = {k.value: sum(1 for s in states if matches(s, a, now)) for k, a in _AUDIENCES.items()}
    return {
        "api_suggest_opportunities": api_rows,
        "analyst_opportunities": analyst_rows,
        "strategist_template_audience_sizes": templates,
        "api_audience_sizes": api_aud,
        "segment_summary": {k: v.model_dump() for k, v in summary(True).items()},
    }


def warm_start_inputs(events: list[Event], states: list[CustomerState]) -> dict[str, Any]:
    from autonomy.decide.models import ValueModel
    from autonomy.learning.observations import logged_sends

    history: dict[str, list[Event]] = {}
    for e in events:
        history.setdefault(e.customer_id, []).append(e)
    sends = logged_sends(history, {})
    aov = [s.avg_order_value for s in states if s.purchases > 0]
    vm = ValueModel()
    return {
        "event_types": dict(Counter(e.type.value for e in events).most_common()),
        "logged_sends": len(sends),
        "logged_sends_delivered": sum(1 for s in sends if s.delivered),
        "logged_clicks": sum(1 for s in sends if s.clicked),
        "default_order_value_prior": vm.default_order_value,
        "default_margin_rate_prior": vm.default_margin_rate,
        "median_customer_aov": round(float(np.median(aov)), 2) if aov else None,
        "aov_over_prior": round(float(np.median(aov)) / vm.default_order_value, 2) if aov else None,
    }


# --------------------------------------------------------------------------- main


def run(name: str, *, http: bool, sql: bool, use_v1: bool, trace: bool) -> dict[str, Any]:
    clock = Clock(tracing=trace)
    t0 = time.perf_counter()
    a = adapt(name, clock)
    payload: dict[str, Any] = {
        "dataset": name,
        "command": command_line(),
        "env": env_info(),
        "currency": a.currency,
        "timezone": a.timezone,
        "synthetic": a.synthetic,
        "notes": a.notes,
        "cannot_without_code": a.cannot,
        "facts": a.facts,
        "mapping": {
            "order_rows_in": a.order_rows,
            "order_events": None if a.order_events is None else len(a.order_events),
            "order_problems": a.order_problems,
            "line_events": a.line_rows,
            "line_problems": a.line_problems,
            "mappings": a.mappings,
        },
        "baskets": basket_stats(a),
    }
    snapshot = decision_moment(a.order_events, a.timezone) if a.order_events else None
    with clock.step("doctor"):
        payload["doctor"] = [run_doctor(d, snapshot, use_v1=use_v1) for d in a.doctor]
    if http and a.doctor:
        payload["doctor_http"] = [
            run_doctor_http(d, snapshot) for d in a.doctor if d.as_published != "xlsx (two sheets)"
        ]
    if a.order_events:
        now = snapshot
        assert now is not None
        states, store_t = rebuild(a.order_events, now, clock, sql=sql)
        payload["rebuild"] = store_t
        with clock.step("summaries"):
            payload["state"] = summarise_states(states, a.order_events, now)
            payload["goals"] = goal_suggestions(states, now, a.currency)
            payload["warm_start_inputs"] = warm_start_inputs(a.order_events, states)
    payload["steps"] = clock.as_list()
    payload["total_seconds"] = round(time.perf_counter() - t0, 2)
    payload["peak_rss_mb"] = round(peak_rss_mb(), 1)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datasets", nargs="*")
    parser.add_argument("--all", action="store_true", help="every family-B dataset, one process each")
    parser.add_argument("--http", action="store_true", help="also post each file to the real route")
    parser.add_argument("--sql", action="store_true", help="also time SQLite stores")
    parser.add_argument("--no-v1", action="store_true", help="skip v1's Indian-identifier enrichment")
    parser.add_argument(
        "--trace", action="store_true", help="tracemalloc the mapping and rebuild steps (slow)"
    )
    args = parser.parse_args(argv)
    if args.all:
        flags = [f for f, on in (("--http", args.http), ("--sql", args.sql), ("--no-v1", args.no_v1)) if on]
        code = 0
        for name in FAMILY_B:
            print(f"== {name}", flush=True)
            code |= subprocess.call([sys.executable, "-P", __file__, name, *flags])
        return code
    for name in args.datasets:
        payload = run(name, http=args.http, sql=args.sql, use_v1=not args.no_v1, trace=args.trace)
        path = write_result("ingest", name, payload)
        print(f"{name}: wrote {path} ({payload['total_seconds']} s, peak RSS {payload['peak_rss_mb']} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
