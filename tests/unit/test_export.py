"""The export stage: `scores.csv`, `scores.parquet` and `scoring_summary.json`.

Plan section 6.3 `export`. Everything goes through `LocalStorage`, so the files are read back the
way the API will read them, and the KPI is exercised in all three forms of the DEC-007 grammar.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.config import UseCaseConfig, load_use_case, load_use_case_document
from engine.contracts import Direction, DriftReport, DriftStatus, Reason, ScoringSummary, scores_csv_columns
from engine.stages.actions import (
    ACTION_COLUMN,
    BAND_COLUMN,
    CONTROL_GROUP_COLUMN,
    SUPPRESSED_REASON_COLUMN,
    apply_actions,
)
from engine.stages.export import SCORES_CSV, SCORES_PARQUET, summarise, write_scores
from engine.storage import LocalStorage

RUN_ID = "r_20260921_aaaaaaaa"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
MODEL_ID = "m_targeted-advertisement_1"
MODEL_NAME = "XGBoost"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def use_case(**patches: object) -> UseCaseConfig:
    """`targeted-advertisement` with dotted paths overridden; the template is emptied first."""
    document = copy.deepcopy(load_use_case_document("targeted-advertisement"))
    document["template"] = {"columns": []}
    for dotted, value in patches.items():
        parts = dotted.split("__")
        node = document
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return UseCaseConfig.model_validate(document)


def reason(feature: str, text: str) -> Reason:
    return Reason(feature=feature, value="12", contribution=0.31, direction=Direction.UP, text=text)


def scored_frame(rows: int = 12, *, reasons: bool = True) -> pd.DataFrame:
    """A frame shaped like the predict stage's output, with reasons attached."""
    frame = pd.DataFrame(
        {
            "customer_id": [f"C-{index:05d}" for index in range(rows)],
            "propensity": [round((index * 7 % 100) / 100, 2) for index in range(rows)],
            "marketing_opt_in": [index % 5 != 0 for index in range(rows)],
            "tenure_months": [float(index * 3) for index in range(rows)],
        }
    )
    if reasons:
        frame["reason_1"] = [
            reason("visits_last_7d", f"visits_last_7d up ({index})") for index in range(rows)
        ]
        frame["reason_2"] = [
            None if index % 3 == 0 else reason("ad_ctr_90d", "ad_ctr_90d up (4.1%)") for index in range(rows)
        ]
    return frame


def acted(config: UseCaseConfig, frame: pd.DataFrame | None = None) -> pd.DataFrame:
    return apply_actions(
        scored_frame() if frame is None else frame,
        config,
        run_id=RUN_ID,
        primary_key="customer_id",
        now=NOW,
    )


@pytest.fixture(scope="module")
def config() -> UseCaseConfig:
    return load_use_case("targeted-advertisement")


@pytest.fixture()
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path)


def export(config: UseCaseConfig, storage: LocalStorage, frame: pd.DataFrame) -> dict[str, str]:
    return write_scores(frame, config, run_id=RUN_ID, primary_key="customer_id", storage=storage)


def summary_of(
    config: UseCaseConfig,
    frame: pd.DataFrame,
    *,
    files: dict[str, str] | None = None,
    drift: DriftReport | None = None,
    kpi_source: pd.DataFrame | None = None,
) -> ScoringSummary:
    return summarise(
        frame,
        config,
        run_id=RUN_ID,
        model_version_id=MODEL_ID,
        model_display_name=MODEL_NAME,
        primary_key="customer_id",
        drift=drift,
        files={} if files is None else files,
        kpi_source=kpi_source,
        scored_at=NOW,
    )


# ---------------------------------------------------------------------------
# The exported files
# ---------------------------------------------------------------------------
def test_both_files_are_written_under_the_run_directory(config, storage) -> None:
    files = export(config, storage, acted(config))
    assert files == {
        SCORES_CSV: f"runs/{RUN_ID}/scores.csv",
        SCORES_PARQUET: f"runs/{RUN_ID}/scores.parquet",
    }
    assert storage.exists(files[SCORES_CSV])
    assert storage.exists(files[SCORES_PARQUET])


def test_the_csv_header_is_exactly_the_contract(config, storage) -> None:
    files = export(config, storage, acted(config))
    header = storage.read_text(files[SCORES_CSV]).splitlines()[0]
    assert header.split(",") == list(scores_csv_columns(config, "customer_id"))
    assert header.split(",") == [
        "customer_id",
        "propensity",
        "band",
        "action",
        "reason_1",
        "reason_2",
        "reason_3",
        "suppressed_reason",
        "control_group",
    ]


