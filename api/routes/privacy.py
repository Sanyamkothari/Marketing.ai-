"""The DPDP controls' API (Phase 4b M48): consent, retention, erasure and access requests.

Engineering controls that support compliance with India's DPDP Act; they are not legal advice. The
work is done by `engine.privacy` - this module decides who may ask, what a request looks like, and
what the audit trail records about it.

**Who.** Every route is Admin (plan M46: "Admin - users, settings, retention"): consent evidence,
deleting a person and handing over everything held about them are decisions made for the client
organisation, not steps in an analysis. The one exception is a scoring run's consent report, which
is Viewer like every other report of a run: it holds counts, never a person, and the Results page
has to be able to say how many customers were left out and why (DEC-747). It is served here, at
`/privacy/runs/{run_id}/consent-report`, rather than added to the allow-list of
`/runs/{run_id}/artefacts/{name}`, because that route belongs to Phase 1 and `PRIVACY_RUN_ARTEFACTS`
is deliberately kept apart from `ARTEFACT_REGISTRY` (DEC-732, DEC-750).

**A principal id is only ever in a request body** (DEC-746). Every route that takes one is a `POST`
with a JSON body, even the two that only read (a consent lookup, an access request): a path or a
query string is written to the server's access log, to a load balancer's log and to browser history,
so `GET /privacy/consent?principal_id=C-104` would copy the very value an erasure is meant to
remove into three places nothing erases. Being `POST`s, both are audited like every mutating
request; the event's `object_id` is the *request's* id (the lookup id, the access-request id, the
erasure-request id) and the person appears only as `principal_hash` in `details`, salted with
`privacy_salt` (the deployment's secret salt, DEC-860) like the ledger and the erasure register (DEC-705, DEC-733). No response
echoes the id either.

**Exactly one audit event per request.** The engine's `erase` and `apply_retention` can each write
their own event, for callers with no request (a script, a job). Here they are passed no audit log,
and the route hands the same details to `set_audit_context` instead, so the audit middleware's one
event carries them (DEC-718).

**Retention is plan, then apply - and apply re-checks the plan** (DEC-748). `GET
/privacy/retention/plan` is the dry run: the engine's `RetentionPlan` plus a `plan_hash` over its
`planned_at` and items. `POST /privacy/retention/apply` takes back `plan_id`, `planned_at` and
`plan_hash`, plans again *as of the same moment*, and deletes only if the new plan hashes the same;
otherwise **409 `RETENTION_PLAN_CHANGED`** and nothing is touched. The server keeps no plan between
the two requests, so any process can apply what another planned, and what an Admin reviewed is
exactly what is deleted - a run that started meanwhile (its inputs are now `IN_USE`), a file that
appeared, or a second apply of the same plan all change the hash. `as_of` may be in the past (a
rehearsal) but not the future: planning ahead is how data would be deleted before its time.

**One store rewrite at a time** (DEC-749). Erasure and retention both rewrite the artefact store;
two at once could each read a file the other is about to rewrite. A process-wide lock serialises
them. It does not coordinate with a pipeline job writing a run at the same moment, nor across API
processes (see the report's gaps).

**A consent file is all or nothing** (DEC-734): `201` with the import report when rows were written,
`422` with the *same* report when it was refused, so the screen shows every problem by row and
column at once; at most 64 MB, UTF-8 (DEC-751). A consent needs a client: the request's, else the
deployment's `client_id`, else 422 `CLIENT_ID_REQUIRED`.

**Erasure is a background job** (Plan D M54, DEC-863; it answered 201 with the outcome before,
DEC-752). `POST /privacy/erasure` writes the register row as `queued` under an id the route mints,
starts the job and answers `202`; `GET /privacy/erasure/{request_id}/progress` shows how far it has
got store by store (a plain read: no hash, no person), and `GET /privacy/erasure/{request_id}` - an
audited read, a record about an identifiable person's request - is the completion report. A store
that still fails after its retries ends the request `failed` (`ERASURE_STORE_FAILED`), and
`POST /privacy/erasure/{request_id}/retry`, given the id again, runs it again. Audited at start (the
`POST`'s event) and at end (the job's `privacy.erasure.complete`).

**Access requests return a zip and store nothing** (DEC-745): the bytes are streamed to the Admin
and the audit event's `after_hash` is their SHA-256 and its `object_id` the export's `ar_` id, so
the trail proves which file was handed over without keeping a copy the next erasure would have to
find (DEC-753).
"""

from __future__ import annotations

