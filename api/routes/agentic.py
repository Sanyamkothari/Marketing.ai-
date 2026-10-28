"""Agent proposals for a person to decide (Plan K M132, DEC-1401; `docs/AGENTIC.md`).

Mounted only when `agentic.enabled` is on (`configs/agentic/agentic.yaml`): with it off these paths do not
exist and the API is byte-identical to Plan J's (the `PLAN-K` hook in `api/main.py`).

| Method and path | Role | Does |
|---|---|---|
| `GET /agentic/proposals` | Viewer | Every proposal, newest first; `status` narrows the list. |
| `GET /agentic/proposals/{proposal_id}` | Viewer | One proposal: its payload, the evidence it cites and the policy's decisions. |
| `POST /agentic/proposals/{proposal_id}/approve` | Approver, audited | Re-checks the policy with the facts as they are now, then executes it through the route function the product already has (a test plan: `api.routes.campaigns.register_plan`). Answers the receipt with the ledger checkpoint, its seq and chain, and the holdout salt's fingerprint (M141). |
| `POST /agentic/proposals/{proposal_id}/reject` | Approver, audited | Rejects it. |
| `POST /agentic/plans` | Analyst, audited | M139: the campaign planner from a warning card, the goal form or a goal in words (`engine.agentic.planner`); runs to the interrupt and answers the plan with its proposal, or the code it stopped with. Words need an AI service (`409 AI_NOT_CONNECTED`); a card and the form do not. |
| `GET /agentic/plans/{plan_id}` | Viewer | M139: where a plan stands: its frame, the evidence, the candidates, the policy's decisions, the proposal, the narrative and, once approved, what was carried out. |
| `POST /agentic/plans/{plan_id}/resume` | Analyst, audited | M139: a plan a crash left part-way continues from the last step it wrote (a plan at its interrupt or end is answered as it is); a draft interrupted after its proposal was made reuses that proposal. |

There is no route that approves on an agent's behalf: an agent drafts through the tool registry
(`engine.agentic.registry`) or the planner, and only a signed-in person approves here. An approved campaign
plan the planner drafted is carried out through the product's own functions (M139): the treat list
(`build_treat_list`, limited to the approved channel and number of contacts - Plan M M184, DEC-1614 (a);
`409 CAMPAIGN_LIST_NOT_AS_APPROVED` otherwise, and `409 MODEL_NOT_APPROVED` for a run whose model is not approved
while its settings require approval), the campaign (`open_campaign`, the engine half of `POST /campaigns`, its treatment starting
at the approval) and its test plan (`api.routes.campaigns.register_plan`). The planner's language-model calls are
metered under the use case's own `generative.llm` and `generative.budget` (`get_planner_deps`). Refusals answer with the policy's code:
`403 POLICY_ROLE_SEPARATION` for the person it was proposed for, `409 POLICY_*` when a rule refuses it now,
`409 PROPOSAL_TAMPERED` when the stored proposal is not the one the ledger recorded at draft time,
`409 PROPOSAL_STALE`, `PROPOSAL_EXPIRED` or `PROPOSAL_NOT_PENDING`, `404 PROPOSAL_NOT_FOUND`.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field

from api.access import PrincipalDep, get_platform_engine, platform_data_dir, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep, StorageDep, get_registry
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Principal, Role
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import signer_for_data_dir
from engine.agentic.planner import (
    BuiltList,
    ExecuteActions,
    ListLimits,
    PlannerDeps,
    PlannerState,
    PlanRequest,
    execute_plan,
    load_plan,
    resume_plan,
    start_plan,
)
from engine.agentic.policy import TestPlanPayload
from engine.agentic.proposals import Proposal, ProposalStatus, ProposalStore, ProposalSummary
from engine.agentic.registry import ToolContext
from engine.agentic.service import agentic_context
from engine.approvals import ProposalReceipt, decide_proposal
from engine.config import StrictBase
from engine.settings import Settings
from engine.storage import Storage
from engine.utils.time import utc_now

__all__ = ["POLICIES", "ProposalDecisionRequest", "get_planner_deps", "router"]

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
    # Plan K M139 (DEC-1406): the campaign planner. Drafting is the Analyst's, as drafting any proposal is
    # (`engine.agentic.proposals.PROPOSER_ROLE`); reading a plan is a Viewer's (aggregates and ids only).
    ("POST", "/agentic/plans"): RoutePolicy(
        role=Role.ANALYST, action="agentic.plan_draft", purpose="have the planner draft a campaign plan"
    ),
    ("GET", "/agentic/plans/{plan_id}"): RoutePolicy(
        role=Role.VIEWER,
        action="agentic.plan_read",
        purpose="see a drafted campaign plan",
        object_type="plan",
        object_param="plan_id",
    ),
    ("POST", "/agentic/plans/{plan_id}/resume"): RoutePolicy(
        role=Role.ANALYST,
        action="agentic.plan_resume",
        purpose="continue a campaign plan a failure left part-way",
        object_type="plan",
        object_param="plan_id",
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


def _executor(request: Request, storage: Storage, ctx: ToolContext) -> Any:
    """Execute an approved proposal through the function the product's own route calls."""

    def execute(proposal: Proposal) -> str | None:
        if proposal.kind == "campaign_plan":  # Plan K M139: a plan the planner drafted
            return _execute_campaign_plan(proposal, request, storage, ctx)
        if proposal.kind != "test_plan":
            raise AgenticError(
                "PROPOSAL_NOT_EXECUTABLE",
                f"Nothing in the product carries out a {proposal.kind} proposal yet, so it stays pending.",
                status=409,
            )
        from api.routes.campaigns import register_plan

        payload = TestPlanPayload.model_validate(proposal.payload)
        try:
            plan = register_plan(
                payload.campaign_id, payload.plan, request, Response(), storage, get_registry(request)
            )
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


