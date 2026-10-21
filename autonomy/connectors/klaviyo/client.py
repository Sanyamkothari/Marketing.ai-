"""A small JSON:API layer over the base `ConnectorHttp` for Klaviyo.

It adds what every Klaviyo call needs on top of the polite base client (which already pins the
`revision` header, authenticates, retries 429/5xx with `Retry-After`, refreshes on 401 and keeps
errors secret-free):

* the per-endpoint pacing of `api.EndpointPacer` (burst and steady windows per endpoint group);
* Klaviyo's media type on requests and responses;
* Klaviyo's error body (`{"errors": [{"code", "title", "detail"}]}`) folded into the `RemoteError`
  message, redacted, so a refused flow creation says why;
* cursor pages (`links.next`, an absolute URL on a.klaviyo.com, which the base client accepts
  because it is the API's own host).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

from autonomy.connectors.base import ConnectionContext, RemoteError, redact
from autonomy.connectors.http import HttpResult, jsonapi_next
from autonomy.connectors.klaviyo.api import JSONAPI, EndpointPacer

__all__ = ["KlaviyoClient", "error_code"]

_HEADERS: Final[Mapping[str, str]] = {"Accept": JSONAPI}
_WRITE_HEADERS: Final[Mapping[str, str]] = {"Accept": JSONAPI, "Content-Type": JSONAPI}
_KEYISH: Final[re.Pattern[str]] = re.compile(r"\b(pk|kl|sk)_[A-Za-z0-9_\-]{6,}")
"""Klaviyo private keys (`pk_...`) and token-shaped words, should an error detail ever echo one."""


class KlaviyoClient:
    """One call's worth of Klaviyo access for one connection (built per `ConnectionContext`)."""

    def __init__(self, ctx: ConnectionContext, pacer: EndpointPacer) -> None:
        self.ctx = ctx
        self.http = ctx.http
        self.pacer = pacer
        self.connection_id = ctx.connection.connection_id

    def _pace(self, group: str) -> None:
        self.pacer.acquire(self.connection_id, group, self.http.clock)

    def get(self, path: str, group: str, params: Mapping[str, str | int] | None = None) -> Mapping[str, Any]:
        """GET a document (a path, or a `links.next` URL with its own query)."""
        self._pace(group)
        result = self.http.send("GET", path, params=params, headers=_HEADERS)
        return _document(_checked(result))

    def collect(
        self,
        path: str,
        group: str,
        params: Mapping[str, str | int] | None = None,
        *,
        max_pages: int = 20,
    ) -> list[Mapping[str, Any]]:
        """Every `data` item across up to `max_pages` pages (for small lists: metrics, flows, templates)."""
        items: list[Mapping[str, Any]] = []
        doc = self.get(path, group, params)
        for _ in range(max_pages):
            items.extend(i for i in _as_list(doc.get("data")) if isinstance(i, Mapping))
            nxt = jsonapi_next(doc)
            if nxt is None:
                break
            doc = self.get(nxt, group)
        return items

    def post(
        self,
        path: str,
        group: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
    ) -> Mapping[str, Any]:
        """POST a JSON:API body. Retried after an ambiguous failure only with an idempotency key or
        when the caller says the request is safe to repeat (it is deduplicated by Klaviyo)."""
        self._pace(group)
        result = self.http.send(
            "POST",
            path,
            json_body=dict(body),
            headers=_WRITE_HEADERS,
            idempotency_key=idempotency_key,
            retry_safe=retry_safe,
        )
        return _document(_checked(result))

    def patch(self, path: str, group: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self._pace(group)
        result = self.http.send("PATCH", path, json_body=dict(body), headers=_WRITE_HEADERS, retry_safe=True)
        return _document(_checked(result))

    def delete(self, path: str, group: str) -> bool:
        """DELETE a resource; True when it is gone (deleted now, or already missing: 404)."""
        self._pace(group)
        result = self.http.send("DELETE", path, headers=_HEADERS)
        if result.status == 404:
            return True
        _checked(result)
        return True


def _checked(result: HttpResult) -> HttpResult:
    if result.ok:
        return result
    code = error_code(result.body)
    detail = _error_detail(result.body)
    words = f"{result.method} {result.path} answered HTTP {result.status}"
    if code or detail:
        words += f" ({', '.join(p for p in (code, detail) if p)})"
    raise RemoteError(redact(words), status=result.status)


def _document(result: HttpResult) -> Mapping[str, Any]:
    if not result.body:
        return {}
    body = result.json()
    return body if isinstance(body, Mapping) else {}


def error_code(body: bytes) -> str | None:
    """The first JSON:API error's `code` (e.g. `invalid`, `not_found`, `permission_denied`)."""
    first = _first_error(body)
    code = first.get("code") if first else None
    return code if isinstance(code, str) and code else None


def _error_detail(body: bytes) -> str | None:
    first = _first_error(body)
    if not first:
        return None
    detail = first.get("detail") or first.get("title")
    return redact(_KEYISH.sub("***", str(detail)), limit=160) if detail else None


def _first_error(body: bytes) -> Mapping[str, Any] | None:
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        return errors[0]
    return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
