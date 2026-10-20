"""The wiring that makes the connectors one system: registry, jobs after connect, worker handlers, providers."""

from __future__ import annotations

from autonomy.channels.klaviyo import PROVIDER, WATCH_JOB_KIND, KlaviyoChannelAdapter, watch_job
from autonomy.connectors import (
    DAILY_KIND,
    SYNC_KIND,
    WEBHOOK_KIND,
    ConnectionStatus,
    after_connect,
    production_registry,
    send_adapters,
    send_providers,
    worker_handlers,
)
from autonomy.connectors.klaviyo import KlaviyoConnector
from autonomy.connectors.shopify import PRIVACY_JOB_KIND, ShopifyPrivacyDesk, privacy_job
from autonomy.contracts import Channel, JobStatus
from autonomy.fakes.channels import FakeChannelAdapter
from autonomy.runtime.worker import connector_handlers
from autonomy.store import SqlEventStore, memory_engine

from .klaviyo_support import connect_key, make_klaviyo_rig
from .shopify_support import connect_token, make_shopify_rig


def test_the_production_registry_links_klaviyo_engagement_to_hand_offs() -> None:
    events = SqlEventStore(memory_engine())
    registry = production_registry(events)
    assert registry.kinds() == ("shopify", "klaviyo")
    klaviyo = registry.get("klaviyo")
    assert isinstance(klaviyo, KlaviyoConnector) and klaviyo.handoffs is not None
    # without an event store it is the plain built-in registry (the watch then holds, never guesses)
    plain = production_registry().get("klaviyo")
    assert isinstance(plain, KlaviyoConnector) and plain.handoffs is None


def test_a_klaviyo_connect_starts_its_hourly_watch_once() -> None:
    rig = make_klaviyo_rig()
    connection = connect_key(rig)
    now = rig.clock.now()
    jobs = after_connect(connection, rig.queue, now)
    assert [j.kind for j in jobs] == [WATCH_JOB_KIND]
    queued = rig.queue.get(watch_job(connection.connection_id, now).job_id)
    assert queued is not None and queued.status is JobStatus.QUEUED
    after_connect(connection, rig.queue, now)  # again: same job id, nothing new
    assert queued.job_id == jobs[0].job_id
    inactive = connection.model_copy(update={"status": ConnectionStatus.NEEDS_RECONNECT})
    assert after_connect(inactive, rig.queue, now) == []


def test_a_shopify_connect_starts_the_privacy_chain() -> None:
    rig, _shop = make_shopify_rig()
    connection = connect_token(rig)
    now = rig.clock.now()
    jobs = after_connect(connection, rig.queue, now)
    assert [j.kind for j in jobs] == [PRIVACY_JOB_KIND]
    assert rig.queue.get(privacy_job(now).job_id) is not None


def test_the_worker_runs_every_connector_job_kind() -> None:
    rig = make_klaviyo_rig()
    base = worker_handlers(rig.engine, rig.queue)
    assert {SYNC_KIND, WEBHOOK_KIND, DAILY_KIND} <= set(base) and WATCH_JOB_KIND not in base
    with_watch = worker_handlers(rig.engine, rig.queue, events=rig.events)
    assert WATCH_JOB_KIND in with_watch
    desk = ShopifyPrivacyDesk(rig.store, rig.store.engine)
    every = connector_handlers(rig.engine, rig.queue, events=rig.events, privacy_desk=desk)
    assert {SYNC_KIND, WEBHOOK_KIND, DAILY_KIND, WATCH_JOB_KIND, PRIVACY_JOB_KIND} <= set(every)


def test_sending_goes_through_the_customers_klaviyo_with_our_own_email_behind_it() -> None:
    rig = make_klaviyo_rig()
    ses = FakeChannelAdapter(Channel.EMAIL)
    whatsapp = FakeChannelAdapter(Channel.WHATSAPP)
    fallback = {Channel.EMAIL: ses, Channel.WHATSAPP: whatsapp}

    # before Klaviyo is connected: our own adapters only
    before = send_providers(rig.engine, "biz_1", fallback=fallback)
    assert [p.name for p in before[Channel.EMAIL]] == ["email"]

    connection = connect_key(rig, sms="on")
    providers = send_providers(rig.engine, "biz_1", fallback=fallback)
    assert [p.name for p in providers[Channel.EMAIL]] == [PROVIDER, "email"]
    assert providers[Channel.EMAIL][1].adapter is ses
    assert [p.name for p in providers[Channel.SMS]] == [PROVIDER]
    assert [p.name for p in providers[Channel.WHATSAPP]] == ["whatsapp"]
    first = providers[Channel.EMAIL][0].adapter
    assert isinstance(first, KlaviyoChannelAdapter) and first.connection_id == connection.connection_id

    # a plain Dispatcher gets one adapter per channel: Klaviyo where connected
    adapters = send_adapters(rig.engine, "biz_1", fallback=fallback)
    assert isinstance(adapters[Channel.EMAIL], KlaviyoChannelAdapter)
    assert adapters[Channel.WHATSAPP] is whatsapp

    # another business, or a Klaviyo that needs reconnecting, falls back to our own sending
    assert [p.name for p in send_providers(rig.engine, "biz_2", fallback=fallback)[Channel.EMAIL]] == [
        "email"
    ]
    rig.store.set_status(
        connection.connection_id, ConnectionStatus.NEEDS_RECONNECT, "revoked", rig.clock.now()
    )
    assert send_adapters(rig.engine, "biz_1", fallback=fallback)[Channel.EMAIL] is ses
