"""The `/v2/connectors` endpoints: connect Shopify or Klaviyo once, then see in plain words that they keep running.

Everything here is a thin layer over the connector base (`autonomy/connectors/`, docs/v2/CONNECTORS.md); the
routes parse, call the engine, and put its answers into words a business owner reads. The router is generic:
every connector in the registry gets the same routes, and nothing here names a platform except two small
lookups (a lazily imported shop-address normaliser, and the copy for each stream's name).

| Route | What it does |
|---|---|
| `GET /v2/connectors` | Every platform this server can connect, with its connections and their health in words |
| `POST /v2/connectors/{kind}/connect` | Start OAuth: returns the platform's consent URL (Shopify needs the shop address) |
| `GET /v2/connectors/{kind}/callback` | The platform's redirect: finish OAuth, seed the sync jobs, send the browser back to Connect |
| `GET /v2/connectors/{kind}/install` | An install started on the platform (Shopify admin): check its signature, then start OAuth |
| `POST /v2/connectors/{kind}/key` | Connect with a pasted private key or custom-app token (pilots) |
| `GET /v2/connectors/connections[/{id}]` | Health per connection: healthy, syncing, loading history n%, behind, needs reconnect |
| `DELETE /v2/connectors/connections/{id}` | Disconnect: forget the tokens, stop every job |
| `POST /v2/connectors/connections/{id}/sync` | Queue a sync of every stream now (at most one per minute) |
| `GET /v2/connectors/connections/{id}/setup` | The one-time guided steps in the customer's tool, when its API could not do them |
| `POST /v2/connectors/webhooks/{kind}[/{id}]` | Webhooks: verified, deduped and queued by `WebhookIntake`; answered at once |

Words rule (docs/v2/DESIGN.md section 6): a connection's `words` is the one line a card shows ("Up to date, 4
minutes ago"), its `detail` is the description behind it, and a problem is never shown as a raw error code.

Wiring: `attach_connector_services(app, ConnectorServices(...))` with the engine built as in
docs/v2/CONNECTORS.md "Wiring"; `api.v2.app.create_app` does it with `local_connector_services` (an in-memory
store and token vault) when it is given nothing, so the demo shows the cards. Times come from
`ConnectorServices.clock`; nothing here reads the clock itself.
"""

import importlib
import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Protocol
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool

from autonomy.connectors import (
    AccessDenied,
    AppCredentials,
    AuthMethod,
    AuthorizationDenied,
    AuthorizationError,
    Connection,
    ConnectionContext,
    ConnectionHealth,
    ConnectionStatus,
    ConnectorCapabilities,
    ConnectorEngine,
    ConnectorFailure,
    Freshness,
    Lane,
    NeedsReconnect,
    NotSupported,
    Problem,
    RateLimited,
    RemoteError,
    SaasConnector,
    SourceUnavailable,
    StreamHealth,
    TransientFailure,
    UnknownConnector,
    WebhookIntake,
    after_connect,
    enqueue_once,
    sync_job,
)
from autonomy.connectors.health import ALERT_DRIFT, ALERT_ERRORS, ALERT_ROW_DROP, ALERT_STALE
from autonomy.contracts import Channel
from autonomy.interfaces import JobQueue

__all__ = [
    "CONNECTORS_PREFIX",
    "ConnectionView",
    "ConnectorServices",
    "ConnectorsOverview",
    "PlatformView",
    "SetupView",
    "StreamView",
    "attach_connector_services",
    "connection_view",
    "local_connector_services",
    "router",
]

CONNECTORS_PREFIX: Final[str] = "/v2/connectors"
MAX_WEBHOOK_BYTES: Final[int] = 2_000_000
"""Platform webhooks are a few KB; anything this big is not one (answered 413 before it is read)."""

STREAM_WORDS: Final[Mapping[str, str]] = {
    "customers": "Customers",
    "orders": "Orders",
    "products": "Products",
    "events": "Site activity",
    "consent": "Marketing consent",
    "engagement": "Message engagement",
}
"""What each stream is called on the screen."""

_NORMALISERS: Final[Mapping[tuple[str, str], str]] = {
    ("shopify", "shop"): "autonomy.connectors.shopify.api:normalize_shop",
}
"""A config field a person types loosely ("acme", "https://acme.myshopify.com/admin") and the connector's own
function that reads it; any other field gets the generic clean-up (`_clean_value`). Imported on first use."""

_CONFIG_KEY: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,39}")
_IDENTITY_KEYS: Final[frozenset[str]] = frozenset({"shop", "account_id"})
"""Settings the base reads as the account's identity (`ConnectorEngine._connected` derives the connection id from
them): only a connector's own declared field may carry one, never a free setting from the browser."""

Phase = Literal[
    "healthy", "warning", "syncing", "backfilling", "stale", "needs_reconnect", "paused", "disconnected"
]


# --- the services this router needs ---------------------------------------------------------------------


@dataclass
class ConnectorServices:
    """The connector engine and what the routes need around it, attached to the app once."""

    engine: ConnectorEngine
    queue: JobQueue
    business_id: str
    clock: Callable[[], datetime]
    public_url: str | None = None
    """`https://host` that platforms redirect back to (OAuth). None: the request's own base URL."""
    app_url: str = "/v2/app/"
    """Where the browser goes after the OAuth callback (the marketer app; `#/connect` is appended)."""

    def now(self) -> datetime:
        return self.clock()


