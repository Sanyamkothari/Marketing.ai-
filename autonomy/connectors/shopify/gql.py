"""GraphQL calls through the connection's `ConnectorHttp`, with Shopify's error codes read properly.

`ConnectorHttp.graphql` turns every top-level error into a `RemoteError`. Shopify answers some
problems with HTTP 200 and an error code instead, and they mean different things to the engine:

* `THROTTLED`: never reaches here; the client's `retry_signal` (`graphql_throttled`) waits for the
  restore time and retries, and raises `RateLimited` when that wait is too long to sleep;
* `ACCESS_DENIED`: the token lacks a scope, or the app lacks protected-customer-data access for a
  field, so `AccessDenied` (a person must reconnect, or approve the access);
* `MAX_COST_EXCEEDED`, `BAD_REQUEST` and the rest: a `RemoteError` with the codes, no data shown.

`mutate` sends a mutation with an idempotency key: the client retries it after an ambiguous
failure (a timeout) only because the key is there, and every mutation the connector sends is safe
to repeat (a deterministic discount code, adding a tag that is already there).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autonomy.connectors.base import AccessDenied, ConnectionContext, RemoteError
from autonomy.connectors.http import HttpResult

__all__ = ["GRAPHQL_PATH", "mutate", "query", "user_errors"]

GRAPHQL_PATH = "graphql.json"


def query(ctx: ConnectionContext, text: str, variables: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """A read: retried after timeouts and 5xx, paced by Shopify's cost header."""
    result = ctx.http.send(
        "POST", GRAPHQL_PATH, json_body={"query": text, "variables": dict(variables or {})}, retry_safe=True
    )
    return _data(result)


def mutate(
    ctx: ConnectionContext, text: str, variables: Mapping[str, Any], *, idempotency_key: str
) -> Mapping[str, Any]:
    result = ctx.http.send(
        "POST",
        GRAPHQL_PATH,
        json_body={"query": text, "variables": dict(variables)},
        idempotency_key=idempotency_key,
    )
    return _data(result)


def user_errors(payload: Any) -> list[Mapping[str, Any]]:
    """A mutation payload's `userErrors`, tolerant of their absence."""
    errors = payload.get("userErrors") if isinstance(payload, Mapping) else None
    return [e for e in errors if isinstance(e, Mapping)] if isinstance(errors, list) else []


def _data(result: HttpResult) -> Mapping[str, Any]:
    if not result.ok:
        raise RemoteError(f"POST {result.path} answered HTTP {result.status}", status=result.status)
    body = result.json()
    if not isinstance(body, Mapping):
        raise RemoteError(f"POST {result.path} returned something other than a JSON object")
    errors = body.get("errors")
    if errors:
        listed = errors if isinstance(errors, list) else [errors]
        codes = sorted({_code(e) for e in listed})
        if "ACCESS_DENIED" in codes:
            raise AccessDenied(
                "Shopify refused a field we read (ACCESS_DENIED): reconnect to grant the scopes; if it "
                "persists, the app's protected customer data access must be approved",
                status=403,
            )
        raise RemoteError(f"POST {result.path} returned GraphQL errors: {', '.join(codes)}")
    data = body.get("data")
    return data if isinstance(data, Mapping) else {}


def _code(error: Any) -> str:
    if isinstance(error, Mapping):
        extensions = error.get("extensions")
        if isinstance(extensions, Mapping) and extensions.get("code"):
            return str(extensions["code"])
    return "ERROR"
