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
  the same suggestion never fork it into two mappings. It then returns the checks over **every
  mapping this client has saved for this use case**, not over the one just written: almost every
  plan §7 mapping-level code is a statement about the set - `NO_ENTITY_SOURCE` and
  `MULTIPLE_ENTITY_SOURCES` are about which tables carry which role, and
  `REQUIRED_STANDARD_COLUMN_UNMAPPED` is satisfied by *any* table mapping the column - so a check run
  over one mapping alone would report a client whose other tables are fine as broken. The cost is a
  second list query per save; the benefit is that the mapping screen tells the truth the moment a
  user saves, well before anything is built.
* **List** is `list_mappings` with an M1 envelope around it; `use_case` narrows exactly as
  `GET /clients/{id}/onboarding-specs` and `GET /datasets` do.

`engine.onboarding.mapping` has since landed and `suggested_mapping_spec(profile, config, *, role,
use_case, mapping_id) -> MappingSpec` is the signature this module already called: the call below is
checked against the real function, not against a guess, and carries no `cast`.
`engine.onboarding.validate` has landed too, and it is *not* the shape an earlier draft of this
module guessed at: `run_onboarding_checks` takes one flat, frozen `OnboardingCheckParams` of
**measured facts** - row counts, the standard names a mapping produced, the mapped table itself -
because "a check knows about data, not about a request" (its own docstring, DEC-065). Adapting to it
is `check_params`/`source_facts` below, and the interesting half of that adaptation is what they
leave *out*: a mapping screen answers before any file is read, so `SourceFacts.frame` stays `None`
and `feature_null_fractions`/`future_event_rows` stay empty. Every check that needs the data itself
then finds nothing to look at and reports nothing, which is the honest answer - "a fact nobody
measured is absent, not zero" - where a zero or an empty frame would have been this route claiming
it had looked.

`HeuristicMappingSuggester` and `apply_mapping`, the other two names `engine.onboarding.mapping`
carries, are deliberately not referenced here: suggestion is this route's whole job and
`suggested_mapping_spec` is its one entry point (the same split `engine/onboarding/sources.py` draws
between `profile_source`, the entry point every caller uses, and the role/key/time detectors it calls
internally); applying a mapping to real data belongs to the build path (`api/routes/datasets.py`),
not to saving one.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, ValidationError

