"""Three pure-ASGI middlewares the served app needs and the laptop never noticed (security review 2).

**`SettingsCORSMiddleware` (M-1).** The deployed process is `uvicorn api.main:app`, built by
`create_app()` with no settings, so a `settings.cors_origins` read at build time was always the
default `*`. This reads the origins on the first request, when `api.deps.get_settings` can load
them, and answers with Starlette's own `CORSMiddleware` built from that list. A laptop keeps `*`
(the default, DEC-024); `Settings` already refuses `*` on prod. If the settings cannot be loaded no
origin is allowed and the route reports the real problem.

**`BodyLimitMiddleware` (M-2).** FastAPI reads and parses a body before the sign-in dependency runs,
and Starlette spools a whole multipart file to disk before `create_upload` can apply
`max_file_size_mb`, so an anonymous caller could make the process hold gigabytes. A body no larger
than `SMALL_BODY_BYTES` (a sign-in, any ordinary request) is passed through untouched. A larger one
is passed only when its `Authorization` header authenticates, and then only up to
`MAX_JSON_BYTES` (or `MAX_UPLOAD_BYTES` for `multipart/form-data`, where the route applies the use
case's own, tighter limit; the multipart ceiling is the largest `max_file_size_mb` the configs
allow). A declared `Content-Length` is used only for an early refusal, before a byte is read; the
body is always counted as it streams, because a request carrying both `Content-Length` and
`Transfer-Encoding: chunked` is framed by the chunks under uvicorn's h11 parser and the declared
length means nothing. A request that fails the check is answered 401 or 413 and the rest of its
body is never handed to the app. `auth_mode=off` authenticates everyone, as it does for
the routes, except on `env=prod`, where the routes answer 503 and so does nothing here help a caller.

**`SecurityHeadersMiddleware` (L-2).** `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`
and a CSP that holds only `frame-ancestors 'none'`. A full `default-src 'self'` policy is not sent:
the screens carry inline `<style>` blocks and `style=` attributes and load Inter from Google Fonts,
so such a policy would blank the UI, and it cannot be proved against a real browser here. HSTS is
added on `env=prod` only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from engine.settings import Settings

__all__ = [
    "MAX_JSON_BYTES",
    "MAX_UPLOAD_BYTES",
    "MULTIPART_OVERHEAD_BYTES",
    "SMALL_BODY_BYTES",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
    "SettingsCORSMiddleware",
]

SMALL_BODY_BYTES: Final[int] = 1024 * 1024
"""A body this small needs no sign-in to be read: it is cheap, and a sign-in body is one of them."""

MAX_JSON_BYTES: Final[int] = 8 * 1024 * 1024
"""The largest non-multipart body a signed-in caller may send; no JSON route takes more than a
configuration document, and files travel as multipart."""

MULTIPART_OVERHEAD_BYTES: Final[int] = 1024 * 1024
"""What a multipart body may add to the file it carries: boundaries, part headers, form fields."""

MAX_UPLOAD_BYTES: Final[int] = 2048 * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES
"""The multipart ceiling when the configs cannot be read: the engine's own 2 GB file limit, plus overhead."""

_BODY_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _deployment_settings(scope: Scope) -> Settings | None:
    """The app's settings, loaded on first use off the event loop; None when they cannot be loaded."""
    app = scope.get("app")
    if app is None:
        return None
    existing = getattr(app.state, "settings", None)
    if isinstance(existing, Settings):
        return existing
    from api.deps import get_settings  # deferred: api.deps imports a good deal

    try:
        return await run_in_threadpool(get_settings, Request(scope))
    except Exception:  # the route reports a settings problem properly
        return None


