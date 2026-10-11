"""Every error and refusal code Plan K can raise (`PLAN_K_CODES`).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change (the
integrator writes the catalogue entries from the milestone's result). Once M130 wires this set into
`engine.pilot.help.known_codes()`, a code added here without a plain-language entry fails
`tests/unit/pilot/test_help.py`, and a catalogue entry for a code that is not raised anywhere fails the
same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AGENTIC_CODES",
    "COMPLIANCE_CODES",
    "COMPLIANCE_CONFIG_CODES",
    "MCP_CODES",
    "MEASUREMENT_CODES",
    "PLANNER_CODES",
    "PLAN_K_CODES",
    "SAVE_DESK_CODES",
    "VARIANT_CODES",
]

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
        "CONFIG_INVALID",  # configs/agentic/agentic.yaml, policy.yaml, mcp.yaml or compliance.yaml not valid
        "RUN_NOT_FOUND",  # a read tool asked about a run that does not exist
    }
)
"""M132's codes (DEC-1401), joined into `PLAN_K_CODES` below."""

COMPLIANCE_CODES: Final[frozenset[str]] = frozenset(
    {
        # M134 (DEC-1405): the pre-send compliance gate (`engine.compliance`). Each is a reason in
        # `compliance_decisions.parquet` and a key of the treat list summary's `compliance_withheld`.
        "COMPLIANCE_CONSENT",  # not contactable on the channel (M131's channel_contactability, consent rules)
        "COMPLIANCE_WHATSAPP_NO_OPT_IN",  # no WhatsApp grant of its own in the consent ledger
        "COMPLIANCE_TEMPLATE_MISMATCH",  # the message is not the registered DLT template with its slots filled
        "COMPLIANCE_URL_NOT_WHITELISTED",  # a link in the message is not on the URL whitelist
        "COMPLIANCE_NO_CHANNEL",  # a contact with no channel: nothing the gate could clear
        "COMPLIANCE_CATALOGUE_CHANGED",  # the catalogue is not the one the run ran under: no message is checked
    }
)
"""M134's codes (DEC-1405), joined into `PLAN_K_CODES` below: exactly the gate's reasons
(`engine.compliance.gate.REASON_ORDER`)."""

COMPLIANCE_CONFIG_CODES: Final[frozenset[str]] = frozenset(
    {
        # M134 at integration (DEC-1405 (a)): the gate's one switch is compliance.gate.enabled in
        # configs/agentic/agentic.yaml; an `enabled` written in configs/compliance/compliance.yaml is refused.
        "COMPLIANCE_SWITCH_MISPLACED",
    }
)
"""The gate's configuration refusal (not a reason on a row), joined into `PLAN_K_CODES` below."""


MCP_CODES: Final[frozenset[str]] = frozenset(
    {
        # M133 (DEC-1404): the verifier over MCP (`api.mcp`).
        "MCP_DEMO_ONLY",  # not the demo workspace, or a run or campaign that is not demo data (or not there)
        "MCP_SCOPE_DENIED",  # the sign-in's roles do not map to the scope the tool needs (propose: Analyst)
        "MCP_RATE_LIMITED",  # over the per-session calls per minute, or too many calls running at once
        "MCP_TIMEOUT",  # a tool ran longer than mcp.call_timeout_seconds (no Tasks in the pinned SDK)
        "MCP_GROUP_TOO_SMALL",  # a campaign with a group of 1 to 9 (totals, an offer, a segment): not served
        "MCP_RESULT_WITHHELD",  # outside text sat in a result field that cannot be left out
        "MCP_NOT_STARTED",  # a request reached /mcp before the server had started, or after it stopped
        "MCP_TOOL_FAILED",  # an unexpected fault inside a tool: answered with a code, never its message
        # Existing access codes the MCP endpoint also answers, listed so the catalogue explains them (the
        # API's access layer raises them too, so their entries are written for every caller).
        "AUTH_REQUIRED",  # no valid bearer session
        "AUTH_NOT_CONFIGURED",  # sign-in is switched off; the MCP server never runs without it
    }
)
"""M133's codes (DEC-1404), joined into `PLAN_K_CODES` below."""


MEASUREMENT_CODES: Final[frozenset[str]] = frozenset(
    {
        # M136 (DEC-1402): pooling across comparable campaigns (`engine.measurement.pooling.POOLING_CODES`).
        "POOL_TOO_FEW",  # fewer than four (or pooling.min_campaigns) comparable campaigns: nothing is pooled
        "POOL_NO_SPREAD",  # a warning: the comparable campaigns differ by no more than chance
        # M138 (DEC-1403): the early harm alarm (`engine.measurement.harm`), a warning card and an alert, never a win.
        "HARM_ALARM",  # the contacted are doing worse than the held back by more than chance, before the planned day
    }
)
"""The measurement milestones' codes (M136, M138), joined into `PLAN_K_CODES` below."""


SAVE_DESK_CODES: Final[frozenset[str]] = frozenset(
    {
        # M144 (DEC-1411): the save-desk card (`engine.agentic.save_desk`, `api.routes.agentic_save_desk`).
        "SAVE_DESK_NO_TREAT_LIST",  # the run's treat list has not been built yet: open the contact list once
        "SAVE_DESK_CUSTOMER_NOT_FOUND",  # no customer with that key in the run's treat list
        "SAVE_DESK_KEY_INVALID",  # the key is empty, over 200 characters, or has the wrong number of parts for this run's key columns
        "SAVE_DESK_KEY_AMBIGUOUS",  # more than one treat-list row has that key: no single customer to show
        "SAVE_DESK_RATE_LIMITED",  # too many cards asked for by one person in a minute
        "SAVE_DESK_NOT_IN_PILOT_ARM",  # an active pilot gives the card only to staff drawn in advance
        # The card also raises RUN_NOT_SCORED, RUN_NOT_FOUND, CONFIG_INVALID and TOOL_ARTEFACT_UNREADABLE, which
        # already have catalogue entries (`configs/pilot/help.yaml`, or M132's list above).
    }
)
"""M144's codes (DEC-1411), joined into `PLAN_K_CODES` below."""

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

VARIANT_CODES: Final[frozenset[str]] = frozenset(
    {
        # M140 (DEC-1407): message variants as measured arms (`engine.decide.variant_arms.VARIANT_ARM_CODES`).
        "VARIANT_TEMPLATE_UNREGISTERED",  # an SMS variant's exact words have no registered DLT template id
        "VARIANT_ACTION_MISSING",  # a variant on another channel has no catalogue action with its copy: reference
        "VARIANT_ARMS_NOT_MADE",  # no arms drawn: the setting off, no copy, fewer than two variants, or no template
        "VARIANT_ARMS_NOT_ARBITRATED",  # a run whose arms are in force is not arbitrated across use cases
    }
)
"""M140's codes (DEC-1407), joined into `PLAN_K_CODES` below."""

PLAN_K_CODES: Final[frozenset[str]] = (
    frozenset()
    | AGENTIC_CODES
    | COMPLIANCE_CODES
    | COMPLIANCE_CONFIG_CODES
    | MCP_CODES
    | MEASUREMENT_CODES
    | PLANNER_CODES
    | SAVE_DESK_CODES
    | VARIANT_CODES
)
"""Empty at M130; each Plan K milestone adds the codes it raises."""
