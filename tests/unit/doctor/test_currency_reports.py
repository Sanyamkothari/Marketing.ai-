"""Plan O M242 (DEC-1812): reports, net value and costs show the run's currency, and the rate when one was used.

The value report (`roi_document`) and the Value Proof Pack (`proof_document`) take the run's money basis
(`report_money`). Without one - every run the Data Doctor flow did not prepare, and every rupee run with nothing
converted - each document is byte-identical to what it was.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from engine import __version__
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.config import ProblemType, RunMode
from engine.contracts import RunRecord, RunState
from engine.doctor.currency import (
    MoneyBasis,
    approve_conversion,
    money_basis,
    propose_conversion,
    report_money,
)
from engine.pilot.document import render_html
from engine.pilot.roi import RoiInputs, compute_roi, roi_document
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import (
    INCREMENTALITY_FILENAME,
    ConfidenceValue,
    IncrementalityReport,
    IncrementalityStatus,
)

RUN = "r_20260923_cc000001"
NOW = datetime(2026, 9, 23, tzinfo=UTC)
AT = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
INPUTS = RoiInputs(value_per_outcome=40.0, value_basis="a year of revenue", offer_cost=1.0, contact_cost=0.02)


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
    store.write_model(
        run_key(RUN, INCREMENTALITY_FILENAME),
        IncrementalityReport(
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
            absolute_lift=ConfidenceValue(value=0.06, ci_low=0.03, ci_high=0.09),
            relative_lift=0.06 / 0.74,
            incremental_conversions=ConfidenceValue(value=60.0, ci_low=30.0, ci_high=90.0),
            p_value=0.1,
            rows_immature=0,
            rows_without_outcome=0,
            rows_suppressed_or_untreated=0,
            causal=True,
            summary="measured",
            computed_at=NOW,
        ),
    )
    return store


def _dollars() -> MoneyBasis:
    steps = (
        RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="bill_amount", params={"unit": "USD"}),
    )
    return money_basis(steps, workspace_currency="INR")


def _converted() -> MoneyBasis:
    proposal = propose_conversion(
        "bill_amount",
        source_code="USD",
        target="INR",
        rate=83.1,
        rate_date="2026-10-10",
        source="RBI reference rate",
        today=date(2026, 10, 12),
    )
    approved = approve_conversion(proposal, sentence=proposal.sentence, approved_by="asha", clock=lambda: AT)
    return money_basis(proposal.steps, workspace_currency="INR", rates=[approved])


RATE_LINE = (
    "'bill_amount': every amount converted from US dollars to Indian rupees at 1 US dollar = 83.1 Indian rupees, the "
    "rate of 10 Oct 2026 (source: RBI reference rate), approved by asha on 12 Oct 2026."
)


APART_LINE = (
    "The value amounts in this report are worked out from your inputs, which are in rupees; the file's amounts are "
    "in US dollars. They are shown apart: nothing here is converted or added together across currencies."
)


def test_rupee_inputs_stay_rupees_beside_a_dollar_column(storage: LocalStorage) -> None:
    """Review blocker (DEC-1812 (h)): the value view's amounts are the person's rupee inputs times measured counts.
    A money column of the file in US dollars is noted under "Currency" - never used to relabel those rupees.
    """
    view = compute_roi(storage, RUN, inputs=INPUTS)
    before = render_html(roi_document(view, now=NOW))
    html = render_html(roi_document(view, now=NOW, money=report_money(_dollars())))
    assert "Value in rupees" in html
    assert "₹1,200 to ₹3,600 (most likely ₹2,400)" in html, "value of those customers, still in rupees"
    assert "Net value per rupee spent" in html
    assert "rupee values use the inputs shown" in html
    assert "$" not in html and "US dollar spent" not in html and "Value in US dollars" not in html
    assert ">Currency<" in html
    assert "&#x27;bill_amount&#x27; is in US dollars, as the file gives it; nothing was converted." in html
    assert APART_LINE.replace("'", "&#x27;") in html
    assert "Currency" not in before


def test_the_document_blocks_only_gain_the_currency_notes(storage: LocalStorage) -> None:
    view = compute_roi(storage, RUN, inputs=INPUTS)
    before = roi_document(view, now=NOW)
    after = roi_document(view, now=NOW, money=report_money(_dollars()))
    added = [b for b in after.blocks if b not in before.blocks]
    assert [b.model_dump()["kind"] for b in added] == ["heading", "bullets"]
    assert [b for b in after.blocks if b in before.blocks] == list(before.blocks)
    assert after.footer == before.footer


def test_an_approved_rate_is_shown_in_the_value_report(storage: LocalStorage) -> None:
    """Acceptance: the report that uses an approved conversion shows its rate, date, source and approver."""
    view = compute_roi(storage, RUN, inputs=INPUTS)
    html = render_html(roi_document(view, now=NOW, money=report_money(_converted())))
    assert "Value in rupees" in html and "₹1,200" in html
    assert ">Currency<" in html
    assert RATE_LINE.replace("'", "&#x27;") in html


def test_without_a_basis_the_value_report_is_byte_identical(storage: LocalStorage) -> None:
    """Acceptance: an INR workspace's report is exactly what it was - with no basis, and with a rupee basis that
    converted nothing."""
    view = compute_roi(storage, RUN, inputs=INPUTS)
    before = roi_document(view, now=NOW)
    rupees = money_basis(
        (RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="amount", params={"unit": "INR"}),),
        workspace_currency="INR",
    )
    assert report_money(rupees) is None
    assert roi_document(view, now=NOW, money=report_money(rupees)) == before
    assert roi_document(view, now=NOW, money=None).model_dump_json() == before.model_dump_json()


def test_an_amount_outcome_is_never_relabelled_by_another_column(storage: LocalStorage) -> None:
    """Review blocker: the outcome column's unit is not recorded (the target is not checked for currency), so its
    average is never labelled with the currency of another column."""
    view = compute_roi(storage, RUN, inputs=INPUTS).model_copy(
        update={"outcome_kind": "continuous", "treated_mean": 12.5, "control_mean": 10.25}
    )
    plain = roi_document(view, now=NOW)
    dollars = roi_document(view, now=NOW, money=report_money(_dollars()))
    assert [b for b in dollars.blocks if b in plain.blocks] == list(plain.blocks)
    html = render_html(dollars)
    assert "Average amount<" in html and "12.50" in html
    assert "Average amount (US dollars)" not in html and "$12.50" not in html


def test_the_proof_pack_shows_the_rate_its_campaign_used(tmp_path: Path) -> None:
    """Acceptance: the Value Proof Pack of a campaign whose run converted shows the rate under "Currency"."""
    from engine.pilot.proof import build_proof, proof_document
    from tests.unit.pilot.test_proof import _store

    stored = _store(tmp_path, 2_000)
    view = build_proof(stored.storage, stored.campaign_id)
    before = proof_document(view)
    assert proof_document(view, money=None) == before
    after = proof_document(view, money=report_money(_converted()))
    added = [b for b in after.blocks if b not in before.blocks]
    assert [b.model_dump()["kind"] for b in added] == ["heading", "bullets"]
    assert json.loads(added[1].model_dump_json())["items"] == [RATE_LINE]


def test_the_proof_pack_notes_a_dollar_column_apart_and_keeps_rupees(tmp_path: Path) -> None:
    from engine.pilot.proof import build_proof, proof_document
    from tests.unit.pilot.test_proof import _store

    stored = _store(tmp_path, 2_000)
    view = build_proof(stored.storage, stored.campaign_id)
    before = proof_document(view)
    after = proof_document(view, money=report_money(_dollars()))
    added = [b for b in after.blocks if b not in before.blocks]
    assert [b.model_dump()["kind"] for b in added] == ["heading", "bullets"]
    assert json.loads(added[1].model_dump_json())["items"][-1] == APART_LINE
    assert "$" not in render_html(after)
