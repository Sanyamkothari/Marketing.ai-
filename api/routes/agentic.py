"""Agent proposals for a person to decide (Plan K M132, DEC-1401; `docs/AGENTIC.md`).

Mounted only when `agentic.enabled` is on (`configs/agentic/agentic.yaml`): with it off these paths do not
exist and the API is byte-identical to Plan J's (the `PLAN-K` hook in `api/main.py`).

| Method and path | Role | Does |
|---|---|---|
| `GET /agentic/proposals` | Viewer | Every proposal, newest first; `status` narrows the list. |
| `GET /agentic/proposals/{proposal_id}` | Viewer | One proposal: its payload, the evidence it cites and the policy's decisions. |
| `POST /agentic/proposals/{proposal_id}/approve` | Approver, audited | Re-checks the policy with the facts as they are now, then executes it through the route function the product already has (a test plan: `api.routes.campaigns.register_plan`). Answers the receipt with the ledger checkpoint, its seq and chain, and the holdout salt's fingerprint (M141). |
| `POST /agentic/proposals/{proposal_id}/reject` | Approver, audited | Rejects it. |

There is no route that drafts or approves on an agent's behalf: an agent drafts through the tool registry
(`engine.agentic.registry`), and only a signed-in person approves here. Refusals answer with the policy's code:
`403 POLICY_ROLE_SEPARATION` for the person it was proposed for, `409 POLICY_*` when a rule refuses it now,
`409 PROPOSAL_TAMPERED` when the stored proposal is not the one the ledger recorded at draft time,
`409 PROPOSAL_STALE`, `PROPOSAL_EXPIRED` or `PROPOSAL_NOT_PENDING`, `404 PROPOSAL_NOT_FOUND`.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field

from api.access import PrincipalDep, get_platform_engine, platform_data_dir, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep, StorageDep
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Principal, Role
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import signer_for_data_dir
from engine.agentic.policy import TestPlanPayload
from engine.agentic.proposals import Proposal, ProposalStatus, ProposalStore, ProposalSummary
from engine.agentic.registry import ToolContext
from engine.agentic.service import agentic_context
from engine.approvals import ProposalReceipt, decide_proposal
from engine.config import StrictBase
from engine.settings import Settings
from engine.storage import Storage
from engine.utils.time import utc_now

__all__ = ["POLICIES", "ProposalDecisionRequest", "router"]

router: APIRouter = APIRouter(tags=["agentic"])


def _on_proposal(role: Role, action: str, purpose: str) -> RoutePolicy:
    return RoutePolicy(
        role=role, action=action, purpose=purpose, object_type="proposal", object_param="proposal_id"
    )


POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/agentic/proposals"): RoutePolicy(
        role=Role.VIEWER, action="agentic.proposals_list", purpose="see agent proposals"
    ),
    ("GET", "/agentic/proposals/{proposal_id}"): _on_proposal(
        Role.VIEWER, "agentic.proposal_read", "see an agent proposal"
    ),
    ("POST", "/agentic/proposals/{proposal_id}/approve"): _on_proposal(
        Role.APPROVER, "agentic.proposal_approve", "approve an agent proposal"
    ),
    ("POST", "/agentic/proposals/{proposal_id}/reject"): _on_proposal(
        Role.APPROVER, "agentic.proposal_reject", "reject an agent proposal"
    ),
}
"""This router's rows of the access table: reading is a Viewer's (a proposal holds no customer row), deciding
is an Approver's alone, and both decisions are audited like every POST."""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


class ProposalDecisionRequest(StrictBase):
    """Body of approve and reject: why, in the decider's words (kept on the ledger as a digest only)."""

    reason: str | None = Field(default=None, max_length=500, description="Why, in the decider's words.")


def _http(exc: AgenticError) -> HTTPException:
    return http_error(exc.status, exc.code, exc.message)


