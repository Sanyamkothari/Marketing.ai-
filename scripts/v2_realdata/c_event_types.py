"""Family C: which event types Autonomy uses, which it ignores, and what event-stream files it can read.

Checks, in order (each result is written to the JSON):

1. **RetailRocket as published** (first 200,000 rows): the mapping a customer would write first
   (`c_adapters.RR_MAPPING_AS_PUBLISHED`), then with the time fixed but no amount on transactions.
   Problem counts per code.
2. **RetailRocket adapted, every row** (transactions collapsed to one purchase per transaction id,
   so `purchases` counts orders, not item lines): event types in, events out, and what the production
   `StateFolder` keeps from views and carts (per state field, how many states it set), segments, how
   many visitors ever buy and how many buy twice.
3. **The cart trigger in the real runtime**: RetailRocket's visitors active in its last 3 days (all
   their events, consent invented and labelled), one cart-recovery goal from the Strategist's
   template, Shadow mode, one tick at 10:00 the day after the data ends. Decisions by trigger
   (`event` = a cart in the trigger window; `sweep` = the daily audience), and whether a viewer with
   no cart is in the audience.
4. **Taobao subset**: no time column; `map_frame` raises `MappingError`.
5. **REES46**: the parquet samples (`pd.read_parquet` error), the 170-row CSV sample with ISO parsing
   (the ` UTC` suffix) and with an explicit format, and a `remove_from_cart` row (inline, one row,
   labelled: the tiny sample has none).
6. **Where each event type is read in the code** (grep of `autonomy/` for `EventType.<X>`), so the
   "used / ignored" table in the notes is checkable.

Run:

    python3 -P scripts/v2_realdata/c_event_types.py

Result: /home/user/datasets/_results/c/event_types/event_types.json
"""

from __future__ import annotations

