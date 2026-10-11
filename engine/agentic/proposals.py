"""Proposals: what an agent may produce instead of an action (Plan K M132, design §3.1; invariant I3).

```
draft (agent) ─policy─► refused (codes, on the ledger)
             └────────► pending ─person─► approved ─► executed by the existing path
                                  ├─────► rejected
                                  └─────► expired (no decision within `proposals.expire_after_days`)
```

A :class:`Proposal` says what would be done (`payload`, canonical JSON), cites the tool results it rests on
(`evidence_ids`), carries the policy's decisions, and is keyed for pass^k by `payload_digest`
(``sha256(canonical(payload))``). It is drafted by :func:`submit_proposal`, which runs the default-deny policy
(`engine.agentic.policy`) with facts the server computes, writes `proposal.drafted` or `proposal.refused` to
the ledger and stores the proposal at `agentic/proposals/<id>.json` (ids, counts and the plan's own words:
no customer row). It becomes real only through `engine.approvals.decide_proposal`, by a person.

**`draft_test_plan`** is the first proposing tool: it computes the test plan a campaign would get -
`engine.measurement.plan.freeze_plan` in dry mode, over the campaign's stored assignment, with the outcome
window resolved and the analysis date checked as `POST /campaigns/{id}/plan` does - and proposes it. Nothing
is frozen and nothing is written to the campaign: the plan is registered only when an Approver approves the
proposal, through the same route function (`api.routes.campaigns.register_plan`).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.access.roles import Role
from engine.agentic.canonical import digest, jsonable
from engine.agentic.errors import AgenticError
from engine.agentic.policy import (
    RULES,
    Decision,
    ProposalSubject,
    RealisedFacts,
    TestPlanPayload,
    allowed,
    build_context,
    evaluate,
    payload_model,
)
from engine.config import StrictBase
from engine.measurement.plan import TestPlanInput

if TYPE_CHECKING:
    from engine.agentic.registry import ToolContext
    from engine.measurement.campaign import Campaign
    from engine.measurement.plan import RealisedPopulation, TestPlan
    from engine.storage import Storage

__all__ = [
    "PROPOSALS_PREFIX",
    "PROPOSER_ROLE",
    "DraftTestPlanInput",
    "Proposal",
    "ProposalStatus",
    "ProposalStore",
    "ProposalSummary",
    "draft_test_plan",
    "dry_test_plan",
    "ledger_mismatch",
    "proposal_key",
    "submit_proposal",
]

PROPOSALS_PREFIX: Final[str] = "agentic/proposals/"
PROPOSER_ROLE: Final[Role] = Role.ANALYST
"""Who a proposal may be drafted for, whatever its kind: the role of `POST /campaigns/{id}/plan`, the first
proposing tool's route. A Viewer reads; drafting is the Analyst's, as registering a plan is."""
_KIND: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}")

ProposalStatus = Literal["refused", "pending", "approved", "rejected", "expired"]


class Proposal(StrictBase):
    """One proposal and its whole lifecycle (design §3.1)."""

    proposal_id: str = Field(description="`prop_<16 hex>`.")
    kind: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="campaign_plan, test_plan, copy_arms (`ProposalKind`); any other kind is refused by default deny.",
    )
    payload: dict[str, Any] = Field(description="What would be done, as canonical JSON; no prose.")
    evidence_ids: tuple[str, ...] = Field(description="Every tool result it rests on, sorted.")
    narrative: dict[str, Any] | None = Field(
        default=None, description="Structured wording (design §4, M139); never needed to approve."
    )
    policy: tuple[Decision, ...] = Field(description="The policy's decisions when it was drafted.")
    payload_digest: str = Field(description="sha256(canonical(payload)): the pass^k key.")
    status: ProposalStatus
    proposed_by: str = Field(description="The user id the agent acted for.")
    actor: str = Field(description="The agent that drafted it (`agent:<name>`).")
    policy_version: str = Field(description="sha256 of configs/agentic/policy.yaml when it was drafted.")
    created_at: AwareDatetime
    expires_at: AwareDatetime
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    decision_codes: tuple[str, ...] = Field(default=(), description="Codes of the last refusal, if any.")
    result_ref: str | None = Field(
        default=None, description="What approving it made, e.g. the frozen plan_hash."
    )
    ledger_checkpoint: str | None = Field(
        default=None, description="The ledger head after the decision: the client's receipt checkpoint."
    )
    workspace: str | None = Field(
        default=None,
        description=(
            "Plan K M141: the client workspace whose chain it was drafted on (an MCP client's); its decision goes "
            "on the same chain. Absent for a proposal on the deployment's chain."
        ),
        exclude_if=lambda value: value is None,
    )

    @property
    def codes(self) -> tuple[str, ...]:
        """The refusal codes of the drafted policy decisions."""
        return tuple(sorted({d.code for d in self.policy if not d.allowed and d.code is not None}))

    def status_at(self, now: datetime) -> ProposalStatus:
        """`expired` for a pending proposal past `expires_at`; otherwise the stored status."""
        return "expired" if self.status == "pending" and now >= self.expires_at else self.status


class ProposalSummary(StrictBase):
    """`GET /agentic/proposals`: the list, newest first."""

    proposals: tuple[Proposal, ...]


def proposal_key(proposal_id: str) -> str:
    return f"{PROPOSALS_PREFIX}{proposal_id}.json"


def _safe_id(proposal_id: str) -> bool:
    return proposal_id.startswith("prop_") and proposal_id[5:].isalnum() and len(proposal_id) <= 40


class ProposalStore:
    """Proposals in the artefact store, one JSON file each (aggregate content only)."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def save(self, proposal: Proposal) -> Proposal:
        self._storage.write_model(proposal_key(proposal.proposal_id), proposal)
        return proposal

    def get(self, proposal_id: str) -> Proposal:
        from engine.storage import StorageError

        if not _safe_id(proposal_id):
            raise AgenticError("PROPOSAL_NOT_FOUND", f"No proposal with id {proposal_id!r}.", status=404)
        try:
            return self._storage.read_model(proposal_key(proposal_id), Proposal)
        except (StorageError, OSError) as exc:
            raise AgenticError(
                "PROPOSAL_NOT_FOUND", f"No proposal with id {proposal_id!r}.", status=404
            ) from exc

    def list(self, *, status: str | None = None, now: datetime | None = None) -> tuple[Proposal, ...]:
        found: list[Proposal] = []
        for key in self._storage.list_keys(PROPOSALS_PREFIX):
            if not key.endswith(".json"):
                continue
            proposal = self._storage.read_model(key, Proposal)
            shown = (
                proposal if now is None else proposal.model_copy(update={"status": proposal.status_at(now)})
            )
            if status is None or shown.status == status:
                found.append(shown)
        return tuple(sorted(found, key=lambda p: (p.created_at, p.proposal_id), reverse=True))


