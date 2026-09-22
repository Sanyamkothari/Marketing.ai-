"""`POST /clients/{id}/mappings/suggest`, `PUT /clients/{id}/mappings/{mid}` and
`GET /clients/{id}/mappings` (Phase 2 plan §9, M12).

Three steps of the mapping screen, each a thin skin over a store call and one engine function, in
the house shape `api/routes/uploads.py` and `api/routes/clients.py` already use: business rules live
in `engine.onboarding.mapping`/`engine.onboarding.validate` and are tested there; this module's job
is the M1 error envelope, loading what a request names, and 404ing what it does not find.

* **Suggest** reads a source's stored profile and a use case's standard schema and returns a
  `MappingSpec` nobody has saved yet - the UI shows it, the user edits columns, transforms and value
  maps, and only then is anything written. Generating a fresh `mapping_id` here rather than at save
  time is what lets `PUT /clients/{id}/mappings/{mid}` be the *same* id the suggestion carried,
  exactly as the endpoint table names it: the two calls describe one mapping's lifecycle, not two
  different documents.
* **Save** is `engine.clients.LocalClientStore.save_mapping`'s upsert, called with the id the URL
  names (never the body's, which this route ignores for exactly that field) so two people editing
  the same suggestion never fork it into two mappings. It returns the checks
  `engine.onboarding.validate.run_onboarding_checks` finds for this one mapping and its source -
  `JOIN_KEY_COVERAGE_LOW`, `MAPPING_LOW_CONFIDENCE`, `REQUIRED_STANDARD_COLUMN_UNMAPPED` and the
  rest of plan §7's mapping-level codes - so the mapping screen can show them the moment a user
  saves, well before anything is built.
* **List** is `list_mappings` with an M1 envelope around it; `use_case` narrows exactly as
  `GET /clients/{id}/onboarding-specs` and `GET /datasets` do.

`engine.onboarding.mapping` and `engine.onboarding.validate` are landing in parallel
(`PARALLEL_WORK_PROTOCOL.md`) and did not exist when this module was written: the two names imported
below, `suggested_mapping_spec` and `run_onboarding_checks`, are this branch's best-effort reading of
the signature their own docstrings would need to carry, given what `MappingSpec`/`SourceProfile`
already fix in `engine/onboarding/specs.py` (`suggested_mapping_spec` reads `client_id`/`source_id`
off the `SourceProfile` it is handed rather than taking them twice; `run_onboarding_checks` takes the
same `sources`/`mappings`/`spec` mapping shape `engine.onboarding.datasets.lineage` already takes,
for the same reason lineage gives - a check about a mapping this call was not handed a profile for
is not a check this module can vouch for). `HeuristicMappingSuggester` and `apply_mapping`, the other
two names `engine.onboarding.mapping` is expected to carry, are not referenced here: suggestion is
this route's whole job and `suggested_mapping_spec` is its one entry point (the same split
`engine/onboarding/sources.py` already draws between `profile_source`, the entry point every caller
uses, and the role/key/time detectors it calls internally); applying a mapping to real data belongs
to the build path (`api/routes/datasets.py`), not to saving one. If either signature does not match
what lands, the fix is at this call site, not a reason to weaken this module - see the test module's
own docstring.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, ValidationError

from api.deps import ConfigRootDep, StorageDep
from api.routes.clients import ClientStoreDep, load_client
from api.routes.sources import load_profile, load_source
from api.routes.uploads import http_error, use_case_config
from api.schemas import ErrorResponse
from engine.clients import ClientStore, ClientStoreError
from engine.config import StrictBase
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.specs import MappingColumn, MappingSpec, OnboardingCheck
from engine.onboarding.validate import run_onboarding_checks
from engine.utils.time import utc_now

router: APIRouter = APIRouter(tags=["mappings"])

_MAPPING_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}

UseCaseQuery = Annotated[str | None, Query(description="Keep only mappings of this use case.")]


# ---------------------------------------------------------------------------
# Request/response models (api/schemas.py is shared; ours live beside the routes that use them)
# ---------------------------------------------------------------------------
class MappingSuggestRequest(StrictBase):
    """Body of `POST /clients/{id}/mappings/suggest`."""

    source_id: str
    use_case: str


class MappingSaveRequest(StrictBase):
    """Body of `PUT /clients/{id}/mappings/{mid}`: the user's decisions for a mapping already
    suggested for this source, saved back under the id the suggestion carried.

    `mapping_id` is deliberately absent - the URL already names it, and a body that could name a
    *different* id than the one it is being saved under is exactly the two-writers-fork-one-mapping
    problem the module docstring describes. `created_at` and `hash` are also absent: the store stamps
    both (`LocalClientStore.save_mapping` calls `MappingSpec.with_hash()`), so this request carries
    only what a human actually decided.
    """

    client_id: str
    source_id: str
    use_case: str
    role: str
    columns: tuple[MappingColumn, ...]
    unmapped_source: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    value_maps: dict[str, dict[str, Any]] = Field(default_factory=dict)


class MappingSaveResponse(StrictBase):
    """Body of the `200` from `PUT /clients/{id}/mappings/{mid}`."""

    mapping_id: str
    checks: tuple[OnboardingCheck, ...]


class MappingListResponse(StrictBase):
    """Body of `GET /clients/{id}/mappings`: one client's mappings, newest first."""

    mappings: tuple[MappingSpec, ...]


