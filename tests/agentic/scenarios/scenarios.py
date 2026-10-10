"""The M132 scenarios (design §5.1): every policy rule's refusal, the allowed cases, holdout-shrink attempts.

Each is `(world, request, expected outcome)`. A campaign-plan payload does not depend on ids the world mints
beyond the ones written into it, so its expected digest is the canonical digest of the payload in its kind's
normal form (`CampaignPlanPayload`), computed here independently of the code under test. A test-plan payload
carries the counts of a campaign the world created, so its scenarios pin the status and codes, and pass^k
pins that all ten digests agree.
"""

from __future__ import annotations

from typing import Any

from engine.agentic.canonical import digest
from engine.agentic.evals import ProposalRequest, Scenario, ToolCallRequest
from engine.agentic.policy import CampaignPlanPayload
from tests.agentic.scenarios.world import PERSISTENT_USE_CASE, USE_CASE, World

HOLDOUT = "POLICY_HOLDOUT_IMMUTABLE"
BUDGET = "POLICY_BUDGET_CAP"
CONSENT = "POLICY_CONSENT_CHANNEL"
FREQUENCY = "POLICY_FREQUENCY_CAP"
TEST = "POLICY_TEST_NOT_REGISTERED"
ROLES = "POLICY_ROLE_SEPARATION"
PLAN_FIXED = "POLICY_TEST_PLAN_FIXED"


def campaign_payload(world: World, **change: Any) -> dict[str, Any]:
    """A campaign plan every rule allows: email (everyone may be emailed), the holdout in force, the frozen plan."""
    payload: dict[str, Any] = {
        "use_case_id": USE_CASE,
        "run_id": world.run_id,
        "campaign_id": world.planned_campaign,
        "channel": "email",
        "holdout": {"fraction": 0.10, "scope": "run", "treat_members": False},
        "test_plan_hash": world.plan_hash,
        "contacts_per_customer": 1,
        "planned_contacts": 300,
        "offer_spend_inr": 1_000.0,
    }
    payload.update(change)
    return payload


def _expected(payload: dict[str, Any]) -> str:
    return digest(CampaignPlanPayload.model_validate(payload).model_dump(mode="json"))


def _campaign(
    world: World,
    name: str,
    description: str,
    status: str,
    codes: tuple[str, ...],
    guards: tuple[str, ...],
    **change: Any,
) -> Scenario:
    payload = campaign_payload(world, **change.pop("payload", {}))
    return Scenario(
        name=name,
        description=description,
        request=ProposalRequest(kind="campaign_plan", payload=payload),
        expect_status=status,  # type: ignore[arg-type]
        expect_codes=codes,
        expect_digest=_expected(payload),
        guards=guards,
        **change,
    )


def _draft(world: World, campaign_id: str, **extra: Any) -> ToolCallRequest:
    return ToolCallRequest(
        tool="draft_test_plan", input={"campaign_id": campaign_id, "plan": world.plan_body(), **extra}
    )


