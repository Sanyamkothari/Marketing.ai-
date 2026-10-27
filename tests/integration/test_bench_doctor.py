"""`scripts/bench_doctor.py`: the harness runs a scripted visit over a small synthetic file and writes a record."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from scripts import bench_doctor


def _customers(path: Path, rows: int = 1_200) -> Path:
    """A tidy one-row-per-customer file with a churn column: 1,200 rows, about a quarter positive."""
    rng = random.Random(7)
    lines = ["customer_id,tenure,monthly_charges,contract,churned"]
    for i in range(rows):
        tenure = rng.randint(1, 72)
        contract = rng.choice(["monthly", "one_year", "two_year"])
        churned = int(rng.random() < (0.45 if contract == "monthly" else 0.1))
        lines.append(f"C{i:05d},{tenure},{rng.uniform(20, 110):.2f},{contract},{churned}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_visit_over_a_synthetic_file_gives_a_complete_record(tmp_path: Path) -> None:
    record = bench_doctor.run_visit(
        _customers(tmp_path / "customers.csv"), "telco-churn", data_dir=tmp_path / "data"
    )
    assert isinstance(record["reached_ready"], bool)
    assert record["state"] is not None
    assert record["wall_s"] > 0
    assert record["peak_mem_mb"] > 0
    assert record["ai_calls"] >= 0 and record["ai_tokens"] >= 0
    assert record["reached_ready"] or record["reason"]  # a failure always says why


def test_a_failed_fetch_is_a_result_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(name: str, data_dir: Path, local: Path | None = None) -> Path:
        raise OSError("no network")

    monkeypatch.setattr(bench_doctor, "fetch", no_network)
    [row] = bench_doctor.run_all(["telco-customer-churn"], tmp_path, {})
    assert row["fetched"] is False
    assert row["reached_ready"] is False
    assert "fetch failed" in row["reason"]
    assert "telco-customer-churn" in bench_doctor.markdown_table([row])
