"""`engine.stages.prepare.split_dataset`: stratified, grouped and time-based splits."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.config import UseCaseConfig, load_use_case_document
from engine.contracts import SplitReport
from engine.stages.prepare import split_dataset
from engine.utils.ids import seed_from

RUN_ID = "r_20260901_abcdef01"
OTHER_RUN_ID = "r_20260901_0badc0de"
PART_NAMES = ("train", "validation", "test")


def config_for(**patch: dict[str, Any]) -> UseCaseConfig:
    """A validated use case with no template, so only the patched block differs from the defaults."""
    document = load_use_case_document("targeted-advertisement")
    document["template"] = {"columns": []}
    document["target"] = {"column": "converted", "positive_label": 1}
    for block, value in patch.items():
        current = document.get(block)
        document[block] = {**current, **value} if isinstance(current, dict) else value
    return UseCaseConfig.model_validate(document)


def labelled_frame(rows: int = 200, positive_rate: float = 0.3) -> pd.DataFrame:
    positives = round(rows * positive_rate)
    labels = [1] * positives + [0] * (rows - positives)
    return pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(rows)],
            "visits": [float(index % 11) for index in range(rows)],
            "converted": labels,
        }
    )


def dated_frame(rows: int = 100, shuffled: bool = True) -> pd.DataFrame:
    frame = labelled_frame(rows, positive_rate=0.5)
    frame["snapshot_date"] = pd.to_datetime("2026-01-01") + pd.to_timedelta(range(rows), unit="D")
    if shuffled:
        frame = frame.iloc[list(range(0, rows, 2)) + list(range(1, rows, 2))].reset_index(drop=True)
    return frame


def parts_of(report: SplitReport) -> dict[str, int]:
    return {part.name: part.rows for part in report.parts}


# ---------------------------------------------------------------------------
# random stratified
# ---------------------------------------------------------------------------
def test_stratified_split_keeps_the_positive_rate_in_every_part() -> None:
    frame = labelled_frame(400, positive_rate=0.25)

    parts, report = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")

    assert report.type.value == "random_stratified"
    for part in report.parts:
        assert part.positive_rate is not None
        assert part.positive_rate == pytest.approx(0.25, abs=0.03)
        assert part.positive_rows == int(parts[part.name]["converted"].sum())


def test_the_three_parts_hold_every_row_exactly_once() -> None:
    frame = labelled_frame(137)

    parts, report = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")

    indexes = [set(parts[name].index) for name in PART_NAMES]
    assert set().union(*indexes) == set(frame.index)
    assert sum(len(index) for index in indexes) == len(frame)
    assert sum(part.rows for part in report.parts) == len(frame)


def test_the_configured_fractions_decide_the_sizes() -> None:
    frame = labelled_frame(200)
    config = config_for(split={"validation_fraction": 0.2, "test_fraction": 0.1})

    _, report = split_dataset(frame, config, run_id=RUN_ID, target="converted")

    assert parts_of(report) == {"train": 140, "validation": 40, "test": 20}
    assert report.validation_fraction == 0.2
    assert report.test_fraction == 0.1
    assert [part.share for part in report.parts] == [0.7, 0.2, 0.1]


def test_the_seed_comes_from_the_run_id_and_the_split_repeats() -> None:
    frame = labelled_frame(150)

    first_parts, first = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")
    second_parts, second = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")

    assert first.seed == seed_from(RUN_ID)
    for name in PART_NAMES:
        assert list(first_parts[name].index) == list(second_parts[name].index)
    assert first.model_dump(exclude={"split_at"}) == second.model_dump(exclude={"split_at"})


def test_a_different_run_id_draws_a_different_split() -> None:
    frame = labelled_frame(150)

    first_parts, _ = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")
    other_parts, _ = split_dataset(frame, config_for(), run_id=OTHER_RUN_ID, target="converted")

    assert seed_from(RUN_ID) != seed_from(OTHER_RUN_ID)
    assert list(first_parts["test"].index) != list(other_parts["test"].index)


def test_the_detail_line_names_the_three_sizes() -> None:
    frame = labelled_frame(200)

    _, report = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")

    assert report.detail == "140 train · 30 validation · 30 test"


def test_a_label_too_rare_to_stratify_still_splits_and_says_so() -> None:
    frame = labelled_frame(60, positive_rate=0.0)
    frame.loc[0, "converted"] = 1

    parts, report = split_dataset(frame, config_for(), run_id=RUN_ID, target="converted")

    assert sum(len(parts[name]) for name in PART_NAMES) == 60
    assert "not stratified" in report.detail


def test_the_positive_label_is_auto_detected_when_the_config_omits_it() -> None:
    frame = labelled_frame(120, positive_rate=0.25)
    frame["converted"] = ["yes" if value else "no" for value in frame["converted"]]
    config = config_for(target={"column": "converted", "positive_label": None})

    _, report = split_dataset(frame, config, run_id=RUN_ID, target="converted")

    assert sum(part.positive_rows or 0 for part in report.parts) == 30


# ---------------------------------------------------------------------------
# group column
# ---------------------------------------------------------------------------
def test_every_row_of_a_group_lands_in_one_part() -> None:
    frame = labelled_frame(150)
    frame["household_id"] = [f"H{index // 5:03d}" for index in range(150)]
    config = config_for(split={"group_column": "household_id"})

    parts, report = split_dataset(frame, config, run_id=RUN_ID, target="converted")

    assert report.group_column == "household_id"
    placement: dict[str, str] = {}
    for name in PART_NAMES:
        for group in parts[name]["household_id"]:
            assert placement.setdefault(group, name) == name
    assert len(placement) == 30
    assert sum(len(parts[name]) for name in PART_NAMES) == len(frame)
    assert "groups kept whole by household_id" in report.detail


def test_a_grouped_split_needs_enough_groups_to_fill_the_parts() -> None:
    frame = labelled_frame(60)
    frame["household_id"] = ["H1"] * 30 + ["H2"] * 30
    config = config_for(split={"group_column": "household_id"})

    with pytest.raises(ValueError, match="household_id"):
        split_dataset(frame, config, run_id=RUN_ID, target="converted")


# ---------------------------------------------------------------------------
# time-based
# ---------------------------------------------------------------------------
def time_config(**split: Any) -> UseCaseConfig:
    return config_for(split={"type": "time_based", "time_column": "snapshot_date", **split})


def test_time_based_split_is_oldest_train_then_validation_then_newest_test() -> None:
    frame = dated_frame(100)

    parts, report = split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")

    assert parts_of(report) == {"train": 70, "validation": 15, "test": 15}
    assert report.time_column == "snapshot_date"
    train_end = parts["train"]["snapshot_date"].max()
    validation_start = parts["validation"]["snapshot_date"].min()
    validation_end = parts["validation"]["snapshot_date"].max()
    test_start = parts["test"]["snapshot_date"].min()
    assert train_end < validation_start
    assert validation_end < test_start


def test_no_training_timestamp_reaches_into_the_test_period() -> None:
    frame = dated_frame(83)

    parts, _ = split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")

    latest_train = parts["train"]["snapshot_date"].max()
    for later in ("validation", "test"):
        assert latest_train < parts[later]["snapshot_date"].min()
    assert parts["test"]["snapshot_date"].min() > parts["validation"]["snapshot_date"].max()


def test_the_cut_off_dates_and_the_running_line_come_from_the_data() -> None:
    frame = dated_frame(100)
    ordered = sorted(frame["snapshot_date"])

    _, report = split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")

    assert report.train_cutoff is not None
    assert report.test_cutoff is not None
    assert report.train_cutoff.date() == ordered[69].date()
    assert report.test_cutoff.date() == ordered[85].date()
    assert report.detail == f"Training on data before {ordered[85].date().isoformat()}, testing after"


def test_each_part_reports_the_period_it_covers() -> None:
    frame = dated_frame(100)
    ordered = sorted(frame["snapshot_date"])

    _, report = split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")

    train, validation, test = report.parts
    assert train.start_date is not None and train.start_date.date() == ordered[0].date()
    assert validation.start_date is not None and validation.start_date.date() == ordered[70].date()
    assert test.end_date is not None and test.end_date.date() == ordered[-1].date()


def test_rows_sharing_the_boundary_timestamp_all_fall_on_the_later_side() -> None:
    rows = 20
    dates = [pd.Timestamp("2026-01-01") + pd.Timedelta(days=index) for index in range(rows)]
    for index in range(12, 17):
        dates[index] = pd.Timestamp("2026-01-13")
    frame = labelled_frame(rows, positive_rate=0.5)
    frame["snapshot_date"] = dates

    parts, report = split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")

    assert parts_of(report)["train"] == 12
    assert set(parts["validation"]["snapshot_date"]) >= {pd.Timestamp("2026-01-13")}
    assert parts["train"]["snapshot_date"].max() < parts["validation"]["snapshot_date"].min()
    assert sum(parts_of(report).values()) == rows


def test_a_split_that_ties_cannot_honour_is_refused_rather_than_leaked() -> None:
    frame = labelled_frame(20, positive_rate=0.5)
    frame["snapshot_date"] = pd.Timestamp("2026-01-01")

    with pytest.raises(ValueError, match="snapshot_date"):
        split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")


def test_an_unparseable_time_column_is_refused_by_name() -> None:
    frame = dated_frame(40)
    frame["snapshot_date"] = frame["snapshot_date"].astype(str)
    frame.loc[0, "snapshot_date"] = "not a date"

    with pytest.raises(ValueError, match="snapshot_date"):
        split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")


def test_a_time_based_split_needs_the_time_column_to_be_there() -> None:
    frame = labelled_frame(40)

    with pytest.raises(ValueError, match="snapshot_date"):
        split_dataset(frame, time_config(), run_id=RUN_ID, target="converted")


def test_the_time_based_split_does_not_depend_on_the_row_order_of_the_file() -> None:
    ordered_rows = dated_frame(60, shuffled=False)
    shuffled_rows = ordered_rows.iloc[::-1].reset_index(drop=True)

    ordered_parts, ordered_report = split_dataset(
        ordered_rows, time_config(), run_id=RUN_ID, target="converted"
    )
    shuffled_parts, shuffled_report = split_dataset(
        shuffled_rows, time_config(), run_id=RUN_ID, target="converted"
    )

    assert ordered_report.detail == shuffled_report.detail
    for name in PART_NAMES:
        assert sorted(ordered_parts[name]["customer_id"]) == sorted(shuffled_parts[name]["customer_id"])


# ---------------------------------------------------------------------------
# invariants (hypothesis)
# ---------------------------------------------------------------------------
FRACTIONS = st.floats(min_value=0.05, max_value=0.30, allow_nan=False, allow_infinity=False)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rows=st.integers(min_value=4, max_value=150),
    validation_fraction=FRACTIONS,
    test_fraction=FRACTIONS,
)
def test_a_random_split_partitions_the_rows_whatever_the_shape(
    rows: int, validation_fraction: float, test_fraction: float
) -> None:
    frame = labelled_frame(rows, positive_rate=0.5)
    config = config_for(split={"validation_fraction": validation_fraction, "test_fraction": test_fraction})

    parts, report = split_dataset(frame, config, run_id=RUN_ID, target="converted")

    indexes = [set(parts[name].index) for name in PART_NAMES]
    assert set().union(*indexes) == set(frame.index)
    assert sum(len(index) for index in indexes) == rows
    assert sum(part.rows for part in report.parts) == rows
    assert indexes[0].isdisjoint(indexes[1])
    assert indexes[0].isdisjoint(indexes[2])
    assert indexes[1].isdisjoint(indexes[2])


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rows=st.integers(min_value=8, max_value=150),
    validation_fraction=FRACTIONS,
    test_fraction=FRACTIONS,
)
def test_a_time_based_split_partitions_the_rows_and_never_leaks(
    rows: int, validation_fraction: float, test_fraction: float
) -> None:
    frame = dated_frame(rows)
    config = time_config(validation_fraction=validation_fraction, test_fraction=test_fraction)

    parts, report = split_dataset(frame, config, run_id=RUN_ID, target="converted")

    indexes = [set(parts[name].index) for name in PART_NAMES]
    assert set().union(*indexes) == set(frame.index)
    assert sum(len(index) for index in indexes) == rows
    assert sum(part.rows for part in report.parts) == rows
    for earlier, later in (("train", "validation"), ("validation", "test"), ("train", "test")):
        if len(parts[earlier]) and len(parts[later]):
            assert parts[earlier]["snapshot_date"].max() < parts[later]["snapshot_date"].min()