import csv
import hashlib
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from fastapi import APIRouter, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from api.access import PrincipalDep, get_audit_log, platform_data_dir, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep, StorageDep, get_settings
from api.routes.clients import CLIENTS_DB_FILENAME, get_client_store
from api.routes.runs import load_run
from api.routes.uploads import http_error
from api.schemas import (
    AccessRequestBody,
    ConsentImportResponse,
    ConsentLookupRequest,
    ConsentLookupResponse,
    ConsentRecordRequest,
    ConsentRecordResponse,
    ConsentReportResponse,
    ErasureAccepted,
    ErasureProgressResponse,
    ErasureRequestBody,
    ErasureRequestList,
    ErasureRetryBody,
    ErrorResponse,
    PrivacyPolicyResponse,
    PurposeConsent,
    PurposeView,
    RetentionApplyRequest,
    RetentionApplyResponse,
    RetentionPlanResponse,
    RetrainFlagList,
)
from engine.access.roles import Role
from engine.audit.events import content_hash, principal_hash
from engine.clients import ClientStore
from engine.platform_db import PLATFORM_DB_FILENAME, platform_engine
from engine.privacy.access_export import export_principal
from engine.privacy.config import PrivacyConfig, check_privacy_salt, privacy_config_or_none, privacy_salt
from engine.privacy.consent import FUTURE_TOLERANCE, ConsentLedger, principal_key
from engine.privacy.contracts import (
    CONSENT_REPORT_FILENAME,
    ConsentImportReport,
    ConsentRecord,
    ConsentReport,
    ErasureRequestRecord,
    RetentionPlan,
)
from engine.privacy.erasure import (
    erasure_request,
    erasure_requests,
    fail_interrupted,
    queue_request,
    requeue_failed,
    retrain_flags,
)
from engine.privacy.erasure_jobs import ErasureJobs
from engine.privacy.errors import PrivacyError, require_principal_id
from engine.privacy.retention import apply_retention, plan_retention
from engine.privacy.tables import create_privacy_tables
from engine.settings import Settings, SettingsError
from engine.storage import run_key
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = ["MAX_CONSENT_FILE_BYTES", "POLICIES", "get_privacy_engine", "plan_hash", "router"]

_LOGGER = get_logger(__name__)

router: APIRouter = APIRouter(tags=["privacy"])

_AD: Final[Role] = Role.ADMIN

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("GET", "/privacy/purposes"): RoutePolicy(
        role=_AD, action="privacy.policy.read", purpose="see the privacy policy"
    ),
    ("POST", "/privacy/consent"): RoutePolicy(
        role=_AD, action="privacy.consent.record", object_type="consent_record", purpose="record consent"
    ),
    ("POST", "/privacy/consent/imports"): RoutePolicy(
        role=_AD,
        action="privacy.consent.import",
        object_type="consent_import",
        purpose="import consent records",
    ),
    ("POST", "/privacy/consent/lookup"): RoutePolicy(
        role=_AD,
        action="privacy.consent.lookup",
        object_type="consent_lookup",
        purpose="look up a person's consent",
    ),
    ("GET", "/privacy/retention/plan"): RoutePolicy(
        role=_AD, action="privacy.retention.plan", purpose="see what the retention job would delete"
    ),
    ("POST", "/privacy/retention/apply"): RoutePolicy(
        role=_AD,
        action="privacy.retention.apply",
        object_type="retention_plan",
        purpose="run the retention job",
    ),
    ("POST", "/privacy/erasure"): RoutePolicy(
        role=_AD, action="privacy.erasure", object_type="erasure_request", purpose="erase a person's data"
    ),
    ("GET", "/privacy/erasure"): RoutePolicy(
        role=_AD, action="privacy.erasure.list", purpose="see erasure requests"
    ),
    ("POST", "/privacy/erasure/{request_id}/retry"): RoutePolicy(
        role=_AD,
        action="privacy.erasure.retry",
        object_type="erasure_request",
        object_param="request_id",
        purpose="retry an erasure request",
    ),
    ("GET", "/privacy/erasure/{request_id}/progress"): RoutePolicy(
        role=_AD,
        action="privacy.erasure.progress",
        object_type="erasure_request",
        object_param="request_id",
        purpose="follow an erasure request",
    ),
    ("GET", "/privacy/erasure/{request_id}"): RoutePolicy(
        role=_AD,
        action="privacy.erasure.read",
        object_type="erasure_request",
        object_param="request_id",
        purpose="see an erasure request",
        audit_reads=True,
    ),
    ("POST", "/privacy/access-requests"): RoutePolicy(
        role=_AD,
        action="privacy.access_request",
        object_type="access_request",
        purpose="export a person's data",
    ),
    ("GET", "/privacy/retrain-flags"): RoutePolicy(
        role=_AD, action="privacy.retrain_flags.list", purpose="see models flagged for retraining"
    ),
    ("GET", "/privacy/runs/{run_id}/consent-report"): RoutePolicy(
        role=Role.VIEWER,
        action="privacy.consent_report.read",
        object_type="run",
        object_param="run_id",
        purpose="see a run's consent report",
    ),
}
"""This router's rows of the policy table, registered at import (see `api/access_policy.py`)."""

