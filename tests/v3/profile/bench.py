"""Measure the nightly profile at scale: wall time and peak memory of `build_profiles`.

    python -m tests.v3.profile.bench --customers 100000

Two processes, so the numbers are the build's own:

1. this one writes synthetic shop-like events (`tests.v3.profile.synthetic`) into a SQLite file with
   `SqlEventStore`, then folds them into one customer state each with the production `StateFolder`
   (`rebuild_states`) into the same file's `SqlStateStore`. SQLite is the closest stand-in here for
   the Postgres stores of a real deployment;
2. a fresh child process opens that file and runs `build_profiles` once, while a thread samples its
   resident memory (RSS) every 10 ms.

It prints one JSON line:

- `load_s`, `fold_s`: time to fill the database (not part of the profile's cost);
- `build_s`, `customers_per_s`: the profile build itself, the shop price pass included;
- `rss_before_mb`, `rss_peak_mb`, `rss_growth_mb`: the child's RSS just before the build (Python,
  pydantic, SQLAlchemy and the open file), its peak during it, and the difference;
- `compute_us_per_customer`: `profile_for` alone on 2,000 customers' events already in memory.

Nothing in this module is imported by the product.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine

from autonomy.contracts import Event
from autonomy.data import StateFolder, rebuild_states
from autonomy.profile import build_profiles, profile_for
from autonomy.store import SqlEventStore, SqlStateStore, create_all
from tests.v3.profile.synthetic import customer_events, customer_id, events

AS_OF = datetime(2026, 1, 31, tzinfo=UTC)
_PAGE = os.sysconf("SC_PAGE_SIZE")


def rss_mb() -> float:
    """This process's resident memory now, in MB."""
    return int(Path("/proc/self/statm").read_text().split()[1]) * _PAGE / 1e6


class PeakRss:
    """Samples RSS on a thread; `peak` is the largest value seen between enter and exit."""

    def __init__(self, every: float = 0.01) -> None:
        self.peak = rss_mb()
        self._every = every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._every):
            self.peak = max(self.peak, rss_mb())

    def __enter__(self) -> PeakRss:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, rss_mb())


def prepare(db: Path, customers: int) -> dict[str, float | int]:
    """Fill the database: events, then one folded state per customer."""
    engine = create_engine(f"sqlite:///{db}")
    create_all(engine)
    store, states = SqlEventStore(engine), SqlStateStore(engine)
    t = time.perf_counter()
    chunk: list[Event] = []
    n_events = 0
    for e in events(customers):
        chunk.append(e)
        if len(chunk) >= 20_000:
            n_events += store.append(chunk)
            chunk = []
    n_events += store.append(chunk)
    load_s = time.perf_counter() - t
    t = time.perf_counter()
    rebuild_states(store, states, StateFolder(), AS_OF)
    fold_s = time.perf_counter() - t
    engine.dispose()
    return {
        "customers": customers,
        "events": n_events,
        "load_s": round(load_s, 1),
        "fold_s": round(fold_s, 1),
    }


def build(db: Path, customers: int, batch: int) -> dict[str, float | int]:
    """The measured part, run in a fresh process."""
    engine = create_engine(f"sqlite:///{db}")
    store, states = SqlEventStore(engine), SqlStateStore(engine)
    before = rss_mb()
    with PeakRss() as peak:
        t = time.perf_counter()
        written = build_profiles(store, states, AS_OF, batch_size=batch)
        build_s = time.perf_counter() - t
    first = states.get(customer_id(0))
    assert first is not None and first.features_as_of == AS_OF
    step = max(customers // 2000, 1)
    sample = [customer_events(i) for i in range(0, customers, step)][:2000]
    t = time.perf_counter()
    for evs in sample:
        profile_for(evs, AS_OF)
    compute_us = (time.perf_counter() - t) / len(sample) * 1e6
    return {
        "batch": batch,
        "build_s": round(build_s, 1),
        "customers_per_s": round(written / build_s),
        "compute_us_per_customer": round(compute_us),
        "rss_before_mb": round(before),
        "rss_peak_mb": round(peak.peak),
        "rss_growth_mb": round(peak.peak - before),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customers", type=int, default=100_000)
    ap.add_argument("--batch", type=int, default=2_000)
    ap.add_argument("--db", type=Path, help="reuse (or create) this SQLite file instead of a temporary one")
    ap.add_argument("--build-only", action="store_true", help="internal: the measured child process")
    args = ap.parse_args()
    if args.build_only:
        print(json.dumps(build(args.db, args.customers, args.batch)))
        return
    with tempfile.TemporaryDirectory() as tmp:
        db = args.db or Path(tmp) / "bench.db"
        info: dict[str, float | int] = {}
        if not (args.db and args.db.exists()):
            info = prepare(db, args.customers)
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.v3.profile.bench",
                "--build-only",
                "--db",
                str(db),
                "--customers",
                str(args.customers),
                "--batch",
                str(args.batch),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        print(json.dumps({**info, **json.loads(child.stdout.strip().splitlines()[-1])}))


if __name__ == "__main__":
    main()