def _ledger_refs(payload: Mapping[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for name in ("campaign_id", "run_id", "use_case_id", "plan_hash"):
        value = payload.get(name)
        if isinstance(value, str):
            refs[name] = value
    return refs


_DECIDED_KINDS: Final[dict[str, ProposalStatus]] = {
    "proposal.approved": "approved",
    "proposal.rejected": "rejected",
    "proposal.refused": "refused",
    "proposal.expired": "expired",
}


def ledger_mismatch(proposal: Proposal, entries: Sequence[Any]) -> tuple[str | None, str | None]:
    """Why the stored `proposal` is not the one the ledger recorded, and the status the ledger says it has.

    `entries` are the ledger's entries naming the proposal, oldest first (`DecisionLedger.proposal_entries`).
    The stored file is ordinary artefact storage, so before anyone acts on it the file must match its
    `proposal.drafted` entry: the payload must hash to `payload_digest`, and the digest, kind, proposer,
    agent, evidence, creation time and expiry must be the ones recorded when it was drafted; and no later
    entry may have decided it already. Returns ``(None, None)`` when it matches, else ``(reason, status)``
    where `status` is the decided status the ledger records, or None while the ledger says it is undecided.
    """
    from engine.agentic.ledger import PROPOSAL_DRAFTED

    if digest(proposal.payload) != proposal.payload_digest:
        return "its payload no longer hashes to the digest it was drafted with", None
    drafted = next((entry for entry in entries if entry.kind == PROPOSAL_DRAFTED), None)
    decided = [_DECIDED_KINDS[entry.kind] for entry in entries if entry.kind in _DECIDED_KINDS]
    if drafted is None:
        return "the ledger has no record of it being drafted as pending", (decided[-1] if decided else None)
    body = drafted.body
    recorded = {
        "payload_digest": body.get("payload_digest"),
        "proposal_kind": body.get("proposal_kind"),
        "proposed_by": body.get("proposed_by"),
        "actor": drafted.actor,
        "evidence_ids": list(body.get("evidence_ids") or []),
        "created_at": drafted.created_at,
    }
    stored = {
        "payload_digest": proposal.payload_digest,
        "proposal_kind": proposal.kind,
        "proposed_by": proposal.proposed_by,
        "actor": proposal.actor,
        "evidence_ids": list(proposal.evidence_ids),
        "created_at": proposal.created_at,
    }
    if "expires_at" in body:
        recorded["expires_at"] = body["expires_at"]
        stored["expires_at"] = proposal.expires_at.isoformat()
    changed = sorted(name for name in recorded if recorded[name] != stored[name])
    if changed:
        return f"its {', '.join(changed)} differ from what the ledger recorded when it was drafted", None
    if decided:
        return f"the ledger records it as {decided[-1]} already", decided[-1]
    return None, None


def submit_proposal(
    kind: str,
    payload: Mapping[str, Any],
    *,
    tool_ctx: ToolContext,
    evidence_ids: Sequence[str] = (),
    proposed_by: str | None = None,
) -> Proposal:
    """Draft a proposal: run the policy, record it on the ledger, store it. Never executes anything."""
    from engine.agentic.config import load_policy, policy_version
    from engine.agentic.ledger import PROPOSAL_DRAFTED, PROPOSAL_REFUSED

    if not tool_ctx.principal.has(PROPOSER_ROLE):
        raise AgenticError(
            "TOOL_ROLE_REQUIRED",
            "Only an Analyst can have a proposal drafted for them; a Viewer can read, not propose.",
            status=403,
        )
    if tool_ctx.ledger is None:
        raise AgenticError(
            "LEDGER_CHAIN_BROKEN", "A proposal is drafted only onto the decision ledger.", status=500
        )
    unknown = sorted(tool_ctx.ledger.missing_evidence(evidence_ids)) if evidence_ids else []
    if unknown:
        raise AgenticError(
            "TOOL_INPUT_INVALID",
            f"The proposal cites evidence that no tool produced: {', '.join(unknown)}.",
            status=422,
        )
    if not _KIND.fullmatch(kind):
        raise AgenticError("TOOL_INPUT_INVALID", "A proposal kind is a short snake_case name.", status=422)
    shape = payload_model(kind)
    canonical_payload: dict[str, Any] = jsonable(dict(payload))
    if shape is not None:
        try:
            canonical_payload = jsonable(shape.model_validate(dict(payload)).model_dump(mode="json"))
        except ValueError:
            canonical_payload = jsonable(dict(payload))  # evaluate() refuses it with POLICY_PAYLOAD_INVALID
    who = proposed_by or tool_ctx.principal.user_id
    subject = ProposalSubject(kind=kind, payload=canonical_payload, proposed_by=who)
    policy = load_policy(tool_ctx.config_root)
    rules = tool_ctx.policy_rules if tool_ctx.policy_rules is not None else RULES
    decisions = evaluate(subject, build_context(subject, tool_ctx, policy), rules=rules)
    status: ProposalStatus = "pending" if allowed(decisions) else "refused"
    now = tool_ctx.clock()
    proposal = Proposal(
        proposal_id=f"prop_{secrets.token_hex(8)}",
        kind=kind,
        payload=canonical_payload,
        evidence_ids=tuple(sorted(set(evidence_ids))),
        policy=decisions,
        payload_digest=digest(canonical_payload),
        status=status,
        proposed_by=who,
        actor=tool_ctx.actor,
        policy_version=policy_version(tool_ctx.config_root),
        created_at=now,
        expires_at=now + timedelta(days=policy.proposals.expire_after_days),
        workspace=tool_ctx.ledger.chain,
    )
    tool_ctx.ledger.append(
        PROPOSAL_DRAFTED if status == "pending" else PROPOSAL_REFUSED,
        tool_ctx.actor,
        {
            "proposal_id": proposal.proposal_id,
            "proposal_kind": kind,
            "payload_digest": proposal.payload_digest,
            "evidence_ids": list(proposal.evidence_ids),
            "status": status,
            "codes": list(proposal.codes),
            "rules": [d.rule for d in decisions],
            "proposed_by": who,
            "expires_at": proposal.expires_at.isoformat(),
            **_ledger_refs(canonical_payload),
        },
        at=now,
    )
    ProposalStore(tool_ctx.storage).save(proposal)
    return proposal


# ---------------------------------------------------------------------------
# draft_test_plan: freeze_plan in dry mode
# ---------------------------------------------------------------------------
class DraftTestPlanInput(StrictBase):
    """`draft_test_plan`'s input: the plan for one campaign, as `POST /campaigns/{id}/plan` takes it."""

    __test__ = False

    campaign_id: str = Field(min_length=1, max_length=64)
    plan: TestPlanInput
    requested_holdout_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="A holdout share to ask for. The campaign's assignment decides it; a different one is refused.",
    )
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=50, description="Tool results it rests on.")