register(POLICIES)

MAX_CONSENT_FILE_BYTES: Final[int] = 64 * 1024 * 1024
"""A consent CSV larger than this is refused before it is parsed: millions of rows, not a form export."""

_CHUNK_BYTES: Final[int] = 1024 * 1024
ConsentState = Literal["valid", "withdrawn", "expired", "none"]
_CLIENT_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
"""A client id is an identifier the product minted or the deployment configured, never free text."""

_STORE_REWRITE_LOCK: Final[threading.Lock] = threading.Lock()
"""Held while erasure or retention rewrites the artefact store, so the two never interleave (DEC-749)."""

_ENGINE_LOCK: Final[threading.Lock] = threading.Lock()
_ENGINE_SLOT: Final[str] = "privacy_engine"

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {**_ERRORS, 404: {"model": ErrorResponse}}

AsOfQuery = Annotated[
    datetime | None,
    Query(description="Plan as of this moment (ISO 8601 with an offset); defaults to now. Never the future."),
]
IncludeClearedQuery = Annotated[bool, Query(description="Also list flags a retraining has cleared.")]
ConsentFileField = Annotated[
    UploadFile,
    File(description="CSV: principal_id, purpose, status, recorded_at; optional source, expires_at."),
]
ClientIdForm = Annotated[
    str | None, Form(description="Client whose ledger to import into; defaults to the deployment's.")
]
PartialForm = Annotated[
    bool, Form(description="Import the valid rows even when some rows are wrong. Default: all or nothing.")
]


# ---------------------------------------------------------------------------
# Providers and small helpers
# ---------------------------------------------------------------------------
def get_privacy_engine(request: Request) -> Engine:
    """The platform database the ledger and the erasure register live in, with their tables present.

    `create_app(data_dir=…)` wins, as for every other service; otherwise `platform_engine` decides
    (SQLite beside the artefacts, or the registry's Postgres, whose schema Alembic's 0003 owns).
    That is the same file the score flow's consent seam finds through `ledger_engine_for`, so a
    record written here gates the next scoring run.
    """
    state = request.app.state
    cached = getattr(state, _ENGINE_SLOT, None)
    if isinstance(cached, Engine):
        return cached
    explicit = getattr(state, "data_dir", None)
    fresh = platform_engine(get_settings(request), data_dir=None if explicit is None else Path(str(explicit)))
    create_privacy_tables(fresh)
    with _ENGINE_LOCK:
        existing = getattr(state, _ENGINE_SLOT, None)
        if not isinstance(existing, Engine):
            setattr(state, _ENGINE_SLOT, fresh)
            return fresh
    return existing


def install_privacy_checks(app: FastAPI) -> None:
    """`PHASE_APP_HOOKS` entry: a production API does not start without its privacy salt (R3, DEC-860),
    and an erasure a stopped process left unfinished is marked failed when it starts (DEC-869).

    With settings given to `create_app`, the salt refusal is immediate - `create_app` raises. The
    deployed `api.main:app` is built with none, so the check runs at startup, where a `SettingsError`
    stops uvicorn before it serves a request: failing to boot is right here, unlike sign-in (DEC-702),
    because every hash written without the secret would have to be thrown away later.

    **Orphaned erasures.** The erasure jobs live in the API process's memory (DEC-863), so a request
    still `queued` or `in_progress` when an API process starts was left by one that stopped, and
    nothing would ever finish it or let it be retried. At startup every such row becomes `failed`
    with `ERASURE_INTERRUPTED`, which the retry route accepts. That is safe because a deployment runs
    **one** API process (DEC-861's premise, the same one the sign-in limiter's memory rests on): no
    other live process can be running a job this would mark. A database that cannot be reached is
    logged, not fatal - the routes that need it will say so.
    """
    settings = getattr(app.state, "settings", None)
    given = settings if isinstance(settings, Settings) else None
    if given is not None:
        check_privacy_salt(given)

    def at_startup() -> None:
        request = Request({"type": "http", "app": app, "headers": [], "method": "GET"})
        if given is None:
            check_privacy_salt(get_settings(request))
        try:
            _fail_interrupted_erasures(request)
        except (SQLAlchemyError, SettingsError, ImportError, OSError) as exc:
            _LOGGER.warning(
                "privacy: unfinished erasures not checked at startup error=%s", type(exc).__name__
            )

    app.router.on_startup.append(at_startup)


