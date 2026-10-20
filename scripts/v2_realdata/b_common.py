"""Shared helpers for the family-B (transactions) real-data runners: timing, memory, JSON output.

Not a runner itself. Every runner (`b_ingest.py`, `b_catalogue.py`, `b_shadow.py`, `b_scale.py`)
imports this and the adapters in `b_adapters.py`. Results go to `/home/user/datasets/_results/b/`
(outside the repository: they are derived from the data, and the data is never committed).

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
RESULTS = DATASETS / "_results" / "b"


def peak_rss_mb() -> float:
    """Peak resident memory of this process so far, in MB (Linux reports KB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


@dataclass
class Step:
    """One measured step: wall seconds, the process's peak RSS after it, and (optionally) the
    tracemalloc peak during it (Python allocations only; numpy buffers are included, C extensions'
    private arenas are not)."""

    name: str
    seconds: float = 0.0
    peak_rss_mb: float = 0.0
    traced_peak_mb: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Clock:
    """Collects `Step`s. `with clock.step("map_frame", trace=True): ...`.

    tracemalloc slows pure-Python code several times over, so `trace=True` on a step is honoured
    only when the clock was built with `tracing=True` (the runners' `--trace` flag); timings in the
    notes come from untraced runs, and memory from the process's peak RSS."""

    def __init__(self, *, tracing: bool = False) -> None:
        self.steps: list[Step] = []
        self.tracing = tracing

    @contextmanager
    def step(self, name: str, *, trace: bool = False) -> Iterator[Step]:
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
            self.steps.append(s)

    def as_list(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.steps]


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


def write_result(stage: str, dataset: str, payload: dict[str, Any]) -> Path:
    """Write `<RESULTS>/<stage>/<dataset>.json` and return its path."""
    out = RESULTS / stage / f"{dataset}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jsonable(payload), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return out


def command_line() -> str:
    """The command that produced a result, recorded beside it."""
    return "python3 -P " + " ".join(sys.argv)


def env_info() -> dict[str, Any]:
    import numpy as np
    import pandas as pd

    return {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "cpus": os.cpu_count(),
        "load_average_1_5_15": [round(x, 2) for x in os.getloadavg()],
        "note": "shared machine: other runners were using the CPUs, so wall times are upper bounds",
    }
