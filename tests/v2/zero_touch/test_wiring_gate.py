"""The runtime knows its business, holds it on stale or reconnect-needed data with one alert, never
blocks on a connection a person disconnected, and a disconnect takes our setup out of the platform."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx

from autonomy.connectors import ConnectionStatus, NeedsReconnect
from autonomy.connectors.health import ALERT_DISCONNECT
from autonomy.connectors.sync import DISCONNECTED_BY_PERSON
from autonomy.contracts import EventType
from autonomy.runtime.service import DataHold
from autonomy.runtime.worker import GATEWAY_KIND
from tests.v2.zero_touch.wiring_support import BUSINESS, SWEEP, T0, Shop, make_shop

CUSTOMERS = ("101", "102")


def codes(shop: Shop) -> list[str]:
    """The alerts a person must act on (warnings and critical); `info` notes go to the briefing."""
    return sorted(a.code for a in shop.alerts.list_alerts() if a.severity != "info")


def test_the_runtime_runs_for_its_business_and_decides_on_fresh_data() -> None:
    shop = make_shop()
    assert shop.runtime.business_id == BUSINESS
    connection = shop.connect_klaviyo()
    shop.add_customers(CUSTOMERS)
    shop.fresh(connection, SWEEP)
    report = shop.runtime.tick(shop.at(SWEEP))
    assert report.data_held == [] and len(report.decisions) == len(CUSTOMERS)


def test_stale_data_holds_every_decision_with_one_alert_and_the_sweep_runs_once_it_catches_up() -> None:
    shop = make_shop()
    connection = shop.connect_klaviyo()
    shop.add_customers(CUSTOMERS)
    shop.fresh(connection, T0 - timedelta(hours=8))  # last synced 10 h before the sweep: stale
    for minutes in (0, 5, 30):
        report = shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=minutes)))
        assert report.decisions == [] and report.data_held
    assert "consent, engagement is out of date" in report.data_held[0]
    assert codes(shop) == ["connector.stale"]  # one alert for the hold, however many ticks and streams
    shop.fresh(connection, SWEEP + timedelta(hours=1))
    report = shop.runtime.tick(shop.at(SWEEP + timedelta(hours=1)))
    assert report.data_held == [] and len(report.decisions) == len(CUSTOMERS)


def test_a_connection_that_needs_reconnecting_holds_with_its_one_alert_until_reconnected_or_removed() -> None:
    shop = make_shop()
    connection = shop.connect_klaviyo()
    shop.add_customers(CUSTOMERS)
    shop.fresh(connection, SWEEP)
    assert shop.engine.tokens.on_revoked is not None
    shop.engine.tokens.on_revoked(connection, NeedsReconnect("the key was deleted"), SWEEP)
    for minutes in (0, 10):
        report = shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=minutes)))
        assert report.decisions == [] and report.data_held == [f"{connection.label} needs reconnecting"]
    assert codes(shop) == ["connector.needs_reconnect"]
    # The person decides to stop using it: disconnecting is intentional and stops holding at once.
    shop.engine.disconnect(connection.connection_id, shop.at(SWEEP + timedelta(minutes=20)))
    stored = shop.engine.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status is ConnectionStatus.DISCONNECTED
    assert stored.status_reason == DISCONNECTED_BY_PERSON
    assert shop.engine.store.open_incidents(connection.connection_id) == []
    report = shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=30)))
    assert report.data_held == [] and len(report.decisions) == len(CUSTOMERS)
    assert codes(shop) == ["connector.needs_reconnect"]  # nothing new to read


def test_disconnecting_klaviyo_deletes_our_flows_and_template_and_names_the_key_left_to_delete() -> None:
    shop = make_shop()
    connection = shop.connect_klaviyo()
    assert shop.klaviyo.flows and shop.klaviyo.templates
    result = shop.engine.disconnect(connection.connection_id, shop.at(SWEEP))
    assert shop.klaviyo.flows == {} and shop.klaviyo.templates == {}
    assert sorted(p.split("/")[2] for p in shop.klaviyo.deleted) == ["flows", "templates"]
    assert any("private API key" in step for step in result.left)
    followups = [a for a in shop.alerts.list_alerts() if a.code == ALERT_DISCONNECT]
    assert len(followups) == 1 and followups[0].severity == "info"
    assert shop.engine.tokens.vault.load(connection.connection_id) is None


def test_disconnecting_an_oauth_klaviyo_revokes_the_grant_and_leaves_nothing_to_do() -> None:
    from autonomy.channels.klaviyo import ensure_klaviyo_setup

    shop = make_shop()
    now = shop.clock.now()
    url = shop.engine.begin_oauth(
        "klaviyo", business_id=BUSINESS, redirect_uri="https://autonomy.example/cb", now=now
    )
    query = parse_qs(url.split("?", 1)[1])
    shop.klaviyo.expected_challenge = query["code_challenge"][0]
    connection = shop.engine.finish_oauth(
        "klaviyo", {"state": query["state"][0], "code": "code_ok"}, now, None
    )
    ensure_klaviyo_setup(shop.engine, connection.connection_id, now)
    refresh = shop.klaviyo.refresh_token
    result = shop.engine.disconnect(connection.connection_id, shop.at(SWEEP))
    assert shop.klaviyo.revoked_tokens == [refresh]
    assert result.left == [] and result.alert is None
    revoke = next(r for r in shop.klaviyo.calls if r.url.path == "/oauth/revoke")
    assert revoke.headers["authorization"].startswith("Basic ")


def test_disconnecting_shopify_uninstalls_our_app_and_its_uninstall_notice_raises_nothing() -> None:
    shop = make_shop()
    connection = shop.connect_shopify_oauth()
    result = shop.engine.disconnect(connection.connection_id, shop.at(SWEEP))
    assert shop.shopify.revoked and result.left == []
    assert any("Revoked" in step for step in result.done)
    stored = shop.engine.store.get_connection(connection.connection_id)
    assert stored is not None
    assert shop.engine.uninstalled(stored, SWEEP + timedelta(minutes=1)) is None  # app/uninstalled follows
    stored = shop.engine.store.get_connection(connection.connection_id)
    assert stored is not None and stored.status_reason == DISCONNECTED_BY_PERSON
    assert codes(shop) == []


def test_a_queued_send_waits_while_the_data_is_stale_and_goes_once_it_is_fresh() -> None:
    shop = make_shop()
    connection = shop.connect_klaviyo()
    shop.add_customers(CUSTOMERS[:1])
    shop.fresh(connection, SWEEP)
    shop.klaviyo.failures.extend([httpx.ReadTimeout("lost")] * 12)  # the first hand-off is ambiguous
    report = shop.runtime.tick(shop.at(SWEEP))
    assert report.dispatch.queued and not report.sent
    shop.klaviyo.failures.clear()
    # The data goes stale before the retry is due: the retry is held (re-queued), not sent.
    later = SWEEP + timedelta(hours=4)
    report = shop.runtime.tick(shop.at(later))
    assert report.data_held and not report.sent
    held = [r for r in report.dispatch.results if r.reason.startswith("data_not_fresh")]
    assert held and all(r.status.value == "queued" for r in held)
    assert not [e for e in shop.runtime.events.scan() if e.type is EventType.SEND]
    # Fresh again: the held send goes out, once.
    shop.fresh(connection, later + timedelta(minutes=45))
    shop.runtime.tick(shop.at(later + timedelta(minutes=45)))
    sends = [e for e in shop.runtime.events.scan() if e.type is EventType.SEND]
    assert len(sends) == 1
    assert GATEWAY_KIND == "gateway_send_decision"


def test_queued_sends_reuse_the_minute_s_data_check_instead_of_one_check_each() -> None:
    shop = make_shop()
    connection = shop.connect_klaviyo()
    shop.fresh(connection, SWEEP)
    inner = shop.runtime.data_gate
    assert inner is not None
    calls: list[object] = []

    def counted(now: datetime) -> DataHold | None:
        calls.append(now)
        return inner(now)

    shop.runtime.data_gate = counted
    decision = object()
    for second in range(0, 60, 5):  # a sweep sending twelve queued messages within one minute
        shop.runtime.data_send_gate(decision, SWEEP + timedelta(seconds=second))  # type: ignore[arg-type]
    assert len(calls) == 1
    shop.runtime.data_send_gate(decision, SWEEP + timedelta(minutes=1))  # type: ignore[arg-type]
    assert len(calls) == 2
