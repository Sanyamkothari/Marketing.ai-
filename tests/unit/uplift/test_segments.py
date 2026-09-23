"""`engine.uplift.segments`: the cuts as applied, the four segments and `segments.json`.

Boundaries and shares are checked on tiny arrays whose answer is written out by hand; the planted
generator is used once, with its TRUE probabilities as the "prediction", to show the rules put every
planted customer back in its planted segment.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from engine.uplift.config import UpliftSegmentsConfig
from engine.uplift.contracts import SEGMENT_ACTIONS, SEGMENT_LABELS, Segment, SegmentThresholds
from engine.uplift.segments import SEGMENT_ORDER, assign_segments, resolve_thresholds, segment_report
from tests.fixtures.make_uplift_data import make_uplift_data

RUN_ID = "r_20260921_aaaaaaaa"

THRESHOLDS = SegmentThresholds(
    persuadable_min_uplift=0.02,
    sleeping_dog_max_uplift=-0.01,
    sure_thing_min_probability=0.40,
    sure_thing_from_base_rate=False,
)


# ---------------------------------------------------------------------------
# resolve_thresholds
# ---------------------------------------------------------------------------
def test_a_configured_sure_thing_cut_is_used_as_is() -> None:
    config = UpliftSegmentsConfig(
        persuadable_min_uplift=0.05, sleeping_dog_max_uplift=-0.02, sure_thing_min_probability=0.3
    )
    thresholds = resolve_thresholds(config, base_rate=0.12)
    assert thresholds == SegmentThresholds(
        persuadable_min_uplift=0.05,
        sleeping_dog_max_uplift=-0.02,
        sure_thing_min_probability=0.3,
        sure_thing_from_base_rate=False,
    )


def test_an_unset_sure_thing_cut_defaults_to_the_training_base_rate_and_says_so() -> None:
    thresholds = resolve_thresholds(UpliftSegmentsConfig(), base_rate=0.12)
    assert thresholds.sure_thing_min_probability == 0.12
    assert thresholds.sure_thing_from_base_rate is True
    assert thresholds.persuadable_min_uplift == 0.02
    assert thresholds.sleeping_dog_max_uplift == -0.01


@pytest.mark.parametrize("base_rate", [0.0, 1.0, -0.1, 1.5, math.nan, math.inf])
def test_a_base_rate_that_cannot_be_a_cut_is_refused(base_rate: float) -> None:
    with pytest.raises(ValueError, match="base rate"):
        resolve_thresholds(UpliftSegmentsConfig(), base_rate=base_rate)


def test_a_configured_cut_does_not_need_a_usable_base_rate() -> None:
    config = UpliftSegmentsConfig(sure_thing_min_probability=0.5)
    assert resolve_thresholds(config, base_rate=math.nan).sure_thing_min_probability == 0.5


# ---------------------------------------------------------------------------
# assign_segments
# ---------------------------------------------------------------------------
def test_the_rules_and_their_inclusive_boundaries() -> None:
    uplift = np.array([0.02, 0.019, -0.01, -0.009, 0.0, 0.0, 0.30, -0.30])
    p_control = np.array([0.9, 0.40, 0.9, 0.39, 0.40, 0.10, 0.9, 0.01])
    segments = assign_segments(uplift, p_control, THRESHOLDS)
    assert segments.dtype == object
    assert segments.tolist() == [
        Segment.PERSUADABLE,  # exactly on the persuadable cut
        Segment.SURE_THING,  # just below it, p_control exactly on the sure-thing cut
        Segment.SLEEPING_DOG,  # exactly on the sleeping-dog cut, whatever p_control
        Segment.LOST_CAUSE,  # just above it, p_control just below the sure-thing cut
        Segment.SURE_THING,
        Segment.LOST_CAUSE,
        Segment.PERSUADABLE,  # a high p_control does not stop a persuadable being one
        Segment.SLEEPING_DOG,
    ]
    assert all(isinstance(item, Segment) for item in segments)


def test_segment_values_compare_equal_to_their_text() -> None:
    segments = assign_segments(np.array([0.5]), np.array([0.1]), THRESHOLDS)
    assert segments[0] == "persuadable"


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_missing_prediction_has_no_segment(bad: float) -> None:
    with pytest.raises(ValueError, match="1 of 2 rows"):
        assign_segments(np.array([0.1, bad]), np.array([0.1, 0.1]), THRESHOLDS)
    with pytest.raises(ValueError, match="p_control"):
        assign_segments(np.array([0.1, 0.1]), np.array([0.1, bad]), THRESHOLDS)


def test_misaligned_inputs_are_refused() -> None:
    with pytest.raises(ValueError, match="same rows"):
        assign_segments(np.array([0.1, 0.2]), np.array([0.1]), THRESHOLDS)


def test_the_planted_segments_are_recovered_from_the_true_probabilities() -> None:
    truth = make_uplift_data(4_000, seed=3).truth
    # The planted sure things have a true uplift of +0.02, so the persuadable cut sits above it.
    thresholds = SegmentThresholds(
        persuadable_min_uplift=0.05,
        sleeping_dog_max_uplift=-0.05,
        sure_thing_min_probability=0.5,
        sure_thing_from_base_rate=False,
    )
    segments = assign_segments(truth["true_uplift"].to_numpy(), truth["p_control"].to_numpy(), thresholds)
    assert [str(item) for item in segments] == truth["true_segment"].tolist()


# ---------------------------------------------------------------------------
# segment_report
# ---------------------------------------------------------------------------
def _report() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    uplift = np.array([0.30, 0.10, 0.0, 0.0, 0.005])
    p_treated = np.array([0.40, 0.20, 0.70, 0.05, 0.035])
    p_control = np.array([0.10, 0.10, 0.70, 0.05, 0.03])
    return uplift, p_treated, p_control


def test_the_report_counts_shares_and_averages_every_segment_in_order() -> None:
    uplift, p_treated, p_control = _report()
    segments = assign_segments(uplift, p_control, THRESHOLDS)
    report = segment_report(
        uplift, p_treated, p_control, segments, THRESHOLDS, run_id=RUN_ID, computed_on="test", causal=True
    )
    assert report.rows == 5
    assert report.computed_on == "test"
    assert report.causal is True
    assert report.thresholds == THRESHOLDS
    assert [summary.segment for summary in report.segments] == list(SEGMENT_ORDER)
    by_segment = {summary.segment: summary for summary in report.segments}

    persuadable = by_segment[Segment.PERSUADABLE]
    assert persuadable.rows == 2
    assert persuadable.share_pct == pytest.approx(40.0)
    assert persuadable.mean_predicted_uplift == pytest.approx(0.20)
    assert persuadable.mean_p_treated == pytest.approx(0.30)
    assert persuadable.mean_p_control == pytest.approx(0.10)

    assert by_segment[Segment.SURE_THING].rows == 1
    assert by_segment[Segment.SURE_THING].mean_p_control == pytest.approx(0.70)
    assert by_segment[Segment.LOST_CAUSE].rows == 2
    assert by_segment[Segment.LOST_CAUSE].mean_predicted_uplift == pytest.approx(0.0025)

    for summary in report.segments:
        assert summary.label == SEGMENT_LABELS[summary.segment]
        assert summary.action == SEGMENT_ACTIONS[summary.segment]
    assert sum(summary.share_pct for summary in report.segments) == pytest.approx(100.0)


def test_an_empty_segment_is_listed_with_null_means_not_zeros() -> None:
    uplift, p_treated, p_control = _report()
    segments = assign_segments(uplift, p_control, THRESHOLDS)
    report = segment_report(
        uplift, p_treated, p_control, segments, THRESHOLDS, run_id=RUN_ID, computed_on="scored", causal=False
    )
    sleeping = next(summary for summary in report.segments if summary.segment is Segment.SLEEPING_DOG)
    assert sleeping.rows == 0
    assert sleeping.share_pct == 0.0
    assert sleeping.mean_predicted_uplift is None
    assert sleeping.mean_p_treated is None
    assert sleeping.mean_p_control is None
    assert report.causal is False


def test_plain_segment_strings_are_accepted() -> None:
    uplift, p_treated, p_control = _report()
    segments = np.array([str(item) for item in assign_segments(uplift, p_control, THRESHOLDS)], dtype=object)
    report = segment_report(
        uplift, p_treated, p_control, segments, THRESHOLDS, run_id=RUN_ID, computed_on="test", causal=True
    )
    assert sum(summary.rows for summary in report.segments) == 5


def test_an_unknown_segment_value_is_refused() -> None:
    uplift, p_treated, p_control = _report()
    segments = np.array(["persuadable", "persuadable", "sure_thing", "lost_cause", "maybe"], dtype=object)
    with pytest.raises(ValueError, match="1 of 5 rows"):
        segment_report(
            uplift, p_treated, p_control, segments, THRESHOLDS, run_id=RUN_ID, computed_on="test", causal=True
        )


def test_no_rows_means_no_report() -> None:
    empty = np.array([], dtype=np.float64)
    with pytest.raises(ValueError, match="no rows"):
        segment_report(
            empty,
            empty,
            empty,
            np.array([], dtype=object),
            THRESHOLDS,
            run_id=RUN_ID,
            computed_on="test",
            causal=True,
        )


def test_misaligned_report_inputs_are_refused() -> None:
    uplift, p_treated, p_control = _report()
    segments = assign_segments(uplift, p_control, THRESHOLDS)
    with pytest.raises(ValueError, match="same number of rows"):
        segment_report(
            uplift,
            p_treated[:-1],
            p_control,
            segments,
            THRESHOLDS,
            run_id=RUN_ID,
            computed_on="test",
            causal=True,
        )
