"""Shared helpers for the family-C (event streams and scale) runners: timing, memory, JSON output.

Not a runner itself. `c_ingest.py`, `c_sweep.py`, `c_harm.py`, `c_event_types.py` and `c_report.py` import it.
Results go to `/home/user/datasets/_results/c/` (outside the repository: derived from the data, and
the data is never committed). Scratch SQLite files go to `/home/user/datasets/_results/c/tmp/` and are
deleted after each run unless `--keep-db` is given.

Memory is measured two ways:

* `peak_rss_mb()`: the process's peak resident set (resource.getrusage), the number that decides
  whether a job fits the machine. Each size runs in its own fresh process so each peak is its own.
* `tracemalloc` (only with `--trace`): Python allocations during one step. It slows pure-Python code
  several times over, so timings in the notes come from untraced runs.

Python 3.11 compatible; numpy and pandas only, plus the repository's own packages.
"""

from __future__ import annotations

import json
import math
import os
import resource
import sys
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DATASETS = Path(os.environ.get("AUTONOMY_DATASETS", "/home/user/datasets"))
RESULTS = DATASETS / "_results" / "c"
TMP = RESULTS / "tmp"


def peak_rss_mb() -> float:
    """Peak resident memory of this process so far, in MB (Linux reports KB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def current_rss_mb() -> float:
    """Resident memory right now, from /proc (Linux), in MB."""
    try:
        with Path("/proc/self/statm").open(encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
    except OSError:
        return float("nan")


@dataclass
class Step:
    name: str
    seconds: float = 0.0
    peak_rss_mb: float = 0.0
    rss_after_mb: float = 0.0
    traced_peak_mb: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Clock:
    """Collects `Step`s: `with clock.step("rebuild") as s: ...; s.extra["n"] = 3`."""

    def __init__(self, *, tracing: bool = False, verbose: bool = True) -> None:
        self.steps: list[Step] = []
        self.tracing = tracing
        self.verbose = verbose

    @contextmanager
    def step(self, name: str, *, trace: bool = True) -> Iterator[Step]:
        s = Step(name)
        trace = trace and self.tracing
        if trace:
            tracemalloc.start()
        t0 = time.perf_counter()
        try:
            yield s
        finally:
            s.seconds = round(time.perf_counter() - t0, 3)
            if trace:
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                s.traced_peak_mb = round(peak / 1e6, 1)
            s.peak_rss_mb = round(peak_rss_mb(), 1)
            s.rss_after_mb = round(current_rss_mb(), 1)
            self.steps.append(s)
            if self.verbose:
                print(
                    f"  [{name}] {s.seconds:.2f} s, peak RSS {s.peak_rss_mb:.0f} MB, now {s.rss_after_mb:.0f} MB"
                    + (f", traced {s.traced_peak_mb} MB" if s.traced_peak_mb is not None else "")
                    + (f" {s.extra}" if s.extra else ""),
                    flush=True,
                )

    def as_dict(self) -> dict[str, Any]:
        return {s.name: asdict(s) for s in self.steps}


def jsonable(obj: Any) -> Any:
    """Plain JSON types from dataclasses, pydantic models, numpy scalars, datetimes and NaN."""
    if obj is None or isinstance(obj, bool | str | int):
        return obj
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else round(obj, 6)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return jsonable(obj.model_dump(mode="json"))
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [jsonable(v) for v in obj]
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(obj)


def write_result(stage: str, name: str, payload: dict[str, Any]) -> Path:
    """Write `<RESULTS>/<stage>/<name>.json` and return its path."""
    out = RESULTS / stage / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jsonable(payload), indent=2) + "\n", encoding="utf-8")
    return out


def command_line() -> str:
    return "python3 -P " + " ".join(sys.argv)


def env_info() -> dict[str, Any]:
    import numpy as np
    import pandas as pd
    import sqlalchemy

    mem_total = mem_avail = None
    try:
        with Path("/proc/meminfo").open(encoding="ascii") as fh:
            info = {line.split(":")[0]: int(line.split()[1]) for line in fh}
        mem_total, mem_avail = info["MemTotal"] // 1024, info["MemAvailable"] // 1024
    except (OSError, KeyError, ValueError):
        pass
    import sqlite3

    return {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "sqlalchemy": sqlalchemy.__version__,
        "sqlite": sqlite3.sqlite_version,
        "cpus": os.cpu_count(),
        "mem_total_mb": mem_total,
        "mem_available_mb_at_start": mem_avail,
        "load_average_1_5_15": [round(x, 2) for x in os.getloadavg()],
        "note": "shared machine: other runners may have used the CPUs, so wall times are upper bounds",
    }


def sqlite_engine(path: Path):  # type: ignore[no-untyped-def]  # noqa: ANN201
    """A file-backed SQLite engine as a laptop install would use it (default pragmas, no tuning)."""
    from sqlalchemy import create_engine

    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    return create_engine(f"sqlite:///{path}")


def file_mb(path: Path) -> float:
    return round(path.stat().st_size / 1e6, 1) if path.exists() else 0.0
