"""Plan J M104 (DEC-1314): each group's measured effect, and the guard against false backfire alarms.

`engine.measurement.segments` reads a campaign once per band, predicted segment and offer, with the campaign's
own measurement (`measure_incrementality`), and gives every group with enough customers a second interval at the
Bonferroni level of the family of groups judged. Campaigns come from the engine's simulator
(`segmented_campaign`, `multi_arm_campaign`), never from hand-written rows.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.measurement.measure import measure_campaign, measure_campaign_segments
from engine.measurement.segments import FAMILY_ALPHA, MIN_ROWS_PER_ARM, SegmentEffects
from engine.measurement.simulate import multi_arm_campaign, revenue_campaign, segmented_campaign
from engine.uplift.incrementality import measure_incrementality


def _segments(campaign: Any, **extra: Any) -> tuple[Any, SegmentEffects]:
    kw = campaign.measure_kwargs
    report = measure_campaign(
        campaign.scores, campaign.outcomes, intended_column="intended_treatment", **kw, **extra
    )
    effects = measure_campaign_segments(
        campaign.scores,
        campaign.outcomes,
        report,
        primary_key=kw["primary_key"],
        outcome_column=kw["outcome_column"],
        treatment_time=kw["treatment_time"],
        treatment_date_column=kw["treatment_date_column"],
        intended_column="intended_treatment",
    )
    return report, effects


def _by_name(effects: SegmentEffects) -> dict[str, Any]:
    return {cell.segment: cell for cell in effects.cells}


def test_a_planted_harmful_group_has_its_whole_family_range_below_zero_and_a_neutral_one_does_not() -> None:
    campaign = segmented_campaign(24_000, 0.15, {"helped": 0.05, "neutral": 0.0, "harmed": -0.08}, seed=7)
    _, effects = _segments(campaign)
    cells = _by_name(effects)
    assert effects.family_size == 3
    harmed, neutral, helped = cells["harmed"], cells["neutral"], cells["helped"]
    assert harmed.family_interval is not None and harmed.family_interval.ci_high is not None
    assert harmed.family_interval.ci_high < 0, "the planted harm is flagged"
    assert neutral.family_interval is not None and neutral.family_interval.ci_low is not None
    assert (
        neutral.family_interval.ci_low < 0 < (neutral.family_interval.ci_high or 0)
    ), "the neutral one is not"
    assert helped.effect is not None and (helped.effect.ci_low or 0) > 0
    for cell, truth in ((harmed, -0.08), (neutral, 0.0), (helped, 0.05)):
        assert cell.effect is not None and cell.effect.ci_low is not None and cell.effect.ci_high is not None
        assert cell.effect.ci_low <= truth <= cell.effect.ci_high, cell.segment


def test_many_neutral_groups_raise_no_alarm_where_reading_each_at_95_percent_would() -> None:
    """Twelve groups the campaign did not touch. At 95% each, two read as harmed by chance on this seed (found by
    search and fixed, so the test is deterministic); at the family's level none does."""
    groups = {f"G{index:02d}": 0.0 for index in range(12)}
    campaign = segmented_campaign(24_000, 0.10, groups, seed=3)
    _, effects = _segments(campaign)
    naive = sorted(
        c.segment for c in effects.cells if c.effect and c.effect.ci_high is not None and c.effect.ci_high < 0
    )
    assert naive == ["G08", "G11"], "reading every group at 95% would raise two false alarms here"
    flagged = [
        c.segment
        for c in effects.cells
        if c.family_interval and c.family_interval.ci_high is not None and c.family_interval.ci_high < 0
    ]
    assert flagged == []
    assert effects.family_size == 12
    assert effects.family_confidence == pytest.approx(1.0 - FAMILY_ALPHA / 12)


def test_each_groups_numbers_are_the_campaigns_measurement_on_that_groups_rows() -> None:
    campaign = segmented_campaign(9_000, 0.2, {"A": 0.04, "B": -0.02}, seed=11)
    _, effects = _segments(campaign)
    kw = campaign.measure_kwargs
    for cell in effects.cells:
        rows = campaign.scores[campaign.scores["band"] == cell.segment]
        direct = measure_incrementality(rows, campaign.outcomes, intended_column="intended_treatment", **kw)
        assert cell.treated_rows == direct.treated_rows and cell.control_rows == direct.control_rows
        assert cell.treated_conversions == direct.treated_conversions
        assert cell.effect == direct.absolute_lift
        assert cell.comparison == "within_group"


def test_a_small_group_is_not_judged_and_does_not_widen_the_family() -> None:
    campaign = segmented_campaign(6_000, 0.2, {"big": 0.0, "other": 0.0}, seed=5)
    scores = campaign.scores.copy()
    small = scores.index[:200]  # about 40 held back, fewer than the rule's minimum
    scores.loc[small, "band"] = "small"
    kw = campaign.measure_kwargs
    report = measure_campaign(scores, campaign.outcomes, intended_column="intended_treatment", **kw)
    effects = measure_campaign_segments(
        scores,
        campaign.outcomes,
        report,
        primary_key=kw["primary_key"],
        outcome_column=kw["outcome_column"],
        treatment_time=kw["treatment_time"],
        intended_column="intended_treatment",
    )
    cells = _by_name(effects)
    assert cells["small"].control_rows < MIN_ROWS_PER_ARM
    assert not cells["small"].judged and cells["small"].family_interval is None
    assert effects.family_size == 2
    assert effects.min_rows_per_arm == MIN_ROWS_PER_ARM


