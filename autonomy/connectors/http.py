"""A polite HTTP client for SaaS APIs: paced, retried, version-pinned, and quiet about secrets.

Every request a connector makes goes through `ConnectorHttp`, which:

* **pins the API version** with the headers the connector's `ApiVersion` names (or the URL does);
* **paces itself** with a token bucket (`autonomy.channels.ratelimit.TokenBucket`) at the
  connector's documented rate, and also reads the platform's own signals: `RateLimit-Remaining` /
  `RateLimit-Reset` (Klaviyo), GraphQL `throttleStatus` (Shopify, through a `pacing` hook);
* **retries** 429, 5xx, timeouts and dropped connections with exponential backoff and jitter
  (`autonomy.channels.gateway.RetryPolicy`: the jitter is fixed by the request, so runs repeat),
  honouring `Retry-After` (seconds or an HTTP date). A wait longer than `max_inline_wait` is not
  slept: it raises `RateLimited(retry_at=...)` so the job is re-queued for then and the worker is
  free;
* **retries a write only when it is safe**: a POST is retried after an ambiguous failure (a timeout)
  only when it carries an idempotency key, which is sent in `idempotency_header`;
* **refreshes once on 401** through `on_unauthorized` (the token manager), and raises `AuthRejected`
  if the fresh token is refused too; 403 is `AccessDenied`; 402/423 `SourceUnavailable`;
* **never sends a token to another host**: absolute URLs (a Klaviyo `links.next`) must be on the
  base URL's host; unauthenticated downloads elsewhere (a Shopify bulk-operation file) go through
  the v1 address guard (`engine.connections.net.check_url`) first;
* **says what went wrong without secrets**: messages carry the method and path, never the query
  string, a header or a body (`base.redact`).

Time is injected (`Clock`): tests use `ManualClock`, whose `sleep` moves time forward, so a test
with a 429 and a `Retry-After: 2` runs in microseconds. Only `SystemClock` reads the wall clock.

httpx is imported only when a client is first used, as in `autonomy.channels.whatsapp`; the
transport is injectable, so tests pass an `httpx.MockTransport`.
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import urlsplit

from autonomy.channels.gateway import RetryPolicy
from autonomy.channels.ratelimit import Rate, TokenBucket
from autonomy.connectors.base import (
    AccessDenied,
    AuthRejected,
    RateLimited,
    RateSpec,
    RemoteError,
    SourceUnavailable,
    TransientFailure,
    redact,
)

if TYPE_CHECKING:
    import httpx

__all__ = [
    "CONNECT_TIMEOUT_S",
    "READ_TIMEOUT_S",
    "Clock",
    "ConnectorHttp",
    "HttpPolicy",
    "HttpResult",
    "ManualClock",
    "SystemClock",
    "bucket_for",
    "graphql_page",
    "graphql_throttle_wait",
    "idempotency_key",
    "iterate_pages",
    "jsonapi_next",
    "link_header_next",
    "parse_retry_after",
]

CONNECT_TIMEOUT_S: Final[float] = 10.0
"""Same bound as v1's `engine.connections.base.CONNECT_TIMEOUT_S`."""
READ_TIMEOUT_S: Final[float] = 60.0
"""Same bound as v1's `engine.connections.base.READ_TIMEOUT_S`."""
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524})
SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


# --- time ----------------------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The wall clock. The only place in the connectors that reads it; wire it in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


@dataclass
class ManualClock:
    """A clock for tests and the Gym: `sleep` advances it, nothing waits."""

    current: datetime
    slept: list[float] = field(default_factory=list)

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.slept.append(seconds)
            self.current = self.current + timedelta(seconds=seconds)

    def advance(self, delta: timedelta) -> None:
        self.current = self.current + delta


