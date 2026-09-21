"""`engine.stages.prepare`: the row phase, the train-only statistical fit, replay and the seam.

Exclusions, PII, missing values, outliers and consent, plus the sequence the train pipeline must
use - `prepare_rows`, `split_dataset`, `fit_transforms(fit_index=...)` - and the proof that no
fitted parameter has seen a validation or test row (DEC-046).
"""

from __future__ import annotations

import inspect
from typing import Any

import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case_document
from engine.contracts import PrepareReport
from engine.stages.prepare import (
    REDACTION,
    fit_transforms,
    prepare,
    prepare_rows,
    replay,
    split_dataset,
)

RUN_ID = "r_20260901_abcdef01"


def config_for(**patch: dict[str, Any]) -> UseCaseConfig:
    """A validated use case with no template (so only the block under test differs from the defaults)."""
    document = load_use_case_document("targeted-advertisement")
    document["template"] = {"columns": []}
    document["target"] = {"column": "converted", "positive_label": 1}
    for block, value in patch.items():
        current = document.get(block)
        document[block] = {**current, **value} if isinstance(current, dict) else value
    return UseCaseConfig.model_validate(document)


def run(frame: pd.DataFrame, config: UseCaseConfig, **kwargs: Any) -> tuple[pd.DataFrame, PrepareReport]:
    return prepare(
        frame,
        config,
        run_id=kwargs.pop("run_id", RUN_ID),
        primary_key=kwargs.pop("primary_key", "customer_id"),
        target=kwargs.pop("target", "converted"),
    )


def base_frame(rows: int = 20) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(rows)],
            "visits": [float(index % 7) for index in range(rows)],
            "plan_type": ["basic" if index % 2 else "premium" for index in range(rows)],
            "converted": [index % 2 for index in range(rows)],
        }
    )


def reasons(report: PrepareReport) -> dict[str, str]:
    return {column.name: column.reason for column in report.dropped_columns}


def removals(report: PrepareReport) -> dict[str, int]:
    return {removal.reason: removal.rows for removal in report.row_removals}


def transforms_of(report: PrepareReport, kind: str) -> list[Any]:
    return [transform for transform in report.transforms if transform.kind == kind]


# ---------------------------------------------------------------------------
# exclusions
# ---------------------------------------------------------------------------
def test_user_excluded_column_is_dropped_with_that_reason() -> None:
    frame = base_frame()
    frame["legacy_score"] = [float(index) for index in range(len(frame))]
    config = config_for(prepare={"exclude_columns": ["legacy_score"]})

    prepared, report = run(frame, config)

    assert reasons(report)["legacy_score"] == "user_excluded"
    assert "legacy_score" not in prepared.columns
    assert "legacy_score" not in report.feature_columns


def test_id_like_column_is_dropped() -> None:
    frame = base_frame()
    frame["session_token"] = [f"tok-{index}" for index in range(len(frame))]

    prepared, report = run(frame, config_for())

    assert reasons(report)["session_token"] == "id_like"
    assert "session_token" not in prepared.columns


def test_constant_column_is_dropped() -> None:
    frame = base_frame()
    frame["region"] = "south"

    _, report = run(frame, config_for())

    assert reasons(report)["region"] == "constant"


def test_high_null_column_is_dropped_with_its_null_rate() -> None:
    frame = base_frame(20)
    frame["late_signal"] = [float(index) if index < 5 else None for index in range(20)]

    _, report = run(frame, config_for())

    dropped = {column.name: column for column in report.dropped_columns}
    assert dropped["late_signal"].reason == "high_null"
    assert "75%" in dropped["late_signal"].detail


def test_a_column_is_high_null_only_above_the_configured_rate() -> None:
    frame = base_frame(20)
    frame["late_signal"] = [float(index) if index < 5 else None for index in range(20)]
    config = config_for(validation={"high_null_column_rate": 0.9})

    _, report = run(frame, config)

    assert "late_signal" not in reasons(report)


