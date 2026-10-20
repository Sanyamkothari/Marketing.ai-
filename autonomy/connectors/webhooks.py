"""Webhook intake: verify, dedupe, queue and answer within milliseconds; do the work later.

Platforms treat a slow answer as a failure (Shopify and HubSpot after ~5 s) and, after enough
failures, delete the subscription (Shopify: 8 tries over ~4 h). So `WebhookIntake.receive` does
only what must happen before answering:

1. find the connector and the connection (from the URL, or the account header such as
   `X-Shopify-Shop-Domain`),
2. **verify the signature** over the raw body, in constant time (`hmac_matches`), failing closed,
3. **dedupe** on the platform's delivery id (`X-Shopify-Webhook-Id`), or a hash of the body when
   there is none; deliveries are at least once,
4. **queue** a `connector_webhook` job carrying the raw headers and body, and ask for the hourly
   **catch-up poll** of the stream the topic feeds (one job per stream per hour, however many
   webhooks arrive: see `ConnectorEngine.request_catch_up`),

and returns an `IntakeResult` whose `http_status` the route sends back (200 for accepted and
duplicate, 401 for a bad signature, 404 for an unknown connector).

`handle_job` runs later in a worker: it parses the delivery and feeds its records through the same
`ConnectorEngine.ingest` the polls use, so an **out-of-order** delivery (an older copy after a
newer one) is skipped by the record-version check, and replays add nothing (stable event ids).
Uninstall topics disconnect the connection and raise its one alert; privacy topics (Shopify's
mandatory `customers/data_request`, `customers/redact`, `shop/redact`) are recorded in
`connector_privacy` for the data layer and raise one alert each, so a person knows a legal clock
(30 days) is running until that is automated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from autonomy.connectors.base import (
    Connection,
    ConnectionStatus,
    NotSupported,
    Stream,
    StreamSpec,
    WebhookDelivery,
    WebhookTopic,
)
from autonomy.connectors.health import ALERT_PRIVACY, Problem
from autonomy.connectors.store import PrivacyRequest
from autonomy.connectors.sync import WEBHOOK_KIND, ConnectorEngine, enqueue_once
from autonomy.contracts import Job, stable_id

if TYPE_CHECKING:
    from autonomy.interfaces import JobQueue

__all__ = [
    "IntakeResult",
    "IntakeStatus",
    "WebhookIntake",
    "hmac_matches",
    "query_hmac_matches",
    "sign",
]

Encoding = Literal["base64", "hex"]


def sign(
    secret: str | bytes, message: bytes, *, encoding: Encoding = "base64", algorithm: str = "sha256"
) -> str:
    """The HMAC of `message` under `secret`, encoded (also used by tests to make signed fixtures)."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    digest = hmac.new(key, message, algorithm).digest()
    return base64.b64encode(digest).decode("ascii") if encoding == "base64" else digest.hex()


def hmac_matches(
    secret: str | bytes,
    message: bytes,
    given: str | None,
    *,
    encoding: Encoding = "base64",
    algorithm: str = "sha256",
    prefix: str = "",
) -> bool:
    """Whether `given` (e.g. `X-Shopify-Hmac-Sha256`) is the HMAC of the raw body. Constant time.

    An empty secret or signature never matches: an unconfigured app must reject, not accept."""
    if not secret or not given:
        return False
    value = given.strip()
    if prefix:
        if not value.startswith(prefix):
            return False
        value = value[len(prefix) :]
    expected = sign(secret, message, encoding=encoding, algorithm=algorithm)
    if encoding == "hex":
        value = value.lower()
    return hmac.compare_digest(value.encode("ascii", "replace"), expected.encode("ascii"))


def query_hmac_matches(secret: str, params: Mapping[str, str], *, field: str = "hmac") -> bool:
    """A signed query string (Shopify's OAuth callback and app launch): hex HMAC-SHA256 of the other
    parameters (decoded values), each written `k=v`, sorted, and joined with `&`; `hmac` and
    `signature` left out. As in Shopify's own libraries, `%` becomes `%25` in keys and values, `=`
    becomes `%3D` in keys and `&` becomes `%26` in a pair, so a value holding `&` or `=` cannot
    forge a different parameter list."""
    given = params.get(field)
    pairs = sorted(
        f"{_escape_key(k)}={v.replace('%', '%25')}".replace("&", "%26")
        for k, v in params.items()
        if k not in (field, "signature")
    )
    return hmac_matches(secret, "&".join(pairs).encode("utf-8"), given, encoding="hex")


def _escape_key(key: str) -> str:
    return key.replace("%", "%25").replace("=", "%3D")


_UNSTORED_HEADERS: frozenset[str] = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
"""Never copied into the queued job: a proxy in front of the route may add credentials."""


class IntakeStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntakeResult:
    status: IntakeStatus
    http_status: int
    connection_id: str | None = None
    job_id: str | None = None
    detail: str | None = None


