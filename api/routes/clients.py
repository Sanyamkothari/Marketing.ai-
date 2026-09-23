"""`POST /clients`, `GET /clients`, `GET /clients/{id}` and `GET /use-cases/{id}/standard-schema`
(Phase 2 plan §9, M12).

The client endpoints are a thin HTTP skin over `engine.clients.ClientStore`: every business rule
(the id scheme, "newest first", re-validation on read) already lives there and is tested there
(`tests/unit/onboarding/test_clients.py`); this module's only job is the M1 error envelope and the
house's dependency style.

`GET /use-cases/{use_case_id}/standard-schema` belongs to `api/routes/use_cases.py` by subject, but
that file is a shared-ownership route this branch may not edit (`PARALLEL_WORK_PROTOCOL.md` §3), so
it lives here instead - said so in the final report, as the task asked. It is the one endpoint in
this module that touches no client at all: the mapping screen calls it once it knows which use case
it is onboarding a client *for*, before any source exists.

`api/deps.py` is the same kind of shared file, and this milestone needs one dependency it does not
provide - the client metadata store - so `get_client_store` below mirrors its siblings' shape
exactly (one instance per app, double-checked under a lock, rooted at the same data directory
`StorageDep` resolves to) rather than adding a fourth Dep alongside `ConfigRootDep`/`StorageDep` to
a file this branch cannot touch. `api/routes/sources.py` imports it from here rather than
duplicating it, so a test app that mounts both routers still gets exactly one store per process.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from api.deps import ConfigRootDep
from api.routes.uploads import http_error, use_case_config
from api.schemas import ErrorResponse
from engine.clients import (
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_NAME,
    ClientStore,
    ClientStoreError,
    LocalClientStore,
)
from engine.config import (
    FeatureDef,
    LabelDefinition,
    RoleCatalogue,
    StandardSchemaConfig,
    StrictBase,
    get_roles,
    list_industries,
)
from engine.onboarding.specs import ClientRecord
from engine.settings import settings

router: APIRouter = APIRouter(tags=["clients"])

CLIENTS_DB_FILENAME: Final[str] = "clients.db"
"""Sibling of `engine.registry.REGISTRY_FILENAME`, inside the same data directory `StorageDep` roots."""

_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}

_STORE_LOCK: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# The one dependency api/deps.py does not provide (see module docstring)
# ---------------------------------------------------------------------------
def get_client_store(request: Request) -> ClientStore:
    """The client metadata store, lazily built and cached on `app.state`, one per app.

    Reads `app.state.data_dir` the same way `api.deps.get_storage` reads it (`create_app(data_dir=…)`
    or, absent that, `$MARKETING_AI_DATA_DIR`/`data/`), so a client's SQLite store and its artefact
    store always agree on which data directory they belong to - a test that points `StorageDep` at a
    temporary directory gets a temporary `clients.db` beside it, not the real one.
    """
    state = request.app.state
    with _STORE_LOCK:
        existing: ClientStore | None = getattr(state, "client_store", None)
        if existing is None:
            configured = getattr(state, "data_dir", None)
            data_dir = Path(str(configured)) if configured is not None else settings().data_dir
            existing = LocalClientStore(data_dir / CLIENTS_DB_FILENAME)
            state.client_store = existing
    return existing


ClientStoreDep = Annotated[ClientStore, Depends(get_client_store)]


# ---------------------------------------------------------------------------
# Response models (api/schemas.py is shared; ours live beside the routes that use them, per the task)
# ---------------------------------------------------------------------------
class ClientCreateRequest(StrictBase):
    """Body of `POST /clients`."""

    name: Annotated[str, Field(min_length=1)]
    industry: Annotated[str, Field(min_length=1)]
    notes: str = ""


class ClientCreateResponse(StrictBase):
    """Body of the `201` from `POST /clients`: the id every later onboarding call names."""

    client_id: str


class ClientListResponse(StrictBase):
    """Body of `GET /clients`: every client, newest first."""

    clients: tuple[ClientRecord, ...]


class StandardSchemaResponse(StrictBase):
    """Body of `GET /use-cases/{id}/standard-schema`: what the mapping UI is generated from.

    Every field is the merged use case's own document, or the role catalogue, verbatim - nothing
    here is computed, so a client onboarded today and one onboarded after a config edit are mapped
    against the same vocabulary the UI shows them.
    """

    standard_schema: StandardSchemaConfig
    suggested_features: tuple[FeatureDef, ...]
    label: LabelDefinition | None
    roles: RoleCatalogue


# ---------------------------------------------------------------------------
# /clients
# ---------------------------------------------------------------------------
@router.post(
    "/clients",
    response_model=ClientCreateResponse,
    status_code=201,
    summary="Register a client whose data will be onboarded",
)
def create_client(body: ClientCreateRequest, store: ClientStoreDep) -> ClientCreateResponse:
    """A new client folder: `engine.clients.LocalClientStore` mints the id, this just carries it back."""
    record = store.create_client(body.name, body.industry, notes=body.notes)
    return ClientCreateResponse(client_id=record.client_id)


@router.get("/clients", response_model=ClientListResponse, summary="Every client, newest first")
def list_clients(store: ClientStoreDep) -> ClientListResponse:
    return ClientListResponse(clients=store.list_clients())


@router.post(
    "/clients/default",
    response_model=ClientRecord,
    responses={409: {"model": ErrorResponse}},
    summary="The client every installation starts with, created the first time it is asked for",
)
def ensure_default_client(root: ConfigRootDep, store: ClientStoreDep) -> ClientRecord:
    """`Demo`, under the fixed id `engine.clients.DEFAULT_CLIENT_ID` (Plan A M35).

    The header's client picker needs a client to stand on before anyone has created one, and the
    onboarding screens need a client id before the first table can be uploaded. A `POST`, because
    the first call writes; idempotent, because every later call returns the same row unchanged.
    Its industry is the first one this installation configures - `configs/industries/`, sorted,
    the order the overview itself reads them in - so the default is never an industry the product
    does not have. An installation with no industry at all has nothing to onboard for, and says so.
    """
    industries = list_industries(root)
    if not industries:
        raise http_error(
            409,
            "NO_INDUSTRY_CONFIGURED",
            "This installation configures no industry, so there is nothing to onboard a client for. "
            "Add an industry file under configs/industries/ and reload.",
        )
    return store.ensure_client(DEFAULT_CLIENT_ID, DEFAULT_CLIENT_NAME, industries[0])


@router.get(
    "/clients/{client_id}",
    response_model=ClientRecord,
    responses=_NOT_FOUND,
    summary="One client",
)
def read_client(client_id: str, store: ClientStoreDep) -> ClientRecord:
    return load_client(store, client_id)


# ---------------------------------------------------------------------------
# /use-cases/{id}/standard-schema (see module docstring for why it lives here)
# ---------------------------------------------------------------------------
@router.get(
    "/use-cases/{use_case_id}/standard-schema",
    response_model=StandardSchemaResponse,
    responses=_NOT_FOUND,
    summary="The standard schema, suggested features, label and role catalogue the mapping UI is built from",
)
def read_standard_schema(use_case_id: str, root: ConfigRootDep) -> StandardSchemaResponse:
    """A planned or unknown id is a 404, exactly as `GET /use-cases/{id}` already answers it."""
    config = use_case_config(use_case_id, root)
    return StandardSchemaResponse(
        standard_schema=config.standard_schema,
        suggested_features=config.suggested_features,
        label=config.label,
        roles=get_roles(root),
    )


# ---------------------------------------------------------------------------
# Helpers shared with api/routes/sources.py
# ---------------------------------------------------------------------------
def load_client(store: ClientStore, client_id: str) -> ClientRecord:
    """One client, or a 404 `CLIENT_NOT_FOUND`."""
    try:
        return store.get_client(client_id)
    except ClientStoreError as exc:
        raise client_not_found(client_id) from exc


def client_not_found(client_id: str) -> HTTPException:
    return http_error(404, "CLIENT_NOT_FOUND", f"No client with id {client_id!r}.")