def test_reserved_columns_survive_every_heuristic() -> None:
    frame = base_frame()
    frame["snapshot_date"] = "2026-01-01"
    config = config_for(
        split={"type": "time_based", "time_column": "snapshot_date"},
        governance={"consent_column": None},
    )

    prepared, report = run(frame, config)

    assert "customer_id" in prepared.columns
    assert "snapshot_date" in prepared.columns
    assert reasons(report) == {}
    assert "customer_id" not in report.feature_columns
    assert "snapshot_date" not in report.feature_columns


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------
def test_pii_columns_are_redacted_in_place_by_default() -> None:
    frame = base_frame()
    frame["email"] = [f"user{index}@example.com" for index in range(len(frame))]

    prepared, report = run(frame, config_for(prepare={"pii_handling": "redact"}))

    assert report.pii_columns == ("email",)
    assert set(prepared["email"]) == {REDACTION}
    assert "email" not in report.feature_columns
    assert transforms_of(report, "redact")[0].columns == ("email",)


def test_pii_columns_are_dropped_when_the_setting_says_so() -> None:
    frame = base_frame()
    frame["email"] = [f"user{index}@example.com" for index in range(len(frame))]

    prepared, report = run(frame, config_for(prepare={"pii_handling": "drop_columns"}))

    assert reasons(report)["email"] == "pii"
    assert "email" not in prepared.columns
    assert report.pii_columns == ("email",)
    assert transforms_of(report, "redact") == []


def test_pii_is_detected_from_values_when_the_name_says_nothing() -> None:
    frame = base_frame(10)
    frame["contact_line"] = [f"person{index}@example.org" for index in range(10)]

    _, report = run(frame, config_for(prepare={"pii_handling": "redact"}))

    assert report.pii_columns == ("contact_line",)


def test_pii_is_reported_but_kept_when_handling_is_keep() -> None:
    frame = base_frame(10)
    frame["email"] = ["a@example.com", "b@example.com"] * 5

    prepared, report = run(frame, config_for(prepare={"pii_handling": "keep"}))

    assert report.pii_columns == ("email",)
    assert "email" in prepared.columns
    assert prepared["email"].iloc[0] == "a@example.com"


# ---------------------------------------------------------------------------
# rows: consent, duplicates, missing target
# ---------------------------------------------------------------------------
def test_consent_filter_keeps_only_consenting_rows_and_counts_the_rest() -> None:
    frame = base_frame(10)
    frame["consent_flag"] = [True, False, True, True, False, None, "yes", "no", 1, 0]
    config = config_for(governance={"consent_column": "consent_flag"}, prepare={"deduplicate": False})

    prepared, report = run(frame, config)

    assert report.consent_column == "consent_flag"
    assert report.consent_rows_removed == 5
    assert removals(report)["consent_false"] == 5
    assert len(prepared) == 5
    assert report.rows_in == 10
    assert report.rows_out == 5


def test_a_configured_consent_column_that_is_absent_is_recorded_not_skipped() -> None:
    config = config_for(governance={"consent_column": "consent_flag"})

    _, report = run(base_frame(6), config)

    consent = transforms_of(report, "consent_filter")[0]
    assert consent.parameters["applied"] is False
    assert consent.parameters["reason"] == "column_absent"
    assert report.consent_rows_removed == 0


def test_exact_duplicate_rows_are_removed_and_counted() -> None:
    frame = pd.concat([base_frame(6), base_frame(6).iloc[:2]], ignore_index=True)

    prepared, report = run(frame, config_for(prepare={"deduplicate": True}))

    assert removals(report)["duplicate"] == 2
    assert len(prepared) == 6
    assert transforms_of(report, "dedupe")[0].parameters["rows_removed"] == 2.0


def test_duplicates_are_kept_when_deduplication_is_off() -> None:
    frame = pd.concat([base_frame(6), base_frame(6).iloc[:2]], ignore_index=True)

    prepared, report = run(frame, config_for(prepare={"deduplicate": False}))

    assert len(prepared) == 8
    assert "duplicate" not in removals(report)


def test_rows_without_a_target_are_removed() -> None:
    frame = base_frame(8)
    frame.loc[0:1, "converted"] = None

    prepared, report = run(frame, config_for(prepare={"deduplicate": False}))

    assert removals(report)["missing_target"] == 2
    assert len(prepared) == 6


