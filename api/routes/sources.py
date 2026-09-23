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
  to measure it against. `resync_join_coverage` is the single place that decides what each stored
  profile's `coverage` says, and every route that can change the answer calls it: uploading a
  source, confirming or changing a role, and deleting one. It derives the number from the client's
  sources *as they now are*, which is what keeps the badge honest in the direction that is easy to
  miss - when the entity source is deleted, or its role moved to something else, there is nothing
  left to have measured against, so the number is cleared back to `None` (an em dash on screen)
  rather than left on display as a measurement of a table that is no longer there.

`profile_source` reads only a `UseCaseConfig`'s `.catalog` (its own docstring says so - the same
root-wide `Catalog` every use case in one root shares, DEC-038), never anything use-case-specific,
which is exactly why this route can call it before any use case is named: `any_use_case_config`
below loads whichever use case sorts first, purely to reach that shared catalogue.
"""

from __future__ import annotations

import secrets
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

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
    refused never touches storage, and anything that fails *after* the first byte lands unwinds
    through `discard_source`, so no half-registered source survives a failed upload.

    The profile returned is re-read from storage rather than returned from memory, so the body of
    this `201` is the same document `GET /clients/{id}/sources` will serve - including the join
    coverage `resync_join_coverage` may have just measured onto it. `uploads.py` keeps the same
    promise for `profile.json`, and it is what stops a badge from reading one way on the upload
    screen and another way on the mapping screen.
    """
    load_client(store, client_id)
    roles = get_roles(root)
    if role is not None:
        require_role(roles, role)
    limits = onboarding_limits(root)
    held = len(store.list_sources(client_id))
    if held >= limits.max_sources:
        raise http_error(
            409,
            "TOO_MANY_SOURCES",
            f"This client already has {held} source{'' if held == 1 else 's'}, and this engine "
            f"allows {limits.max_sources}. Remove one before adding another.",
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
        try:
            result = ingest.read_upload(
                storage,
                raw_key,
                file_format=file_format,
                row_cap=min(limits.max_source_rows, PROFILE_ROW_CAP),
            )
        except ingest.IngestError as exc:
            raise ingest_http(exc.code, exc.message) from exc

        if result.row_count > limits.max_source_rows:
            raise http_error(
                409,
                "SOURCE_TOO_LARGE",
                f"This file has {result.row_count:,} rows, above the {limits.max_source_rows:,} "
                "row limit for one source. Split the file or raise the limit before uploading it again.",
            )

        file_name = file.filename or f"source.{file_format}"
        profile = profile_source(
            result.frame,
            any_use_case_config(root),
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
        store.add_source(client_id, spec)
        storage.write_model(source_profile_key(client_id, source_id), profile)
        resync_join_coverage(
            storage,
            store,
            root=root,
            roles=roles,
            client_id=client_id,
            only=None if role == roles.entity_role else frozenset({source_id}),
        )
    except Exception:
        discard_source(storage, store, client_id=client_id, source_id=source_id, raw_key=raw_key)
        raise

    response.headers["Location"] = f"/clients/{client_id}/sources/{source_id}"
    return SourceCreateResponse(source_id=source_id, profile=load_profile(storage, client_id, source_id))


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
    """Confirm a role on both documents that carry it, then re-derive the client's join coverage.

    The role lives in two places by design - on the `SourceSpec` the registry lists and on the
    `SourceProfile` the mapping screen reads - so writing only one of them leaves the two halves of
    `GET /clients/{id}/sources` contradicting each other about a fact the user just settled. It is
    written here as `DecidedBy.USER` because this endpoint *is* the user deciding; a role the
    detector merely proposed stays in `role_candidates` where it can be told apart.

    Coverage is re-derived on every role change, not only when the new role is the entity: moving a
    role *away* from the entity leaves the client with nothing to measure against, and the stale
    number has to come off the badge (house rule 2) as surely as a fresh one has to go on it.
    """
    load_source(store, client_id, source_id)
    roles = get_roles(root)
    require_role(roles, body.role)
    updated = store.set_source_role(source_id, body.role)
    profile = load_profile(storage, client_id, source_id)
    storage.write_model(
        source_profile_key(client_id, source_id),
        profile.model_copy(update={"role": body.role, "role_decided_by": DecidedBy.USER}),
    )
    resync_join_coverage(storage, store, root=root, roles=roles, client_id=client_id)
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
def delete_source(
    client_id: str,
    source_id: str,
    storage: StorageDep,
    root: ConfigRootDep,
    store: ClientStoreDep,
) -> Response:
    """Remove the row and both stored files, then re-derive what is left of the client's coverage.

    Deleting the entity source is the case worth spelling out: every other source's `coverage` was
    measured against the table that just went away, so leaving those numbers on file would show the
    user a measurement of something that no longer exists.
    """
    source = load_source(store, client_id, source_id)
    store.delete_source(source_id)
    storage.delete(source.storage_key)
    storage.delete(source_profile_key(client_id, source_id))
    resync_join_coverage(storage, store, root=root, roles=get_roles(root), client_id=client_id)
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
            500,
            "NO_USE_CASES_CONFIGURED",
            "This engine has no use cases configured, so there is nothing to profile a source "
            "against. Add a use case to the configuration before uploading client data.",
        )
    return load_use_case(ids[0], root)


def require_role(roles: RoleCatalogue, role: str) -> None:
    """`409 ROLE_UNKNOWN` when `role` is not in `configs/roles.yaml` (a data problem, not a 404/422:
    the request named a real source, just an impossible role for it)."""
    if role not in roles.roles:
        raise http_error(
            409,
            "ROLE_UNKNOWN",
            f"{role!r} is not a kind of table this engine knows. "
            f"Choose one of: {', '.join(roles.names)}.",
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
def entity_key_column(entity_profile: SourceProfile) -> str | None:
    """The entity source's best-ranked key candidate - no mapping is confirmed yet at upload time,
    so this is the same heuristic best guess the mapping screen will show the user first."""
    return entity_profile.key_candidates[0].column if entity_profile.key_candidates else None


def resync_join_coverage(
    storage: Storage,
    store: ClientStore,
    *,
    root: Path,
    roles: RoleCatalogue,
    client_id: str,
    only: frozenset[str] | None = None,
) -> None:
    """Re-derive `KeyCandidate.coverage` on this client's stored profiles from the sources as they
    now are, and rewrite the ones whose answer changed.

    One function rather than one per direction, because coverage is not a fact about the moment a
    source was uploaded - it is a fact about the client's current set of sources, and it changes
    from *every* side: a source arrives, a role is confirmed, a role moves, a source is deleted.
    Deriving it from scratch each time is the only version of this that cannot drift; the three
    cases the earlier add-only version got wrong (the entity deleted, its role moved elsewhere, a
    previously-measured source becoming the entity itself) all fall out of it for free.

    Coverage is a measurement or it is `None` - never a leftover. A source is measurable only when
    the client has a confirmed entity source, that entity has a key column to compare against, and
    the source is not that entity; anything else clears the number back to `None`, which the UI
    renders as an em dash (house rule 2).

    `only` narrows the *rewrite* to named sources - the upload path uses it, because adding one
    non-entity source cannot change any other source's answer and re-reading every file to confirm
    that would make each upload cost a full pass over the client's data. It never narrows what is
    measured *against*: the entity is always resolved from the whole list, newest first, so when two
    sources are both confirmed as the entity - which nothing forbids, since which table is the
    entity is the user's to change - the more recent confirmation is the one that counts.
    """
    sources = store.list_sources(client_id)
    entity = next((source for source in sources if source.role == roles.entity_role), None)
    entity_keys: pd.Series[Any] | None = None
    reader: FileSourceReader | None = None
    if entity is not None:
        column = entity_key_column(load_profile(storage, client_id, entity.source_id))
        if column is not None:
            reader = FileSourceReader(storage, any_use_case_config(root))
            entity_frame = reader.read(entity, max_rows=None)
            if column in entity_frame.columns:
                entity_keys = entity_frame[column]
    for source in sources:
        if only is not None and source.source_id not in only:
            continue
        profile = load_profile(storage, client_id, source.source_id)
        if not profile.key_candidates:
            continue
        if entity is None or entity_keys is None or reader is None or source.source_id == entity.source_id:
            updated = cleared_coverage(profile.key_candidates)
        else:
            updated = measured_coverage(
                profile.key_candidates, reader.read(source, max_rows=None), entity_keys
            )
        if updated != profile.key_candidates:
            storage.write_model(
                source_profile_key(client_id, source.source_id),
                profile.model_copy(update={"key_candidates": updated}),
            )


def measured_coverage(
    candidates: tuple[KeyCandidate, ...], frame: pd.DataFrame, entity_keys: pd.Series[Any]
) -> tuple[KeyCandidate, ...]:
    """`candidates` with each one's share of keys found in `entity_keys` measured on `frame`.

    `frame` is the source's whole table, read for this comparison rather than reused from the
    profiling pass: that pass stops at `PROFILE_ROW_CAP` rows, and a share of the first two million
    rows presented as "share of this source's rows" would be a number that means something other
    than what the badge says it means.

    A candidate whose column is not in the frame at all is cleared rather than left alone, so a
    stale number can never outlive the column it was measured on.
    """
    return tuple(
        (
            candidate.model_copy(update={"coverage": join_coverage(frame[candidate.column], entity_keys)})
            if candidate.column in frame.columns
            else candidate.model_copy(update={"coverage": None})
        )
        for candidate in candidates
    )


def cleared_coverage(candidates: tuple[KeyCandidate, ...]) -> tuple[KeyCandidate, ...]:
    """`candidates` with every `coverage` back to `None` - there is nothing to measure against."""
    return tuple(
        candidate if candidate.coverage is None else candidate.model_copy(update={"coverage": None})
        for candidate in candidates
    )


def discard_source(
    storage: Storage, store: ClientStore, *, client_id: str, source_id: str, raw_key: str
) -> None:
    """Unwind a half-finished upload: both storage keys and the registry row, if they got that far.

    `api.routes.uploads.delete_upload`'s promise, kept for a source: whatever failed between the
    first byte landing and the last write finishing, `GET /clients/{id}/sources` must not afterwards
    show a source with no file, or a row whose profile was never written. The registry row may
    legitimately not exist yet, which is why its absence is suppressed rather than reported.
    """
    storage.delete(raw_key)
    storage.delete(source_profile_key(client_id, source_id))
    with suppress(ClientStoreError):
        store.delete_source(source_id)


__all__ = ["router"]
