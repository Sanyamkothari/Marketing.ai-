"""The model registry endpoints: `GET /models`, `POST /models/{id}/approve`, `POST /models/{id}/promote`.

This router owns no policy. The champion rule lives in `engine.registry.should_promote` and is applied
by the `register` stage; the atomic swap that demotes the incumbent and crowns the successor lives in
`LocalModelRegistry._make_champion`. Everything here is transport: read the registry, map a
`RegistryError` onto M1's error envelope, and answer with the row as it now stands.

**Approve and promote are not two spellings of the same thing.**

* `approve` finishes a decision the engine already made. `register` marked the version
  `pending_approval` because it *did* beat the re-scored champion but `governance.approval_required`
  held it back (M3 design §7.3). Approving is a human saying yes to that, and nothing else: any other
  status is refused, because there is no pending engine decision to say yes to.
* `promote` overrides the champion rule by hand, from any state the registry deems eligible. It takes
  a required `reason`, because a champion swap nobody can account for later is worse than no swap.

**"Who" is not an identity.** Phase 1 has no authentication (plan §1.3), so `approved_by` and
`promoted_by` are strings the caller typed. This module neither verifies them nor invents one when
they are absent: both fields are required, and a request without them is a `422`. Storing a name the
API made up would put a fabricated identity in the audit trail; storing an unverified one at least
records what the caller claimed.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query

from api.deps import RegistryDep
from api.routes.uploads import http_error
from api.schemas import (
    ErrorResponse,
    ModelApproveRequest,
    ModelListResponse,
    ModelPromoteRequest,
    ModelVersionResponse,
)
from engine.contracts import ModelStatus, ModelVersion
from engine.registry import RegistryError

router: APIRouter = APIRouter(tags=["models"])

REGISTRY_STATUS: Final[dict[str, int]] = {
    "MODEL_NOT_FOUND": 404,
    "INVALID_TRANSITION": 409,
    "METRIC_MISMATCH": 409,
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
def approve_model(model_id: str, body: ModelApproveRequest, registry: RegistryDep) -> ModelVersionResponse:
    """`pending_approval` -> `champion`; any other status is a `409 INVALID_TRANSITION`.

    `body.approved_by` is caller-supplied and unverified (plan §1.3 leaves Phase 1 without
    authentication); the registry stores it verbatim so the row names whoever claimed the decision.
    """
    try:
        version = registry.approve(model_id, by=body.approved_by)
    except RegistryError as exc:
        raise registry_http(exc) from exc
    return as_response(version)


@router.post(
    "/models/{model_id}/promote",
    response_model=ModelVersionResponse,
    responses=_TRANSITION_ERRORS,
    summary="Make a version champion by hand, recording who did it and why",
)
def promote_model(model_id: str, body: ModelPromoteRequest, registry: RegistryDep) -> ModelVersionResponse:
    """The manual override of plan §8: the champion rule is bypassed, so the reason is mandatory.

    `body.promoted_by` carries the same caveat as `approve`'s `approved_by` - a caller-supplied string,
    not a verified identity. `body.reason` is stored as the version's `promotion_note`, which is the
    only record of why the rule was overridden.
    """
    try:
        version = registry.promote(model_id, by=body.promoted_by, note=body.reason)
    except RegistryError as exc:
        raise registry_http(exc) from exc
    return as_response(version)


def as_response(version: ModelVersion) -> ModelVersionResponse:
    """The row plus plan §8's champion flag, restating `status` rather than asking the registry twice."""
    return ModelVersionResponse(version=version, is_champion=version.status is ModelStatus.CHAMPION)


def registry_http(exc: RegistryError) -> HTTPException:
    """A `RegistryError` in M1's envelope. An unmapped code is a 500: this router does not guess."""
    return http_error(REGISTRY_STATUS.get(exc.code, UNMAPPED_STATUS), exc.code, exc.message)