def _decision_chain(proposal_id: str, request: Request, storage: Storage, root: Any) -> str | None:
    """The chain a proposal is decided on (Plan K M141, DEC-1408 (c)): None, the deployment's, or a workspace's.

    The ledger says where it was drafted (`chains_drafting`), not the stored file, which is ordinary storage and
    can be edited: a proposal drafted on exactly one chain is decided there, and a file naming another workspace
    (or a name no chain can have) is refused on that chain as PROPOSAL_TAMPERED (`ledger_mismatch` compares the
    workspace). With no draft anywhere, the file's workspace is used only when it is a well-formed id of a
    configured workspace; otherwise the deployment's chain records the refusal, so one client's ids never land on
    another's chain and a malformed name never reaches the ledger."""
    import re

    from api.mcp.config import load_mcp_config
    from engine.agentic.ledger import CHAIN_PATTERN, chains_drafting

    holders = chains_drafting(get_platform_engine(request), proposal_id)
    if len(holders) == 1:
        return holders[0]
    try:
        stored = ProposalStore(storage).get(proposal_id).workspace
    except AgenticError:
        return None  # decide_proposal answers PROPOSAL_NOT_FOUND itself
    if stored is None or not re.fullmatch(CHAIN_PATTERN, stored):
        return None
    try:
        configured = {workspace.id for workspace in load_mcp_config(root).mcp.workspaces}
    except AgenticError:
        return None
    return stored if stored in configured else None


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
    ctx = _context(
        request, storage, root, settings, principal, _decision_chain(proposal_id, request, storage, root)
    )
    try:
        receipt = decide_proposal(
            proposal_id,
            "approved" if decision == "approve" else "rejected",
            principal=principal,
            reason=body.reason,
            tool_ctx=ctx,
            execute=_executor(request, storage, ctx),
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


# ---------------------------------------------------------------------------
# Plan K M139 (DEC-1406): the campaign planner
# ---------------------------------------------------------------------------
class PlanView(StrictBase):
    """`POST /agentic/plans` and `GET /agentic/plans/{plan_id}`: the plan, and its proposal once drafted."""

    plan: PlannerState
    proposal: Proposal | None = None


def _meter(storage: Storage, settings: Settings, llm: Any, budget: Any, job_id: str) -> Any:
    """The Product AI through a Meter with `llm` and `budget`, or None when no AI service answers or the client has
    not allowed the AI to read its data."""
    from engine.ai_consent import WORKSPACE_CLIENT, AiConsentRequiredError, ClientData
    from engine.ai_service import AiNotConnectedError, resolve_client
    from engine.aws_connection import profile_in_force
    from engine.generative.budget import Meter

    try:
        resolved = resolve_client(
            llm,
            slot="product",
            storage=storage,
            settings=settings,
            consent=ClientData(WORKSPACE_CLIENT),
            profile=profile_in_force(),
        )
    except AiNotConnectedError:
        return None
    try:
        resolved.client.require()  # the planner's prompts carry the client's data
    except AiConsentRequiredError:
        return None  # without the client's yes the planner works from its rules, as with no AI service
    return Meter(resolved.client, job_id=job_id, llm=resolved.llm, budget=budget)


def get_planner_deps(
    request: Request, storage: StorageDep, root: ConfigRootDep, settings: SettingsDep
) -> PlannerDeps:
    """The planner's dependencies: the registry, the model registry for the challenger card, and the Product AI.

    The language model is metered as every other metered route meters it (`api.routes.agent`,
    `api.routes.generative`): under the use case's own `generative.llm` and `generative.budget`. The
    narrative is written once FRAME knows the use case (`PlannerDeps.meter_for`). Goal words come before any
    use case is known, so they are read with the deployment's configured AI (the first use case whose file
    names Bedrock, as the AI service screen reads it; else the defaults) and the default budget, for that
    one call. No AI service: no meter, so words are refused with `AI_NOT_CONNECTED` and the narrative is the
    rules-only summary. Tests replace this whole dependency (`app.dependency_overrides`)."""
    from api.deps import get_registry
    from engine.agentic.tools import default_registry
    from engine.ai_service import yaml_bedrock
    from engine.config import BudgetConfig, ConfigError, LlmConfig, load_use_case

    job_id = f"agentic_plan_{utc_now():%Y%m%d%H%M%S}"

    def meter_for(use_case_id: str) -> Any:
        try:
            generative = load_use_case(use_case_id, root).generative
        except ConfigError:
            return None
        return _meter(storage, settings, generative.llm, generative.budget, job_id)

    words = _meter(storage, settings, yaml_bedrock(root) or LlmConfig(), BudgetConfig(), job_id)
    try:
        model_registry: Any = get_registry(request)
    except Exception:  # no model registry on this deployment: no challenger card can be drawn
        model_registry = None
    return PlannerDeps(
        registry=default_registry(), meter=words, model_registry=model_registry, meter_for=meter_for
    )


PlannerDepsDep = Annotated[PlannerDeps, Depends(get_planner_deps)]


def _plan_view(storage: Storage, state: PlannerState) -> PlanView:
    proposal = None
    if state.proposal_id is not None:
        found = ProposalStore(storage).get(state.proposal_id)
        proposal = found.model_copy(update={"status": found.status_at(utc_now())})
    return PlanView(plan=state, proposal=proposal)


@router.post(
    "/agentic/plans",
    response_model=PlanView,
    status_code=201,
    responses=_ERRORS,
    summary="Have the planner draft a campaign plan from a warning card or a goal",
)
def draft_plan(
    body: PlanRequest,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    deps: PlannerDepsDep,
) -> PlanView:
    """Run the planner to its interrupt. A plan that stops answers its code (`PLANNER_*`, `AI_NOT_CONNECTED`)
    and is kept, so `GET /agentic/plans/{id}` shows how far it went."""
    ctx = _context(request, storage, root, settings, principal)
    try:
        state = start_plan(body, ctx, deps)
    except AgenticError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise _http(exc) from exc
    if state.stopped is not None:
        set_audit_context(request, object_id=state.plan_id, details={"reason_code": state.stopped.code})
        raise http_error(state.stopped.status, state.stopped.code, state.stopped.message)
    set_audit_context(request, object_id=state.plan_id)
    return _plan_view(storage, state)


@router.get(
    "/agentic/plans/{plan_id}",
    response_model=PlanView,
    responses=_ERRORS,
    summary="Where a drafted campaign plan stands",
)
def read_plan(plan_id: str, storage: StorageDep, root: ConfigRootDep, principal: PrincipalDep) -> PlanView:
    from engine.agentic.config import require_enabled

    try:
        require_enabled(root)
        state = load_plan(
            ToolContext(storage=storage, principal=principal, config_root=root, clock=utc_now), plan_id
        )
    except AgenticError as exc:
        raise _http(exc) from exc
    return _plan_view(storage, state)


@router.post(
    "/agentic/plans/{plan_id}/resume",
    response_model=PlanView,
    responses=_ERRORS,
    summary="Continue a campaign plan a failure left part-way",
)
def resume_a_plan(
    plan_id: str,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    deps: PlannerDepsDep,
) -> PlanView:
    """The plan continued from the last step it wrote (each step is written as it completes). A plan waiting
    for a person, done or stopped is answered as it is; a stop on resuming answers its code, like a draft."""
    ctx = _context(request, storage, root, settings, principal)
    try:
        state = resume_plan(plan_id, ctx, deps)
    except AgenticError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        raise _http(exc) from exc
    set_audit_context(request, object_id=state.plan_id)
    if state.stopped is not None:
        set_audit_context(request, object_id=state.plan_id, details={"reason_code": state.stopped.code})
        raise http_error(state.stopped.status, state.stopped.code, state.stopped.message)
    return _plan_view(storage, state)


def _execute_campaign_plan(proposal: Proposal, request: Request, storage: Storage, ctx: ToolContext) -> str:
    """An approved campaign plan, through the product's own functions (`engine.agentic.planner.execute_plan`)."""
    from api.routes.campaigns import get_campaign_store, register_plan
    from api.routes.runs import requested_by
    from engine.config import load_use_case
    from engine.contracts import RunRecord
    from engine.decide.campaign_scope import CAMPAIGN_SCOPE_FILENAME, CampaignScope, write_campaign_scope
    from engine.decide.treat_list import TreatListError, build_treat_list
    from engine.measurement.cycle import CycleError, open_campaign
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key

    root = ctx.config_root
    store = get_campaign_store(request)

    def build(run_id: str, limits: ListLimits) -> BuiltList:
        # Plan M M184 (DEC-1614 (a)): the approved limits are written first, then the list is built under them
        # (always built again: a list stored before the approval contacts the whole run).
        write_campaign_scope(
            storage,
            CampaignScope(
                run_id=run_id,
                channel=limits.channel,
                max_contacts=limits.max_contacts,
                proposal_id=limits.proposal_id,
                set_at=ctx.clock(),
            ),
        )
        try:
            summary = build_treat_list(storage, run_id, config_root=root)
        except TreatListError as exc:
            # Nothing was carried out, so the limits of a plan that was not are not left on the run.
            storage.delete(run_key(run_id, CAMPAIGN_SCOPE_FILENAME))
            raise AgenticError(exc.code, exc.message, status=409) from exc
        return BuiltList(
            total_rows=summary.total_rows,
            treat_rows=summary.treat_rows,
            channel_rows=dict(summary.channel_rows or {}),
        )

    def check_model(run_id: str) -> None:
        # Plan M M184 (DEC-1614 (b)): the run's model must be an approved one while approval is required - by the
        # use case's settings now (as the planner's skip reads them) or by the run's own (as the contact-list
        # routes read them, `api.routes.runs.unapproved_list_model`); either one is enough (fails closed). The
        # rule and the code are the list routes' (`engine.model_approval`), plus a candidate nobody approved.
        from engine.config import ResolvedConfig
        from engine.model_approval import MODEL_NOT_APPROVED, unapproved_model_message
        from engine.runs import RUN_CONFIG_FILENAME
        from engine.storage import StorageError

        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
        required = load_use_case(record.use_case_id, root).governance.approval_required
        if not required:
            try:
                own = storage.read_model(run_key(run_id, RUN_CONFIG_FILENAME), ResolvedConfig).config
                required = own.governance.approval_required
            except (StorageError, ValueError):
                pass
        if not required:
            return
        refusal = unapproved_model_message(get_registry(request), record.model_version_id)
        if refusal is not None:
            raise AgenticError(MODEL_NOT_APPROVED, refusal, status=409)

    def open_(run_id: str, window: int) -> str:
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
        if record.finished_at is None:
            raise AgenticError("RUN_NOT_SCORED", "The scoring run has not finished.", status=409)
        try:
            campaign = open_campaign(
                storage,
                store,
                record,
                load_use_case(record.use_case_id, root),
                finished_at=record.finished_at,
                # Nobody is contacted before the approval: the measured window starts now.
                treatment_start=max(record.finished_at, ctx.clock()),
                bands=None,
                outcome_window_days=window,
                name=None,
                created_by=requested_by(request) or ctx.principal.user_id,
                now=utc_now(),
            )
        except CycleError as exc:
            raise AgenticError(exc.code, exc.message, status=exc.status) from exc
        return campaign.campaign_id

    def register(campaign_id: str, plan: Any) -> str:
        try:
            frozen = register_plan(campaign_id, plan, request, Response(), storage, get_registry(request))
        except HTTPException as exc:
            raw: Any = exc.detail
            detail: dict[str, Any] = raw if isinstance(raw, dict) else {}
            raise AgenticError(
                str(detail.get("code", "TEST_PLAN_INVALID")),
                str(detail.get("message", "The test plan could not be registered.")),
                status=exc.status_code,
            ) from exc
        return frozen.plan_hash

    def schedule(use_case_id: str) -> str | None:
        from api.routes.schedules import get_schedule_store
        from engine.scheduling.schedules import ScheduleKind

        try:
            found = get_schedule_store(request).list(use_case_id=use_case_id, kind=ScheduleKind.MEASURE)
        except Exception:  # no scheduler on this deployment: nothing measures on its own
            return None
        enabled = sorted(item.schedule_id for item in found if item.enabled)
        return enabled[0] if enabled else None

    return execute_plan(
        proposal,
        ctx,
        ExecuteActions(
            build_treat_list=build,
            open_campaign=open_,
            register_plan=register,
            measure_schedule=schedule,
            check_model=check_model,
        ),
    )
