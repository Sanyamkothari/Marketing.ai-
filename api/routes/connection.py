"""`/connection/aws`: choose, check and show the AWS identity Bedrock is called as.

The screen above this module lets different people use different AWS credentials, and this module is
where that stays safe. The rules are `engine/aws_connection.py`'s; what is added here is the part
only an HTTP boundary can get wrong.

**A body is parsed here, never by FastAPI's default validation.** The default `422` repeats the
offending input back to the caller - measured, a request carrying `aws_secret_access_key` gets the
secret reflected in the response, and so does a bare JSON string. That puts the key into devtools,
into any proxy that logs responses and into every HAR file attached to a bug report. So these routes
read the raw body, and an error names what is *accepted* rather than repeating what was sent. The
body is read as a stream and abandoned at 4 KiB, so an oversized upload is refused rather than held
in memory; nesting deeper than a connection setting can be, and a key named twice (the way to hide
one value behind another), are refused as malformed rather than half-parsed.

**A pasted credential is recognised and explained.** A field whose name looks like a secret, or any
value shaped like an access key id, a secret access key or a session token, gets
`CREDENTIALS_NOT_ACCEPTED`: a sentence saying this product never takes keys, what to do instead, and
that a key which has been pasted into a web page should be rotated. A person making that mistake
deserves to find out immediately, not to believe it worked - and a secret key, which is a valid
profile name about half the time, must never be quoted back as "no profile named ...".

**Who may choose is decided by the deployment and the connection, never by a claim.** The peer that
opened the socket must be loopback, the `Host` must name this machine and a browser's `Origin` must
be a loopback page (`engine.aws_connection.editability`), because the API is unauthenticated and
CORS is open on a laptop: without the last two, any web page open in the same browser could list the
profiles, read every identity unmasked and repoint Bedrock at the profile of its choosing. A request
carrying `X-Forwarded-For`, `Forwarded` or `X-Real-IP` came through something else and is not local,
whatever its peer says. Everything else - reading the state, testing what is saved - stays open, and
is masked exactly as for a caller on another machine.

**The check runs off the event loop, and only a few at a time.** It makes up to four AWS calls with
ten-second timeouts. Inline, it would freeze every request; unbounded, forty unauthenticated calls
would hold every worker thread the API has for over a minute. At most `_MAX_CONCURRENT_CHECKS` run at
once and the rest are told `429 CHECK_BUSY` straight away.

**Nothing here is cached.** Every response carries `Cache-Control: no-store`: an identity is the kind
of answer a shared browser or an intermediate cache should not keep.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import partial
from typing import Any, Final, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from api.deps import ConfigRootDep, SettingsDep
from api.routes.uploads import http_error, use_case_config
from api.schemas import AwsConnectionState, ConnectionTestRequest
from engine.aws_connection import (
    AwsConnection,
    ConnectionReport,
    CredentialSource,
    LockReason,
    available_profiles,
    check_connection,
    editability,
    load_connection,
    save_connection,
    valid_region,
)
from engine.config import ConfigError, GenerativeKind
from engine.settings import Settings

__all__ = ["router"]

router = APIRouter(tags=["connection"])

_M = TypeVar("_M", bound=BaseModel)

_NO_STORE: Final[str] = "no-store"
_MAX_BODY_BYTES: Final[int] = 4096
"""A connection body is a source and a profile name. Anything larger is not one."""

_CREDENTIAL_KEY: Final[re.Pattern[str]] = re.compile(
    r"secret|access[_-]?key|session[_-]?token|password|credential", re.IGNORECASE
)
_ACCESS_KEY_ID: Final[re.Pattern[str]] = re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b")
_SECRET_KEY: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9/+=])"
    r"(?=[A-Za-z0-9/+]{0,39}[A-Z])(?=[A-Za-z0-9/+]{0,39}[a-z])(?=[A-Za-z0-9/+]{0,39}[0-9])"
    r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"
)
"""A secret access key: exactly forty base64 characters mixing cases and digits. Without a `/` it is
also a valid profile name, which is why it has to be caught before a profile name is ever quoted."""
_SESSION_TOKEN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9/+=]{100,}")
"""A session token: a long unbroken base64 run. Nothing this API accepts is anywhere near that long."""

_MAX_DEPTH: Final[int] = 8
"""Deeper than any body these routes accept (two levels), shallow enough to walk without recursion."""

_FORWARDING_HEADERS: Final[tuple[str, ...]] = ("x-forwarded-for", "forwarded", "x-real-ip")

_UNKNOWN_USE_CASE: Final[frozenset[str]] = frozenset({"USE_CASE_NOT_FOUND", "USE_CASE_PLANNED"})

_MAX_CONCURRENT_CHECKS: Final[int] = 4
_CHECKS: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(_MAX_CONCURRENT_CHECKS)
"""Connection checks in flight. A thread semaphore, not an event-loop one, so it is shared by every
loop the process runs - the test client starts one per client."""

_NOT_ACCEPTED: Final[str] = (
    "Marketing AI never accepts AWS keys through the browser, and nothing from this request was "
    "stored. Set up a profile on the machine running Marketing AI - `aws configure --profile NAME`, "
    "or `aws configure sso` for single sign-on - and choose it here by name. If that was a real key, "
    "rotate it: a key pasted into a web page should be treated as exposed."
)

_EXPLANATION: Final[dict[LockReason | None, str]] = {
    None: (
        "Marketing AI uses the AWS credentials already set up on this machine, and never asks for "
        "your keys. Choose a profile from your AWS CLI configuration, or use the default chain: "
        "AWS_PROFILE, environment variables, an SSO login or an instance role, in that order."
    ),
    LockReason.DEPLOYED: (
        "This deployment calls Bedrock as its own IAM role, so no keys are stored anywhere and none "
        "can be chosen from a browser. An operator changes the role, not the page."
    ),
    LockReason.REMOTE_CLIENT: (
        "The AWS identity can only be changed from the machine running Marketing AI, on the page it "
        "serves at http://localhost or http://127.0.0.1. From anywhere else you can test the "
        "connection, but not change it."
    ),
}


_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {*AwsConnection.model_fields, *ConnectionTestRequest.model_fields}
)
"""Every field name this module could have written. Only these are ever named back in an error."""


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    """The OpenAPI `requestBody` for a route that reads its body itself, so `/docs` still shows it.

    Nested models are referenced as components rather than inlined: the response models publish them
    already, and a local `$defs` reference would not resolve against the OpenAPI document root.
    """
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": False, "content": {"application/json": {"schema": schema}}}}


# ---------------------------------------------------------------------------
# GET /connection/aws
# ---------------------------------------------------------------------------
@router.get(
    "/connection/aws",
    response_model=AwsConnectionState,
    summary="The AWS identity Bedrock is called as, and whether this caller may change it",
)
def get_aws_connection(request: Request, response: Response, current: SettingsDep) -> AwsConnectionState:
    """Where the credentials come from - never the credentials themselves."""
    response.headers["Cache-Control"] = _NO_STORE
    return _state(current, _lock(request, current))


# ---------------------------------------------------------------------------
# PUT /connection/aws
# ---------------------------------------------------------------------------
@router.put(
    "/connection/aws",
    response_model=AwsConnectionState,
    summary="Choose the AWS identity: the default chain, or an AWS CLI profile by name (local only)",
    openapi_extra=_request_body(AwsConnection),
)
async def put_aws_connection(
    request: Request, response: Response, current: SettingsDep
) -> AwsConnectionState:
    """Save the choice. `403 CONNECTION_LOCKED` on a deployment or from another machine."""
    response.headers["Cache-Control"] = _NO_STORE
    connection = await _parse(request, AwsConnection)
    lock = _lock(request, current)
    if lock is not None:
        raise _locked(lock)
    if connection.source is CredentialSource.PROFILE and connection.profile not in available_profiles():
        raise _error(
            422,
            "PROFILE_NOT_FOUND",
            f"There is no AWS profile named {connection.profile!r} on this machine. Create it with "
            f"`aws configure --profile {connection.profile}` or `aws configure sso`, then reload.",
        )
    save_connection(current, connection)
    return _state(current, lock)


# ---------------------------------------------------------------------------
# DELETE /connection/aws
# ---------------------------------------------------------------------------
@router.delete(
    "/connection/aws",
    response_model=AwsConnectionState,
    summary="Forget the chosen profile and go back to the default credential chain (local only)",
)
def delete_aws_connection(request: Request, response: Response, current: SettingsDep) -> AwsConnectionState:
    """Return to the default chain. `403 CONNECTION_LOCKED` on a deployment or from another machine."""
    response.headers["Cache-Control"] = _NO_STORE
    lock = _lock(request, current)
    if lock is not None:
        raise _locked(lock)
    save_connection(current, AwsConnection())
    return _state(current, lock)


# ---------------------------------------------------------------------------
# POST /connection/aws/test
# ---------------------------------------------------------------------------
@router.post(
    "/connection/aws/test",
    response_model=ConnectionReport,
    summary="Check the AWS identity and whether each configured Bedrock model is enabled - free, no tokens",
    openapi_extra=_request_body(ConnectionTestRequest),
)
async def test_aws_connection(
    request: Request, response: Response, config_root: ConfigRootDep, current: SettingsDep
) -> ConnectionReport:
    """Who the credentials are, and which models they can use. Masked for any caller who cannot edit."""
    response.headers["Cache-Control"] = _NO_STORE
    body = await _parse(request, ConnectionTestRequest)
    lock = _lock(request, current)
    if body.connection is not None and lock is not None:
        raise _locked(lock)
    try:
        use_case = use_case_config(body.use_case_id, config_root)
    except ConfigError as exc:
        # Re-raised rather than left to the app's handler, whose message quotes the id back.
        if exc.code in _UNKNOWN_USE_CASE:
            raise _error(404, exc.code, "There is no available use case with that id.") from None
        raise _error(422, exc.code, exc.message) from None
    # `generative` is never None - engine.yaml's defaults give every use case the block - so a
    # predictive use case is told apart by its kind, exactly as api/routes/generative.py does.
    if use_case.generative.kind is GenerativeKind.NONE:
        raise _error(
            409,
            "NOT_A_GENERATIVE_USE_CASE",
            f"{body.use_case_id} does not call a language model, so it has no models to check.",
        )
    llm = use_case.generative.llm
    region = body.region or llm.region
    if not valid_region(region):
        raise _error(422, "REGION_INVALID", "`region` must be an AWS region name, such as ap-south-1.")
    check = partial(
        check_connection,
        body.connection or load_connection(current),
        region=region,
        model_ids={
            "generation": llm.generation_model_id,
            "judge": llm.judge_model_id,
            "embedding": llm.embedding_model_id,
        },
        mask_identity=lock is not None,
    )
    if not _CHECKS.acquire(blocking=False):
        busy = _error(429, "CHECK_BUSY", "Other connection checks are running. Try again in a few seconds.")
        busy.headers = {**(busy.headers or {}), "Retry-After": "5"}
        raise busy
    try:
        return await run_in_threadpool(check)
    finally:
        _CHECKS.release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _peer(request: Request) -> str | None:
    """The address that opened this connection. Deliberately not `X-Forwarded-For`, which is a claim."""
    return request.client.host if request.client is not None else None


def _lock(request: Request, current: Settings) -> LockReason | None:
    """`editability` for this request: its peer, its `Host`, its `Origin`, and whether it was forwarded.

    A header sent twice is treated as one that names nowhere: `""` matches no local authority.
    """
    return editability(
        current,
        _peer(request),
        host=_single(request, "host"),
        origin=_single(request, "origin"),
        forwarded=any(name in request.headers for name in _FORWARDING_HEADERS),
    )


def _single(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if not values:
        return None
    return values[0] if len(values) == 1 else ""


def _state(current: Settings, lock: LockReason | None) -> AwsConnectionState:
    editable = lock is None
    return AwsConnectionState(
        editable=editable,
        locked_reason=lock,
        env=current.env,
        connection=load_connection(current),
        profiles=available_profiles() if editable else (),
        aws_profile_env=(os.environ.get("AWS_PROFILE") or None) if editable else None,
        explanation=_EXPLANATION[lock],
    )


def _error(status_code: int, code: str, message: str) -> HTTPException:
    """`http_error`, plus `no-store`: a header set on the injected `Response` is dropped when a route
    raises, so an error would otherwise be the one answer from this module a cache may keep."""
    exc = http_error(status_code, code, message)
    exc.headers = {"Cache-Control": _NO_STORE}
    return exc


def _locked(lock: LockReason) -> HTTPException:
    return _error(403, "CONNECTION_LOCKED", _EXPLANATION[lock])


async def _parse(request: Request, model: type[_M]) -> _M:
    """`model` from the raw body, with errors that describe the shape and never repeat the input."""
    raw = await _read_body(request)
    text = raw.decode("utf-8", errors="replace")
    if _ACCESS_KEY_ID.search(text) or _SECRET_KEY.search(text) or _SESSION_TOKEN.search(text):
        # Checked on the raw text as well as the parsed value: a key hidden under a duplicated
        # field name, or in a body too deep to walk, is still a key that was pasted.
        raise _error(422, "CREDENTIALS_NOT_ACCEPTED", _NOT_ACCEPTED)
    if not raw.strip():
        data: Any = {}
    else:
        try:
            data = json.loads(raw, object_pairs_hook=_unique_keys)
        except _DuplicateKeyError:
            raise _error(422, "BODY_INVALID", "The request body names a field more than once.") from None
        except (ValueError, RecursionError):
            raise _error(422, "BODY_INVALID", "The request body is not JSON.") from None
    if _too_deep(data):
        raise _error(422, "BODY_INVALID", "The request body is nested deeper than this endpoint accepts.")
    if _looks_like_credentials(data):
        raise _error(422, "CREDENTIALS_NOT_ACCEPTED", _NOT_ACCEPTED)
    if not isinstance(data, dict):
        raise _error(422, "BODY_INVALID", "The request body must be a JSON object.")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()} - {""})
        accepted = ", ".join(f"`{name}`" for name in model.model_fields)
        raise _error(
            422,
            "BODY_INVALID",
            f"This endpoint accepts {accepted}. "
            + (
                f"Check {', '.join(_safe_names(fields))}." if fields else "The body did not match that shape."
            ),
        ) from None


async def _read_body(request: Request) -> bytes:
    """The body, refused with `413` as soon as it is longer than a connection setting can be.

    Streamed rather than `await request.body()`, which buffers the whole upload first: a declared or
    chunked body of any size would otherwise be held in memory before its length was ever checked.
    """
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
    return _error(413, "BODY_TOO_LARGE", "This request body is larger than a connection setting can be.")


class _DuplicateKeyError(ValueError):
    """A JSON object named one field twice. Never carries the field or its values."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads`' object hook: a dict, unless a key repeats - `json` would silently keep the last."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _DuplicateKeyError
    return result