# ---------------------------------------------------------------------------
# POST /clients/{id}/mappings/suggest
# ---------------------------------------------------------------------------
@router.post(
    "/clients/{client_id}/mappings/suggest",
    response_model=MappingSpec,
    responses=_MAPPING_ERRORS,
    summary="Suggest a mapping of one source against one use case's standard schema; nothing is saved",
)
def suggest_mapping(
    client_id: str,
    body: MappingSuggestRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    store: ClientStoreDep,
) -> MappingSpec:
    """A fresh, unsaved `MappingSpec` the mapping screen renders and the user edits.

    Ordered so every check that can be answered without reading the source's profile runs first:
    the client exists, the use case is real and loaded, the source exists and belongs to this
    client, and its role has been confirmed - a mapping is "this source's columns against the
    standard schema for *this* role", so a source nobody has told the engine the role of has
    nothing to be suggested against yet (`SOURCE_ROLE_UNCONFIRMED`, not a guess at one).
    """
    load_client(store, client_id)
    config = use_case_config(body.use_case, root)
    source = load_source(store, client_id, body.source_id)
    if source.role is None:
        raise http_error(
            409,
            "SOURCE_ROLE_UNCONFIRMED",
            f"{source.source_id!r} has no confirmed role yet. Confirm one "
            f"(PATCH /clients/{client_id}/sources/{source.source_id}) before mapping it.",
        )
    profile = load_profile(storage, client_id, source.source_id)
    return suggested_mapping_spec(
        profile, config, role=source.role, use_case=body.use_case, mapping_id=new_mapping_id()
    )


