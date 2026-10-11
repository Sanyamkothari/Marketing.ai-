"""Every error and refusal code Plan K can raise (`PLAN_K_CODES`).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change (the
integrator writes the catalogue entries from the milestone's result). Once M130 wires this set into
`engine.pilot.help.known_codes()`, a code added here without a plain-language entry fails
`tests/unit/pilot/test_help.py`, and a catalogue entry for a code that is not raised anywhere fails the
same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = ["AGENTIC_CODES", "PLAN_K_CODES", "SAVE_DESK_CODES"]

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

PLAN_K_CODES: Final[frozenset[str]] = frozenset() | AGENTIC_CODES | SAVE_DESK_CODES
"""Empty at M130; each Plan K milestone adds the codes it raises."""
