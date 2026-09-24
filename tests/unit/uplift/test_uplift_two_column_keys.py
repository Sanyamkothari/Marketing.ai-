"""Uplift on a two-column key - customer + snapshot date (M53, DEC-853 to DEC-855).

A periodic uplift table holds each customer at several snapshot dates. Treatment is assigned per
customer, so the checks must refuse a customer who is in both arms of one campaign, count the arms in
customers, and the hold-out must keep every snapshot of a customer on one side. These tests pin that
on the fixture generator's snapshot tables (`tests/fixtures/make_uplift_data.make_uplift_snapshots`),
without training anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine import keys
from engine.config import UseCaseConfig, load_use_case, resolve_config
from engine.contracts import Severity
from engine.uplift.checks import CHECK_ORDER, UpliftCheckResult, run_uplift_checks
from engine.uplift.contracts import UPLIFT_VALIDATION_CODES
from engine.uplift.data import fit_feature_spec, reserved_feature_exclusions, split_holdout
from engine.uplift.incrementality import measure_incrementality
from tests.fixtures.make_uplift_data import SNAPSHOT_DATES, make_uplift_snapshots

USE_CASE = "win-back-campaign"
KEY = ["customer_id", "snapshot_date"]
TARGET = "reactivated_90d"
REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 23, tzinfo=UTC)


def _config(**uplift: object) -> UseCaseConfig:
    overrides: dict[str, object] = {
        "problem_type": "uplift",
        "uplift": {"min_arm_rows": 200, "min_arm_positives": 20, "treatment_column": "treatment", **uplift},
    }
    return resolve_config(USE_CASE, overrides).config


def _check(frame: pd.DataFrame, config: UseCaseConfig, key: object = KEY) -> UpliftCheckResult:
    return run_uplift_checks(
        frame, config, primary_key=key, target=TARGET, upload_id="u_snapshots", now=NOW, seed=3  # type: ignore[arg-type]
    )


def _codes(result: UpliftCheckResult) -> list[str]:
    return [check.code for check in result.report.checks]


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def test_a_snapshot_table_with_one_arm_per_customer_passes_and_counts_customers() -> None:
    data = make_uplift_snapshots(1500, seed=5)
    result = _check(data.frame, _config())
    assert result.report.passed, _codes(result)
    assert "TREATMENT_VARIES_WITHIN_ENTITY" not in _codes(result)
    report = result.report
    assert report.entity_column == "customer_id"
    arms = data.frame.groupby("customer_id")["treatment"].first()
    assert report.treated_entities == int((arms == 1).sum())
    assert report.control_entities == int((arms == 0).sum())
    assert report.treated_rows == int((data.frame["treatment"] == 1).sum())
    assert report.treated_rows == report.treated_entities * len(SNAPSHOT_DATES)
    # Random assignment per customer: the grouped cross-validation must not see targeting in it.
    assert report.causal is True and report.randomness_auc is not None and report.randomness_auc < 0.6


def test_a_customer_in_both_arms_of_one_campaign_is_refused() -> None:
    data = make_uplift_snapshots(1500, seed=5, mixed_customers=40)
    result = _check(data.frame, _config())
    assert not result.report.passed
    finding = next(c for c in result.report.checks if c.code == "TREATMENT_VARIES_WITHIN_ENTITY")
    assert finding.severity is Severity.ERROR and not finding.acknowledgeable
    assert finding.details["mixed_entities"] == 40
    assert finding.details["entities"] == 1500
    assert finding.details["entity_column"] == "customer_id"
    assert "40 of 1,500 customers" in finding.message
    # Arms cannot be counted per customer when customers are in both, so those checks are skipped.
    assert "TREATMENT_ARM_TOO_SMALL" not in _codes(result)
    assert result.report.randomness_auc is None


def test_the_refusal_cannot_be_acknowledged_away() -> None:
    data = make_uplift_snapshots(800, seed=5, mixed_customers=10)
    result = run_uplift_checks(
        data.frame,
        _config(),
        primary_key=KEY,
        target=TARGET,
        upload_id="u",
        acknowledged=("TREATMENT_VARIES_WITHIN_ENTITY", "TREATMENT_VARIES_WITHIN_ENTITY:treatment"),
        now=NOW,
    )
    assert not result.report.passed


def test_different_arms_in_different_campaigns_are_allowed() -> None:
    """Within one campaign a customer has one arm; across campaigns they may be re-drawn."""
    data = make_uplift_snapshots(1500, seed=5, mixed_customers=40)
    frame = data.frame.copy()
    frame["campaign"] = np.where(frame["snapshot_date"] == SNAPSHOT_DATES[-1], "wave_b", "wave_a")
    result = _check(frame, _config(campaign_id_column="campaign"))
    assert "TREATMENT_VARIES_WITHIN_ENTITY" not in _codes(result)
    # The same file without the campaign column configured is refused.
    assert "TREATMENT_VARIES_WITHIN_ENTITY" in _codes(_check(frame, _config()))


def test_a_one_column_key_is_checked_exactly_as_before() -> None:
    data = make_uplift_snapshots(1500, snapshots=("2026-01-31",), seed=5)
    frame = data.frame.drop(columns=["snapshot_date"])
    result = _check(frame, _config(), key="customer_id")
    assert result.report.entity_column is None
    assert result.report.treated_entities is None and result.report.control_entities is None
    assert result.report.treated_rows == int((frame["treatment"] == 1).sum())


def test_a_snapshot_taken_after_the_treatment_date_is_a_feature_after_treatment() -> None:
    data = make_uplift_snapshots(1500, seed=5, with_treatment_date=True)
    frame = data.frame.copy()
    config = _config(treatment_date_column="treatment_date")
    assert "FEATURE_AFTER_TREATMENT" not in _codes(_check(frame, config))
    frame["treatment_date"] = "2026-01-15"  # every snapshot is after this campaign reached customers
    finding = next(c for c in _check(frame, config).report.checks if c.code == "FEATURE_AFTER_TREATMENT")
    assert finding.column == "snapshot_date"


def test_the_new_code_is_registered_and_ordered_after_the_binary_check() -> None:
    assert "TREATMENT_VARIES_WITHIN_ENTITY" in UPLIFT_VALIDATION_CODES
    assert set(CHECK_ORDER) == UPLIFT_VALIDATION_CODES
    assert (
        CHECK_ORDER.index("TREATMENT_VARIES_WITHIN_ENTITY") == CHECK_ORDER.index("TREATMENT_NOT_BINARY") + 1
    )


# ---------------------------------------------------------------------------
# Features and the hold-out
# ---------------------------------------------------------------------------
def test_neither_key_column_nor_the_row_key_is_ever_a_feature() -> None:
    config = _config()
    reserved = reserved_feature_exclusions(
        config, primary_key=KEY, target=TARGET, treatment_column="treatment"
    )
    assert {*KEY, keys.ROW_KEY_COLUMN} <= set(reserved)
    frame = keys.with_row_key(make_uplift_snapshots(500, seed=2).frame, KEY)
    spec = fit_feature_spec(frame, config, primary_key=KEY, target=TARGET, treatment_column="treatment")
    assert not {*KEY, keys.ROW_KEY_COLUMN} & set(spec.feature_columns)
    assert not {*KEY, keys.ROW_KEY_COLUMN} & set(spec.dropped)


def test_the_grouped_hold_out_keeps_every_customer_on_one_side() -> None:
    frame = make_uplift_snapshots(3000, seed=9).frame
    t = frame["treatment"].to_numpy()
    y = frame[TARGET].to_numpy()
    groups = frame["customer_id"].to_numpy(dtype=object)
    train, test = split_holdout(t, y, test_fraction=0.3, seed=4, groups=groups)
    assert len(train) + len(test) == len(frame) and not set(train) & set(test)
    train_ids, test_ids = set(groups[train]), set(groups[test])
    assert not train_ids & test_ids
    assert abs(len(test_ids) / 3000 - 0.3) < 0.01
    # Stratified on the customer's arm: the hold-out's treated share is the file's.
    assert abs(t[test].mean() - t.mean()) < 0.02
    # Reproducible from the seed, and a different seed draws different customers.
    again = split_holdout(t, y, test_fraction=0.3, seed=4, groups=groups)
    other = split_holdout(t, y, test_fraction=0.3, seed=5, groups=groups)
    assert np.array_equal(test, again[1]) and not np.array_equal(test, other[1])


def test_without_groups_the_hold_out_is_unchanged() -> None:
    rng = np.random.default_rng(0)
    t, y = rng.integers(0, 2, 500), rng.integers(0, 2, 500)
    assert all(
        np.array_equal(a, b)
        for a, b in zip(
            split_holdout(t, y, test_fraction=0.3, seed=1),
            split_holdout(t, y, test_fraction=0.3, seed=1, groups=None),
            strict=True,
        )
    )


def test_a_grouped_split_needs_one_entity_per_row() -> None:
    with pytest.raises(ValueError, match="one entity per row"):
        split_holdout(np.array([0, 1]), np.array([0, 1]), test_fraction=0.3, seed=0, groups=["a"])


# ---------------------------------------------------------------------------
# Campaign results joined on both columns
# ---------------------------------------------------------------------------
def test_campaign_results_join_the_outcomes_on_both_key_columns() -> None:
    scores = pd.DataFrame(
        {
            "customer_id": ["C1", "C1", "C2", "C2"],
            "snapshot_date": ["2026-01-31", "2026-02-28", "2026-01-31", "2026-02-28"],
            "control_group": [False, False, True, True],
            "suppressed_reason": [None, None, None, None],
        }
    )
    outcomes = pd.DataFrame(
        {
            "customer_id": ["C1", "C1", "C2", "C2"],
            # The outcomes file dates its snapshots as timestamps; the scores file as text.
            "snapshot_date": pd.to_datetime(["2026-02-28", "2026-01-31", "2026-02-28", "2026-01-31"]),
            "converted": [1, 0, 0, 0],
        }
    )
    report = measure_incrementality(
        scores,
        outcomes,
        run_id="r_1",
        primary_key=KEY,
        outcome_column="converted",
        treatment_time=datetime(2026, 3, 1, tzinfo=UTC),
        as_of=NOW,
    )
    assert report.rows_without_outcome == 0
    assert (report.treated_rows, report.treated_conversions) == (2, 1)
    assert (report.control_rows, report.control_conversions) == (2, 0)


def test_a_repeated_customer_and_snapshot_pair_is_refused() -> None:
    scores = pd.DataFrame(
        {"customer_id": ["C1", "C1"], "snapshot_date": ["2026-01-31", "2026-01-31"], "control_group": [0, 1]}
    )
    outcomes = pd.DataFrame({"customer_id": ["C1"], "snapshot_date": ["2026-01-31"], "converted": [1]})
    with pytest.raises(ValueError, match="repeats 1 primary key"):
        measure_incrementality(
            scores,
            outcomes,
            run_id="r_1",
            primary_key=KEY,
            outcome_column="converted",
            treatment_time=NOW,
            as_of=NOW,
        )


# ---------------------------------------------------------------------------
# The codes are documented where the data contract lives
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("document", ["docs/DATA_CONTRACT.md", "docs/UPLIFT.md"])
def test_every_uplift_check_code_is_documented(document: str) -> None:
    text = (REPO / document).read_text(encoding="utf-8")
    missing = sorted(code for code in UPLIFT_VALIDATION_CODES if f"`{code}`" not in text)
    assert not missing, f"{document} does not document {missing}"


def test_the_data_contract_gives_each_uplift_code_its_severity() -> None:
    text = (REPO / "docs/DATA_CONTRACT.md").read_text(encoding="utf-8")
    section = text[text.index("## 11.") :]
    for code in UPLIFT_VALIDATION_CODES:
        row = next(line for line in section.splitlines() if line.startswith(f"| `{code}`"))
        severity = "warning" if code == "OUTCOME_WINDOW_IMMATURE" else "error"
        assert f"| {severity}" in row, row


def test_the_config_default_tolerance_is_what_engine_yaml_says() -> None:
    assert load_use_case(USE_CASE).uplift.drift_treated_share_tolerance == 0.05
