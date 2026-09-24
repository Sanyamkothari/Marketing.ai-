"""`engine.stages.explain.build_beeswarm`: the Model page's Details plot, computed at explain time.

The page draws `shap_beeswarm.json` without computing anything, so every property the plot relies
on is asserted here on the artefact itself: the sample size, the top-15 ordering, the swarm layout,
the 5th-95th percentile colour scale, the axis, and that no key or raw value leaves the engine.
Expected numbers are worked out by hand from the definitions, not read back from the implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from engine.column_names import ColumnNames, restore_beeswarm
from engine.contracts import (
    ARTEFACT_REGISTRY,
    TRAIN_ARTEFACTS,
    BeeswarmFeature,
    ReasonMethod,
    ShapBeeswarm,
    dump_artefact,
    load_artefact,
)
from engine.stages.explain import (
    BEESWARM_FEATURES,
    BEESWARM_MAX_ROWS,
    BEESWARM_UNAVAILABLE_CAPTION,
    SHAP_BEESWARM_FILENAME,
    MeasuredContributions,
    _axis_ticks,
    _beeswarm_colours,
    _swarm_offsets,
    build_beeswarm,
    reasons_for,
    unavailable_beeswarm,
)
from tests.unit.test_explain import PRIMARY_KEY, FakeScorer, LinearOnlyPredictor, TreePredictor, make_frame

RUN_ID = "r_20260924_0000b33s"
SEED = 20260924


@pytest.fixture(scope="module")
def config():
    from engine.config import load_use_case

    return load_use_case("targeted-advertisement")


def measured(
    features: int = 3, rows: int = 50, method: ReasonMethod = ReasonMethod.TREE_SHAP
) -> MeasuredContributions:
    """Feature `f{i}` contributes `(i + 1) * (value - mean)` of one shared column, so the order is known."""
    base = np.random.default_rng(3).normal(size=rows)
    frame = pd.DataFrame({f"f{index:02d}": base for index in range(features)})
    frame.insert(0, PRIMARY_KEY, [f"secret-{index:05d}" for index in range(rows)])
    totals = {
        f"f{index:02d}": list((index + 1) * (frame[f"f{index:02d}"] - frame[f"f{index:02d}"].mean()))
        for index in range(features)
    }
    return MeasuredContributions(rows=frame, totals=totals, method=method)


# ---------------------------------------------------------------------------
# The registry and the capture in `reasons_for`
# ---------------------------------------------------------------------------
def test_the_beeswarm_is_a_registered_training_artefact() -> None:
    assert ARTEFACT_REGISTRY[SHAP_BEESWARM_FILENAME] is ShapBeeswarm
    assert SHAP_BEESWARM_FILENAME in TRAIN_ARTEFACTS


def test_reasons_for_keeps_what_the_first_tier_measured(config) -> None:
    frame = make_frame(30)
    reasons = reasons_for(
        FakeScorer(predictor=TreePredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        keep_measured=True,
    )
    assert reasons.measured is not None
    assert reasons.measured.method is ReasonMethod.TREE_SHAP is reasons.method
    assert len(reasons.measured.rows) == 30
    assert all(len(values) == 30 for values in reasons.measured.totals.values())
    assert {"visits_last_7d", "spend_last_30d"} <= set(reasons.measured.totals)


def test_the_capture_names_the_tier_that_really_measured(config) -> None:
    frame = make_frame(30)
    reasons = reasons_for(
        FakeScorer(predictor=LinearOnlyPredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        keep_measured=True,
    )
    assert reasons.measured is not None
    assert reasons.measured.method is ReasonMethod.KERNEL_SHAP


def test_the_capture_does_not_change_how_reasons_compare(config) -> None:
    frame = make_frame(12)
    scorer = FakeScorer(predictor=TreePredictor(frame))
    first = reasons_for(scorer, frame, config, primary_key=PRIMARY_KEY, seed=1, keep_measured=True)
    second = reasons_for(scorer, frame, config, primary_key=PRIMARY_KEY, seed=1, keep_measured=True)
    assert first == second  # a DataFrame in `measured` would make this raise, were it compared


def test_nothing_is_kept_unless_the_caller_asks(config) -> None:
    """The score flow explains every row and has no beeswarm, so it must not hold a copy of the frame."""
    frame = make_frame(12)
    reasons = reasons_for(
        FakeScorer(predictor=TreePredictor(frame)), frame, config, primary_key=PRIMARY_KEY, seed=1
    )
    assert reasons.measured is None


def test_a_beeswarm_from_real_treeshap_contributions(config) -> None:
    frame = make_frame(40)
    reasons = reasons_for(
        FakeScorer(predictor=TreePredictor(frame)),
        frame,
        config,
        primary_key=PRIMARY_KEY,
        seed=1,
        keep_measured=True,
    )
    beeswarm = build_beeswarm(reasons.measured, run_id=RUN_ID, seed=SEED)
    assert beeswarm.method is ReasonMethod.TREE_SHAP
    assert beeswarm.rows_sampled == beeswarm.rows_explained == 40
    assert beeswarm.x_label.startswith("SHAP value")
    for item in beeswarm.features:
        assert item.numeric
        assert all(value is not None and 0.0 <= value <= 1.0 for value in item.colours)


# ---------------------------------------------------------------------------
# Sample and ordering
# ---------------------------------------------------------------------------
def test_at_most_two_thousand_rows_are_plotted_and_the_sample_is_seeded() -> None:
    source = measured(rows=5000)
    beeswarm = build_beeswarm(source, run_id=RUN_ID, seed=SEED)
    assert BEESWARM_MAX_ROWS == 2000
    assert beeswarm.rows_explained == 5000 and beeswarm.rows_sampled == 2000
    assert all(len(item.contributions) == 2000 for item in beeswarm.features)
    assert build_beeswarm(source, run_id=RUN_ID, seed=SEED) == beeswarm
    assert build_beeswarm(source, run_id=RUN_ID, seed=SEED + 1) != beeswarm
    assert "2,000 of the 5,000 test rows" in beeswarm.caption


def test_a_small_frame_is_plotted_whole() -> None:
    beeswarm = build_beeswarm(measured(rows=40), run_id=RUN_ID, seed=SEED)
    assert beeswarm.rows_sampled == beeswarm.rows_explained == 40
    assert "all 40 test rows" in beeswarm.caption


def test_only_the_fifteen_strongest_features_are_plotted_strongest_first() -> None:
    beeswarm = build_beeswarm(measured(features=20), run_id=RUN_ID, seed=SEED)
    assert BEESWARM_FEATURES == 15 == beeswarm.top_n == len(beeswarm.features)
    # f19 contributes 20x its centred value, f18 19x, ... so the order is the reverse of the names.
    assert [item.feature for item in beeswarm.features] == [f"f{index:02d}" for index in range(19, 4, -1)]
    assert [item.rank for item in beeswarm.features] == list(range(1, 16))
    strengths = [item.mean_abs_contribution for item in beeswarm.features]
    assert strengths == sorted(strengths, reverse=True)


def test_a_tie_in_strength_is_broken_by_the_global_chart_rank() -> None:
    source = measured(features=1)
    source = MeasuredContributions(
        rows=source.rows.assign(b=source.rows["f00"], a=source.rows["f00"]),
        totals={"a": source.totals["f00"], "b": source.totals["f00"]},
        method=source.method,
    )
    beeswarm = build_beeswarm(source, run_id=RUN_ID, seed=SEED, rank={"b": 1, "a": 2})
    assert [item.feature for item in beeswarm.features] == ["b", "a"]


# ---------------------------------------------------------------------------
# Coordinates: contributions, offsets, colours, axis
# ---------------------------------------------------------------------------
def test_each_dot_is_the_measured_contribution() -> None:
    source = measured(features=1, rows=10)
    beeswarm = build_beeswarm(source, run_id=RUN_ID, seed=SEED)
    assert beeswarm.features[0].contributions == tuple(round(value, 6) for value in source.totals["f00"])


def test_identical_contributions_stack_alternately_above_and_below_the_line() -> None:
    # Five equal values share one bin: layers 0, +1, -1, +2, -2, divided by the tallest pile plus one.
    offsets = _swarm_offsets(np.zeros(5), seed=0)
    assert sorted(offsets) == [-0.667, -0.333, 0.0, 0.333, 0.667]


def test_well_separated_contributions_sit_on_the_centre_line() -> None:
    assert _swarm_offsets(np.array([0.0, 1.0, 2.0, 3.0]), seed=0) == (0.0, 0.0, 0.0, 0.0)


def test_every_offset_stays_inside_its_band() -> None:
    beeswarm = build_beeswarm(measured(rows=3000), run_id=RUN_ID, seed=SEED)
    for item in beeswarm.features:
        assert all(-1.0 < offset < 1.0 for offset in item.offsets)
        assert len(item.offsets) == len(item.contributions) == len(item.colours)


def test_colour_runs_between_the_fifth_and_ninety_fifth_percentiles() -> None:
    column = pd.Series(np.arange(101, dtype=float))  # 5th percentile 5, 95th 95
    numeric, colours = _beeswarm_colours(column)
    assert numeric
    assert colours[0] == colours[5] == 0.0
    assert colours[50] == 0.5
    assert colours[95] == colours[100] == 1.0
    assert colours[23] == round(18 / 90, 3)


def test_a_missing_value_is_grey_and_a_category_is_grey_throughout() -> None:
    _, colours = _beeswarm_colours(pd.Series([1.0, None, 3.0, 4.0]))
    assert colours[1] is None and colours[0] is not None
    numeric, grey = _beeswarm_colours(pd.Series(["basic", "pro", "basic"]))
    assert not numeric and grey == (None, None, None)


def test_booleans_nullable_integers_and_dates_are_numeric() -> None:
    _, flags = _beeswarm_colours(pd.Series([True, False, True, False] * 10))
    assert set(flags) == {0.0, 1.0}
    _, counts = _beeswarm_colours(pd.Series([1, None, 3] * 10, dtype="Int64"))
    assert counts[1] is None and counts[0] == 0.0 and counts[2] == 1.0
    _, days = _beeswarm_colours(pd.Series(pd.to_datetime(["2026-01-01", "2026-06-01", None] * 10)))
    assert days[0] == 0.0 and days[1] == 1.0 and days[2] is None


def test_a_constant_feature_sits_in_the_middle_of_the_scale() -> None:
    _, colours = _beeswarm_colours(pd.Series([7.0] * 20))
    assert set(colours) == {0.5}


def test_the_axis_covers_every_dot_and_zero_with_round_ticks() -> None:
    beeswarm = build_beeswarm(measured(), run_id=RUN_ID, seed=SEED)
    plotted = [value for item in beeswarm.features for value in item.contributions]
    assert beeswarm.x_min == beeswarm.ticks[0] <= min(plotted)
    assert beeswarm.x_max == beeswarm.ticks[-1] >= max(plotted)
    assert 0.0 in beeswarm.ticks
    assert list(beeswarm.ticks) == sorted(beeswarm.ticks)
    assert _axis_ticks(0.03, 0.87) == (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    assert _axis_ticks(-0.3, 0.1) == (-0.3, -0.2, -0.1, 0.0, 0.1)
    assert _axis_ticks(0.0, 0.0) == (-1.0, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Method, emptiness, privacy, names
# ---------------------------------------------------------------------------
def test_permutation_contributions_are_never_called_shap() -> None:
    beeswarm = build_beeswarm(measured(method=ReasonMethod.PERMUTATION), run_id=RUN_ID, seed=SEED)
    assert beeswarm.method is ReasonMethod.PERMUTATION
    assert "SHAP value" not in beeswarm.x_label
    assert beeswarm.caption.startswith("SHAP could not be computed for this model")


def test_nothing_measured_is_an_empty_plot_that_says_so() -> None:
    for source in (
        None,
        MeasuredContributions(rows=pd.DataFrame(), totals={}, method=ReasonMethod.TREE_SHAP),
    ):
        beeswarm = build_beeswarm(source, run_id=RUN_ID, seed=SEED)
        assert beeswarm == unavailable_beeswarm(RUN_ID)
        assert beeswarm.features == () and beeswarm.method is None
        assert beeswarm.caption == BEESWARM_UNAVAILABLE_CAPTION


def test_a_measurement_that_cannot_be_plotted_costs_the_plot_not_the_run() -> None:
    source = measured(rows=10)
    broken = MeasuredContributions(rows=source.rows, totals={"f00": [0.1, 0.2]}, method=source.method)
    assert build_beeswarm(broken, run_id=RUN_ID, seed=SEED) == unavailable_beeswarm(RUN_ID)


def test_the_file_carries_no_key_and_no_raw_value_and_round_trips() -> None:
    source = measured(features=2, rows=30)
    beeswarm = build_beeswarm(source, run_id=RUN_ID, seed=SEED)
    payload = dump_artefact(beeswarm)
    assert "secret-" not in payload
    raw = f"{source.rows['f00'].iloc[0]:.6f}"
    assert raw not in payload
    assert load_artefact(SHAP_BEESWARM_FILENAME, payload) == beeswarm


def test_the_three_per_row_tuples_must_be_parallel() -> None:
    with pytest.raises(ValidationError, match="one entry per sampled row"):
        BeeswarmFeature(
            rank=1,
            feature="a",
            mean_abs_contribution=0.1,
            numeric=True,
            contributions=(0.1, 0.2),
            offsets=(0.0,),
            colours=(0.5, None),
        )


def test_features_are_restored_to_the_client_s_column_names() -> None:
    names = ColumnNames(renamed={"Monthly Spend": "monthly_spend"})
    source = measured(features=1)
    source = MeasuredContributions(
        rows=source.rows.rename(columns={"f00": "monthly_spend"}),
        totals={"monthly_spend": source.totals["f00"]},
        method=source.method,
    )
    beeswarm = restore_beeswarm(build_beeswarm(source, run_id=RUN_ID, seed=SEED), names)
    assert [item.feature for item in beeswarm.features] == ["Monthly Spend"]