def test_offers_against_a_shared_control_repeat_the_reports_own_arms() -> None:
    sim = multi_arm_campaign(9_000, 0.10, (0.03, 0.06), seed=21)
    kw = sim.measure_kwargs
    report = measure_campaign(sim.scores, sim.outcomes, **kw)
    offers = sim.scores["offer"].where(~sim.scores["control_group"].astype(bool))
    effects = measure_campaign_segments(
        sim.scores,
        sim.outcomes,
        report,
        primary_key=kw["primary_key"],
        outcome_column=kw["outcome_column"],
        treatment_time=kw["treatment_time"],
        treatment_date_column=kw.get("treatment_date_column"),
        offers=offers,
        control_level=sim.levels[0],
    )
    cells = {cell.segment: cell for cell in effects.cells if cell.dimension == "offer"}
    assert report.arms is not None
    for arm in report.arms:
        assert cells[arm.arm].comparison == "shared_control"
        assert cells[arm.arm].effect == arm.effect
        assert cells[arm.arm].control_rows == arm.control_rows


def test_an_amounts_family_range_is_wider_than_its_95_percent_range() -> None:
    sim = revenue_campaign(8_000, 5.0, seed=4, control_share=0.3)
    scores = sim.scores.assign(band=np.where(np.arange(len(sim.scores)) % 2 == 0, "even", "odd"))
    kw = sim.measure_kwargs
    report = measure_campaign(scores, sim.outcomes, **kw)
    effects = measure_campaign_segments(
        scores,
        sim.outcomes,
        report,
        primary_key=kw["primary_key"],
        outcome_column=kw["outcome_column"],
        treatment_time=kw["treatment_time"],
        treatment_date_column=kw["treatment_date_column"],
    )
    assert effects.outcome_kind == "continuous" and effects.family_size == 2
    for cell in effects.cells:
        assert cell.effect is not None and cell.family_interval is not None
        assert cell.treated_mean is not None and cell.treated_rate is None
        assert (cell.family_interval.ci_low or 0) < (cell.effect.ci_low or 0)
        assert (cell.family_interval.ci_high or 0) > (cell.effect.ci_high or 0)


def test_an_uplift_runs_band_that_repeats_its_segment_is_read_once() -> None:
    campaign = segmented_campaign(4_000, 0.2, {"persuadable": 0.05, "sleeping_dog": -0.05}, seed=9)
    labels = {"persuadable": "Persuadables", "sleeping_dog": "Sleeping dogs"}
    scores = campaign.scores.assign(segment=campaign.scores["band"], band=campaign.scores["band"].map(labels))
    kw = campaign.measure_kwargs
    report = measure_campaign(scores, campaign.outcomes, intended_column="intended_treatment", **kw)
    effects = measure_campaign_segments(
        scores,
        campaign.outcomes,
        report,
        primary_key=kw["primary_key"],
        outcome_column=kw["outcome_column"],
        treatment_time=kw["treatment_time"],
        intended_column="intended_treatment",
    )
    assert {cell.dimension for cell in effects.cells} == {"segment"}
    assert [note.dimension for note in effects.not_measured] == ["band"]


def test_the_file_holds_no_customer_id() -> None:
    campaign = segmented_campaign(3_000, 0.2, {"A": 0.0, "B": 0.0}, seed=2)
    _, effects = _segments(campaign)
    text = effects.model_dump_json()
    assert not [key for key in campaign.scores["customer_id"] if key in text]


def _timed(n: int) -> float:
    campaign = segmented_campaign(n, 0.15, {"A": 0.02, "B": 0.0, "C": -0.02}, seed=1)
    kw = campaign.measure_kwargs
    report = measure_campaign(campaign.scores, campaign.outcomes, intended_column="intended_treatment", **kw)
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        measure_campaign_segments(
            campaign.scores,
            campaign.outcomes,
            report,
            primary_key=kw["primary_key"],
            outcome_column=kw["outcome_column"],
            treatment_time=kw["treatment_time"],
            treatment_date_column=kw["treatment_date_column"],
            intended_column="intended_treatment",
        )
        best = min(best, time.perf_counter() - start)
    return best


GROWTH_LIMIT = 30.0
"""Ten times the rows may take at most this many times as long: linear work gives about 10x, quadratic about
100x; the margin leaves room for a busy machine (best of three runs, as the arbitration timing test does)."""


def test_measuring_the_groups_grows_linearly_with_the_campaign() -> None:
    small, large = _timed(20_000), _timed(200_000)
    assert large / small < GROWTH_LIMIT, f"{small:.3f}s for 20k rows, {large:.3f}s for 200k"


def test_an_assignment_that_repeats_a_customer_is_refused() -> None:
    campaign = segmented_campaign(1_000, 0.2, {"A": 0.0}, seed=2)
    kw = campaign.measure_kwargs
    report = measure_campaign(campaign.scores, campaign.outcomes, intended_column="intended_treatment", **kw)
    doubled = pd.concat([campaign.scores, campaign.scores.iloc[:1]], ignore_index=True)
    with pytest.raises(ValueError, match="repeats a customer"):
        measure_campaign_segments(
            doubled,
            campaign.outcomes,
            report,
            primary_key=kw["primary_key"],
            outcome_column=kw["outcome_column"],
            treatment_time=kw["treatment_time"],
        )
