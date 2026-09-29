"""`/ai-service`: connect the language service the product talks to, one for each of two purposes (DEC-1140).

Owned by Plan H. The rules are `engine.ai_service`'s; what is added here is what only an HTTP boundary
can get wrong. There are two **slots**: `product` (Product AI - the Guided-setup chat helper, your
team's own tool) and `deliverable` (Deliverable AI - the document assistant, root-cause summaries,
campaign copy and their checks: what the customer receives). Each is saved, tested and disconnected on
its own; a deliverable slot with nothing saved follows the product slot's service.

**No key is ever sent back.** A state carries `has_key` and nothing more. Save, test and model
listing read their body themselves, like `/connections` and `/connection/aws`: FastAPI's default `422`
repeats the offending input, which for these routes would put a key into the response, devtools and any
proxy log. Their errors name fields only, and the body is capped at 16 KiB.

**Every network call runs off the event loop, a few at a time.** A test or a listing talks to someone
else's server with timeouts of up to twenty seconds; at most four run at once and the rest are told
`429 AI_BUSY` straight away. Every response is `Cache-Control: no-store`.

Access (single-user today, but the policies stay): reading is Viewer, everything that changes a slot or
talks to the service is Analyst. A test that fails is `200` with `ok: false` and a plain fix, because
"the key was rejected" is an answer, not a fault of this server.

The routes that need a model call `resolve_slot` (or turn `AiNotConnectedError` into
`not_connected_http`): `409 AI_NOT_CONNECTED`, with `slot` saying which one, before any work is started.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, SettingsDep, StorageDep
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.ai_service import (
    PROVIDERS,
    SLOT_LABELS,
    SLOTS,
    AiNotConnectedError,
    AiServiceError,
    AiServiceRecord,
    AiServiceState,
    AiServiceStates,
    AiServiceStore,
    Candidate,
    LastTest,
    ModelList,
    ResolvedAi,
    ServiceInput,
    TestOutcome,
    build_state,
    build_states,
    candidate_from,
    key_for,
    list_models,
    resolve_client,
    run_test,
    save_service,
    yaml_bedrock,
)
from engine.aws_connection import profile_in_force
from engine.config import LlmConfig
from engine.settings import Settings
from engine.storage import Storage
from engine.utils.time import utc_now

__all__ = ["not_connected_http", "resolve_slot", "router"]

router = APIRouter(tags=["ai-service"])

_M = TypeVar("_M", bound=BaseModel)
_T = TypeVar("_T")

_NO_STORE: Final[str] = "no-store"
_MAX_BODY_BYTES: Final[int] = 16 * 1024
"""A provider, a key, two model names, an address and a region: well under a kilobyte. More is not one."""
_MAX_CONCURRENT_CALLS: Final[int] = 4
_CALLS: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(_MAX_CONCURRENT_CALLS)


def _policy(role: Role, action: str, purpose: str, *, object_param: str | None = "slot") -> RoutePolicy:
    return RoutePolicy(
        role=role,
        action=action,
        purpose=purpose,
        object_type="ai_service",
        object_param=object_param,
    )


register(
    {
        ("GET", "/ai-service"): _policy(
            Role.VIEWER, "ai_service.read", "see the AI service", object_param=None
        ),
        ("PUT", "/ai-service/{slot}"): _policy(Role.ANALYST, "ai_service.save", "connect an AI service"),
        ("POST", "/ai-service/{slot}/test"): _policy(Role.ANALYST, "ai_service.test", "test an AI service"),
        ("POST", "/ai-service/{slot}/models"): _policy(
            Role.ANALYST, "ai_service.models", "list an AI service's models"
        ),
        ("DELETE", "/ai-service/{slot}"): _policy(
            Role.ANALYST, "ai_service.delete", "disconnect an AI service"
        ),
    }
)

_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    """The OpenAPI `requestBody` of a route that reads its body itself, so `/docs` still shows it."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get(
    "/ai-service",
    response_model=AiServiceStates,
    summary="Both AI service slots (Product AI and Deliverable AI) and the providers a person can connect",
)
def read_ai_service(
    response: Response, storage: StorageDep, current: SettingsDep, root: ConfigRootDep
) -> AiServiceStates:
    response.headers["Cache-Control"] = _NO_STORE
    return build_states(storage, current, root)