def attach_connector_services(app: Starlette, services: ConnectorServices) -> None:
    """Make `services` what every `/v2/connectors` route uses (FastAPI's `app.state`)."""
    app.state.connector_services = services


def get_connector_services(request: Request) -> ConnectorServices:
    services = getattr(request.app.state, "connector_services", None)
    if not isinstance(services, ConnectorServices):
        raise HTTPException(status_code=503, detail="Connecting platforms is not set up on this server.")
    return services


ServicesDep = Annotated[ConnectorServices, Depends(get_connector_services)]


def local_connector_services(
    *,
    business_id: str,
    sink: Callable[[Sequence[Any]], int],
    environ: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
) -> ConnectorServices:
    """Connector services for the local demo app: SQLite in memory, tokens kept in memory only.

    Our app's OAuth credentials come from `environ` (`AUTONOMY_<KIND>_CLIENT_ID` / `_CLIENT_SECRET`) and
    `AUTONOMY_PUBLIC_URL` is the address platforms call back. Production wiring builds the engine with the
    encrypted `SqlTokenVault`, Postgres, the alert store and a worker (docs/v2/CONNECTORS.md "Wiring"); here
    nothing runs the queued sync jobs, so a demo connection shows "First sync running" until a worker does."""
    from autonomy.connectors import (
        ConnectorStore,
        HealthMonitor,
        IncidentDesk,
        MemoryTokenVault,
        SystemClock,
        create_connector_tables,
        default_registry,
    )
    from autonomy.store import SqlJobQueue, create_all, memory_engine

    db = memory_engine()
    create_all(db)
    create_connector_tables(db)
    store = ConnectorStore(db)
    registry = default_registry()
    apps = {k: a for k in registry.kinds() if (a := AppCredentials.from_env(k, environ)) is not None}
    public = (environ.get("AUTONOMY_PUBLIC_URL") or "").strip().rstrip("/") or None
    system = SystemClock()
    engine = ConnectorEngine(
        registry=registry,
        store=store,
        vault=MemoryTokenVault(),
        sink=sink,
        clock=system,
        apps=apps,
        monitor=HealthMonitor(store, registry, desk=IncidentDesk(store)),
        webhook_base_url=f"{public}{CONNECTORS_PREFIX}/webhooks" if public else None,
        holder="api-local",
    )
    return ConnectorServices(
        engine=engine,
        queue=SqlJobQueue(db),
        business_id=business_id,
        clock=clock or system.now,
        public_url=public,
    )


# --- what the screen gets -------------------------------------------------------------------------------


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldView(_View):
    """One value a person types to connect (Shopify: the shop address)."""

    name: str
    label: str
    help: str | None = None
    pattern: str | None = None
    required: bool = True


class ProblemView(_View):
    """Something about a connection a person may want to know, in words (never an error code)."""

    code: str = Field(
        description="What kind of problem (connector.stale, connector.errors, ...), for the UI's logic"
    )
    severity: Literal["info", "warning", "critical"]
    stream: str | None = None
    words: str


class StreamView(_View):
    stream: str
    label: str = Field(description="The stream's name on screen, e.g. 'Orders'")
    critical: bool = Field(description="Stale data here holds decisions (orders, consent)")
    freshness: Freshness
    last_sync_at: datetime | None = Field(description="When the live lane last finished a window")
    lag_seconds: float | None
    words: str = Field(description="e.g. 'Up to date, 4 minutes ago' or 'Not synced yet'")
    history_ready: bool = Field(description="Enough history for decisions to rely on this stream")
    history_progress: float | None = Field(
        description="0..1 of the backfill depth read; None without a history lane"
    )
    history_words: str | None = Field(description="e.g. 'History 42% loaded'; None without a history lane")
    rows_last_day: int | None = Field(
        description="Records read in the last 24 hours (None before a week of runs)"
    )
    last_rows: int = Field(description="Records read by the last finished sync")


class ConnectionView(_View):
    """One connected account and its health, in the words the Connect screen shows."""

    connection_id: str
    kind: str
    label: str = Field(description="The account's name, e.g. the shop's name or the Klaviyo organisation")
    external_id: str | None = None
    status: ConnectionStatus
    phase: Phase = Field(
        description="healthy | warning (working, something to look at) | syncing (first sync) | backfilling "
        "(loading history) | stale (behind) | needs_reconnect | paused | disconnected"
    )
    words: str = Field(description="The card's one line, e.g. 'Up to date, 4 minutes ago'")
    detail: str = Field(description="What that line means and what changes it (the description behind it)")
    can_act: bool = Field(description="Decisions may rely on this connection now")
    last_sync_at: datetime | None = Field(
        description="The oldest last sync across the streams decisions rely on"
    )
    history_progress: float | None = Field(
        description="While loading history: 0..1 of the critical streams' least"
    )
    rows_last_day: int | None = None
    connected_at: datetime
    setup: str = Field(
        description="automatic | guided: whether the platform may need a one-time step by hand"
    )
    streams: tuple[StreamView, ...] = ()
    problems: tuple[ProblemView, ...] = ()


