"""`POST/GET/PATCH/DELETE /clients/{id}/sources` (Phase 2 plan §9, M12).

Follows `api/routes/uploads.py` for everything a raw-file endpoint has to get right: the file is
streamed into `Storage` rather than held in memory, an unsupported suffix is a `415` before a byte
is written, a failure part-way through leaves no orphan key behind, and every error is M1's
envelope. What is new here, because a *source* is not an *upload*, is layered on top of that:

* `config.onboarding.limits` (`SOURCE_TOO_LARGE`, `TOO_MANY_SOURCES`) are enforced as `409`s, in
  plain language, because a client's raw table is read on a laptop, not a warehouse. They are read
  straight off `engine.yaml:defaults.onboarding.limits` rather than a merged `UseCaseConfig`,
  because this endpoint names no use case: one client's sources can feed several use cases.
* A source carries a `role` - what kind of table it is - confirmed here (an explicit form field on
  upload, or `PATCH`) or left for the user to settle from the ranked `role_candidates`
  `engine.onboarding.sources.profile_source` already attaches. An unconfirmed role is `None`, never
  guessed into the record (house rule 2: nothing fabricated reaches a field that means "the user
  decided this").
* Once a client has a confirmed *entity* source, every other source's `key_candidates` carry a real
  `coverage` number against it (`engine.onboarding.sources.join_coverage`) - the join-coverage badge
  the mapping screen shows is a measurement, not a placeholder, from the moment there is something
  to measure it against. `with_join_coverage`/`refresh_other_sources_against_new_entity` recompute it
  in both directions: for the source just added, against whichever entity source already exists, and
  for every source already on file, the moment one of them is confirmed as the entity.

`profile_source` reads only a `UseCaseConfig`'s `.catalog` (its own docstring says so - the same
root-wide `Catalog` every use case in one root shares, DEC-038), never anything use-case-specific,
which is exactly why this route can call it before any use case is named: `any_use_case_config`
below loads whichever use case sorts first, purely to reach that shared catalogue.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from api.deps import ConfigRootDep, StorageDep
from api.routes.clients import ClientStoreDep, load_client
from api.routes.uploads import CHUNK_BYTES, http_error, ingest_http
from api.schemas import ErrorResponse
from engine.clients import ClientStore, ClientStoreError
from engine.config import (
    OnboardingLimits,
    RoleCatalogue,
    StrictBase,
    UseCaseConfig,
    get_roles,
    list_use_case_ids,
    load_engine_config,
    load_use_case,
)
from engine.onboarding.sources import FileSourceReader, join_coverage, profile_source
from engine.onboarding.specs import DecidedBy, KeyCandidate, SourceProfile, SourceSpec
from engine.stages import ingest
from engine.storage import Storage, StorageError
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

router: APIRouter = APIRouter(tags=["sources"])

FileField = Annotated[UploadFile, File(description="The client's raw CSV or Parquet table.")]
RoleField = Annotated[
    str | None, Form(description="Confirmed role, when the caller already knows it; left unset otherwise.")
]

_SOURCE_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}
_PATCH_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}

PROFILE_ROW_CAP: Final[int] = 2_000_000
"""Rows actually held for profiling stats; independent of `max_source_rows`, which is the reject
line. Mirrors `api.routes.uploads.DEFAULT_PROFILE_ROW_CAP` so a huge configured limit (the default
is fifty million) never asks the profiler to hold fifty million rows just to measure that it should
have said no. `ingest.read_upload`'s row count is exact regardless of this cap (design §1.3), so
`SOURCE_TOO_LARGE` is judged on the real total even when the profile itself is built from a sample."""


class SourceCreateResponse(StrictBase):
    """Body of the `201` from `POST /clients/{id}/sources`."""

    source_id: str
    profile: SourceProfile


class SourceListResponse(StrictBase):
    """Body of `GET /clients/{id}/sources`: the registry rows and their stored profiles, by id."""

    sources: tuple[SourceSpec, ...]
    profiles: dict[str, SourceProfile]


class SourceRoleUpdate(StrictBase):
    """Body of `PATCH /clients/{id}/sources/{sid}`."""

    role: str


# ---------------------------------------------------------------------------
# POST /clients/{id}/sources
# ---------------------------------------------------------------------------
@router.post(
    "/clients/{client_id}/sources",
    response_model=SourceCreateResponse,
    status_code=201,
    responses=_SOURCE_ERRORS,
    summary="Store a client's raw table, profile it and propose its role",
)
async def create_source(
    client_id: str,
    storage: StorageDep,
    root: ConfigRootDep,
    store: ClientStoreDep,
    response: Response,
    file: FileField,
    role: RoleField = None,
) -> SourceCreateResponse:
    """Stream the file in, profile it, detect its role and persist `clients/<id>/sources/<sid>/`.

    Ordered exactly as `create_upload` orders it (design intent shared with `uploads.py`): every
    check that can be answered before a byte is written - the client exists, the role (if given) is
    real, the source count is under the limit - runs first, so a request that was always going to be
    refused never touches storage.
    """
    load_client(store, client_id)
    roles = get_roles(root)
    if role is not None:
        require_role(roles, role)
    limits = onboarding_limits(root)
    if len(store.list_sources(client_id)) >= limits.max_sources:
        raise http_error(
            409,
            "TOO_MANY_SOURCES",
            f"{client_id} already has {limits.max_sources} sources, the most this engine allows. "
            "Remove one before adding another.",
        )
    try:
        file_format = ingest.file_format_for(file.filename or "")
    except ingest.IngestError as exc:
        raise ingest_http(exc.code, exc.message) from exc

    source_id = new_source_id()
    raw_key = source_raw_key(client_id, source_id, file_format)
    with storage.open_write(raw_key) as sink:
        while chunk := await file.read(CHUNK_BYTES):
            sink.write(chunk)

    try:
        result = ingest.read_upload(
            storage, raw_key, file_format=file_format, row_cap=min(limits.max_source_rows, PROFILE_ROW_CAP)
        )
    except ingest.IngestError as exc:
        storage.delete(raw_key)
        raise ingest_http(exc.code, exc.message) from exc

    if result.row_count > limits.max_source_rows:
        storage.delete(raw_key)
        raise http_error(
            409,
            "SOURCE_TOO_LARGE",
            f"{file.filename} has {result.row_count:,} rows, above the {limits.max_source_rows:,} "
            "row limit for one source. Split the file or raise the limit before uploading it again.",
        )

    use_case = any_use_case_config(root)
    file_name = file.filename or f"source.{file_format}"
    profile = profile_source(
        result.frame,
        use_case,
        source_id=source_id,
        client_id=client_id,
        file_name=file_name,
        file_format=result.file_format,
        file_size_bytes=storage.size_bytes(raw_key),
        delimiter=result.delimiter,
        encoding=result.encoding,
        row_count=result.row_count,
        fingerprint=result.fingerprint,
        roles=roles,
    )
    profile = profile.model_copy(
        update={"role": role, "role_decided_by": DecidedBy.USER if role is not None else None}
    )

    spec = SourceSpec(
        source_id=source_id,
        client_id=client_id,
        file_name=file_name,
        storage_key=raw_key,
        file_format=file_format,
        role=role,
        rows=profile.rows,
        columns=tuple(column.name for column in profile.profile.columns),
        fingerprint=profile.fingerprint,
        created_at=utc_now(),
    )

    if role != roles.entity_role:
        entity = find_entity_source(store, roles, client_id)
        if entity is not None:
            profile = with_join_coverage(
                storage, use_case=use_case, entity=entity, source_frame=result.frame, profile=profile
            )

    store.add_source(client_id, spec)
    storage.write_model(source_profile_key(client_id, source_id), profile)

    if role == roles.entity_role:
        refresh_other_sources_against_new_entity(
            storage, store, use_case=use_case, client_id=client_id, entity=spec
        )

    response.headers["Location"] = f"/clients/{client_id}/sources/{source_id}"
    return SourceCreateResponse(source_id=source_id, profile=profile)


# ---------------------------------------------------------------------------
# GET /clients/{id}/sources
# ---------------------------------------------------------------------------
@router.get(
    "/clients/{client_id}/sources",
    response_model=SourceListResponse,
    responses=_NOT_FOUND,
    summary="A client's sources and their stored profiles",
)
def list_sources(client_id: str, storage: StorageDep, store: ClientStoreDep) -> SourceListResponse:
    load_client(store, client_id)
    sources = store.list_sources(client_id)
    profiles = {source.source_id: load_profile(storage, client_id, source.source_id) for source in sources}
    return SourceListResponse(sources=sources, profiles=profiles)


# ---------------------------------------------------------------------------
# PATCH /clients/{id}/sources/{sid}
# ---------------------------------------------------------------------------
@router.patch(
    "/clients/{client_id}/sources/{source_id}",
    response_model=SourceSpec,
    responses=_PATCH_ERRORS,
    summary="Confirm (or change) one source's role",
)
def update_source_role(
    client_id: str,
    source_id: str,
    body: SourceRoleUpdate,
    storage: StorageDep,
    root: ConfigRootDep,
    store: ClientStoreDep,
) -> SourceSpec:
    load_source(store, client_id, source_id)
    roles = get_roles(root)
    require_role(roles, body.role)
    updated = store.set_source_role(source_id, body.role)
    if body.role == roles.entity_role:
        use_case = any_use_case_config(root)
        refresh_other_sources_against_new_entity(
            storage, store, use_case=use_case, client_id=client_id, entity=updated
        )
    return updated


# ---------------------------------------------------------------------------
# DELETE /clients/{id}/sources/{sid}
# ---------------------------------------------------------------------------
@router.delete(
    "/clients/{client_id}/sources/{source_id}",
    status_code=204,
    responses=_NOT_FOUND,
    summary="Remove one source",
)
def delete_source(client_id: str, source_id: str, storage: StorageDep, store: ClientStoreDep) -> Response:
    source = load_source(store, client_id, source_id)
    store.delete_source(source_id)
    storage.delete(source.storage_key)
    storage.delete(source_profile_key(client_id, source_id))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Storage keys and ids
# ---------------------------------------------------------------------------
def source_raw_key(client_id: str, source_id: str, file_format: Literal["csv", "parquet"]) -> str:
    return f"clients/{client_id}/sources/{source_id}/raw.{file_format}"


def source_profile_key(client_id: str, source_id: str) -> str:
    return f"clients/{client_id}/sources/{source_id}/profile.json"


def new_source_id() -> str:
    """`src_<12 hex>`, the same shape as `engine.utils.ids.new_upload_id` for the same reason: short,
    URL-safe and collision-free without a counter this route would have to coordinate itself."""
    return f"src_{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# Limits, roles and the one UseCaseConfig profile_source needs
# ---------------------------------------------------------------------------
def onboarding_limits(root: Path) -> OnboardingLimits:
    """`engine.yaml:defaults.onboarding.limits`, read directly.

    A source is uploaded before any use case is named (`POST /clients/{id}/sources` takes no
    `use_case`, because one client's raw tables can feed several use cases), so the merged,
    per-use-case `OnboardingConfig` on `UseCaseConfig.onboarding` is not in scope here; the engine
    default is the only limit that could apply.
    """
    defaults = load_engine_config(root).defaults
    onboarding = defaults.get("onboarding") if isinstance(defaults, dict) else None
    limits = onboarding.get("limits") if isinstance(onboarding, dict) else None
    return OnboardingLimits.model_validate(limits or {})


def any_use_case_config(root: Path) -> UseCaseConfig:
    """Some real, loaded `UseCaseConfig` - needed only for its shared `.catalog` (DEC-038).

    A source is profiled before any use case has been chosen for it, but `profile_source` reads
    `config.catalog` alone (its own docstring says so), and every use case validated against one
    root shares that same `Catalog` object - so which one is loaded here never changes what gets
    profiled. Picking one is not fabricating a value house rule 2 would forbid: nothing use-case-
    specific about the loaded config is ever read.
    """
    ids = list_use_case_ids(root)
    if not ids:
        raise http_error(
            500, "NO_USE_CASES_CONFIGURED", "No use case is configured, so a source cannot be profiled."
        )
    return load_use_case(ids[0], root)


def require_role(roles: RoleCatalogue, role: str) -> None:
    """`409 ROLE_UNKNOWN` when `role` is not in `configs/roles.yaml` (a data problem, not a 404/422:
    the request named a real source, just an impossible role for it)."""
    if role not in roles.roles:
        raise http_error(
            409,
            "ROLE_UNKNOWN",
            f"{role!r} is not a role; configs/roles.yaml defines {', '.join(roles.names)}.",
        )


# ---------------------------------------------------------------------------
# Loading and 404s
# ---------------------------------------------------------------------------
def load_source(store: ClientStore, client_id: str, source_id: str) -> SourceSpec:
    """One source belonging to `client_id`, or a 404 `SOURCE_NOT_FOUND`.

    A source that exists but belongs to a *different* client answers the same 404: which clients
    exist is not this endpoint's business to reveal through a mismatched id.
    """
    try:
        source = store.get_source(source_id)
    except ClientStoreError as exc:
        raise source_not_found(source_id) from exc
    if source.client_id != client_id:
        raise source_not_found(source_id)
    return source


def load_profile(storage: Storage, client_id: str, source_id: str) -> SourceProfile:
    try:
        return storage.read_model(source_profile_key(client_id, source_id), SourceProfile)
    except StorageError as exc:
        raise source_not_found(source_id) from exc


def source_not_found(source_id: str) -> HTTPException:
    return http_error(404, "SOURCE_NOT_FOUND", f"No source with id {source_id!r}.")


# ---------------------------------------------------------------------------
# Join coverage
# ---------------------------------------------------------------------------
def find_entity_source(store: ClientStore, roles: RoleCatalogue, client_id: str) -> SourceSpec | None:
    """The client's confirmed entity source, or `None` while there is none."""
    for candidate in store.list_sources(client_id):
        if candidate.role == roles.entity_role:
            return candidate
    return None


def entity_key_column(entity_profile: SourceProfile) -> str | None:
    """The entity source's best-ranked key candidate - no mapping is confirmed yet at upload time,
    so this is the same heuristic best guess the mapping screen will show the user first."""
    return entity_profile.key_candidates[0].column if entity_profile.key_candidates else None


def with_join_coverage(
    storage: Storage,
    *,
    use_case: UseCaseConfig,
    entity: SourceSpec,
    source_frame: pd.DataFrame,
    profile: SourceProfile,
) -> SourceProfile:
    """`profile.key_candidates`, each carrying its measured coverage against `entity`.

    `source_frame` is the frame already read for `profile` - the new source's own upload - so
    nothing is re-read for its side of the comparison; only `entity`'s raw file is read here, once,
    through `FileSourceReader` (`engine.onboarding.sources`'s own seam for this).
    """
    if not profile.key_candidates:
        return profile
    entity_profile = load_profile(storage, entity.client_id, entity.source_id)
    column = entity_key_column(entity_profile)
    if column is None:
        return profile
    entity_frame = FileSourceReader(storage, use_case).read(entity, max_rows=None)
    if column not in entity_frame.columns:
        return profile
    entity_keys = entity_frame[column]
    updated: tuple[KeyCandidate, ...] = tuple(
        (
            candidate.model_copy(
                update={"coverage": join_coverage(source_frame[candidate.column], entity_keys)}
            )
            if candidate.column in source_frame.columns
            else candidate
        )
        for candidate in profile.key_candidates
    )
    return profile.model_copy(update={"key_candidates": updated})


def refresh_other_sources_against_new_entity(
    storage: Storage,
    store: ClientStore,
    *,
    use_case: UseCaseConfig,
    client_id: str,
    entity: SourceSpec,
) -> None:
    """When a source becomes the confirmed entity table, every other source's coverage badge turns
    from decorative to real for the first time - so each one is re-measured and rewritten now,
    rather than only ever measured against whichever entity source happened to exist first."""
    entity_profile = load_profile(storage, client_id, entity.source_id)
    column = entity_key_column(entity_profile)
    if column is None:
        return
    reader = FileSourceReader(storage, use_case)
    entity_frame = reader.read(entity, max_rows=None)
    if column not in entity_frame.columns:
        return
    entity_keys = entity_frame[column]
    for other in store.list_sources(client_id):
        if other.source_id == entity.source_id:
            continue
        profile = load_profile(storage, client_id, other.source_id)
        if not profile.key_candidates:
            continue
        other_frame = reader.read(other, max_rows=None)
        updated: tuple[KeyCandidate, ...] = tuple(
            (
                candidate.model_copy(
                    update={"coverage": join_coverage(other_frame[candidate.column], entity_keys)}
                )
                if candidate.column in other_frame.columns
                else candidate
            )
            for candidate in profile.key_candidates
        )
        if updated != profile.key_candidates:
            storage.write_model(
                source_profile_key(client_id, other.source_id),
                profile.model_copy(update={"key_candidates": updated}),
            )


__all__ = ["router"]