class WebhookIntake:
    def __init__(self, engine: ConnectorEngine, queue: JobQueue) -> None:
        self.engine = engine
        self.queue = queue

    # -- the fast path (the HTTP route) ---------------------------------------------------------

    def receive(
        self,
        kind: str,
        headers: Mapping[str, str],
        body: bytes,
        now: datetime,
        *,
        connection_id: str | None = None,
    ) -> IntakeResult:
        connector = self.engine.registry.find(kind)
        if connector is None:
            return IntakeResult(IntakeStatus.UNKNOWN, 404, detail="no such connector")
        lowered = {k.lower(): v for k, v in headers.items()}
        topic = connector.webhook_topic(lowered, body)
        spec = connector.capabilities.topic(topic) if topic else None
        connection = self._connection(kind, connection_id, lowered, body, spec)
        secret = connector.webhook_secret(self.engine.apps.get(kind), {})
        if not secret or not connector.verify_webhook(secret, lowered, body):
            return IntakeResult(IntakeStatus.REJECTED, 401, detail="the signature does not match")
        if connection is None:
            # Signed by the platform, but for an account we do not hold (deleted, or never connected):
            # answer 200 so the platform does not retry, and remove nothing.
            return IntakeResult(IntakeStatus.UNKNOWN, 200, detail="no connection for this account")
        delivery_id = (
            connector.webhook_delivery_id(lowered, body) or "sha256:" + hashlib.sha256(body).hexdigest()
        )
        account = connection.connection_id
        if self.engine.store.delivery_seen(account, delivery_id):
            return IntakeResult(IntakeStatus.DUPLICATE, 200, connection_id=account)
        job = Job(
            job_id=stable_id("job", WEBHOOK_KIND, account, delivery_id),
            kind=WEBHOOK_KIND,
            payload={
                "connection_id": account,
                "kind": kind,
                "delivery_id": delivery_id,
                "headers": json.dumps(
                    {k: v for k, v in lowered.items() if k not in _UNSTORED_HEADERS}, sort_keys=True
                ),
                "body": body.decode("utf-8", "replace"),
            },
            run_at=now,
        )
        # Queued before it is remembered: a crash here means a retry, not a loss. `enqueue_once`, so
        # a concurrent duplicate never resets the copy a worker is already running.
        enqueue_once(self.queue, job)
        self.engine.store.remember_delivery(account, delivery_id, now)
        if spec is not None and spec.stream is not None and connection.status is ConnectionStatus.ACTIVE:
            self.engine.request_catch_up(account, spec.stream, now, self.queue)
        return IntakeResult(IntakeStatus.ACCEPTED, 200, connection_id=account, job_id=job.job_id)

    def _connection(
        self,
        kind: str,
        connection_id: str | None,
        headers: Mapping[str, str],
        body: bytes,
        topic: WebhookTopic | None,
    ) -> Connection | None:
        store = self.engine.store
        if connection_id:
            found = store.get_connection(connection_id)
            return found if found is not None and found.kind == kind else None
        connector = self.engine.registry.get(kind)
        account = connector.webhook_account(headers, body)
        if not account:
            return None
        found = store.find_connection(kind, account)
        if found is None and topic is not None and (topic.privacy is not None or topic.uninstall):
            # Shopify sends shop/redact ~48 h *after* app/uninstalled: a privacy request for an
            # account we disconnected must still be recorded, not answered "unknown".
            found = store.find_connection(kind, account, include_disconnected=True)
        return found

    # -- the slow path (a worker) ----------------------------------------------------------------

    def handle_job(self, job: Job, now: datetime) -> None:
        from autonomy.runtime.worker import PermanentJobError

        connection = self.engine.store.get_connection(str(job.payload.get("connection_id") or ""))
        if connection is None:
            return
        connector = self.engine.registry.find(connection.kind)
        if connector is None:
            raise PermanentJobError(f"no connector for {connection.kind}")
        try:
            headers = {str(k): str(v) for k, v in json.loads(str(job.payload.get("headers") or "{}")).items()}
        except (ValueError, AttributeError) as exc:
            raise PermanentJobError("the stored webhook headers are unreadable") from exc
        body = str(job.payload.get("body") or "").encode("utf-8")
        try:
            delivery = connector.parse_webhook(headers, body)
        except NotSupported as exc:
            raise PermanentJobError(str(exc)) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentJobError(
                f"could not read the {connection.kind} webhook: {type(exc).__name__}"
            ) from exc
        self.apply(connection, delivery, now)

    def apply(self, connection: Connection, delivery: WebhookDelivery, now: datetime) -> int:
        """Act on a parsed delivery; return how many events it added."""
        if delivery.uninstalled:
            self.engine.uninstalled(connection, now)
            return 0
        if delivery.privacy is not None:
            request = PrivacyRequest(
                request_id=stable_id("privacy", connection.connection_id, delivery.delivery_id),
                connection_id=connection.connection_id,
                kind=delivery.privacy,
                received_at=now,
                subject={str(k): v for k, v in delivery.subject.items()},
            )
            if self.engine.store.put_privacy_request(request):
                self.engine.monitor.desk.raise_problem(
                    connection,
                    Problem(
                        code=ALERT_PRIVACY,
                        severity="warning",
                        message=f"{connection.label} sent a privacy request ({delivery.privacy.value}); it must be "
                        "completed within 30 days.",
                    ),
                    now,
                    code=f"{ALERT_PRIVACY}:{request.request_id}",
                )
            return 0
        if (
            delivery.stream is None
            or not delivery.records
            or connection.status is not ConnectionStatus.ACTIVE
        ):
            return 0
        spec = self._spec(connection, delivery.stream)
        if spec is None:
            return 0
        return self.engine.ingest(connection, spec, delivery.records, now).events

    def _spec(self, connection: Connection, stream: Stream) -> StreamSpec | None:
        connector = self.engine.registry.find(connection.kind)
        if connector is None:
            return None
        return next((s for s in connector.capabilities.streams if s.stream is stream), None)
