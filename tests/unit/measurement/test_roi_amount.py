"""Plan J M102 (DEC-1312): the value view of a campaign measured on an amount.

Before M102 a continuous outcome read "No outcomes have been recorded for this campaign yet" in
`engine.pilot.roi`. Now a report measured on revenue (through `measure_incrementality`, on simulated
campaigns) is priced in rupees from its per-customer difference times the contacted customers - the
adjusted estimate when one was measured - and anything that cannot be priced is null with a plain reason.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest

from engine.measurement.simulate import revenue_campaign
from engine.pilot.document import render_html
from engine.pilot.plain import jargon_in
from engine.pilot.roi import RoiInputs, compute_roi, roi_document
from engine.scheduling.outcomes import GroupOutcome, IncrementalityInput, OutcomeWindow
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import INCREMENTALITY_FILENAME, IncrementalityReport
from engine.uplift.incrementality import measure_incrementality
from tests.unit.pilot.test_roi import NOW, RUN, storage

_ = storage  # the pilot tests' run fixture: a finished telco-churn scoring run

REVENUE = RoiInputs(value_per_outcome=1.0, value_basis="revenue in rupees", contact_cost=2.0)


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _report(*, adjusted: bool, seed: int = 31, effect: float = 6.0) -> IncrementalityReport:
    campaign = revenue_campaign(20_000, effect, seed=seed, rho=0.6)
    kwargs = campaign.adjusted_kwargs if adjusted else campaign.measure_kwargs
    return measure_incrementality(campaign.scores, campaign.outcomes, **{**kwargs, "run_id": RUN})


def test_a_revenue_report_is_priced_in_rupees(storage: LocalStorage) -> None:
    report = _report(adjusted=False)
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), report)
    view = compute_roi(storage, RUN, inputs=REVENUE)
    assert report.mean_difference_ci is not None
    rows = report.treated_rows
    assert view.status == "measured" and view.outcome_kind == "continuous" and view.adjusted is False
    assert view.incremental is not None
    assert view.incremental.value == pytest.approx(report.mean_difference_ci.value * rows)
    assert view.incremental.low == pytest.approx(report.mean_difference_ci.ci_low * rows)  # type: ignore[operator]
    assert view.gross_value is not None and view.gross_value.value == pytest.approx(view.incremental.value)
    assert view.contact_cost_total == pytest.approx(2.0 * rows)
    assert view.net_value is not None and view.net_value.value == pytest.approx(
        view.incremental.value - 2.0 * rows
    )
    assert (view.treated_mean, view.control_mean) == (report.treated_mean, report.control_mean)
    assert view.value_note is not None and not jargon_in(view.value_note)
    html = render_html(roi_document(view, now=NOW))
    assert "Average amount" in html and "Value of one unit of the amount" in html


def test_the_adjusted_estimate_is_priced_when_it_was_measured(storage: LocalStorage) -> None:
    report = _report(adjusted=True)
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), report)
    view = compute_roi(storage, RUN, inputs=REVENUE)
    assert report.adjusted_interval is not None and view.adjusted is True
    assert view.incremental is not None
    assert view.incremental.value == pytest.approx(report.adjusted_interval.value * report.treated_rows)
    assert view.value_note is not None and "adjusted" in view.value_note


def test_without_values_nothing_is_put_in_rupees(storage: LocalStorage) -> None:
    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), _report(adjusted=False))
    view = compute_roi(storage, RUN)
    assert view.status == "measured" and view.incremental is not None
    assert view.gross_value is None and view.net_value is None and view.roi is None
    assert view.value_note is not None and view.value_note.startswith("Enter what one unit")


def test_outcomes_ingested_as_amounts_say_why_there_is_no_range(storage: LocalStorage) -> None:
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
            outcome_name="revenue_60d",
            outcome_kind="continuous",
            window=window,
            control_group_fraction=0.1,
            treated=GroupOutcome(rows=900, positives=None, outcome_rate=None, outcome_mean=410.0),
            control=GroupOutcome(rows=100, positives=None, outcome_rate=None, outcome_mean=380.0),
            by_band=(),
            observed_difference=30.0,
            suppressed_rows_excluded=0,
            unmatched_scored_rows=0,
            created_at=NOW,
        ),
    )
    view = compute_roi(storage, RUN, inputs=REVENUE)
    assert view.status == "not_measured" and view.source == "outcome_ingestion"
    assert view.outcome_kind == "continuous" and view.incremental is None and view.gross_value is None
    assert "No outcomes have been recorded" not in view.summary
    assert "no range can be given" in view.summary and not jargon_in(view.summary)


def test_a_yes_no_view_has_no_new_key(storage: LocalStorage) -> None:
    from tests.unit.pilot.test_roi import INPUTS, mature_report

    storage.write_model(run_key(RUN, INCREMENTALITY_FILENAME), mature_report(low=30.0, high=90.0))
    payload = compute_roi(storage, RUN, inputs=INPUTS).model_dump(mode="json")
    assert not {"outcome_kind", "treated_mean", "control_mean", "adjusted", "value_note"} & set(payload)
