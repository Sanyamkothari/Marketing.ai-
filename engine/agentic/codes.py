"""Every error and refusal code Plan K can raise (`PLAN_K_CODES`).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change (the
integrator writes the catalogue entries from the milestone's result). Once M130 wires this set into
`engine.pilot.help.known_codes()`, a code added here without a plain-language entry fails
`tests/unit/pilot/test_help.py`, and a catalogue entry for a code that is not raised anywhere fails the
same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = ["AGENTIC_CODES", "PLANNER_CODES", "PLAN_K_CODES"]

AGENTIC_CODES: Final[frozenset[str]] = frozenset(
    {
        # M132 (DEC-1401): the agent-safe core.
        # The policy engine (`engine.agentic.policy`): a refused proposal carries one of these.
        "POLICY_NO_RULES",  # a proposal kind with no rule set is refused (default deny)
        "POLICY_PAYLOAD_INVALID",  # a payload that does not match its kind's shape
        "POLICY_HOLDOUT_IMMUTABLE",  # holdout.immutable: shrunk, skipped, reassigned or omitted
        "POLICY_BUDGET_CAP",  # budget.cap: above the run-cost cap or the offer budget
        "POLICY_CONSENT_CHANNEL",  # consent.channel: someone on the list is not contactable on the channel
        "POLICY_FREQUENCY_CAP",  # frequency.cap: above the contact caps of decide/arbitration.yaml
        "POLICY_TEST_NOT_REGISTERED",  # test.registered: no frozen test plan, or not the one cited
        "POLICY_TEST_PLAN_FIXED",  # test.registered on a test plan: another plan is frozen, or it was measured
        "POLICY_ROLE_SEPARATION",  # role.separation: the proposer approving their own proposal
        # The tool registry (`engine.agentic.registry`).
        "TOOL_UNKNOWN",
        "TOOL_INPUT_INVALID",
        "TOOL_ROLE_REQUIRED",
        "TOOL_OUTPUT_TOO_LARGE",
        "TOOL_ARTEFACT_UNREADABLE",  # a stored artefact a tool or the policy reads is there but does not read
        # Proposals and their approval (`engine.agentic.proposals`, `engine.approvals`, `api.routes.agentic`).
        "PROPOSAL_NOT_FOUND",
        "PROPOSAL_NOT_PENDING",
        "PROPOSAL_EXPIRED",
        "PROPOSAL_STALE",
        "PROPOSAL_TAMPERED",  # the stored proposal is not the one the ledger recorded when it was drafted
        "PROPOSAL_NOT_EXECUTABLE",  # approved, but nothing carries out its kind yet (campaign plans: M139)
        # The decision ledger (`engine.agentic.ledger`).
        "LEDGER_CHAIN_BROKEN",
        "LEDGER_SIGNATURE_INVALID",
        # The switch and the policy file (`engine.agentic.config`).
        "AGENTIC_DISABLED",
        # Existing engine codes the agent-safe core also raises, listed so the catalogue explains them:
        # neither was in `known_codes()` before (`engine.decide.treat_list`, `api.routes.pilot` and
        # `engine.config` raise them too, so their entries are written for every caller).
        "CONFIG_INVALID",  # configs/agentic/agentic.yaml or policy.yaml missing or not valid
        "RUN_NOT_FOUND",  # a read tool asked about a run that does not exist
    }
)
"""M132's codes (DEC-1401), joined into `PLAN_K_CODES` below."""

PLANNER_CODES: Final[frozenset[str]] = frozenset(
    {
        # M139 (DEC-1406): the claim verifier (`engine.agentic.claims`). A sentence that fails is dropped;
        # more than one failure replaces the narrative with the rules-only summary.
        "CLAIM_DIRECTION",  # a comparison or trend the evidence's interval does not support
        "CLAIM_LABEL",  # a label other than the one the tool returned ("Causal" only where it is)
        "CLAIM_UNREFERENCED",  # evidence, a slot or a proposal the claim names is not there
        "CLAIM_FREE_TEXT",  # the model's own words carry a number, a direction or fail a guardrail
        "CLAIM_MALFORMED",  # the model's answer is not the structured narrative asked for
        # M139: the campaign planner (`engine.agentic.planner`, `POST /agentic/plans`).
        "PLANNER_NO_CANDIDATE",  # no run without a campaign, no channel, nothing safe or within budget
        "PLANNER_NOTHING_TO_PLAN",  # the card asks for waiting or a person's decision, not a campaign
        "PLANNER_CARD_NOT_FOUND",  # the warning is no longer on the Results page
        "PLANNER_NOT_FOUND",  # no plan with that id
        "PLANNER_GOAL_UNPARSED",  # the goal in words could not be matched to the form's values
    }
)
"""M139's codes (DEC-1406), joined into `PLAN_K_CODES` below."""

PLAN_K_CODES: Final[frozenset[str]] = frozenset() | AGENTIC_CODES | PLANNER_CODES
"""Empty at M130; each Plan K milestone adds the codes it raises."""