class PlatformView(_View):
    """One platform card: what it is, how it connects, and its connections."""

    kind: str
    label: str
    tagline: str = Field(
        description="One short line for the card, e.g. 'Brings in orders, customers and products.'"
    )
    description: str = Field(description="The connector's full description (the card's description)")
    auth: AuthMethod
    oauth_ready: bool = Field(
        description="This server holds our app's credentials, so 'Connect' can use OAuth"
    )
    missing_settings: tuple[str, ...] = Field(
        default=(), description="Server settings OAuth still needs (for the operator)"
    )
    key_allowed: bool = Field(description="A private key or custom-app token can be pasted instead (pilots)")
    install_url: str | None = Field(
        description="The app URL to give the platform (installs started there); None if not used"
    )
    scopes: tuple[str, ...] = Field(default=(), description="The access a key needs (the OAuth scopes)")
    setup: str
    setup_note: str | None = None
    config_fields: tuple[FieldView, ...] = ()
    streams: tuple[str, ...] = Field(default=(), description="The streams' names on screen")
    sends: bool = Field(description="Sends Autonomy's messages through the customer's own tool")
    regions: tuple[str, ...] = ()
    api_version: str
    connections: tuple[ConnectionView, ...] = ()


class ConnectorsOverview(_View):
    business_id: str
    as_of: datetime
    can_act: bool = Field(description="The runtime's answer: may Autonomy decide for this business now")
    on_hold: tuple[str, ...] = Field(default=(), description="Why not, in plain words")
    platforms: tuple[PlatformView, ...] = ()


class ConnectStart(_View):
    config: dict[str, str] = Field(
        default_factory=dict, description="The platform's connect fields, e.g. {'shop': 'acme.myshopify.com'}"
    )


class ConnectRedirect(_View):
    kind: str
    url: str = Field(description="Send the browser here: the platform's consent screen")


KEY_MIN_CHARS: Final[int] = 8
KEY_MAX_CHARS: Final[int] = 4096


class KeyConnect(_View):
    api_key: str = Field(
        repr=False,
        description=f"A private key or custom-app token ({KEY_MIN_CHARS} to {KEY_MAX_CHARS} characters)",
    )
    """No length constraint on the model: a 422 echoes the refused input, so the length is checked in the route
    and refused in words without the key."""
    config: dict[str, str] = Field(default_factory=dict)


class SyncRequested(_View):
    connection_id: str
    streams: tuple[str, ...]
    run_at: datetime
    words: str


class SetupChannelView(_View):
    channel: str
    ready: bool
    flow_name: str | None = None
    words: str
    steps: tuple[str, ...] = ()


class SetupView(_View):
    connection_id: str
    kind: str
    state: Literal["ready", "pending", "guided", "not_needed", "unavailable"]
    words: str = Field(description="The one line, e.g. 'One step to finish in Klaviyo.'")
    steps: tuple[str, ...] = Field(default=(), description="The one-time steps to do by hand, in order")
    channels: tuple[SetupChannelView, ...] = ()
    checked_at: datetime


# --- words ----------------------------------------------------------------------------------------------


def ago(seconds: float) -> str:
    """60 -> "1 minute ago"; under a minute is "just now"."""
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    return f"{hours // 24} days ago"


def _since(at: datetime | None, now: datetime) -> str | None:
    return None if at is None else ago((now - at).total_seconds())


