"""Which campaigns the files can power: every trainable use case is checked, best first, and a guess is a question."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from api.routes.doctor import offers
from engine.doctor.contracts import UseCaseReadiness
from engine.doctor.diagnose import readiness
from tests.unit.doctor.test_diagnose import loaded


def bank_like(rows: int = 3000) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    duration = rng.gamma(2.0, 150.0, size=rows).round()
    said_yes = rng.random(rows) < 1 / (1 + np.exp(-(duration - 600) / 150)) * 0.5
    return pd.DataFrame(
        {
            "client_id": [f"B{index}" for index in range(rows)],
            "age": rng.integers(20, 80, size=rows),
            "job": rng.choice(["admin", "technician", "services", "retired"], size=rows),
            "duration": duration,
            "y": np.where(said_yes, "yes", "no"),
        }
    )


def customers(*yes_columns: str, rows: int = 1200) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{index}" for index in range(rows)],
            "orders": rng.integers(0, 30, size=rows),
            "spend": rng.gamma(2.0, 400.0, size=rows).round(2),
        }
    )
    for name in yes_columns:
        frame[name] = (rng.random(rows) < 0.2).astype(int)
    return frame


def verdicts(frame: pd.DataFrame, config_root: Path) -> list[UseCaseReadiness]:
    return list(readiness([loaded(frame, "customers.csv", config_root)], offers(config_root), config_root))


def test_a_bank_marketing_file_gets_term_deposit_first(config_root: Path) -> None:
    found = list(readiness([loaded(bank_like(), "bank.csv", config_root)], offers(config_root), config_root))
    top = found[0]
    assert top.use_case_id == "bank-term-deposit"
    assert top.status in {"ready", "check"} and top.outcome_column == "y"
    assert top.customers == 3000 and top.yes_count and top.yes_count > 100
    assert "bank.csv has 3,000" in top.why
    assert all(v.status == "missing" and v.first_fix for v in found[1:])


def test_every_trainable_use_case_is_considered_including_campaign_tests_but_not_telco_churn(
    config_root: Path,
) -> None:
    ids = {offer.config.id for offer in offers(config_root)}
    assert {"bank-term-deposit", "card-default-propensity", "insurance-cross-sell", "retail-win-back"} <= ids
    assert {"hillstrom-email", "customer-spend"} <= ids  # a campaign test and an amount goal (W6)
    assert "telco-churn" not in ids


def test_a_returned_goods_column_is_a_question_not_ready(config_root: Path) -> None:
    found = {v.use_case_id: v for v in verdicts(customers("returned_goods"), config_root)}
    won = found["retail-win-back"]
    assert won.status == "check" and won.reason_code == "GUESSED_COLUMN"
    assert won.outcome_column is None and won.candidate_columns == ("returned_goods",)
    assert won.summary.endswith("?")


def test_two_candidate_yes_columns_are_one_question(config_root: Path) -> None:
    found = verdicts(customers("purchased", "bought"), config_root)
    ads = [v for v in found if v.use_case_id == "targeted-advertisement"]
    assert len(ads) == 1 and ads[0].reason_code == "TWO_COLUMNS" and ads[0].questions == 1
    assert set(ads[0].candidate_columns) == {"purchased", "bought"}