# ---------------------------------------------------------------------------
# missing values
# ---------------------------------------------------------------------------
def test_missing_values_auto_leaves_the_gaps_for_autogluon() -> None:
    frame = base_frame(10)
    frame.loc[0, "visits"] = None

    prepared, report = run(frame, config_for(prepare={"missing_values": "auto"}))

    assert prepared["visits"].isna().sum() == 1
    assert transforms_of(report, "fill_median") == []
    assert transforms_of(report, "fill_mode") == []


def test_missing_values_fill_uses_the_median_and_the_mode() -> None:
    frame = base_frame(10)
    frame.loc[0, "visits"] = None
    frame.loc[1, "plan_type"] = None
    config = config_for(prepare={"missing_values": "fill", "outliers": "keep"})

    prepared, report = run(frame, config)

    median = float(frame["visits"].dropna().median())
    mode = str(frame["plan_type"].dropna().mode().iloc[0])
    assert prepared["visits"].isna().sum() == 0
    assert prepared["plan_type"].isna().sum() == 0
    assert prepared["visits"].iloc[0] == pytest.approx(median)
    assert prepared["plan_type"].iloc[1] == mode
    assert transforms_of(report, "fill_median")[0].parameters["value"] == pytest.approx(median)
    assert transforms_of(report, "fill_mode")[0].parameters["value"] == mode


def test_missing_values_drop_rows_drops_only_rows_with_a_missing_feature() -> None:
    frame = base_frame(10)
    frame.loc[0, "visits"] = None
    frame.loc[1, "plan_type"] = None
    config = config_for(prepare={"missing_values": "drop_rows", "deduplicate": False})

    prepared, report = run(frame, config)

    assert len(prepared) == 8
    assert report.rows_out == 8
    assert "rows dropped for missing values" in report.detail


def test_a_column_that_cannot_be_filled_is_recorded_as_not_applied() -> None:
    frame = base_frame(10)
    frame["sensor"] = None
    config = config_for(
        prepare={"missing_values": "fill", "outliers": "keep"},
        validation={"high_null_column_rate": 1.0},
    )

    _, report = run(frame, config)

    empty = [
        transform
        for transform in report.transforms
        if transform.columns == ("sensor",) and transform.parameters.get("applied") is False
    ]
    assert empty and empty[0].parameters["reason"] == "all_values_missing"


# ---------------------------------------------------------------------------
# outliers
# ---------------------------------------------------------------------------
def test_numeric_outliers_are_clipped_to_the_first_and_ninety_ninth_percentile() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(100)],
            "spend": [float(index) for index in range(99)] + [10_000.0],
            "converted": [index % 2 for index in range(100)],
        }
    )
    config = config_for(prepare={"outliers": "clip", "deduplicate": False})

    prepared, report = run(frame, config)

    lower = float(frame["spend"].quantile(0.01))
    upper = float(frame["spend"].quantile(0.99))
    clip = transforms_of(report, "clip_percentile")[0]
    assert clip.columns == ("spend",)
    assert clip.parameters["lower"] == pytest.approx(lower)
    assert clip.parameters["upper"] == pytest.approx(upper)
    assert clip.parameters["lower_quantile"] == 0.01
    assert clip.parameters["upper_quantile"] == 0.99
    assert prepared["spend"].max() == pytest.approx(upper)
    assert prepared["spend"].min() == pytest.approx(lower)


def test_outliers_keep_leaves_the_values_alone() -> None:
    frame = base_frame(20)
    frame.loc[0, "visits"] = 9_999.0
    config = config_for(prepare={"outliers": "keep", "deduplicate": False})

    prepared, report = run(frame, config)

    assert prepared["visits"].max() == 9_999.0
    assert transforms_of(report, "clip_percentile") == []


def test_outliers_remove_rows_removes_them_and_counts_them() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(100)],
            "spend": [float(index) for index in range(99)] + [10_000.0],
            "converted": [index % 2 for index in range(100)],
        }
    )
    config = config_for(prepare={"outliers": "remove_rows", "deduplicate": False})

    prepared, report = run(frame, config)

    assert removals(report)["outlier"] == len(frame) - len(prepared)
    assert prepared["spend"].max() < 10_000.0