def test_the_csv_is_utf8_without_a_bom_and_uses_newline_endings(config, storage) -> None:
    frame = scored_frame()
    frame["customer_id"] = [f"Ç-{index:05d}" for index in range(len(frame))]
    files = export(config, storage, acted(config, frame))
    raw = storage.read_bytes(files[SCORES_CSV])
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert "Ç-00000" in raw.decode("utf-8")


def test_the_csv_round_trips(config, storage) -> None:
    result = acted(config)
    files = export(config, storage, result)
    back = pd.read_csv(storage.local_path(files[SCORES_CSV]))
    assert list(back.columns) == list(scores_csv_columns(config, "customer_id"))
    assert list(back["customer_id"]) == list(result["customer_id"])
    assert list(back["propensity"]) == list(result["propensity"])
    assert list(back[BAND_COLUMN]) == list(result[BAND_COLUMN])
    assert list(back[ACTION_COLUMN]) == list(result[ACTION_COLUMN])
    assert back[CONTROL_GROUP_COLUMN].dtype == bool
    assert list(back[CONTROL_GROUP_COLUMN]) == list(result[CONTROL_GROUP_COLUMN])


def test_the_parquet_round_trips_with_the_same_header_and_types(config, storage) -> None:
    result = acted(config)
    files = export(config, storage, result)
    back = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    assert list(back.columns) == list(scores_csv_columns(config, "customer_id"))
    assert back[CONTROL_GROUP_COLUMN].dtype == bool
    assert list(back["propensity"]) == list(result["propensity"])
    assert list(back[BAND_COLUMN]) == list(result[BAND_COLUMN])


def test_the_two_files_hold_the_same_table(config, storage) -> None:
    files = export(config, storage, acted(config))
    from_csv = pd.read_csv(storage.local_path(files[SCORES_CSV]))
    from_parquet = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    for column in scores_csv_columns(config, "customer_id"):
        assert list(from_csv[column].fillna("")) == list(from_parquet[column].fillna(""))


def test_only_the_contract_columns_are_exported(config, storage) -> None:
    files = export(config, storage, acted(config))
    back = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    assert "marketing_opt_in" not in back.columns
    assert "tenure_months" not in back.columns


# ---------------------------------------------------------------------------
# Reason columns
# ---------------------------------------------------------------------------
def test_a_reason_column_exports_the_ready_to_render_sentence(config, storage) -> None:
    files = export(config, storage, acted(config))
    back = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    assert back["reason_1"].iat[0] == "visits_last_7d up (0)"
    assert back["reason_2"].iat[0] is None  # this row has only one reason
    assert back["reason_2"].iat[1] == "ad_ctr_90d up (4.1%)"


def test_a_reason_column_the_explain_stage_never_wrote_is_empty_not_invented(config, storage) -> None:
    files = export(config, storage, acted(config))
    back = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    assert "reason_3" in back.columns
    assert back["reason_3"].isna().all()


def test_a_reason_may_arrive_as_a_mapping(config, storage) -> None:
    frame = scored_frame(reasons=False)
    frame["reason_1"] = [
        {
            "feature": "plan_tier",
            "value": "basic",
            "contribution": -0.1,
            "direction": "down",
            "text": "plan_tier = basic",
        }
    ] * len(frame)
    files = export(config, storage, acted(config, frame))
    back = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    assert set(back["reason_1"]) == {"plan_tier = basic"}


def test_a_reason_cell_that_is_neither_a_reason_nor_a_null_is_refused(config, storage) -> None:
    frame = scored_frame(reasons=False)
    frame["reason_1"] = ["just a sentence"] * len(frame)
    with pytest.raises(ValueError, match="reason column holds Reason objects"):
        export(config, storage, acted(config, frame))


