"""The value view (Plan E M62): measured counts times the client's values, always as a range."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from engine import __version__
from engine.config import ProblemType, RunMode
from engine.contracts import RunRecord, RunState
from engine.pilot.document import render_html
from engine.pilot.roi import (
    RoiInputs,
    compute_roi,
    format_inr,
    load_roi_inputs,
    roi_document,
    save_roi_inputs,
)
from engine.runs import RUN_FILENAME
from engine.scheduling.outcomes import GroupOutcome, IncrementalityInput, OutcomeWindow
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import (
    INCREMENTALITY_FILENAME,
    ConfidenceValue,
    IncrementalityReport,
    IncrementalityStatus,
)
from engine.uplift.incrementality import newcombe_interval

RUN = "r_20260923_aa000001"
NOW = datetime(2026, 9, 23, tzinfo=UTC)
INPUTS = RoiInputs(
    value_per_outcome=4000.0, value_basis="12 months of revenue", offer_cost=100.0, contact_cost=2.0
)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    store = LocalStorage(tmp_path)
    store.write_model(
        run_key(RUN, RUN_FILENAME),
        RunRecord(
            run_id=RUN,
            use_case_id="telco-churn",
            use_case_name="Telco Customer Churn",
            mode=RunMode.SCORE,
            state=RunState.DONE,
            created_at=NOW - timedelta(days=90),
            finished_at=NOW - timedelta(days=90),
            file_name="scores.csv",
            primary_key="customer_id",
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            model_choice="auto",
            engine_version=__version__,
        ),
    )
    return store


def mature_report(*, low: float = 30.0, high: float = 90.0) -> IncrementalityReport:
    return IncrementalityReport(
        run_id=RUN,
        outcome_column="retained_60d",
        outcome_window_days=60,
        as_of=NOW,
        status=IncrementalityStatus.MATURE,
        results_available_on=None,
        treated_rows=1000,
        treated_conversions=800,
        treated_rate=0.8,
        control_rows=100,
        control_conversions=74,
        control_rate=0.74,
        absolute_lift=ConfidenceValue(value=0.06, ci_low=low / 1000, ci_high=high / 1000),
        relative_lift=0.06 / 0.74,
        incremental_conversions=ConfidenceValue(value=60.0, ci_low=low, ci_high=high),
        p_value=0.1,
        rows_immature=0,
        rows_without_outcome=0,
        rows_suppressed_or_untreated=0,
        causal=True,
        summary="measured",
        computed_at=NOW,
    )


def test_value_is_a_range_from_the_intervals_two_ends(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report())
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.status == "measured" and view.source == "incrementality_report"
    assert view.gross_value is not None
    assert (view.gross_value.low, view.gross_value.value, view.gross_value.high) == (
        120_000,
        240_000,
        360_000,
    )
    assert view.contact_cost_total == 2_000 and view.offer_cost_total == 80_000
    assert view.net_value is not None and view.net_value.low == 38_000 and view.net_value.high == 278_000
    assert view.roi is not None and view.roi.value == pytest.approx(158_000 / 82_000)


def test_without_inputs_the_count_is_shown_and_no_rupee_is_invented(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report())
    view = compute_roi(storage, RUN)
    assert view.incremental is not None and view.gross_value is None and view.net_value is None
    html = render_html(roi_document(view, now=NOW))
    assert "Enter your values" in html and "₹" not in html


def test_an_interval_that_includes_zero_is_called_that(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report(low=-10.0, high=90.0))
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert "includes zero" in view.summary


def test_an_immature_campaign_shows_the_date_results_will_be_ready(storage: LocalStorage) -> None:
    ready = date(2026, 11, 1)
    report = mature_report().model_copy(
        update={
            "status": IncrementalityStatus.IMMATURE,
            "results_available_on": ready,
            "incremental_conversions": None,
            "absolute_lift": None,
        }
    )
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), report)
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.status == "not_mature" and view.results_available_on == ready
    assert "2026-11-01" in render_html(roi_document(view, now=NOW))


def test_outcome_ingestion_takes_its_interval_from_the_same_function(storage: LocalStorage) -> None:
    window = OutcomeWindow(
        horizon_days=60,
        horizon_source="use_case_label",
        anchor=NOW - timedelta(days=90),
        anchor_source="scored_at",
        matures_at=NOW - timedelta(days=30),
    )
    storage.write_model(
        run_key(RUN, "incrementality_input.json"),
        IncrementalityInput(
            run_id=RUN,
            use_case_id="telco-churn",
            client_id=None,
            model_version_id="m_telco-churn_1",
            outcome_name="retained_60d",
            outcome_kind="binary",
            window=window,
            control_group_fraction=0.1,
            treated=GroupOutcome(rows=900, positives=720, outcome_rate=0.8, outcome_mean=None),
            control=GroupOutcome(rows=100, positives=70, outcome_rate=0.7, outcome_mean=None),
            by_band=(),
            observed_difference=0.1,
            suppressed_rows_excluded=0,
            unmatched_scored_rows=0,
            created_at=NOW,
        ),
    )
    view = compute_roi(storage, RUN, inputs=INPUTS)
    difference, low, high = newcombe_interval(720, 900, 70, 100)
    assert view.source == "outcome_ingestion"
    assert view.incremental is not None
    assert view.incremental.value == pytest.approx(difference * 900)
    assert (view.incremental.low, view.incremental.high) == (
        pytest.approx(low * 900),
        pytest.approx(high * 900),
    )


def test_with_no_measurement_the_view_says_when_one_can_be_made(
    storage: LocalStorage, config_root: Path
) -> None:
    view = compute_roi(storage, RUN, root=config_root)
    assert view.status == "not_measured" and view.incremental is None
    assert view.results_available_on == (NOW - timedelta(days=90) + timedelta(days=60)).date()


def test_inputs_are_stored_with_the_run_and_stamped(storage: LocalStorage) -> None:
    saved = save_roi_inputs(storage, RUN, INPUTS)
    assert saved.entered_at is not None
    assert load_roi_inputs(storage, RUN) == saved
    assert storage.exists(run_key(RUN, "pilot_roi_inputs.json"))


def test_the_report_says_the_value_depends_on_the_inputs(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report())
    html = render_html(roi_document(compute_roi(storage, RUN, inputs=INPUTS), now=NOW))
    assert "The value depends on these inputs" in html
    assert "₹1,20,000" in html and "₹3,60,000" in html


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (0, "₹0"),
        (999, "₹999"),
        (12_345, "₹12,345"),
        (1_234_567, "₹12,34,567 (12.35 lakh)"),
        (-5_000, "-₹5,000"),
        (25_000_000, "₹2,50,00,000 (2.50 crore)"),
    ],
)
def test_rupees_are_grouped_the_indian_way(amount: float, text: str) -> None:
    assert format_inr(amount) == text


def test_an_outcome_to_prevent_counts_fewer_outcomes_as_the_gain(storage: LocalStorage) -> None:
    """Churn: the campaign's gain is the leavers it prevented, and offers go to those who stayed."""
    report = mature_report().model_copy(
        update={"incremental_conversions": ConfidenceValue(value=-60.0, ci_low=-90.0, ci_high=-30.0)}
    )
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), report)
    view = compute_roi(storage, RUN, inputs=INPUTS.model_copy(update={"outcome_is_good": False}))
    assert view.benefit is not None and (view.benefit.low, view.benefit.value, view.benefit.high) == (
        30.0,
        60.0,
        90.0,
    )
    assert view.offer_cost_total == (1000 - 800) * 100.0
    assert "worked" in view.summary and view.benefit_label == "Customers kept by the campaign"