def _join(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def stream_label(stream: str) -> str:
    return STREAM_WORDS.get(stream, stream.replace("_", " ").capitalize())


def tagline(caps: ConnectorCapabilities) -> str:
    """The card's one line: what it sends (a tool that sends for us), or what it brings in."""
    if caps.sends:
        return "Sends Autonomy's messages through your own flows."
    names = [stream_label(s.stream.value).lower() for s in caps.streams]
    return f"Brings in {_join(names)}." if names else caps.label


def _stream_words(health: StreamHealth, now: datetime) -> str:
    since = _since(health.last_success_at, now)
    if since is None:
        return "Not synced yet"
    if health.freshness is Freshness.FRESH:
        return f"Up to date, {since}"
    if health.freshness is Freshness.LATE:
        return f"Last synced {since}"
    return f"Behind, last synced {since}"


def problem_words(
    problem: Problem, label: str, streams: Mapping[str, StreamHealth], now: datetime
) -> ProblemView:
    """The base's problem as a sentence for a business owner: no codes, no stack, what Autonomy does about it."""
    stream_key = (problem.stream or "").split(".", 1)[0]
    history = (problem.stream or "").endswith(".history")
    name = stream_label(stream_key) if stream_key else label
    health = streams.get(stream_key)
    if problem.code == ALERT_STALE:
        since = _since(health.last_success_at if health else None, now)
        words = f"{name} last synced {since}." if since else f"{name} have not synced yet."
        if health is not None and health.critical:
            words += " Decisions wait until it catches up."
    elif problem.code == ALERT_ERRORS and history:
        words = f"Loading past {name.lower()} keeps failing. Autonomy keeps retrying on its own."
    elif problem.code == ALERT_ERRORS:
        words = f"The last few {name.lower()} syncs failed. Autonomy keeps retrying on its own."
    elif problem.code == ALERT_ROW_DROP:
        words = f"Far fewer {name.lower()} than usual came in over the last day."
    elif problem.code == ALERT_DRIFT:
        words = (
            f"Some {name.lower()} records lack details Autonomy relies on; {label} may have changed its data."
        )
    else:
        words = f"Something about {label} needs a look."
    return ProblemView(code=problem.code, severity=problem.severity, stream=problem.stream, words=words)


def _phase(
    connection: Connection,
    health: ConnectionHealth,
    label: str,
    now: datetime,
    specs: Mapping[str, int],
    *,
    pushes: bool = False,
) -> tuple[Phase, str, str]:
    """(phase, the one line, its description) for a connection. `pushes`: the platform sends webhooks."""
    healthy_detail = (
        f"Autonomy checks {label} every hour, and right away when {label} reports a change."
        if pushes
        else f"Autonomy checks {label} every hour."
    )
    if connection.status is ConnectionStatus.NEEDS_RECONNECT:
        return (
            "needs_reconnect",
            "Needs reconnecting",
            f"Autonomy's access to {label} stopped working (it was revoked, or a password or app changed). "
            "Nothing syncs and decisions that rely on it wait until you reconnect. Reconnecting keeps "
            "everything already loaded.",
        )
    if connection.status is ConnectionStatus.DISCONNECTED:
        removed = connection.status_reason == "uninstalled"
        return (
            "disconnected",
            f"Removed in {label}" if removed else "Disconnected",
            (
                f"The Autonomy app was removed in {label}. Connect it again to resume."
                if removed
                else "Autonomy no longer reads this account. Connect it again to resume."
            ),
        )
    if connection.status is ConnectionStatus.PAUSED:
        return "paused", "Paused", "Syncing is paused. Access is kept, so resuming needs no reconnect."
    critical = [s for s in health.streams if s.critical] or list(health.streams)
    if health.state == "stale":
        if not any(p.code == ALERT_STALE for p in health.problems):
            return (
                "syncing",
                "First sync running",
                f"Autonomy is reading {label} for the first time. Decisions start once it has.",
            )
        # The line is about the streams that are behind, not the freshest one: "Orders up to date" must not read
        # as "Last synced just now" next to a "Behind" pill while Customers never synced.
        behind = [s for s in critical if s.freshness in (Freshness.STALE, Freshness.NEVER)] or critical
        never = [s for s in behind if s.last_success_at is None]
        oldest = min((s.last_success_at for s in behind if s.last_success_at), default=None)
        since = _since(oldest, now)
        if never and len(never) < len(critical):
            line = f"{stream_label(never[0].stream)} not synced yet"
        else:
            line = f"Last synced {since}" if since and not never else "Not synced yet"
        return (
            "stale",
            line,
            f"{label} has not synced for longer than usual, so decisions that rely on it wait. Autonomy keeps "
            "retrying on its own and says here once it has caught up.",
        )
    if health.state == "catching_up":
        waiting = [s for s in critical if not s.history_ready]
        progress = min((s.history_progress for s in waiting), default=1.0)
        need = max((specs.get(s.stream, 0) for s in waiting), default=0)
        return (
            "backfilling",
            f"Loading history, {progress:.0%}",
            f"Autonomy reads your past data newest first. It starts deciding once it has {need} days; the rest "
            "keeps loading in the background.",
        )
    oldest = min((s.last_success_at for s in critical if s.last_success_at), default=None)
    since = _since(oldest, now)
    fresh = all(s.freshness is Freshness.FRESH for s in critical)
    line = (f"Up to date, {since}" if fresh else f"Last synced {since}") if since else "Connected"
    if health.state == "degraded":
        return "warning", line, "Working. Something is worth a look; the details are below."
    return "healthy", line, healthy_detail


def connection_view(services: ConnectorServices, connection: Connection, now: datetime) -> ConnectionView:
    """A connection and its health, in words (see the module docstring's words rule)."""
    engine = services.engine
    connector = engine.registry.find(connection.kind)
    label = connector.capabilities.label if connector is not None else connection.kind
    health = engine.monitor.connection_health(connection, now)
    specs = {s.stream.value: s for s in connector.capabilities.streams} if connector is not None else {}
    by_stream = {s.stream: s for s in health.streams}
    pushes = connector is not None and any(
        t.stream is not None for t in connector.capabilities.webhook_topics
    )
    phase, words, detail = _phase(
        connection, health, label, now, {k: v.min_history_days for k, v in specs.items()}, pushes=pushes
    )
    streams = []
    for s in health.streams:
        spec = specs.get(s.stream)
        has_history = spec is not None and spec.backfill_days > 0
        position = engine.store.position(connection.connection_id, spec.stream, Lane.LIVE) if spec else None
        streams.append(
            StreamView(
                stream=s.stream,
                label=stream_label(s.stream),
                critical=s.critical,
                freshness=s.freshness,
                last_sync_at=s.last_success_at,
                lag_seconds=s.lag_seconds,
                words=_stream_words(s, now),
                history_ready=s.history_ready,
                history_progress=s.history_progress if has_history else None,
                history_words=(
                    (
                        "History loaded"
                        if s.history_progress >= 1
                        else f"History {s.history_progress:.0%} loaded"
                    )
                    if has_history
                    else None
                ),
                rows_last_day=s.rows_last_day,
                last_rows=position.last_rows if position is not None else 0,
            )
        )
    critical = [s for s in health.streams if s.critical] or list(health.streams)
    waiting = [s.history_progress for s in critical if not s.history_ready]
    counted = [s.rows_last_day for s in health.streams if s.rows_last_day is not None]
    return ConnectionView(
        connection_id=connection.connection_id,
        kind=connection.kind,
        label=connection.label,
        external_id=connection.external_id,
        status=connection.status,
        phase=phase,
        words=words,
        detail=detail,
        can_act=health.can_act,
        last_sync_at=min((s.last_success_at for s in critical if s.last_success_at), default=None),
        history_progress=min(waiting) if waiting else None,
        rows_last_day=sum(counted) if counted else None,
        connected_at=connection.created_at,
        setup=connector.capabilities.setup.value if connector is not None else "automatic",
        streams=tuple(streams),
        problems=tuple(problem_words(p, label, by_stream, now) for p in health.problems),
    )


def _install_url(services: ConnectorServices, connector: SaasConnector) -> str | None:
    if not callable(getattr(connector, "validate_install", None)) or not services.public_url:
        return None
    return f"{services.public_url}{CONNECTORS_PREFIX}/{connector.kind}/install"


def platform_view(services: ConnectorServices, connector: SaasConnector, now: datetime) -> PlatformView:
    caps = connector.capabilities
    engine = services.engine
    oauth = caps.auth is AuthMethod.OAUTH2 and connector.oauth is not None
    ready = oauth and caps.kind in engine.apps
    missing = AppCredentials.env_names(caps.kind) if oauth and not ready else ()
    connections = engine.store.connections(business_id=services.business_id, kind=caps.kind)
    # Live connections first, then the newest; a disconnected one stays visible (it says why) until it is
    # connected again, but only the latest of those.
    live = [c for c in connections if c.status is not ConnectionStatus.DISCONNECTED]
    gone = sorted(
        (c for c in connections if c.status is ConnectionStatus.DISCONNECTED), key=lambda c: c.updated_at
    )
    shown = live or gone[-1:]
    return PlatformView(
        kind=caps.kind,
        label=caps.label,
        tagline=tagline(caps),
        description=caps.description,
        auth=caps.auth,
        oauth_ready=ready,
        missing_settings=missing,
        key_allowed=True,
        install_url=_install_url(services, connector),
        scopes=connector.oauth.scopes if connector.oauth is not None else (),
        setup=caps.setup.value,
        setup_note=caps.setup_note,
        config_fields=tuple(
            FieldView(name=f.name, label=f.label, help=f.help, pattern=f.pattern, required=f.required)
            for f in caps.config_fields
        ),
        streams=tuple(stream_label(s.stream.value) for s in caps.streams),
        sends=caps.sends,
        regions=caps.regions,
        api_version=caps.api_version.value,
        connections=tuple(connection_view(services, c, now) for c in shown),
    )


# --- input clean-up -------------------------------------------------------------------------------------


def _clean_value(value: str) -> str:
    """What a person pasted, without the scheme, a path or spaces, in lower case (domains and ids)."""
    text = value.strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.split("/", 1)[0]


def _normaliser(kind: str, name: str) -> Callable[[str], str | None] | None:
    path = _NORMALISERS.get((kind, name))
    if path is None:
        return None
    module_name, _, attr = path.partition(":")
    try:
        found = getattr(importlib.import_module(module_name), attr, None)
    except ModuleNotFoundError:
        return None
    return found if callable(found) else None


def clean_config(connector: SaasConnector, given: Mapping[str, str]) -> dict[str, str]:
    """The connect fields checked against the connector's own patterns, plus any other simple settings.

    Raises a 400 in words ("Shop address is not valid. Your shop's permanent address, ending in
    .myshopify.com") before anything is stored or any platform is called."""
    caps = connector.capabilities
    out: dict[str, str] = {}
    for key, value in given.items():
        if key not in {f.name for f in caps.config_fields}:
            if (
                key in _IDENTITY_KEYS
                or not _CONFIG_KEY.fullmatch(key)
                or not isinstance(value, str)
                or len(value) > 200
            ):
                raise HTTPException(status_code=400, detail=f"The setting {key!r} is not one Autonomy knows.")
            out[key] = value.strip()
    for spec in caps.config_fields:
        raw = str(given.get(spec.name) or "").strip()
        if not raw:
            if spec.required:
                raise HTTPException(
                    status_code=400, detail=f"{spec.label} is needed to connect {caps.label}."
                )
            continue
        normalise = _normaliser(caps.kind, spec.name)
        cleaned = normalise(raw) if normalise is not None else _clean_value(raw)
        if not cleaned or (spec.pattern and not re.fullmatch(spec.pattern, cleaned)):
            hint = f" {spec.help.rstrip('.')}." if spec.help else ""
            raise HTTPException(status_code=400, detail=f"{spec.label} is not valid.{hint}")
        out[spec.name] = cleaned
    return out


# --- failures in words ----------------------------------------------------------------------------------


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else text + "."


def failure_http(exc: ConnectorFailure, label: str, *, connecting_with_key: bool = False) -> HTTPException:
    """A connector failure as an HTTP status and one plain sentence (`ConnectorFailure.message` is secret-free)."""
    if isinstance(exc, UnknownConnector):
        return HTTPException(status_code=404, detail=f"Autonomy cannot connect to {label}.")
    if isinstance(exc, AuthorizationDenied):
        return HTTPException(status_code=400, detail=f"The connection was not approved in {label}.")
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=400, detail=_sentence(exc.message))
    if isinstance(exc, AccessDenied):
        return HTTPException(
            status_code=400,
            detail=f"{label} says this access is missing some permissions Autonomy needs. Give it the access "
            "listed on the card and try again.",
        )
    if isinstance(exc, NeedsReconnect):
        what = "this key" if connecting_with_key else "the connection"
        return HTTPException(
            status_code=400, detail=f"{label} did not accept {what}. Check it and try again."
        )
    if isinstance(exc, RateLimited | TransientFailure | SourceUnavailable):
        return HTTPException(
            status_code=503, detail=f"Could not reach {label} just now. Try again in a minute."
        )
    if isinstance(exc, NotSupported):
        return HTTPException(status_code=409, detail=_sentence(exc.message))
    if isinstance(exc, RemoteError):
        return HTTPException(status_code=400, detail=f"{label} refused the request: {_sentence(exc.message)}")
    return HTTPException(status_code=502, detail=f"Connecting {label} failed. Try again in a minute.")