def test_the_explain_stage_fills_the_columns_this_stage_reads(config, storage) -> None:
    # Blocker 2: nothing used to produce reason_1..reason_n. The adapter in `explain` is the one
    # producer, and the whole join runs here - explanations in one order, scored rows in another.
    from engine.contracts import RowExplanation
    from engine.stages.explain import with_reason_columns

    scored = acted(config, scored_frame(rows=6, reasons=False))
    keys = list(scored["customer_id"])
    explanations = [
        RowExplanation(
            primary_key=key,
            score=0.5,
            reasons=(
                Reason(
                    feature="visits_last_7d",
                    value="12",
                    contribution=0.4,
                    direction=Direction.UP,
                    text=f"visits_last_7d up ({position})",
                ),
            ),
        )
        for position, key in reversed(list(enumerate(keys)))
    ]
    merged = with_reason_columns(explanations, scored, config, primary_key="customer_id")

    files = export(config, storage, merged)
    back = pd.read_csv(storage.local_path(files[SCORES_CSV]))
    assert list(back["reason_1"]) == [f"visits_last_7d up ({position})" for position in range(6)]
    assert back["reason_2"].isna().all(), "a row with one reason leaves the other slots empty"

    summary = summary_of(config, merged, files=files)
    assert summary.sample_rows[0].reasons[0].text == "visits_last_7d up (0)"
    assert summary.sample_rows[0].reasons[0].feature == "visits_last_7d"


def test_the_number_of_reason_columns_follows_the_configuration(storage) -> None:
    config = use_case(evaluation__reasons_per_row=1)
    files = export(config, storage, acted(config))
    header = storage.read_text(files[SCORES_CSV]).splitlines()[0]
    assert header.split(",").count("reason_1") == 1
    assert "reason_2" not in header


# ---------------------------------------------------------------------------
# Suppression in the files
# ---------------------------------------------------------------------------
def test_a_suppressed_row_keeps_its_score_and_carries_its_reason(config, storage) -> None:
    result = acted(config)
    files = export(config, storage, result)
    back = pd.read_parquet(storage.local_path(files[SCORES_PARQUET]))
    suppressed = back[back[SUPPRESSED_REASON_COLUMN].notna()]
    assert len(suppressed) == len(result[result[SUPPRESSED_REASON_COLUMN].notna()])
    assert set(suppressed[SUPPRESSED_REASON_COLUMN]) == {"opted_out"}
    assert suppressed["propensity"].notna().all()


def test_an_eligible_row_has_an_empty_suppression_cell(config, storage) -> None:
    files = export(config, storage, acted(config))
    raw = storage.read_text(files[SCORES_CSV]).splitlines()
    eligible = [line for line in raw[1:] if ",Suppressed," not in line]
    assert eligible
    for line in eligible:
        assert line.split(",")[-2] == ""


# ---------------------------------------------------------------------------
# The scoring summary
# ---------------------------------------------------------------------------
def test_the_summary_reports_what_the_caller_told_it(config) -> None:
    files = {SCORES_CSV: "runs/x/scores.csv"}
    summary = summary_of(config, acted(config), files=files)
    assert summary.run_id == RUN_ID
    assert summary.model_version_id == MODEL_ID
    assert summary.model_display_name == MODEL_NAME
    assert summary.score_field == "propensity"
    assert summary.files == files
    assert summary.scored_at == NOW


def test_the_band_counts_are_in_configured_order_and_sum_to_the_rows(config) -> None:
    result = acted(config)
    summary = summary_of(config, result)
    assert [band.name for band in summary.bands] == ["High", "Medium", "Low"]
    assert [band.action for band in summary.bands] == ["Serve ad", "Retarget", "Suppress"]
    assert [band.min_score for band in summary.bands] == [0.80, 0.50, 0.00]
    assert sum(band.rows for band in summary.bands) == summary.rows_scored == len(result)


def test_a_band_with_no_rows_is_still_reported(config) -> None:
    frame = scored_frame(rows=4)
    frame["propensity"] = [0.1, 0.2, 0.3, 0.4]
    summary = summary_of(config, acted(config, frame))
    high = next(band for band in summary.bands if band.name == "High")
    assert high.rows == 0
    assert high.share_pct == 0.0


def test_the_action_counts_are_largest_first_and_sum_to_the_rows(config) -> None:
    result = acted(config)
    summary = summary_of(config, result)
    assert [action.rows for action in summary.actions] == sorted(
        (action.rows for action in summary.actions), reverse=True
    )
    assert sum(action.rows for action in summary.actions) == summary.rows_scored


def test_shares_are_percentages_not_fractions(config) -> None:
    summary = summary_of(config, acted(config))
    assert round(sum(band.share_pct for band in summary.bands)) == 100
    assert round(sum(action.share_pct for action in summary.actions)) == 100


def test_the_score_statistics_are_rounded_for_the_page(config) -> None:
    frame = scored_frame(rows=4)
    frame["propensity"] = [0.0, 0.1, 0.2, 0.9]
    summary = summary_of(config, acted(config, frame))
    assert summary.score_mean == 0.3
    assert summary.score_median == 0.15


