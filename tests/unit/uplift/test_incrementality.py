"""`engine.uplift.incrementality`: treated rate − control rate, its Newcombe interval and p-value.

The interval is pinned three ways: Newcombe's own published examples (1998, Table II, method 10),
an independent recomputation from scipy's Wilson intervals, and the report built from a frame with
exactly those counts. The p-value is checked against scipy's uncorrected chi-square test, which is
the square of the pooled z-test. Maturity, the population rules and the counters use small
hand-built frames whose answers can be read off.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus
from engine.uplift.incrementality import (
    Z_95,
    measure_incrementality,
    newcombe_interval,
    two_proportion_p_value,
    wilson_interval,
)
from tests.fixtures.make_uplift_data import make_winback_campaign

RUN_ID = "r_20260921_bbbbbbbb"
SENT = datetime(2026, 5, 1, tzinfo=UTC)
LATER = datetime(2026, 9, 1, tzinfo=UTC)


def build(
    treated: tuple[int, int],
    control: tuple[int, int],
    *,
    extra_suppressed: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scores and outcomes with `treated = (conversions, rows)` and `control = (conversions, rows)`."""
    keys: list[str] = []
    is_control: list[bool] = []
    outcome: list[int] = []
    suppressed: list[str | None] = []
    for arm_control, (conversions, rows) in ((False, treated), (True, control)):
        for index in range(rows):
            keys.append(f"{'C' if arm_control else 'T'}{index:05d}")
            is_control.append(arm_control)
            outcome.append(1 if index < conversions else 0)
            suppressed.append(None)
    for index in range(extra_suppressed):
        keys.append(f"S{index:05d}")
        is_control.append(False)
        outcome.append(1)
        suppressed.append("opted_out")
    scores = pd.DataFrame(
        {
            "customer_id": keys,
            "band": "High",
            "action": ["Control (hold out)" if c else "Call" for c in is_control],
            "control_group": is_control,
            "suppressed_reason": suppressed,
        }
    )
    outcomes = pd.DataFrame({"customer_id": keys, "converted": outcome})
    return scores, outcomes