class SettingsCORSMiddleware:
    """`CORSMiddleware` whose allowed origins are `Settings.cors_origins`, read on the first request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._built_for: tuple[str, ...] | None = None
        self._inner: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through, wrapped in a CORS layer for the configured origins."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        settings = await _deployment_settings(scope)
        origins: tuple[str, ...] = () if settings is None else tuple(settings.cors_origins)
        if self._inner is None or self._built_for != origins:
            self._inner = CORSMiddleware(
                self.app,
                allow_origins=list(origins),
                allow_credentials=False,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            self._built_for = origins
        await self._inner(scope, receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


def _refusal(status: int, code: str, message: str, *, challenge: bool = False) -> tuple[Message, Message]:
    """A `{"detail": {"code", "message", "path"}}` response, the shape every refusal here has."""
    body = json.dumps({"detail": {"code": code, "message": message, "path": None}}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if challenge:
        headers.append((b"www-authenticate", b"Bearer"))
    return (
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    )


def _largest_file_limit_bytes(root: Path) -> int:
    """The biggest `validation.max_file_size_mb` any use case (or the engine default) allows, in bytes."""
    from engine.config import list_use_case_ids, load_use_case_document  # deferred: a good deal to import

    limits = [
        int(load_use_case_document(use_case_id, root).get("validation", {}).get("max_file_size_mb", 0))
        for use_case_id in list_use_case_ids(root)
    ]
    largest = max(limits, default=0)
    if largest < 1:
        raise ValueError("no use case declares a file size limit")
    return largest * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES


class BodyLimitMiddleware:
    """Refuse an oversized or unauthenticated large body before the app reads it (M-2)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count every body as it streams; refuse early when a trustworthy `Content-Length` allows."""
        if scope["type"] != "http" or str(scope["method"]).upper() not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        multipart = (_header(scope, b"content-type") or "").lower().startswith("multipart/")
        # `Content-Length` frames the body only when `Transfer-Encoding` is absent: with both, h11
        # reads the chunks and the declared size says nothing about what will arrive.
        declared_text = _header(scope, b"content-length")
        declared = (
            int(declared_text)
            if declared_text and declared_text.isdigit() and _header(scope, b"transfer-encoding") is None
            else None
        )
        ceiling: int | None = None
        signed_in: bool | None = None
        if declared is not None and declared > SMALL_BODY_BYTES:
            # The size is known before a byte is read: decide now.
            signed_in = await self._signed_in(scope)
            if not signed_in:
                await self._refuse(send, *self._unauthenticated())
                return
            ceiling = await self._ceiling(scope, multipart)
            if declared > ceiling:
                await self._refuse(send, *self._too_large(ceiling))
                return
        await self._stream(scope, receive, send, multipart, signed_in, ceiling)

    @staticmethod
    def _unauthenticated() -> tuple[int, str, str, bool]:
        return 401, "AUTH_REQUIRED", "Sign in to do this.", True

    @staticmethod
    def _too_large(ceiling: int) -> tuple[int, str, str, bool]:
        return (
            413,
            "REQUEST_TOO_LARGE",
            f"The request is larger than the {ceiling // (1024 * 1024)} MB limit.",
            False,
        )

    @staticmethod
    async def _refuse(send: Send, status: int, code: str, message: str, challenge: bool) -> None:
        start, body = _refusal(status, code, message, challenge=challenge)
        await send(start)
        await send(body)

    async def _ceiling(self, scope: Scope, multipart: bool) -> int:
        """The most a signed-in caller may send: JSON is capped low, a file at the largest configured limit."""
        if not multipart:
            return MAX_JSON_BYTES
        from api.deps import get_config_root  # deferred: api.deps imports a good deal

        try:
            root = get_config_root(Request(scope))
            return await run_in_threadpool(_largest_file_limit_bytes, root)
        except Exception:  # unreadable configs: the engine's own default limit
            return MAX_UPLOAD_BYTES

    async def _signed_in(self, scope: Scope) -> bool:
        """Whether this request's `Authorization` header authenticates (or sign-in is off)."""
        settings = await _deployment_settings(scope)
        if settings is None:
            return False
        if settings.auth_mode == "off":
            return settings.env != "prod"
        from api.access import get_identity  # deferred: api.access imports a good deal

        request = Request(scope)
        header = request.headers.get("authorization")
        if not header:
            return False

        def check() -> bool:
            try:
                return get_identity(request).authenticate(header) is not None
            except Exception:
                return False

        return await run_in_threadpool(check)

    async def _stream(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        multipart: bool,
        signed_in: bool | None,
        ceiling: int | None,
    ) -> None:
        """Count the body as it arrives and refuse at the first limit it crosses.

        `signed_in` and `ceiling` are already known when `Content-Length` was trusted and large;
        otherwise they are worked out when the body first outgrows the small allowance.
        """
        received = 0
        answered = False

        async def guarded_receive() -> Message:
            nonlocal received, signed_in, ceiling, answered
            message = await receive()
            if message["type"] != "http.request" or answered:
                return message
            received += len(message.get("body", b""))
            if received <= SMALL_BODY_BYTES:
                return message
            if signed_in is None:
                signed_in = await self._signed_in(scope)
            if signed_in and ceiling is None:
                ceiling = await self._ceiling(scope, multipart)
            if not signed_in or (ceiling is not None and received > ceiling):
                answered = True
                await self._refuse(
                    send, *(self._too_large(ceiling or 0) if signed_in else self._unauthenticated())
                )
                return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not answered:
                await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception:
            # The app was told the client went away (`ClientDisconnect` for a plain Starlette route)
            # after the refusal was already sent: that is not an error worth a traceback in the log.
            if not answered:
                raise


class SecurityHeadersMiddleware:
    """Add the basic response headers to every HTTP response (L-2)."""

    _HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
        (b"content-security-policy", b"frame-ancestors 'none'"),
    )
    _HSTS: Final[tuple[bytes, bytes]] = (b"strict-transport-security", b"max-age=31536000")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through, appending the headers a response does not already carry."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings: Any = getattr(scope.get("app"), "state", None)
        settings = getattr(settings, "settings", None)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {bytes(name).lower() for name, _ in headers}
                extra = list(self._HEADERS)
                if isinstance(settings, Settings) and settings.env == "prod":
                    extra.append(self._HSTS)
                headers.extend(pair for pair in extra if pair[0] not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