@router.put(
    "/ai-service/{slot}",
    response_model=AiServiceState,
    responses=_ERRORS,
    summary="Connect one slot; the key is encrypted and never returned. Makes no network call",
    openapi_extra=_request_body(ServiceInput),
)
async def save_ai_service(
    slot: str,
    request: Request,
    response: Response,
    storage: StorageDep,
    current: SettingsDep,
    root: ConfigRootDep,
) -> AiServiceState:
    response.headers["Cache-Control"] = _NO_STORE
    slot = _slot(slot)
    body = await _parse(request, ServiceInput)

    def save() -> AiServiceState:
        store = AiServiceStore(storage, current)
        _catch(lambda: save_service(store, slot, body, settings=current, storage=storage))
        return _state(storage, current, slot, root)

    state = await run_in_threadpool(save)
    set_audit_context(request, object_id=slot)
    return state


@router.post(
    "/ai-service/{slot}/test",
    response_model=TestOutcome,
    responses=_ERRORS,
    summary="One tiny completion against a slot, or against the settings in the body; a failure is ok: false",
    openapi_extra=_request_body(ServiceInput),
)
async def test_ai_service(
    slot: str,
    request: Request,
    response: Response,
    storage: StorageDep,
    current: SettingsDep,
    root: ConfigRootDep,
) -> TestOutcome:
    response.headers["Cache-Control"] = _NO_STORE
    slot = _slot(slot)
    body = await _parse(request, ServiceInput)
    testing_saved = not body.model_fields_set

    def run() -> TestOutcome:
        store = AiServiceStore(storage, current)
        active = store.active(slot)
        record = active[0] if active is not None else None
        candidate = _candidate(body, record, current, root, slot, testing_saved)
        try:
            key = key_for(store, record, candidate)
        except AiServiceError as exc:  # the saved key cannot be read: an answer, not a fault
            return TestOutcome(ok=False, message=exc.message, fix=exc.fix, model=candidate.model)
        outcome = _bounded(lambda: run_test(candidate, key, profile=profile_in_force()))
        if testing_saved and active is not None:
            owner_record, owner = active
            store.record_test(
                owner,
                owner_record,
                LastTest(
                    ok=outcome.ok,
                    message=outcome.message,
                    fix=outcome.fix,
                    latency_ms=outcome.latency_ms,
                    tested_at=utc_now(),
                ),
            )
        return outcome

    return await run_in_threadpool(run)


@router.post(
    "/ai-service/{slot}/models",
    response_model=ModelList,
    responses=_ERRORS,
    summary="The model names a service lists (at most 200); on failure an empty list and a note",
    openapi_extra=_request_body(ServiceInput),
)
async def list_ai_service_models(
    slot: str, request: Request, response: Response, storage: StorageDep, current: SettingsDep
) -> ModelList:
    response.headers["Cache-Control"] = _NO_STORE
    slot = _slot(slot)
    body = await _parse(request, ServiceInput)

    def run() -> ModelList:
        store = AiServiceStore(storage, current)
        active = store.active(slot)
        record = active[0] if active is not None else None
        candidate = _catch(
            lambda: candidate_from(
                body, saved=record, settings=current, partial=True, resolve=True, require_model=False
            )
        )
        try:
            key = key_for(store, record, candidate)
        except AiServiceError as exc:
            return ModelList(note=f"{exc.message} {exc.fix or ''}".strip())
        return _bounded(lambda: list_models(candidate, key, profile=profile_in_force()))

    return await run_in_threadpool(run)


@router.delete(
    "/ai-service/{slot}",
    response_model=AiServiceState,
    responses=_ERRORS,
    summary="Disconnect one slot: removes its saved record and key; the state after (it may inherit or fall back)",
)
def delete_ai_service(
    slot: str,
    request: Request,
    response: Response,
    storage: StorageDep,
    current: SettingsDep,
    root: ConfigRootDep,
) -> AiServiceState:
    response.headers["Cache-Control"] = _NO_STORE
    slot = _slot(slot)
    AiServiceStore(storage, current).delete(slot)
    set_audit_context(request, object_id=slot)
    return _state(storage, current, slot, root)


