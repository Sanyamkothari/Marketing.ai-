"""The Phase 1 one-million-row test (plan §11), on a named machine, written down (Plan D M57).

Plan §11 asks for "1M rows in under the time limit on a laptop". `scripts/bench_large_file.py`
measures the ingest and score paths and has been run at a million rows, but only ever on a 4-CPU
container, and README says so. This script is the part that was missing: it runs that benchmark at
one million rows, reads the numbers it printed, says which machine they belong to - the CPU model,
the cores the process may use, the RAM, the OS - and, with `--record`, appends one row to the table
in `docs/PERFORMANCE.md`, so the measurement lands in the repository as it was taken rather than as
somebody retyped it.

Nothing here is estimated: every second and megabyte in the row is a number the benchmark printed.
`--kind` must be given, so a container run can never be recorded as the laptop run plan §11 asks
for. The benchmark runs in a child process, so its peak memory is its own.

    .venv/bin/python -m scripts.bench_1m --kind laptop --label "MacBook Pro 14 (M3 Pro), 18 GB" --record
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

COMMAND: Final[str] = "python -m scripts.bench_1m"
ROWS: Final[int] = 1_000_000
PERFORMANCE_DOC: Final[Path] = Path(__file__).resolve().parent.parent / "docs" / "PERFORMANCE.md"
TABLE_MARKER: Final[str] = "<!-- bench_1m rows: appended by scripts/bench_1m.py --record -->"
KINDS: Final[tuple[str, ...]] = ("laptop", "desktop", "container", "server")

_MEASURED: Final[re.Pattern[str]] = re.compile(
    r"^measured\s*:\s*(?P<stage>ingest|score) (?P<format>csv|parquet) (?P<seconds>[\d,.]+) s"
)
_PEAK: Final[re.Pattern[str]] = re.compile(r"^peak memory\s*:\s*(?P<mb>[\d,]+) MB")


def cpu_model() -> str:
    """The CPU's marketing name, from the OS; `unknown CPU` when it will not say."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True
            )
            return out.stdout.strip() or "unknown CPU"
        except (OSError, subprocess.CalledProcessError):
            return "unknown CPU"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown CPU"


def memory_gib() -> float | None:
    """Total RAM in GiB, or None when the OS will not say."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True)
            return int(out.stdout.strip()) / 1024**3
        except (OSError, ValueError, subprocess.CalledProcessError):
            return None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024**2
    return None


def machine() -> dict[str, object]:
    affinity = getattr(os, "sched_getaffinity", None)
    return {
        "cpu": cpu_model(),
        "cpus": len(affinity(0)) if affinity is not None else (os.cpu_count() or 0),
        "memory_gib": None if (gib := memory_gib()) is None else round(gib, 1),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
    }


def parse(output: str) -> dict[str, float]:
    """`{"ingest csv": seconds, "score csv": seconds, "peak_mb": mb}` from the benchmark's output."""
    found: dict[str, float] = {}
    for line in output.splitlines():
        if match := _MEASURED.match(line.strip()):
            found[f"{match['stage']} {match['format']}"] = float(match["seconds"].replace(",", ""))
        elif match := _PEAK.match(line.strip()):
            found["peak_mb"] = float(match["mb"].replace(",", ""))
    return found


def table_row(result: dict[str, object]) -> str:
    """One markdown row: date, kind, label, machine, the two stages, peak memory."""
    host = result["machine"]
    assert isinstance(host, dict)
    numbers = result["measured"]
    assert isinstance(numbers, dict)
    memory = "?" if host["memory_gib"] is None else f"{host['memory_gib']} GiB"

    def seconds(key: str) -> str:
        return "not measured" if key not in numbers else f"{numbers[key]:,.1f} s"

    peak = "?" if "peak_mb" not in numbers else f"{numbers['peak_mb']:,.0f} MB"
    return (
        f"| {result['date']} | {result['kind']} | {result['label']} | {host['cpu']}, {host['cpus']} CPUs, "
        f"{memory}, {host['os']}, Python {host['python']} | {seconds('ingest csv')} | "
        f"{seconds('score csv')} | {peak} |"
    )


def record(row: str, doc: Path = PERFORMANCE_DOC) -> None:
    """Append `row` after the table marker in `docs/PERFORMANCE.md`; refuse if the marker is gone."""
    text = doc.read_text(encoding="utf-8")
    if TABLE_MARKER not in text:
        raise SystemExit(f"{doc} has no {TABLE_MARKER!r}; add the row by hand")
    head, tail = text.split(TABLE_MARKER, 1)
    doc.write_text(f"{head}{TABLE_MARKER}\n{row}{tail}", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND, description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--kind", choices=KINDS, required=True, help="what the machine is; plan §11 asks for a laptop"
    )
    parser.add_argument(
        "--label", required=True, help='the machine as a person would name it, e.g. "ThinkPad T14 Gen 3"'
    )
    parser.add_argument("--record", action="store_true", help="append the row to docs/PERFORMANCE.md")
    parser.add_argument("--json", type=Path, default=None, help="also write the result as JSON here")
    parser.add_argument("--rows", type=int, default=ROWS, help=argparse.SUPPRESS)  # tests use a small size
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = [sys.executable, "-m", "scripts.bench_large_file", "--rows", str(args.rows), "--format", "csv"]
    print(f"running      : {' '.join(command[1:])}", flush=True)
    started = datetime.now(UTC)
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    sys.stdout.write(completed.stdout)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr[-4000:])
        print(f"the benchmark failed (exit {completed.returncode}); nothing is recorded", file=sys.stderr)
        return completed.returncode
    measured = parse(completed.stdout)
    if "ingest csv" not in measured:
        print("the benchmark printed no measurement; nothing is recorded", file=sys.stderr)
        return 1
    result: dict[str, object] = {
        "date": started.date().isoformat(),
        "rows": args.rows,
        "kind": args.kind,
        "label": args.label,
        "machine": machine(),
        "measured": measured,
    }
    row = table_row(result)
    print(row)
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=1), encoding="utf-8")
    if args.record:
        if args.rows != ROWS:
            print(f"--record is for a {ROWS:,}-row run; this was {args.rows:,} rows", file=sys.stderr)
            return 2
        record(row)
        print(f"recorded in  : {PERFORMANCE_DOC}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
