"""The save-desk card: a read-only lookup for call-centre staff (Plan K M144, DEC-1411; `docs/AGENTIC.md`).

Mounted only when `agentic.enabled` is on (`configs/agentic/agentic.yaml`): with it off this path does not
exist and the API is byte-identical to Plan J's (the `PLAN-K (M144)` hook in `api/main.py`).

| Method and path | Role | Does |
|---|---|---|
| `POST /agentic/save-desk/card` | Analyst, audited | The card for one customer of one scoring run: top reasons, the recommended offer and channel, net value in rupees, and the "likely to stay anyway, do not discount" flag, each line with its evidence label. |

**Why a POST.** The customer's key is in the request body, never in a URL or query string (DEC-746): a path is
written to every access log on the way. The response does not echo the key.

**Who.** Analyst, the role that may already download the treat list row by row (`ROW_LEVEL_DOWNLOAD`); a Viewer is
refused with `403 ROLE_REQUIRED` before the route runs. A narrower `agent_desk` role was not added: roles are
the closed `engine.access.roles.Role` enum, and a fifth member would change every access table and every
"for every role" test of Plan D. The route gives a call-centre agent no more than that role could already read.

**Audited row-level read.** Every call is one audit event (the middleware writes it, as for every POST): the
actor, the run, and `principal_hash`, a salted hash of the key (`privacy_salt`), so "who looked at this
customer" is answerable by recomputing the hash and no key is in the trail. A refusal carries its code as
`reason_code`.

**Rate limited.** At most `rate_limit_per_minute` cards per signed-in person in a sliding minute
(`configs/agentic/save_desk.yaml`, default 30); past it `429 SAVE_DESK_RATE_LIMITED` with `Retry-After`. One
customer in front of one agent is a call; a copy of the customer list is a loop.

**The pilot.** While `configs/agentic/save_desk_pilot.yaml` is `status: active`, only staff in its `card_access`
list are served a card; anyone else gets `403 SAVE_DESK_NOT_IN_PILOT_ARM`. That is what makes access the
randomised one rather than whoever asked.

Nothing is logged about a customer: no key, no value, no reason text. The response is `Cache-Control: no-store`.
"""

from __future__ import annotations

import threading
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, Response

from api.access import PrincipalDep, get_platform_engine, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep, StorageDep
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.agentic.errors import AgenticError
from engine.agentic.save_desk import (
    CardRateLimiter,
    CardRequest,
    SaveDeskCard,
    build_card,
    card_access_allowed,
    load_pilot,
    load_settings,
)

__all__ = ["POLICIES", "router"]

router: APIRouter = APIRouter(tags=["agentic"])

CARD_ROUTE: Final[tuple[str, str]] = ("POST", "/agentic/save-desk/card")

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    CARD_ROUTE: RoutePolicy(
        role=Role.ANALYST,
        action="agentic.save_desk_card",
        purpose="look up a customer's save-desk card",
        object_type="run",
    ),
}
"""This router's row of the access table: an Analyst (who may already download customer rows).

No `audit_reads`: that flag is for GET routes, and the middleware audits every POST once per call anyway.
"""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}

_LIMITER_SLOT: Final[str] = "save_desk_limiter"
_LIMITER_LOCK: Final[threading.Lock] = threading.Lock()


def _http(exc: AgenticError) -> HTTPException:
    return http_error(exc.status, exc.code, exc.message)


def _limiter(request: Request, limit: int) -> CardRateLimiter:
    """The app's limiter, built once (a changed limit in the file takes a restart, like the sign-in throttle)."""
    state = request.app.state
    with _LIMITER_LOCK:
        found = getattr(state, _LIMITER_SLOT, None)
        if not isinstance(found, CardRateLimiter):
            found = CardRateLimiter(limit)
            setattr(state, _LIMITER_SLOT, found)
        return found


def _key_hash(request: Request, settings: Any, key: str) -> str:
    from engine.audit.events import principal_hash
    from engine.privacy.config import privacy_salt
    from engine.privacy.consent import principal_key

    salt = privacy_salt(settings, engine=get_platform_engine(request))
    return principal_hash(principal_key(key), salt=salt)


@router.post(
    "/agentic/save-desk/card",
    response_model=SaveDeskCard,
    responses=_ERRORS,
    summary="A customer's save-desk card: reasons, offer, net value and a do-not-discount flag, each labelled",
)
def save_desk_card(
    body: CardRequest,
    request: Request,
    response: Response,
    storage: StorageDep,
    root: ConfigRootDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> SaveDeskCard:
    """The card for `body.customer_key` in `body.run_id`'s treat list. Reads only; the key is not echoed."""
    set_audit_context(
        request,
        object_id=body.run_id,
        object_type="run",
        details={"run_id": body.run_id, "principal_hash": _key_hash(request, settings, body.customer_key)},
    )
    response.headers["Cache-Control"] = "no-store"
    try:
        if not card_access_allowed(load_pilot(root), principal.user_id):
            raise AgenticError(
                "SAVE_DESK_NOT_IN_PILOT_ARM",
                "Cards in this pilot are given to staff chosen in advance, and you are not among them.",
                status=403,
            )
        wait = _limiter(request, load_settings(root).rate_limit_per_minute).allow(principal.user_id)
        if wait is not None:
            raise AgenticError(
                "SAVE_DESK_RATE_LIMITED",
                f"Too many cards asked for in a minute. Try again in {wait} seconds.",
                status=429,
                detail={"retry_after": wait},
            )
        return build_card(storage, body)
    except AgenticError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        error = _http(exc)
        if exc.status == 429:
            error.headers = {"Retry-After": str(exc.detail["retry_after"])}
        raise error from exc