# ---------------------------------------------------------------------------
# For the routes that need a model
# ---------------------------------------------------------------------------
def not_connected_http(exc: AiNotConnectedError) -> HTTPException:
    """`409 AI_NOT_CONNECTED`, naming the slot; the message says what to do (`fix`)."""
    return HTTPException(
        status_code=409,
        detail={"code": exc.code, "message": exc.message, "path": None, "fix": exc.fix, "slot": exc.slot},
        headers={"Cache-Control": _NO_STORE},
    )


def resolve_slot(llm: LlmConfig, *, slot: str, storage: Storage, settings: Settings) -> ResolvedAi:
    """The client and effective `LlmConfig` for a language call of `slot`, or `409 AI_NOT_CONNECTED`."""
    try:
        return resolve_client(llm, slot=slot, storage=storage, settings=settings, profile=profile_in_force())
    except AiNotConnectedError as exc:
        raise not_connected_http(exc) from None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _state(storage: Storage, settings: Settings, slot: str, root: Path | None) -> AiServiceState:
    return build_state(storage, settings, slot, configured=yaml_bedrock(root))


def _slot(slot: str) -> str:
    if slot not in SLOTS:
        raise _error(
            404,
            "AI_SLOT_UNKNOWN",
            f"There is no AI service slot {slot[:40]!r}. The slots are: {', '.join(SLOTS)}.",
            field="slot",
        )
    return slot


def _candidate(
    body: ServiceInput,
    record: AiServiceRecord | None,
    settings: Settings,
    root: Path | None,
    slot: str,
    testing_saved: bool,
) -> Candidate:
    if testing_saved and record is None:
        configured = yaml_bedrock(root)
        if configured is None:
            raise _error(
                409,
                "AI_NOT_CONNECTED",
                f"No AI service is connected for {SLOT_LABELS[slot]}.",
                fix=f"Open Connections → AI service → {SLOT_LABELS[slot]} and connect one.",
                slot=slot,
            )
        return Candidate(
            provider=PROVIDERS["bedrock"],
            model=configured.generation_model_id,
            embedding_model=configured.embedding_model_id,
            base_url="",
            region=configured.region,
            api_key=None,
        )
    return _catch(lambda: candidate_from(body, saved=record, settings=settings, partial=True, resolve=True))


def _catch(action: Callable[[], _T]) -> _T:
    try:
        return action()
    except AiServiceError as exc:
        raise _error(exc.status, exc.code, exc.message, fix=exc.fix, field=exc.field) from None


def _bounded(action: Callable[[], _T]) -> _T:
    if not _CALLS.acquire(blocking=False):
        busy = _error(429, "AI_BUSY", "Other AI service calls are running. Try again in a few seconds.")
        busy.headers = {**(busy.headers or {}), "Retry-After": "5"}
        raise busy
    try:
        return action()
    finally:
        _CALLS.release()


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    fix: str | None = None,
    field: str | None = None,
    slot: str | None = None,
) -> HTTPException:
    """The API's error envelope (`detail.code`, `.message`, `.path`), plus `field`, `fix` and `slot` when known."""
    detail: dict[str, Any] = {"code": code, "message": message, "path": field}
    if field is not None:
        detail["field"] = field
    if fix is not None:
        detail["fix"] = fix
    if slot is not None:
        detail["slot"] = slot
    return HTTPException(status_code=status_code, detail=detail, headers={"Cache-Control": _NO_STORE})


async def _parse(request: Request, model: type[_M]) -> _M:
    """`model` from the raw body; errors name the accepted fields and never repeat a value."""
    raw = await _read_body(request)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        raise _error(422, "BODY_INVALID", "The request body is not JSON.") from None
    if not isinstance(data, dict):
        raise _error(422, "BODY_INVALID", "The request body must be a JSON object.")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        accepted = set(model.model_fields)
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]} & accepted)
        named = f" Check {', '.join(f'`{f}`' for f in fields)}." if fields else ""
        raise _error(
            422,
            "BODY_INVALID",
            f"This endpoint accepts {', '.join(f'`{f}`' for f in model.model_fields)}.{named}",
            field=fields[0] if fields else None,
        ) from None


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (
        not (declared.isascii() and declared.isdigit()) or int(declared) > _MAX_BODY_BYTES
    ):
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large() -> HTTPException:
    return _error(413, "BODY_TOO_LARGE", "This request body is larger than an AI service's settings can be.")