# --- policy and results --------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpPolicy:
    """How one connection's client behaves. Built by the engine from the connector's capabilities."""

    base_url: str
    version_headers: tuple[tuple[str, str], ...] = ()
    default_headers: tuple[tuple[str, str], ...] = (("Accept", "application/json"),)
    connect_timeout: float = CONNECT_TIMEOUT_S
    read_timeout: float = READ_TIMEOUT_S
    max_attempts: int = 5
    retry: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(
            base=timedelta(seconds=1), factor=2.0, cap=timedelta(seconds=60), max_rounds=5, jitter=0.25
        )
    )
    max_inline_wait: timedelta = timedelta(seconds=30)
    idempotency_header: str | None = "Idempotency-Key"
    user_agent: str = "Autonomy-Connectors/1.0"
    max_body_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class HttpResult:
    """A final response: what the client returns after pacing and retries."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    method: str
    path: str

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise RemoteError(f"{self.method} {self.path} returned a body that is not JSON") from exc

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


Pacing = Callable[[HttpResult], float | None]
"""After a successful response: seconds to wait before the next request (None: no opinion)."""
RetrySignal = Callable[[HttpResult], float | None]
"""On a 2xx: seconds after which to retry because the body says so (GraphQL THROTTLED), or None."""
HeaderSource = Callable[[], Mapping[str, str]]
"""Returns the auth headers for the next request (asked before every attempt, so a refresh is seen)."""


def bucket_for(spec: RateSpec | None) -> TokenBucket | None:
    """A token bucket for a connector's documented rate (None: no client-side pacing).

    The bucket holds `burst` tokens and refills at `requests / per`, so it takes
    `per * burst / requests` to fill from empty."""
    if spec is None:
        return None
    burst = spec.burst or spec.requests
    return TokenBucket(Rate(capacity=burst, per=spec.per * (burst / spec.requests)))


def idempotency_key(*parts: object) -> str:
    """A deterministic key for a write: the same decision, action and target always give the same key."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]
    return f"idem_{digest}"


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """`Retry-After` as seconds from `now`: an integer, a decimal, or an HTTP date."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class ConnectorHttp:
    """One connection's HTTP client. See the module docstring for everything it promises."""

    def __init__(
        self,
        policy: HttpPolicy,
        clock: Clock,
        *,
        transport: httpx.BaseTransport | None = None,
        auth: HeaderSource | None = None,
        on_unauthorized: Callable[[], None] | None = None,
        bucket: TokenBucket | None = None,
        pacing: Pacing | None = None,
        retry_signal: RetrySignal | None = None,
        host_guard: Callable[[str], None] | None = None,
    ) -> None:
        self.policy = policy
        self.clock = clock
        self._transport = transport
        self._auth = auth
        self._on_unauthorized = on_unauthorized
        self._bucket = bucket
        self._pacing = pacing
        self._retry_signal = retry_signal
        self._host_guard = host_guard
        self._client: httpx.Client | None = None
        self._not_before: datetime | None = None
        self.requests_made = 0
        base = urlsplit(policy.base_url)
        if base.scheme != "https" and not (base.scheme == "http" and transport is not None):
            raise ValueError("a connector's base URL must be https")
        self._host = (base.hostname or "").lower()
        self._scheme = base.scheme
        self._port = base.port

    # -- lifecycle ---------------------------------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            import httpx

            timeout = httpx.Timeout(
                self.policy.read_timeout,
                connect=self.policy.connect_timeout,
            )
            self._client = httpx.Client(transport=self._transport, timeout=timeout, follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ConnectorHttp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- requests ----------------------------------------------------------------------------------

    def url(self, path_or_url: str) -> str:
        """An absolute URL on the base host. Another host is refused (it would receive our token)."""
        if path_or_url.startswith(("http://", "https://")):
            target = urlsplit(path_or_url)
            if (
                (target.hostname or "").lower() != self._host
                or target.scheme != self._scheme
                or target.port != self._port
            ):
                raise RemoteError("refused to send credentials to a host other than the API's")
            return path_or_url
        return self.policy.base_url.rstrip("/") + "/" + path_or_url.lstrip("/")

    def send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        authenticated: bool = True,
        retry_safe: bool = False,
    ) -> HttpResult:
        """Send with pacing and retries; return the final response (any status the caller must judge).

        Raises `RateLimited`/`TransientFailure` when retries run out or a wait is too long to sleep,
        `AuthRejected` on a second 401, `AccessDenied` on 403, `SourceUnavailable` on 402/423.
        `retry_safe` marks a POST that only reads (a GraphQL query), so it may be retried."""
        method = method.upper()
        url = self.url(path)
        shown = urlsplit(url).path or "/"
        retry_ok = retry_safe or method in SAFE_METHODS or idempotency_key is not None
        refreshed = False
        attempt = 0
        key = f"{method} {shown} {idempotency_key or ''}"
        while True:
            attempt += 1
            self._pace()
            request_headers = self._headers(headers, idempotency_key, authenticated)
            try:
                response = self._http().request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=json_body,
                    data=dict(form) if form is not None else None,
                    headers=request_headers,
                )
                self.requests_made += 1
            except Exception as exc:  # httpx.TransportError and friends: timeouts, resets, DNS
                if not _is_transport_error(exc):
                    raise
                self.requests_made += 1
                if not retry_ok:
                    raise TransientFailure(
                        f"{method} {shown} failed without an answer ({type(exc).__name__}); not retried "
                        "because the request has no idempotency key"
                    ) from None
                self._backoff_or_raise(attempt, key, None, f"{method} {shown}: {type(exc).__name__}")
                continue
            body = response.content
            if len(body) > self.policy.max_body_bytes:
                raise RemoteError(
                    f"{method} {shown} returned more than the {self.policy.max_body_bytes} bytes allowed"
                )
            result = HttpResult(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
                method=method,
                path=shown,
            )
            self._note_limits(result)
            if result.status == 401 and authenticated:
                if self._on_unauthorized is not None and not refreshed:
                    refreshed = True
                    self._on_unauthorized()  # forces a token refresh; `auth` then returns the new one
                    continue
                raise AuthRejected(
                    f"{method} {shown} was refused: the credentials are no longer accepted", status=401
                )
            if result.status == 403 and authenticated:
                raise AccessDenied(
                    f"{method} {shown} is not allowed with the permissions granted", status=403
                )
            if result.status in (402, 423):
                raise SourceUnavailable(
                    f"{method} {shown}: the account cannot be read right now (HTTP {result.status})",
                    status=result.status,
                    retry_at=self.clock.now() + timedelta(hours=1),
                )
            if result.status in RETRYABLE_STATUS:
                if not retry_ok and result.status != 429:
                    return result
                wait = parse_retry_after(result.header("retry-after"), self.clock.now())
                self._backoff_or_raise(
                    attempt, key, wait, f"{method} {shown} answered {result.status}", result.status
                )
                continue
            if result.ok and self._retry_signal is not None:
                wait = self._retry_signal(result)
                if wait is not None:
                    self._backoff_or_raise(attempt, key, wait, f"{method} {shown} was throttled", 429)
                    continue
            if result.ok and self._pacing is not None:
                wait = self._pacing(result)
                if wait is not None and wait > 0:
                    self._defer(wait)
            return result

    def json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        authenticated: bool = True,
        accept: Sequence[int] = (),
    ) -> Any:
        """`send`, then the JSON body of a 2xx (or a status in `accept`); `RemoteError` for anything else."""
        result = self.send(
            method,
            path,
            params=params,
            json_body=json_body,
            form=form,
            headers=headers,
            idempotency_key=idempotency_key,
            authenticated=authenticated,
        )
        if not (result.ok or result.status in accept):
            raise RemoteError(
                f"{result.method} {result.path} answered HTTP {result.status}", status=result.status
            )
        return result.json()

    def graphql(
        self, query: str, variables: Mapping[str, Any] | None = None, *, path: str = "graphql.json"
    ) -> Any:
        """POST a GraphQL query; return `data`. Top-level `errors` raise `RemoteError` (THROTTLED retries).

        A query is a read, so it is safe to retry; mutations should pass through `send` with an
        idempotency key of their own."""
        result = self.send(
            "POST",
            path,
            json_body={"query": query, "variables": dict(variables or {})},
            retry_safe=True,
        )
        if not result.ok:
            raise RemoteError(f"POST {result.path} answered HTTP {result.status}", status=result.status)
        body = result.json()
        if not isinstance(body, Mapping):
            raise RemoteError(f"POST {result.path} returned something other than a JSON object")
        errors = body.get("errors")
        if errors:
            codes = sorted({str(_get(e, "extensions", "code") or "ERROR") for e in _as_list(errors)})
            raise RemoteError(f"POST {result.path} returned GraphQL errors: {', '.join(codes)}")
        return body.get("data")

    def download_lines(self, url: str) -> Iterator[bytes]:
        """Stream a file at an absolute URL line by line, without our credentials (signed URLs).

        For bulk exports (Shopify bulk-operation JSONL on cloud storage). The host passes the v1
        address guard first, so a crafted URL cannot reach instance metadata. A connection that
        breaks off mid-file is resumed where it stopped (`download_lines_from`)."""
        for line, _offset in self.download_lines_from(url):
            yield line

    def download_lines_from(self, url: str, start: int = 0) -> Iterator[tuple[bytes, int]]:
        """`(line, offset after it)` from byte `start` on, so a big export can be read in bounded pages.

        A connector keeps the offset in its page cursor (e.g. `"<operation id>@<offset>"`), stops
        after N lines, and the next page continues with `start=offset`: memory stays bounded and the
        file is not downloaded again from the top. A `Range` request is sent from `start`; a server
        that ignores it (200) has the first `start` bytes skipped. A connection that breaks off is
        re-requested from the last whole line handed out. Line endings (`\n`, `\r\n`) are stripped;
        empty lines are skipped."""
        parts = urlsplit(url)
        if parts.scheme != "https" and self._transport is None:
            raise RemoteError("a download URL must be https")
        if self._host_guard is not None:
            self._host_guard(url)
        offset = max(0, start)
        attempt = 0
        while True:
            attempt += 1
            headers = {"User-Agent": self.policy.user_agent}
            if offset > 0:
                headers["Range"] = f"bytes={offset}-"
            try:
                with self._http().stream("GET", url, headers=headers) as response:
                    self.requests_made += 1
                    status = response.status_code
                    if status == 416:
                        return  # nothing after `offset`: the file was read to the end
                    if status in RETRYABLE_STATUS:
                        self._backoff_or_raise(attempt, "download", None, f"download answered {status}")
                        continue
                    if status >= 400:
                        raise RemoteError(f"the download answered HTTP {status}", status=status)
                    skip = offset if (status == 200 and offset > 0) else 0
                    position = offset
                    pending = b""
                    for chunk in response.iter_bytes():
                        if skip:
                            dropped = min(skip, len(chunk))
                            chunk, skip = chunk[dropped:], skip - dropped
                        pending += chunk
                        while (cut := pending.find(b"\n")) >= 0:
                            line, pending = pending[:cut], pending[cut + 1 :]
                            position += cut + 1
                            offset = position
                            stripped = line.rstrip(b"\r")
                            if stripped:
                                yield stripped, position
                    if pending.rstrip(b"\r"):
                        position += len(pending)
                        yield pending.rstrip(b"\r"), position
                    return
            except Exception as exc:
                if not _is_transport_error(exc):
                    raise
                self._backoff_or_raise(attempt, "download", None, f"download failed ({type(exc).__name__})")

    # -- internals --------------------------------------------------------------------------------

    def _headers(
        self, extra: Mapping[str, str] | None, idem: str | None, authenticated: bool
    ) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": self.policy.user_agent}
        headers.update(dict(self.policy.default_headers))
        headers.update(dict(self.policy.version_headers))
        if authenticated and self._auth is not None:
            headers.update(self._auth())
        if idem is not None and self.policy.idempotency_header:
            headers[self.policy.idempotency_header] = idem
        if extra:
            headers.update(extra)
        return headers

    def _pace(self) -> None:
        now = self.clock.now()
        if self._not_before is not None and self._not_before > now:
            self._wait((self._not_before - now).total_seconds(), "the platform's rate-limit reset")
            now = self.clock.now()
        if self._bucket is not None:
            wait = self._bucket.wait(now)
            if wait > timedelta(0):
                self._wait(wait.total_seconds(), "the connector's rate limit")
                now = self.clock.now()
            self._bucket.take(now)

    def _wait(self, seconds: float, why: str) -> None:
        if seconds > self.policy.max_inline_wait.total_seconds():
            raise RateLimited(
                f"waiting {seconds:.0f}s for {why}; the job comes back later",
                retry_at=self.clock.now() + timedelta(seconds=seconds),
            )
        self.clock.sleep(seconds)

    def _defer(self, seconds: float) -> None:
        until = self.clock.now() + timedelta(seconds=seconds)
        if self._not_before is None or until > self._not_before:
            self._not_before = until

    def _note_limits(self, result: HttpResult) -> None:
        """Klaviyo-style `RateLimit-Remaining: 0` + `RateLimit-Reset: <seconds>`: hold off until then."""
        remaining = result.header("ratelimit-remaining") or result.header("x-ratelimit-remaining")
        reset = result.header("ratelimit-reset") or result.header("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            left, seconds = float(remaining.split(",")[0]), float(reset.split(",")[0])
        except ValueError:
            return
        if left <= 0 and 0 < seconds < 3600:
            self._defer(seconds)

    def _backoff_or_raise(
        self, attempt: int, key: str, hinted: float | None, what: str, status: int | None = None
    ) -> None:
        if attempt >= self.policy.max_attempts:
            delay = self.policy.retry.delay(attempt, key).total_seconds()
            wait = max(delay, hinted or 0.0)
            failure = RateLimited if status == 429 else TransientFailure
            raise failure(
                f"{what}; gave up after {attempt} attempts",
                retry_at=self.clock.now() + timedelta(seconds=wait),
                status=status,
            )
        wait = hinted if hinted is not None else self.policy.retry.delay(attempt, key).total_seconds()
        self._wait(wait, redact(what))


def _is_transport_error(exc: BaseException) -> bool:
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is present wherever a client is built
        return False
    return isinstance(exc, httpx.TransportError)


# --- pagination helpers --------------------------------------------------------------------------


def jsonapi_next(body: Any) -> str | None:
    """The next page of a JSON:API response (Klaviyo): `links.next`, an absolute URL, or None."""
    nxt = _get(body, "links", "next")
    return nxt if isinstance(nxt, str) and nxt else None


def link_header_next(value: str | None) -> str | None:
    """The `rel="next"` URL of an RFC 8288 `Link` header, or None."""
    if not value:
        return None
    for part in value.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if any(p.strip().replace(" ", "") in ('rel="next"', "rel=next") for p in section[1:]):
            return url
    return None


def graphql_page(data: Any, path: Sequence[str]) -> tuple[list[Mapping[str, Any]], str | None]:
    """The nodes of a GraphQL connection at `path` and its next cursor (None when it is the last page).

    Accepts both `nodes` and `edges { node }`; schema drift in the shape gives an empty page, not a crash."""
    conn = _get(data, *path)
    if not isinstance(conn, Mapping):
        return [], None
    nodes: list[Mapping[str, Any]] = []
    if isinstance(conn.get("nodes"), list):
        nodes = [n for n in conn["nodes"] if isinstance(n, Mapping)]
    elif isinstance(conn.get("edges"), list):
        nodes = [
            e["node"] for e in conn["edges"] if isinstance(e, Mapping) and isinstance(e.get("node"), Mapping)
        ]
    info = conn.get("pageInfo")
    if isinstance(info, Mapping) and info.get("hasNextPage") and isinstance(info.get("endCursor"), str):
        return nodes, str(info["endCursor"])
    return nodes, None


def graphql_throttle_wait(result: HttpResult) -> float | None:
    """Seconds to wait before the next GraphQL call, from Shopify's `extensions.cost.throttleStatus`.

    When fewer points are left than the last query cost, wait until enough have restored."""
    try:
        body = result.json()
    except RemoteError:
        return None
    cost = _get(body, "extensions", "cost")
    status = _get(cost, "throttleStatus")
    try:
        requested = float(_get(cost, "requestedQueryCost") or 0)
        available = float(_get(status, "currentlyAvailable"))
        restore = float(_get(status, "restoreRate"))
    except (TypeError, ValueError):
        return None
    if restore <= 0 or available >= requested:
        return None
    return (requested - available) / restore


def graphql_throttled(result: HttpResult) -> float | None:
    """A `RetrySignal`: a 200 whose errors say THROTTLED retries after the restore time (or 1 s)."""
    try:
        body = result.json()
    except RemoteError:
        return None
    errors = _get(body, "errors")
    if not errors or not any(_get(e, "extensions", "code") == "THROTTLED" for e in _as_list(errors)):
        return None
    return graphql_throttle_wait(result) or 1.0


def iterate_pages(
    fetch: Callable[[str | None], tuple[Sequence[Mapping[str, Any]], str | None]],
    *,
    start: str | None = None,
    max_pages: int = 10_000,
) -> Iterator[Mapping[str, Any]]:
    """Every record across pages: `fetch(cursor) -> (records, next_cursor)` until the cursor is None.

    A cursor repeated twice in a row stops the loop (a platform bug would otherwise spin forever)."""
    cursor = start
    for _ in range(max_pages):
        records, nxt = fetch(cursor)
        yield from records
        if nxt is None or nxt == cursor:
            return
        cursor = nxt


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
