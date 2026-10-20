"""Klaviyo API facts the connector is written against, and the per-endpoint pacer.

Source of truth: Klaviyo's own OpenAPI document for revision **2026-10-15** (the `stable.json` in
github.com/klaviyo/openapi, read 2026-10-20), plus the OAuth guide and its troubleshooting page.
Every endpoint shape, filter, page size, scope and rate limit below is copied from that document;
anything the spec does not cover (OAuth, the names of Klaviyo's built-in metrics, template tags) is
marked *verify* in `docs/v2/CONNECTORS.md`.

Rate limits are **per endpoint** with two fixed windows, a burst (per second) and a steady (per
minute) one, per account (an OAuth app gets its own quota per install; a private key shares the
account's with every other integration). The base client paces one bucket per connection and reads
`RateLimit-*` / `Retry-After`; `EndpointPacer` adds a burst and a steady bucket per endpoint group,
so the 1/s create-flow limit is never hit by the same connection that reads events at 350/s.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final

from autonomy.channels.ratelimit import Rate, TokenBucket
from autonomy.connectors.base import ApiVersion, RateLimited
from autonomy.connectors.http import Clock
from autonomy.connectors.oauth import OAuthProvider

__all__ = [
    "API_BASE",
    "API_HOST",
    "API_REVISION",
    "AUTONOMY_FLOW_PREFIX",
    "DECISION_METRIC",
    "ENDPOINT_LIMITS",
    "JSONAPI",
    "KLAVIYO_OAUTH",
    "KLAVIYO_SCOPES",
    "EndpointLimit",
    "EndpointPacer",
    "klaviyo_time",
    "parse_klaviyo_time",
]

API_HOST: Final[str] = "a.klaviyo.com"
API_BASE: Final[str] = f"https://{API_HOST}/api"
API_REVISION: Final[ApiVersion] = ApiVersion(
    value="2026-10-15",
    header="revision",
    doc_date=date(2026, 10, 20),
    docs_url="https://developers.klaviyo.com/en/v2026-10-15/reference/api_overview",
    # Klaviyo keeps a revision stable for two years and deprecates it after one; bump well before.
    review_by=date(2027, 7, 15),
)
JSONAPI: Final[str] = "application/vnd.api+json"
"""Klaviyo's media type for request bodies and responses (JSON:API)."""

DECISION_METRIC: Final[str] = "Autonomy Decision"
"""The custom metric every handed-off decision is recorded as; the send flows trigger on it."""
AUTONOMY_FLOW_PREFIX: Final[str] = "Autonomy ·"
"""Flows and templates Autonomy creates start with this, so they are easy to find in Klaviyo."""

KLAVIYO_SCOPES: Final[tuple[str, ...]] = (
    "accounts:read",  # identify the account; default sender for the send flow
    "profiles:read",  # profiles and their subscriptions (consent)
    "lists:read",
    "segments:read",
    "metrics:read",
    "events:read",  # engagement and behaviour
    "events:write",  # the decision events that trigger the send flows
    "flows:read",
    "flows:write",  # create the send flows on connect
    "templates:read",
    "templates:write",
)

KLAVIYO_OAUTH: Final[OAuthProvider] = OAuthProvider(
    name="Klaviyo",
    authorize_url="https://www.klaviyo.com/oauth/authorize",
    token_url=f"https://{API_HOST}/oauth/token",
    scopes=KLAVIYO_SCOPES,
    scope_separator=" ",
    use_pkce=True,  # required by Klaviyo (S256)
    client_auth="basic",  # client id and secret in an HTTP Basic header, form body
    revoke_url=f"https://{API_HOST}/oauth/revoke",
    revoked_errors=frozenset({"invalid_grant"}),  # expired after 90 days unused, revoked, uninstalled
    # A 401 from the token endpoint is `invalid_client`: our app's credentials, not the merchant's
    # grant. Reconnecting cannot fix it, so it is an error streak for an operator, not "reconnect".
    refresh_unauthorized_is_revoked=False,
    refresh_margin=timedelta(minutes=5),  # access tokens last an hour (read from expires_in)
    keep_alive_every=timedelta(days=30),  # refresh tokens die after 90 days without use
    min_refresh_interval=timedelta(seconds=10),  # at most 10 refreshes a minute
)