def _fail_interrupted_erasures(request: Request) -> None:
    """`fail_interrupted` on the platform database - unless it is a SQLite file that does not exist yet:
    then nothing can be unfinished, and starting the API must not create one in a Phase 1 directory."""
    explicit = getattr(request.app.state, "data_dir", None)
    settings = get_settings(request)
    if explicit is not None or settings.metadata_backend == "sqlite":
        directory = Path(str(explicit)) if explicit is not None else settings.data_dir
        if not (directory / PLATFORM_DB_FILENAME).is_file():
            return
    fail_interrupted(get_privacy_engine(request))


def _salt(request: Request, settings: Settings) -> str:
    """This deployment's secret principal-hash salt (R3, DEC-860), kept with the platform database."""
    return privacy_salt(settings, engine=get_privacy_engine(request))


def _privacy_config(root: Path) -> PrivacyConfig:
    """`configs/privacy.yaml`, or 409 `PRIVACY_NOT_CONFIGURED` when this configuration has none.

    A file that exists and is wrong raises `ConfigError`, which the app renders as its 422: silently
    treating a typo as "no privacy controls" is the failure `privacy_config_or_none` refuses too.
    """
    config = privacy_config_or_none(root)
    if config is None:
        raise http_error(
            409,
            "PRIVACY_NOT_CONFIGURED",
            "This deployment has no privacy policy (configs/privacy.yaml), so there is nothing to apply.",
        )
    return config


def _principal_id(raw: str) -> str:
    try:
        return require_principal_id(raw)
    except PrivacyError as exc:
        raise http_error(422, exc.code, exc.message, path="principal_id") from None


def _client_id(given: str | None, settings: Settings, *, required: bool) -> str | None:
    """The request's client id, else the deployment's; 422 when one is required and neither exists."""
    client = given or settings.client_id
    if client is None:
        if required:
            raise http_error(
                422,
                "CLIENT_ID_REQUIRED",
                "Name the client: this deployment has no client id of its own.",
                path="client_id",
            )
        return None
    if not _CLIENT_ID.fullmatch(client):
        raise http_error(
            422, "CLIENT_ID_INVALID", "A client id is letters, digits and . _ : - only.", path="client_id"
        )
    return client


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else uuid.uuid4().hex


def _not_future(moment: datetime, *, field: str) -> datetime:
    if moment.tzinfo is None:
        raise http_error(
            422, "TIMESTAMP_NEEDS_OFFSET", f"Give `{field}` with a time-zone offset.", path=field
        )
    if moment > utc_now() + FUTURE_TOLERANCE:
        raise http_error(
            422,
            "TIMESTAMP_IN_FUTURE",
            f"`{field}` is in the future; retention and consent are decided as of now or earlier.",
            path=field,
        )
    return moment


def _existing_client_store(request: Request) -> ClientStore | None:
    """The onboarding client store when this deployment has one, without creating one.

    Retention dates a raw source by its registry row and removes the row with the file. A data
    directory with no `clients.db` has no rows to read or remove, and a dry run must not leave an
    empty database behind as its only side effect - so, like `scripts/run_retention.py`, the store is
    used only when it already exists (DEC-754).
    """
    if getattr(request.app.state, "client_store", None) is not None:
        return get_client_store(request)
    data_dir = platform_data_dir(request)
    if data_dir is None or not (data_dir / CLIENTS_DB_FILENAME).is_file():
        return None
    return get_client_store(request)


def plan_hash(plan: RetentionPlan) -> str:
    """SHA-256 of what a retention plan would do: its `planned_at` and its items, not its random id.

    `planned_at` is hashed in UTC, so the same instant sent back with another offset is the same plan.
    """
    digest = content_hash(
        {
            "planned_at": plan.planned_at.astimezone(UTC).isoformat(),
            "items": [item.model_dump(mode="json") for item in plan.items],
        }
    )
    assert digest is not None  # a mapping always hashes
    return digest


