"""The sync engine end to end against the fake platform: connect, backfill, live, resume, fail well."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from autonomy.connectors import (
    ConnectionStatus,
    Lane,
    Stream,
    sync_job,
)
from autonomy.connectors.sync import DAILY_KIND, SYNC_KIND, daily_job, slot_after
from autonomy.contracts import EventType, JobStatus
from autonomy.runtime.worker import Worker

from .base_support import T0, FakePlatform, Rig, connect, iso, make_rig


def at(rig: Rig, now: datetime) -> datetime:
    rig.clock.current = now
    return now


def shop_with_history(n_days: int = 27) -> FakePlatform:
    platform = FakePlatform(page_size=3)
    for day in range(1, n_days + 1, 2):
        created = T0 - timedelta(days=day)
        platform.add_order(f"o{day:03d}", customer=f"c{day % 5}", total=10.0 + day, created=created)
    platform.add_order("o_now", customer="c1", total=99.0, created=T0 - timedelta(minutes=30))
    return platform


def order_events(rig: Rig) -> list[str]:
    return sorted(e.attrs.get("id", e.event_id) for e in rig.events.scan() if e.type is EventType.PURCHASE)


def worker(rig: Rig) -> Worker:
    return Worker(queue=rig.queue, handlers=rig.engine.handlers(rig.queue), name="w1")


def test_connect_identifies_the_account_and_seeds_every_chain() -> None:
    rig = make_rig()
    connection = connect(rig)
    assert connection.external_id == "acct_9" and connection.label == "Demo Shop"
    assert connection.status is ConnectionStatus.ACTIVE
    cid = connection.connection_id
    for stream, lane in (
        (Stream.ORDERS, Lane.LIVE),
        (Stream.ORDERS, Lane.HISTORY),
        (Stream.CUSTOMERS, Lane.LIVE),
    ):
        job = rig.queue.get(sync_job(cid, stream, lane, T0).job_id)
        assert job is not None and job.status is JobStatus.QUEUED
    assert rig.queue.get(sync_job(cid, Stream.CUSTOMERS, Lane.HISTORY, T0).job_id) is None  # no history depth
    assert rig.queue.get(daily_job(cid, T0).job_id) is not None
    # the same account connected again is the same connection
    assert connect(rig).connection_id == cid


def test_worker_backfills_newest_first_in_chunks_and_stores_each_order_once() -> None:
    platform = shop_with_history()
    rig = make_rig(platform)
    connection = connect(rig)
    outcomes = worker(rig).drain(T0)
    assert outcomes and all(o.status == "done" for o in outcomes)
    purchases = [e for e in rig.events.scan() if e.type is EventType.PURCHASE]
    assert len(purchases) == len(platform.orders)  # live and history overlap; ids dedupe
    assert {e.source for e in purchases} == {"demo"}
    history = rig.store.position(connection.connection_id, Stream.ORDERS, Lane.HISTORY)
    assert history.done and history.reached == T0 - timedelta(days=28)
    # history windows were read newest first, one chunk per job
    ends = [
        r.url.params["updated_max"]
        for r in platform.calls
        if r.url.path == "/api/orders"
        and "cursor" not in r.url.params
        and r.url.params.get("updated_min", "") < iso(T0 - timedelta(hours=2))
    ]
    assert ends == sorted(ends, reverse=True)
    assert sum(rig.history_calls) > 0  # the runtime was told history landed (it rebuilds state)
    live = rig.store.position(connection.connection_id, Stream.ORDERS, Lane.LIVE)
    assert live.high_watermark == T0 and live.last_success_at == T0
    # the live chain continues on the next hourly slot; the history chain has ended
    nxt = rig.queue.get(
        sync_job(
            connection.connection_id, Stream.ORDERS, Lane.LIVE, slot_after(T0, timedelta(hours=1))
        ).job_id
    )
    assert nxt is not None and nxt.status is JobStatus.QUEUED


def test_incremental_reads_from_the_watermark_minus_the_overlap_and_dedupes() -> None:
    platform = shop_with_history(3)
    rig = make_rig(platform)
    connection = connect(rig)
    worker(rig).drain(T0)
    before = rig.events.count()
    later = at(rig, T0 + timedelta(hours=1))
    platform.add_order("o_new", customer="c9", total=5.0, created=T0 + timedelta(minutes=40))
    platform.orders["o001"]["updated_at"] = iso(T0 + timedelta(minutes=45))  # an edit: same purchase
    platform.orders["o001"]["note"] = "gift wrap"
    calls = len(platform.calls)
    report = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, later)
    first = next(r for r in platform.calls[calls:] if r.url.path == "/api/orders")
    assert first.url.params["updated_min"] == iso(T0 - timedelta(hours=2))
    assert report.status == "ok" and report.new_records == 2  # o_new, and the edited o001
    assert rig.events.count() == before + 1  # the edit re-maps to the same purchase event id
    again = rig.engine.sync(
        connection.connection_id, Stream.ORDERS, Lane.LIVE, at(rig, later + timedelta(minutes=5))
    )
    assert again.rows >= 2 and again.new_records == 0 and again.events == 0


def test_a_failure_mid_window_resumes_at_the_saved_cursor() -> None:
    platform = FakePlatform(page_size=2)
    for i in range(5):
        platform.add_order(f"r{i}", customer="c1", total=1.0 + i, created=T0 - timedelta(minutes=50 - i))
    rig = make_rig(platform)
    connection = connect(rig, seed=False)
    cid = connection.connection_id
    first = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0, max_pages=1)
    assert first.status == "more" and first.rows == 2
    platform.failures = [httpx.Response(503)] * 5
    failed = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0)
    assert failed.status == "retry" and failed.error_code == "connector.transient"
    position = rig.store.position(cid, Stream.ORDERS, Lane.LIVE)
    assert position.cursor == "c:2" and position.window_end == T0 and position.error_streak == 1
    later = at(rig, failed.retry_at or T0)
    done = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, later)
    assert done.status == "ok"
    resumed = [r for r in platform.calls if r.url.path == "/api/orders"][-3]
    assert resumed.url.params["cursor"] == "c:2"
    assert len([e for e in rig.events.scan() if e.type is EventType.PURCHASE]) == 5
    assert rig.store.position(cid, Stream.ORDERS, Lane.LIVE).error_streak == 0


def test_a_long_rate_limit_parks_the_job_until_retry_after() -> None:
    platform = FakePlatform()
    rig = make_rig(platform)
    connection = connect(rig, seed=False)
    platform.failures = [httpx.Response(429, headers={"Retry-After": "600"})]
    report = rig.engine.run_and_reschedule(connection.connection_id, Stream.ORDERS, Lane.LIVE, T0, rig.queue)
    assert report.status == "retry" and report.retry_at == T0 + timedelta(seconds=600)
    assert report.error_code == "connector.rate_limited"
    assert rig.queue.claim([SYNC_KIND], worker="t", now=T0 + timedelta(seconds=599)) is None
    parked = rig.queue.claim([SYNC_KIND], worker="t", now=T0 + timedelta(seconds=600))
    assert parked is not None and parked.payload["lane"] == "live" and rig.clock.slept == []


def test_an_expired_cursor_restarts_its_window() -> None:
    platform = FakePlatform(page_size=2)
    for i in range(5):
        platform.add_order(f"x{i}", customer="c2", total=2.0, created=T0 - timedelta(minutes=40 - i))
    rig = make_rig(platform)
    cid = connect(rig, seed=False).connection_id
    rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0, max_pages=1)
    platform.expired_cursors = {"c:2"}
    report = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0)
    assert report.status == "ok"
    assert len([e for e in rig.events.scan() if e.type is EventType.PURCHASE]) == 5


def test_schema_drift_is_counted_and_unknown_fields_are_ignored() -> None:
    platform = FakePlatform(page_size=50)
    for i in range(12):
        platform.add_order(
            f"d{i}", customer="c3", total=3.0, created=T0 - timedelta(minutes=30), surprise_field={"x": 1}
        )
        if i < 8:
            del platform.orders[f"d{i}"]["total"]  # renamed upstream without a version bump
    rig = make_rig(platform)
    cid = connect(rig, seed=False).connection_id
    report = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0)
    assert report.status == "ok" and report.rows == 12
    assert report.missing_required == 8 and report.problems == 8 and report.events == 4
    drift = [a for a in rig.alerts.list_alerts() if a.code == "connector.schema_drift"]
    assert len(drift) == 1
    rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, at(rig, T0 + timedelta(minutes=10)))
    assert len([a for a in rig.alerts.list_alerts() if a.code == "connector.schema_drift"]) == 1


def test_revoked_access_stops_the_connection_with_one_alert_and_reconnect_resumes() -> None:
    platform = shop_with_history(3)
    rig = make_rig(platform)
    connection = connect(rig)
    cid = connection.connection_id
    worker(rig).drain(T0)
    platform.revoked = True
    later = at(rig, T0 + timedelta(hours=1))
    report = rig.engine.run_and_reschedule(cid, Stream.ORDERS, Lane.LIVE, later, rig.queue)
    assert report.status == "reconnect"
    assert rig.store.get_connection(cid).status is ConnectionStatus.NEEDS_RECONNECT  # type: ignore[union-attr]
    for _ in range(3):  # every other job, and every later check, stays quiet
        rig.engine.run_and_reschedule(cid, Stream.CUSTOMERS, Lane.LIVE, later, rig.queue)
        rig.engine.daily(cid, later, rig.queue)
    alerts = rig.alerts.list_alerts()
    assert [a.code for a in alerts] == ["connector.needs_reconnect"]
    verdict = rig.engine.monitor.ready_to_act("biz_1", later)
    assert not verdict.can_act and verdict.reasons == ("Demo Shop needs reconnecting",)
    # a person reconnects: same connection, positions kept, the incident closes
    platform.revoked = False
    again = connect(rig, at(rig, T0 + timedelta(hours=2)))
    assert again.connection_id == cid and again.status is ConnectionStatus.ACTIVE
    assert again.created_at == connection.created_at
    assert rig.store.open_incidents(cid) == []
    assert rig.store.position(cid, Stream.ORDERS, Lane.HISTORY).done


def test_webhook_catch_up_requests_collapse_to_one_poll_per_hour() -> None:
    rig = make_rig()
    cid = connect(rig, seed=False).connection_id
    jobs = {
        rig.engine.request_catch_up(cid, Stream.ORDERS, T0 + timedelta(minutes=m), rig.queue).job_id
        for m in (1, 7, 59)
    }
    assert len(jobs) == 1
    job = rig.queue.get(next(iter(jobs)))
    assert job is not None and job.run_at == T0 + timedelta(hours=1) and job.kind == SYNC_KIND


def test_one_syncer_per_stream() -> None:
    rig = make_rig()
    cid = connect(rig, seed=False).connection_id
    assert rig.store.acquire_lease(f"sync:{cid}:orders:live", "w2", T0, timedelta(minutes=15))
    report = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0)
    assert report.status == "skipped" and "another worker" in (report.error or "")


def test_daily_job_renews_webhooks_and_keeps_idle_tokens_alive() -> None:
    platform = FakePlatform()
    rig = make_rig(platform)
    connection = connect(rig)
    month = at(rig, T0 + timedelta(days=31))
    refreshes = sum(1 for c in platform.token_calls if c["grant_type"] == "refresh_token")
    rig.engine.daily(connection.connection_id, month, rig.queue)
    assert platform.webhooks == {"orders/updated", "app/uninstalled", "customers/redact"}
    registered = next(r for r in platform.calls if r.url.path == "/api/webhooks")
    assert registered.headers["idempotency-key"] == "reg"
    assert sum(1 for c in platform.token_calls if c["grant_type"] == "refresh_token") == refreshes + 1
    # the worker's daily chain re-queues itself
    w = worker(rig)
    w.drain(month)
    assert (
        rig.queue.get(daily_job(connection.connection_id, slot_after(month, timedelta(days=1))).job_id)
        is not None
    )
    assert any(o.kind == DAILY_KIND for o in w.outcomes)


def test_a_disconnected_connection_ends_its_chains() -> None:
    rig = make_rig()
    cid = connect(rig).connection_id
    rig.engine.disconnect(cid, T0)
    outcomes = worker(rig).drain(T0)
    assert all(o.status == "done" for o in outcomes)
    assert rig.events.count() == 0
    assert rig.engine.tokens.vault.load(cid) is None


def test_a_bad_key_at_connect_time_is_an_error_not_an_alert() -> None:
    import pytest

    from autonomy.connectors import NeedsReconnect

    rig = make_rig()
    with pytest.raises(NeedsReconnect):
        rig.engine.connect_api_key("demo", business_id="biz_1", api_key="wrong", now=T0)
    assert rig.store.connections() == [] and rig.alerts.list_alerts() == []


def test_a_mistyped_key_for_a_connected_account_leaves_the_live_connection_alone() -> None:
    """The provisional id of a second attempt is the live connection's own; identify runs with the new
    key unsaved, so a typo never replaces or deletes the working credentials."""
    import pytest

    from autonomy.connectors import NeedsReconnect

    rig = make_rig()
    live = rig.engine.connect_api_key(
        "demo", business_id="biz_1", api_key="tok_1", now=T0, config={"account_id": "acct_9"}
    )
    with pytest.raises(NeedsReconnect):
        rig.engine.connect_api_key(
            "demo", business_id="biz_1", api_key="wrong", now=T0, config={"account_id": "acct_9"}
        )
    kept = rig.engine.tokens.vault.load(live.connection_id)
    assert kept is not None and kept.access_token == "tok_1"
    stored = rig.store.get_connection(live.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.ACTIVE
    assert rig.alerts.list_alerts() == []


def test_a_failure_after_stored_pages_keeps_their_progress() -> None:
    """A 429/5xx after page N of a run must not roll the position back to where the run began:
    a lane that is throttled every run would otherwise never move."""
    platform = FakePlatform(page_size=2)
    for i in range(9):
        platform.add_order(f"p{i}", customer="c1", total=1.0, created=T0 - timedelta(minutes=50 - i))
    rig = make_rig(platform)
    cid = connect(rig, seed=False).connection_id
    served = platform.handle
    pages = {"n": 0, "down": True}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/orders":
            pages["n"] += 1
            if pages["n"] >= 3 and pages["down"]:  # from the third page on, the platform is down
                return httpx.Response(503)
        return served(request)

    rig.engine.transport_for = lambda base: httpx.MockTransport(flaky)
    first = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, T0)
    assert first.status == "retry" and first.rows == 4
    position = rig.store.position(cid, Stream.ORDERS, Lane.LIVE)
    assert position.cursor == "c:4" and position.window_end == T0 and position.error_streak == 1
    pages["down"] = False  # the platform recovers
    done = rig.engine.sync(cid, Stream.ORDERS, Lane.LIVE, at(rig, first.retry_at or T0))
    assert done.status == "ok"
    assert len([e for e in rig.events.scan() if e.type is EventType.PURCHASE]) == 9
