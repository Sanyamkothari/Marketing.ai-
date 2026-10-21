"""Family C probe: why `SqlEventStore.scan` is quadratic on SQLite, and whether the proposed fix helps.

Appends N synthetic VIEW events (one second apart, 50,000 customers) to a fresh file-backed SQLite
database through the product's `SqlEventStore`, then:

1. times the product's `scan()` per 100,000 events (later pages should be slower if each page
   walks the index from the start);
2. captures the keyset SQL the store sends and prints SQLite's `EXPLAIN QUERY PLAN` for it with
   bound parameters, with the same values inlined, with a leading `ts >= ?` conjunct (the fix), and
   as a row-value comparison;
3. pages through the table with the original and the fixed SQL directly (raw rows, no pydantic), so
   the SQL cost is measured apart from `Event.model_validate`.

The product code is not changed: the fixed query is only run here. Synthetic rows: this measures
the store, not any dataset.

Historical: this probe string-patches the keyset SQL of the store as it was at commit f55c57b3. The
real-data fix wave put the fix in the store (`autonomy.store.sql._after`, index `ix_event_order`), so
against today's store step 3 no longer builds a valid query; run it on a f55c57b3 checkout to
reproduce docs/v2/real_data/C-scale.md section 4.

Run:

    python3 -P scripts/v2_realdata/c_sqlscan.py --events 400000

Result: /home/user/datasets/_results/c/sqlscan/sqlscan_<N>.json
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from c_common import TMP, command_line, env_info, sqlite_engine, write_result  # noqa: E402
from sqlalchemy import event as sa_event  # noqa: E402

from autonomy.contracts import Event, EventType  # noqa: E402
from autonomy.store import create_all  # noqa: E402
from autonomy.store.sql import SqlEventStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", type=int, default=400_000)
    args = parser.parse_args(argv)
    n = args.events
    db = TMP / f"sqlscan_{n}.sqlite"
    engine = sqlite_engine(db)
    create_all(engine)
    store = SqlEventStore(engine)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    start = time.perf_counter()
    for k in range(0, n, 50_000):
        store.append(
            Event(
                event_id=f"e{i:08d}",
                customer_id=f"c{i % 50_000}",
                type=EventType.VIEW,
                ts=t0 + timedelta(seconds=i),
                source="c-sqlscan-synthetic",
            )
            for i in range(k, min(n, k + 50_000))
        )
    append_s = time.perf_counter() - start
    seen: list[tuple[str, Any]] = []

    @sa_event.listens_for(engine, "before_cursor_execute")
    def _grab(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202, ARG001
        if "event.ts >" in statement and not seen:
            seen.append((statement, parameters))

    start = last = time.perf_counter()
    count, per_100k = 0, []
    for _ in store.scan():
        count += 1  # noqa: SIM113
        if count % 100_000 == 0:
            now = time.perf_counter()
            per_100k.append(round(now - last, 2))
            last = now
    scan_s = time.perf_counter() - start
    statement, params = seen[0]
    limit = params[-2]  # ... LIMIT ? OFFSET ?
    fixed = statement.replace("WHERE ", "WHERE event.ts >= ? AND (", 1).replace(" ORDER BY", ") ORDER BY", 1)
    rowv = re.sub(
        r"WHERE .* ORDER BY",
        "WHERE (event.ts, event.seq, event.event_id) > (?, ?, ?) ORDER BY",
        statement,
        flags=re.S,
    )
    first = (
        "SELECT event.ts, event.seq, event.event_id, event.body FROM event "
        "ORDER BY event.ts, event.seq, event.event_id LIMIT ? OFFSET 0"
    )
    with engine.connect() as c:

        def plan(q: str, p: Any = ()) -> str:
            return " | ".join(str(r[3]) for r in c.exec_driver_sql("EXPLAIN QUERY PLAN " + q, p))

        literal = statement
        for v in params:
            literal = literal.replace("?", repr(v) if isinstance(v, str) else str(v), 1)
        plans = {
            "bound (as the store sends it)": plan(statement, params),
            "literal values": plan(literal),
            "fixed: leading ts >= ?": plan(fixed, (params[0], *params)),
            "row value": plan(rowv, (params[0], params[2], params[4], params[-2], params[-1])),
        }

        def page(sql: str, args_of) -> float:  # type: ignore[no-untyped-def]  # noqa: ANN001
            t = time.perf_counter()
            rows = c.exec_driver_sql(first, (limit,)).all()
            while len(rows) == limit:
                ts, seq, eid = rows[-1][0], rows[-1][1], rows[-1][2]
                rows = c.exec_driver_sql(sql, args_of(ts, seq, eid)).all()
            return round(time.perf_counter() - t, 2)

        raw_original = page(statement, lambda ts, seq, eid: (ts, ts, seq, seq, eid, limit, 0))
        raw_fixed = page(fixed, lambda ts, seq, eid: (ts, ts, ts, seq, seq, eid, limit, 0))
    payload = {
        "events": n,
        "command": command_line(),
        "env": env_info(),
        "synthetic": "synthetic VIEW events; measures the store only",
        "append_seconds": round(append_s, 1),
        "product_scan_seconds": round(scan_s, 2),
        "product_scan_seconds_per_100k": per_100k,
        "keyset_sql": re.sub(r"\s+", " ", statement),
        "plans": plans,
        "raw_paging_seconds_original_sql": raw_original,
        "raw_paging_seconds_fixed_sql": raw_fixed,
    }
    engine.dispose()
    db.unlink(missing_ok=True)
    path = write_result("sqlscan", f"sqlscan_{n}", payload)
    print({k: v for k, v in payload.items() if k not in ("env", "command")}, f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