def _context(
    request: Request,
    storage: Storage,
    root: Any,
    settings: Settings,
    principal: Principal,
    workspace: str | None = None,
) -> ToolContext:
    from api.routes.campaigns import get_campaign_store

    data_dir = platform_data_dir(request) or settings.data_dir
    try:
        return agentic_context(
            storage=storage,
            principal=principal,
            config_root=root,
            platform_engine=get_platform_engine(request),
            signer=signer_for_data_dir(data_dir),
            settings=settings,
            campaigns=get_campaign_store(request),
            workspace=workspace,
        )
    except AgenticError as exc:
        raise _http(exc) from exc


def _executor(request: Request, storage: Storage) -> Any:
    """Execute an approved proposal through the function the product's own route calls."""

    def execute(proposal: Proposal) -> str | None:
        if proposal.kind != "test_plan":
            raise AgenticError(
                "PROPOSAL_NOT_EXECUTABLE",
                f"Nothing in the product carries out a {proposal.kind} proposal yet, so it stays pending.",
                status=409,
            )
        from api.routes.campaigns import register_plan

        payload = TestPlanPayload.model_validate(proposal.payload)
        try:
            plan = register_plan(payload.campaign_id, payload.plan, request, Response(), storage)
        except HTTPException as exc:
            raw: Any = exc.detail
            detail: dict[str, Any] = raw if isinstance(raw, dict) else {}
            raise AgenticError(
                str(detail.get("code", "TEST_PLAN_INVALID")),
                str(detail.get("message", "The test plan could not be registered.")),
                status=exc.status_code,
            ) from exc
        return plan.plan_hash

    return execute


@router.get(
    "/agentic/proposals",
    response_model=ProposalSummary,
    responses=_ERRORS,
    summary="Agent proposals, newest first",
)
def list_proposals(
    storage: StorageDep,
    status: Annotated[ProposalStatus | None, Query(description="Only proposals in this status.")] = None,
) -> ProposalSummary:
    return ProposalSummary(proposals=ProposalStore(storage).list(status=status, now=utc_now()))


@router.get(
    "/agentic/proposals/{proposal_id}",
    response_model=Proposal,
    responses=_ERRORS,
    summary="One agent proposal: payload, evidence and the policy's decisions",
)
def read_proposal(proposal_id: str, storage: StorageDep) -> Proposal:
    try:
        proposal = ProposalStore(storage).get(proposal_id)
    except AgenticError as exc:
        raise _http(exc) from exc
    return proposal.model_copy(update={"status": proposal.status_at(utc_now())})


def _decide(
    decision: str,
    proposal_id: str,
    body: ProposalDecisionRequest,
    request: Request,
    storage: Storage,
    root: Any,
    settings: Settings,
    principal: Principal,
) -> ProposalReceipt:
    # Plan K M141 (DEC-1408 (c)): a proposal an MCP client drafted in its workspace is decided on that
    # workspace's chain. The stored file names the chain; a file edited to name another one finds no draft there
    # and is refused as PROPOSAL_TAMPERED (`ledger_mismatch`), never decided on the wrong chain.
    try:
        workspace = ProposalStore(storage).get(proposal_id).workspace
    except AgenticError:
        workspace = None  # decide_proposal answers PROPOSAL_NOT_FOUND itself
    ctx = _context(request, storage, root, settings, principal, workspace)
    try:
        receipt = decide_proposal(
            proposal_id,
            "approved" if decision == "approve" else "rejected",
            principal=principal,
            reason=body.reason,
            tool_ctx=ctx,
            execute=_executor(request, storage),
        )
    except AgenticError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise _http(exc) from exc
    return receipt


@router.post(
    "/agentic/proposals/{proposal_id}/approve",
    response_model=ProposalReceipt,
    responses=_ERRORS,
    summary="Approve an agent proposal: the policy is checked again, then the product's own path runs it",
)
def approve_proposal(
    proposal_id: str,
    body: ProposalDecisionRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> ProposalReceipt:
    return _decide("approve", proposal_id, body, request, storage, root, settings, principal)


@router.post(
    "/agentic/proposals/{proposal_id}/reject",
    response_model=ProposalReceipt,
    responses=_ERRORS,
    summary="Reject an agent proposal",
)
def reject_proposal(
    proposal_id: str,
    body: ProposalDecisionRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> ProposalReceipt:
    return _decide("reject", proposal_id, body, request, storage, root, settings, principal)