# ---------------------------------------------------------------------------
# PUT /clients/{id}/mappings/{mid}
# ---------------------------------------------------------------------------
@router.put(
    "/clients/{client_id}/mappings/{mapping_id}",
    response_model=MappingSaveResponse,
    responses=_MAPPING_ERRORS,
    summary="Save a user's mapping decisions and return the checks they imply",
)
def save_mapping(
    client_id: str,
    mapping_id: str,
    body: MappingSaveRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    store: ClientStoreDep,
) -> MappingSaveResponse:
    """Persist `body` under `mapping_id` (an upsert - see `engine.clients.LocalClientStore.save_mapping`)
    and return the mapping-level checks it now implies.

    `body.client_id` must agree with the URL, and `body.role` with the source's own confirmed role:
    both are carried on the body because `MappingSpec` needs them, not because either is this
    request's to change - `PATCH /clients/{id}/sources/{sid}` is where a role is decided, and a
    mismatch here is refused rather than silently overwritten (house rule 2 extends to a field that
    would otherwise drift out from under the record that owns it).
    """
    load_client(store, client_id)
    if body.client_id != client_id:
        raise http_error(
            409,
            "CLIENT_MISMATCH",
            f"This mapping is for client {body.client_id!r}, not {client_id!r}.",
        )
    use_case_config(body.use_case, root)
    source = load_source(store, client_id, body.source_id)
    if body.role != source.role:
        raise http_error(
            409,
            "MAPPING_ROLE_MISMATCH",
            f"{source.source_id!r} is confirmed as {source.role!r}, but this mapping says {body.role!r}. "
            "Re-confirm the source's role, or suggest the mapping again, before saving.",
        )
    mapping = build_mapping_spec(mapping_id, client_id, body)
    saved = store.save_mapping(mapping)
    profile = load_profile(storage, client_id, source.source_id)
    checks = run_onboarding_checks(sources={source.source_id: profile}, mappings={saved.mapping_id: saved})
    return MappingSaveResponse(mapping_id=saved.mapping_id, checks=checks)


# ---------------------------------------------------------------------------
# GET /clients/{id}/mappings
# ---------------------------------------------------------------------------
@router.get(
    "/clients/{client_id}/mappings",
    response_model=MappingListResponse,
    responses=_NOT_FOUND,
    summary="A client's saved mappings, newest first",
)
def list_mappings(
    client_id: str, store: ClientStoreDep, use_case: UseCaseQuery = None
) -> MappingListResponse:
    load_client(store, client_id)
    return MappingListResponse(mappings=store.list_mappings(client_id, use_case))


# ---------------------------------------------------------------------------
# Ids, construction and loading (also used by api/routes/datasets.py)
# ---------------------------------------------------------------------------
def new_mapping_id() -> str:
    """`map_<12 hex>` - the same shape as `api.routes.sources.new_source_id`, for the same reason:
    short, URL-safe and collision-free without a counter this route would have to coordinate."""
    return f"map_{secrets.token_hex(6)}"


def build_mapping_spec(mapping_id: str, client_id: str, body: MappingSaveRequest) -> MappingSpec:
    """`body` plus the id and timestamp the store's write owns, or a `422` naming the first problem.

    `MappingSpec`'s own validators (`_one_claim_each`: no source or standard column claimed twice,
    no overlap between mapped and deliberately-unmapped columns) run on construction, so a body that
    fails them never reaches `store.save_mapping`; `exc.errors()[0]["msg"]` is already the
    business-language sentence the validator raised, exactly as `engine.config`'s own `ConfigError`
    translation reads it (plan section 13, rule 3 - a code, a business-language message, no
    traceback).
    """
    try:
        return MappingSpec(
            mapping_id=mapping_id,
            client_id=client_id,
            source_id=body.source_id,
            use_case=body.use_case,
            role=body.role,
            columns=body.columns,
            unmapped_source=body.unmapped_source,
            missing_required=body.missing_required,
            value_maps=body.value_maps,
            created_at=utc_now(),
        )
    except ValidationError as exc:
        raise http_error(422, "MAPPING_INVALID", str(exc.errors()[0]["msg"])) from exc


def load_mapping(store: ClientStore, client_id: str, mapping_id: str) -> MappingSpec:
    """One mapping belonging to `client_id`, or a `404 MAPPING_NOT_FOUND`.

    A mapping that exists but belongs to a *different* client answers the same 404, exactly as
    `api.routes.sources.load_source` hides a cross-client id behind one not-found rather than a
    mismatch that would confirm the id is real.
    """
    try:
        mapping = store.get_mapping(mapping_id)
    except ClientStoreError as exc:
        raise mapping_not_found(mapping_id) from exc
    if mapping.client_id != client_id:
        raise mapping_not_found(mapping_id)
    return mapping


def mapping_not_found(mapping_id: str) -> HTTPException:
    return http_error(404, "MAPPING_NOT_FOUND", f"No mapping with id {mapping_id!r}.")


__all__ = ["router"]
