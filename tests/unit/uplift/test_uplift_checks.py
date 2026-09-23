"""`engine.uplift.checks.run_uplift_checks`: the six plan B §4 checks, each firing and not firing.

`TREATMENT_NOT_RANDOM` is the one that decides whether anything the run reports is causal, so it is
exercised on the generator's targeted assignment (must fire) and on random assignment over several
seeds (must not fire - a check that fires by chance would block honest experiments).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case_document
from engine.contracts import Severity
from engine.uplift.checks import UpliftCheckResult, run_uplift_checks, treatment_predictability
from engine.uplift.contracts import UpliftValidationReport
from tests.fixtures.make_uplift_data import make_uplift_data

PK = "customer_id"
TARGET = "reactivated_90d"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def use_case(
    *, uplift: dict[str, Any] | None = None, acknowledged: tuple[str, ...] = (), **sections: dict[str, Any]
) -> UseCaseConfig:
    """`win-back-campaign`, template emptied, with an uplift block and extra sections merged in."""
    document = copy.deepcopy(load_use_case_document("win-back-campaign"))
    document["template"] = {"columns": []}
    document["uplift"] = {**(document.get("uplift") or {}), **(uplift or {})}
    document["validation"] = {**(document.get("validation") or {}), "acknowledged": list(acknowledged)}
    for name, values in sections.items():
        document[name] = {**(document.get(name) or {}), **values}
    return UseCaseConfig.model_validate(document)


def check(frame: pd.DataFrame, config: UseCaseConfig | None = None, **kwargs: Any) -> UpliftCheckResult:
    return run_uplift_checks(
        frame,
        config or use_case(),
        primary_key=PK,
        target=TARGET,
        upload_id="u_checks",
        run_id="r_20260901_checks00",
        now=NOW,
        **kwargs,
    )


def codes(report: UpliftValidationReport) -> list[str]:
    return [item.code for item in report.checks]


def only(report: UpliftValidationReport, code: str) -> Any:
    found = [item for item in report.checks if item.code == code]
    assert len(found) == 1, codes(report)
    return found[0]


@pytest.fixture(scope="module")
def random_frame() -> pd.DataFrame:
    return make_uplift_data(4000, seed=21).frame


@pytest.fixture(scope="module")
def targeted_frame() -> pd.DataFrame:
    return make_uplift_data(4000, seed=21, targeted=True).frame


# ---------------------------------------------------------------------------
# A clean randomised experiment passes everything
# ---------------------------------------------------------------------------
def test_clean_random_experiment_passes_with_a_complete_report(random_frame: pd.DataFrame) -> None:
    result = check(random_frame)
    report = result.report
    assert report.checks == ()
    assert report.passed and report.causal
    assert report.treatment_column == "treatment"
    assert report.randomness_auc is not None and report.randomness_auc < 0.6
    assert report.rows_checked == len(random_frame)
    assert report.rows_immature == 0
    assert report.upload_id == "u_checks" and report.run_id == "r_20260901_checks00"
    assert report.checked_at == NOW
    assert result.keep is None  # no maturity check configured


# ---------------------------------------------------------------------------
# TREATMENT_COLUMN_MISSING
# ---------------------------------------------------------------------------
def test_missing_treatment_column_is_one_error_and_the_arm_checks_are_skipped(
    random_frame: pd.DataFrame,
) -> None:
    report = check(random_frame.drop(columns=["treatment"])).report
    assert codes(report) == ["TREATMENT_COLUMN_MISSING"]
    item = only(report, "TREATMENT_COLUMN_MISSING")
    assert item.severity is Severity.ERROR
    assert "'treatment', 'treated', 'contacted' and 'is_treated'" in item.message
    assert not report.passed
    assert report.treatment_column is None and report.randomness_auc is None


def test_a_configured_treatment_column_that_is_absent_is_named(random_frame: pd.DataFrame) -> None:
    report = check(random_frame, use_case(uplift={"treatment_column": "variant"})).report
    item = only(report, "TREATMENT_COLUMN_MISSING")
    assert "'variant'" in item.message
    assert item.column == "variant"
    assert not report.passed


def test_a_hinted_column_under_another_spelling_is_found(random_frame: pd.DataFrame) -> None:
    frame = random_frame.rename(columns={"treatment": "Is_Treated"})
    report = check(frame).report
    assert "TREATMENT_COLUMN_MISSING" not in codes(report)
    assert report.treatment_column == "Is_Treated"


# ---------------------------------------------------------------------------
# TREATMENT_NOT_BINARY
# ---------------------------------------------------------------------------
def test_non_binary_treatment_counts_bad_values_and_nulls(random_frame: pd.DataFrame) -> None:
    frame = random_frame.copy()
    frame["treatment"] = frame["treatment"].astype(object)
    frame.loc[:9, "treatment"] = 2
    frame.loc[10:14, "treatment"] = None
    report = check(frame).report
    assert codes(report) == ["TREATMENT_NOT_BINARY"]  # the arm checks cannot run and are skipped
    item = only(report, "TREATMENT_NOT_BINARY")
    assert item.severity is Severity.ERROR
    assert item.details["bad_values"] == 15
    assert item.details["null_values"] == 5
    assert "15 of 4,000 rows" in item.message
    assert not report.passed


def test_boolean_and_text_treatment_are_binary(random_frame: pd.DataFrame) -> None:
    as_bool = random_frame.assign(treatment=random_frame["treatment"].astype(bool))
    as_text = random_frame.assign(treatment=random_frame["treatment"].astype(str))
    for frame in (as_bool, as_text):
        assert "TREATMENT_NOT_BINARY" not in codes(check(frame).report)


# ---------------------------------------------------------------------------
# TREATMENT_ARM_TOO_SMALL
# ---------------------------------------------------------------------------
def test_small_control_group_is_an_error() -> None:
    frame = make_uplift_data(4000, seed=22, treat_share=0.9).frame
    report = check(frame).report
    item = only(report, "TREATMENT_ARM_TOO_SMALL")
    control = int((frame["treatment"] == 0).sum())
    assert item.details["control_rows"] == control < 1000
    assert f"the control group has {control:,} customers (at least 1,000 needed)" in item.message
    assert "treated group has" not in item.message
    assert not report.passed


def test_too_few_positives_in_an_arm_is_an_error(random_frame: pd.DataFrame) -> None:
    config = use_case(uplift={"min_arm_positives": 5000})
    item = only(check(random_frame, config).report, "TREATMENT_ARM_TOO_SMALL")
    assert "had a positive 'reactivated_90d'" in item.message
    y = random_frame[TARGET].to_numpy()
    t = random_frame["treatment"].to_numpy()
    assert item.details["treated_positives"] == int(y[t == 1].sum())
    assert item.details["control_positives"] == int(y[t == 0].sum())


def test_arms_at_the_minimum_pass(random_frame: pd.DataFrame) -> None:
    t = random_frame["treatment"]
    config = use_case(uplift={"min_arm_rows": int(min((t == 1).sum(), (t == 0).sum()))})
    assert "TREATMENT_ARM_TOO_SMALL" not in codes(check(random_frame, config).report)


# ---------------------------------------------------------------------------
# TREATMENT_NOT_RANDOM
# ---------------------------------------------------------------------------
def test_targeted_assignment_is_not_random(targeted_frame: pd.DataFrame) -> None:
    report = check(targeted_frame).report
    item = only(report, "TREATMENT_NOT_RANDOM")
    assert item.severity is Severity.ERROR
    assert item.acknowledgeable and not item.acknowledged
    assert item.column == "treatment"
    assert report.randomness_auc is not None and report.randomness_auc > 0.6
    assert item.details["auc"] == report.randomness_auc
    # The generator targets on visits_30d; the message must say so.
    assert item.details["top_features"][0] == "visits_30d"
    assert "'visits_30d'" in item.message
    assert not report.passed and not report.causal


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_assignment_is_not_flagged_whatever_the_seed(seed: int) -> None:
    frame = make_uplift_data(3000, seed=seed).frame
    report = check(frame, seed=seed).report
    assert "TREATMENT_NOT_RANDOM" not in codes(report)
    assert report.randomness_auc is not None and report.randomness_auc < 0.56
    assert report.causal


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_targeted_assignment_is_flagged_whatever_the_seed(seed: int) -> None:
    frame = make_uplift_data(3000, seed=seed, targeted=True).frame
    assert "TREATMENT_NOT_RANDOM" in codes(check(frame, seed=seed).report)


@pytest.mark.parametrize("token", ["TREATMENT_NOT_RANDOM", "TREATMENT_NOT_RANDOM:treatment"])
def test_acknowledging_non_random_lets_the_run_go_ahead_as_not_causal(
    targeted_frame: pd.DataFrame, token: str
) -> None:
    report = check(targeted_frame, acknowledged=[token]).report
    item = only(report, "TREATMENT_NOT_RANDOM")
    assert item.acknowledged and item.severity is Severity.ERROR
    assert report.passed
    assert not report.causal


def test_acknowledgement_is_read_from_the_config_too(targeted_frame: pd.DataFrame) -> None:
    config = use_case(acknowledged=("TREATMENT_NOT_RANDOM",))
    report = check(targeted_frame, config).report
    assert report.passed and not report.causal


def test_acknowledging_another_column_does_not_count(targeted_frame: pd.DataFrame) -> None:
    report = check(targeted_frame, acknowledged=["TREATMENT_NOT_RANDOM:visits_30d"]).report
    assert not only(report, "TREATMENT_NOT_RANDOM").acknowledged
    assert not report.passed


def test_other_errors_are_not_acknowledgeable() -> None:
    frame = make_uplift_data(4000, seed=23, treat_share=0.9).frame
    report = check(frame, acknowledged=["TREATMENT_ARM_TOO_SMALL"]).report
    assert not only(report, "TREATMENT_ARM_TOO_SMALL").acknowledged
    assert not report.passed


def test_randomness_is_deterministic_by_seed(targeted_frame: pd.DataFrame) -> None:
    first = check(targeted_frame, seed=3).report.randomness_auc
    again = check(targeted_frame, seed=3).report.randomness_auc
    assert first == again


def test_treatment_predictability_skips_without_features_or_rows() -> None:
    t = np.array([0, 1] * 100)
    assert treatment_predictability(pd.DataFrame(index=range(200)), t, seed=0) is None
    features = pd.DataFrame({"x": np.arange(40, dtype=float)})
    assert treatment_predictability(features, np.array([1] * 35 + [0] * 5), seed=0) is None


# ---------------------------------------------------------------------------
# OUTCOME_WINDOW_IMMATURE
# ---------------------------------------------------------------------------
def dated(frame: pd.DataFrame, *, recent_every: int = 0, undated_every: int = 0) -> pd.DataFrame:
    """Everyone treated 200 days before NOW, except every `recent_every`-th row (10 days before)."""
    old = (NOW - timedelta(days=200)).date().isoformat()
    recent = (NOW - timedelta(days=10)).date().isoformat()
    dates: list[str | None] = []
    for index in range(len(frame)):
        if undated_every and index % undated_every == 0:
            dates.append(None)
        elif recent_every and index % recent_every == 0:
            dates.append(recent)
        else:
            dates.append(old)
    return frame.assign(treatment_date=pd.Series(dates, index=frame.index, dtype=object))


WINDOW = {"treatment_date_column": "treatment_date", "outcome_window_days": 90}


def test_immature_rows_are_dropped_counted_and_warned(random_frame: pd.DataFrame) -> None:
    frame = dated(random_frame, recent_every=4)
    result = check(frame, use_case(uplift=WINDOW))
    item = only(result.report, "OUTCOME_WINDOW_IMMATURE")
    assert item.severity is Severity.WARNING
    assert item.details["rows_immature"] == 1000
    assert (
        item.details["results_complete_on"]
        == (NOW - timedelta(days=10) + timedelta(days=90)).date().isoformat()
    )
    assert "1,000 customers were treated less than 90 days before 2026-09-01" in item.message
    assert result.report.rows_immature == 1000
    assert result.keep is not None and int((~result.keep).sum()) == 1000
    assert not result.keep[0] and result.keep[1]
    assert result.report.passed  # a warning never blocks


def test_undated_rows_cannot_be_shown_mature(random_frame: pd.DataFrame) -> None:
    result = check(dated(random_frame, undated_every=100), use_case(uplift=WINDOW))
    item = only(result.report, "OUTCOME_WINDOW_IMMATURE")
    assert item.details["rows_undated"] == 40
    assert item.details["rows_immature"] == 0
    assert result.report.rows_immature == 40
    assert result.keep is not None and int((~result.keep).sum()) == 40


def test_mature_outcomes_raise_nothing_and_keep_every_row(random_frame: pd.DataFrame) -> None:
    result = check(dated(random_frame), use_case(uplift=WINDOW))
    assert "OUTCOME_WINDOW_IMMATURE" not in codes(result.report)
    assert result.keep is not None and bool(result.keep.all())


def test_arm_sizes_are_counted_after_immature_rows_are_dropped() -> None:
    frame = dated(make_uplift_data(2600, seed=24).frame, recent_every=2)
    report = check(frame, use_case(uplift=WINDOW)).report
    item = only(report, "TREATMENT_ARM_TOO_SMALL")
    assert item.details["rows_excluded"] == 1300
    assert item.details["treated_rows"] + item.details["control_rows"] == 1300
    assert "after leaving out 1,300 customers whose outcome is not final yet" in item.message


def test_a_configured_date_column_missing_from_the_file_is_a_warning(random_frame: pd.DataFrame) -> None:
    result = check(random_frame, use_case(uplift=WINDOW))
    item = only(result.report, "OUTCOME_WINDOW_IMMATURE")
    assert item.details["column_missing"] is True
    assert result.keep is None
    assert result.report.passed


# ---------------------------------------------------------------------------
# FEATURE_AFTER_TREATMENT
# ---------------------------------------------------------------------------
def test_a_date_after_the_treatment_date_is_an_error(random_frame: pd.DataFrame) -> None:
    frame = dated(random_frame)
    treated_on = NOW - timedelta(days=200)
    frame["last_purchase_date"] = [
        (treated_on + timedelta(days=5 if index % 8 == 0 else -30)).date().isoformat()
        for index in range(len(frame))
    ]
    frame["last_login_date"] = treated_on.date().isoformat()  # same day: not later
    report = check(frame, use_case(uplift={"treatment_date_column": "treatment_date"})).report
    item = only(report, "FEATURE_AFTER_TREATMENT")
    assert item.severity is Severity.ERROR
    assert item.column == "last_purchase_date"
    assert item.details["rows_after_treatment"] == 500
    assert item.details["rows_compared"] == 4000
    assert "'last_purchase_date' is later than the treatment date in 500 of 4,000 rows" in item.message
    assert not report.passed


def test_dates_before_treatment_or_excluded_columns_pass(random_frame: pd.DataFrame) -> None:
    frame = dated(random_frame)
    frame["signup_date"] = "2020-01-01"
    frame["outcome_date"] = (NOW - timedelta(days=100)).date().isoformat()
    config = use_case(
        uplift={"treatment_date_column": "treatment_date"}, prepare={"exclude_columns": ["outcome_date"]}
    )
    report = check(frame, config).report
    assert "FEATURE_AFTER_TREATMENT" not in codes(report)
    assert report.passed


def test_errors_are_listed_before_warnings(targeted_frame: pd.DataFrame) -> None:
    frame = dated(targeted_frame, recent_every=10)
    report = check(frame, use_case(uplift=WINDOW)).report
    assert codes(report) == ["TREATMENT_NOT_RANDOM", "OUTCOME_WINDOW_IMMATURE"]
