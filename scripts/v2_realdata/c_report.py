"""Family C: print the scale tables in C-scale.md from the JSON results (no numbers typed by hand).

Run:

    python3 -P scripts/v2_realdata/c_report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from c_common import RESULTS  # noqa: E402


def load(pattern: str) -> list[tuple[str, dict[str, Any]]]:
    return [(p.stem, json.loads(p.read_text())) for p in sorted((RESULTS).glob(pattern))]


def ingest() -> None:
    print("\n### Ingestion and rebuild (RetailRocket)\n")
    print(
        "| rows | backend | events | states | map_frame s | append s | scan s | rebuild s | total s | peak RSS MB | DB MB |"
    )
    print("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    rows = []
    for _, d in load("ingest/*.json"):
        st, sm = d["steps"], d["summary"]
        if d["backend"] == "memory":
            m, a, sc = st["map_frame"]["seconds"], st["append"]["seconds"], "-"
        else:
            x = st["read_map_append"]["extra"]
            m, a, sc = x["map_seconds"], x["append_seconds"], st["scan_only"]["seconds"]
        rows.append(
            (
                sm["rows"],
                d["backend"],
                f"| {sm['rows']:,} | {d['backend']} | {sm['events']:,} | {sm['states']:,} | {m} | {a} | {sc} | "
                f"{st['rebuild_states']['seconds']} | {d['total_seconds']} | {d['peak_rss_mb']:,.0f} | {sm.get('db_file_mb', '-')} |",
            )
        )
    for _, _, line in sorted(rows):
        print(line)


def sweep() -> None:
    print("\n### Daily sweep and nightly learning (synthetic customers from Online Retail II)\n")
    print(
        "| customers | backend | history in log | decisions | tick s | decisions/s | peak RSS MB at end of tick | nightly s | process peak RSS MB (incl. nightly) |"
    )
    print("|---:|---|---|---:|---:|---:|---:|---:|---:|")
    rows = []
    for name, d in load("sweep/*.json"):
        if d.get("profiled"):
            continue
        st = d["steps"]
        rows.append(
            (
                d["customers"],
                name,
                f"| {d['customers']:,} | {d['backend']} | {d.get('history_in_log', False)} | {d['sweep']['decisions']:,} | "
                f"{st['tick_sweep']['seconds']} | {d['sweep_decisions_per_second']} | {st['tick_sweep']['peak_rss_mb']:,.0f} | "
                f"{st.get('nightly', {}).get('seconds', '-')} | {d['peak_rss_mb']:,.0f} |",
            )
        )
    for _, _, line in sorted(rows):
        print(line)


def harm() -> None:
    print("\n### ContactHarm.refit alone (synthetic records)\n")
    print("| customers | refit s | peak RSS MB | extra peak MB |")
    print("|---:|---:|---:|---:|")
    for _, d in sorted(load("harm/*.json"), key=lambda t: t[1]["customers"]):
        if d.get("profiled"):
            continue
        print(
            f"| {d['customers']:,} | {d['refit_seconds']} | {d['peak_rss_mb']:,.0f} | {d['refit_extra_peak_mb']:,.0f} |"
        )


if __name__ == "__main__":
    ingest()
    sweep()
    harm()
