"""`/connection/aws`: choose, check and show the AWS identity Bedrock is called as.

The screen above this module lets different people use different AWS credentials, and this module is
where that stays safe. The rules are `engine/aws_connection.py`'s; what is added here is the part
only an HTTP boundary can get wrong.

**A body is parsed here, never by FastAPI's default validation.** The default `422` repeats the
offending input back to the caller - measured, a request carrying `aws_secret_access_key` gets the
secret reflected in the response, and so does a bare JSON string. That puts the key into devtools,
into any proxy that logs responses and into every HAR file attached to a bug report. So these routes
read the raw body, and an error names what is *accepted* rather than repeating what was sent.

**A pasted credential is recognised and explained.** A field whose name looks like a secret, or any
value shaped like an access key id, gets `CREDENTIALS_NOT_ACCEPTED`: a sentence saying this product
never takes keys, what to do instead, and that a key which has been pasted into a web page should be
rotated. A person making that mistake deserves to find out immediately, not to believe it worked.

**Who may choose is decided by the deployment and the socket, never by a header.**
`request.client.host` is the peer that opened the connection; `X-Forwarded-For` is whatever a
caller chose to write. Only the first is consulted, and even that only unlocks a `local` deployment
(`engine.aws_connection.editability`).

**The check runs off the event loop.** It makes up to four AWS calls with ten-second timeouts, and
running them inline would freeze every other request for as long as AWS took to answer.

**Nothing here is cached.** Every response carries `Cache-Control: no-store`: an identity is the kind
of answer a shared browser or an intermediate cache should not keep.
"""

from __future__ import annotations

import json
import os
import re
from functools import partial
from typing import Any, Final, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from api.deps import ConfigRootDep
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
from engine.settings import Settings, settings

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
        "The AWS identity can only be changed from the machine running Marketing AI. From another "
        "machine you can test the connection, but not change it."
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
def get_aws_connection(request: Request, response: Response) -> AwsConnectionState:
    """Where the credentials come from - never the credentials themselves."""
    response.headers["Cache-Control"] = _NO_STORE
    current = settings()
    return _state(current, editability(current, _peer(request)))


# ---------------------------------------------------------------------------
# PUT /connection/aws
# ---------------------------------------------------------------------------
@router.put(
    "/connection/aws",
    response_model=AwsConnectionState,
    summary="Choose the AWS identity: the default chain, or an AWS CLI profile by name (local only)",
    openapi_extra=_request_body(AwsConnection),
)
async def put_aws_connection(request: Request, response: Response) -> AwsConnectionState:
    """Save the choice. `403 CONNECTION_LOCKED` on a deployment or from another machine."""
    response.headers["Cache-Control"] = _NO_STORE
    connection = await _parse(request, AwsConnection)
    current = settings()
    lock = editability(current, _peer(request))
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
def delete_aws_connection(request: Request, response: Response) -> AwsConnectionState:
    """Return to the default chain. `403 CONNECTION_LOCKED` on a deployment or from another machine."""
    response.headers["Cache-Control"] = _NO_STORE
    current = settings()
    lock = editability(current, _peer(request))
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
    request: Request, response: Response, config_root: ConfigRootDep
) -> ConnectionReport:
    """Who the credentials are, and which models they can use. Masked for any caller who cannot edit."""
    response.headers["Cache-Control"] = _NO_STORE
    body = await _parse(request, ConnectionTestRequest)
    current = settings()
    lock = editability(current, _peer(request))
    if body.connection is not None and lock is not None:
        raise _locked(lock)
    use_case = use_case_config(body.use_case_id, config_root)
    if use_case.generative is None:
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
    return await run_in_threadpool(check)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _peer(request: Request) -> str | None:
    """The address that opened this connection. Deliberately not `X-Forwarded-For`, which is a claim."""
    return request.client.host if request.client is not None else None


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
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise _error(413, "BODY_TOO_LARGE", "This request body is larger than a connection setting can be.")
    if not raw.strip():
        data: Any = {}
    else:
        try:
            data = json.loads(raw)
        except ValueError:
            raise _error(422, "BODY_INVALID", "The request body is not JSON.") from None
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


def _looks_like_credentials(value: Any) -> bool:
    """True when anywhere in `value` a key is named like a secret, or a string is shaped like a key id.

    Recursive, because `ConnectionTestRequest` nests `AwsConnection` and a key pasted one level down
    is still a key pasted into a web page.
    """
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and (_CREDENTIAL_KEY.search(key) or _ACCESS_KEY_ID.search(key)))
            or _looks_like_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_looks_like_credentials(item) for item in value)
    return isinstance(value, str) and _ACCESS_KEY_ID.search(value) is not None


def _safe_names(fields: list[str]) -> list[str]:
    """Field paths to name in an error, limited to the model's own vocabulary.

    A path is only ever echoed if every part of it is an identifier this module could have written;
    anything else - a key the caller invented - is summarised rather than repeated.
    """
    named = [f"`{field}`" for field in fields if set(field.split(".")) <= _KNOWN_FIELDS]
    return named if len(named) == len(fields) else [*named, "fields this endpoint does not accept"]