def _connector(services: ConnectorServices, kind: str) -> SaasConnector:
    connector = services.engine.registry.find(kind)
    if connector is None:
        raise HTTPException(status_code=404, detail=f"Autonomy cannot connect to {kind!r}.")
    return connector


def _owned(services: ConnectorServices, connection_id: str) -> Connection:
    connection = services.engine.store.get_connection(connection_id)
    if connection is None or connection.business_id != services.business_id:
        raise HTTPException(status_code=404, detail="There is no such connection.")
    return connection


def _base_url(services: ConnectorServices, request: Request) -> str:
    return services.public_url or str(request.base_url).rstrip("/")


def _redirect_uri(services: ConnectorServices, request: Request, kind: str) -> str:
    return f"{_base_url(services, request)}{CONNECTORS_PREFIX}/{kind}/callback"


def _back_to_app(services: ConnectorServices, **params: str) -> str:
    """The Connect screen with an outcome the screen turns into a toast (`#/connect?connected=shopify`)."""
    return f"{services.app_url}#/connect?{urlencode(params, quote_via=quote)}"


# --- the routes -----------------------------------------------------------------------------------------

router = APIRouter(prefix=CONNECTORS_PREFIX, tags=["connectors"])


@router.get(
    "", response_model=ConnectorsOverview, summary="Every platform this server connects, with health in words"
)
def overview(services: ServicesDep) -> ConnectorsOverview:
    now = services.now()
    engine = services.engine
    platforms = tuple(platform_view(services, engine.registry.get(k), now) for k in engine.registry.kinds())
    verdict = engine.monitor.ready_to_act(services.business_id, now)
    # The runtime's own answer (`ready_to_act`), with its reasons put as each card says them.
    every = [
        connection_view(services, c, now) for c in engine.store.connections(business_id=services.business_id)
    ]
    held = [v for v in every if not v.can_act]
    labels = {p.kind: p.label for p in platforms}
    return ConnectorsOverview(
        business_id=services.business_id,
        as_of=now,
        can_act=verdict.can_act,
        on_hold=tuple(
            f"{labels.get(v.kind, v.kind)} ({v.label}): {v.words[0].lower()}{v.words[1:]}." for v in held
        ),
        platforms=platforms,
    )


