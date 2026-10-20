"""Family C, scale 1: ingestion and customer-state rebuild on RetailRocket's real event stream.

RetailRocket (2,756,101 view / add-to-cart / transaction rows, 1,407,580 visitors) is the largest
real event stream we can read here. For the first N rows of the file this measures, in one fresh
process per (size, backend):

1. read (pandas) and the adapter (`c_adapters.adapt_retailrocket`: epoch ms -> aware time, a 0.0
   placeholder amount on transactions);
2. `map_frame` (the product's mapping) to `Event`s;
3. append to the event store, then `rebuild_states` with the production `StateFolder`:
   * `memory`: `InMemoryEventStore` / `InMemoryStateStore` (what the Gym and tests use);
   * `sqlite`: `SqlEventStore` / `SqlStateStore` on a file-backed SQLite database with default
     settings (what a laptop install would use; production is Postgres, not measured here). Rows are
     read, mapped and appended in chunks of `--chunk` rows, so the events never all sit in memory.
     It also times a plain `scan()` of the log, 200 `for_customer` and 200 `states.get` lookups,
     and an incremental `apply_events` of the last 1% of events (the per-tick path).

Wall time and peak RSS per step are written to
`/home/user/datasets/_results/c/ingest/retailrocket_<N>_<backend>.json`.

Run:

    python3 -P scripts/v2_realdata/c_ingest.py --rows 10000 --backend memory
    python3 -P scripts/v2_realdata/c_ingest.py --curve 10000,100000,1000000,all --backend memory,sqlite
"""

from __future__ import annotations

import argparse
import random
import subprocess
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

import c_adapters as A  # noqa: E402, N812
from c_common import (  # noqa: E402
    TMP,
    Clock,
    command_line,
    env_info,
    file_mb,
    peak_rss_mb,
    sqlite_engine,
    write_result,
)

from autonomy.contracts import Event  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.mapping import map_frame, problem_counts  # noqa: E402
from autonomy.data.rebuild import apply_events, rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402


def _segments(states: Any) -> dict[str, int]:
    return dict(Counter(s.segment for s in states.scan()).most_common())


def run_memory(rows: int | None, clock: Clock) -> dict[str, Any]:
    with clock.step("read") as s:
        frame = A.read_retailrocket(rows)
        s.extra["rows"] = len(frame)
    with clock.step("adapt"):
        frame = A.adapt_retailrocket(frame)
    with clock.step("map_frame") as s:
        events, problems = map_frame(frame, A.RR_MAPPING, "retailrocket")
        s.extra.update(events=len(events), problems=problem_counts(problems))
    n_rows = len(frame)
    del frame
    with clock.step("append") as s:
        store = InMemoryEventStore()
        s.extra["new"] = store.append(events)
    del events
    now = max(e.ts for e in store.scan()) + timedelta(days=1)
    with clock.step("rebuild_states") as s:
        states = InMemoryStateStore()
        s.extra["states"] = rebuild_states(store, states, StateFolder(), now)
    return {"rows": n_rows, "events": len(store), "states": s.extra["states"], "segments": _segments(states)}


