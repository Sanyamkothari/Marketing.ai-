"""Cost in Settings: the exchange rate an Admin saves and the monthly spend (Plan J M108, DEC-1318).

`GET /cost/fx-rate` (Viewer) shows the saved rate, its source and its date; `PUT /cost/fx-rate` and
`DELETE /cost/fx-rate` (Admin, audited by the middleware) save or forget it. Without one there are no
rupee figures anywhere: the product never guesses a rate.

`GET /cost/spend` (Viewer) adds up what finished runs recorded costing, by calendar month, at AWS's
list price (an estimate, not a bill). A month where no run recorded a figure has no amount, not zero.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, Request

from api.access import get_platform_engine
from api.access_policy import RoutePolicy, register
from api.deps import StorageDep
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.aws.run_cost import FxRate
from engine.aws.spend import FxStore, SpendView, monthly_spend
from engine.config import StrictBase
from engine.utils.time import utc_now

__all__ = ["POLICIES", "FxRateView", "router"]

router: APIRouter = APIRouter(tags=["cost"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/cost/fx-rate"): RoutePolicy(
        role=Role.VIEWER, action="cost.fx_read", purpose="see the exchange rate"
    ),
    ("PUT", "/cost/fx-rate"): RoutePolicy(
        role=Role.ADMIN, action="cost.fx_update", object_type="setting", purpose="set the exchange rate"
    ),
    ("DELETE", "/cost/fx-rate"): RoutePolicy(
        role=Role.ADMIN, action="cost.fx_clear", object_type="setting", purpose="remove the exchange rate"
    ),
    ("GET", "/cost/spend"): RoutePolicy(
        role=Role.VIEWER, action="cost.spend_read", purpose="see what runs have cost"
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}

MonthsQuery = Annotated[
    int, Query(ge=1, le=60, description="How many calendar months, ending with this one.")
]


class FxRateView(StrictBase):
    """The saved exchange rate; `fx_rate` is null when an Admin has not saved one."""

    fx_rate: FxRate | None


@router.get("/cost/fx-rate", response_model=FxRateView, responses=_ERRORS, summary="The saved exchange rate")
def read_fx_rate(request: Request) -> FxRateView:
    return FxRateView(fx_rate=FxStore(get_platform_engine(request)).get())


@router.put(
    "/cost/fx-rate",
    response_model=FxRateView,
    responses=_ERRORS,
    summary="Save the exchange rate rupee figures are worked out with (Admin)",
)
def save_fx_rate(body: FxRate, request: Request) -> FxRateView:
    FxStore(get_platform_engine(request)).put(body, at=utc_now())
    return FxRateView(fx_rate=body)


@router.delete(
    "/cost/fx-rate",
    response_model=FxRateView,
    responses=_ERRORS,
    summary="Forget the exchange rate; rupee figures disappear (Admin)",
)
def clear_fx_rate(request: Request) -> FxRateView:
    FxStore(get_platform_engine(request)).clear()
    return FxRateView(fx_rate=None)


@router.get(
    "/cost/spend",
    response_model=SpendView,
    responses=_ERRORS,
    summary="What finished runs cost each month, at list price",
)
def read_spend(request: Request, storage: StorageDep, months: MonthsQuery = 6) -> SpendView:
    fx = FxStore(get_platform_engine(request)).get()
    return monthly_spend(storage, months=months, fx=fx, now=utc_now())
