"""Plan J M102 (DEC-1312): the planner sees amounts, and an expected rho² shrinks the test it needs."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from engine.measurement.plan import PLAN_UNDERPOWERED, RealisedPopulation, TestPlanInput, freeze_plan
from engine.measurement.planner import (
    REASON_EMPTY_GROUP,
    REASON_NO_EFFECT,
    REASON_SPREAD_UNKNOWN,
    achieved_power_continuous,
    mde_continuous,
    n_for_mde_continuous,
)
from engine.pilot.plain import jargon_in

Z = 1.959963984540054 + 0.8416212335729143  # z(0.975) + z(0.80)


def test_the_smallest_change_is_z_times_the_spread_of_the_difference() -> None:
    mde = mde_continuous(1_000, 1_000, 40.0)
    assert mde.absolute == pytest.approx(Z * 40.0 * math.sqrt(2 / 1_000))
    assert mde.reason is None and mde.rho2 == 0.0


def test_an_expected_rho2_shrinks_the_change_by_sqrt_of_one_minus_it() -> None:
    plain = mde_continuous(5_000, 500, 40.0).absolute
    adjusted = mde_continuous(5_000, 500, 40.0, rho2=0.36).absolute
    assert plain is not None and adjusted is not None
    assert adjusted / plain == pytest.approx(0.8)


def test_customers_needed_fall_by_rho2() -> None:
    plain = n_for_mde_continuous(1.0, 0.1)
    adjusted = n_for_mde_continuous(1.0, 0.1, rho2=0.36)
    assert plain.n_treat == math.ceil(Z**2 * 2 / 0.01 - 1e-9) == 1_570
    assert adjusted.n_treat == 1_005 and adjusted.n_control == 1_005
    unequal = n_for_mde_continuous(1.0, 0.1, control_ratio=0.25)
    assert unequal.n_treat == math.ceil(Z**2 * (1 + 4) / 0.01 - 1e-9)
    assert unequal.n_control == math.ceil(0.25 * unequal.n_treat - 1e-9)


def test_the_planners_own_n_has_its_planned_power() -> None:
    sizes = n_for_mde_continuous(40.0, 3.0, rho2=0.36)
    assert sizes.n_treat is not None and sizes.n_control is not None
    power = achieved_power_continuous(sizes.n_treat, sizes.n_control, 40.0, 3.0, rho2=0.36).power
    assert power == pytest.approx(0.80, abs=0.002)


def test_what_cannot_be_said_has_a_plain_reason() -> None:
    assert mde_continuous(100, 100, None).reason == REASON_SPREAD_UNKNOWN
    assert mde_continuous(100, 0, 3.0).reason == REASON_EMPTY_GROUP
    assert achieved_power_continuous(100, 100, 3.0, 0.0).reason == REASON_NO_EFFECT
    assert n_for_mde_continuous(None, 1.0).reason == REASON_SPREAD_UNKNOWN
    assert not jargon_in(REASON_SPREAD_UNKNOWN)
    with pytest.raises(ValueError, match="rho2"):
        mde_continuous(100, 100, 3.0, rho2=1.0)
    with pytest.raises(ValueError, match="above 0"):
        mde_continuous(100, 100, -1.0)


# ---------------------------------------------------------------------------
# The registered test plan of an amount
# ---------------------------------------------------------------------------
REALISED = RealisedPopulation(population_rows=2_000, n_treat=1_000, n_holdout=1_000)


def _decided(**extra: object) -> TestPlanInput:
    return TestPlanInput.model_validate(
        {
            "metric": "revenue in 30 days",
            "outcome_column": "revenue",
            "outcome_kind": "continuous",
            "outcome_window_days": 30,
            "analysis_date": date(2026, 6, 1),
            **extra,
        }
    )


def _freeze(decided: TestPlanInput) -> object:
    return freeze_plan(
        decided,
        REALISED,
        campaign_id="c_1",
        registered_by="u_1",
        registered_at=datetime(2026, 4, 1, tzinfo=UTC),
    )


def test_an_amounts_plan_is_powered_with_its_spread_and_expected_rho2() -> None:
    plain = _freeze(_decided(mde_value=5.0, outcome_sd=40.0))
    adjusted = _freeze(
        _decided(mde_value=5.0, outcome_sd=40.0, covariate_column="pre_revenue", expected_rho2=0.36)
    )
    expected = achieved_power_continuous(1_000, 1_000, 40.0, 5.0).power
    assert plain.achieved_power == pytest.approx(expected, abs=1e-6)  # type: ignore[attr-defined]
    assert plain.warnings == (PLAN_UNDERPOWERED,)  # type: ignore[attr-defined]
    assert adjusted.achieved_power > plain.achieved_power  # type: ignore[attr-defined]
    assert adjusted.warnings == ()  # type: ignore[attr-defined]
    unknown = _freeze(_decided())
    assert unknown.achieved_power is None and not jargon_in(unknown.power_note)  # type: ignore[attr-defined]


def test_a_plan_mixes_neither_kind_of_planning() -> None:
    with pytest.raises(ValidationError, match="mde_value and outcome_sd"):
        _decided(mde_pp=2.0)
    with pytest.raises(ValidationError, match="yes/no outcome is planned"):
        _decided(outcome_kind="binary", mde_value=2.0)
    with pytest.raises(ValidationError, match="name the covariate_column"):
        _decided(expected_rho2=0.3)


def test_a_plan_without_the_new_fields_hashes_and_stores_as_before() -> None:
    """The binary golden plan (recorded before M102) keeps its hash: `test_m102_binary_identity`."""
    plan = _freeze(_decided())
    stored = plan.model_dump(mode="json")  # type: ignore[attr-defined]
    assert not {"mde_value", "outcome_sd", "expected_rho2"} & set(stored)