def _resolved(campaign: Campaign, plan: TestPlanInput) -> TestPlanInput:
    """The plan with the campaign's own window, checked as `api.routes.campaigns._resolved` checks it."""
    window = (
        plan.outcome_window_days if plan.outcome_window_days is not None else campaign.outcome_window_days
    )
    decided = plan.model_copy(update={"outcome_window_days": window})
    if window is not None:
        complete = (campaign.treatment_start + timedelta(days=window)).date()
        if decided.analysis_date < complete:
            raise AgenticError(
                "TEST_PLAN_INVALID",
                f"The analysis date is before every outcome is in ({complete.isoformat()}); choose that day or later.",
                status=422,
            )
    return decided


def dry_test_plan(
    tool_ctx: ToolContext, campaign: Campaign, plan: TestPlanInput
) -> tuple[TestPlan, RealisedPopulation]:
    """The plan `freeze_plan` would freeze for `campaign` now, and the realised population it rests on.

    Reads the campaign's stored assignment exactly as the plan route does (`realised_population` over
    `assignment.parquet` with the intended column); writes nothing.
    """
    from engine.measurement.campaign import ASSIGNMENT_FILENAME, INTENDED_COLUMN, campaign_key, read_frame
    from engine.measurement.plan import freeze_plan, realised_population

    decided = _resolved(campaign, plan)
    assignment = read_frame(tool_ctx.storage, campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME))
    realised = realised_population(assignment, intended_column=INTENDED_COLUMN)
    frozen = freeze_plan(
        decided,
        realised,
        campaign_id=campaign.campaign_id,
        registered_by=tool_ctx.actor,
        registered_at=tool_ctx.clock(),
    )
    return frozen, realised


def draft_test_plan(given: DraftTestPlanInput, tool_ctx: ToolContext) -> Proposal:
    """The `draft_test_plan` tool: propose registering the plan; nothing is frozen until it is approved."""
    from engine.agentic.policy import _campaign

    campaign = _campaign(tool_ctx, given.campaign_id)
    frozen, realised = dry_test_plan(tool_ctx, campaign, given.plan)
    payload = TestPlanPayload(
        campaign_id=campaign.campaign_id,
        plan=_resolved(campaign, given.plan),
        requested_holdout_fraction=given.requested_holdout_fraction,
        realised=RealisedFacts(
            population_rows=realised.population_rows,
            n_treat=realised.n_treat,
            n_holdout=realised.n_holdout,
            n_explore=realised.n_explore,
            holdout_fraction=realised.holdout_fraction,
        ),
        plan_hash=frozen.plan_hash,
    )
    return submit_proposal(
        "test_plan",
        payload.model_dump(mode="json"),
        tool_ctx=tool_ctx,
        evidence_ids=given.evidence_ids,
    )
