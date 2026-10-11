"""The planner's pure parts (Plan K M139): the trigger, the card mapping, amounts in words, its codes, and the
campaign-plan shape it drafts and the one policy rule it changes (`test.registered`, DEC-1406 (b))."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from engine.agentic.canonical import digest
from engine.agentic.claims import CLAIM_CODES
from engine.agentic.codes import PLAN_K_CODES, PLANNER_CODES
from engine.agentic.config import load_policy
from engine.agentic.planner import (
    CARD_OBJECTIVES,
    NOTHING_TO_PLAN,
    OBJECTIVES,
    PlanRequest,
    amounts_in,
)
from engine.agentic.planner import (
    PLANNER_CODES as RAISED,
)
from engine.agentic.policy import CampaignPlanPayload, PolicyContext, ProposalSubject, evaluate
from engine.measurement.summary import CardKind

TEST_PLAN: dict[str, Any] = {
    "metric": "reactivated_90d within 90 days",
    "outcome_column": "reactivated_90d",
    "outcome_window_days": 90,
    "analysis_date": "2027-01-08",
}


def test_every_card_kind_is_either_planned_or_explained() -> None:
    kinds = set(get_args(CardKind))
    assert set(CARD_OBJECTIVES) | set(NOTHING_TO_PLAN) == kinds
    assert not set(CARD_OBJECTIVES) & set(NOTHING_TO_PLAN)
    assert set(CARD_OBJECTIVES.values()) <= set(OBJECTIVES)
    assert all(not any(ch.isdigit() for ch in reason) for reason in NOTHING_TO_PLAN.values())


def test_the_planners_and_the_verifiers_codes_are_plan_k_codes() -> None:
    assert RAISED == PLANNER_CODES - CLAIM_CODES
    assert CLAIM_CODES <= PLANNER_CODES <= PLAN_K_CODES
    assert {"CLAIM_DIRECTION", "CLAIM_LABEL", "CLAIM_UNREFERENCED", "PLANNER_NO_CANDIDATE"} <= PLAN_K_CODES


@pytest.mark.parametrize(
    ("text", "amounts"),
    [
        ("win back lapsed customers in Mumbai within ₹2 lakh", (200000.0,)),
        ("a budget of Rs 1,50,000 or 3 crore", (150000.0, 30000000.0)),
        ("spend 40k, maybe 2.5 lakhs", (40000.0, 250000.0)),
        ("no amount here", ()),
        ("the Q3 campaign", ()),
    ],
)
def test_amounts_are_read_from_the_words_by_code(text: str, amounts: tuple[float, ...]) -> None:
    assert amounts_in(text) == amounts


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"goal_text": "x", "goal": {"use_case_id": "win-back-campaign"}},
        {"card": {"code": "PLAN_UNDERPOWERED"}, "goal_text": "x"},
        {"goal": {"use_case_id": "win-back-campaign", "objective": "skip_the_holdout"}},
        {"goal": {"use_case_id": "../etc"}},
        {"goal": {"use_case_id": "win-back-campaign", "holdout": 0.0}},
        {"goal_text": "x" * 501},
    ],
)
def test_a_plan_request_is_one_trigger_of_enumerated_values(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PlanRequest.model_validate(body)


def test_an_m132_campaign_plan_hashes_as_it_did() -> None:
    """The fields M139 adds are left out while unset: a payload without them has the same digest."""
    m132 = {
        "use_case_id": "win-back-campaign",
        "run_id": "r_1",
        "campaign_id": "c_1",
        "channel": "email",
        "holdout": {"fraction": 0.1, "scope": "run", "treat_members": False},
        "test_plan_hash": "b" * 64,
        "contacts_per_customer": 1,
        "planned_contacts": 300,
        "offer_spend_inr": 1000.0,
    }
    dumped = CampaignPlanPayload.model_validate(m132).model_dump(mode="json")
    assert dumped == m132 and digest(dumped) == digest(m132)


def _decide(payload: dict[str, Any], root: Path, **facts: Any) -> Any:
    from engine.agentic.policy import RULES

    subject = ProposalSubject(kind="campaign_plan", payload=payload, proposed_by="u_asha")
    ctx = PolicyContext(thresholds=load_policy(root), **facts)
    return {d.rule: d for d in evaluate(subject, ctx, rules={"test.registered": RULES["test.registered"]})}


def _new_campaign(**change: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "use_case_id": "win-back-campaign",
        "run_id": "r_1",
        "campaign_id": None,
        "channel": "sms",
        "holdout": {"fraction": 0.1, "scope": "run", "treat_members": False},
        "test_plan": TEST_PLAN,
        "planned_contacts": 300,
    }
    base.update(change)
    return base


def test_a_plan_for_a_new_campaign_carrying_its_test_plan_is_registered_on_approval(
    config_root: Path,
) -> None:
    decision = _decide(_new_campaign(), config_root)["test.registered"]
    assert decision.allowed and decision.detail == {"registers_on_approval": True}


def test_a_new_campaign_with_no_test_plan_or_a_cited_hash_is_refused(config_root: Path) -> None:
    for payload in (_new_campaign(test_plan=None), _new_campaign(test_plan_hash="c" * 64)):
        decision = _decide(payload, config_root)["test.registered"]
        assert not decision.allowed and decision.code == "POLICY_TEST_NOT_REGISTERED", payload
