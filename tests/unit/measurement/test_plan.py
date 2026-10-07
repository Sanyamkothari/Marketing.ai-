"""`engine.measurement.plan`: a frozen, hashed test plan, its power warning and its comparisons."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from engine.measurement.plan import (
    ERASURE_ALLOWANCE_SHARE,
    HOLDOUT_TOLERANCE,
    PLAN_UNDERPOWERED,
    RealisedPopulation,
    TestPlan,
    TestPlanAmendment,
    TestPlanInput,
    freeze_plan,
    is_early_look,
    plan_differences,
    plan_hash,
    plan_inputs,
    population_kept,
)
from engine.uplift.power import power_of_lift

POPULATION = RealisedPopulation(population_rows=12_000, n_treat=10_800, n_holdout=1_200)
WHEN = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def decided(**extra: object) -> TestPlanInput:
    return TestPlanInput.model_validate(
        {
            "metric": "reactivated within 90 days",
            "outcome_column": "reactivated_90d",
            "outcome_window_days": 90,
            "analysis_date": "2026-08-15",
            **extra,
        }
    )


def frozen(**extra: object):  # type: ignore[no-untyped-def]
    return freeze_plan(
        decided(**extra), POPULATION, campaign_id="c_1", registered_by="u_1", registered_at=WHEN
    )


def test_the_hash_is_the_content_and_not_who_registered_it_when() -> None:
    plan = frozen()
    again = freeze_plan(
        decided(),
        POPULATION,
        campaign_id="c_1",
        registered_by="u_2",
        registered_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert plan.plan_hash == again.plan_hash and len(plan.plan_hash) == 64
    assert plan.plan_hash == plan_hash(plan.model_dump(mode="json"))
    for change in (
        {"mde_pp": 2.0},
        {"analysis_date": "2026-08-16"},
        {"covariate_column": "tenure"},
        {"secondary_analysis_dates": ["2026-09-15"]},
        {"expectation": "1 to 3 points"},
    ):
        assert frozen(**change).plan_hash != plan.plan_hash, change
    other_campaign = freeze_plan(
        decided(), POPULATION, campaign_id="c_2", registered_by="u_1", registered_at=WHEN
    )
    assert other_campaign.plan_hash != plan.plan_hash
    amended = freeze_plan(
        decided(),
        POPULATION,
        campaign_id="c_1",
        registered_by="u_1",
        registered_at=WHEN,
        version=2,
        amends="x" * 64,
    )
    assert amended.plan_hash != plan.plan_hash


def test_the_plan_records_the_population_it_was_made_for() -> None:
    plan = frozen()
    assert (plan.population_rows, plan.n_treat, plan.n_holdout, plan.holdout_fraction) == (
        12_000,
        10_800,
        1_200,
        0.1,
    )
    assert plan_inputs(plan) == decided()


def test_power_is_computed_from_the_planned_arms_and_a_shortfall_is_a_warning() -> None:
    plan = frozen(mde_pp=3.0, base_rate=0.10)
    expected = power_of_lift(10_800, 1_200, 0.10, 0.03)
    assert plan.achieved_power == pytest.approx(expected, abs=1e-6) and expected > 0.8
    assert plan.warnings == () and plan.power_note is None
    weak = frozen(mde_pp=0.5, base_rate=0.10)
    assert weak.achieved_power is not None and weak.achieved_power < 0.8
    assert weak.warnings == (PLAN_UNDERPOWERED,)


def test_without_an_effect_and_a_rate_the_power_is_not_invented() -> None:
    plan = frozen()
    assert plan.achieved_power is None and plan.power_note is not None and plan.warnings == ()
    beyond = frozen(mde_pp=50.0, base_rate=0.9)
    assert beyond.achieved_power is None and "above 100%" in (beyond.power_note or "")


def test_the_inputs_are_checked() -> None:
    with pytest.raises(ValidationError, match="after the analysis date"):
        decided(secondary_analysis_dates=["2026-08-01"])
    with pytest.raises(ValidationError, match="repeated"):
        decided(secondary_analysis_dates=["2026-09-01", "2026-09-01"])
    with pytest.raises(ValidationError):
        decided(mde_pp=0)
    with pytest.raises(ValidationError):
        TestPlanAmendment.model_validate({**decided().model_dump(mode="json"), "reason": ""})


def test_differences_name_each_field_that_moved() -> None:
    plan = frozen(positive_label="Yes")
    same = plan_differences(
        plan,
        realised=POPULATION,
        outcome_column="reactivated_90d",
        positive_label=" yes ",
        outcome_window_days=90,
        covariate_column=None,
    )
    assert same == (), "a label is compared as the measurement reads it"
    moved = plan_differences(
        plan,
        realised=RealisedPopulation(population_rows=12_000, n_treat=11_400, n_holdout=600),
        outcome_column="reactivated_90d",
        positive_label="yes",
        outcome_window_days=60,
        covariate_column="tenure",
    )
    assert [d.field for d in moved] == ["holdout_fraction", "outcome_window_days", "covariate_column"]
    assert moved[0].planned == "0.1" and moved[0].realised == "0.05"


def test_an_early_look_is_any_moment_before_the_analysis_date_in_utc() -> None:
    plan = frozen()
    assert is_early_look(plan, datetime(2026, 8, 14, 23, 59, tzinfo=UTC))
    assert not is_early_look(plan, datetime(2026, 8, 15, 0, 0, tzinfo=UTC))
    assert not is_early_look(plan, datetime(2026, 8, 15, 0, 0))  # naive is read as UTC
    assert plan.analysis_date == date(2026, 8, 15)


def _population_fields(plan: TestPlan, realised: RealisedPopulation) -> list[str]:
    differences = plan_differences(
        plan,
        realised=realised,
        outcome_column="reactivated_90d",
        positive_label=None,
        outcome_window_days=90,
        covariate_column=None,
    )
    return [d.field for d in differences]


@pytest.mark.parametrize(
    ("n_treat", "n_holdout"),
    [(10_799, 1_200), (10_800, 1_199), (10_740, 1_140)],  # one erased, one erased, 120 (1 %) erased
)
def test_customers_erased_since_the_plan_are_not_a_change(n_treat: int, n_holdout: int) -> None:
    """Erasure (DEC-741) removes rows from the assignment; that is not anybody moving the goalposts."""
    plan = frozen()
    realised = RealisedPopulation(population_rows=n_treat + n_holdout, n_treat=n_treat, n_holdout=n_holdout)
    assert population_kept(plan, realised)
    assert _population_fields(plan, realised) == []


def test_a_small_campaign_may_lose_one_customer() -> None:
    small = freeze_plan(
        decided(),
        RealisedPopulation(population_rows=41, n_treat=30, n_holdout=11),
        campaign_id="c_1",
        registered_by="u_1",
        registered_at=WHEN,
    )
    one_gone = RealisedPopulation(population_rows=40, n_treat=30, n_holdout=10)
    assert _population_fields(small, one_gone) == [], "the holdout share moved 1.8 points, by erasure"
    two_gone = RealisedPopulation(population_rows=39, n_treat=30, n_holdout=9)
    assert _population_fields(small, two_gone) == ["holdout_fraction", "population_rows"]


@pytest.mark.parametrize(
    ("realised", "fields"),
    [
        # more than 1 % gone: a narrower population, not an erasure
        (RealisedPopulation(population_rows=11_879, n_treat=10_680, n_holdout=1_199), ["population_rows"]),
        # a larger arm cannot come from erasure, even with the same head count
        (RealisedPopulation(population_rows=12_000, n_treat=11_400, n_holdout=600), ["holdout_fraction"]),
        # a customer added
        (RealisedPopulation(population_rows=12_001, n_treat=10_801, n_holdout=1_200), ["population_rows"]),
    ],
)
def test_a_different_population_is_still_a_change(realised: RealisedPopulation, fields: list[str]) -> None:
    plan = frozen()
    assert not population_kept(plan, realised)
    assert _population_fields(plan, realised) == fields
    assert HOLDOUT_TOLERANCE == 0.005 and ERASURE_ALLOWANCE_SHARE == 0.01