# ---------------------------------------------------------------------------
# the report itself
# ---------------------------------------------------------------------------
def test_the_report_counts_and_detail_line_describe_what_happened() -> None:
    frame = base_frame(10)
    frame["region"] = "south"
    config = config_for(prepare={"deduplicate": False})

    prepared, report = run(frame, config)

    assert report.run_id == RUN_ID
    assert report.rows_in == 10
    assert report.rows_out == len(prepared)
    assert report.columns_in == 5
    assert report.columns_out == len(prepared.columns)
    assert report.feature_columns == ("visits", "plan_type")
    assert report.detail == "10 rows ready · 2 features · 1 column dropped"


def test_transform_order_is_dense_and_starts_at_one() -> None:
    frame = base_frame(10)
    frame.loc[0, "visits"] = None
    config = config_for(prepare={"missing_values": "fill"}, governance={"consent_column": None})

    _, report = run(frame, config)

    assert [transform.order for transform in report.transforms] == list(range(1, len(report.transforms) + 1))


def test_prepare_does_not_touch_the_caller_frame() -> None:
    frame = base_frame(10)
    frame["email"] = [f"user{index}@example.com" for index in range(10)]
    before = frame.copy()

    run(frame, config_for())

    pd.testing.assert_frame_equal(frame, before)


def test_the_same_input_and_run_id_give_the_same_report() -> None:
    frame = base_frame(20)
    frame.loc[0, "visits"] = None
    config = config_for(prepare={"missing_values": "fill"})

    first_frame, first = run(frame, config)
    second_frame, second = run(frame, config)

    pd.testing.assert_frame_equal(first_frame, second_frame)
    assert first.model_dump(exclude={"prepared_at"}) == second.model_dump(exclude={"prepared_at"})


# ---------------------------------------------------------------------------
# fit on train only, and replay
# ---------------------------------------------------------------------------
def test_fill_and_clip_use_the_training_statistic_not_the_new_frame() -> None:
    train = pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(100)],
            "spend": [10.0] * 99 + [11.0],
            "converted": [index % 2 for index in range(100)],
        }
    )
    later = pd.DataFrame(
        {
            "customer_id": [f"D{index:04d}" for index in range(100)],
            "spend": [1_000.0] * 99 + [None],
            "converted": [index % 2 for index in range(100)],
        }
    )
    config = config_for(prepare={"missing_values": "fill", "outliers": "clip", "deduplicate": False})

    _, report = run(train, config)
    replayed = replay(later, report)

    train_median = float(train["spend"].median())
    train_upper = float(train["spend"].quantile(0.99))
    assert train_median == 10.0
    assert replayed["spend"].iloc[-1] == pytest.approx(train_median)
    assert replayed["spend"].max() == pytest.approx(train_upper)
    assert replayed["spend"].max() < 1_000.0


def test_replay_reproduces_the_prepared_frame_exactly() -> None:
    frame = base_frame(20)
    frame.loc[0, "visits"] = None
    frame["email"] = [f"user{index}@example.com" for index in range(20)]
    frame["region"] = "south"
    config = config_for(prepare={"missing_values": "fill", "deduplicate": False})

    prepared, report = run(frame, config)
    replayed = replay(frame, report)

    pd.testing.assert_frame_equal(replayed, prepared)


def test_replay_skips_a_transform_whose_column_is_missing() -> None:
    frame = base_frame(20)
    frame.loc[0, "visits"] = None
    frame["plan_type"] = [f"tier{index % 3}" for index in range(20)]
    config = config_for(prepare={"missing_values": "fill", "deduplicate": False})

    prepared, report = run(frame, config)
    without_visits = frame.drop(columns=["visits"])

    replayed = replay(without_visits, report)

    assert "visits" not in replayed.columns
    assert len(replayed) == len(prepared)
    assert list(replayed["plan_type"]) == list(frame["plan_type"])


def test_replay_leaves_an_extra_column_untouched() -> None:
    frame = base_frame(20)
    config = config_for(prepare={"deduplicate": False})
    _, report = run(frame, config)

    extra = frame.copy()
    extra["new_feature"] = [float(index) for index in range(20)]
    replayed = replay(extra, report)

    assert "new_feature" in replayed.columns
    assert list(replayed["new_feature"]) == [float(index) for index in range(20)]


def test_replay_drops_the_columns_the_report_dropped_and_tolerates_their_absence() -> None:
    frame = base_frame(20)
    frame["region"] = "south"
    _, report = run(frame, config_for())

    assert "region" not in replay(frame, report).columns
    assert "region" not in replay(frame.drop(columns=["region"]), report).columns