@router.get("/connections", response_model=list[ConnectionView], summary="Health of every connection")
def list_connections(services: ServicesDep) -> list[ConnectionView]:
    now = services.now()
    return [
        connection_view(services, c, now)
        for c in services.engine.store.connections(business_id=services.business_id)
    ]


@router.get("/connections/{connection_id}", response_model=ConnectionView, summary="Health of one connection")
def get_connection(connection_id: str, services: ServicesDep) -> ConnectionView:
    return connection_view(services, _owned(services, connection_id), services.now())


@router.delete(
    "/connections/{connection_id}",
    response_model=ConnectionView,
    responses={404: {"description": "No such connection"}},
    summary="Disconnect: forget the tokens and stop syncing",
)
def disconnect(connection_id: str, services: ServicesDep) -> ConnectionView:
    connection = _owned(services, connection_id)
    now = services.now()
    services.engine.disconnect(connection.connection_id, now)
    return connection_view(services, _owned(services, connection_id), now)


@router.post(
    "/connections/{connection_id}/sync",
    response_model=SyncRequested,
    responses={
        404: {"description": "No such connection"},
        409: {"description": "Not active: reconnect first"},
    },
    summary="Queue a sync of every stream now",
)
def sync_now(connection_id: str, services: ServicesDep) -> SyncRequested:
    connection = _owned(services, connection_id)
    connector = _connector(services, connection.kind)
    if connection.status is not ConnectionStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="Reconnect it first; nothing can sync until then.")
    now = services.now()
    # On a one-minute grid (the slot that has already started): repeated presses queue one sync per stream.
    run_at = now.replace(second=0, microsecond=0)
    names = []
    for spec in connector.capabilities.streams:
        enqueue_once(
            services.queue,
            sync_job(connection.connection_id, spec.stream, Lane.LIVE, run_at, tag="requested"),
        )
        names.append(stream_label(spec.stream.value).lower())
    return SyncRequested(
        connection_id=connection.connection_id,
        streams=tuple(s.stream.value for s in connector.capabilities.streams),
        run_at=run_at,
        words=f"A sync of {_join(names)} starts now.",
    )


