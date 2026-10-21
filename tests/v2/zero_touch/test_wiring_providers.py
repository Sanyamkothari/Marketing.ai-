"""Klaviyo is the default send provider in the production runtime, SES its fallback, and the
providers follow the connections at every tick with no restart and no double send."""

from __future__ import annotations

from datetime import timedelta

import httpx

from autonomy.channels.klaviyo import PROVIDER, provider_rates
from autonomy.connectors import ConnectionStatus
from autonomy.contracts import Channel, EventType
from tests.v2.zero_touch.wiring_support import SWEEP, make_shop

CUSTOMERS = ("101", "102", "103")


def names(shop: object) -> tuple[str, ...]:
    gateway = shop.runtime.gateway  # type: ignore[attr-defined]
    assert gateway is not None
    return tuple(p.name for p in gateway.providers.get(Channel.EMAIL, ()))


def test_klaviyo_sends_first_with_our_ses_behind_it_paced_by_klaviyos_limits() -> None:
    shop = make_shop(hold=False)
    shop.connect_klaviyo()
    shop.add_customers(CUSTOMERS)
    shop.runtime.tick(shop.at(SWEEP))  # the start-of-tick refresh sees the connection
    assert names(shop) == (PROVIDER, "email")
    gateway = shop.runtime.gateway
    assert gateway is not None and gateway.limiter.provider_rates == provider_rates()
    report = shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=5)))
    first = shop.runtime.decisions.scan()
    handed = [e for e in shop.klaviyo.created_events if e["properties"].get("decision_id")]
    assert len(handed) == len(CUSTOMERS) and shop.ses.sent == []  # the customer's own Klaviyo sent them
    sends = [e for e in shop.runtime.events.scan() if e.type is EventType.SEND]
    assert {str(e.attrs["provider_message_id"]).split(":")[0] for e in sends} == {PROVIDER}
    assert report.data_held == [] and list(first)


def test_the_providers_follow_the_connection_made_lost_and_remade_without_a_restart() -> None:
    shop = make_shop(hold=False)
    shop.runtime.tick(shop.at(SWEEP - timedelta(hours=2)))
    assert names(shop) == ("email",)  # nothing connected: our own SES sends alone
    connection = shop.connect_klaviyo()
    shop.runtime.tick(shop.at(SWEEP - timedelta(hours=1)))
    assert names(shop) == (PROVIDER, "email")
    shop.engine.store.set_status(
        connection.connection_id, ConnectionStatus.NEEDS_RECONNECT, "revoked", shop.clock.now()
    )
    shop.runtime.tick(shop.at(SWEEP - timedelta(minutes=30)))
    assert names(shop) == ("email",)
    again = shop.connect_klaviyo()  # the reconnect reuses the connection id
    assert again.connection_id == connection.connection_id
    shop.runtime.tick(shop.at(SWEEP - timedelta(minutes=20)))
    assert names(shop) == (PROVIDER, "email")
    assert shop.wiring.routing is not None and shop.wiring.routing.changes == 3
    # A setting that turns Klaviyo sending off takes it out of the chain as well.
    shop.engine.store.put_connection(again.model_copy(update={"config": {"send": "off"}}))
    shop.runtime.tick(shop.at(SWEEP - timedelta(minutes=10)))
    assert names(shop) == ("email",)


def test_a_hand_off_that_may_have_reached_klaviyo_never_goes_out_again_through_ses() -> None:
    shop = make_shop(hold=False)
    connection = shop.connect_klaviyo()
    shop.add_customers(CUSTOMERS[:1])
    shop.runtime.tick(shop.at(SWEEP - timedelta(minutes=5)))
    # Klaviyo takes the POST but the answer never comes back, on every retry of the base client.
    shop.klaviyo.failures.extend([httpx.ReadTimeout("lost")] * 12)
    report = shop.runtime.tick(shop.at(SWEEP))
    assert report.dispatch.queued and not report.sent  # retried later, on Klaviyo only
    assert shop.ses.sent == []
    # Klaviyo's access goes before the retry: the providers drop Klaviyo, the pin keeps the decision off SES.
    shop.klaviyo.failures.clear()
    shop.engine.store.set_status(
        connection.connection_id, ConnectionStatus.NEEDS_RECONNECT, "revoked", shop.clock.now()
    )
    for minutes in (10, 30, 60):
        shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=minutes)))
    assert names(shop) == ("email",)
    assert shop.ses.sent == []
    # Reconnected: the same decision is handed to Klaviyo again with the same unique_id, recorded once.
    shop.connect_klaviyo()
    for minutes in (120, 180, 300):
        shop.runtime.tick(shop.at(SWEEP + timedelta(minutes=minutes)))
    assert shop.ses.sent == []
    handed = [e for e in shop.klaviyo.created_events if e["properties"].get("decision_id")]
    assert len(handed) == 1
    sends = [e for e in shop.runtime.events.scan() if e.type is EventType.SEND]
    assert len(sends) == 1