def test_replay_keeps_every_row_even_when_training_removed_some() -> None:
    frame = pd.concat([base_frame(6), base_frame(6).iloc[:2]], ignore_index=True)
    frame["consent_flag"] = [True] * 6 + [False, False]
    config = config_for(governance={"consent_column": "consent_flag"}, prepare={"deduplicate": True})

    prepared, report = run(frame, config)
    replayed = replay(frame, report)

    assert len(prepared) < len(frame)
    assert len(replayed) == len(frame)


def test_replay_does_not_touch_the_caller_frame() -> None:
    frame = base_frame(20)
    frame["email"] = [f"user{index}@example.com" for index in range(20)]
    _, report = run(frame, config_for())
    before = frame.copy()

    replay(frame, report)

    pd.testing.assert_frame_equal(frame, before)


# ---------------------------------------------------------------------------
# the seam: row phase, then the split, then a fit that sees training rows only
# ---------------------------------------------------------------------------
SKEW_TRAIN_ROWS = 80  # 200 rows, 30% validation and 30% test, so the hold-out is the majority


def skewed_frame(rows: int = 200) -> pd.DataFrame:
    """A file whose hold-out is a different world: training spends 10-12, everything later 1_000+.

    The validation and test rows are 60% of the file, so any statistic fitted over the whole thing
    lands on *their* numbers: the whole-file median and 99th percentile are three orders of magnitude
    away from the training ones, and the whole-file mode is the other category. Rows are handed over
    out of date order, so nothing can pass by reading the first N rows instead of the split.
    """
    spend: list[float | None] = []
    plan_type: list[str | None] = []
    for index in range(rows):
        early = index < SKEW_TRAIN_ROWS
        spend.append(10.0 + index % 3 if early else 1_000.0 + index)
        plan_type.append("basic" if early else "premium")
    spend[SKEW_TRAIN_ROWS + 1] = None  # a gap in validation ...
    spend[rows - 1] = None  # ... and one in test
    plan_type[SKEW_TRAIN_ROWS + 2] = None
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(rows)],
            "snapshot_date": pd.to_datetime("2026-01-01") + pd.to_timedelta(range(rows), unit="D"),
            "spend": spend,
            "plan_type": plan_type,
            "converted": [index % 2 for index in range(rows)],
        }
    )
    return frame.iloc[list(range(0, rows, 2)) + list(range(1, rows, 2))].reset_index(drop=True)


def skew_config(**prepare_block: Any) -> UseCaseConfig:
    return config_for(
        split={
            "type": "time_based",
            "time_column": "snapshot_date",
            "validation_fraction": 0.3,
            "test_fraction": 0.3,
        },
        governance={"consent_column": None},
        prepare={"deduplicate": False, **prepare_block},
    )


def seam(frame: pd.DataFrame, config: UseCaseConfig) -> tuple[pd.DataFrame, pd.DataFrame, PrepareReport, Any]:
    """The sequence the train pipeline must use: row phase, split, then fit on the training rows."""
    rows, plan = prepare_rows(frame, config, primary_key="customer_id", target="converted")
    parts, _ = split_dataset(rows, config, run_id=RUN_ID, target="converted")
    prepared, report = fit_transforms(rows, config, plan, run_id=RUN_ID, fit_index=parts["train"].index)
    return rows, prepared, report, parts


def gaps(frame: pd.DataFrame, column: str) -> Any:
    """A positional mask of the rows whose `column` was missing in the row-prepared frame."""
    return frame[column].isna().to_numpy()


def only(report: PrepareReport, kind: str, column: str) -> Any:
    matching = [
        transform
        for transform in report.transforms
        if transform.kind == kind and transform.columns == (column,)
    ]
    assert len(matching) == 1, f"expected exactly one {kind} for {column}, got {len(matching)}"
    return matching[0]