class _SetupStatusLike(Protocol):
    """What a connector's `send_setup` returns (Klaviyo: `autonomy.connectors.klaviyo.setup.SetupStatus`)."""

    state: str
    channels: Sequence[Any]
    checked_at: datetime

    def steps(self) -> tuple[str, ...]: ...


def _setup_channels(connector: SaasConnector, config: Mapping[str, str]) -> tuple[Channel, ...]:
    """The channels a sending connector sets up for this connection: its module's `channels_for(config)`."""
    module = inspect.getmodule(type(connector))
    pick = getattr(module, "channels_for", None) if module is not None else None
    if callable(pick):
        chosen = pick(config)
        if isinstance(chosen, tuple) and all(isinstance(c, Channel) for c in chosen):
            return chosen
    return (Channel.EMAIL,)


SetupState = Literal["ready", "pending", "guided", "not_needed", "unavailable"]
_SETUP_WORDS: Final[Mapping[SetupState, str]] = {
    "ready": "Sending is set up in {label}.",
    "pending": "Finishing setup in {label}; this takes a few minutes.",
    "guided": "One step to finish in {label}.",
    "not_needed": "Nothing to set up in {label}.",
    "unavailable": "Could not check {label} just now; Autonomy checks again within the hour.",
}


def _setup_state(value: object) -> SetupState:
    for state in _SETUP_WORDS:
        if value == state:
            return state
    return "unavailable"


def _channel_view(c: object) -> SetupChannelView:
    channel = getattr(c, "channel", None)
    name = str(getattr(channel, "value", channel) or "sending")
    ready = bool(getattr(c, "ready", False))
    flow = getattr(c, "flow_name", None)
    return SetupChannelView(
        channel=name,
        ready=ready,
        flow_name=str(flow) if flow else None,
        words=f"{name.capitalize()}: {'ready' if ready else 'not ready yet'}",
        steps=tuple(str(step) for step in (getattr(c, "steps", ()) or ())),
    )


def _setup_view(
    connection: Connection,
    label: str,
    state: SetupState,
    now: datetime,
    status: _SetupStatusLike | None = None,
) -> SetupView:
    return SetupView(
        connection_id=connection.connection_id,
        kind=connection.kind,
        state=state,
        words=_SETUP_WORDS[state].format(label=label),
        steps=tuple(status.steps()) if status is not None and state == "guided" else (),
        channels=tuple(_channel_view(c) for c in status.channels) if status is not None else (),
        checked_at=status.checked_at if status is not None else now,
    )


@router.get(
    "/connections/{connection_id}/setup",
    response_model=SetupView,
    responses={404: {"description": "No such connection"}},
    summary="The one-time guided steps in the customer's tool, when its API could not do them",
)
def setup(
    connection_id: str,
    services: ServicesDep,
    refresh: Annotated[bool, Query(description="Check the platform again instead of the last check")] = False,
) -> SetupView:
    connection = _owned(services, connection_id)
    connector = _connector(services, connection.kind)
    label = connector.capabilities.label
    now = services.now()
    send_setup = getattr(connector, "send_setup", None)
    if not callable(send_setup) or connection.config.get("send") == "off":
        return _setup_view(connection, label, "not_needed", now)
    if connection.status is not ConnectionStatus.ACTIVE:
        return _setup_view(connection, label, "unavailable", now)
    ctx: ConnectionContext = services.engine.context(connection, now)
    try:
        status = send_setup(ctx, _setup_channels(connector, connection.config), refresh=refresh)
    except NeedsReconnect as failure:
        hook = services.engine.tokens.on_revoked
        if hook is not None:
            hook(connection, failure, now)  # marks needs_reconnect and raises its one alert
        return _setup_view(connection, label, "unavailable", now)
    except ConnectorFailure:
        return _setup_view(connection, label, "unavailable", now)
    finally:
        ctx.http.close()
    return _setup_view(connection, label, _setup_state(getattr(status, "state", None)), now, status)


# Webhooks come before the `/{kind}/...` routes, so a kind can never shadow them.
async def _receive(
    services: ConnectorServices, kind: str, request: Request, connection_id: str | None
) -> JSONResponse:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_WEBHOOK_BYTES:
        return JSONResponse(status_code=413, content={"status": "too_large"})
    # Read up to the cap and stop: a chunked body has no content-length, and must not be held whole in memory.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            return JSONResponse(status_code=413, content={"status": "too_large"})
        chunks.append(chunk)
    body = b"".join(chunks)
    intake = WebhookIntake(services.engine, services.queue)
    result = await run_in_threadpool(
        intake.receive, kind, dict(request.headers), body, services.now(), connection_id=connection_id
    )
    return JSONResponse(status_code=result.http_status, content={"status": result.status.value})


