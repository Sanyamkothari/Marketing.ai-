"""The simulator behind the validity harness makes what the measurement reads, deterministically (Plan J M95).

These are fast checks of `engine.measurement.simulate`. The statistical claims about the measurement
itself (coverage, false positives, bias) live in `tests/statistical/` and run nightly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

import pandas as pd
import pytest

from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population
from engine.uplift.contracts import IncrementalityStatus
from engine.uplift.incrementality import measure_incrementality


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    logger = logging.getLogger("engine")
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(level)


def test_a_seed_is_one_campaign_bit_for_bit() -> None:
    first = population(500, 0.05, 0.02, immature_share=0.2, seed=7)
    again = population(500, 0.05, 0.02, immature_share=0.2, seed=7)
    other = population(500, 0.05, 0.02, immature_share=0.2, seed=8)
    pd.testing.assert_frame_equal(first.scores, again.scores)
    pd.testing.assert_frame_equal(first.outcomes, again.outcomes)
    assert not first.outcomes.equals(other.outcomes)


def test_the_frames_are_what_measure_incrementality_consumes() -> None:
    campaign = population(2_000, 0.10, 0.03, seed=1)
    assert list(campaign.scores.columns) == [
        "customer_id",
        "control_group",
        "suppressed_reason",
        "intended_treatment",
    ]
    assert list(campaign.outcomes.columns) == ["customer_id", "converted", "treatment_date"]
    assert campaign.scores["control_group"].dtype == bool
    assert campaign.scores["customer_id"].is_unique

    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.status is IncrementalityStatus.MATURE
    assert report.control_rows == 1_000 and report.treated_rows == 1_000  # half are held back
    assert report.rows_immature == 0 and report.rows_without_outcome == 0
    assert report.causal and report.absolute_lift is not None


def test_the_control_share_is_exact_and_the_two_arms_are_disjoint() -> None:
    campaign = population(1_000, 0.05, 0.0, seed=3, control_share=0.1)
    assert int(campaign.scores["control_group"].sum()) == 100


def test_immature_customers_are_the_rows_the_maturity_rule_leaves_out() -> None:
    campaign = population(4_000, 0.05, 0.02, immature_share=0.25, seed=2)
    assert 0.22 < campaign.immature.mean() < 0.28
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.rows_immature == int(campaign.immature.sum())
    assert report.treated_rows + report.control_rows == 4_000 - report.rows_immature
    # an immature customer's treatment date is inside the outcome window, a mature one's beyond it
    dates = pd.to_datetime(campaign.outcomes["treatment_date"], utc=True)
    age = (pd.Timestamp(AS_OF) - dates).dt.days.to_numpy()
    assert (age[campaign.immature] < OUTCOME_WINDOW_DAYS).all()
    assert (age[~campaign.immature] >= OUTCOME_WINDOW_DAYS).all()


def test_an_immature_row_under_reports_conversions_as_a_real_file_does() -> None:
    campaign = population(20_000, 0.30, 0.0, immature_share=0.5, seed=4)
    converted = campaign.outcomes["converted"].to_numpy()
    mature_rate = converted[~campaign.immature].mean()
    immature_rate = converted[campaign.immature].mean()
    assert mature_rate == pytest.approx(0.30, abs=0.015)
    assert immature_rate == pytest.approx(
        0.30 * 0.5, abs=0.015
    )  # about half the window has passed on average


def test_non_compliance_and_contamination_move_the_itt_not_the_effect_on_the_treated() -> None:
    campaign = population(200_000, 0.05, 0.10, compliance=0.6, contamination=0.1, seed=5)
    assert campaign.true_itt == pytest.approx(0.05)
    report = measure_incrementality(campaign.scores, campaign.outcomes, **campaign.measure_kwargs)
    assert report.absolute_lift is not None
    # a point check, not a coverage claim (that is the nightly suite's): the estimate is the ITT, not 0.10
    assert report.absolute_lift.value == pytest.approx(campaign.true_itt, abs=0.005)
    received_treated = campaign.received_treatment[~campaign.scores["control_group"].to_numpy()].mean()
    received_control = campaign.received_treatment[campaign.scores["control_group"].to_numpy()].mean()
    assert received_treated == pytest.approx(0.6, abs=0.01) and received_control == pytest.approx(
        0.1, abs=0.01
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n": 1, "base_rate": 0.1, "effect": 0.0},
        {"n": 100, "base_rate": 1.2, "effect": 0.0},
        {"n": 100, "base_rate": 0.9, "effect": 0.2},  # base + effect above 1
        {"n": 100, "base_rate": 0.05, "effect": -0.1},  # base + effect below 0
        {"n": 100, "base_rate": 0.1, "effect": 0.0, "compliance": 1.5},
        {"n": 100, "base_rate": 0.1, "effect": 0.0, "contamination": -0.1},
        {"n": 100, "base_rate": 0.1, "effect": 0.0, "immature_share": 1.0},
        {"n": 100, "base_rate": 0.1, "effect": 0.0, "control_share": 0.0},
        {"n": 2, "base_rate": 0.1, "effect": 0.0, "control_share": 0.9},
    ],
)
def test_a_value_out_of_range_is_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"must be|empty"):
        population(seed=1, **kwargs)  # type: ignore[arg-type]
