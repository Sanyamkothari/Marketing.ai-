"""`GET /runs/{run_id}/checks`: things we checked, and the too-good-to-be-true alarm (DEC-1599).

Viewer, like the run's artefacts: the answer is read from the training run's own reports
(`engine.run_checks.things_checked`) and holds no customer value, only column names and scores.
`RESULT_SUSPICIOUSLY_GOOD` is a warning: nothing here approves, rejects or blocks a model.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.runs import load_run
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.run_checks import RunChecks, things_checked

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["runs"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/runs/{run_id}/checks"): RoutePolicy(
        role=Role.VIEWER,
        action="run_checks.read",
        object_type="run",
        object_param="run_id",
        purpose="see what a training run checked",
    ),
}
"""This router's row of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)


@router.get(
    "/runs/{run_id}/checks",
    response_model=RunChecks,
    responses={404: {"model": ErrorResponse}},
    summary="What a training run checked, and whether its result is too good to be true",
)
def read_run_checks(run_id: str, storage: StorageDep, root: ConfigRootDep) -> RunChecks:
    """The checks from the run's artefacts; empty with a note for a scoring run. 404 `RUN_NOT_FOUND`."""
    return things_checked(storage, load_run(storage, run_id), root)