@dataclass(frozen=True)
class EndpointLimit:
    """One endpoint group's documented limits (`x-klaviyo-ratelimit` in the OpenAPI document)."""

    burst_per_second: float
    steady_per_minute: float


ENDPOINT_LIMITS: Final[Mapping[str, EndpointLimit]] = {
    "accounts": EndpointLimit(1, 15),  # GET /api/accounts
    "profiles": EndpointLimit(75, 750),  # GET /api/profiles
    "lists": EndpointLimit(75, 750),  # GET /api/lists
    "segments": EndpointLimit(75, 750),  # GET /api/segments
    "metrics": EndpointLimit(10, 150),  # GET /api/metrics
    "events:read": EndpointLimit(350, 3500),  # GET /api/events
    "events:write": EndpointLimit(350, 3500),  # POST /api/events
    "flows:read": EndpointLimit(3, 60),  # GET /api/flows
    "flows:create": EndpointLimit(1, 15),  # POST /api/flows (also 100 a day)
    "flows:update": EndpointLimit(3, 60),  # PATCH /api/flows/{id}
    "templates": EndpointLimit(75, 750),  # GET and POST /api/templates
}


@dataclass
class EndpointPacer:
    """A burst and a steady token bucket per (connection, endpoint group), deterministic in time.

    Each bucket is sized so no one-second (burst) or one-minute (steady) window can exceed the
    documented limit, whatever the timing.

    `acquire` waits in line (through the client's clock) when the wait is short, and raises
    `RateLimited(retry_at=...)` when it is longer than `max_inline_wait`, so a sync job is re-queued
    and a send is retried later instead of holding a worker."""

    limits: Mapping[str, EndpointLimit] = field(default_factory=lambda: dict(ENDPOINT_LIMITS))
    max_inline_wait: timedelta = timedelta(seconds=30)
    _buckets: dict[tuple[str, str], tuple[TokenBucket, TokenBucket]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _pair(self, connection_id: str, group: str) -> tuple[TokenBucket, TokenBucket] | None:
        limit = self.limits.get(group)
        if limit is None:
            return None
        key = (connection_id, group)
        pair = self._buckets.get(key)
        if pair is None:
            pair = (
                _window_bucket(limit.burst_per_second, timedelta(seconds=1), share=0.5),
                _window_bucket(limit.steady_per_minute, timedelta(minutes=1), share=0.25),
            )
            self._buckets[key] = pair
        return pair

    def acquire(self, connection_id: str, group: str, clock: Clock) -> None:
        """Take one request's worth from both buckets of `group`, waiting if that is quick."""
        with self._lock:
            pair = self._pair(connection_id, group)
        if pair is None:
            return
        burst, steady = pair
        for _ in range(3):  # a second look after a sleep; the clock may be shared with other work
            with self._lock:
                now = clock.now()
                wait = max(burst.wait(now), steady.wait(now))
                if wait <= timedelta(0):
                    burst.take(now)
                    steady.take(now)
                    return
            if wait > self.max_inline_wait:
                raise RateLimited(
                    f"Klaviyo's {group} limit is used up for now; the job comes back later",
                    retry_at=now + wait,
                    status=429,
                )
            clock.sleep(wait.total_seconds())
        raise RateLimited(f"Klaviyo's {group} limit is busy", retry_at=clock.now() + timedelta(seconds=1))


def _window_bucket(limit: float, window: timedelta, *, share: float) -> TokenBucket:
    """A token bucket that never lets more than `limit` requests into any `window`.

    Klaviyo counts in fixed windows; a bucket of capacity `c` refilling at `r` admits at most
    `c + r * window` in any window, so `c` is `share` of the limit (at least one request) and the
    refill is the rest spread over the window."""
    capacity = max(1.0, limit * share)
    refill = max(limit - capacity, limit * share) / window.total_seconds()
    return TokenBucket(Rate(capacity=capacity, per=timedelta(seconds=capacity / refill)))


def klaviyo_time(ts: datetime) -> str:
    """A filter value: UTC, whole seconds, `Z` (Klaviyo filters take unquoted ISO 8601 datetimes)."""
    return ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_klaviyo_time(value: object) -> datetime | None:
    """An ISO 8601 string from Klaviyo (with or without offset or microseconds) as an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