def test_with_no_inputs_a_churn_campaign_is_read_the_right_way_round(
    storage: LocalStorage, config_root: Path
) -> None:
    """telco-churn is measured on its own label, an outcome to prevent: fewer leavers is the gain."""
    report = mature_report().model_copy(
        update={
            "outcome_column": "churn_next_60d",
            "incremental_conversions": ConfidenceValue(value=-60.0, ci_low=-90.0, ci_high=-30.0),
        }
    )
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), report)
    view = compute_roi(storage, RUN, root=config_root)
    assert view.outcome_is_good is False
    assert view.benefit is not None and view.benefit.low == 30.0 and "worked" in view.summary


def test_a_measured_ingestion_wins_over_an_immature_report(storage: LocalStorage) -> None:
    immature = mature_report().model_copy(
        update={
            "status": IncrementalityStatus.IMMATURE,
            "results_available_on": date(2026, 9, 1),
            "incremental_conversions": None,
            "absolute_lift": None,
        }
    )
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), immature)
    window = OutcomeWindow(
        horizon_days=60,
        horizon_source="use_case_label",
        anchor=NOW - timedelta(days=90),
        anchor_source="scored_at",
        matures_at=NOW - timedelta(days=30),
    )
    storage.write_model(
        run_key(RUN, "incrementality_input.json"),
        IncrementalityInput(
            run_id=RUN,
            use_case_id="telco-churn",
            client_id=None,
            model_version_id="m_telco-churn_1",
            outcome_name="retained_60d",
            outcome_kind="binary",
            window=window,
            control_group_fraction=0.1,
            treated=GroupOutcome(rows=900, positives=720, outcome_rate=0.8, outcome_mean=None),
            control=GroupOutcome(rows=100, positives=70, outcome_rate=0.7, outcome_mean=None),
            by_band=(),
            observed_difference=0.1,
            suppressed_rows_excluded=0,
            unmatched_scored_rows=0,
            created_at=NOW,
        ),
    )
    view = compute_roi(storage, RUN, inputs=INPUTS)
    assert view.status == "measured" and view.source == "outcome_ingestion"
