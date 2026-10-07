"""The holdout's own routes (Plan J M92): see it, and start a new epoch.

`GET /holdout` (Viewer) answers what the deployment's holdout is: whether the salt is set and
whether it is the recorded one (by fingerprint, never the salt), every persistent holdout's ledger
entry (scope, epoch, the fraction it has reached, when the epoch started), and each use case's
configured scope, effective fraction and explore share. It reads configuration and the platform
database only: no customer data.

`PUT /holdout` (Admin, audited by the middleware) starts a new epoch of one persistent holdout at a
given fraction - the only way to lower a fraction, which scoring otherwise refuses with
`HOLDOUT_FRACTION_LOWERED`, and the only way to change the universal share - and, with `rotate_salt`,
adopts the currently configured salt, which starts a new epoch of every persistent holdout
(`engine.holdout.salt`, DEC-1302 (b)). It refuses (409) a rotation to the salt already recorded
(`HOLDOUT_SALT_UNCHANGED`), and a universal epoch at a share some universal use case does not declare
(`HOLDOUT_FRACTION_MISMATCH`: the universal share is one number for every use case on it). A use case
named `universal` or `explore` cannot have its own holdout (`HOLDOUT_SCOPE_RESERVED`, 422). The audit event
carries the ledger's hash before and after, the use case, the new epoch (`count`) and what was done
(`reason_code`). The scope and fraction a use case *asks for* stay in its configuration: an epoch is
bookkeeping about the holdout, not a second place to set it.
"""

from __future__ import annotations

from typing import Final, Self

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.access import get_platform_engine, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.audit.events import content_hash
from engine.config import ConfigError, list_use_case_ids, load_use_case
from engine.holdout.salt import (
    HoldoutLedger,
    configured_salt,
    salt_fingerprint,
    salt_id,
    start_epoch,
)
from engine.holdout.spec import (
    HOLDOUT_FRACTION_MISMATCH,
    HOLDOUT_SALT_CHANGED,
    HOLDOUT_SALT_UNCHANGED,
    MAX_HOLDOUT_FRACTION,
    UNIVERSAL_SCOPE_KEY,
    HoldoutError,
    HoldoutLedgerEntry,
    HoldoutScope,
    PersistentScope,
    effective_holdout_fraction,
    ledger_key,
    reserved_scope_error,
)
from engine.utils.time import utc_now

__all__ = ["POLICIES", "HoldoutUpdateRequest", "HoldoutUseCase", "HoldoutView", "router"]