def measure(scores: pd.DataFrame, outcomes: pd.DataFrame, **kwargs: object) -> IncrementalityReport:
    options: dict[str, object] = {
        "run_id": RUN_ID,
        "primary_key": "customer_id",
        "outcome_column": "converted",
        "treatment_time": SENT,
        "as_of": LATER,
    }
    options.update(kwargs)
    return measure_incrementality(scores, outcomes, **options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The statistics
# ---------------------------------------------------------------------------
NEWCOMBE_TABLE_II = [
    # (x1, n1, x2, n2, difference, lower, upper) - Newcombe (1998), Table II, method 10.
    (56, 70, 48, 80, 0.2000, 0.0524, 0.3339),
    (9, 10, 3, 10, 0.6000, 0.1705, 0.8090),
    (6, 7, 2, 7, 0.5714, 0.0582, 0.8062),
    (5, 56, 0, 29, 0.0893, -0.0381, 0.1926),
    (0, 10, 0, 20, 0.0000, -0.1611, 0.2775),
    (0, 10, 0, 10, 0.0000, -0.2775, 0.2775),
    (10, 10, 0, 20, 1.0000, 0.6791, 1.0000),
    (10, 10, 0, 10, 1.0000, 0.6075, 1.0000),
]


@pytest.mark.parametrize(("x1", "n1", "x2", "n2", "difference", "lower", "upper"), NEWCOMBE_TABLE_II)
def test_newcombe_matches_the_published_examples(
    x1: int, n1: int, x2: int, n2: int, difference: float, lower: float, upper: float
) -> None:
    d, lo, hi = newcombe_interval(x1, n1, x2, n2)
    assert d == pytest.approx(difference, abs=5e-5)
    assert lo == pytest.approx(lower, abs=5e-5)
    assert hi == pytest.approx(upper, abs=5e-5)


def test_wilson_matches_scipy() -> None:
    stats = pytest.importorskip("scipy.stats")
    for successes, trials in ((0, 10), (3, 10), (56, 70), (48, 80), (10, 10), (137, 4000)):
        ci = stats.binomtest(successes, trials).proportion_ci(confidence_level=0.95, method="wilson")
        low, high = wilson_interval(successes, trials)
        assert low == pytest.approx(ci.low, abs=1e-12)
        assert high == pytest.approx(ci.high, abs=1e-12)


def test_newcombe_matches_an_independent_recomputation_from_scipy_wilson() -> None:
    stats = pytest.importorskip("scipy.stats")
    rng = np.random.default_rng(0)
    for _ in range(50):
        n1, n2 = (int(v) for v in rng.integers(1, 400, size=2))
        x1, x2 = int(rng.integers(0, n1 + 1)), int(rng.integers(0, n2 + 1))
        w1 = stats.binomtest(x1, n1).proportion_ci(method="wilson")
        w2 = stats.binomtest(x2, n2).proportion_ci(method="wilson")
        p1, p2 = x1 / n1, x2 / n2
        expected_low = (p1 - p2) - math.hypot(p1 - w1.low, w2.high - p2)
        expected_high = (p1 - p2) + math.hypot(w1.high - p1, p2 - w2.low)
        d, lo, hi = newcombe_interval(x1, n1, x2, n2)
        assert d == pytest.approx(p1 - p2, abs=1e-12)
        assert lo == pytest.approx(expected_low, abs=1e-10)
        assert hi == pytest.approx(expected_high, abs=1e-10)
        assert -1.0 <= lo <= d <= hi <= 1.0


def test_p_value_is_the_pooled_z_test() -> None:
    # Hand-worked: pooled rate 104/150; se = sqrt(p(1−p)(1/70 + 1/80)); z = 0.2/se ≈ 2.6502.
    pooled = 104 / 150
    z = 0.2 / math.sqrt(pooled * (1 - pooled) * (1 / 70 + 1 / 80))
    assert z == pytest.approx(2.6502, abs=1e-4)
    p = two_proportion_p_value(56, 70, 48, 80)
    assert p == pytest.approx(math.erfc(z / math.sqrt(2)), rel=1e-12)
    assert p == pytest.approx(0.00805, abs=1e-5)


def test_p_value_matches_scipy_uncorrected_chi_square() -> None:
    stats = pytest.importorskip("scipy.stats")
    for x1, n1, x2, n2 in ((56, 70, 48, 80), (9, 10, 3, 10), (120, 1000, 90, 1000), (5, 56, 1, 29)):
        table = np.array([[x1, n1 - x1], [x2, n2 - x2]])
        chi2 = stats.chi2_contingency(table, correction=False)
        assert two_proportion_p_value(x1, n1, x2, n2) == pytest.approx(chi2.pvalue, rel=1e-9)


def test_p_value_is_none_when_undefined() -> None:
    assert two_proportion_p_value(0, 10, 0, 20) is None
    assert two_proportion_p_value(10, 10, 20, 20) is None
    assert two_proportion_p_value(1, 10, 0, 0) is None


def test_z_is_the_exact_quantile() -> None:
    assert pytest.approx(1.959963984540054, abs=1e-12) == Z_95


def test_wilson_refuses_an_empty_arm() -> None:
    with pytest.raises(ValueError, match="Wilson"):
        wilson_interval(0, 0)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def test_known_rates_give_the_exact_lift_interval_and_p_value() -> None:
    scores, outcomes = build((56, 70), (48, 80), extra_suppressed=7)
    report = measure(scores, outcomes, outcome_window_days=90, campaign_id="winback-may")

    assert report.status is IncrementalityStatus.MATURE
    assert report.results_available_on is None
    assert (report.treated_rows, report.treated_conversions) == (70, 56)
    assert (report.control_rows, report.control_conversions) == (80, 48)
    assert report.treated_rate == pytest.approx(0.8)
    assert report.control_rate == pytest.approx(0.6)
    assert report.absolute_lift is not None
    assert report.absolute_lift.value == pytest.approx(0.2)
    assert report.absolute_lift.ci_low == pytest.approx(0.0524, abs=5e-5)
    assert report.absolute_lift.ci_high == pytest.approx(0.3339, abs=5e-5)
    assert report.absolute_lift.confidence_level == 0.95
    assert report.relative_lift == pytest.approx(0.2 / 0.6)
    assert report.incremental_conversions is not None
    assert report.incremental_conversions.value == pytest.approx(0.2 * 70)
    assert report.incremental_conversions.ci_low == pytest.approx(report.absolute_lift.ci_low * 70)
    assert report.incremental_conversions.ci_high == pytest.approx(report.absolute_lift.ci_high * 70)
    assert report.p_value == pytest.approx(two_proportion_p_value(56, 70, 48, 80))
    assert report.rows_suppressed_or_untreated == 7
    assert report.rows_immature == 0
    assert report.rows_without_outcome == 0
    assert report.causal is True
    assert report.campaign_id == "winback-may"
    assert "80.0%" in report.summary and "60.0%" in report.summary and "+20.0 points" in report.summary
    # The artefact round-trips through its JSON form.
    assert IncrementalityReport.model_validate_json(report.model_dump_json()) == report


def test_an_interval_including_zero_says_so() -> None:
    scores, outcomes = build((11, 100), (10, 100))
    report = measure(scores, outcomes)
    assert report.absolute_lift is not None and not report.absolute_lift.excludes_zero
    assert "includes zero" in report.summary


def test_immature_rows_are_excluded_and_counted() -> None:
    scores, outcomes = build((30, 60), (10, 40))
    dates = np.where(np.arange(len(outcomes)) % 4 == 0, "2026-08-20", "2026-05-01")
    outcomes["treatment_date"] = dates
    report = measure(
        scores,
        outcomes,
        treatment_date_column="treatment_date",
        outcome_window_days=30,
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
    )
    immature = dates == "2026-08-20"
    is_control = scores["control_group"].to_numpy()
    converted = outcomes["converted"].to_numpy()
    assert report.status is IncrementalityStatus.MATURE
    assert report.rows_immature == int(immature.sum())
    assert report.treated_rows == int((~immature & ~is_control).sum())
    assert report.control_rows == int((~immature & is_control).sum())
    assert report.treated_conversions == int(converted[~immature & ~is_control].sum())
    assert report.control_conversions == int(converted[~immature & is_control].sum())
    # The last row matures on 2026-08-20 + 30 days.
    assert report.results_available_on == date(2026, 9, 19)
    assert "2026-09-19" in report.summary


def test_all_immature_gives_no_rates_and_a_date() -> None:
    scores, outcomes = build((30, 60), (10, 40))
    report = measure(
        scores,
        outcomes,
        treatment_time=datetime(2026, 8, 25, 15, 0, tzinfo=UTC),
        outcome_window_days=90,
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert report.status is IncrementalityStatus.IMMATURE
    assert report.results_available_on == date(2026, 11, 23)
    assert report.rows_immature == 100
    assert report.treated_rows == report.control_rows == 0
    assert report.treated_rate is None and report.control_rate is None
    assert report.absolute_lift is None and report.relative_lift is None
    assert report.incremental_conversions is None and report.p_value is None
    assert report.summary.startswith("Results available on 2026-11-23")


def test_maturity_boundary_is_inclusive() -> None:
    scores, outcomes = build((5, 10), (2, 10))
    report = measure(scores, outcomes, outcome_window_days=30, as_of=datetime(2026, 5, 31, tzinfo=UTC))
    assert report.status is IncrementalityStatus.MATURE and report.rows_immature == 0


def test_without_a_window_every_row_is_mature() -> None:
    scores, outcomes = build((5, 10), (2, 10))
    report = measure(scores, outcomes, as_of=datetime(2026, 5, 1, tzinfo=UTC))
    assert report.status is IncrementalityStatus.MATURE
    assert report.treated_rows == 10 and report.results_available_on is None


def test_missing_outcomes_and_unreadable_dates_are_counted_not_guessed() -> None:
    scores, outcomes = build((5, 10), (2, 10))
    outcomes["treatment_date"] = "2026-05-01"
    outcomes.loc[0, "treatment_date"] = "not a date"  # a treated converter
    outcomes.loc[15, "converted"] = None  # a control row
    outcomes = outcomes.drop(index=[1, 2])  # two treated converters have no outcome row
    report = measure(scores, outcomes, treatment_date_column="treatment_date", outcome_window_days=30)
    assert report.rows_without_outcome == 4
    assert (report.treated_rows, report.treated_conversions) == (7, 2)
    assert (report.control_rows, report.control_conversions) == (9, 2)


def test_intended_column_restricts_both_arms() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    scores["intended_treatment"] = [True] * 5 + [False] * 5 + [True] * 3 + [False] * 7
    report = measure(scores, outcomes, intended_column="intended_treatment")
    # Treated: rows 0-4 (all converters); control: rows 10-12 (all converters).
    assert (report.treated_rows, report.treated_conversions) == (5, 5)
    assert (report.control_rows, report.control_conversions) == (3, 3)
    assert report.rows_suppressed_or_untreated == 12


def test_intended_column_read_from_csv_text() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    scores["intended_treatment"] = ["True"] * 5 + ["False"] * 5 + ["true"] * 3 + ["false"] * 7
    scores["control_group"] = scores["control_group"].map({True: "True", False: "False"})
    report = measure(scores, outcomes, intended_column="intended_treatment")
    assert (report.treated_rows, report.control_rows) == (5, 3)


def test_bands_restrict_both_arms() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    scores["band"] = ["High", "Low"] * 10
    report = measure(scores, outcomes, bands=["High"])
    assert (report.treated_rows, report.control_rows) == (5, 5)
    assert report.rows_suppressed_or_untreated == 10


def test_positive_label_and_text_outcomes() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    outcomes["converted"] = np.where(outcomes["converted"] == 1, "Reactivated", "Lapsed")
    report = measure(scores, outcomes, positive_label="reactivated")
    assert (report.treated_conversions, report.control_conversions) == (6, 4)

    outcomes["converted"] = np.where(outcomes["converted"] == "Reactivated", "yes", "no")
    report = measure(scores, outcomes)
    assert (report.treated_conversions, report.control_conversions) == (6, 4)


def test_a_non_binary_outcome_is_refused() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    outcomes["converted"] = outcomes["converted"] * 3 + np.arange(len(outcomes)) % 2
    with pytest.raises(ValueError, match="1/true/yes/y"):
        measure(scores, outcomes)
    with pytest.raises(ValueError, match="binary"):
        measure(scores, outcomes, positive_label="3")


def test_keys_match_across_types_and_duplicates_are_refused() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    scores["customer_id"] = range(20)
    outcomes["customer_id"] = [str(value) for value in range(20)]
    assert measure(scores, outcomes).treated_rows == 10
    outcomes.loc[3, "customer_id"] = "2"
    with pytest.raises(ValueError, match="repeats 1"):
        measure(scores, outcomes)


def test_a_missing_column_is_named() -> None:
    scores, outcomes = build((6, 10), (4, 10))
    with pytest.raises(ValueError, match="'reactivated'"):
        measure(scores, outcomes, outcome_column="reactivated")
    with pytest.raises(ValueError, match="'intended_treatment'"):
        measure(scores, outcomes, intended_column="intended_treatment")


def test_no_control_group_is_not_causal_and_has_no_lift() -> None:
    scores, outcomes = build((6, 10), (0, 0))
    report = measure(scores, outcomes)
    assert report.causal is False
    assert report.control_rate is None and report.absolute_lift is None
    assert "no control group" in report.summary


def test_zero_control_rate_leaves_relative_lift_null() -> None:
    scores, outcomes = build((3, 10), (0, 10))
    report = measure(scores, outcomes)
    assert report.control_rate == 0.0 and report.relative_lift is None
    assert report.absolute_lift is not None and report.absolute_lift.value == pytest.approx(0.3)


def test_winback_campaign_recovers_the_true_average_effect() -> None:
    campaign = make_winback_campaign(n=12_000, seed=11)
    frame = campaign.frame
    scores = pd.DataFrame(
        {
            "customer_id": frame["customer_id"],
            "band": "All",
            "action": "Send",
            "control_group": frame["treatment"] == 0,
            "suppressed_reason": None,
        }
    )
    outcomes = frame[["customer_id", "reactivated_90d", "treatment_date"]]
    report = measure_incrementality(
        scores,
        outcomes,
        run_id=RUN_ID,
        primary_key="customer_id",
        outcome_column="reactivated_90d",
        treatment_time=SENT,
        treatment_date_column="treatment_date",
        outcome_window_days=90,
        as_of=datetime(2026, 8, 15, tzinfo=UTC),
    )
    true_effect = float(campaign.truth["true_uplift"].mean())
    assert report.status is IncrementalityStatus.MATURE
    assert report.treated_rows + report.control_rows == 12_000
    assert report.absolute_lift is not None
    assert report.absolute_lift.ci_low is not None and report.absolute_lift.ci_high is not None
    assert report.absolute_lift.ci_low <= true_effect <= report.absolute_lift.ci_high


def test_a_lift_that_rounds_to_zero_is_never_printed_with_a_minus_sign() -> None:
    # 499/2500 = 19.96% against 50/250 = 20.00%: a lift of -0.04 points, printed to one decimal.
    scores, outcomes = build((499, 2_500), (50, 250))
    report = measure(scores, outcomes)
    assert report.absolute_lift is not None and report.absolute_lift.value == pytest.approx(-0.0004)
    assert "-0.0 points" not in report.summary
    assert "a difference of +0.0 points" in report.summary
