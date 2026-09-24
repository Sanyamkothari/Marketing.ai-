"""The model registry endpoints: `GET /models`, `POST /models/{id}/approve`, `POST /models/{id}/promote`.

This router owns no policy. The champion rule lives in `engine.registry.should_promote` and is applied
by the `register` stage; the atomic swap that demotes the incumbent and crowns the successor lives in
`LocalModelRegistry._make_champion`. Everything here is transport: read the registry, map a
`RegistryError` onto M1's error envelope, and answer with the row as it now stands.

**Approve and promote are not two spellings of the same thing.**

* `approve` finishes a decision the engine already made. `register` marked the version
  `pending_approval` because it *did* beat the re-scored champion but `governance.approval_required`
  held it back (plan §6.3). Approving is a human saying yes to that, and nothing else: any other
  status is refused, because there is no pending engine decision to say yes to.
* `promote` overrides the champion rule by hand, from any state the registry deems eligible. It takes
  a required `reason`, because a champion swap nobody can account for later is worse than no swap.

**"Who" is an identity when sign-in is on** (Plan D M54, DEC-862). Phase 1 had no authentication
(plan §1.3), so `approved_by` and `promoted_by` were strings the caller typed. Both fields are still
required, and with sign-in off they are still stored as typed. With sign-in on, the registry records
the signed-in username instead, whatever the body says, so the row cannot name somebody else.

**Separation of duties** (DEC-862). Whoever started the training run that produced a version cannot
approve or promote it: **403 `SEPARATION_OF_DUTIES`**. `POST /models/{id}/reject` (Plan D) turns a
waiting challenger down with a reason. Every decision - approved, rejected, promoted - is recorded with
its reason in `model_decision` (`engine.approvals`), which the Approvals screen reads.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, Request

from api.deps import RegistryDep, StorageDep
from api.routes.uploads import http_error
from api.schemas import (
    ErrorResponse,
    ModelApproveRequest,
    ModelListResponse,
    ModelPromoteRequest,
    ModelVersionResponse,
)
from engine.access.roles import Principal
from engine.approvals import DecisionKind, record_decision, separation_refusal, trainer_of
from engine.contracts import ModelStatus, ModelVersion
from engine.registry import ModelRegistry, RegistryError
from engine.storage import Storage

router: APIRouter = APIRouter(tags=["models"])

REGISTRY_STATUS: Final[dict[str, int]] = {
    "MODEL_NOT_FOUND": 404,
    "INVALID_TRANSITION": 409,
    "METRIC_MISMATCH": 409,
    "CHAMPION_CHANGED": 409,
}
"""`RegistryError.code` -> HTTP status. An unlisted code is a 500, not a guessed 4xx (`registry_http`)."""

UNMAPPED_STATUS: Final[int] = 500
"""A code this router has not been taught is a server fault, not the caller's; it is reported as one."""

UseCaseQuery = Annotated[str | None, Query(description="Keep only the versions of this use case.")]

_TRANSITION_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/models",
    response_model=ModelListResponse,
    summary="Registered model versions, newest first, with the champion flagged",
)
def list_models(registry: RegistryDep, use_case: UseCaseQuery = None) -> ModelListResponse:
    """Plan §8's `GET /models?use_case=`. Without the filter, every use case's versions are returned."""
    versions = registry.list_versions(use_case)
    return ModelListResponse(versions=tuple(as_response(version) for version in versions))