def _stores_text(counts: dict[str, int]) -> str:
    return ",".join(f"{name}:{count}" for name, count in sorted(counts.items()))[:200]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@router.get(
    "/privacy/purposes",
    response_model=PrivacyPolicyResponse,
    responses=_ERRORS,
    summary="The consent purposes and the erasure policy",
)
def read_policy(root: ConfigRootDep) -> PrivacyPolicyResponse:
    """What a consent may be given for, which use case is gated by which purpose, and how erasure works."""
    config = _privacy_config(root)
    return PrivacyPolicyResponse(
        purposes=tuple(
            PurposeView(purpose_id=purpose_id, label=purpose.label, description=purpose.description)
            for purpose_id, purpose in sorted(config.purposes.items())
        ),
        use_case_purposes=dict(sorted(config.use_case_purposes.items())),
        erasure_mode=config.erasure.mode.value,
        consent_history=config.erasure.consent_history,
    )


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
@router.post(
    "/privacy/consent",
    response_model=ConsentRecordResponse,
    status_code=201,
    responses=_ERRORS,
    summary="Record one consent given or withdrawn",
)
def record_consent(
    body: ConsentRecordRequest, request: Request, root: ConfigRootDep, settings: SettingsDep
) -> ConsentRecord:
    """Append one row to the ledger. The latest row as of a moment decides (DEC-731)."""
    config = _privacy_config(root)
    principal = _principal_id(body.principal_id)
    client = _client_id(body.client_id, settings, required=True)
    assert client is not None
    if not config.is_purpose(body.purpose):
        raise http_error(
            422,
            "CONSENT_PURPOSE_UNKNOWN",
            f"The purpose must be one of {', '.join(sorted(config.purposes))}.",
            path="purpose",
        )
    recorded_at = _not_future(body.recorded_at or utc_now(), field="recorded_at")
    if body.expires_at is not None and body.expires_at <= recorded_at:
        raise http_error(
            422,
            "CONSENT_EXPIRY_BEFORE_RECORDED",
            "`expires_at` must be after `recorded_at`.",
            path="expires_at",
        )
    ledger = ConsentLedger(get_privacy_engine(request), salt=_salt(request, settings))
    record = ledger.record(
        client_id=client,
        principal_id=principal,
        purpose=body.purpose,
        status=body.status,
        source=body.source,
        recorded_at=recorded_at,
        expires_at=body.expires_at,
    )
    set_audit_context(
        request,
        object_id=f"consent_{record.seq}",
        details={
            "request_kind": "consent_record",
            "principal_hash": record.principal_hash,
            "client_id": client,
            "purpose": record.purpose,
        },
    )
    return record