from api.deps import ConfigRootDep, StorageDep
from api.routes.clients import ClientStoreDep, load_client
from api.routes.sources import load_profile, load_source
from api.routes.uploads import http_error, use_case_config
from api.schemas import ErrorResponse
from engine.clients import ClientStore, ClientStoreError
from engine.config import RoleCatalogue, StrictBase, UseCaseConfig, get_roles
from engine.onboarding.mapping import EVENT_TIME, suggested_mapping_spec
from engine.onboarding.specs import (
    MappingColumn,
    MappingSpec,
    OnboardingCheck,
    OnboardingSpec,
    SourceSpec,
)
from engine.onboarding.validate import OnboardingCheckParams, SourceFacts, run_onboarding_checks
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
            f"{source.file_name!r} has not been told what kind of table it is yet. "
            "Confirm its role on the sources screen, then map it.",
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
    request's to change - a role is decided on the sources screen, and a mismatch here is refused
    rather than silently overwritten (house rule 2 extends to a field that would otherwise drift out
    from under the record that owns it).

    What an id already names is checked *before* the upsert runs, because `save_mapping`'s
    `ON CONFLICT ... DO UPDATE SET client_id = excluded.client_id` will happily move a stored mapping
    to whichever client writes last: an id belonging to another client answers the same
    `MAPPING_NOT_FOUND` `load_mapping` gives (the id's existence is not this client's to learn), and
    an id already describing a *different file* is refused outright rather than quietly repointed at
    this one, which would leave every recipe naming that mapping building from a table nobody chose.
    Everything that can 404 - the client, the use case, the source and its profile - is resolved
    before the write, so a refused save leaves nothing half-written behind.
    """
    load_client(store, client_id)
    if body.client_id != client_id:
        raise http_error(
            409,
            "CLIENT_MISMATCH",
            f"This mapping belongs to client {body.client_id!r}, not to {client_id!r}. "
            "Open it from the client it belongs to and save it there.",
        )
    source = load_source(store, client_id, body.source_id)
    if body.role != source.role:
        raise http_error(
            409,
            "MAPPING_ROLE_MISMATCH",
            f"{source.file_name!r} is confirmed as {source.role!r}, but this mapping says "
            f"{body.role!r}. Re-confirm the file's role, or suggest the mapping again, before saving.",
        )
    existing = stored_mapping_or_none(store, mapping_id)
    if existing is not None:
        if existing.client_id != client_id:
            raise mapping_not_found(mapping_id)
        if existing.source_id != body.source_id:
            raise http_error(
                409,
                "MAPPING_SOURCE_MISMATCH",
                f"This mapping was saved for a different file, so saving {source.file_name!r} over it "
                "would change what every recipe using it reads. Suggest a new mapping for this file "
                "instead.",
            )
    load_profile(storage, client_id, source.source_id)
    config = use_case_config(body.use_case, root)
    mapping = build_mapping_spec(mapping_id, client_id, body)
    saved = store.save_mapping(mapping)
    return MappingSaveResponse(
        mapping_id=saved.mapping_id,
        checks=run_onboarding_checks(
            check_params(config, get_roles(root), client_mapping_facts(store, client_id, body.use_case))
        ),
    )


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
def source_facts(source: SourceSpec, mapping: MappingSpec | None) -> SourceFacts:
    """One uploaded table as an API route can honestly describe it, without reading a single row.

    `rows` is `SourceSpec.rows`, the count ingest measured over the whole file when it was uploaded -
    not the possibly-capped `row_count` on its profile - because `SourceFacts` documents `rows` as
    the file's own count and `SOURCE_TOO_LARGE` is a statement about the file.

    `frame` stays `None`, and that is the point. Every check that needs the mapped table itself -
    `ENTITY_DUPLICATE_KEYS`, `JOIN_KEY_COVERAGE_LOW`, `EVENT_TIME_UNPARSEABLE`, `KEY_FORMAT_MISMATCH` -
    then finds nothing to read and returns no finding, which is the truth: this request did not read
    the data. Handing them an empty frame instead would turn "nobody looked" into "we looked and
    found none", which is exactly the fabrication house rule 2 forbids. Those checks are answered
    for real by the build, which has the tables in front of it, and their findings reach the user
    through the build report.

    A source whose role nobody has confirmed carries `""`: it is not the entity role and it is not
    any event role, so it is counted and sized like any other table and claims nothing else.
    """
    return SourceFacts(
        source_id=source.source_id,
        role=source.role or "",
        rows=source.rows,
        mapped_standard=() if mapping is None else tuple(column.standard for column in mapping.columns),
        event_time_format=event_time_format(mapping),
    )


def event_time_format(mapping: MappingSpec | None) -> str | None:
    """The `strptime` pattern the user pinned on `event_time`, or `None` when they pinned none.

    `DATE_FORMAT_AMBIGUOUS` exists to ask for exactly this pattern and falls silent once one is set,
    so reading it off the mapping is what stops a resolved question being asked again.
    """
    if mapping is None:
        return None
    for column in mapping.columns:
        if column.standard == EVENT_TIME and column.transform is not None:
            return column.transform.date_format
    return None


def check_params(
    config: UseCaseConfig,
    roles: RoleCatalogue,
    facts: tuple[SourceFacts, ...],
    *,
    spec: OnboardingSpec | None = None,
) -> OnboardingCheckParams:
    """What a route can tell `run_onboarding_checks` before anything has been built.

    `engine.onboarding.validate` deliberately has no `params_from_config` - "every threshold field is
    named exactly like its `onboarding.*` config leaf, so the build stage copies them across and this
    module never reads a config" - so the copying lives at the caller, and this is the API's copy of
    it. Nothing here branches on a use case or a client (house rule 1): every value is read off
    `UseCaseConfig`, `configs/roles.yaml` or the recipe by name.

    `join_coverage_warn`, `join_coverage_error` and `max_unparseable_fraction` are deliberately not
    passed: they have no config leaf yet, and the checks that read them need a frame this call does
    not have, so overriding the module's own defaults here would be inventing a threshold for a check
    that will not run.

    Without a recipe (`spec=None`, the mapping screen) the feature list and the label are genuinely
    unknown and stay empty, and the snapshot mode is the use case's configured default - the mode any
    recipe started from this config will carry, not a guess at one the user has not made yet.
    """
    onboarding = config.onboarding
    return OnboardingCheckParams(
        sources=facts,
        feature_names=() if spec is None else spec.feature_spec.names,
        label_name="" if spec is None or spec.label_spec is None else spec.label_spec.name,
        required_standard_columns=tuple(column.name for column in config.standard_schema.required_columns),
        entity=config.entity,
        entity_role=roles.entity_role,
        snapshot_mode=onboarding.snapshots.mode if spec is None else spec.snapshot_spec.mode,
        max_features=onboarding.features.max_features,
        drop_if_null_fraction_above=onboarding.features.drop_if_null_fraction_above,
        max_source_rows=onboarding.limits.max_source_rows,
        max_sources=onboarding.limits.max_sources,
    )


def facts_for(sources: Mapping[str, SourceSpec], mappings: Iterable[MappingSpec]) -> tuple[SourceFacts, ...]:
    """`SourceFacts` for each source in `sources`, carrying whichever mapping targets it.

    Keyed off the *sources*, not the mappings, so a source nobody has mapped yet is still counted:
    that is what makes `ENTITY_KEY_UNMAPPED` and `REQUIRED_STANDARD_COLUMN_UNMAPPED` fire while a
    recipe is still half-mapped, instead of a half-mapped recipe looking complete.
    """
    by_source = {mapping.source_id: mapping for mapping in mappings}
    return tuple(source_facts(source, by_source.get(source_id)) for source_id, source in sources.items())


def client_mapping_facts(store: ClientStore, client_id: str, use_case: str) -> tuple[SourceFacts, ...]:
    """Every source this client has mapped for `use_case`, as facts - the set the mapping screen asks
    about (see the module docstring on why one mapping alone cannot answer plan §7's codes).

    A source with no mapping for this use case is left out rather than reported as unmapped: it may
    belong to another use case's recipe entirely, and a table nobody has pointed at this use case is
    not yet a claim about it. A source a mapping names but the registry no longer holds is skipped
    for the same reason `load_mapping` hides a cross-client id - this call reports on what exists.
    """
    mappings = store.list_mappings(client_id, use_case)
    sources = {source.source_id: source for source in store.list_sources(client_id)}
    named = {mapping.source_id for mapping in mappings}
    return facts_for({key: row for key, row in sources.items() if key in named}, mappings)


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


def stored_mapping_or_none(store: ClientStore, mapping_id: str) -> MappingSpec | None:
    """Whatever is stored under `mapping_id`, or `None` when nothing is - *not* a 404.

    The one place a missing mapping is an ordinary answer rather than an error: `PUT` is an upsert,
    so the id a suggestion minted has nothing stored under it the first time it is saved, and only
    what is already there can be checked for ownership.
    """
    try:
        return store.get_mapping(mapping_id)
    except ClientStoreError:
        return None


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
