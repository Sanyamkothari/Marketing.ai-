"""Plan J: planning a test before it runs (M93 onwards).

* `POST /measurement/power-preview` - for each control-group share asked about: how many customers
  are contacted and held back, the smallest change the test is sure to see, and what holding them
  back (and contacting a random few outside the selection) costs. Counts and rates only: the body
  carries no customer data, so the route is a Viewer's and is not audited as a read of one.

The route only wraps `engine.measurement.planner.power_preview`, a pure function; every number it
returns is computed there, and a number that cannot be computed is null with the reason.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from api.access_policy import RoutePolicy, register
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.measurement.planner import PowerPreview, PowerPreviewRequest, power_preview

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
