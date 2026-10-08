"""Plan J: planning a test before it runs (M93 onwards).

* `POST /measurement/power-preview` - for each control-group share asked about: how many customers
  are contacted and held back, the smallest change the test is sure to see, and what holding them
  back (and contacting a random few outside the selection) costs. Counts and rates only: the body
  carries no customer data, so the route is a Viewer's and is not audited as a read of one.

The route only wraps `engine.measurement.planner.power_preview`, a pure function; every number it
returns is computed there, and a number that cannot be computed is null with the reason.

* `GET /runs/{run_id}/risk-comparison` (Plan J M96) - an uplift training run's equal-budget
  comparison, `risk_comparison.json`: uplift top-N against risk top-N, cross-fitted on its randomised
  rows, by extra conversions and per rupee with intervals (`engine.measurement.compare`). Read-only and
  aggregate (no customer rows), so a Viewer's. It is computed by the training run when
  `uplift.evidence.risk_comparison` is on; a run without it answers `404 ARTEFACT_NOT_FOUND` saying so.
  Nothing is fitted on request.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from api.access_policy import RoutePolicy, register
from api.deps import StorageDep
from api.routes.runs import load_run
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.measurement.planner import PowerPreview, PowerPreviewRequest, power_preview
from engine.storage import StorageError, run_key
from engine.uplift.contracts import RISK_COMPARISON_FILENAME, RiskComparison

router = APIRouter(tags=["measurement"])

_V: Final[Role] = Role.VIEWER

POLICIES: dict[tuple[str, str], RoutePolicy] = {
    ("POST", "/measurement/power-preview"): RoutePolicy(
        role=_V,
        action="measurement.power_preview",
        purpose="preview how big a test needs to be",
    ),
}
"""This router's rows of the access table (DEC-704): planning from counts is a Viewer's, like reading a
result. A POST because the inputs are a body, not because anything changes; nothing is stored."""

register(POLICIES)

RISK_COMPARISON_POLICIES: dict[tuple[str, str], RoutePolicy] = {
    ("GET", "/runs/{run_id}/risk-comparison"): RoutePolicy(
        role=_V,
        action="measurement.risk_comparison",
        purpose="see whether uplift ranking beats risk ranking at the same budget",
        object_type="run",
        object_param="run_id",
    ),
}
"""Plan J M96: the run-scoped read this router also serves; kept apart from `POLICIES`, which lists the
`/measurement` routes, because it is about one run (Viewer: aggregate numbers, no customer rows)."""

register(RISK_COMPARISON_POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "/measurement/power-preview",
    response_model=PowerPreview,
    responses=_ERRORS,
    summary="How small a change each control-group share can see, and what it costs (counts only)",
)
def preview_power(body: PowerPreviewRequest) -> PowerPreview:
    """The planner at each control-group share in the body; nothing is stored or read."""
    return power_preview(body)


@router.get(
    "/runs/{run_id}/risk-comparison",
    response_model=RiskComparison,
    responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    summary="Uplift top-N against risk top-N at equal budget, cross-fitted (an uplift training run's)",
)
def read_risk_comparison(run_id: str, storage: StorageDep) -> RiskComparison:
    """`risk_comparison.json` as the training run wrote it; nothing is computed on request."""
    load_run(storage, run_id)
    try:
        return storage.read_model(run_key(run_id, RISK_COMPARISON_FILENAME), RiskComparison)
    except StorageError as exc:
        raise http_error(
            404,
            "ARTEFACT_NOT_FOUND",
            "This run has no equal-budget comparison. An uplift training run computes it when "
            "uplift.evidence.risk_comparison is turned on (LightGBM base model only).",
        ) from exc