@router.post(
    "/privacy/consent/imports",
    response_model=ConsentImportResponse,
    status_code=201,
    responses={**_ERRORS, 413: {"model": ErrorResponse}, 422: {"model": ConsentImportResponse}},
    summary="Import a consent CSV (all or nothing unless partial)",
)
async def import_consent(
    request: Request,
    root: ConfigRootDep,
    settings: SettingsDep,
    file: ConsentFileField,
    client_id: ClientIdForm = None,
    partial: PartialForm = False,
) -> ConsentImportResponse | JSONResponse:
    """Validate every row, then append them all - or none, with the problems by row and column (DEC-734).

    `201` with the report when rows were written; `422` with the same report when the file was
    refused. No message quotes a cell: a consent file is personal data.
    """
    config = _privacy_config(root)
    client = _client_id(client_id, settings, required=True)
    assert client is not None
    data = bytearray()
    while chunk := await file.read(_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_CONSENT_FILE_BYTES:
            raise http_error(
                413,
                "CONSENT_FILE_TOO_LARGE",
                f"A consent file may be at most {MAX_CONSENT_FILE_BYTES // (1024 * 1024)} MB; split it.",
            )

    def load() -> ConsentImportReport:
        ledger = ConsentLedger(get_privacy_engine(request), salt=_salt(request, settings))
        return ledger.import_csv(bytes(data), client_id=client, privacy=config, partial=partial)

    try:
        # Parsing and writing are blocking; off the event loop, like FastAPI runs a plain `def` route.
        report = await run_in_threadpool(load)
    except UnicodeDecodeError:
        raise http_error(
            422, "CONSENT_FILE_NOT_UTF8", "Save the consent file as UTF-8 CSV and try again."
        ) from None
    except csv.Error:
        raise http_error(422, "CONSENT_FILE_UNREADABLE", "The consent file is not a readable CSV.") from None
    set_audit_context(
        request,
        details={"request_kind": "consent_import", "client_id": client, "count": report.rows_imported},
    )
    if not report.imported:
        return JSONResponse(status_code=422, content=report.model_dump(mode="json"))
    return report


@router.post(
    "/privacy/consent/lookup",
    response_model=ConsentLookupResponse,
    responses=_ERRORS,
    summary="Look up one person's consent (the id goes in the body, never the URL)",
)
def lookup_consent(
    body: ConsentLookupRequest, request: Request, root: ConfigRootDep, settings: SettingsDep
) -> ConsentLookupResponse:
    """Per purpose, whether the person may be processed as of `as_of`, and every ledger row of theirs."""
    config = _privacy_config(root)
    principal = _principal_id(body.principal_id)
    client = _client_id(body.client_id, settings, required=True)
    assert client is not None
    moment = _not_future(body.as_of, field="as_of") if body.as_of is not None else utc_now()
    ledger = ConsentLedger(get_privacy_engine(request), salt=_salt(request, settings))
    states: list[PurposeConsent] = []
    for purpose in sorted(config.purposes):
        verdict = ledger.classify(client, purpose, [principal], moment)
        state: ConsentState = (
            "valid"
            if verdict.valid
            else "withdrawn" if verdict.withdrawn else "expired" if verdict.expired else "none"
        )
        states.append(PurposeConsent(purpose=purpose, state=state))
    records = ledger.history(principal, client_id=client)
    lookup_id = _request_id(request)
    hashed = ledger.hash(principal)
    set_audit_context(
        request,
        object_id=lookup_id,
        details={
            "request_kind": "consent_lookup",
            "principal_hash": hashed,
            "client_id": client,
            "count": len(records),
        },
    )
    return ConsentLookupResponse(
        lookup_id=lookup_id,
        principal_hash=hashed,
        client_id=client,
        as_of=moment,
        purposes=tuple(states),
        records=records,
    )


@router.get(
    "/privacy/runs/{run_id}/consent-report",
    response_model=ConsentReportResponse,
    responses=_NOT_FOUND,
    summary="How the consent ledger gated one scoring run",
)
def read_consent_report(run_id: str, storage: StorageDep) -> ConsentReport:
    """`consent_report.json` of a scoring run: rows checked, and how many were excluded and why.

    Only a scoring run whose client and purpose had a consent ledger writes one (DEC-732); any other
    run answers 404 `CONSENT_REPORT_NOT_FOUND`, which the UI reads as "not gated".
    """
    load_run(storage, run_id)
    key = run_key(run_id, CONSENT_REPORT_FILENAME)
    if not storage.exists(key):
        raise http_error(
            404,
            "CONSENT_REPORT_NOT_FOUND",
            "This run was not gated by a consent ledger, so it has no consent report.",
        )
    return storage.read_model(key, ConsentReport)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
@router.get(
    "/privacy/retention/plan",
    response_model=RetentionPlanResponse,
    responses=_ERRORS,
    summary="Retention dry run: what the job would delete now",
)
def retention_plan(
    request: Request, root: ConfigRootDep, storage: StorageDep, as_of: AsOfQuery = None
) -> RetentionPlanResponse:
    """The plan, exactly as `POST /privacy/retention/apply` would execute it. Deletes nothing."""
    _privacy_config(root)
    moment = _not_future(as_of, field="as_of") if as_of is not None else utc_now()
    plan = plan_retention(storage, root, moment, client_store=_existing_client_store(request))
    return RetentionPlanResponse(plan=plan, plan_hash=plan_hash(plan), counts=plan.counts())


@router.post(
    "/privacy/retention/apply",
    response_model=RetentionApplyResponse,
    responses=_ERRORS,
    summary="Run the retention job on a reviewed dry run",
)
def retention_apply(
    body: RetentionApplyRequest,
    request: Request,
    root: ConfigRootDep,
    storage: StorageDep,
    principal: PrincipalDep,
) -> RetentionApplyResponse:
    """Plan again as of the reviewed `planned_at`; delete only when it hashes the same (DEC-748)."""
    _privacy_config(root)
    planned_at = _not_future(body.planned_at, field="planned_at")
    set_audit_context(request, object_id=body.plan_id, before_hash=body.plan_hash)
    clients = _existing_client_store(request)
    with _STORE_REWRITE_LOCK:
        current = plan_retention(storage, root, planned_at, client_store=clients)
        digest = plan_hash(current)
        if digest != body.plan_hash:
            set_audit_context(request, details={"request_kind": "retention", "reason_code": "PLAN_CHANGED"})
            raise http_error(
                409,
                "RETENTION_PLAN_CHANGED",
                "The data changed since this plan was made (or it was already applied). "
                "Review a new dry run and apply that.",
            )
        plan = current.model_copy(update={"plan_id": body.plan_id})
        result = apply_retention(plan, storage, None, principal, client_store=clients)
    set_audit_context(
        request,
        details={
            "request_kind": "retention",
            "deleted": len(result.deleted),
            "count": len(result.stripped),
            "stores": _stores_text(plan.counts()),
            "dry_run": False,
        },
    )
    return RetentionApplyResponse(plan_id=plan.plan_id, plan_hash=digest, result=result)


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------
@router.post(
    "/privacy/erasure",
    response_model=ErasureAccepted,
    status_code=202,
    responses=_ERRORS,
    summary="Erase one person from every store, as a background job (the id goes in the body)",
)
def create_erasure(
    body: ErasureRequestBody,
    request: Request,
    response: Response,
    root: ConfigRootDep,
    storage: StorageDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> ErasureAccepted:
    """Queue the erasure and answer `202` at once; a background job does the work (DEC-863).

    The job finds the person everywhere, deletes or tombstones them store by store - retrying a store
    that fails - and flags the models trained on them (DEC-741-743). Models are flagged, never
    retrained here: the next scheduled retraining produces a challenger that still needs the champion
    rule and an Approver. Follow it at `GET /privacy/erasure/{id}/progress`; the full record is
    `GET /privacy/erasure/{id}`. This request's audit event is the start; the job appends
    `privacy.erasure.complete` at the end.
    """
    config = _privacy_config(root)
    principal_id = _principal_id(body.principal_id)
    client = _client_id(body.client_id, settings, required=False)
    salt = _salt(request, settings)
    engine = get_privacy_engine(request)
    erasure_id = f"er_{uuid.uuid4().hex[:20]}"
    hashed = principal_hash(principal_key(principal_id), salt=salt)
    set_audit_context(
        request,
        object_id=erasure_id,
        details={
            "request_kind": "erasure",
            "principal_hash": hashed,
            "client_id": client,
            "trigger": "queued",
        },
    )
    queue_request(
        engine,
        request_id=erasure_id,
        principal_hash=hashed,
        client_id=client,
        mode=config.erasure.mode.value,
        requested_by=principal.user_id,
        requested_at=utc_now(),
        history_all_clients=not body.client_id,
    )
    _erasure_jobs(request).submit(
        request_id=erasure_id,
        storage=storage,
        principal_id=principal_id,
        engine=engine,
        principal=principal,
        salt=salt,
        client_id=client,
        config_root=root,
        audit_log=get_audit_log(request),
        history_all_clients=not body.client_id,
    )
    return _accepted(response, erasure_id, hashed, client)


@router.post(
    "/privacy/erasure/{request_id}/retry",
    response_model=ErasureAccepted,
    status_code=202,
    responses=_NOT_FOUND,
    summary="Run a failed erasure request again (the id goes in the body again)",
)
def retry_erasure(
    request_id: str,
    body: ErasureRetryBody,
    request: Request,
    response: Response,
    root: ConfigRootDep,
    storage: StorageDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> ErasureAccepted:
    """A `failed` request is queued again; the job re-finds whatever still holds the person (DEC-863).

    The id was never stored, so it is asked for again and checked against the request's salted hash:
    **422 `PRINCIPAL_MISMATCH`** when it is not the same person. Only a failed request can be retried
    (**409 `ERASURE_NOT_RETRYABLE`**): a finished one has nothing left to do and a running one is
    already doing it. The request is claimed by one conditional update (`failed` -> `queued`) before
    the job is submitted, so the progress route reads `queued` as soon as this answers and a second
    retry sent meanwhile gets the 409 instead of a second job (DEC-869). The retry deletes the consent
    history the request was made to delete - every client's when no client was named (DEC-870).
    """
    _privacy_config(root)
    engine = get_privacy_engine(request)
    record = erasure_request(engine, request_id)
    if record is None:
        raise http_error(404, "ERASURE_REQUEST_NOT_FOUND", "There is no erasure request with this id.")
    set_audit_context(request, object_id=request_id)
    principal_id = _principal_id(body.principal_id)
    salt = _salt(request, settings)
    hashed = principal_hash(principal_key(principal_id), salt=salt)
    if hashed != record.principal_hash:
        set_audit_context(request, details={"reason_code": "PRINCIPAL_MISMATCH"})
        raise http_error(
            422,
            "PRINCIPAL_MISMATCH",
            "This id is not the one the erasure request was made for.",
            path="principal_id",
        )
    if not requeue_failed(engine, request_id):
        current = erasure_request(engine, request_id)
        set_audit_context(request, details={"reason_code": "ERASURE_NOT_RETRYABLE"})
        raise http_error(
            409,
            "ERASURE_NOT_RETRYABLE",
            "Only a failed erasure request can be retried; "
            f"this one is {record.status if current is None else current.status}.",
        )
    set_audit_context(
        request,
        details={
            "request_kind": "erasure",
            "principal_hash": hashed,
            "client_id": record.client_id,
            "trigger": "retry",
        },
    )
    _erasure_jobs(request).submit(
        request_id=request_id,
        storage=storage,
        principal_id=principal_id,
        engine=engine,
        principal=principal,
        salt=salt,
        client_id=record.client_id,
        config_root=root,
        audit_log=get_audit_log(request),
        history_all_clients=record.history_all_clients,
    )
    return _accepted(response, request_id, hashed, record.client_id)


@router.get(
    "/privacy/erasure/{request_id}/progress",
    response_model=ErasureProgressResponse,
    responses=_NOT_FOUND,
    summary="How far an erasure request has got, store by store",
)
def erasure_progress(request_id: str, request: Request) -> ErasureProgressResponse:
    """Status and per-store progress only - no hash, no counts per person - so polling it is a plain read."""
    record = erasure_request(get_privacy_engine(request), request_id)
    if record is None:
        raise http_error(404, "ERASURE_REQUEST_NOT_FOUND", "There is no erasure request with this id.")
    return ErasureProgressResponse(
        request_id=record.request_id,
        status=record.status,
        error_code=record.error_code,
        progress=record.progress,
        completed_at=record.completed_at,
    )


def _accepted(response: Response, request_id: str, hashed: str, client: str | None) -> ErasureAccepted:
    progress_url = f"/privacy/erasure/{request_id}/progress"
    response.headers["Location"] = f"/privacy/erasure/{request_id}"
    return ErasureAccepted(
        request_id=request_id,
        status="queued",
        principal_hash=hashed,
        client_id=client,
        progress_url=progress_url,
    )


def _erasure_jobs(request: Request) -> ErasureJobs:
    """The app's erasure job runner, created on first use; it holds the store rewrite lock (DEC-749)."""
    state = request.app.state
    jobs = getattr(state, "erasure_jobs", None)
    if isinstance(jobs, ErasureJobs):
        return jobs
    with _ENGINE_LOCK:
        jobs = getattr(state, "erasure_jobs", None)
        if not isinstance(jobs, ErasureJobs):
            jobs = ErasureJobs(rewrite_lock=_STORE_REWRITE_LOCK)
            state.erasure_jobs = jobs
    return jobs


@router.get(
    "/privacy/erasure",
    response_model=ErasureRequestList,
    responses=_ERRORS,
    summary="The erasure register, newest first",
)
def list_erasures(request: Request) -> ErasureRequestList:
    """Every erasure request, with its counts; people appear only as hashes."""
    return ErasureRequestList(requests=erasure_requests(get_privacy_engine(request)))


@router.get(
    "/privacy/erasure/{request_id}",
    response_model=ErasureRequestRecord,
    responses=_NOT_FOUND,
    summary="One erasure request",
)
def read_erasure(request_id: str, request: Request) -> ErasureRequestRecord:
    """The register row of one request: status, per-store counts, flagged models. An audited read."""
    record = erasure_request(get_privacy_engine(request), request_id)
    if record is None:
        raise http_error(404, "ERASURE_REQUEST_NOT_FOUND", "There is no erasure request with this id.")
    return record


@router.get(
    "/privacy/retrain-flags",
    response_model=RetrainFlagList,
    responses=_ERRORS,
    summary="Models flagged for retraining by an erasure",
)
def list_retrain_flags(request: Request, include_cleared: IncludeClearedQuery = False) -> RetrainFlagList:
    """Open flags by default: the model versions the next scheduled retraining must redo (DEC-743)."""
    return RetrainFlagList(flags=retrain_flags(get_privacy_engine(request), open_only=not include_cleared))


# ---------------------------------------------------------------------------
# Access requests
# ---------------------------------------------------------------------------
@router.post(
    "/privacy/access-requests",
    response_class=Response,
    responses={**_ERRORS, 200: {"content": {"application/zip": {}}}},
    summary="Export everything held about one person, as a zip",
)
def create_access_request(
    body: AccessRequestBody, request: Request, root: ConfigRootDep, storage: StorageDep, settings: SettingsDep
) -> Response:
    """The zip of `engine.privacy.access_export`: the person's rows, values, consent and erasure history.

    Returned, never stored (DEC-745). The audit event records the archive's SHA-256 as `after_hash`.
    """
    _privacy_config(root)
    principal_id = _principal_id(body.principal_id)
    client = _client_id(body.client_id, settings, required=False)
    export = export_principal(
        storage,
        principal_id,
        salt=_salt(request, settings),
        engine=get_privacy_engine(request),
        client_id=client,
        history_all_clients=not body.client_id,
    )
    counts: dict[str, int] = {}
    for location in export.manifest.locations:
        counts[location.store] = counts.get(location.store, 0) + 1
    set_audit_context(
        request,
        object_id=export.request_id,
        after_hash=hashlib.sha256(export.content).hexdigest(),
        details={
            "request_kind": "access",
            "principal_hash": export.manifest.principal_hash,
            "client_id": client,
            "count": len(export.manifest.files),
            "stores": _stores_text(counts),
        },
    )
    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store",
        },
    )
