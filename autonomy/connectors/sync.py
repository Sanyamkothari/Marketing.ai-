"""The sync engine: connect once, then keep every stream fresh with nobody watching.

`ConnectorEngine` ties the pieces together:

* **Connect.** `begin_oauth` / `finish_oauth` run the authorization-code flow (`oauth`), save the
  tokens in the vault, ask the platform who the account is, store the `Connection` and seed its
  jobs. `connect_api_key` does the same for a pasted private key. Reconnecting the same account for
  the same business reuses the connection id, so sync positions resume and the "reconnect"
  incident closes.
* **Sync.** Each stream has two lanes (`base.Lane`) with their own positions. The *live* lane reads
  `[watermark - overlap, now)` on the stream's cadence. The *history* lane walks back from the
  connect time in `backfill_chunk` windows, newest first, until `backfill_days`. A position is
  saved after every stored page (cursor inside the window), so a crash or a 429 resumes exactly
  there; the watermark moves only when a window is finished. A cursor the platform no longer
  accepts restarts its window (stable ids make the repeat free).
* **Ingest.** Records are checked against the newest version seen (`ConnectorStore`) so an older
  copy (an out-of-order webhook, the overlap re-read) is skipped; the rest are mapped by the
  connector into `Event`s with stable ids and handed to the sink (the event store's `append`,
  which drops ids it has), and only then is the version recorded, so a crash in between replays
  rather than loses. Records missing a required field are counted for the drift alarm; unknown
  fields are ignored. The engine does not fold state: the runtime folds new events itself, and a
  history page (events older than the runtime's late window) calls `history_hook` so the runtime
  can rebuild.
* **Schedule.** Jobs on the SQL queue (`autonomy.store.SqlJobQueue`): `connector_sync` per (stream,
  lane), `connector_webhook` per delivery, `connector_daily` per connection (re-register webhooks,
  keep-alive refresh, health, pruning). Live jobs land on cadence-aligned slots, and a webhook asks
  for a catch-up on the next hourly slot: same slot, same job id, so a burst of webhooks queues
  one poll. `handlers(queue)` gives the worker its handlers.
* **Fail well.** A failure's class says what to do (`base.ConnectorFailure.action`): retry at its
  `retry_at`, restart the window, or mark the connection `needs_reconnect` and raise its one
  alert. Error streaks back off exponentially up to the stream's cadence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from autonomy.channels.ratelimit import TokenBucket
from autonomy.connectors.base import (
    AuthMethod,
    Connection,
    ConnectionContext,
    ConnectionStatus,
    ConnectorFailure,
    CursorExpired,
    Identity,
    Lane,
    NeedsReconnect,
    NotSupported,
    RecordKey,
    SaasConnector,
    Stream,
    StreamSpec,
    SyncWindow,
    redact,
)
from autonomy.connectors.health import (
    ALERT_RECONNECT,
    ALERT_UNINSTALLED,
    ALERT_WEBHOOKS,
    HealthMonitor,
    Problem,
)
from autonomy.connectors.http import Clock, ConnectorHttp, HttpPolicy, bucket_for
from autonomy.connectors.oauth import (
    AppCredentials,
    AuthorizationError,
    OAuthClient,
    begin_authorization,
    finish_authorization,
)
from autonomy.connectors.registry import ConnectorRegistry
from autonomy.connectors.store import ConnectorStore, SyncPosition, SyncRun
from autonomy.connectors.tokens import TokenManager, TokenSet, TokenVault
from autonomy.contracts import Alert, Event, Job, JobStatus, stable_id

if TYPE_CHECKING:
    import httpx

    from autonomy.interfaces import JobQueue

__all__ = [
    "DAILY_KIND",
    "JOB_KINDS",
    "SYNC_KIND",
    "WEBHOOK_KIND",
    "ConnectorEngine",
    "SyncReport",
    "daily_job",
    "enqueue_once",
    "slot_after",
    "sync_job",
]

SYNC_KIND: Final[str] = "connector_sync"
WEBHOOK_KIND: Final[str] = "connector_webhook"
DAILY_KIND: Final[str] = "connector_daily"
JOB_KINDS: Final[tuple[str, ...]] = (SYNC_KIND, WEBHOOK_KIND, DAILY_KIND)

CATCH_UP_EVERY: Final[timedelta] = timedelta(hours=1)
SYNC_LEASE_TTL: Final[timedelta] = timedelta(minutes=15)
MAX_PAGES: Final[int] = 20
DELIVERY_MEMORY: Final[timedelta] = timedelta(days=7)
RUN_MEMORY: Final[timedelta] = timedelta(days=35)
TOKEN_READ_TIMEOUT_S: Final[float] = 20.0
LATE_WINDOW: Final[timedelta] = timedelta(days=2)
"""Events older than this when stored are 'history' for the runtime (see `history_hook`)."""

Sink = Callable[[Sequence[Event]], int]
"""Stores events, dropping ids it already has; returns how many were new (`EventStore.append`)."""
HistoryHook = Callable[[Connection, int, datetime], None]
"""(connection, new events older than the late window, now): the runtime should rebuild state."""
TransportFor = Callable[[str], "httpx.BaseTransport | None"]
"""base URL -> the transport to use (tests: an httpx.MockTransport); None: the real network."""


def _epoch() -> datetime:
    return datetime(2000, 1, 1, tzinfo=UTC)


def slot_after(now: datetime, every: timedelta) -> datetime:
    """The first slot strictly after `now` on a grid of `every` (from a fixed epoch, in UTC)."""
    base = _epoch()
    steps = (now - base) // every + 1
    return base + every * steps


def sync_job(connection_id: str, stream: Stream, lane: Lane, run_at: datetime, *, tag: str = "") -> Job:
    """A sync job. Jobs on the cadence grid share an id per slot (so catch-ups collapse); a
    continuation passes a `tag` from the saved position, so it never collides with the job running now."""
    when = run_at.astimezone(UTC).isoformat()
    return Job(
        job_id=stable_id(
            "job", SYNC_KIND, connection_id, stream.value, lane.value, when, *((tag,) if tag else ())
        ),
        kind=SYNC_KIND,
        payload={"connection_id": connection_id, "stream": stream.value, "lane": lane.value},
        run_at=run_at,
    )


def daily_job(connection_id: str, run_at: datetime) -> Job:
    return Job(
        job_id=stable_id("job", DAILY_KIND, connection_id, run_at.astimezone(UTC).isoformat()),
        kind=DAILY_KIND,
        payload={"connection_id": connection_id},
        run_at=run_at,
    )


def enqueue_once(queue: JobQueue, job: Job) -> bool:
    """Enqueue unless a job with this id is already waiting (the SQL queue's `enqueue` replaces).

    Replacing a job another worker is running would reset it to queued; a repeated catch-up
    request for the same slot is therefore a no-op instead."""
    getter = getattr(queue, "get", None)
    if callable(getter):
        existing = getter(job.job_id)
        if isinstance(existing, Job) and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            return False
    queue.enqueue(job)
    return True


@dataclass
class SyncReport:
    """What one sync run did."""

    connection_id: str
    stream: Stream
    lane: Lane
    status: str = "ok"
    """'ok' (window finished), 'more' (pages left), 'waiting' (async export), 'done' (history finished),
    'skipped' (busy, inactive), 'retry' (failed, back later), 'reconnect' (access gone)."""
    rows: int = 0
    new_records: int = 0
    events: int = 0
    problems: int = 0
    missing_required: int = 0
    retry_at: datetime | None = None
    error_code: str | None = None
    error: str | None = None
    alerts: list[Alert] = field(default_factory=list)


@dataclass
class _Ingested:
    rows: int = 0
    new_records: int = 0
    events: int = 0
    problems: int = 0
    missing_required: int = 0
    historical: int = 0


class ConnectorEngine:
    """Everything a worker or the API needs to run SaaS connectors. See the module docstring."""

    def __init__(
        self,
        *,
        registry: ConnectorRegistry,
        store: ConnectorStore,
        vault: TokenVault,
        sink: Sink,
        clock: Clock,
        apps: Mapping[str, AppCredentials] | None = None,
        transport_for: TransportFor | None = None,
        monitor: HealthMonitor | None = None,
        history_hook: HistoryHook | None = None,
        webhook_base_url: str | None = None,
        holder: str = "worker-1",
        host_guard: Callable[[str], None] | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.sink = sink
        self.clock = clock
        self.apps = dict(apps or {})
        self.transport_for = transport_for
        self.monitor = monitor or HealthMonitor(store, registry)
        self.history_hook = history_hook
        self.webhook_base_url = webhook_base_url
        self.holder = holder
        self.host_guard = (
            host_guard if host_guard is not None else (_default_guard if transport_for is None else None)
        )
        self.tokens = TokenManager(
            vault, store, self._oauth_client, holder=holder, on_revoked=self._on_revoked
        )
        self._buckets: dict[str, TokenBucket | None] = {}

    # -- clients ----------------------------------------------------------------------------------

    def connector(self, connection: Connection) -> SaasConnector:
        return self.registry.get(connection.kind)

    def _transport(self, base_url: str) -> httpx.BaseTransport | None:
        return self.transport_for(base_url) if self.transport_for is not None else None

    def _plain_http(self, base_url: str) -> ConnectorHttp:
        """The client for token endpoints. Bounded well inside the refresh lease (`tokens.LEASE_TTL`):
        3 attempts of at most 20 s plus short backoffs, so a slow refresh cannot outlive its lease
        and let a second worker spend the same (rotating) refresh token."""
        if self.host_guard is not None:
            self.host_guard(base_url)
        policy = HttpPolicy(base_url=base_url, read_timeout=TOKEN_READ_TIMEOUT_S, max_attempts=3)
        return ConnectorHttp(policy, self.clock, transport=self._transport(base_url))

    def _oauth_client(self, connection: Connection) -> OAuthClient | None:
        connector = self.registry.find(connection.kind)
        if (
            connector is None
            or connector.oauth is None
            or connector.capabilities.auth is not AuthMethod.OAUTH2
        ):
            return None
        app = self.apps.get(connection.kind)
        if app is None:
            return None
        return OAuthClient(connector.oauth, app, self._plain_http)

    def http(self, connection: Connection, *, candidate: TokenSet | None = None) -> ConnectorHttp:
        """An authenticated, paced, version-pinned client for one connection.

        `candidate` authenticates with tokens that are not stored yet (a connect checking who the
        account is): they are used as they are, never refreshed, and the vault is not touched, so a
        mistyped key for an account that is already connected cannot disturb the live connection."""
        connector = self.connector(connection)
        caps = connector.capabilities
        base_url = connector.base_url(connection)
        if self.host_guard is not None:
            self.host_guard(base_url)
        version = caps.api_version
        headers = ((version.header, version.value),) if version.header else ()
        if connection.connection_id not in self._buckets:
            self._buckets[connection.connection_id] = bucket_for(caps.rate)

        def auth() -> Mapping[str, str]:
            if candidate is not None:
                return connector.auth_headers(connection, candidate)
            return connector.auth_headers(connection, self.tokens.access_token(connection, self.clock.now()))

        def unauthorized() -> None:
            self.tokens.force_refresh(connection, self.clock.now())

        policy = HttpPolicy(base_url=base_url, version_headers=headers)
        return ConnectorHttp(
            policy,
            self.clock,
            transport=self._transport(base_url),
            auth=auth,
            on_unauthorized=unauthorized if candidate is None else None,
            bucket=self._buckets[connection.connection_id],
            pacing=connector.pacing,
            retry_signal=connector.retry_signal,
            host_guard=self.host_guard,
        )

    def context(
        self, connection: Connection, now: datetime, *, candidate: TokenSet | None = None
    ) -> ConnectionContext:
        return ConnectionContext(
            connection=connection,
            http=self.http(connection, candidate=candidate),
            now=now,
            app=self.apps.get(connection.kind),
        )

    # -- connecting -------------------------------------------------------------------------------

    def begin_oauth(
        self,
        kind: str,
        *,
        business_id: str,
        redirect_uri: str,
        now: datetime,
        params: Mapping[str, str] | None = None,
        return_to: str | None = None,
    ) -> str:
        """The consent URL to send the person's browser to."""
        connector = self.registry.get(kind)
        if connector.oauth is None:
            raise NotSupported(f"{connector.capabilities.label} connects with an API key, not OAuth")
        app = self._app(kind)
        url, _pending = begin_authorization(
            connector.oauth,
            app,
            self.store,
            kind=kind,
            business_id=business_id,
            redirect_uri=redirect_uri,
            now=now,
            params=params,
            return_to=return_to,
        )
        return url

    def finish_oauth(
        self, kind: str, query: Mapping[str, str], now: datetime, queue: JobQueue | None = None
    ) -> Connection:
        """Handle the platform's redirect: check state, exchange the code, store everything, seed jobs.

        Platform-specific callback checks (Shopify's query `hmac`) belong in the connector's
        `validate_callback(query, app)`, called here first."""
        connector = self.registry.get(kind)
        app = self._app(kind)
        connector.validate_callback(query, app)
        pending, code = finish_authorization(query, self.store, now)
        if pending.kind != kind:
            raise AuthorizationError("this connect link was for another platform; start again")
        assert connector.oauth is not None
        client = OAuthClient(connector.oauth, app, self._plain_http)
        tokens = client.exchange(pending, code, now)
        config = dict(pending.params)
        return self._connected(
            connector, pending.business_id, tokens, config, now, queue, fallback=pending.state
        )

    def connect_api_key(
        self,
        kind: str,
        *,
        business_id: str,
        api_key: str,
        now: datetime,
        config: Mapping[str, str] | None = None,
        queue: JobQueue | None = None,
    ) -> Connection:
        connector = self.registry.get(kind)
        tokens = TokenSet.api_key(api_key.strip(), now)
        digest = hashlib.sha256(api_key.strip().encode()).hexdigest()[:16]
        return self._connected(
            connector, business_id, tokens, dict(config or {}), now, queue, fallback=digest
        )

    def _connected(
        self,
        connector: SaasConnector,
        business_id: str,
        tokens: TokenSet,
        config: dict[str, str],
        now: datetime,
        queue: JobQueue | None,
        *,
        fallback: str,
    ) -> Connection:
        kind = connector.kind
        external = config.get("shop") or config.get("account_id") or tokens.extra.get("account_id")
        provisional = Connection(
            connection_id=stable_id("conn", kind, business_id, external or fallback),
            kind=kind,
            business_id=business_id,
            label=external or connector.capabilities.label,
            external_id=external,
            config=config,
            created_at=now,
            updated_at=now,
        )
        # Ask who the account is with the new tokens *before* storing them: the provisional id can be a
        # live connection's (the same shop pasted again with a mistyped token, a reconnect whose identity
        # call times out), and a failed attempt must leave that connection's access untouched.
        ctx = self.context(provisional, now, candidate=tokens)
        try:
            identity: Identity | None = connector.identify(ctx)
        finally:
            ctx.http.close()
        external_id = identity.external_id if identity is not None else external
        connection_id = stable_id("conn", kind, business_id, external_id or fallback)
        previous = self.store.get_connection(connection_id)
        connection = provisional.model_copy(
            update={
                "connection_id": connection_id,
                "external_id": external_id,
                "label": identity.label if identity is not None else provisional.label,
                "status": ConnectionStatus.ACTIVE,
                "status_reason": None,
                "created_at": previous.created_at if previous is not None else now,
                "history_anchor": previous.history_anchor if previous is not None else None,
            }
        )
        self.tokens.save(connection, tokens, now)
        self.store.put_connection(connection)
        self.monitor.desk.resolve_others(connection_id, (), now)
        if queue is not None:
            self.seed(connection, now, queue)
        return connection

    def _app(self, kind: str) -> AppCredentials:
        app = self.apps.get(kind)
        if app is None:
            id_name, secret_name = AppCredentials.env_names(kind)
            raise NotSupported(f"this server has no {kind} app credentials: set {id_name} and {secret_name}")
        return app

    def disconnect(
        self, connection_id: str, now: datetime, *, reason: str = "disconnected by a person"
    ) -> None:
        """Forget the tokens and stop every job for this connection (they find it inactive and end)."""
        self.store.set_status(connection_id, ConnectionStatus.DISCONNECTED, reason, now)
        self.tokens.vault.delete(connection_id)

    # -- scheduling -------------------------------------------------------------------------------

    def seed(self, connection: Connection, now: datetime, queue: JobQueue) -> list[Job]:
        """Queue the first job of every chain: each stream's live lane, the history lanes, the daily job."""
        jobs: list[Job] = []
        for spec in self.connector(connection).capabilities.streams:
            jobs.append(sync_job(connection.connection_id, spec.stream, Lane.LIVE, now))
            if spec.backfill_days > 0:
                jobs.append(sync_job(connection.connection_id, spec.stream, Lane.HISTORY, now))
        jobs.append(daily_job(connection.connection_id, now))
        for job in jobs:
            enqueue_once(queue, job)
        return jobs

    def request_catch_up(self, connection_id: str, stream: Stream, now: datetime, queue: JobQueue) -> Job:
        """The hourly catch-up poll behind a webhook: one job per stream per hour, however many webhooks."""
        job = sync_job(connection_id, stream, Lane.LIVE, slot_after(now, CATCH_UP_EVERY))
        enqueue_once(queue, job)
        return job

    def handlers(self, queue: JobQueue) -> dict[str, Callable[[Job, datetime], None]]:
        """The worker's handlers for the connector job kinds (`autonomy.runtime.worker.Worker`)."""
        from autonomy.connectors.webhooks import WebhookIntake

        intake = WebhookIntake(self, queue)

        def run_sync(job: Job, now: datetime) -> None:
            connection_id = str(job.payload.get("connection_id") or "")
            try:
                stream = Stream(str(job.payload.get("stream")))
                lane = Lane(str(job.payload.get("lane")))
            except ValueError:
                return  # a job from an older build; its chain ends and the daily job re-seeds
            self.run_and_reschedule(connection_id, stream, lane, now, queue)

        def run_daily(job: Job, now: datetime) -> None:
            connection_id = str(job.payload.get("connection_id") or "")
            connection = self.store.get_connection(connection_id)
            if connection is None or connection.status is ConnectionStatus.DISCONNECTED:
                return
            enqueue_once(queue, daily_job(connection_id, slot_after(now, timedelta(days=1))))
            self.daily(connection_id, now, queue)

        return {SYNC_KIND: run_sync, WEBHOOK_KIND: intake.handle_job, DAILY_KIND: run_daily}

    def run_and_reschedule(
        self, connection_id: str, stream: Stream, lane: Lane, now: datetime, queue: JobQueue
    ) -> SyncReport:
        connection = self.store.get_connection(connection_id)
        if connection is None or connection.status is not ConnectionStatus.ACTIVE:
            return SyncReport(connection_id, stream, lane, status="skipped")
        spec = self._spec(connection, stream)
        if spec is None:
            return SyncReport(connection_id, stream, lane, status="skipped")
        if lane is Lane.LIVE:
            regular = slot_after(now, spec.cadence)
            report = self.sync(connection_id, stream, lane, now)
            if report.status == "reconnect":
                return report
            if report.status == "more":
                next_at = now
            elif report.retry_at is not None and report.retry_at < regular:
                next_at = report.retry_at
            else:
                next_at = max(regular, report.retry_at or regular)
            tag = "" if next_at == regular else self._continuation_tag(connection_id, stream, lane, report)
            enqueue_once(queue, sync_job(connection_id, stream, lane, next_at, tag=tag))
            return report
        report = self.sync(connection_id, stream, lane, now)
        if report.status in ("done", "reconnect", "skipped"):
            return report
        tag = self._continuation_tag(connection_id, stream, lane, report)
        enqueue_once(queue, sync_job(connection_id, stream, lane, report.retry_at or now, tag=tag))
        return report

    def _continuation_tag(self, connection_id: str, stream: Stream, lane: Lane, report: SyncReport) -> str:
        p = self.store.position(connection_id, stream, lane)
        return stable_id(
            "next", p.cursor, p.reached, p.window_start, p.high_watermark, p.error_streak, report.status
        )

    # -- syncing ----------------------------------------------------------------------------------

    def _spec(self, connection: Connection, stream: Stream) -> StreamSpec | None:
        connector = self.registry.find(connection.kind)
        if connector is None:
            return None
        try:
            return connector.capabilities.stream(stream)
        except NotSupported:
            return None

    def sync(
        self, connection_id: str, stream: Stream, lane: Lane, now: datetime, *, max_pages: int = MAX_PAGES
    ) -> SyncReport:
        """Run one lane of one stream for up to `max_pages` pages; never raises for a platform problem."""
        report = SyncReport(connection_id, stream, lane)
        connection = self.store.get_connection(connection_id)
        if connection is None or connection.status is not ConnectionStatus.ACTIVE:
            report.status = "skipped"
            return report
        spec = self._spec(connection, stream)
        if spec is None or (lane is Lane.HISTORY and spec.backfill_days == 0):
            report.status = "skipped"
            return report
        lease = f"sync:{connection_id}:{stream.value}:{lane.value}"
        if not self.store.acquire_lease(lease, self.holder, now, SYNC_LEASE_TTL):
            report.status = "skipped"
            report.error = "another worker is syncing this stream"
            return report
        started = now
        position = self.store.position(connection_id, stream, lane)
        try:
            if lane is Lane.HISTORY and position.done:
                report.status = "done"
                return report
            if position.waiting_until is not None and position.waiting_until > now:
                report.status = "waiting"
                report.retry_at = position.waiting_until
                return report
            position = self._run_pages(connection, spec, lane, position, now, max_pages, report)
        except ConnectorFailure as failure:
            # Pages stored before the failure saved their cursor; start from that, not from the
            # position this run began with, or a 429 after page 15 would throw 15 pages away
            # (and a lane rate-limited every run would never move).
            saved = self.store.position(connection_id, stream, lane)
            position = self._failed(connection, spec, saved, failure, now, report)
        except Exception as exc:  # a connector bug: record it like a failure, never kill the chain
            bug = ConnectorFailure(f"{type(exc).__name__}: {exc}")
            saved = self.store.position(connection_id, stream, lane)
            position = self._failed(connection, spec, saved, bug, now, report)
        finally:
            self.store.release_lease(lease, self.holder)
        position = position.model_copy(update={"last_attempt_at": now})
        self.store.put_position(position, now)
        self.store.record_run(
            SyncRun(
                run_id=stable_id("run", connection_id, stream.value, lane.value, started.isoformat()),
                connection_id=connection_id,
                stream=stream,
                lane=lane,
                started_at=started,
                finished_at=now,
                ok=report.status not in ("retry", "reconnect"),
                complete=report.status in ("ok", "done"),
                rows=report.rows,
                new_records=report.new_records,
                events=report.events,
                problems=report.problems,
                missing_required=report.missing_required,
                error_code=report.error_code,
                error=report.error,
            )
        )
        if lane is Lane.LIVE and report.status != "reconnect":
            refreshed = self.store.get_connection(connection_id) or connection
            report.alerts.extend(self.monitor.evaluate(refreshed, now))
        return report

    def _window(
        self, connection: Connection, spec: StreamSpec, lane: Lane, position: SyncPosition, now: datetime
    ) -> SyncWindow | None:
        if position.in_window:
            assert position.window_end is not None
            return SyncWindow(position.window_start, position.window_end)
        if lane is Lane.LIVE:
            if spec.snapshot:
                return SyncWindow(None, now)
            if position.high_watermark is not None:
                return SyncWindow(position.high_watermark - spec.overlap, now)
            if spec.backfill_days > 0:
                return SyncWindow(connection.anchor - spec.overlap, now)
            return SyncWindow(None, now)
        oldest = connection.anchor - timedelta(days=spec.backfill_days)
        end = position.reached or connection.anchor
        if end <= oldest:
            return None
        return SyncWindow(max(oldest, end - spec.backfill_chunk), end)

    def _run_pages(
        self,
        connection: Connection,
        spec: StreamSpec,
        lane: Lane,
        position: SyncPosition,
        now: datetime,
        max_pages: int,
        report: SyncReport,
    ) -> SyncPosition:
        connector = self.connector(connection)
        window = self._window(connection, spec, lane, position, now)
        if window is None:
            report.status = "done"
            return position.model_copy(
                update={"done": True, "window_start": None, "window_end": None, "cursor": None}
            )
        position = position.model_copy(
            update={"window_start": window.start, "window_end": window.end, "waiting_until": None}
        )
        ctx = self.context(connection, now)
        try:
            for _ in range(max_pages):
                try:
                    page = connector.read(ctx, spec.stream, window, position.cursor)
                except CursorExpired:
                    if position.cursor is None:
                        raise
                    position = position.model_copy(update={"cursor": None})
                    continue
                if page.pending_until is not None:
                    position = position.model_copy(
                        update={"cursor": page.next_cursor, "waiting_until": page.pending_until}
                    )
                    self.store.put_position(position, now)
                    report.status = "waiting"
                    report.retry_at = page.pending_until
                    return position
                got = self.ingest(connection, spec, page.records, now)
                report.rows += got.rows
                report.new_records += got.new_records
                report.events += got.events
                report.problems += got.problems
                report.missing_required += got.missing_required
                if got.historical and self.history_hook is not None:
                    self.history_hook(connection, got.historical, now)
                position = position.model_copy(update={"cursor": page.next_cursor})
                if page.next_cursor is None:
                    return self._window_done(connection, spec, lane, position, window, now, report)
                self.store.put_position(position, now)  # resume here after a crash or a 429
                lease = f"sync:{connection.connection_id}:{spec.stream.value}:{lane.value}"
                self.store.acquire_lease(lease, self.holder, self.clock.now(), SYNC_LEASE_TTL)  # renew
        finally:
            ctx.http.close()
        report.status = "more"
        return position

    def _window_done(
        self,
        connection: Connection,
        spec: StreamSpec,
        lane: Lane,
        position: SyncPosition,
        window: SyncWindow,
        now: datetime,
        report: SyncReport,
    ) -> SyncPosition:
        cleared: dict[str, Any] = {
            "window_start": None,
            "window_end": None,
            "cursor": None,
            "last_success_at": now,
            "last_rows": report.rows,
            "error_streak": 0,
            "last_error_code": None,
            "last_error": None,
        }
        if lane is Lane.LIVE:
            watermark = (
                window.end if position.high_watermark is None else max(position.high_watermark, window.end)
            )
            cleared["high_watermark"] = watermark
            report.status = "ok"
        else:
            oldest = connection.anchor - timedelta(days=spec.backfill_days)
            reached = window.start if window.start is not None else oldest
            cleared["reached"] = reached
            cleared["done"] = reached <= oldest
            report.status = "done" if cleared["done"] else "ok"
        return position.model_copy(update=cleared)

    def _failed(
        self,
        connection: Connection,
        spec: StreamSpec,
        position: SyncPosition,
        failure: ConnectorFailure,
        now: datetime,
        report: SyncReport,
    ) -> SyncPosition:
        report.error_code = failure.code
        report.error = failure.message
        if isinstance(failure, NeedsReconnect):
            report.status = "reconnect"
            self._on_revoked(connection, failure, now)
            return position.model_copy(
                update={"last_error_code": failure.code, "last_error": failure.message}
            )
        streak = position.error_streak + 1
        if failure.action == "restart":
            position = position.model_copy(update={"cursor": None})
        report.status = "retry"
        backoff = min(spec.cadence, timedelta(minutes=1) * (2 ** min(streak - 1, 12)))
        report.retry_at = failure.retry_at if failure.retry_at is not None else now + backoff
        return position.model_copy(
            update={
                "error_streak": streak,
                "last_error_code": failure.code,
                "last_error": redact(failure.message),
            }
        )

    # -- ingesting --------------------------------------------------------------------------------

    def ingest(
        self, connection: Connection, spec: StreamSpec, records: Sequence[Mapping[str, Any]], now: datetime
    ) -> _Ingested:
        """Map and store the records that are newer than what was seen; return the counts."""
        out = _Ingested(rows=len(records))
        if not records:
            return out
        connector = self.connector(connection)
        keyed: dict[str, tuple[RecordKey, Mapping[str, Any]]] = {}
        for record in records:
            if spec.required_fields and any(record.get(f) is None for f in spec.required_fields):
                out.missing_required += 1
            try:
                key = connector.record_key(spec.stream, record)
            except (KeyError, TypeError, ValueError):
                out.problems += 1
                continue
            held = keyed.get(key.record_id)
            if held is None or _newer(key.updated_at, held[0].updated_at):
                keyed[key.record_id] = (key, record)
        stored = self.store.record_versions(connection.connection_id, spec.stream, list(keyed))
        fresh = [
            (key, record)
            for key, record in keyed.values()
            if key.updated_at is None
            or key.record_id not in stored
            or _newer(key.updated_at, stored[key.record_id])
        ]
        out.new_records = len(fresh)
        if not fresh:
            return out
        mapped = connector.map_records(spec.stream, [r for _, r in fresh], connection)
        out.problems += len(mapped.problems)
        if mapped.events:
            out.events = self.sink(list(mapped.events))
            floor = now - LATE_WINDOW
            out.historical = sum(1 for e in mapped.events if e.ts < floor) if out.events else 0
        self.store.claim_newer(connection.connection_id, spec.stream, [k for k, _ in fresh])
        return out

    # -- maintenance ------------------------------------------------------------------------------

    def daily(self, connection_id: str, now: datetime, queue: JobQueue | None = None) -> list[Alert]:
        """Keep-alive refresh, webhook re-registration, a health check and pruning, for one connection."""
        alerts: list[Alert] = []
        connection = self.store.get_connection(connection_id)
        if connection is None or connection.status is ConnectionStatus.DISCONNECTED:
            return alerts
        try:
            self.tokens.keep_alive(connection, now)
        except NeedsReconnect:
            return alerts  # `_on_revoked` raised the one alert
        except ConnectorFailure:
            pass  # transient: tomorrow's job (or any sync) refreshes
        if connection.status is not ConnectionStatus.ACTIVE:
            return alerts
        alerts.extend(self._reregister(connection, now))
        if queue is not None:
            self._reseed_missing(connection, now, queue)
        alerts.extend(self.monitor.evaluate(connection, now))
        self.store.prune_deliveries(now - DELIVERY_MEMORY)
        self.store.prune_runs(now - RUN_MEMORY)
        return alerts

    def _reregister(self, connection: Connection, now: datetime) -> list[Alert]:
        connector = self.connector(connection)
        topics = [t.topic for t in connector.capabilities.webhook_topics]
        overridden = type(connector).register_webhooks is not SaasConnector.register_webhooks
        if not topics or self.webhook_base_url is None or not overridden:
            return []  # app-level subscriptions (e.g. shopify.app.toml) need no renewal from here
        callback = f"{self.webhook_base_url.rstrip('/')}/{connection.kind}/{connection.connection_id}"
        ctx = self.context(connection, now)
        try:
            live = connector.register_webhooks(ctx, callback, topics)
        except NeedsReconnect as failure:
            self._on_revoked(connection, failure, now)
            return []
        except ConnectorFailure as failure:
            problem = Problem(
                code=ALERT_WEBHOOKS,
                severity="warning",
                message=f"{connection.label}: could not renew webhook subscriptions ({failure.code}); "
                "the hourly catch-up keeps data flowing meanwhile.",
            )
            alert = self.monitor.desk.raise_problem(connection, problem, now)
            return [alert] if alert is not None else []
        finally:
            ctx.http.close()
        missing = sorted(set(topics) - set(live))
        if missing:
            problem = Problem(
                code=ALERT_WEBHOOKS,
                severity="warning",
                message=f"{connection.label}: webhooks not active for {', '.join(missing)}; polling covers them.",
            )
            alert = self.monitor.desk.raise_problem(connection, problem, now)
            return [alert] if alert is not None else []
        self.monitor.desk.resolve(connection.connection_id, ALERT_WEBHOOKS, now)
        return []

    def _reseed_missing(self, connection: Connection, now: datetime, queue: JobQueue) -> None:
        """A chain can end (a job left FAILED by an operator, a lost queue row): restart live lanes found idle."""
        for spec in self.connector(connection).capabilities.streams:
            live = self.store.position(connection.connection_id, spec.stream, Lane.LIVE)
            idle = live.last_attempt_at is None or now - live.last_attempt_at > spec.cadence * 3
            if idle:
                enqueue_once(queue, sync_job(connection.connection_id, spec.stream, Lane.LIVE, now))
            history = self.store.position(connection.connection_id, spec.stream, Lane.HISTORY)
            if spec.backfill_days > 0 and not history.done:
                stalled = history.last_attempt_at is None or now - history.last_attempt_at > timedelta(
                    hours=6
                )
                if stalled:
                    enqueue_once(queue, sync_job(connection.connection_id, spec.stream, Lane.HISTORY, now))

    # -- status changes ---------------------------------------------------------------------------

    def _on_revoked(self, connection: Connection, failure: NeedsReconnect, now: datetime) -> None:
        current = self.store.get_connection(connection.connection_id)
        if current is None or current.status is ConnectionStatus.DISCONNECTED:
            return  # not connected yet (a bad key at connect time is answered to the person directly)
        self.store.set_status(connection.connection_id, ConnectionStatus.NEEDS_RECONNECT, failure.code, now)
        label = self.registry.find(connection.kind)
        name = label.capabilities.label if label is not None else connection.kind
        self.monitor.desk.raise_problem(
            current,
            Problem(
                code=ALERT_RECONNECT,
                severity="critical",
                message=(
                    f"{name} ({connection.label}) needs reconnecting: {failure.message}. Decisions that "
                    "rely on it are on hold. Open Connect and reconnect it; nothing else is needed."
                ),
            ),
            now,
            auto_paused=True,
        )

    def uninstalled(self, connection: Connection, now: datetime) -> Alert | None:
        """The app was removed on the platform: forget tokens, stop syncing, tell a person once."""
        self.store.set_status(connection.connection_id, ConnectionStatus.DISCONNECTED, "uninstalled", now)
        self.tokens.vault.delete(connection.connection_id)
        connector = self.registry.find(connection.kind)
        name = connector.capabilities.label if connector is not None else connection.kind
        return self.monitor.desk.raise_problem(
            connection,
            Problem(
                code=ALERT_UNINSTALLED,
                severity="critical",
                message=f"The Autonomy app was removed from {name} ({connection.label}). Decisions that rely on it "
                "are on hold. Connect it again to resume.",
            ),
            now,
            auto_paused=True,
        )


def _newer(a: datetime | None, b: datetime | None) -> bool:
    if a is None:
        return False
    return b is None or a > b


def _default_guard(url: str) -> None:
    """v1's address guard (refuses link-local / metadata addresses), imported only when used."""
    from engine.connections.net import check_url

    try:
        check_url(url)
    except Exception as exc:  # engine.connections.ConnectorError → our own failure, same words
        message = getattr(exc, "message", str(exc))
        raise NotSupported(str(message)) from None