def run_sqlite(rows: int | None, clock: Clock, chunk: int, keep_db: bool) -> dict[str, Any]:
    from sqlalchemy import text

    from autonomy.store import create_all
    from autonomy.store.sql import SqlEventStore, SqlStateStore

    db = TMP / f"ingest_{rows or 'all'}.sqlite"
    engine = sqlite_engine(db)
    create_all(engine)
    events_store = SqlEventStore(engine)
    total_rows = 0
    appended = 0
    problems: Counter[str] = Counter()
    last_events: list[Event] = []
    t_map = t_append = 0.0
    with clock.step("read_map_append") as s:
        import zipfile

        import pandas as pd

        with zipfile.ZipFile(A.RR_ZIP) as zf, zf.open(A.RR_MEMBER) as fh:
            reader = pd.read_csv(
                fh,
                nrows=rows,
                chunksize=chunk,
                dtype={"timestamp": "int64", "visitorid": "int64", "event": "string", "itemid": "int64"},
            )
            for part in reader:
                total_rows += len(part)
                t0 = time.perf_counter()
                # Row labels carry on across chunks (pandas keeps the file position), but the row key
                # is the four columns, so ids do not depend on the chunk size.
                evs, probs = map_frame(A.adapt_retailrocket(part), A.RR_MAPPING, "retailrocket")
                t1 = time.perf_counter()
                appended += events_store.append(evs)
                t2 = time.perf_counter()
                t_map += t1 - t0
                t_append += t2 - t1
                problems.update(p.code for p in probs)
                last_events = evs
        s.extra.update(
            rows=total_rows,
            appended=appended,
            map_seconds=round(t_map, 2),
            append_seconds=round(t_append, 2),
            problems=dict(problems),
            db_mb=file_mb(db),
        )
    with clock.step("count") as s:
        s.extra["events"] = events_store.count()
    with clock.step("scan_only") as s:
        n = 0
        newest = None
        for e in events_store.scan():
            n += 1
            newest = e.ts
        s.extra["scanned"] = n
    assert newest is not None
    now = newest + timedelta(days=1)
    states = SqlStateStore(engine)
    with clock.step("rebuild_states") as s:
        s.extra["states"] = rebuild_states(events_store, states, StateFolder(), now)
        s.extra["db_mb"] = file_mb(db)
    rng = random.Random(7)
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(text("SELECT customer_id FROM customer_state LIMIT 200000"))]
    sample = rng.sample(ids, min(200, len(ids)))
    with clock.step("for_customer_x200") as s:
        got = sum(len(events_store.for_customer(c)) for c in sample)
        s.extra["events_read"] = got
    with clock.step("states_get_x200") as s:
        s.extra["found"] = sum(1 for c in sample if states.get(c) is not None)
    tail = last_events[-max(1, len(last_events) // 100) :] if last_events else []
    with clock.step("apply_events_tail") as s:
        # The per-tick path folds only new events into stored states. These events are already
        # folded by the rebuild, so this times the path, not a correct second fold.
        s.extra["events"] = len(tail)
        s.extra["customers"] = apply_events(tail, states, StateFolder(), now=now)
    with clock.step("states_scan") as s:
        s.extra["states"] = sum(1 for _ in states.scan())
    out = {
        "rows": total_rows,
        "events": appended,
        "states": states.count(),
        "db_file_mb": file_mb(db),
        "db_bytes_per_event": round(db.stat().st_size / max(appended, 1), 1),
    }
    engine.dispose()
    if not keep_db:
        db.unlink(missing_ok=True)
    return out


def one(rows: int | None, backend: str, chunk: int, keep_db: bool, trace: bool) -> Path:
    clock = Clock(tracing=trace)
    t0 = time.perf_counter()
    print(f"== retailrocket rows={rows or 'all'} backend={backend}", flush=True)
    summary = run_memory(rows, clock) if backend == "memory" else run_sqlite(rows, clock, chunk, keep_db)
    payload = {
        "dataset": "retailrocket",
        "rows_requested": rows or "all",
        "backend": backend,
        "command": command_line(),
        "env": env_info(),
        "summary": summary,
        "steps": clock.as_dict(),
        "total_seconds": round(time.perf_counter() - t0, 2),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "notes": [
            "Transactions carry a 0.0 placeholder amount (RetailRocket has no amounts).",
            "First N rows in file order, which is not time order: a customer's history may be partial.",
        ],
    }
    path = write_result("ingest", f"retailrocket_{rows or 'all'}_{backend}", payload)
    print(f"wrote {path}: {payload['total_seconds']} s, peak RSS {payload['peak_rss_mb']} MB", flush=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", default="10000", help="row count or 'all'")
    parser.add_argument("--curve", default=None, help="comma-separated sizes, each in a fresh process")
    parser.add_argument("--backend", default="memory", help="memory, sqlite, or both comma-separated")
    parser.add_argument("--chunk", type=int, default=100_000)
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args(argv)
    backends = args.backend.split(",")
    if args.curve or len(backends) > 1:
        code = 0
        for size in (args.curve or args.rows).split(","):
            for b in backends:
                extra = ["--keep-db"] if args.keep_db else []
                code |= subprocess.call(
                    [
                        sys.executable,
                        "-P",
                        __file__,
                        "--rows",
                        size,
                        "--backend",
                        b,
                        "--chunk",
                        str(args.chunk),
                        *extra,
                    ]
                )
        return code
    rows = None if args.rows == "all" else int(args.rows)
    one(rows, backends[0], args.chunk, args.keep_db, args.trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
