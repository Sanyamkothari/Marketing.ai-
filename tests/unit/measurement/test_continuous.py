"""Plan J M102 (DEC-1312): campaigns measured on an amount, and the adjusted (CUPED) estimate.

Every measurement here runs through the product's own functions - `measure_incrementality` and
`measure_campaign` - on campaigns drawn by `engine.measurement.simulate.revenue_campaign`, whose true
difference in means is known. The statistics underneath (Welch's interval, Student's t without a
statistics library) are checked against `scipy`, which the test environment has.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime

import numpy as np
import pytest

from engine.measurement.campaign import INTENDED_COLUMN, build_assignment
from engine.measurement.continuous import (
    CONTINUOUS_CODES,
    COVARIATE_NOT_BEFORE_CAMPAIGN,
    OUTCOME_SKEWED,
    SKEW_RULE,
    CovariateNotBeforeCampaignError,
    adjusted_difference,
    mean_difference,
    skewness,
    t_quantile,
    t_two_sided,
)
from engine.measurement.measure import amount_verdict, campaign_verdict_for, measure_campaign
from engine.measurement.plan import (
    TestPlan,
    TestPlanChangedError,
    TestPlanInput,
    freeze_plan,
    realised_population,
)
from engine.measurement.simulate import (
    COVARIATE_COLUMN,
    COVARIATE_DATE_COLUMN,
    KEY_COLUMN,
    REVENUE_COLUMN,
    TREATMENT_DATE_COLUMN,
    multi_arm_campaign,
    revenue_campaign,
)
from engine.pilot.plain import jargon_in
from engine.uplift.contracts import IncrementalityStatus
from engine.uplift.incrementality import measure_incrementality
from engine.uplift.measure import VerdictKind

REGISTERED = datetime(2026, 4, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _plan(assignment: object, **extra: object) -> TestPlan:
    decided = TestPlanInput.model_validate(
        {
            "metric": "revenue in the 30 days after the campaign",
            "outcome_column": REVENUE_COLUMN,
            "outcome_kind": "continuous",
            "outcome_window_days": 30,
            "analysis_date": date(2026, 6, 1),
            **extra,
        }
    )
    return freeze_plan(
        decided,
        realised_population(assignment, intended_column=INTENDED_COLUMN),  # type: ignore[arg-type]
        campaign_id="c_m102",
        registered_by="u_1",
        registered_at=REGISTERED,
    )


# ---------------------------------------------------------------------------
# The statistics, against scipy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("df", [1.0, 2.5, 4.0, 11.3, 30.0, 250.0, 10_000.0, 2_000_000.0])
def test_students_t_matches_scipy(df: float) -> None:
    stats = pytest.importorskip("scipy.stats")
    for statistic in (0.3, 1.0, 1.96, 2.7, 6.0):
        assert t_two_sided(statistic, df) == pytest.approx(2 * stats.t.sf(statistic, df), rel=1e-7, abs=1e-15)
    for probability in (0.6, 0.9, 0.975, 0.995):
        assert t_quantile(probability, df) == pytest.approx(stats.t.ppf(probability, df), rel=1e-8)


def test_welchs_interval_and_p_value_match_scipy() -> None:
    stats = pytest.importorskip("scipy.stats")
    rng = np.random.default_rng(7)
    treated, control = rng.normal(10.0, 3.0, size=40), rng.normal(8.5, 6.0, size=25)
    result = mean_difference(treated, control)
    assert result is not None
    test = stats.ttest_ind(treated, control, equal_var=False)
    assert result.p_value == pytest.approx(test.pvalue, rel=1e-8)
    low, high = test.confidence_interval(0.95)
    assert result.low == pytest.approx(low, rel=1e-8) and result.high == pytest.approx(high, rel=1e-8)
    assert result.difference == pytest.approx(treated.mean() - control.mean())


def test_an_arm_of_fewer_than_two_amounts_has_no_interval() -> None:
    assert mean_difference(np.array([1.0]), np.array([1.0, 2.0])) is None
    assert adjusted_difference(np.array([1.0, 2.0]), np.ones(2), np.array([3.0, 4.0]), np.ones(2)) is None


def test_the_adjusted_difference_is_the_difference_less_theta_times_the_covariate_gap() -> None:
    rng = np.random.default_rng(3)
    x1, x0 = rng.normal(0, 1, 500), rng.normal(0, 1, 400)
    y1, y0 = 2.0 + 0.8 * x1 + rng.normal(0, 1, 500), 0.8 * x0 + rng.normal(0, 1, 400)
    adjusted = adjusted_difference(y1, x1, y0, x0)
    plain = mean_difference(y1, y0)
    assert adjusted is not None and plain is not None
    assert adjusted.difference == pytest.approx(plain.difference - adjusted.theta * (x1.mean() - x0.mean()))
    assert adjusted.theta == pytest.approx(0.8, abs=0.1)
    assert 0.0 < adjusted.variance_reduction < 1.0
    assert adjusted.high - adjusted.low < plain.high - plain.low


def test_the_skew_rule_is_kohavis_355_g_squared() -> None:
    assert SKEW_RULE == 355.0
    rng = np.random.default_rng(1)
    long_tail = np.where(rng.random(2_000) < 0.2, np.exp(rng.normal(6.0, 1.5, 2_000)), 0.0)
    g = skewness(long_tail)
    assert g is not None and len(long_tail) < SKEW_RULE * g * g
    assert skewness(np.ones(10)) is None and skewness(np.array([1.0, 2.0])) is None


# ---------------------------------------------------------------------------
# measure_incrementality(outcome_kind="continuous")
# ---------------------------------------------------------------------------
def test_an_amount_is_reported_as_a_difference_in_means_with_welchs_interval() -> None:
    stats = pytest.importorskip("scipy.stats")
    campaign = revenue_campaign(6_000, 3.0, seed=11)
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    treated = campaign.outcomes[REVENUE_COLUMN][~campaign.scores["control_group"]].to_numpy()
    control = campaign.outcomes[REVENUE_COLUMN][campaign.scores["control_group"]].to_numpy()
    test = stats.ttest_ind(treated, control, equal_var=False)
    assert report.outcome_kind == "continuous" and report.status is IncrementalityStatus.MATURE
    assert report.treated_mean == pytest.approx(treated.mean())
    assert report.control_mean == pytest.approx(control.mean())
    assert report.mean_difference == pytest.approx(treated.mean() - control.mean())
    assert report.mean_difference_ci is not None
    assert (report.mean_difference_ci.ci_low, report.mean_difference_ci.ci_high) == pytest.approx(
        tuple(test.confidence_interval(0.95))
    )
    assert report.p_value == pytest.approx(test.pvalue)
    # rates mean nothing on an amount; the yes/no counts are the customers who spent anything
    assert (
        report.treated_rate is None
        and report.absolute_lift is None
        and report.incremental_conversions is None
    )
    assert report.treated_conversions == int((treated > 0).sum())
    assert report.relative_lift == pytest.approx(report.mean_difference / control.mean())
    assert (
        report.adjusted_lift is None
        and report.adjusted_interval is None
        and report.variance_reduction is None
    )
    assert "per customer" in report.summary and not jargon_in(report.summary)


def test_rho_06_removes_036_of_the_variance_within_three_points() -> None:
    """Plan J M102 acceptance: with rho = 0.6, `variance_reduction` is within +-0.03 of 0.36."""
    campaign = revenue_campaign(40_000, 2.0, seed=12, rho=0.6)
    assignment = build_assignment(campaign.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, covariate_column=COVARIATE_COLUMN)
    report = measure_campaign(
        assignment, campaign.outcomes, intended_column=INTENDED_COLUMN, plan=plan, **campaign.adjusted_kwargs
    )
    assert report.variance_reduction is not None
    assert abs(report.variance_reduction - 0.36) <= 0.03, report.variance_reduction
    assert report.adjusted_interval is not None and report.mean_difference_ci is not None
    assert report.adjusted_lift == report.adjusted_interval.value
    width = report.adjusted_interval.ci_high - report.adjusted_interval.ci_low  # type: ignore[operator]
    plain = report.mean_difference_ci.ci_high - report.mean_difference_ci.ci_low  # type: ignore[operator]
    assert width / plain == pytest.approx(np.sqrt(1 - report.variance_reduction), rel=0.01)
    assert report.covariate_column == COVARIATE_COLUMN and report.rows_covariate_missing == 0
    assert report.test_plan_hash == plan.plan_hash
    assert "removed 3" in report.summary and not jargon_in(report.summary)


def test_an_uncorrelated_covariate_gives_about_the_unadjusted_result() -> None:
    campaign = revenue_campaign(40_000, 2.0, seed=13, rho=0.0)
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
    assert report.variance_reduction is not None and abs(report.variance_reduction) < 0.005
    assert report.adjusted_interval is not None and report.mean_difference_ci is not None
    assert report.mean_difference is not None and report.adjusted_lift is not None
    plain_width = report.mean_difference_ci.ci_high - report.mean_difference_ci.ci_low  # type: ignore[operator]
    adjusted_width = report.adjusted_interval.ci_high - report.adjusted_interval.ci_low  # type: ignore[operator]
    assert abs(report.adjusted_lift - report.mean_difference) < 0.05 * plain_width
    assert adjusted_width == pytest.approx(plain_width, rel=0.005)


def test_a_covariate_measured_on_or_after_the_campaign_is_refused() -> None:
    """No leakage: the adjustment only reads an amount fixed before each customer was contacted."""
    campaign = revenue_campaign(2_000, 2.0, seed=14, rho=0.6)
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
    assert report.adjusted_interval is not None, "dated the day before each treatment date: accepted"
    for late in (campaign.outcomes[TREATMENT_DATE_COLUMN], "2026-06-29"):  # the same day, and after
        leaked = campaign.outcomes.copy()
        leaked.loc[leaked.index[:5], COVARIATE_DATE_COLUMN] = late if isinstance(late, str) else late[:5]
        with pytest.raises(CovariateNotBeforeCampaignError, match="5 customers") as raised:
            measure_incrementality(campaign.scores, leaked, **campaign.adjusted_kwargs)
        assert raised.value.code == COVARIATE_NOT_BEFORE_CAMPAIGN
    undated = {**campaign.adjusted_kwargs, "covariate_date_column": None}
    with pytest.raises(CovariateNotBeforeCampaignError, match="no date column"):
        measure_incrementality(campaign.scores, campaign.outcomes, **undated)


def test_a_customer_with_no_earlier_amount_keeps_their_row() -> None:
    campaign = revenue_campaign(4_000, 2.0, seed=15, rho=0.6)
    outcomes = campaign.outcomes.copy()
    outcomes.loc[outcomes.index[:100], COVARIATE_COLUMN] = None
    outcomes.loc[outcomes.index[100:150], COVARIATE_DATE_COLUMN] = None
    report = measure_incrementality(campaign.scores, outcomes, **campaign.adjusted_kwargs)
    assert report.treated_rows + report.control_rows == 4_000
    assert report.rows_covariate_missing == 150
    assert report.adjusted_interval is not None


def test_a_covariate_that_does_not_vary_gives_a_plain_reason() -> None:
    campaign = revenue_campaign(2_000, 2.0, seed=16)
    outcomes = campaign.outcomes.assign(**{COVARIATE_COLUMN: 5.0})
    report = measure_incrementality(campaign.scores, outcomes, **campaign.adjusted_kwargs)
    assert report.adjusted_interval is None and report.adjustment_note is not None
    assert "same for every" in report.adjustment_note and not jargon_in(report.adjustment_note)
    assert report.mean_difference_ci is not None


def test_a_long_tailed_amount_carries_the_skew_warning_and_a_normal_one_does_not() -> None:
    small = revenue_campaign(2_000, 10.0, seed=17, shape="zero_inflated_lognormal")
    skewed = measure_incrementality(small.scores, small.outcomes, **small.measure_kwargs)
    assert skewed.outcome_warnings == (OUTCOME_SKEWED,)
    assert "too narrow" in skewed.summary and not jargon_in(skewed.summary)
    normal = revenue_campaign(2_000, 2.0, seed=17)
    plain = measure_incrementality(normal.scores, normal.outcomes, **normal.measure_kwargs)
    assert plain.outcome_warnings is None


def test_an_amount_that_is_not_a_number_is_refused_with_a_count() -> None:
    campaign = revenue_campaign(200, 2.0, seed=18)
    outcomes = campaign.outcomes.astype({REVENUE_COLUMN: object})
    outcomes.loc[outcomes.index[:3], REVENUE_COLUMN] = "1,200"
    with pytest.raises(ValueError, match="3 outcome value"):
        measure_incrementality(campaign.scores, outcomes, **campaign.measure_kwargs)
    with pytest.raises(ValueError, match="positive_label"):
        measure_incrementality(
            campaign.scores, campaign.outcomes, positive_label="1", **campaign.measure_kwargs
        )
    with pytest.raises(ValueError, match="outcome_kind"):
        measure_incrementality(
            campaign.scores, campaign.outcomes, **{**campaign.measure_kwargs, "outcome_kind": "count"}
        )


def test_a_yes_no_outcome_ignores_a_covariate() -> None:
    """The adjusted estimate is for amounts; a binary report with a covariate named is unchanged."""
    from engine.measurement.simulate import population

    campaign = population(2_000, 0.1, 0.02, seed=19)
    plain = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    named = measure_incrementality(
        campaign.scores, campaign.outcomes, covariate_column="tenure", **campaign.measure_kwargs
    )
    assert plain.model_dump(exclude={"computed_at"}) == named.model_dump(exclude={"computed_at"})


# ---------------------------------------------------------------------------
# measure_campaign: only a pre-registered covariate is used
# ---------------------------------------------------------------------------
def test_a_covariate_without_a_registered_plan_is_a_change_of_plan() -> None:
    campaign = revenue_campaign(1_000, 2.0, seed=20, rho=0.6)
    with pytest.raises(TestPlanChangedError, match="No test plan is registered") as raised:
        measure_campaign(campaign.scores, campaign.outcomes, **campaign.adjusted_kwargs)
    assert [d.field for d in raised.value.differences] == ["covariate_column"]


def test_a_covariate_or_kind_the_plan_did_not_register_is_refused() -> None:
    campaign = revenue_campaign(1_000, 2.0, seed=21, rho=0.6)
    assignment = build_assignment(campaign.scores, primary_key=KEY_COLUMN)
    unadjusted = _plan(assignment)
    with pytest.raises(TestPlanChangedError) as raised:
        measure_campaign(
            assignment,
            campaign.outcomes,
            intended_column=INTENDED_COLUMN,
            plan=unadjusted,
            **campaign.adjusted_kwargs,
        )
    assert [d.field for d in raised.value.differences] == ["covariate_column"]
    as_binary = {**campaign.measure_kwargs, "outcome_kind": "binary"}
    with pytest.raises(TestPlanChangedError) as kind:
        measure_campaign(
            assignment, campaign.outcomes, intended_column=INTENDED_COLUMN, plan=unadjusted, **as_binary
        )
    assert [d.field for d in kind.value.differences] == ["outcome_kind"]
    measured = measure_campaign(
        assignment,
        campaign.outcomes,
        intended_column=INTENDED_COLUMN,
        plan=unadjusted,
        **campaign.measure_kwargs,
    )
    assert measured.adjusted_interval is None and measured.mean_difference_ci is not None


def test_several_offers_on_an_amount_are_refused_in_plain_words() -> None:
    offers = multi_arm_campaign(600, 0.05, (0.02, 0.04), seed=22)
    with pytest.raises(ValueError, match="yes/no outcome only"):
        measure_campaign(
            offers.scores, offers.outcomes, **{**offers.measure_kwargs, "outcome_kind": "continuous"}
        )


def test_the_verdict_of_an_amount_is_the_total_it_changed() -> None:
    campaign = revenue_campaign(40_000, 4.0, seed=23, rho=0.6)
    assignment = build_assignment(campaign.scores, primary_key=KEY_COLUMN)
    report = measure_campaign(
        assignment,
        campaign.outcomes,
        intended_column=INTENDED_COLUMN,
        plan=_plan(assignment, covariate_column=COVARIATE_COLUMN),
        **campaign.adjusted_kwargs,
    )
    verdict = campaign_verdict_for(report, outcome_is_good=True)
    assert verdict is not None and verdict.kind is VerdictKind.ADDED
    assert report.adjusted_lift is not None
    assert verdict.amount == round(
        report.adjusted_lift * report.treated_rows
    ), "the adjusted, pre-registered one"
    assert "revenue" in verdict.headline and not jargon_in(verdict.headline + verdict.detail)
    null = revenue_campaign(2_000, 0.0, seed=24)
    flat = measure_incrementality(null.scores, null.outcomes, **null.measure_kwargs)
    assert amount_verdict(flat, outcome_is_good=True).kind in {VerdictKind.NO_CLEAR_EFFECT, VerdictKind.ADDED}


def test_the_new_codes_are_two_and_plain() -> None:
    assert {COVARIATE_NOT_BEFORE_CAMPAIGN, OUTCOME_SKEWED} == CONTINUOUS_CODES


def test_an_early_look_at_an_amount_gives_the_averages_and_no_verdict() -> None:
    campaign = revenue_campaign(1_000, 2.0, seed=25)
    assignment = build_assignment(campaign.scores, primary_key=KEY_COLUMN)
    plan = _plan(assignment, analysis_date=date(2026, 7, 31))
    report = measure_campaign(
        assignment, campaign.outcomes, intended_column=INTENDED_COLUMN, plan=plan, **campaign.measure_kwargs
    )
    assert report.early_look and campaign_verdict_for(report, outcome_is_good=True) is None
    assert "averaged" in report.summary and "per customer" not in report.summary
