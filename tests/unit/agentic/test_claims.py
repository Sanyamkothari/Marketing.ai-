"""The claim verifier and renderer (Plan K M139, design §4): one test per claim type and per failure code.

The evidence index here is what the planner builds from tool results (`engine.agentic.planner`); the
narratives are what a model could return, honest or not. Nothing a model writes reaches a sentence unless the
verifier passes it, and every number in a rendered sentence was put there by the renderer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.agentic.claims import (
    CLAIM_DIRECTION,
    CLAIM_FREE_TEXT,
    CLAIM_LABEL,
    CLAIM_MALFORMED,
    CLAIM_UNREFERENCED,
    ComparisonFact,
    EvidenceIndex,
    Interval,
    SentenceSpec,
    SeriesFact,
    guardrails_for,
    rules_narrative,
    trend_holds,
    verify_narrative,
    verify_sentence,
)

EV = "ev_0123456789abcdef"
TREATED = f"{EV}:arms.treated"
CONTROL = f"{EV}:arms.control"
LABEL = f"{EV}:label"
SERIES = "ev_fedcba9876543210:series.cycles"
PROPOSAL = "prop_0011223344556677"
REPO = Path(__file__).resolve().parents[3]


def index(
    *, low: float = 0.01, high: float = 0.05, points: tuple[float, ...] = (0.01, 0.03, 0.06)
) -> EvidenceIndex:
    return EvidenceIndex(
        arms={TREATED: "the contacted group", CONTROL: "the held-back group"},
        comparisons=(
            ComparisonFact(
                left=TREATED,
                right=CONTROL,
                metric="reactivated_90d",
                metric_label="reactivated within 90 days",
                unit="share",
                difference=Interval(estimate=(low + high) / 2, low=low, high=high),
            ),
        ),
        labels={LABEL: "Causal"},
        label_subjects={LABEL: "the last measured result"},
        series={
            SERIES: SeriesFact(
                name="the measured effect",
                metric_label="the outcome rate",
                unit="share",
                points=tuple(Interval(estimate=p, low=p - 0.004, high=p + 0.004) for p in points),
            )
        },
        proposals={PROPOSAL: "send this list by email"},
    )


def comparison(comparator: str, *, left: str = TREATED, right: str = CONTROL) -> dict[str, Any]:
    return {
        "template": "{arm_a} {cmp} {arm_b} on {metric}.",
        "claim": {
            "type": "comparison",
            "left": left,
            "right": right,
            "metric": "reactivated_90d",
            "comparator": comparator,
        },
    }


def one(spec: dict[str, Any], idx: EvidenceIndex | None = None) -> Any:
    return verify_sentence(
        SentenceSpec.model_validate(spec), idx or index(), guardrails=guardrails_for(REPO / "configs")
    )


# --- comparison ------------------------------------------------------------------------------------------
def test_a_comparison_in_the_intervals_direction_is_rendered_from_the_evidence() -> None:
    sentence, failure, correction = one(comparison("gt"))
    assert failure is None and correction is None
    assert sentence.text == "The contacted group is above the held-back group on reactivated within 90 days."
    assert sentence.refs == (TREATED, CONTROL)


def test_a_comparison_read_the_other_way_round_is_checked_against_the_negated_interval() -> None:
    sentence, failure, _ = one(comparison("lt", left=CONTROL, right=TREATED))
    assert failure is None
    assert sentence.text.startswith("The held-back group is below the contacted group")


def test_the_opposite_direction_fails_with_claim_direction() -> None:
    sentence, failure, _ = one(comparison("lt"))
    assert sentence is None and failure[0] == CLAIM_DIRECTION


def test_a_direction_the_interval_does_not_support_is_written_as_not_distinguishable() -> None:
    sentence, failure, correction = one(comparison("gt"), index(low=-0.01, high=0.03))
    assert failure is None and correction is not None and correction[0] == CLAIM_DIRECTION
    assert "is not distinguishable from" in sentence.text and "above" not in sentence.text


def test_equal_passes_only_while_the_interval_includes_zero_and_is_never_called_equal() -> None:
    sentence, failure, _ = one(comparison("eq"), index(low=-0.01, high=0.03))
    assert failure is None and "is not distinguishable from" in sentence.text and "equal" not in sentence.text
    sentence, failure, _ = one(comparison("eq"))
    assert sentence is None and failure[0] == CLAIM_DIRECTION


def test_numbers_are_filled_by_the_renderer_only() -> None:
    spec = {
        **comparison("gt"),
        "template": "{arm_a} {cmp} {arm_b}: {difference} ({level} range {low} to {high}).",
    }
    sentence, failure, _ = one(spec)
    assert failure is None
    assert sentence.text.endswith(
        "+3.0 percentage points (95% range +1.0 percentage points to +5.0 percentage points)."
    )


def test_a_comparison_of_arms_the_evidence_does_not_hold_is_unreferenced() -> None:
    sentence, failure, _ = one(comparison("gt", left=f"{EV}:arms.someone_else"))
    assert sentence is None and failure[0] == CLAIM_UNREFERENCED
    spec = comparison("gt")
    spec["claim"]["metric"] = "revenue"
    assert one(spec)[1][0] == CLAIM_UNREFERENCED


def test_a_template_that_does_not_state_the_comparison_is_unreferenced() -> None:
    spec = {**comparison("gt"), "template": "{arm_a} and {arm_b} were sent on {metric}."}
    assert one(spec)[1][0] == CLAIM_UNREFERENCED


# --- label -----------------------------------------------------------------------------------------------
def test_a_label_that_matches_the_evidence_passes() -> None:
    sentence, failure, _ = one(
        {"template": "{subject} is {label}.", "claim": {"type": "label", "ref": LABEL, "value": "causal"}}
    )
    assert failure is None and sentence.text == "The last measured result is Causal."


def test_causal_is_refused_where_the_tool_did_not_return_causal() -> None:
    idx = index().model_copy(update={"labels": {LABEL: "Estimated"}})
    sentence, failure, _ = one(
        {"template": "The result is {label}.", "claim": {"type": "label", "ref": LABEL, "value": "Causal"}},
        idx,
    )
    assert sentence is None and failure[0] == CLAIM_LABEL


def test_a_label_of_evidence_that_is_not_there_is_unreferenced() -> None:
    spec = {
        "template": "It is {label}.",
        "claim": {"type": "label", "ref": "ev_ffffffffffffffff:label", "value": "Causal"},
    }
    assert one(spec)[1][0] == CLAIM_UNREFERENCED


# --- trend -----------------------------------------------------------------------------------------------
def test_a_trend_needs_three_points_one_direction_and_apart_endpoints() -> None:
    spec = {"template": "{series} {direction}.", "claim": {"type": "trend", "ref": SERIES, "direction": "up"}}
    sentence, failure, _ = one(spec)
    assert failure is None and sentence.text == "The measured effect has risen."
    for points in ((0.01, 0.06), (0.01, 0.05, 0.04), (0.010, 0.011, 0.012)):
        assert one(spec, index(points=points))[1][0] == CLAIM_DIRECTION, points
    down = {
        "template": "{series} {direction}.",
        "claim": {"type": "trend", "ref": SERIES, "direction": "down"},
    }
    assert one(down)[1][0] == CLAIM_DIRECTION
    assert trend_holds(index(points=(0.06, 0.03, 0.01)).series[SERIES].points, "down")


# --- recommendation --------------------------------------------------------------------------------------
def test_a_recommendation_names_a_proposal_the_policy_allowed() -> None:
    spec = {
        "template": "Approve {proposal} to {action}.",
        "claim": {"type": "recommendation", "proposal_id": PROPOSAL},
    }
    sentence, failure, _ = one(spec)
    assert failure is None and sentence.text == f"Approve {PROPOSAL} to send this list by email."
    spec["claim"]["proposal_id"] = "prop_9999999999999999"
    assert one(spec)[1][0] == CLAIM_UNREFERENCED


# --- free text -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "template",
    [
        "{arm_a} {cmp} {arm_b} by 4 points.",
        "{arm_a} {cmp} {arm_b}, about 12% more.",
        "{arm_a} {cmp} {arm_b}; the e-mail caused it.",
        "Our offer is significantly better.",
        "Write to asha@example.com for the list.",
    ],
)
def test_the_models_own_words_carry_no_number_no_direction_and_pass_the_guardrails(template: str) -> None:
    spec = {**comparison("gt"), "template": template} if "{" in template else {"template": template}
    sentence, failure, _ = one(spec)
    assert sentence is None and failure[0] == CLAIM_FREE_TEXT, template


@pytest.mark.parametrize(
    "template",
    [
        # spelled-out numbers and multipliers (M139 review: digits alone are not every number)
        "Expect ten times more sales.",
        "Sales will be twice what they were.",
        "About a quarter more people will buy.",
        "Sales will grow by a third.",
        "A tenfold return is expected.",
        "Seventeen customers in a lakh will buy.",
        # a direction or an outcome stated in the model's own words
        "Sales go up.",
        "This will improve sales.",
        "Sales will surge.",
        "Fewer customers will leave.",
        "Revenue is boosted by the offer.",
        # a certainty around a checked recommendation
        "{proposal} is recommended and is a sure win.",
    ],
)
def test_number_words_directions_and_certainties_in_the_models_words_are_free_text(template: str) -> None:
    claim = {"type": "recommendation", "proposal_id": PROPOSAL} if "{proposal}" in template else None
    sentence, failure, _ = one({"template": template, "claim": claim})
    assert sentence is None and failure is not None and failure[0] == CLAIM_FREE_TEXT, template


@pytest.mark.parametrize(
    "template",
    [
        "Approve {proposal} to {action}.",
        "If you agree, approve {proposal}; it will {action}.",
        "The next step: approve {proposal} so the product can {action}.",
        "Approving proposal {proposal} will {action}.",
    ],
)
def test_plain_connective_words_around_a_checked_claim_still_pass(template: str) -> None:
    sentence, failure, _ = one(
        {"template": template, "claim": {"type": "recommendation", "proposal_id": PROPOSAL}}
    )
    assert failure is None and sentence is not None, template


def test_a_slot_the_claim_does_not_fill_or_a_format_trick_is_unreferenced() -> None:
    assert (
        one({**comparison("gt"), "template": "{arm_a} {cmp} {arm_b} {proposal}."})[1][0] == CLAIM_UNREFERENCED
    )
    assert (
        one({**comparison("gt"), "template": "{arm_a.__class__} {cmp} {arm_b}."})[1][0] == CLAIM_UNREFERENCED
    )
    assert one({**comparison("gt"), "template": "{arm_a!r} {cmp} {arm_b}."})[1][0] == CLAIM_UNREFERENCED
    assert one({**comparison("gt"), "template": "{arm_a {cmp} {arm_b}."})[1][0] == CLAIM_MALFORMED


# --- the whole narrative ---------------------------------------------------------------------------------
def narrate(sentences: list[dict[str, Any]], idx: EvidenceIndex | None = None) -> Any:
    idx = idx or index()
    return verify_narrative(
        json.dumps({"sentences": sentences}),
        idx,
        rules=rules_narrative(idx),
        guardrails=guardrails_for(REPO / "configs"),
    )


def test_one_failed_sentence_is_dropped_and_the_rest_is_the_models() -> None:
    good = {"template": "{subject} is {label}.", "claim": {"type": "label", "ref": LABEL, "value": "Causal"}}
    narrative = narrate([comparison("gt"), comparison("lt"), good])
    assert narrative.source == "model" and not narrative.replaced
    assert len(narrative.sentences) == 2
    assert [f.code for f in narrative.failures] == [CLAIM_DIRECTION]


def test_more_than_one_failure_replaces_the_narrative_with_the_rules_summary() -> None:
    narrative = narrate([comparison("lt"), {"template": "It rose by 7 points."}, comparison("gt")])
    assert narrative.source == "rules" and narrative.replaced
    assert sorted(f.code for f in narrative.failures) == [CLAIM_DIRECTION, CLAIM_FREE_TEXT]
    assert narrative.text == verify_narrative(None, index(), rules=rules_narrative(index())).text


def test_an_answer_that_is_not_the_json_asked_for_is_replaced() -> None:
    idx = index()
    for raw in (
        "The e-mail worked well.",
        json.dumps({"sentences": []}),
        json.dumps({"text": "x", "sentences": []}),
    ):
        narrative = verify_narrative(raw, idx, rules=rules_narrative(idx))
        assert narrative.replaced and narrative.failures[0].code == CLAIM_MALFORMED


def test_without_a_model_the_rules_summary_is_written_and_states_only_what_verifies() -> None:
    idx = index()
    narrative = verify_narrative(
        None, idx, rules=rules_narrative(idx), guardrails=guardrails_for(REPO / "configs")
    )
    assert narrative.source == "rules" and not narrative.failures and not narrative.replaced
    text = narrative.text
    assert f"Approving proposal {PROPOSAL} will send this list by email." in text
    assert "The contacted group is above the held-back group" in text
    assert "The measured effect has risen" in text
    assert "The last measured result is labelled Causal." in text


def test_the_rules_summary_says_not_distinguishable_when_the_interval_includes_zero() -> None:
    idx = index(low=-0.02, high=0.02, points=(0.01, 0.0, 0.02))
    text = verify_narrative(None, idx, rules=rules_narrative(idx)).text
    assert "is not distinguishable from" in text and "has risen" not in text and "has fallen" not in text


def test_an_empty_index_still_has_a_true_sentence() -> None:
    narrative = verify_narrative(None, EvidenceIndex(), rules=rules_narrative(EvidenceIndex()))
    assert narrative.text == "There is no measured result to describe yet."
