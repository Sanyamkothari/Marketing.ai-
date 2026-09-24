"""`scripts/bench_1m.py` (Plan D M57): what it reads from the benchmark and what it writes down."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import bench_1m

OUTPUT = """workspace    : /tmp/x
measured     : ingest csv 68.5 s · 14,590 rows/sec · 1,000,000 rows
measured     : score csv 1,245.3 s · 803 rows/sec · 1,000,000 rows

peak memory  : 6,160 MB
"""


def test_the_numbers_are_the_ones_the_benchmark_printed() -> None:
    assert bench_1m.parse(OUTPUT) == {"ingest csv": 68.5, "score csv": 1245.3, "peak_mb": 6160.0}


def test_a_row_names_the_machine_and_the_kind() -> None:
    row = bench_1m.table_row(
        {
            "date": "2026-09-23",
            "kind": "laptop",
            "label": "ThinkPad T14",
            "machine": {
                "cpu": "AMD Ryzen 7",
                "cpus": 16,
                "memory_gib": 32.0,
                "os": "Linux 6.8",
                "python": "3.11.9",
            },
            "measured": bench_1m.parse(OUTPUT),
        }
    )
    assert row == (
        "| 2026-09-23 | laptop | ThinkPad T14 | AMD Ryzen 7, 16 CPUs, 32.0 GiB, Linux 6.8, Python 3.11.9 | "
        "68.5 s | 1,245.3 s | 6,160 MB |"
    )


def test_record_appends_under_the_marker_and_refuses_without_it(tmp_path: Path) -> None:
    doc = tmp_path / "PERFORMANCE.md"
    doc.write_text(f"# Perf\n\n| a |\n|---|\n{bench_1m.TABLE_MARKER}\n| old |\n\nafter\n", encoding="utf-8")
    bench_1m.record("| new |", doc)
    assert (
        doc.read_text(encoding="utf-8")
        == f"# Perf\n\n| a |\n|---|\n{bench_1m.TABLE_MARKER}\n| new |\n| old |\n\nafter\n"
    )
    bare = tmp_path / "bare.md"
    bare.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        bench_1m.record("| new |", bare)


def test_the_kind_is_required_so_a_container_is_never_recorded_as_the_laptop() -> None:
    with pytest.raises(SystemExit):
        bench_1m.main(["--label", "x"])


def test_the_performance_doc_has_the_marker() -> None:
    assert bench_1m.TABLE_MARKER in bench_1m.PERFORMANCE_DOC.read_text(encoding="utf-8")


def test_the_machine_is_described() -> None:
    host = bench_1m.machine()
    assert host["cpus"] and host["python"] and host["os"]