def test_the_suppression_counts_add_up_to_the_suppressed_rows(config) -> None:
    result = acted(config)
    summary = summary_of(config, result)
    assert [entry.reason for entry in summary.suppressed] == ["opted_out"]
    assert sum(entry.rows for entry in summary.suppressed) == int(
        result[SUPPRESSED_REASON_COLUMN].notna().sum()
    )


def test_a_rule_that_matched_nothing_is_reported_as_zero() -> None:
    config = use_case(actions__control_group_fraction=0.0)
    frame = scored_frame(rows=4)
    frame["marketing_opt_in"] = [True, True, True, True]
    frame["last_contacted_at"] = [None, None, None, None]
    summary = summary_of(config, acted(config, frame))
    assert {entry.reason: entry.rows for entry in summary.suppressed} == {
        "opted_out": 0,
        "recently_contacted": 0,
    }


def test_a_skipped_rule_has_no_entry_at_all() -> None:
    config = use_case(actions__control_group_fraction=0.0)
    frame = scored_frame(rows=4)  # no last_contacted_at column at all
    summary = summary_of(config, acted(config, frame))
    assert [entry.reason for entry in summary.suppressed] == ["opted_out"]


def test_the_control_group_is_counted(config) -> None:
    result = acted(config)
    summary = summary_of(config, result)
    assert summary.control_group_rows == int(result[CONTROL_GROUP_COLUMN].sum())


def test_drift_is_copied_into_the_summary_when_it_was_computed(config) -> None:
    drift = DriftReport(
        run_id=RUN_ID,
        baseline_run_id="r_20260901_00000000",
        model_version_id=MODEL_ID,
        threshold=0.20,
        features=(),
        max_psi=0.06,
        drifted_features=(),
        status=DriftStatus.STABLE,
        summary="PSI 0.06, stable",
        computed_at=NOW,
    )
    summary = summary_of(config, acted(config), drift=drift)
    assert summary.drift_status is DriftStatus.STABLE
    assert summary.drift_max_psi == 0.06
    assert summary.drift_summary == "PSI 0.06, stable"


def test_no_drift_leaves_the_three_drift_fields_empty(config) -> None:
    summary = summary_of(config, acted(config))
    assert summary.drift_status is None
    assert summary.drift_max_psi is None
    assert summary.drift_summary is None


# ---------------------------------------------------------------------------
# The Output page's sample table (prototype: key, score, band pill, top reason, action)
# ---------------------------------------------------------------------------
def test_the_sample_rows_carry_everything_the_output_table_renders(config) -> None:
    summary = summary_of(config, acted(config))
    first = summary.sample_rows[0]
    assert first.primary_key == "C-00000"
    assert first.score == 0.0
    assert first.band == "Low"
    assert first.action
    assert first.reasons[0].text == "visits_last_7d up (0)"
    assert first.reasons[0].direction is Direction.UP


def test_the_sample_is_the_first_ten_rows(config) -> None:
    result = acted(config, scored_frame(rows=25))
    summary = summary_of(config, result)
    assert len(summary.sample_rows) == 10
    assert [row.primary_key for row in summary.sample_rows] == list(result["customer_id"].head(10))


def test_a_short_file_gives_a_short_sample(config) -> None:
    summary = summary_of(config, acted(config, scored_frame(rows=3)))
    assert len(summary.sample_rows) == 3


def test_a_sample_row_shows_why_it_was_suppressed(config) -> None:
    summary = summary_of(config, acted(config))
    suppressed = [row for row in summary.sample_rows if row.action == "Suppressed"]
    assert suppressed
    assert all(row.suppressed_reason == "opted_out" for row in suppressed)
    assert all(row.score is not None for row in suppressed)


# ---------------------------------------------------------------------------
# The KPI grammar, evaluated
# ---------------------------------------------------------------------------
def test_count_rows_counts_every_scored_row_suppressed_included() -> None:
    config = use_case(output__kpi__formula="count_rows()", output__kpi__label="Customers scored")
    summary = summary_of(config, acted(config, scored_frame(rows=12)))
    assert summary.kpi.label == "Customers scored"
    assert summary.kpi.formula == "count_rows()"
    assert summary.kpi.value == 12.0
    assert summary.kpi.display == "12"


def test_count_rows_uses_the_humanised_display() -> None:
    config = use_case(output__kpi__formula="count_rows()")
    summary = summary_of(config, acted(config, scored_frame(rows=2500)))
    assert summary.kpi.value == 2500.0
    assert summary.kpi.display == "3K"