@router.post(
    "/models/{model_id}/approve",
    response_model=ModelVersionResponse,
    responses=_TRANSITION_ERRORS,
    summary="Approve a version that is waiting for a human, making it champion",
)
def approve_model(
    model_id: str, body: ModelApproveRequest, request: Request, registry: RegistryDep, storage: StorageDep
) -> ModelVersionResponse:
    """`pending_approval` -> `champion`; any other status is a `409 INVALID_TRANSITION`.

    A pending version whose promotion decision was measured against a champion that no longer
    holds the title is refused by the registry with `CHAMPION_CHANGED`, which is a `409` here: the
    request is well formed and the version exists, but the comparison behind it is stale, and
    approval may not re-decide the championship on a head-to-head with a model that is no longer
    the incumbent (DEC-047). The user retrains or re-scores, or overrides deliberately through
    `promote`.

    `body.approved_by` is caller-supplied and unverified (plan §1.3 leaves Phase 1 without
    authentication); the registry stores it verbatim so the row names whoever claimed the decision
    (DEC-055).
    """
    principal, champion = _check_decider(request, registry, storage, model_id)
    try:
        version = registry.approve(model_id, by=_decider_name(principal, body.approved_by))
    except RegistryError as exc:
        raise registry_http(exc) from exc
    _record(request, version, "approved", principal, body.reason, champion)
    return as_response(version)


@router.post(
    "/models/{model_id}/promote",
    response_model=ModelVersionResponse,
    responses=_TRANSITION_ERRORS,
    summary="Make a version champion by hand, recording who did it and why",
)
def promote_model(
    model_id: str, body: ModelPromoteRequest, request: Request, registry: RegistryDep, storage: StorageDep
) -> ModelVersionResponse:
    """The manual override of plan §8: the champion rule is bypassed, so the reason is mandatory.

    `body.promoted_by` carries the same caveat as `approve`'s `approved_by` - a caller-supplied string,
    not a verified identity. `body.reason` is stored as the version's `promotion_note`, which is the
    only record of why the rule was overridden.
    """
    principal, champion = _check_decider(request, registry, storage, model_id)
    try:
        version = registry.promote(model_id, by=_decider_name(principal, body.promoted_by), note=body.reason)
    except RegistryError as exc:
        raise registry_http(exc) from exc
    _record(request, version, "promoted", principal, body.reason, champion)
    return as_response(version)


def _principal(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


def _check_decider(
    request: Request, registry: ModelRegistry, storage: Storage, model_id: str
) -> tuple[Principal | None, str | None]:
    """The caller and the current champion's id; 404 for an unknown version, 403 for its trainer (DEC-862)."""
    from api.access import set_audit_context  # api.access imports the routers' package

    try:
        version = registry.get(model_id)
    except RegistryError as exc:
        raise registry_http(exc) from exc
    principal = _principal(request)
    if principal is not None:
        refusal = separation_refusal(principal, trainer_of(storage, version))
        if refusal is not None:
            set_audit_context(request, details={"reason_code": "SEPARATION_OF_DUTIES"})
            raise http_error(403, "SEPARATION_OF_DUTIES", refusal)
    champion = registry.get_champion(version.use_case_id)
    return principal, None if champion is None else champion.model_id


def _decider_name(principal: Principal | None, typed: str) -> str:
    """The signed-in username when sign-in is on; what was typed otherwise (DEC-862)."""
    return principal.username if principal is not None and principal.kind == "user" else typed


def _record(
    request: Request,
    version: ModelVersion,
    decision: DecisionKind,
    principal: Principal | None,
    reason: str | None,
    champion_id: str | None,
) -> None:
    """Append the decision to `model_decision`; an app without access control has nobody to record."""
    if principal is None:
        return
    from api.access import get_platform_engine

    record_decision(
        get_platform_engine(request),
        version,
        decision,
        principal=principal,
        reason=reason,
        champion_id=champion_id,
    )


def as_response(version: ModelVersion) -> ModelVersionResponse:
    """The row plus plan §8's champion flag, restating `status` rather than asking the registry twice."""
    return ModelVersionResponse(version=version, is_champion=version.status is ModelStatus.CHAMPION)


def registry_http(exc: RegistryError) -> HTTPException:
    """A `RegistryError` in M1's envelope. An unmapped code is a 500: this router does not guess."""
    return http_error(REGISTRY_STATUS.get(exc.code, UNMAPPED_STATUS), exc.code, exc.message)
