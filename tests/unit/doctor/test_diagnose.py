"""Data Doctor's diagnosis (Plan L M151, DEC-1511): what a file is, and what the files can power."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.config import get_roles, load_use_case
from engine.doctor.diagnose import LoadedFile, Offer, diagnose_file, readiness
from engine.stages.ingest import profile_dataset
from tests.fixtures.make_data import GenerationSpec, generate

USE_CASE = "targeted-advertisement"


def loaded(frame: pd.DataFrame, name: str, config_root: Path, upload_id: str = "u_1") -> LoadedFile:
    config = load_use_case(USE_CASE, config_root)
    profile = profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)


def bills(rows: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "customer_id": [f"C{index % 150}" for index in range(rows)],
            "invoice_date": pd.date_range("2025-01-01", periods=rows, freq="D").strftime("%Y-%m-%d"),
            "amount": rng.gamma(2.0, 300.0, size=rows).round(2),
        }
    )


@pytest.fixture
def offer(config_root: Path) -> Offer:
    return Offer(config=load_use_case(USE_CASE, config_root), stage="Awareness")


def test_a_customer_file_is_named_and_described(config_root: Path) -> None:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=800, variant="clean"))
    file = diagnose_file(
        loaded(frame, "customers.csv", config_root),
        load_use_case(USE_CASE, config_root),
        get_roles(config_root),
    )
    assert file.kind == "entity"
    assert file.rows == 800 and file.columns == frame.shape[1]
    assert file.id_column == "customer_id" and file.rows_per_id == 1.0


def test_a_bills_log_is_named_as_one(config_root: Path) -> None:
    file = diagnose_file(
        loaded(bills(), "bills_2025.csv", config_root),
        load_use_case(USE_CASE, config_root),
        get_roles(config_root),
    )
    assert file.kind == "bills"
    assert "invoice" in file.kind_label
    assert file.rows_per_id is not None and file.rows_per_id > 1
    assert file.date_columns == ("invoice_date",)


def test_a_file_with_the_outcome_is_ready_or_ready_after_a_check(config_root: Path, offer: Offer) -> None:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=3000, variant="clean"))
    (verdict,) = readiness([loaded(frame, "history.csv", config_root, "u_hist")], [offer], config_root)
    assert verdict.status in {"ready", "check"}, verdict.summary
    assert verdict.upload_id == "u_hist" and verdict.outcome_column == "converted_30d"
    assert "history.csv" in verdict.summary


def test_a_synonym_names_the_outcome_too(config_root: Path, offer: Offer) -> None:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=3000, variant="clean")).rename(
        columns={"converted_30d": "purchased"}
    )
    (verdict,) = readiness([loaded(frame, "history.csv", config_root)], [offer], config_root)
    assert verdict.outcome_column == "purchased"
    assert verdict.status in {"ready", "check"}


def test_no_outcome_anywhere_says_what_to_collect(config_root: Path, offer: Offer) -> None:
    (verdict,) = readiness([loaded(bills(), "bills.csv", config_root)], [offer], config_root)
    assert verdict.status == "missing" and verdict.upload_id is None
    assert any("purchased within 30 days" in need.lower() for need in verdict.needs)
    assert any("Campaign conversion logs" in need for need in verdict.needs)


def test_too_little_history_is_too_small_with_the_advisors_reason(config_root: Path, offer: Offer) -> None:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=60, variant="clean"))
    (verdict,) = readiness([loaded(frame, "tiny.csv", config_root)], [offer], config_root)
    assert verdict.status in {"too_small", "missing"}
    assert verdict.summary.startswith("tiny.csv:")


def test_the_best_file_wins_and_ready_comes_first(config_root: Path, offer: Offer) -> None:
    good = generate(GenerationSpec(use_case_id=USE_CASE, rows=3000, variant="clean"))
    tiny = generate(GenerationSpec(use_case_id=USE_CASE, rows=60, variant="clean"))
    payment = Offer(config=load_use_case("payment-propensity", config_root), stage="Service")
    verdicts = readiness(
        [loaded(tiny, "tiny.csv", config_root, "u_tiny"), loaded(good, "good.csv", config_root, "u_good")],
        [payment, offer],
        config_root,
    )
    assert [v.use_case_id for v in verdicts] == [USE_CASE, "payment-propensity"]
    assert verdicts[0].upload_id == "u_good"
    assert verdicts[1].status == "missing"