@router.post(
    "/webhooks/{kind}",
    summary="App-level webhooks (Shopify's mandatory privacy topics): verified, deduped, queued, answered at once",
)
async def webhook_app(kind: str, request: Request, services: ServicesDep) -> JSONResponse:
    return await _receive(services, kind, request, None)


@router.post(
    "/webhooks/{kind}/{connection_id}",
    summary="Per-connection webhooks (the callback URL register_webhooks subscribes)",
)
async def webhook_connection(
    kind: str, connection_id: str, request: Request, services: ServicesDep
) -> JSONResponse:
    return await _receive(services, kind, request, connection_id)


@router.post(
    "/{kind}/connect",
    response_model=ConnectRedirect,
    responses={
        400: {"description": "A connect field is not valid"},
        404: {"description": "No such platform"},
        409: {"description": "This server has no app credentials for the platform: use a key"},
    },
    summary="Start OAuth: the platform's consent URL",
)
def start_connect(
    kind: str, request: Request, services: ServicesDep, body: Annotated[ConnectStart | None, Body()] = None
) -> ConnectRedirect:
    connector = _connector(services, kind)
    label = connector.capabilities.label
    if connector.oauth is None or kind not in services.engine.apps:
        raise HTTPException(
            status_code=409,
            detail=f"This server is not set up to connect {label} by sign-in yet. Paste an access key instead.",
        )
    config = clean_config(connector, (body.config if body is not None else {}))
    try:
        url = services.engine.begin_oauth(
            kind,
            business_id=services.business_id,
            redirect_uri=_redirect_uri(services, request, kind),
            now=services.now(),
            params=config,
        )
    except ConnectorFailure as exc:
        raise failure_http(exc, label) from exc
    return ConnectRedirect(kind=kind, url=url)


@router.get(
    "/{kind}/callback",
    response_class=RedirectResponse,
    status_code=303,
    summary="The platform's OAuth redirect: finish connecting, then back to the Connect screen",
)
def oauth_callback(kind: str, request: Request, services: ServicesDep) -> RedirectResponse:
    connector = services.engine.registry.find(kind)
    label = connector.capabilities.label if connector is not None else kind
    now = services.now()
    try:
        connection = services.engine.finish_oauth(kind, dict(request.query_params), now, services.queue)
        after_connect(connection, services.queue, now)
    except ConnectorFailure as exc:
        detail = str(failure_http(exc, label).detail)
        return RedirectResponse(_back_to_app(services, connect_error=detail, kind=kind), status_code=303)
    return RedirectResponse(
        _back_to_app(services, connected=kind, connection=connection.connection_id), status_code=303
    )


@router.get(
    "/{kind}/install",
    response_class=RedirectResponse,
    status_code=303,
    responses={404: {"description": "This platform does not start installs on its side"}},
    summary="An install started on the platform (the Shopify admin's app URL): check it, then start OAuth",
)
def platform_install(kind: str, request: Request, services: ServicesDep) -> RedirectResponse:
    connector = _connector(services, kind)
    label = connector.capabilities.label
    validate = getattr(connector, "validate_install", None)
    if not callable(validate):
        raise HTTPException(status_code=404, detail=f"{label} installs start on the Connect screen.")
    app = services.engine.apps.get(kind)
    if app is None:
        return RedirectResponse(
            _back_to_app(
                services, connect_error=f"This server is not set up to connect {label} yet.", kind=kind
            ),
            status_code=303,
        )
    now = services.now()
    query = dict(request.query_params)
    try:
        shop = str(validate(query, app, now))
        known = services.engine.store.find_connection(kind, shop)
        if (
            known is not None
            and known.business_id == services.business_id
            and known.status is ConnectionStatus.ACTIVE
        ):
            # Opening the app from the platform's admin once it is installed: nothing to authorise again.
            return RedirectResponse(_back_to_app(services, kind=kind), status_code=303)
        url = services.engine.begin_oauth(
            kind,
            business_id=services.business_id,
            redirect_uri=_redirect_uri(services, request, kind),
            now=now,
            params={"shop": shop},
        )
    except ConnectorFailure as exc:
        detail = str(failure_http(exc, label).detail)
        return RedirectResponse(_back_to_app(services, connect_error=detail, kind=kind), status_code=303)
    return RedirectResponse(url, status_code=303)


@router.post(
    "/{kind}/key",
    response_model=ConnectionView,
    responses={
        400: {"description": "The key or a field was refused"},
        404: {"description": "No such platform"},
        503: {"description": "The platform could not be reached"},
    },
    summary="Connect with a pasted private key or custom-app token (pilots)",
)
def connect_with_key(kind: str, body: KeyConnect, services: ServicesDep) -> ConnectionView:
    connector = _connector(services, kind)
    label = connector.capabilities.label
    key = body.api_key.strip()
    if not KEY_MIN_CHARS <= len(key) <= KEY_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"That does not look like a {label} key. Paste the whole key and try again.",
        )
    config = clean_config(connector, body.config)
    now = services.now()
    try:
        connection = services.engine.connect_api_key(
            kind,
            business_id=services.business_id,
            api_key=key,
            now=now,
            config=config,
            queue=services.queue,
        )
        after_connect(connection, services.queue, now)
    except ConnectorFailure as exc:
        raise failure_http(exc, label, connecting_with_key=True) from exc
    return connection_view(services, connection, now)
