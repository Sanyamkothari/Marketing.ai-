"""The SaaS connector contract: what a Shopify or Klaviyo connector is, and the errors it raises.

v1's `engine.connections.Connector` is a person-driven, read-only reader of files and databases: a
form, a test, a browser, a preview and one bounded fetch. A SaaS connector is different. After one
OAuth (or API-key) connect it runs for months with nobody watching, so it is described by:

* **streams** it can read (customers, orders, products, events, consent, engagement), each with an
  expected cadence, a history depth for the first backfill, the webhook topics that feed it and the
  fields a page must carry (`StreamSpec`);
* optional **write actions** (Klaviyo: create an event that starts a flow) carried out with an
  idempotency key (`WriteActionSpec`);
* **webhook topics** it subscribes to, including the privacy ones a platform makes mandatory;
* a pinned **API version** with the date of the docs it was written against (`ApiVersion`);
* its **rate limit**, which the HTTP client paces against before the platform says 429.

A connector is a stateless catalogue object, like v1's: every call gets a `ConnectionContext` with
the connection, an authenticated HTTP client and the time. Where things are stored (positions,
tokens, deliveries) is the sync engine's business, not the connector's. What it keeps from v1: the
secrets live in the encrypted credential store's key scheme (`tokens.FernetCipher`), the address
guard (`engine.connections.net.check_url`) runs before any request to a host the connector did not
hard-code, and an error message never repeats a secret (`redact`).

Errors are a small hierarchy (`ConnectorFailure`) whose class says what the engine must do:
retry later (`RateLimited`, `TransientFailure`, `SourceUnavailable`), stop and ask a person once
(`NeedsReconnect`, `AuthRejected`, `AccessDenied`), restart a window (`CursorExpired`), or give up
on this one request (`RemoteError`, `NotSupported`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from autonomy.contracts import Event
from autonomy.data.mapping import EventMapping, MappingProblem, map_frame

if TYPE_CHECKING:
    from autonomy.connectors.http import ConnectorHttp, HttpResult
    from autonomy.connectors.oauth import AppCredentials, OAuthProvider
    from autonomy.connectors.tokens import TokenSet

__all__ = [
    "AccessDenied",
    "ApiVersion",
    "AuthMethod",
    "AuthRejected",
    "ConfigField",
    "Connection",
    "ConnectionContext",
    "ConnectionStatus",
    "ConnectorCapabilities",
    "ConnectorFailure",
    "CursorExpired",
    "Identity",
    "Lane",
    "MappedRecords",
    "NeedsReconnect",
    "NotSupported",
    "PrivacyKind",
    "RateLimited",
    "RateSpec",
    "RecordKey",
    "RemoteError",
    "SaasConnector",
    "SchemaDrift",
    "SetupMode",
    "SourceUnavailable",
    "Stream",
    "StreamPage",
    "StreamSpec",
    "SyncWindow",
    "Teardown",
    "TransientFailure",
    "WebhookDelivery",
    "WebhookTopic",
    "WriteActionSpec",
    "WriteResult",
    "map_with",
    "redact",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- what a connector can do ---------------------------------------------------------------------


class Stream(StrEnum):
    """The kinds of data a SaaS source feeds Autonomy. Every connector maps its own objects onto these."""

    CUSTOMERS = "customers"
    ORDERS = "orders"
    PRODUCTS = "products"
    EVENTS = "events"
    """Behaviour the platform records (viewed product, started checkout, Klaviyo metrics)."""
    CONSENT = "consent"
    """Marketing consent per channel. Stale consent is never acted on (`StreamSpec.critical`)."""
    ENGAGEMENT = "engagement"
    """What happened to messages the customer's tool sent for us: received, opened, clicked, bounced."""


class Lane(StrEnum):
    """Each stream syncs in two independent lanes, each with its own resumable position.

    `live` keeps recent data fresh (incremental by `updated_at`, with an overlap window, on the
    stream's cadence and behind every webhook). `history` walks back from the connect time in date
    chunks, newest first, until the stream's backfill depth is reached, so decisions can start
    before the whole history has loaded."""

    LIVE = "live"
    HISTORY = "history"


