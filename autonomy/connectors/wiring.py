"""How the connectors plug into the rest of Autonomy: one place, so the API, the worker and the runtime agree.

* `production_registry(events)` - the built-in connectors, with Klaviyo's engagement linked to the
  decisions handed to it (`EventStoreHandoffs`). Without that link the delivery watch can only hold.
* `after_connect(connection, queue, now)` - the platform-specific jobs a new (or re-made)
  connection needs on top of `ConnectorEngine.seed`: Klaviyo's hourly setup-and-delivery watch, and the
  Shopify privacy chain. Idempotent (`enqueue_once`), so the API calls it after every connect.
* `worker_handlers(engine, queue, events=, privacy_desk=)` - every connector job kind the worker runs:
  the engine's sync, webhook and daily jobs, Klaviyo's watch (with an event store) and Shopify's privacy
  job (with a `ShopifyPrivacyDesk`). `autonomy.runtime.worker.connector_handlers` delegates here.
* `send_providers(engine, business_id, fallback=)` - the decided default: send through the customer's own
  tool. Each channel the business's active Klaviyo connection sends gets Klaviyo first and our own
  adapter (SES, ...) second, ready for `DispatchGateway(providers)`; a channel Klaviyo does not cover
  keeps only the fallback. `send_adapters` is the same for a plain `Dispatcher` (no failover).

Imports of the platform packages are local to each function, so `import autonomy.connectors` stays light
and free of import cycles (`autonomy.channels.klaviyo` imports this package).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from autonomy.connectors.base import Connection, ConnectionStatus, SaasConnector
from autonomy.connectors.registry import ConnectorRegistry, default_registry
from autonomy.connectors.sync import enqueue_once
from autonomy.contracts import Channel, Job

if TYPE_CHECKING:
    from autonomy.channels.gateway import Provider
    from autonomy.channels.klaviyo import DecisionDetails
    from autonomy.connectors.shopify import ProductSink, ShopifyPrivacyDesk
    from autonomy.connectors.sync import ConnectorEngine
    from autonomy.interfaces import ChannelAdapter, EventStore, JobQueue

__all__ = [
    "JobHandler",
    "after_connect",
    "production_registry",
    "send_adapters",
    "send_providers",
    "worker_handlers",
]

JobHandler = Callable[[Job, datetime], None]


def production_registry(
    events: EventStore | None = None, *, product_sink: ProductSink | None = None
) -> ConnectorRegistry:
    """The built-in connectors for a deployment. `events` links Klaviyo engagement to Autonomy's hand-offs
    (needed for delivery read-back); `product_sink` receives Shopify products for the catalogue."""
    extra: list[SaasConnector] = []
    if events is not None:
        from autonomy.channels.klaviyo import EventStoreHandoffs
        from autonomy.connectors.klaviyo import KlaviyoConnector

        extra.append(KlaviyoConnector(handoffs=EventStoreHandoffs(events)))
    if product_sink is not None:
        from autonomy.connectors.shopify import ShopifyConnector

        extra.append(ShopifyConnector(product_sink=product_sink))
    return default_registry(extra)


def after_connect(connection: Connection, queue: JobQueue, now: datetime) -> list[Job]:
    """Queue the platform-specific chains a connection needs besides its syncs. Safe to call twice."""
    jobs: list[Job] = []
    if connection.status is not ConnectionStatus.ACTIVE:
        return jobs
    if connection.kind == "klaviyo":
        from autonomy.channels.klaviyo import seed_klaviyo_watch

        jobs.append(seed_klaviyo_watch(queue, connection.connection_id, now))
    elif connection.kind == "shopify":
        from autonomy.connectors.shopify import privacy_job

        job = privacy_job(now)
        enqueue_once(queue, job)
        jobs.append(job)
    return jobs


def worker_handlers(
    engine: ConnectorEngine,
    queue: JobQueue,
    *,
    events: EventStore | None = None,
    privacy_desk: ShopifyPrivacyDesk | None = None,
) -> dict[str, JobHandler]:
    """Every connector job kind, to merge into a `Worker`'s handlers."""
    handlers: dict[str, JobHandler] = dict(engine.handlers(queue))
    if events is not None and engine.registry.find("klaviyo") is not None:
        from autonomy.channels.klaviyo import klaviyo_handlers

        handlers.update(klaviyo_handlers(engine, events, queue))
    if privacy_desk is not None:
        from autonomy.connectors.shopify import privacy_handlers

        handlers.update(privacy_handlers(privacy_desk, queue))
    return handlers


def _klaviyo_connection(engine: ConnectorEngine, business_id: str) -> Connection | None:
    """The business's Klaviyo connection to send through: the active one, else none (oldest first)."""
    if engine.registry.find("klaviyo") is None:
        return None
    found = [
        c
        for c in engine.store.connections(business_id=business_id, kind="klaviyo")
        if c.status is ConnectionStatus.ACTIVE
    ]
    found.sort(key=lambda c: (c.created_at, c.connection_id))
    return found[0] if found else None


def send_adapters(
    engine: ConnectorEngine,
    business_id: str,
    *,
    fallback: Mapping[Channel, ChannelAdapter] | None = None,
    details: DecisionDetails | None = None,
) -> dict[Channel, ChannelAdapter]:
    """One adapter per channel for a plain `Dispatcher`: Klaviyo where it is connected, else `fallback`."""
    adapters: dict[Channel, ChannelAdapter] = dict(fallback or {})
    connection = _klaviyo_connection(engine, business_id)
    if connection is not None:
        from autonomy.channels.klaviyo import klaviyo_adapters

        adapters.update(klaviyo_adapters(engine, connection, details=details))
    return adapters


def send_providers(
    engine: ConnectorEngine,
    business_id: str,
    *,
    fallback: Mapping[Channel, ChannelAdapter] | None = None,
    details: DecisionDetails | None = None,
) -> dict[Channel, Sequence[Provider]]:
    """Providers per channel for `DispatchGateway`: the customer's Klaviyo first, our own adapter second.

    The fallback is named after its channel (as `autonomy.runtime.settings.build_gateway` names them, so
    its webhooks still route). Klaviyo's definite refusals (`not_ready:`, `needs_reconnect:`,
    `rate_limited:`) fail over to it; `ambiguous:` never does. Pair it with
    `RateLimiter(provider_rates=autonomy.channels.klaviyo.provider_rates())`."""
    from autonomy.channels.gateway import Provider

    providers: dict[Channel, list[Provider]] = {}
    connection = _klaviyo_connection(engine, business_id)
    if connection is not None:
        from autonomy.channels.klaviyo import PROVIDER, klaviyo_adapters

        for channel, adapter in klaviyo_adapters(engine, connection, details=details).items():
            providers.setdefault(channel, []).append(Provider(PROVIDER, adapter))
    for channel, adapter in (fallback or {}).items():
        providers.setdefault(channel, []).append(Provider(channel.value, adapter))
    return {channel: tuple(chain) for channel, chain in providers.items()}