def scenarios(world: World) -> tuple[Scenario, ...]:
    """The suite, in the order the report lists it."""
    return (
        # --- test plans (draft_test_plan through the registry) ------------------------------------------
        Scenario(
            name="test_plan_allowed",
            description="A plan for a campaign with no plan yet is proposed, pending an Approver.",
            request=_draft(world, world.open_campaign),
            expect_status="pending",
        ),
        Scenario(
            name="test_plan_holdout_shrink",
            description="The agent asks for a 1% holdout: a test plan cannot change the assignment's holdout.",
            request=_draft(world, world.open_campaign, requested_holdout_fraction=0.01),
            expect_status="refused",
            expect_codes=(HOLDOUT,),
            guards=("holdout.immutable",),
        ),
        Scenario(
            name="test_plan_self_approval",
            description="The person the plan was proposed for approves it: refused, another Approver must.",
            request=_draft(world, world.open_campaign),
            approve_as="analyst",
            expect_status="approval_refused",
            expect_codes=(ROLES,),
            guards=("role.separation",),
        ),
        Scenario(
            name="test_plan_other_approver",
            description="Another Approver approves it: the policy allows it through to execution.",
            request=_draft(world, world.open_campaign),
            approve_as="approver",
            expect_status="approval_allowed",
        ),
        Scenario(
            name="test_plan_another_plan_frozen",
            description="A different plan for a campaign that already has a frozen one: refused at draft.",
            request=_draft(world, world.planned_campaign, plan=world.plan_body(mde_pp=3.0)),
            expect_status="refused",
            expect_codes=(PLAN_FIXED,),
            guards=("test.registered",),
        ),
        Scenario(
            name="test_plan_same_plan_again",
            description="The very plan the campaign already froze: allowed, approving it registers nothing new.",
            request=_draft(world, world.planned_campaign),
            expect_status="pending",
        ),
        Scenario(
            name="test_plan_viewer_cannot_propose",
            description="A Viewer cannot call a propose tool (the plan route is an Analyst's).",
            request=_draft(world, world.open_campaign),
            principal="viewer",
            expect_status="error",
            expect_codes=("TOOL_ROLE_REQUIRED",),
        ),
        # --- campaign plans -------------------------------------------------------------------------------
        _campaign(
            world,
            "campaign_plan_allowed",
            "Every rule allows an email campaign on the frozen plan.",
            "pending",
            (),
            (),
        ),
        _campaign(
            world,
            "campaign_plan_holdout_shrink",
            "The plan holds back 2% where 10% is in force.",
            "refused",
            (HOLDOUT,),
            ("holdout.immutable",),
            payload={"holdout": {"fraction": 0.02, "scope": "run"}},
        ),
        _campaign(
            world,
            "campaign_plan_holdout_omitted",
            "The plan has no holdout terms at all.",
            "refused",
            (HOLDOUT,),
            ("holdout.immutable",),
            payload={"holdout": None},
        ),
        _campaign(
            world,
            "campaign_plan_holdout_members_treated",
            "The plan would contact customers who are held out.",
            "refused",
            (HOLDOUT,),
            ("holdout.immutable",),
            payload={"holdout": {"fraction": 0.10, "scope": "run", "treat_members": True}},
        ),
        _campaign(
            world,
            "campaign_plan_persistent_to_run",
            "A use case with a persistent holdout, proposed with a per-run draw instead.",
            "refused",
            (HOLDOUT,),
            ("holdout.immutable",),
            payload={"use_case_id": PERSISTENT_USE_CASE},
        ),
        _campaign(
            world,
            "campaign_plan_other_persistent_scope",
            "A use case holding back per use case, proposed with the universal holdout instead.",
            "refused",
            (HOLDOUT,),
            ("holdout.immutable",),
            payload={"use_case_id": PERSISTENT_USE_CASE, "holdout": {"fraction": 0.10, "scope": "universal"}},
        ),
        _campaign(
            world,
            "campaign_plan_over_offer_budget",
            "Offers worth ₹10,000 against a ₹5,000 offer budget.",
            "refused",
            (BUDGET,),
            ("budget.cap",),
            payload={"offer_spend_inr": 10_000.0},
        ),
        _campaign(
            world,
            "campaign_plan_over_cost_cap",
            "On a billed deployment the run's list-price ceiling is above the one-cent cap.",
            "refused",
            (BUDGET,),
            ("budget.cap",),
            deployment="sagemaker",
        ),
        _campaign(
            world,
            "campaign_plan_sms_with_opt_outs_allowed",
            "SMS for 300, in a run where a quarter opted out of SMS: they are not on the SMS list, so it is allowed.",
            "pending",
            (),
            (),
            payload={"channel": "sms"},
        ),
        _campaign(
            world,
            "campaign_plan_sms_beyond_contactable",
            "SMS for 900, more than the run's customers who may be texted (about 810).",
            "refused",
            (CONSENT,),
            ("consent.channel",),
            payload={"channel": "sms", "planned_contacts": 900},
        ),
        _campaign(
            world,
            "campaign_plan_channel_not_recorded",
            "Push, a channel the run did not record anyone's consent for.",
            "refused",
            (CONSENT,),
            ("consent.channel",),
            payload={"channel": "push"},
        ),
        _campaign(
            world,
            "campaign_plan_two_contacts_each",
            "Two contacts per customer where the cap is one.",
            "refused",
            (FREQUENCY,),
            ("frequency.cap",),
            payload={"contacts_per_customer": 2},
        ),
        _campaign(
            world,
            "campaign_plan_over_channel_cap",
            "900 emails where the email cap is 500.",
            "refused",
            (FREQUENCY,),
            ("frequency.cap",),
            payload={"planned_contacts": 900},
        ),
        _campaign(
            world,
            "campaign_plan_no_frozen_plan",
            "A campaign with no frozen test plan.",
            "refused",
            (TEST,),
            ("test.registered",),
            payload={"campaign_id": world.open_campaign, "test_plan_hash": None},
        ),
        _campaign(
            world,
            "campaign_plan_other_plan_cited",
            "A plan hash that is not the campaign's frozen plan.",
            "refused",
            (TEST,),
            ("test.registered",),
            payload={"test_plan_hash": "0" * 64},
        ),
        _campaign(
            world,
            "campaign_plan_self_approval",
            "The person it was proposed for approves it.",
            "approval_refused",
            (ROLES,),
            ("role.separation",),
            approve_as="analyst",
        ),
        Scenario(
            name="campaign_plan_viewer_cannot_propose",
            description="A Viewer cannot have a campaign plan drafted for them, through any path.",
            request=ProposalRequest(kind="campaign_plan", payload=campaign_payload(world)),
            principal="viewer",
            expect_status="error",
            expect_codes=("TOOL_ROLE_REQUIRED",),
        ),
        # --- default deny ---------------------------------------------------------------------------------
        Scenario(
            name="copy_arms_has_no_rules",
            description="A kind with no rule set is refused, whatever it says.",
            request=ProposalRequest(kind="copy_arms", payload={"arms": ["A", "B"]}),
            expect_status="refused",
            expect_codes=("POLICY_NO_RULES",),
            expect_digest=digest({"arms": ["A", "B"]}),
        ),
        Scenario(
            name="unknown_kind_has_no_rules",
            description="A kind nobody defined (an agent asking to send) is refused.",
            request=ProposalRequest(kind="send_sms_now", payload={"channel": "sms"}),
            expect_status="refused",
            expect_codes=("POLICY_NO_RULES",),
            expect_digest=digest({"channel": "sms"}),
        ),
        Scenario(
            name="campaign_plan_extra_field",
            description="A campaign plan with a field its shape does not have ('skip_holdout') is refused.",
            request=ProposalRequest(
                kind="campaign_plan", payload={**campaign_payload(world), "skip_holdout": True}
            ),
            expect_status="refused",
            expect_codes=("POLICY_PAYLOAD_INVALID",),
            expect_digest=digest({**campaign_payload(world), "skip_holdout": True}),
        ),
        # --- a read tool --------------------------------------------------------------------------------
        Scenario(
            name="check_contactable_reads",
            description="The run's per-channel counts, read through the registry.",
            request=ToolCallRequest(tool="check_contactable", input={"run_id": world.run_id}),
            expect_status="read",
        ),
    )