class AuthMethod(StrEnum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    """A private key pasted once (Klaviyo private keys for pilots, Shopify custom-app tokens)."""


class SetupMode(StrEnum):
    AUTOMATIC = "automatic"
    """Connect and walk away: everything the connector needs it creates through the API."""
    GUIDED = "guided"
    """One short, guided step in the customer's own tool that the API cannot do (e.g. a receiving flow)."""


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_RECONNECT = "needs_reconnect"
    """Access was revoked or no longer works. Nothing syncs; one alert asked a person to reconnect."""
    PAUSED = "paused"
    """Paused by a person. Tokens are still kept alive."""
    DISCONNECTED = "disconnected"
    """The app was uninstalled or the connection deleted. Tokens are gone."""


class PrivacyKind(StrEnum):
    """Privacy requests a platform sends by webhook (Shopify's three mandatory compliance topics)."""

    DATA_REQUEST = "data_request"
    REDACT_CUSTOMER = "redact_customer"
    REDACT_SHOP = "redact_shop"


class StreamSpec(_Model):
    """One stream a connector reads, and how the engine should keep it fresh."""

    stream: Stream
    cadence: timedelta = Field(
        default=timedelta(hours=1),
        description="How often the live lane polls. Streams fed by webhooks still poll this often: "
        "the catch-up poll is the safety net for deliveries that never arrive",
    )
    overlap: timedelta = Field(
        default=timedelta(hours=2),
        description="Each live poll re-reads this much before its watermark; stable ids dedupe it",
    )
    backfill_days: int = Field(
        default=730, ge=0, description="History depth of the first backfill; 0 for no history lane"
    )
    backfill_chunk: timedelta = Field(default=timedelta(days=7), description="One history job's window")
    min_history_days: int = Field(
        default=30,
        ge=0,
        description="How far back the history lane must have reached before decisions may rely on it",
    )
    webhook_topics: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = Field(
        default=(),
        description="Top-level fields every record must carry. Missing ones count toward schema "
        "drift; unknown extra fields are always ignored",
    )
    critical: bool = Field(
        default=True, description="Stale data here holds decisions for the business (orders, consent)"
    )
    snapshot: bool = Field(
        default=False,
        description="The source cannot filter by updated time: every live poll re-reads it whole",
    )


class WriteActionSpec(_Model):
    name: str
    description: str
    idempotent: bool = Field(default=True, description="Safe to repeat with the same idempotency key")


class WebhookTopic(_Model):
    topic: str
    stream: Stream | None = None
    privacy: PrivacyKind | None = None
    uninstall: bool = False
    mandatory: bool = Field(default=False, description="Required by the platform (compliance topics)")


class ApiVersion(_Model):
    """The platform API version a connector is pinned to, and when to look at it again."""

    value: str = Field(description="e.g. Shopify '2026-07', Klaviyo revision '2026-07-15'")
    header: str | None = Field(
        default=None, description="The header that carries it (Klaviyo: 'revision'); None when in the URL"
    )
    doc_date: date = Field(description="The date of the docs the connector was written against")
    docs_url: str
    review_by: date = Field(description="Bump before this date (the platform's support window)")


class RateSpec(_Model):
    """The steady rate the client paces to (a token bucket), below the platform's documented limit."""

    requests: float = Field(gt=0)
    per: timedelta = Field(default=timedelta(seconds=1))
    burst: float | None = Field(default=None, description="Bucket size; defaults to `requests`")


class ConfigField(_Model):
    """One value a person types at connect time (e.g. the shop's myshopify.com domain)."""

    name: str
    label: str
    help: str | None = None
    pattern: str | None = Field(default=None, description="A full-match regular expression")
    required: bool = True


class ConnectorCapabilities(_Model):
    """Everything the API and the Connect screen need to know about a connector, without calling it."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    label: str
    description: str
    auth: AuthMethod
    setup: SetupMode
    setup_note: str | None = Field(default=None, description="What the guided step asks, in plain words")
    streams: tuple[StreamSpec, ...] = ()
    write_actions: tuple[WriteActionSpec, ...] = ()
    webhook_topics: tuple[WebhookTopic, ...] = ()
    api_version: ApiVersion
    rate: RateSpec | None = None
    config_fields: tuple[ConfigField, ...] = ()
    regions: tuple[str, ...] = ("US", "UK", "AU")
    sends: bool = Field(default=False, description="Can send messages for Autonomy (decide here, send there)")

    def stream(self, stream: Stream) -> StreamSpec:
        for spec in self.streams:
            if spec.stream is stream:
                return spec
        raise NotSupported(f"{self.label} has no {stream.value} stream")

    def topic(self, topic: str) -> WebhookTopic | None:
        return next((t for t in self.webhook_topics if t.topic == topic), None)


# --- a connection and what moves through it ------------------------------------------------------


class Connection(_Model):
    """One business's link to one platform account. Tokens live in the vault, never here."""

    connection_id: str
    kind: str
    business_id: str
    label: str = Field(description="e.g. 'acme.myshopify.com'")
    external_id: str | None = Field(
        default=None, description="The platform's id for the account (shop domain, Klaviyo account id)"
    )
    status: ConnectionStatus = ConnectionStatus.ACTIVE
    status_reason: str | None = None
    config: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    history_anchor: datetime | None = Field(
        default=None,
        description="Where the history lane starts walking back from; the connect time by default",
    )

    @property
    def anchor(self) -> datetime:
        return self.history_anchor or self.created_at


class Identity(_Model):
    """Who a new connection is, as the platform says after connect."""

    external_id: str
    label: str


@dataclass(frozen=True)
class SyncWindow:
    """`start <= updated_at < end`. `start` None means from the beginning."""

    start: datetime | None
    end: datetime


@dataclass(frozen=True)
class StreamPage:
    """One page of a stream. `next_cursor` None means the window is finished.

    A platform that exports history asynchronously (Shopify bulk operations) returns a page with no
    records, the operation's id as `next_cursor` and `pending_until` set: the engine saves the
    cursor and comes back then instead of holding a worker."""

    records: tuple[Mapping[str, Any], ...] = ()
    next_cursor: str | None = None
    pending_until: datetime | None = None


@dataclass(frozen=True)
class RecordKey:
    """A record's stable id and the time it was last changed (None when the platform has none)."""

    record_id: str
    updated_at: datetime | None


@dataclass(frozen=True)
class MappedRecords:
    """Events made from records, and the records that could not be mapped (never fatal)."""

    events: tuple[Event, ...] = ()
    problems: tuple[MappingProblem, ...] = ()


@dataclass(frozen=True)
class WebhookDelivery:
    """A verified webhook, read: what it is about, and the records it carries."""

    delivery_id: str
    topic: str
    stream: Stream | None = None
    records: tuple[Mapping[str, Any], ...] = ()
    occurred_at: datetime | None = None
    privacy: PrivacyKind | None = None
    uninstalled: bool = False
    subject: Mapping[str, Any] = field(default_factory=dict)
    """For a privacy request: who it is about (ids, email), as the platform sent it."""


@dataclass(frozen=True)
class Teardown:
    """What `SaasConnector.teardown` did on the platform, and what a person must still do there, in words."""

    done: tuple[str, ...] = ()
    left: tuple[str, ...] = ()


@dataclass(frozen=True)
class WriteResult:
    accepted: bool
    provider_id: str | None = None
    detail: str | None = None


@dataclass
class ConnectionContext:
    """What a connector is handed for one call: the connection, an authenticated client and the time."""

    connection: Connection
    http: ConnectorHttp
    now: datetime
    app: AppCredentials | None = None
    secrets: Mapping[str, str] = field(default_factory=dict)
    """Per-connection secrets other than the OAuth tokens (e.g. a webhook signing secret)."""


# --- the connector -------------------------------------------------------------------------------


class SaasConnector:
    """Base class for a SaaS source (and optionally a send target). Subclasses override what they support.

    Required: `capabilities`, `base_url`, `read`, `record_key`, `map_records`. Everything else has
    a safe default: no webhooks (every delivery is rejected), no writes, no identity call.
    """

    capabilities: ConnectorCapabilities
    oauth: OAuthProvider | None = None
    """The OAuth provider for `AuthMethod.OAUTH2` connectors; None for API-key ones."""

    @property
    def kind(self) -> str:
        return self.capabilities.kind

    # -- how to talk to it ------------------------------------------------------------------------

    def base_url(self, connection: Connection) -> str:
        """The API root for this connection, e.g. https://acme.myshopify.com/admin/api/2026-07."""
        raise NotImplementedError

    def auth_headers(self, connection: Connection, tokens: TokenSet) -> dict[str, str]:  # noqa: ARG002
        """Headers that authenticate a request. Default: a bearer token."""
        return {"Authorization": f"Bearer {tokens.access_token}"}

    def pacing(self, result: HttpResult) -> float | None:  # noqa: ARG002
        """After a 2xx: seconds to hold off before the next request (Shopify: `http.graphql_throttle_wait`)."""
        return None

    def retry_signal(self, result: HttpResult) -> float | None:  # noqa: ARG002
        """On a 2xx that is really a throttle (GraphQL THROTTLED): seconds until a retry (`http.graphql_throttled`)."""
        return None

    def validate_callback(self, query: Mapping[str, str], app: AppCredentials) -> None:  # noqa: ARG002
        """Platform checks on the OAuth redirect before the code is exchanged (Shopify: the query `hmac`).

        Raise `oauth.AuthorizationError` to refuse it."""
        return

    def identify(self, ctx: ConnectionContext) -> Identity | None:  # noqa: ARG002
        """Who the account is, right after connect. None keeps what the connect flow knew."""
        return None

    def check(self, ctx: ConnectionContext) -> None:
        """A cheap call proving the credentials still work; raise a `ConnectorFailure` if not."""

    # -- reading ----------------------------------------------------------------------------------

    def read(
        self, ctx: ConnectionContext, stream: Stream, window: SyncWindow, cursor: str | None
    ) -> StreamPage:
        """One page of `stream` changed within `window`, continuing from `cursor` (None: first page)."""
        raise NotImplementedError

    def record_key(self, stream: Stream, record: Mapping[str, Any]) -> RecordKey:
        raise NotImplementedError

    def map_records(
        self, stream: Stream, records: Sequence[Mapping[str, Any]], connection: Connection
    ) -> MappedRecords:
        """Turn records into Autonomy events with stable ids (`autonomy.data.mapping` or `stable_id`)."""
        raise NotImplementedError

    # -- webhooks ---------------------------------------------------------------------------------

    def webhook_secret(
        self, app: AppCredentials | None, secrets: Mapping[str, str]  # noqa: ARG002
    ) -> str | None:
        """The key deliveries are signed with. Default: the app's client secret (Shopify does this)."""
        return app.client_secret if app is not None else None

    def webhook_account(self, headers: Mapping[str, str], body: bytes) -> str | None:  # noqa: ARG002
        """The platform account a delivery is for (Shopify: X-Shopify-Shop-Domain), to find the connection."""
        return None

    def verify_webhook(self, secret: str, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        """Constant-time signature check of the raw body. Default: reject (fail closed)."""
        return False

    def webhook_delivery_id(self, headers: Mapping[str, str], body: bytes) -> str | None:  # noqa: ARG002
        """The platform's id for this delivery (dedupe key). None: the engine hashes the body."""
        return None

    def webhook_topic(self, headers: Mapping[str, str], body: bytes) -> str | None:  # noqa: ARG002
        """The delivery's topic (Shopify: X-Shopify-Topic), for the catch-up poll of its stream."""
        return None

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookDelivery:
        del headers, body
        raise NotSupported(f"{self.capabilities.label} does not send webhooks")

    def register_webhooks(
        self, ctx: ConnectionContext, callback_url: str, topics: Sequence[str]  # noqa: ARG002
    ) -> tuple[str, ...]:
        """Make sure every topic is subscribed to `callback_url`; idempotent; return the topics now live."""
        return ()

    def teardown(self, ctx: ConnectionContext, tokens: TokenSet | None) -> Teardown:  # noqa: ARG002
        """A person disconnected: remove what Autonomy set up on the platform, then revoke our access.

        Called once, while the connection still works, before its tokens are forgotten. Best effort
        and it must not raise for a platform refusal: what the API does not allow comes back in
        `Teardown.left` as one plain instruction. Default: nothing to remove."""
        return Teardown()

    # -- writing ----------------------------------------------------------------------------------

    def write(
        self,
        ctx: ConnectionContext,
        action: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> WriteResult:
        """Carry out one write action with `idempotency_key` (sent to the platform where it takes one)."""
        del ctx, payload, idempotency_key
        raise NotSupported(f"{self.capabilities.label} has no write action {action!r}")


# --- mapping helper ------------------------------------------------------------------------------

ACCOUNT_COLUMN: Final[str] = "_account"


def map_with(
    mapping: EventMapping,
    records: Sequence[Mapping[str, Any]],
    *,
    source: str,
    account: str,
    flatten: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> MappedRecords:
    """Records through an `autonomy.data.mapping.EventMapping`, with event ids stable per account.

    `mapping.row_key` must name the columns that identify a record version's event (e.g. the order
    id for a purchase): the frame's index would change between pages and break replay dedupe. The
    account (a shop domain, a Klaviyo account id) is prepended to the key, so two shops' order
    #1001 never share an event id. `flatten` turns a nested record into flat columns first.
    Records that cannot be mapped come back as problems, never as an exception."""
    if not mapping.row_key:
        raise ValueError("a connector's mapping needs row_key columns for stable event ids")
    if not records:
        return MappedRecords()
    import pandas as pd

    rows = [dict(flatten(r) if flatten is not None else r) | {ACCOUNT_COLUMN: account} for r in records]
    frame = pd.DataFrame(rows)
    for column in mapping.columns():
        if column not in frame.columns:
            frame[column] = None  # a missing optional field is a per-row problem, not a crash
    keyed = mapping.model_copy(update={"row_key": (ACCOUNT_COLUMN, *mapping.row_key)})
    events, problems = map_frame(frame, keyed, source)
    return MappedRecords(events=tuple(events), problems=tuple(problems))


# --- errors --------------------------------------------------------------------------------------

ErrorAction = Literal["retry", "reconnect", "restart", "fail"]


class ConnectorFailure(Exception):  # noqa: N818 - the subclasses read as what happened
    """Something a connector call could not do. `message` is plain words with no secret in it."""

    action: ErrorAction = "fail"
    code: str = "connector.failed"

    def __init__(self, message: str, *, retry_at: datetime | None = None, status: int | None = None) -> None:
        self.message = redact(message)
        super().__init__(self.message)
        self.retry_at = retry_at
        self.status = status


class RateLimited(ConnectorFailure):
    """The platform asked us to slow down for longer than is worth waiting in-line."""

    action = "retry"
    code = "connector.rate_limited"


class TransientFailure(ConnectorFailure):
    """A timeout, a dropped connection or a 5xx that did not clear within the retry budget."""

    action = "retry"
    code = "connector.transient"


class SourceUnavailable(ConnectorFailure):
    """The account exists but cannot be read now (Shopify 402 frozen shop, 423 locked)."""

    action = "retry"
    code = "connector.source_unavailable"


class NeedsReconnect(ConnectorFailure):
    """Access is gone (revoked, uninstalled, refresh token expired): a person must connect again."""

    action = "reconnect"
    code = "connector.needs_reconnect"


class AuthRejected(NeedsReconnect):
    """The API refused our credentials even after a fresh token."""

    code = "connector.auth_rejected"


class AccessDenied(NeedsReconnect):
    """The credentials work but lack a permission (a scope was removed): reconnect with the scopes."""

    code = "connector.access_denied"


class CursorExpired(ConnectorFailure):
    """A saved page cursor is no longer valid: restart the window (stable ids make it safe)."""

    action = "restart"
    code = "connector.cursor_expired"


class SchemaDrift(ConnectorFailure):
    """Records no longer carry fields the connector relies on (renamed without a version bump)."""

    code = "connector.schema_drift"


class RemoteError(ConnectorFailure):
    """The platform refused this one request (a 4xx that retrying will not change)."""

    code = "connector.remote_error"


class NotSupported(ConnectorFailure):
    code = "connector.not_supported"


# --- secret-free text ----------------------------------------------------------------------------

_SECRETISH: Final[re.Pattern[str]] = re.compile(
    r"(password|pwd|secret|token|key|code|code_verifier|refresh_token|access_token|signature|hmac)"
    r"(\"?\s*[=:]\s*\"?)[^\s&\",}]+",
    re.IGNORECASE,
)
_BEARER: Final[re.Pattern[str]] = re.compile(r"(Bearer|Basic|Klaviyo-API-Key)\s+[A-Za-z0-9._~+/=-]+", re.I)
_QUERY: Final[re.Pattern[str]] = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
_LONG_TOKEN: Final[re.Pattern[str]] = re.compile(r"\b(shp[a-z]{2}_[A-Za-z0-9]{8,}|pk_[A-Za-z0-9]{16,})\b")


def redact(text: str, limit: int = 300) -> str:
    """One short line with anything shaped like a secret removed.

    Same promise as v1's `engine.connections.base.plain_error` (which the light v2 core cannot
    import at module scope), plus: bearer/basic credentials, URL query strings (where OAuth codes
    and signed-URL signatures travel) and the platforms' recognisable token prefixes."""
    line = " ".join(str(text).split())
    line = _BEARER.sub(r"\1 ***", line)
    line = _QUERY.sub(r"\1?***", line)
    line = _SECRETISH.sub(r"\1\2***", line)
    line = _LONG_TOKEN.sub("***", line)
    return line[:limit] + ("…" if len(line) > limit else "")
