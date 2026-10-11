"""Every error and refusal code Plan K can raise (`PLAN_K_CODES`).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change (the
integrator writes the catalogue entries from the milestone's result). Once M130 wires this set into
`engine.pilot.help.known_codes()`, a code added here without a plain-language entry fails
`tests/unit/pilot/test_help.py`, and a catalogue entry for a code that is not raised anywhere fails the
same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = ["AGENTIC_CODES", "CLIENT_MCP_CODES", "COMPLIANCE_CODES", "MCP_CODES", "PLAN_K_CODES"]

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
        "CONFIG_INVALID",  # configs/agentic/agentic.yaml, policy.yaml or mcp.yaml missing or not valid
        "RUN_NOT_FOUND",  # a read tool asked about a run that does not exist
    }
)
"""M132's codes (DEC-1401), joined into `PLAN_K_CODES` below."""

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
"""M134's codes (DEC-1405), joined into `PLAN_K_CODES` below."""

CLIENT_MCP_CODES: Final[frozenset[str]] = frozenset(
    {
        # M141 (DEC-1408): the verifier on client data (`api.mcp.workspace`, `api.mcp.tasks`,
        # `engine.agentic.receipts`).
        "MCP_WORKSPACE_DENIED",  # a sign-in bound to no workspace, or data outside the caller's workspace
        "RECEIPT_MISMATCH",  # a receipt the client kept is not in the chain any more: the history was rewritten
        "MCP_TASK_NOT_FOUND",  # tasks/get, tasks/result or tasks/cancel on a task this session did not start
        "MCP_TASK_CANCELLED",  # the task was cancelled (its result discarded), or it ended before the cancel
    }
)
"""M141's codes (DEC-1408), joined into `PLAN_K_CODES` below."""

PLAN_K_CODES: Final[frozenset[str]] = (
    frozenset() | AGENTIC_CODES | MCP_CODES | COMPLIANCE_CODES | CLIENT_MCP_CODES
)
"""Empty at M130; each Plan K milestone adds the codes it raises."""
