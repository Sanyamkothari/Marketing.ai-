"""Plan J M109: a verdict that says "no clear effect" or "not over yet" uses the planner's words.

The planner (`engine.measurement.planner`, M93) calls the smallest change a test of a given size can
show its *detectable effect*, and a result read before the planned date an *early look*. The verdict a
campaign page reads (`engine.uplift.measure.campaign_verdict`, and its amount twin
`engine.measurement.measure.amount_verdict`) used to say "a bigger campaign may show one" and nothing
about the earlier reading, so the verdict, the plan card and the Output page spoke three ways.

Only the sentences changed: every headline, kind and number is what it was, and each sentence is plain
(`engine.pilot.plain.jargon_in` finds nothing in it).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from engine.measurement.measure import amount_verdict
from engine.pilot.plain import jargon_in
from engine.uplift.contracts import ConfidenceValue, IncrementalityReport, IncrementalityStatus
from engine.uplift.measure import VerdictKind, campaign_verdict


def _report(**fields: object) -> IncrementalityReport:
    base: dict[str, object] = {
        "run_id": "r1",
        "outcome_column": "converted",
        "outcome_window_days": 30,
        "as_of": datetime(2026, 9, 1, tzinfo=UTC),
        "status": IncrementalityStatus.MATURE,
        "results_available_on": None,
        "treated_rows": 9000,
        "treated_conversions": 1080,
        "treated_rate": 0.12,
        "control_rows": 1000,
        "control_conversions": 100,
        "control_rate": 0.10,
        "absolute_lift": ConfidenceValue(value=0.005, ci_low=-0.01, ci_high=0.02),
        "relative_lift": 0.05,
        "incremental_conversions": ConfidenceValue(value=45.0, ci_low=-90.0, ci_high=180.0),
        "p_value": 0.4,
        "rows_immature": 0,
        "rows_without_outcome": 0,
        "rows_suppressed_or_untreated": 0,
        "causal": True,
        "summary": "x",
        "computed_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    return IncrementalityReport.model_validate({**base, **fields})


def test_no_clear_effect_names_the_detectable_effect_the_planner_names() -> None:
    verdict = campaign_verdict(_report(), outcome_is_good=True)
    assert verdict.kind is VerdictKind.NO_CLEAR_EFFECT
    assert verdict.headline == "No clear effect yet"  # the headline is pinned and unchanged
    assert "detectable effect" in verdict.detail
    assert "smallest change" in verdict.detail  # said once in plain words, where the term first appears
    assert "could be chance" in verdict.detail
    assert verdict.amount is None and verdict.likely_low is None


def test_an_outcome_window_not_over_is_called_an_early_look_in_the_detail() -> None:
    early = _report(
        status=IncrementalityStatus.IMMATURE,
        results_available_on=date(2026, 7, 30),
        treated_rate=None,
        control_rate=None,
        absolute_lift=None,
        incremental_conversions=None,
        relative_lift=None,
        p_value=None,
        rows_immature=10_000,
    )
    verdict = campaign_verdict(early, outcome_is_good=True)
    assert verdict.kind is VerdictKind.TOO_EARLY and verdict.headline == "Outcome window not over yet"
    assert "early look" in verdict.detail
    assert verdict.detail.endswith("on or after 30 Jul 2026.")  # the date still closes the sentence
    undated = campaign_verdict(early.model_copy(update={"results_available_on": None}), outcome_is_good=True)
    assert "early look" in undated.detail and undated.detail.endswith("once it is over.")


def test_the_amount_verdict_says_it_the_same_way() -> None:
    flat = _report(
        outcome_kind="continuous",
        treated_mean=10.0,
        control_mean=9.9,
        mean_difference=0.1,
        mean_difference_ci=ConfidenceValue(value=0.1, ci_low=-0.5, ci_high=0.7),
        treated_rate=None,
        control_rate=None,
    )
    verdict = amount_verdict(flat, outcome_is_good=True)
    assert verdict.kind is VerdictKind.NO_CLEAR_EFFECT and verdict.headline == "No clear effect yet"
    assert "detectable effect" in verdict.detail and "smallest change" in verdict.detail
    assert "about the same amount" in verdict.detail


def test_every_verdict_sentence_is_plain_language() -> None:
    reports = [
        _report(),
        _report(causal=False),
        _report(
            status=IncrementalityStatus.IMMATURE,
            results_available_on=date(2026, 7, 30),
            treated_rate=None,
            control_rate=None,
            absolute_lift=None,
            incremental_conversions=None,
            relative_lift=None,
            p_value=None,
        ),
        _report(treated_rows=0, control_rows=0, treated_rate=None, control_rate=None),
    ]
    for report in reports:
        verdict = campaign_verdict(report, outcome_is_good=True)
        for text in (verdict.headline, verdict.detail):
            assert not jargon_in(text), (text, jargon_in(text))


def test_a_clear_result_reads_exactly_as_before() -> None:
    clear = _report(
        absolute_lift=ConfidenceValue(value=0.02, ci_low=0.001, ci_high=0.039),
        incremental_conversions=ConfidenceValue(value=180.0, ci_low=9.0, ci_high=351.0),
    )
    verdict = campaign_verdict(clear, outcome_is_good=True, outcome_label="Bought within 30 days")
    assert verdict.headline == "The campaign added about 180 conversions"
    assert verdict.detail == "Likely between 9 and 351. Counted: Bought within 30 days."


def test_the_campaign_page_path_carries_the_new_wording_and_no_verdict_for_an_early_look() -> None:
    """`campaign_verdict_for` is what `GET /campaigns/{id}` calls: the same sentences, and none for an early look."""
    from engine.measurement.measure import campaign_verdict_for

    verdict = campaign_verdict_for(_report(), outcome_is_good=True)
    assert verdict is not None and "detectable effect" in verdict.detail
    assert campaign_verdict_for(_report(early_look=True), outcome_is_good=True) is None
