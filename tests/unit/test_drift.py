"""The drift format end to end: `register.drift_baseline` writes it, `score.compute_drift` reads it.

Producer and consumer are tested together, because a PSI whose two sides bin differently is not a
number anyone can act on.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.config import ResolvedConfig, UseCaseConfig, resolve_config
from engine.contracts import CategoryCount, DriftBaseline, DriftStatus, FeatureBaseline, HistogramBin
from engine.stages.register import (
    MAX_CATEGORY_LEVELS,
    NUMERIC_BIN_COUNT,
    OTHER_CATEGORY,
    drift_baseline,
)
from engine.stages.score import PSI_EPSILON, _drift_status, _psi, compute_drift
from engine.utils.time import utc_now

USE_CASE: str = "targeted-advertisement"
PRIMARY_KEY: str = "customer_id"
TRAIN_RUN: str = "r_20260921_0000beef"
SCORE_RUN: str = "r_20260922_0000cafe"
MODEL_ID: str = "m_targeted-advertisement_1"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------
@pytest.fixture
def config(config_root: Path) -> UseCaseConfig:
    """The shipped use case, whose drift threshold is the engine default of 0.20."""
    return resolve_config(USE_CASE, root=config_root).config


def with_threshold(config_root: Path, threshold: float) -> UseCaseConfig:
    """The same use case with another PSI threshold, as a run override would set it."""
    resolved: ResolvedConfig = resolve_config(
        USE_CASE, {"monitoring.drift_psi_threshold": threshold}, root=config_root
    )
    return resolved.config


def baseline_of(frame: pd.DataFrame, config: UseCaseConfig) -> DriftBaseline:
    """The drift baseline of a training frame for the use case under test."""
    return drift_baseline(frame, config, run_id=TRAIN_RUN, model_version_id=MODEL_ID, primary_key=PRIMARY_KEY)


def features_of(baseline: DriftBaseline) -> dict[str, FeatureBaseline]:
    return {feature.feature: feature for feature in baseline.features}


def train_frame(rows: int = 100) -> pd.DataFrame:
    """A training frame with one column of every shape the baseline has to handle."""
    return pd.DataFrame(
        {
            "customer_id": [f"C-{index}" for index in range(rows)],
            "visits_last_7d": [index % 20 for index in range(rows)],
            "ad_ctr_90d": [index / rows for index in range(rows)],
            "plan_tier": ["premium" if index % 4 == 0 else "basic" for index in range(rows)],
            "tenure_months": [12.0] * rows,  # constant
            "region": [None] * rows,  # never observed
            "skewed_spend": [0.0] * (rows - rows // 3) + [float(index) for index in range(rows // 3)],
            "converted_30d": [index % 2 for index in range(rows)],
        }
    )


def two_bin_baseline() -> DriftBaseline:
    """A hand-written baseline: one numeric feature, two bins of half the training mass each."""
    return DriftBaseline(
        run_id=TRAIN_RUN,
        model_version_id=MODEL_ID,
        rows=100,
        bin_count=2,
        features=(
            FeatureBaseline(
                feature="visits",
                kind="numeric",
                null_rate=0.0,
                bins=(
                    HistogramBin(lower=0.0, upper=1.0, count=50, share=0.5),
                    HistogramBin(lower=1.0, upper=2.0, count=50, share=0.5),
                ),
                categories=(),
                mean=1.0,
                std=0.5,
            ),
        ),
        created_at=utc_now(),
    )


def categorical_baseline(*categories: tuple[str, float]) -> DriftBaseline:
    """A hand-written baseline with one categorical feature and the given levels and shares."""
    return DriftBaseline(
        run_id=TRAIN_RUN,
        model_version_id=MODEL_ID,
        rows=100,
        bin_count=NUMERIC_BIN_COUNT,
        features=(
            FeatureBaseline(
                feature="plan_tier",
                kind="categorical",
                null_rate=0.0,
                bins=(),
                categories=tuple(
                    CategoryCount(value=value, count=round(share * 100), share=share)
                    for value, share in categories
                ),
                mean=None,
                std=None,
            ),
        ),
        created_at=utc_now(),
    )


# ---------------------------------------------------------------------------
# The baseline: what the training frame becomes
# ---------------------------------------------------------------------------
def test_a_numeric_column_is_binned_into_quantiles(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    feature = features_of(baseline)["ad_ctr_90d"]
    assert baseline.bin_count == NUMERIC_BIN_COUNT
    assert feature.kind == "numeric"
    assert len(feature.bins) == NUMERIC_BIN_COUNT
    assert [bin_.count for bin_ in feature.bins] == [10] * NUMERIC_BIN_COUNT  # equal frequency
    assert sum(bin_.share for bin_ in feature.bins) == pytest.approx(1.0)
    assert sum(bin_.count for bin_ in feature.bins) == 100
    assert [bin_.lower for bin_ in feature.bins[1:]] == [bin_.upper for bin_ in feature.bins[:-1]]
    assert feature.bins[0].lower == 0.0
    assert feature.bins[-1].upper == 0.99
    assert feature.mean == pytest.approx(0.495)
    assert feature.std is not None and feature.std > 0.0
    assert feature.null_rate == 0.0
    assert feature.categories == ()


def test_a_categorical_column_becomes_a_frequency_table(config: UseCaseConfig) -> None:
    feature = features_of(baseline_of(train_frame(), config))["plan_tier"]
    assert feature.kind == "categorical"
    assert feature.bins == ()
    assert [(category.value, category.count) for category in feature.categories] == [
        ("basic", 75),
        ("premium", 25),
    ]
    assert [category.share for category in feature.categories] == [0.75, 0.25]
    assert feature.mean is None and feature.std is None


def test_a_constant_column_keeps_one_zero_width_bin(config: UseCaseConfig) -> None:
    feature = features_of(baseline_of(train_frame(), config))["tenure_months"]
    assert len(feature.bins) == 1
    assert (feature.bins[0].lower, feature.bins[0].upper) == (12.0, 12.0)
    assert (feature.bins[0].count, feature.bins[0].share) == (100, 1.0)
    assert feature.std == 0.0


def test_a_column_that_was_never_observed_has_no_distribution(config: UseCaseConfig) -> None:
    feature = features_of(baseline_of(train_frame(), config))["region"]
    assert feature.null_rate == 1.0
    assert feature.bins == () and feature.categories == ()
    assert feature.mean is None and feature.std is None


def test_a_skewed_column_keeps_fewer_but_valid_bins(config: UseCaseConfig) -> None:
    feature = features_of(baseline_of(train_frame(), config))["skewed_spend"]
    assert 1 < len(feature.bins) < NUMERIC_BIN_COUNT  # the repeated quantiles collapse
    assert sum(bin_.count for bin_ in feature.bins) == 100
    assert sum(bin_.share for bin_ in feature.bins) == pytest.approx(1.0)
    assert feature.bins[0].lower == 0.0
    assert feature.bins[-1].upper == 32.0
    assert [bin_.lower for bin_ in feature.bins[1:]] == [bin_.upper for bin_ in feature.bins[:-1]]


def test_a_column_that_is_almost_all_one_value_collapses_to_one_bin(config: UseCaseConfig) -> None:
    """Quantile binning has a floor: 95 % of one value leaves a single bin spanning the range."""
    frame = train_frame()
    frame["skewed_spend"] = [0.0] * 95 + [10.0, 20.0, 30.0, 40.0, 500.0]
    feature = features_of(baseline_of(frame, config))["skewed_spend"]
    assert len(feature.bins) == 1
    assert (feature.bins[0].lower, feature.bins[0].upper) == (0.0, 500.0)
    assert feature.bins[0].share == 1.0


def test_nulls_are_recorded_and_excluded_from_the_shares(config: UseCaseConfig) -> None:
    frame = train_frame()
    frame.loc[:19, "ad_ctr_90d"] = None
    feature = features_of(baseline_of(frame, config))["ad_ctr_90d"]
    assert feature.null_rate == 0.2
    assert sum(bin_.count for bin_ in feature.bins) == 80
    assert sum(bin_.share for bin_ in feature.bins) == pytest.approx(1.0)


def test_infinities_count_as_missing(config: UseCaseConfig) -> None:
    frame = train_frame()
    frame.loc[0, "ad_ctr_90d"] = float("inf")
    frame.loc[1, "ad_ctr_90d"] = float("-inf")
    feature = features_of(baseline_of(frame, config))["ad_ctr_90d"]
    assert feature.null_rate == 0.02
    assert all(math.isfinite(bin_.lower) and math.isfinite(bin_.upper) for bin_ in feature.bins)


def test_a_high_cardinality_column_buckets_its_tail(config: UseCaseConfig) -> None:
    rows = 400
    levels = [f"tier-{index % (MAX_CATEGORY_LEVELS + 10)}" for index in range(rows)]
    frame = train_frame(rows)
    frame["plan_tier"] = levels
    feature = features_of(baseline_of(frame, config))["plan_tier"]
    assert len(feature.categories) == MAX_CATEGORY_LEVELS + 1
    assert feature.categories[-1].value == OTHER_CATEGORY
    assert sum(category.count for category in feature.categories) == rows
    assert sum(category.share for category in feature.categories) == pytest.approx(1.0)


def test_booleans_are_categorical_and_datetimes_numeric(config: UseCaseConfig) -> None:
    frame = train_frame(10)
    frame["weather_alert"] = [index % 2 == 0 for index in range(10)]
    frame["seen_at"] = pd.to_datetime([f"2026-08-{index + 1:02d}" for index in range(10)])
    features = features_of(baseline_of(frame, config))
    assert features["weather_alert"].kind == "categorical"
    assert {category.value for category in features["weather_alert"].categories} == {"True", "False"}
    assert features["seen_at"].kind == "numeric"
    assert features["seen_at"].bins[0].lower == pd.Timestamp("2026-08-01").timestamp()  # epoch seconds


def test_a_missing_timestamp_is_missing_and_not_the_smallest_number(config: UseCaseConfig) -> None:
    frame = train_frame(10)
    frame["seen_at"] = pd.to_datetime([None] + [f"2026-08-{index + 1:02d}" for index in range(9)])
    feature = features_of(baseline_of(frame, config))["seen_at"]
    assert feature.null_rate == 0.1
    assert sum(bin_.count for bin_ in feature.bins) == 9
    assert feature.bins[0].lower == pd.Timestamp("2026-08-01").timestamp()


def test_a_numeric_column_with_no_training_values_compares_against_nothing(
    config: UseCaseConfig,
) -> None:
    frame = train_frame()
    frame["ad_ctr_90d"] = float("nan")  # numeric dtype, never observed
    baseline = baseline_of(frame, config)
    assert features_of(baseline)["ad_ctr_90d"].bins == ()
    report = compute_drift(baseline, train_frame(), config, run_id=SCORE_RUN)
    drift = {feature.feature: feature for feature in report.features}["ad_ctr_90d"]
    assert (drift.psi, drift.status) == (0.0, DriftStatus.STABLE)
    assert (drift.null_rate_baseline, drift.null_rate_current) == (1.0, 0.0)


def test_the_baseline_covers_the_feature_columns_only(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    names = [feature.feature for feature in baseline.features]
    assert names == [
        "visits_last_7d",
        "ad_ctr_90d",
        "plan_tier",
        "tenure_months",
        "region",
        "skewed_spend",
    ]
    assert baseline.rows == 100
    assert baseline.run_id == TRAIN_RUN
    assert baseline.model_version_id == MODEL_ID
    assert baseline.created_at.tzinfo is not None


def test_the_baseline_is_deterministic(config: UseCaseConfig) -> None:
    frame = train_frame()
    first = baseline_of(frame, config)
    second = baseline_of(frame, config)
    assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})


# ---------------------------------------------------------------------------
# PSI arithmetic
# ---------------------------------------------------------------------------
def test_psi_matches_a_hand_computed_expectation(config: UseCaseConfig) -> None:
    frame = pd.DataFrame({"visits": [0.5, 0.5] + [1.5] * 8})
    report = compute_drift(two_bin_baseline(), frame, config, run_id=SCORE_RUN)
    # two of ten rows in [0, 1), eight in [1, 2]; the baseline expects half in each
    expected = (0.2 - 0.5) * math.log(0.2 / 0.5) + (0.8 - 0.5) * math.log(0.8 / 0.5)
    assert expected == pytest.approx(0.4158883083)
    assert report.features[0].psi == pytest.approx(round(expected, 4))
    assert report.max_psi == report.features[0].psi


def test_psi_is_zero_when_the_file_matches_the_training_data(config: UseCaseConfig) -> None:
    frame = train_frame()
    report = compute_drift(baseline_of(frame, config), frame, config, run_id=SCORE_RUN)
    assert [feature.psi for feature in report.features] == [0.0] * len(report.features)
    assert report.max_psi == 0.0
    assert report.status is DriftStatus.STABLE
    assert report.summary == "PSI 0.00, stable"
    assert report.drifted_features == ()


def test_psi_is_large_when_the_distributions_are_disjoint(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    frame = train_frame()
    frame["plan_tier"] = "gold"  # not a level the model ever saw
    report = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    drift = {feature.feature: feature for feature in report.features}["plan_tier"]
    assert drift.psi > 10.0
    assert drift.status is DriftStatus.DRIFTED
    assert "plan_tier" in report.drifted_features
    assert report.status is DriftStatus.DRIFTED


def test_an_unseen_category_lands_in_the_other_bucket_when_there_is_one(config: UseCaseConfig) -> None:
    frame = pd.DataFrame({"plan_tier": ["zzz"] * 10})
    with_bucket = compute_drift(
        categorical_baseline(("basic", 0.5), (OTHER_CATEGORY, 0.5)), frame, config, run_id=SCORE_RUN
    )
    without_bucket = compute_drift(
        categorical_baseline(("basic", 0.5), ("premium", 0.5)), frame, config, run_id=SCORE_RUN
    )
    assert 0.0 < with_bucket.max_psi < without_bucket.max_psi
    assert with_bucket.status is DriftStatus.DRIFTED


def test_a_numeric_value_outside_every_bin_is_clamped_into_the_edge_bin(config: UseCaseConfig) -> None:
    baseline = two_bin_baseline()
    far_above = compute_drift(baseline, pd.DataFrame({"visits": [100.0] * 10}), config, run_id=SCORE_RUN)
    at_the_edge = compute_drift(baseline, pd.DataFrame({"visits": [1.5] * 10}), config, run_id=SCORE_RUN)
    assert far_above.features[0].psi == at_the_edge.features[0].psi
    assert math.isfinite(far_above.max_psi)
    assert far_above.status is DriftStatus.DRIFTED


def test_a_constant_baseline_notices_a_different_constant(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    same = compute_drift(baseline, train_frame(), config, run_id=SCORE_RUN)
    frame = train_frame()
    frame["tenure_months"] = 99.0
    moved = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    assert {feature.feature: feature for feature in same.features}["tenure_months"].psi == 0.0
    drift = {feature.feature: feature for feature in moved.features}["tenure_months"]
    assert drift.psi > 10.0
    assert drift.status is DriftStatus.DRIFTED


def test_a_feature_missing_from_the_file_reads_as_entirely_null(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    frame = train_frame().drop(columns=["ad_ctr_90d"])
    report = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    drift = {feature.feature: feature for feature in report.features}["ad_ctr_90d"]
    assert drift.null_rate_current == 1.0
    assert drift.psi > 10.0
    assert drift.status is DriftStatus.DRIFTED


def test_a_feature_that_is_present_but_all_null_behaves_the_same(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    missing = compute_drift(baseline, train_frame().drop(columns=["ad_ctr_90d"]), config, run_id=SCORE_RUN)
    emptied = train_frame()
    emptied["ad_ctr_90d"] = None
    all_null = compute_drift(baseline, emptied, config, run_id=SCORE_RUN)
    by_name = {feature.feature: feature for feature in all_null.features}
    assert by_name["ad_ctr_90d"].null_rate_current == 1.0
    assert by_name["ad_ctr_90d"].psi == {f.feature: f for f in missing.features}["ad_ctr_90d"].psi


def test_a_feature_with_no_training_distribution_has_nothing_to_compare(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    frame = train_frame()
    frame["region"] = "south"  # values now, none at training time
    report = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    drift = {feature.feature: feature for feature in report.features}["region"]
    assert drift.psi == 0.0
    assert drift.status is DriftStatus.STABLE
    assert drift.null_rate_baseline == 1.0
    assert drift.null_rate_current == 0.0


def test_an_empty_file_is_not_measured_rather_than_reported_as_drifted(
    config: UseCaseConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """A file with no rows is not a comparison, so it gets no verdict at all.

    An empty file puts no mass in any bin, so every baseline bin reads as fully emptied and PSI
    comes out at its maximum: the report used to call an empty upload heavily drifted, which is a
    statement about data that is not there rather than about the data. The same baseline still
    measures a file that does have rows, so this is about the rows and not about the baseline.
    """
    baseline = baseline_of(train_frame(), config)

    with caplog.at_level("WARNING", logger="engine.stages.score"):
        assert compute_drift(baseline, train_frame().iloc[:0], config, run_id=SCORE_RUN) is None
    assert "drift=unavailable" in caplog.text and "reason=no_rows_to_compare" in caplog.text

    populated = compute_drift(baseline, train_frame(), config, run_id=SCORE_RUN)
    assert populated is not None and populated.status is DriftStatus.STABLE


def test_a_baseline_with_no_features_is_not_measured_rather_than_reported_as_stable(
    config: UseCaseConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """A baseline that named no feature has nothing to compare, and says so.

    The per-feature sum over no features is zero, so such a baseline used to report "PSI 0.00,
    stable" - a clean bill of health for a comparison that never happened, which is the one thing
    a monitoring signal must never say.
    """
    empty = DriftBaseline(
        run_id=TRAIN_RUN,
        model_version_id=MODEL_ID,
        rows=0,
        bin_count=NUMERIC_BIN_COUNT,
        features=(),
        created_at=utc_now(),
    )

    with caplog.at_level("WARNING", logger="engine.stages.score"):
        assert compute_drift(empty, train_frame(), config, run_id=SCORE_RUN) is None
    assert "drift=unavailable" in caplog.text and "reason=baseline_has_no_features" in caplog.text
    assert MODEL_ID in caplog.text, "the log names the model version that stored the baseline"


def test_null_rates_are_reported_on_both_sides(config: UseCaseConfig) -> None:
    frame = train_frame()
    frame.loc[:19, "ad_ctr_90d"] = None
    baseline = baseline_of(frame, config)
    current = train_frame()
    current.loc[:49, "ad_ctr_90d"] = None
    report = compute_drift(baseline, current, config, run_id=SCORE_RUN)
    drift = {feature.feature: feature for feature in report.features}["ad_ctr_90d"]
    assert (drift.null_rate_baseline, drift.null_rate_current) == (0.2, 0.5)


# ---------------------------------------------------------------------------
# The three status levels and the report
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("psi", "threshold", "expected"),
    [
        (0.2, 0.2, DriftStatus.DRIFTED),  # at the threshold
        (0.1999, 0.2, DriftStatus.WATCH),  # just below it
        (0.1, 0.2, DriftStatus.WATCH),  # at half the threshold
        (0.0999, 0.2, DriftStatus.STABLE),  # just below half
        (0.0, 0.2, DriftStatus.STABLE),
        (0.06, 0.2, DriftStatus.STABLE),  # the prototype's "PSI 0.06, stable"
        (0.11, 0.2, DriftStatus.WATCH),  # the prototype's "PSI 0.11, watch"
    ],
)
def test_the_three_levels_at_their_boundaries(psi: float, threshold: float, expected: DriftStatus) -> None:
    assert _drift_status(psi, threshold) is expected


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (1.0, DriftStatus.STABLE),  # watch starts at 0.50
        (0.8318, DriftStatus.WATCH),  # watch starts at exactly this PSI
        (0.4159, DriftStatus.DRIFTED),  # the threshold is exactly this PSI
    ],
)
def test_the_configured_threshold_decides_the_verdict_of_a_run(
    config_root: Path, threshold: float, expected: DriftStatus
) -> None:
    config = with_threshold(config_root, threshold)
    frame = pd.DataFrame({"visits": [0.5, 0.5] + [1.5] * 8})
    report = compute_drift(two_bin_baseline(), frame, config, run_id=SCORE_RUN)
    assert report.features[0].psi == 0.4159
    assert report.threshold == threshold
    assert report.status is expected
    assert report.summary == f"PSI 0.42, {expected.value}"
    assert report.drifted_features == (("visits",) if expected is DriftStatus.DRIFTED else ())


def test_the_prototype_deployment_lines_are_reproducible() -> None:
    for max_psi, line in ((0.06, "PSI 0.06, stable"), (0.11, "PSI 0.11, watch")):
        assert f"PSI {max_psi:.2f}, {_drift_status(max_psi, 0.2).value}" == line


def test_the_report_links_the_run_the_baseline_and_the_model(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    report = compute_drift(baseline, train_frame(), config, run_id=SCORE_RUN)
    assert report.run_id == SCORE_RUN
    assert report.baseline_run_id == TRAIN_RUN
    assert report.model_version_id == MODEL_ID
    assert report.threshold == config.monitoring.drift_psi_threshold == 0.2
    assert {feature.feature for feature in report.features} == {
        feature.feature for feature in baseline.features
    }
    assert report.computed_at.tzinfo is not None


def test_the_features_are_sorted_most_drifted_first(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    frame = train_frame()
    frame["plan_tier"] = "gold"
    frame["ad_ctr_90d"] = [index / 100 * 0.9 for index in range(100)]
    report = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    psis = [feature.psi for feature in report.features]
    assert psis == sorted(psis, reverse=True)
    assert report.features[0].feature == "plan_tier"
    assert report.max_psi == psis[0]
    assert list(report.drifted_features) == [
        feature.feature for feature in report.features if feature.psi >= report.threshold
    ]


def test_a_drifted_run_is_logged_as_a_warning(
    config: UseCaseConfig, caplog: pytest.LogCaptureFixture
) -> None:
    baseline = baseline_of(train_frame(), config)
    frame = train_frame()
    frame["plan_tier"] = "gold"
    with caplog.at_level("WARNING", logger="engine.stages.score"):
        compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    assert "drift=exceeded" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="engine.stages.score"):
        compute_drift(baseline, train_frame(), config, run_id=SCORE_RUN)
    assert caplog.text == ""


def test_computing_drift_twice_gives_the_same_report(config: UseCaseConfig) -> None:
    baseline = baseline_of(train_frame(), config)
    frame = train_frame()
    frame["plan_tier"] = "gold"
    first = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    second = compute_drift(baseline, frame, config, run_id=SCORE_RUN)
    assert first.model_dump(exclude={"computed_at"}) == second.model_dump(exclude={"computed_at"})


# ---------------------------------------------------------------------------
# Invariants of the PSI formula itself
# ---------------------------------------------------------------------------
_SHARES = st.lists(
    st.tuples(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    ),
    min_size=1,
    max_size=12,
)


def _vectors(pairs: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """Two share vectors of the same length, each normalised when it holds any mass at all."""
    expected = [pair[0] for pair in pairs]
    actual = [pair[1] for pair in pairs]
    return _normalised(expected), _normalised(actual)


def _normalised(shares: list[float]) -> list[float]:
    total = math.fsum(shares)
    return shares if total == 0.0 else [share / total for share in shares]


@settings(deadline=None)
@given(pairs=_SHARES)
def test_psi_is_never_negative(pairs: list[tuple[float, float]]) -> None:
    expected, actual = _vectors(pairs)
    assert _psi(expected, actual) >= 0.0


@settings(deadline=None)
@given(pairs=_SHARES)
def test_psi_is_zero_for_two_identical_distributions(pairs: list[tuple[float, float]]) -> None:
    expected, _ = _vectors(pairs)
    assert _psi(expected, expected) == 0.0


@settings(deadline=None)
@given(pairs=_SHARES)
def test_psi_is_symmetric_under_swapping(pairs: list[tuple[float, float]]) -> None:
    expected, actual = _vectors(pairs)
    assert _psi(expected, actual) == pytest.approx(_psi(actual, expected), rel=1e-9, abs=1e-12)


def test_the_epsilon_keeps_an_empty_bin_finite() -> None:
    assert PSI_EPSILON == 1e-6
    emptied = _psi([0.5, 0.5], [1.0, 0.0])
    assert math.isfinite(emptied)
    assert emptied == pytest.approx(
        (1.0 - 0.5) * math.log(1.0 / 0.5) + (PSI_EPSILON - 0.5) * math.log(PSI_EPSILON / 0.5)
    )
