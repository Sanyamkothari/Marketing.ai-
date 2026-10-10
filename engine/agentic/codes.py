"""Every error and refusal code Plan K can raise (`PLAN_K_CODES`).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change (the
integrator writes the catalogue entries from the milestone's result). Once M130 wires this set into
`engine.pilot.help.known_codes()`, a code added here without a plain-language entry fails
`tests/unit/pilot/test_help.py`, and a catalogue entry for a code that is not raised anywhere fails the
same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = ["AGENTIC_CODES", "PLAN_K_CODES"]

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
        "POLICY_ROLE_SEPARATION",  # role.separation: the proposer approving their own proposal
        # The tool registry (`engine.agentic.registry`).
        "TOOL_UNKNOWN",
        "TOOL_INPUT_INVALID",
        "TOOL_ROLE_REQUIRED",
        "TOOL_OUTPUT_TOO_LARGE",
        # Proposals and their approval (`engine.agentic.proposals`, `engine.approvals`, `api.routes.agentic`).
        "PROPOSAL_NOT_FOUND",
        "PROPOSAL_NOT_PENDING",
        "PROPOSAL_EXPIRED",
        "PROPOSAL_STALE",
        "PROPOSAL_NOT_EXECUTABLE",  # approved, but nothing carries out its kind yet (campaign plans: M139)
        # The decision ledger (`engine.agentic.ledger`).
        "LEDGER_CHAIN_BROKEN",
        "LEDGER_SIGNATURE_INVALID",
        # The switch (`engine.agentic.config`).
        "AGENTIC_DISABLED",
    }
)
"""M132's codes (DEC-1401), joined into `PLAN_K_CODES` below."""

PLAN_K_CODES: Final[frozenset[str]] = frozenset() | AGENTIC_CODES
"""Empty at M130; each Plan K milestone adds the codes it raises."""