def test_statistics_are_fitted_on_the_training_rows_alone() -> None:
    config = skew_config(missing_values="fill", outliers="clip")
    frame = skewed_frame()

    rows, prepared, report, parts = seam(frame, config)

    train = rows.loc[parts["train"].index]
    held_out = rows.loc[parts["validation"].index.union(parts["test"].index)]
    assert len(train) == SKEW_TRAIN_ROWS
    # The premise: the two populations really are wildly different, and the hold-out is the majority.
    assert float(train["spend"].median()) == pytest.approx(11.0)
    assert float(held_out["spend"].median()) > 1_000.0
    assert float(rows["spend"].median()) > 1_000.0
    assert float(rows["spend"].quantile(0.99)) > 1_000.0

    fill = only(report, "fill_median", "spend")
    clip = only(report, "clip_percentile", "spend")
    mode = only(report, "fill_mode", "plan_type")

    # Fitted on the training rows, to the digit.
    assert fill.parameters["value"] == pytest.approx(float(train["spend"].median()))
    assert clip.parameters["lower"] == pytest.approx(float(train["spend"].quantile(0.01)))
    assert clip.parameters["upper"] == pytest.approx(float(train["spend"].quantile(0.99)))
    assert mode.parameters["value"] == "basic"
    assert fill.parameters["fit_rows"] == float(len(train))
    # ... and nowhere near the numbers the hold-out would have produced.
    assert float(fill.parameters["value"]) < 20.0
    assert float(clip.parameters["upper"]) < 20.0
    assert str(mode.parameters["value"]) != str(held_out["plan_type"].mode().iloc[0])

    # Applying them leaves the hold-out visibly shifted, not centred on itself: every hold-out spend
    # is pinned to the training ceiling, and the gaps are filled with the training median.
    ceiling = float(clip.parameters["upper"])
    fitted_holdout = prepared.loc[held_out.index, "spend"]
    missing = gaps(held_out, "spend")
    assert set(fitted_holdout[~missing]) == {ceiling}
    assert set(fitted_holdout[missing]) == {float(fill.parameters["value"])}
    assert float(fitted_holdout.median()) < 20.0 < float(held_out["spend"].median())
    fitted_plans = prepared.loc[held_out.index, "plan_type"]
    assert set(fitted_plans[gaps(held_out, "plan_type")]) == {"basic"}


def test_the_train_only_fit_replaces_the_whole_file_numbers_the_old_prepare_used() -> None:
    """Before and after, side by side: `prepare` still fits on every row it is given, and leaks."""
    config = skew_config(missing_values="fill", outliers="clip")
    frame = skewed_frame()

    _, leaky_report = run(frame, config)  # every row a fit row: the pre-DEC-046 behaviour
    rows, _, report, parts = seam(frame, config)

    train = rows.loc[parts["train"].index]
    leaky_fill = float(only(leaky_report, "fill_median", "spend").parameters["value"])
    leaky_upper = float(only(leaky_report, "clip_percentile", "spend").parameters["upper"])
    fitted_fill = float(only(report, "fill_median", "spend").parameters["value"])
    fitted_upper = float(only(report, "clip_percentile", "spend").parameters["upper"])

    assert leaky_fill > 1_000.0 and leaky_upper > 1_000.0
    assert leaky_fill == pytest.approx(float(rows["spend"].median()))
    assert fitted_fill == pytest.approx(float(train["spend"].median()))
    assert fitted_upper == pytest.approx(float(train["spend"].quantile(0.99)))
    assert leaky_fill / fitted_fill > 50.0  # 1_099.5 against 11.0 on this fixture
    assert leaky_report.transforms != report.transforms
    assert only(leaky_report, "fill_median", "spend").parameters["fit_rows"] == float(len(rows))


def test_a_column_only_the_hold_out_has_seen_is_not_filled_from_it() -> None:
    config = skew_config(missing_values="fill", outliers="keep")
    frame = skewed_frame()
    # Observed in the hold-out only: `skewed_frame` hands rows over out of order, so the gap is
    # keyed on the customer id rather than on the row's position in the file.
    ages = [int(key[1:]) for key in frame["customer_id"]]
    frame["late_signal"] = [None if age < SKEW_TRAIN_ROWS else 900.0 + age for age in ages]

    rows, prepared, report, parts = seam(frame, config)

    train = rows.loc[parts["train"].index]
    assert train["late_signal"].isna().all()
    assert rows["late_signal"].notna().any()
    late = only(report, "fill_median", "late_signal")
    assert late.parameters["applied"] is False
    assert late.parameters["reason"] == "all_values_missing"
    assert prepared.loc[train.index, "late_signal"].isna().all()