import re
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
from c_adapters import (  # noqa: E402
    REES46_MAPPING,
    REES46_MAPPING_ISO,
    REES46_PARQUET,
    RR_MAPPING,
    RR_MAPPING_AS_PUBLISHED,
    RR_MAPPING_ORDERS,
    TAOBAO_MAPPING,
    adapt_retailrocket,
    read_rees46_csv,
    read_rees46_parquet,
    read_retailrocket,
    read_taobao,
)
from c_common import REPO, Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.agents.strategist import BusinessFacts, template_brief  # noqa: E402
from autonomy.contracts import (  # noqa: E402
    AutonomyLevel,
    Channel,
    CustomerState,
    Event,
    EventType,
    Goal,
    GoalKind,
    GoalStatus,
    stable_id,
)
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.mapping import MappingError, map_frame, problem_counts  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import (  # noqa: E402
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_content  # noqa: E402
from autonomy.proof.split import group_assigner  # noqa: E402
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, Trigger, _decision_id  # noqa: E402
from autonomy.runtime.settings import workspace_policy_factory  # noqa: E402


def as_published(clock: Clock) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with clock.step("rr_as_published"):
        frame = read_retailrocket(200_000)
        _, problems = map_frame(frame, RR_MAPPING_AS_PUBLISHED, "retailrocket")
        out["as_published"] = {
            "rows": len(frame),
            "problems": problem_counts(problems),
            "example": problems[0].model_dump() if problems else None,
        }
        fixed = adapt_retailrocket(frame)
        no_value = RR_MAPPING.model_copy(update={"value_column": None})
        events, problems = map_frame(fixed, no_value, "retailrocket")
        ex = next((p for p in problems if p.code == "invalid_event"), None)
        out["time_fixed_no_amount"] = {
            "events": len(events),
            "problems": problem_counts(problems),
            "example": ex.model_dump() if ex else None,
            "transactions_in_rows": int((frame["event"] == "transaction").sum()),
        }
    return out


def full_stream(clock: Clock, rows: int | None) -> tuple[dict[str, Any], list[Event]]:
    with clock.step("rr_full_map") as s:
        raw = read_retailrocket(rows)
        rows_by_type = dict(raw["event"].value_counts())
        # One purchase per transaction id (an order), not per item line: `purchases` counts orders.
        frame = adapt_retailrocket(raw, grain="order")
        del raw
        events, problems = map_frame(frame, RR_MAPPING_ORDERS, "retailrocket")
        s.extra["events"] = len(events)
    del frame
    with clock.step("rr_full_fold") as s:
        store = InMemoryEventStore()
        store.append(events)
        now = max(e.ts for e in events) + timedelta(days=1)
        states = InMemoryStateStore()
        rebuild_states(store, states, StateFolder(), now)
        state_rows = list(states.scan())
        s.extra["states"] = len(state_rows)
    by_type = Counter(e.type.value for e in events)
    # Per customer, which event types they have
    kinds: dict[str, set[str]] = {}
    for e in events:
        kinds.setdefault(e.customer_id, set()).add(e.type.value)
    only_views = sum(1 for k in kinds.values() if k == {"view"})
    with_cart = sum(1 for k in kinds.values() if "cart" in k)
    cart_no_buy = sum(1 for k in kinds.values() if "cart" in k and "purchase" not in k)
    buyers = [r for r in state_rows if r.purchases > 0]
    default = CustomerState(customer_id="x", as_of=now)
    set_fields = {
        f: sum(1 for r in state_rows if getattr(r, f) != getattr(default, f))
        for f in (
            "last_seen_at",
            "first_seen_at",
            "last_purchase_at",
            "purchases",
            "revenue",
            "top_categories",
            "category_counts",
            "channels",
            "active_hours",
            "hour_counts",
            "country",
            "timezone",
        )
    }
    out = {
        "rows_by_type": {str(k): int(v) for k, v in rows_by_type.items()},
        "events_by_type": dict(by_type),
        "problems": problem_counts(problems),
        "states": len(state_rows),
        "segments": dict(Counter(r.segment for r in state_rows).most_common()),
        "customers_only_views": only_views,
        "customers_with_cart": with_cart,
        "customers_cart_never_bought": cart_no_buy,
        "buyers": len(buyers),
        "buyers_twice_or_more": sum(1 for r in buyers if r.purchases >= 2),
        "state_fields_set": set_fields,
        "item_ids_on_views_and_carts": sum(
            1 for e in events if e.item_id and e.type in (EventType.VIEW, EventType.CART)
        ),
        "data_end": max(e.ts for e in events),
        "grain": "order: one purchase event per transaction id (item lines collapsed)",
        "note": "revenue is 0 for every buyer: transactions carry a 0.0 placeholder amount",
    }
    return out, events


def cart_trigger(clock: Clock, events: list[Event]) -> dict[str, Any]:
    end = max(e.ts for e in events)
    recent = {e.customer_id for e in events if e.ts >= end - timedelta(days=3)}
    mine = [e for e in events if e.customer_id in recent]
    first: dict[str, Event] = {}
    for e in mine:
        if e.customer_id not in first or e.ts < first[e.customer_id].ts:
            first[e.customer_id] = e
    consent = [
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
    now = (end + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    if now - end > timedelta(days=1):
        now = (end).replace(hour=23, minute=59, second=0, microsecond=0)
    ws = InMemoryWorkspace()
    g = Goal(
        goal_id="goal_cart_recovery",
        business_id="retailrocket",
        text="recover abandoned carts",
        kind=GoalKind.CART_RECOVERY,
        budget=10_000.0,
        starts_at=now - timedelta(days=2),
        created_at=now - timedelta(days=2),
        autonomy=AutonomyLevel.SHADOW,
        status=GoalStatus.ACTIVE,
    )
    facts = BusinessFacts(business_id="retailrocket", channels=(Channel.EMAIL,), offers=(TEN_OFF, FREE_SHIP))
    b = template_brief(g, facts, now - timedelta(days=2), kind=GoalKind.CART_RECOVERY)
    b = b.model_copy(update={"approved_by": "c-runner", "approved_at": now - timedelta(days=2)})
    ws.put_goal(g)
    ws.put_brief(b)
    ws.put_content(
        [
            c.model_copy(update={"content_id": f"{g.goal_id}_{c.content_id}"})
            for c in sample_content(now - timedelta(days=2), g.goal_id)
            if c.channel is Channel.EMAIL
        ]
    )
    store = InMemoryEventStore()
    store.append(consent + mine)
    rt = AutonomyRuntime(
        events=store,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=ws,
        folder=StateFolder(),
        policy_factory=workspace_policy_factory(ws),
        assign_group=group_assigner("autonomy-split", holdout_share=0.1, baseline_share=0.1),
        config=RuntimeConfig(holdout_share=0.1, baseline_share=0.1, run_send_jobs=False),
    )
    with clock.step("cart_tick") as s:
        t0 = time.perf_counter()
        report = rt.tick(now)
        s.extra["decisions"] = len(report.decisions)
        tick_s = time.perf_counter() - t0
    carts_in_window = [
        e for e in mine if e.type is EventType.CART and now - rt.config.trigger_window <= e.ts <= now
    ]
    day = now.date().isoformat()
    sweep_ids = {_decision_id(g.goal_id, cid, Trigger.SWEEP, day) for cid in recent}
    trig = Counter("sweep" if d.decision_id in sweep_ids else "event" for d in report.decisions)
    kinds: dict[str, set[EventType]] = {}
    for e in mine:
        kinds.setdefault(e.customer_id, set()).add(e.type)
    sweep_customers = {d.customer_id for d in report.decisions if d.decision_id in sweep_ids}
    viewers_only_in_sweep = sum(1 for c in sweep_customers if kinds.get(c) == {EventType.VIEW})
    return {
        "now": now,
        "customers_active_last_3_days": len(recent),
        "events_loaded": len(mine),
        "consent_invented_for": len(consent),
        "audience": b.audience.description,
        "audience_filters": [f.model_dump() for f in b.audience.filters],
        "cart_events_in_trigger_window": len(carts_in_window),
        "customers_with_cart_in_window": len({e.customer_id for e in carts_in_window}),
        "decisions": len(report.decisions),
        "decisions_by_trigger": dict(trig),
        "decisions_by_group_kind": dict(Counter(f"{d.group.value}/{d.kind.value}" for d in report.decisions)),
        "sweep_customers_who_only_viewed": viewers_only_in_sweep,
        "sweep_customers": len(sweep_customers),
        "tick_seconds": round(tick_s, 2),
        "alerts": [f"{a.code}: {a.message[:200]}" for a in report.alerts],
    }


def other_files(clock: Clock) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with clock.step("taobao"):
        frame = read_taobao()
        out["taobao"] = {"rows": len(frame), "behaviour": dict(frame["behaviour"].value_counts())}
        try:
            map_frame(frame, TAOBAO_MAPPING, "taobao")
            out["taobao"]["result"] = "mapped"
        except MappingError as err:
            out["taobao"]["result"] = f"MappingError: {err}"[:400]
    with clock.step("rees46"):
        r: dict[str, Any] = {}
        for p in REES46_PARQUET:
            try:
                read_rees46_parquet(p)
                r[p.name] = "read"
            except ImportError as err:
                r[p.name] = f"ImportError: {err}"[:300]
        frame = read_rees46_csv()
        r["csv_rows"] = len(frame)
        r["csv_event_types"] = dict(frame["event_type"].value_counts())
        _, problems = map_frame(frame, REES46_MAPPING_ISO, "rees46")
        r["csv_iso"] = {
            "problems": problem_counts(problems),
            "example": problems[0].model_dump() if problems else None,
        }
        events, problems = map_frame(frame, REES46_MAPPING, "rees46")
        r["csv_with_format"] = {"events": len(events), "problems": problem_counts(problems)}
        extra = pd.DataFrame(
            [
                {
                    "event_time": "2019-10-01 00:05:00 UTC",
                    "event_type": "remove_from_cart",
                    "product_id": "1",
                    "category_code": "",
                    "price": "9.99",
                    "user_id": "1",
                }
            ]
        )
        _, problems = map_frame(extra, REES46_MAPPING, "rees46-inline")
        r["remove_from_cart_inline_row"] = problem_counts(problems)
        out["rees46"] = r
    return out


def code_readers() -> dict[str, list[str]]:
    """Files under autonomy/ (not the Gym or fakes) that name each customer-action event type."""
    out: dict[str, list[str]] = {}
    root = REPO / "autonomy"
    for name in ("VISIT", "VIEW", "CART", "PURCHASE", "REFUND", "SIGNUP"):
        pat = re.compile(rf"EventType\.{name}\b")
        hits = []
        for p in sorted(root.rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            if "/gym/" in rel or "/fakes/" in rel:
                continue
            n = sum(1 for line in p.read_text(encoding="utf-8").splitlines() if pat.search(line))
            if n:
                hits.append(f"{rel} ({n})")
        out[name] = hits
    return out


def tx_counts() -> int:
    """Transaction rows vs transaction ids on every RetailRocket row, and who bought more than once."""
    frame = read_retailrocket()
    tx = frame[frame["event"] == "transaction"]
    per_rows = tx.groupby("visitorid").size()
    per_ids = tx.groupby("visitorid")["transactionid"].nunique()
    payload = {
        "command": command_line(),
        "rows": len(frame),
        "transaction_rows": len(tx),
        "transaction_ids": int(tx["transactionid"].nunique()),
        "transaction_ids_spanning_several_timestamps": int(
            (tx.groupby("transactionid")["timestamp"].nunique() > 1).sum()
        ),
        "buyers": int(per_rows.size),
        "buyers_with_2plus_transaction_rows": int((per_rows >= 2).sum()),
        "buyers_with_2plus_transaction_ids": int((per_ids >= 2).sum()),
        "exact_duplicate_rows": int(frame.duplicated().sum()),
    }
    path = write_result("event_types", "tx_counts", payload)
    print(payload, f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rows", type=int, default=None, help="only the first N RetailRocket rows (a trial run)"
    )
    parser.add_argument(
        "--tx-counts",
        action="store_true",
        help="only count transaction rows, transaction ids and repeat buyers on every row (pandas, light)",
    )
    args = parser.parse_args(argv)
    if args.tx_counts:
        return tx_counts()
    clock = Clock()
    t0 = time.perf_counter()
    payload: dict[str, Any] = {"command": command_line(), "env": env_info()}
    payload["retailrocket_as_published"] = as_published(clock)
    full, events = full_stream(clock, args.rows)
    full["rows_read"] = args.rows or "all"
    payload["retailrocket_full"] = full
    payload["cart_trigger"] = cart_trigger(clock, events)
    del events
    payload.update(other_files(clock))
    payload["code_readers"] = code_readers()
    payload["steps"] = clock.as_dict()
    payload["total_seconds"] = round(time.perf_counter() - t0, 2)
    payload["peak_rss_mb"] = round(peak_rss_mb(), 1)
    # "_order": transactions collapsed to one purchase per transaction id. The earlier files without
    # the suffix (event_types.json, event_types_300000.json) were made at item-line grain.
    path = write_result(
        "event_types", "event_types" + (f"_{args.rows}" if args.rows else "") + "_order", payload
    )
    print(f"wrote {path}: {payload['total_seconds']} s, peak RSS {payload['peak_rss_mb']} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