router: APIRouter = APIRouter(tags=["holdout"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/holdout"): RoutePolicy(role=Role.VIEWER, action="holdout.read", purpose="see the holdout"),
    ("PUT", "/holdout"): RoutePolicy(
        role=Role.ADMIN,
        action="holdout.update",
        object_type="holdout",
        purpose="start a new holdout epoch",
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


class HoldoutUseCase(BaseModel):
    """One use case's holdout, as its configuration asks for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    use_case_id: str = Field(description="The use case.")
    scope: HoldoutScope = Field(description="actions.holdout.scope: run, use_case or universal.")
    fraction: float = Field(description="The effective holdout share (effective_holdout_fraction).")
    explore_fraction: float = Field(description="actions.explore_fraction.")
    epoch: int | None = Field(
        description="The current epoch of its persistent holdout; null under run or before first use."
    )
    epoch_fraction: float | None = Field(
        description=(
            "The share its persistent holdout has reached in that epoch; scoring is refused while this "
            "differs from fraction (lower, or any difference under universal). Null under run or before "
            "first use."
        )
    )


class HoldoutView(BaseModel):
    """`GET /holdout`: the salt's state, the ledger and each use case's holdout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    salt_configured: bool = Field(description="Whether MARKETING_AI_HOLDOUT_SALT is set.")
    salt_id: str | None = Field(description="The configured salt's short fingerprint; never the salt.")
    recorded_salt_id: str | None = Field(
        description="The fingerprint the holdout was drawn with; null before first use."
    )
    salt_matches: bool | None = Field(
        description="Whether the configured salt is the recorded one; null when either is missing."
    )
    holdouts: tuple[HoldoutLedgerEntry, ...] = Field(description="Every persistent holdout's current epoch.")
    use_cases: tuple[HoldoutUseCase, ...] = Field(description="Each use case's configured holdout.")


class HoldoutUpdateRequest(BaseModel):
    """`PUT /holdout`: start a new epoch of one persistent holdout (and, optionally, adopt a new salt)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    scope: PersistentScope = Field(description="use_case or universal.")
    use_case_id: str | None = Field(
        default=None, description="Required with scope use_case; absent with universal."
    )
    fraction: float = Field(gt=0.0, le=MAX_HOLDOUT_FRACTION, description="The new epoch's holdout share.")
    rotate_salt: bool = Field(
        default=False,
        description="Adopt the configured salt as the new one; every persistent holdout starts a new epoch.",
    )

    @model_validator(mode="after")
    def _key_matches_scope(self) -> Self:
        if self.scope == "use_case" and not self.use_case_id:
            raise ValueError("use_case_id is required with scope use_case")
        if self.scope == "universal" and self.use_case_id:
            raise ValueError("use_case_id must be absent with scope universal")
        return self


def _view(request: Request, settings: SettingsDep, root: ConfigRootDep) -> HoldoutView:
    ledger = HoldoutLedger(get_platform_engine(request))
    salt = configured_salt(settings)
    current = None if salt is None else salt_id(salt_fingerprint(salt))
    stored = ledger.fingerprint()
    recorded = None if stored is None else salt_id(stored)
    entries = ledger.entries()
    by_key = {(entry.scope, entry.scope_key): entry for entry in entries}
    use_cases: list[HoldoutUseCase] = []
    for use_case_id in list_use_case_ids(root):
        try:
            config = load_use_case(use_case_id, root)
        except ConfigError:
            continue  # a use case that does not load has no holdout to show; its own screens say why
        scope = config.actions.holdout.scope
        key = use_case_id if scope == "use_case" else UNIVERSAL_SCOPE_KEY
        entry = None if scope == "run" else by_key.get((scope, key))
        use_cases.append(
            HoldoutUseCase(
                use_case_id=use_case_id,
                scope=scope,
                fraction=effective_holdout_fraction(config.actions),
                explore_fraction=float(config.actions.explore_fraction),
                epoch=None if entry is None else entry.epoch,
                epoch_fraction=None if entry is None else entry.fraction,
            )
        )
    return HoldoutView(
        salt_configured=salt is not None,
        salt_id=current,
        recorded_salt_id=recorded,
        salt_matches=None if current is None or recorded is None else current == recorded,
        holdouts=entries,
        use_cases=tuple(use_cases),
    )


def _universal_mismatches(root: ConfigRootDep, fraction: float) -> list[str]:
    """The use cases on the universal holdout whose configured share is not `fraction`."""
    found: list[str] = []
    for use_case_id in list_use_case_ids(root):
        try:
            config = load_use_case(use_case_id, root)
        except ConfigError:
            continue  # a use case that does not load cannot score, so it cannot disagree
        actions = config.actions
        if actions.holdout.scope == "universal" and effective_holdout_fraction(actions) != fraction:
            found.append(use_case_id)
    return found


@router.get(
    "/holdout", response_model=HoldoutView, responses=_ERRORS, summary="The holdout: salt, epochs, use cases"
)
def read_holdout(request: Request, settings: SettingsDep, root: ConfigRootDep) -> HoldoutView:
    """The deployment's holdout; no customer data and never the salt."""
    return _view(request, settings, root)


@router.put(
    "/holdout",
    response_model=HoldoutView,
    responses=_ERRORS,
    summary="Start a new holdout epoch (Admin): a lower share, or a new salt",
)
def update_holdout(
    body: HoldoutUpdateRequest,
    request: Request,
    settings: SettingsDep,
    root: ConfigRootDep,
) -> HoldoutView:
    """A new epoch of one persistent holdout; `rotate_salt` also adopts the configured salt everywhere."""
    key = UNIVERSAL_SCOPE_KEY
    if body.scope == "use_case":
        key = str(body.use_case_id)
        reserved = reserved_scope_error(body.scope, key)
        if reserved is not None:
            set_audit_context(request, details={"reason_code": reserved.code})
            raise http_error(422, reserved.code, reserved.message, "use_case_id")
        if key not in set(list_use_case_ids(root)):
            raise http_error(404, "USE_CASE_NOT_FOUND", f"Unknown use case: {key}.", "use_case_id")
    else:
        others = _universal_mismatches(root, body.fraction)
        if others:
            set_audit_context(request, details={"reason_code": HOLDOUT_FRACTION_MISMATCH})
            raise http_error(
                409,
                HOLDOUT_FRACTION_MISMATCH,
                f"The universal holdout is one share for every use case on it, and {', '.join(others)} "
                f"declare{'s' if len(others) == 1 else ''} a different one than {body.fraction:.0%}. Set "
                "actions.holdout.fraction of every universal use case to the new share first.",
                "fraction",
            )
    ledger = HoldoutLedger(get_platform_engine(request))
    before = content_hash(ledger.snapshot())
    try:
        entry = start_epoch(
            ledger,
            scope=body.scope,
            key=key,
            fraction=body.fraction,
            settings=settings,
            rotate_salt=body.rotate_salt,
            at=utc_now(),
        )
    except HoldoutError as exc:
        set_audit_context(request, details={"reason_code": exc.code})
        status = 409 if exc.code in (HOLDOUT_SALT_CHANGED, HOLDOUT_SALT_UNCHANGED) else 503
        raise http_error(status, exc.code, exc.message) from exc
    set_audit_context(
        request,
        object_id=ledger_key(entry.scope, entry.scope_key),
        object_type="holdout",
        before_hash=before,
        after_hash=content_hash(ledger.snapshot()),
        details={
            "use_case_id": key if body.scope == "use_case" else None,
            "setting": "actions.holdout",
            "count": entry.epoch,
            "reason_code": "HOLDOUT_SALT_ROTATED" if body.rotate_salt else "HOLDOUT_NEW_EPOCH",
        },
    )
    return _view(request, settings, root)
