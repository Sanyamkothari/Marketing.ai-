"""The Approver's screen's API (Plan D M54): the challengers waiting, and turning one down.

`GET /approvals` lists every `pending_approval` version with its head-to-head against the champion it
was measured against (`engine.approvals.head_to_head`: the same held-out rows, every metric the train
flow recorded for both models, the differences), who trained it, the earlier decisions on it, and
whether *the caller* may decide - a person without the Approver role, or the person who trained the
model, sees the reason on the screen before clicking (DEC-862, DEC-864). It is a Viewer read: seeing
what is waiting is not deciding it.

`POST /models/{model_id}/reject` is the Approver's "no", with a required reason: the version is
archived and the decision recorded. Only a version waiting for approval can be rejected
(`409 INVALID_TRANSITION` otherwise) - a champion is replaced by promoting another, never by
rejecting it, which would leave the use case with no model. The status is checked again by the
registry under its lock as the version is archived (`archive(expected_status=...)`), so an approval
that lands between this route's read and its write is not undone by the reject (DEC-873). The trainer may reject their own
challenger: withdrawing a model needs no second person, approving one does.

Approve and promote stay in `api/routes/models.py`, which records their decisions in the same table.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, Request

from api.access import PrincipalDep, get_platform_engine, set_audit_context
from api.access_policy import RoutePolicy, policy_for, refusal_message, register
from api.deps import RegistryDep, SettingsDep, StorageDep
from api.routes.models import registry_http
from api.routes.uploads import http_error
from api.schemas import ApprovalListResponse, ErrorResponse, ModelDecisionResponse, ModelRejectRequest
from engine.access.roles import Role
from engine.approvals import pending_approvals, record_decision
from engine.contracts import ModelStatus
from engine.registry import RegistryError

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["models"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/approvals"): RoutePolicy(
        role=Role.VIEWER, action="approvals.list", purpose="see the challengers waiting for approval"
    ),
    ("POST", "/models/{model_id}/reject"): RoutePolicy(
        role=Role.APPROVER,
        action="models.reject",
        object_type="model",
        object_param="model_id",
        purpose="reject a challenger",
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

UseCaseQuery = Annotated[str | None, Query(description="Keep only this use case's challengers.")]


@router.get(
    "/approvals",
    response_model=ApprovalListResponse,
    responses=_ERRORS,
    summary="Challengers waiting for an Approver, with the head-to-head and who may decide",
)
def list_approvals(
    request: Request,
    principal: PrincipalDep,
    registry: RegistryDep,
    storage: StorageDep,
    settings: SettingsDep,
    use_case: UseCaseQuery = None,
) -> ApprovalListResponse:
    """Newest first. `can_decide` and `blocked_reason` are for the caller, so the screen explains itself."""
    approve = policy_for("POST", "/models/{model_id}/approve")
    may_approve = approve is None or approve.role is None or principal.has(approve.role)
    items = pending_approvals(
        storage,
        registry,
        get_platform_engine(request),
        principal,
        use_case_id=use_case,
        may_approve=may_approve,
        role_reason=None if may_approve or approve is None else refusal_message(approve),
    )
    return ApprovalListResponse(items=items, separation_enforced=settings.auth_mode != "off")


@router.post(
    "/models/{model_id}/reject",
    response_model=ModelDecisionResponse,
    responses=_ERRORS,
    summary="Turn down a challenger waiting for approval, with a reason",
)
def reject_model(
    model_id: str,
    body: ModelRejectRequest,
    request: Request,
    principal: PrincipalDep,
    registry: RegistryDep,
) -> ModelDecisionResponse:
    """`pending_approval` -> `archived`, with the reason recorded; any other status is `409 INVALID_TRANSITION`."""
    try:
        version = registry.get(model_id)
    except RegistryError as exc:
        raise registry_http(exc) from exc
    if version.status is not ModelStatus.PENDING_APPROVAL:
        set_audit_context(request, details={"reason_code": "INVALID_TRANSITION"})
        raise http_error(
            409,
            "INVALID_TRANSITION",
            f"Only a version waiting for approval can be rejected; {model_id} is {version.status.value}.",
        )
    champion = registry.get_champion(version.use_case_id)
    try:
        archived = registry.archive(model_id, expected_status=ModelStatus.PENDING_APPROVAL)
    except RegistryError as exc:
        # Approved or promoted since the read above: the registry refused under its lock (DEC-873).
        set_audit_context(request, details={"reason_code": exc.code})
        raise registry_http(exc) from exc
    decision = record_decision(
        get_platform_engine(request),
        archived,
        "rejected",
        principal=principal,
        reason=body.reason,
        champion_id=None if champion is None else champion.model_id,
    )
    set_audit_context(request, details={"use_case_id": version.use_case_id, "model_id": model_id})
    return ModelDecisionResponse(version=archived, is_champion=False, decision=decision)