def test_count_where_band_in_counts_only_the_named_bands(config) -> None:
    result = acted(config)
    summary = summary_of(config, result)
    expected = int(result[BAND_COLUMN].isin(["High", "Medium"]).sum())
    assert summary.kpi.formula == 'count_where_band_in(["High","Medium"])'
    assert summary.kpi.value == float(expected)
    assert summary.kpi.display == str(expected)


def test_a_kpi_naming_a_band_with_no_rows_is_zero_not_missing() -> None:
    config = use_case(output__kpi__formula='count_where_band_in(["High"])')
    frame = scored_frame(rows=4)
    frame["propensity"] = [0.1, 0.2, 0.3, 0.4]
    summary = summary_of(config, acted(config, frame))
    assert summary.kpi.value == 0.0
    assert summary.kpi.display == "0"


def test_sum_where_band_in_totals_the_named_column() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium"])')
    uploaded = scored_frame()
    result = acted(config, uploaded.copy())
    summary = summary_of(config, result, kpi_source=uploaded)
    expected = float(result.loc[result[BAND_COLUMN].isin(["High", "Medium"]), "tenure_months"].sum())
    assert summary.kpi.value == round(expected, 2)


def test_sum_where_band_in_humanises_a_large_total() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium","Low"])')
    frame = scored_frame(rows=4)
    frame["tenure_months"] = [100_000.0, 42_000.0, 42_000.0, 0.0]
    summary = summary_of(config, acted(config, frame.copy()), kpi_source=frame)
    assert summary.kpi.value == 184000.0
    assert summary.kpi.display == "184K"


def test_sum_where_band_in_keeps_two_decimals_below_a_thousand() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium","Low"])')
    frame = scored_frame(rows=2)
    frame["tenure_months"] = [12.25, 0.5]
    summary = summary_of(config, acted(config, frame.copy()), kpi_source=frame)
    assert summary.kpi.value == 12.75
    assert summary.kpi.display == "12.75"


def test_sum_where_band_in_ignores_values_that_are_not_numbers() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium","Low"])')
    frame = scored_frame(rows=3)
    frame["tenure_months"] = [10.0, None, "not a number"]
    summary = summary_of(config, acted(config, frame.copy()), kpi_source=frame)
    assert summary.kpi.value == 10.0


def test_a_kpi_that_sums_a_column_the_file_does_not_have_is_refused() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("revenue_ltv", ["High"])')
    frame = scored_frame(rows=3)
    with pytest.raises(ValueError, match=re.escape("carry no 'revenue_ltv'")):
        summary_of(config, acted(config, frame.copy()), kpi_source=frame)


# ---------------------------------------------------------------------------
# A total is of the uploaded values, never of the replayed ones (finding 10)
# ---------------------------------------------------------------------------
def prepared(frame: pd.DataFrame, **replacements: list[object]) -> pd.DataFrame:
    """The frame as prepare left it: some columns clipped, some dropped altogether."""
    replayed = frame.copy()
    for column, values in replacements.items():
        if values:
            replayed[column] = values
        else:
            replayed = replayed.drop(columns=[column])
    return replayed


def test_a_total_over_a_clipped_feature_is_of_the_real_values() -> None:
    # tenure_months is a model feature, so prepare clipped it to the training percentiles. The
    # tile says "months of tenure", not "months of tenure after clipping", so the real ones count.
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium","Low"])')
    uploaded = scored_frame(rows=3)
    uploaded["tenure_months"] = [10.0, 20.0, 900.0]
    replayed = prepared(uploaded, tenure_months=[10.0, 20.0, 99.0])  # clipped at the 99th percentile
    summary = summary_of(config, acted(config, replayed), kpi_source=uploaded)
    assert summary.kpi.value == 930.0
    assert summary.kpi.value != 129.0, "the clipped total would be 129.0"


def test_a_total_over_a_column_prepare_dropped_still_has_an_answer() -> None:
    # The KPI column need not be a feature at all: a dropped one is gone from the scored frame,
    # which used to end the run in a ValueError at the very last stage.
    config = use_case(output__kpi__formula='sum_where_band_in("revenue_ltv", ["High","Medium","Low"])')
    uploaded = scored_frame(rows=3)
    uploaded["revenue_ltv"] = [1000.0, 250.5, 49.5]
    replayed = prepared(uploaded, revenue_ltv=[])
    assert "revenue_ltv" not in replayed.columns
    summary = summary_of(config, acted(config, replayed), kpi_source=uploaded)
    assert summary.kpi.value == 1300.0
    assert summary.kpi.display == "1K"