def _walk(value: Any) -> list[tuple[Any, int]]:
    """Every value inside `value` with its depth, walked with a stack rather than recursion and stopping
    below `_MAX_DEPTH`, so the walk is bounded however the body was built."""
    found: list[tuple[Any, int]] = []
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        found.append((item, depth))
        if depth > _MAX_DEPTH:
            continue
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return found


def _too_deep(value: Any) -> bool:
    return any(depth > _MAX_DEPTH for _, depth in _walk(value))


def _looks_like_credentials(value: Any) -> bool:
    """True when anywhere in `value` a key is named like a secret, or a string is shaped like a key.

    The whole value is walked, because `ConnectionTestRequest` nests `AwsConnection` and a key pasted
    one level down is still a key pasted into a web page.
    """
    for item, _depth in _walk(value):
        if isinstance(item, dict) and any(
            isinstance(key, str) and (_CREDENTIAL_KEY.search(key) or _looks_like_key(key)) for key in item
        ):
            return True
        if isinstance(item, str) and _looks_like_key(item):
            return True
    return False


def _looks_like_key(text: str) -> bool:
    return any(pattern.search(text) for pattern in (_ACCESS_KEY_ID, _SECRET_KEY, _SESSION_TOKEN))


def _safe_names(fields: list[str]) -> list[str]:
    """Field paths to name in an error, limited to the model's own vocabulary.

    A path is only ever echoed if every part of it is an identifier this module could have written;
    anything else - a key the caller invented - is summarised rather than repeated.
    """
    named = [f"`{field}`" for field in fields if set(field.split(".")) <= _KNOWN_FIELDS]
    return named if len(named) == len(fields) else [*named, "fields this endpoint does not accept"]