def test_the_row_level_phase_is_independent_of_the_split() -> None:
    frame = pd.concat([base_frame(12), base_frame(12).iloc[:3]], ignore_index=True)
    frame["consent_flag"] = [True] * 13 + [False, "no"]
    frame.loc[2, "converted"] = None
    blocks: dict[str, Any] = {
        "governance": {"consent_column": "consent_flag"},
        "prepare": {"deduplicate": True, "missing_values": "fill"},
    }
    wide = config_for(split={"validation_fraction": 0.1, "test_fraction": 0.1}, **blocks)
    narrow = config_for(split={"validation_fraction": 0.3, "test_fraction": 0.2}, **blocks)

    wide_rows, wide_plan = prepare_rows(frame, wide, primary_key="customer_id", target="converted")
    narrow_rows, narrow_plan = prepare_rows(frame, narrow, primary_key="customer_id", target="converted")

    pd.testing.assert_frame_equal(wide_rows, narrow_rows)
    assert wide_plan == narrow_plan
    # Nothing statistical is fitted before the split, and the phase cannot even see the run id.
    assert {transform.kind for transform in wide_plan.transforms} <= {
        "redact",
        "cast",
        "consent_filter",
        "dedupe",
    }
    parameters = inspect.signature(prepare_rows).parameters
    assert "run_id" not in parameters
    assert "fit_index" not in parameters


def test_the_row_level_phase_removes_twins_before_anything_can_separate_them() -> None:
    frame = pd.concat([base_frame(12), base_frame(12).iloc[:4]], ignore_index=True)
    config = config_for(prepare={"deduplicate": True})

    rows, plan = prepare_rows(frame, config, primary_key="customer_id", target="converted")

    assert frame.duplicated().sum() == 4
    assert not rows.duplicated().any()
    assert {removal.reason: removal.rows for removal in plan.row_removals}["duplicate"] == 4


def test_prepare_is_the_two_phases_with_every_row_used_as_a_fit_row() -> None:
    frame = skewed_frame()
    config = skew_config(missing_values="fill", outliers="clip")

    whole, whole_report = run(frame, config)
    rows, plan = prepare_rows(frame, config, primary_key="customer_id", target="converted")
    fitted, report = fit_transforms(rows, config, plan, run_id=RUN_ID)

    pd.testing.assert_frame_equal(whole, fitted.reset_index(drop=True))
    assert whole_report.model_dump(exclude={"prepared_at"}) == report.model_dump(exclude={"prepared_at"})


def test_fit_transforms_refuses_an_index_that_does_not_belong_to_the_frame() -> None:
    config = skew_config(missing_values="fill")
    rows, plan = prepare_rows(skewed_frame(), config, primary_key="customer_id", target="converted")

    with pytest.raises(ValueError, match="not in this frame"):
        fit_transforms(rows, config, plan, run_id=RUN_ID, fit_index=pd.Index([10_000, 10_001]))


def test_fit_transforms_refuses_an_empty_training_part() -> None:
    config = skew_config(missing_values="fill")
    rows, plan = prepare_rows(skewed_frame(), config, primary_key="customer_id", target="converted")

    with pytest.raises(ValueError, match="empty"):
        fit_transforms(rows, config, plan, run_id=RUN_ID, fit_index=pd.Index([]))


def test_a_train_fitted_report_replays_exactly() -> None:
    config = skew_config(missing_values="fill", outliers="clip")
    frame = skewed_frame()

    rows, prepared, report, _ = seam(frame, config)
    replayed = replay(rows, report)

    pd.testing.assert_frame_equal(replayed, prepared)


def test_the_report_of_a_train_fitted_run_still_counts_the_whole_file() -> None:
    config = skew_config(missing_values="fill", outliers="clip")
    frame = skewed_frame()

    rows, prepared, report, parts = seam(frame, config)

    assert report.run_id == RUN_ID
    assert report.rows_in == len(frame)
    assert report.rows_out == len(prepared) == len(rows)
    assert report.feature_columns == ("spend", "plan_type")
    assert [transform.order for transform in report.transforms] == list(range(1, len(report.transforms) + 1))
    assert sum(len(parts[name]) for name in ("train", "validation", "test")) == len(rows)