def test_a_total_joins_the_uploaded_rows_by_key_and_not_by_position() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High"])')
    uploaded = scored_frame(rows=6)
    uploaded["tenure_months"] = [float(index) for index in range(6)]
    uploaded["propensity"] = [0.99, 0.1, 0.1, 0.1, 0.1, 0.1]  # only the first row bands High
    replayed = prepared(uploaded, tenure_months=[0.0] * 6)
    shuffled = uploaded.iloc[[3, 5, 0, 4, 1, 2]]
    summary = summary_of(config, acted(config, replayed), kpi_source=shuffled)
    assert summary.bands[0].rows == 1
    assert summary.kpi.value == 0.0, "row C-00000 carries tenure 0.0 whatever order the rows arrive in"

    uploaded["tenure_months"] = [7.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    summary = summary_of(config, acted(config, replayed), kpi_source=uploaded.iloc[::-1])
    assert summary.kpi.value == 7.0


def test_a_total_without_the_uploaded_rows_is_refused_rather_than_guessed() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium"])')
    frame = scored_frame(rows=3)
    with pytest.raises(ValueError, match="kpi_source"):
        summary_of(config, acted(config, frame))


def test_the_counting_kpis_need_no_uploaded_rows() -> None:
    for formula in ("count_rows()", 'count_where_band_in(["High","Medium"])'):
        config = use_case(output__kpi__formula=formula)
        summary = summary_of(config, acted(config, scored_frame(rows=5)))
        assert summary.kpi.formula == formula


def test_a_scored_row_the_uploaded_rows_do_not_have_is_refused() -> None:
    # A short total is a wrong headline number; the run says which row it could not find.
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium","Low"])')
    uploaded = scored_frame(rows=4)
    with pytest.raises(ValueError, match="not in the uploaded rows"):
        summary_of(config, acted(config, uploaded.copy()), kpi_source=uploaded.head(2))


def test_uploaded_rows_with_the_same_key_twice_are_refused() -> None:
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High","Medium","Low"])')
    uploaded = scored_frame(rows=3)
    doubled = pd.concat([uploaded, uploaded.head(1)], ignore_index=True)
    with pytest.raises(ValueError, match="more than once"):
        summary_of(config, acted(config, uploaded.copy()), kpi_source=doubled)


def test_a_row_outside_the_counted_bands_need_not_be_in_the_uploaded_rows() -> None:
    # Only the rows the formula totals have to be found, so a KPI over one band does not fail
    # because of a row it never counts.
    config = use_case(output__kpi__formula='sum_where_band_in("tenure_months", ["High"])')
    uploaded = scored_frame(rows=3)
    uploaded["propensity"] = [0.99, 0.01, 0.01]
    uploaded["tenure_months"] = [25.0, 1.0, 2.0]
    summary = summary_of(config, acted(config, uploaded.copy()), kpi_source=uploaded.head(1))
    assert summary.kpi.value == 25.0


def test_the_kpi_evaluator_does_not_execute_the_formula() -> None:
    import engine.stages.export as module

    source = module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "eval(" not in text
    assert "exec(" not in text


# ---------------------------------------------------------------------------
# Determinism and refusals
# ---------------------------------------------------------------------------
def test_the_whole_export_is_byte_identical_across_two_runs(config, tmp_path) -> None:
    first_store = LocalStorage(tmp_path / "first")
    second_store = LocalStorage(tmp_path / "second")
    first_files = export(config, first_store, acted(config))
    second_files = export(config, second_store, acted(config))
    assert first_files == second_files
    for name in (SCORES_CSV, SCORES_PARQUET):
        assert first_store.read_bytes(first_files[name]) == second_store.read_bytes(second_files[name])


def test_the_summary_is_identical_across_two_runs(config) -> None:
    first = summary_of(config, acted(config))
    second = summary_of(config, acted(config))
    assert first == second


def test_a_frame_that_never_saw_the_actions_stage_is_refused(config, storage) -> None:
    with pytest.raises(ValueError, match=re.escape("run engine.stages.actions.apply_actions")):
        export(config, storage, scored_frame())


def test_a_summary_of_a_frame_without_the_action_columns_is_refused(config) -> None:
    with pytest.raises(ValueError, match=re.escape("run engine.stages.actions.apply_actions")):
        summary_of(config, scored_frame())
