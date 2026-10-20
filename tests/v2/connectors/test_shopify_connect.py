"""Shopify connect: install-link and callback HMAC, shop validation, expiring offline tokens, custom-app tokens."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from autonomy.connectors import AuthorizationError, ConnectionStatus, Lane, NeedsReconnect, Stream
from autonomy.connectors.shopify import SCOPES, ShopifyConnector, normalize_shop
from autonomy.connectors.shopify.api import SHOP_PATTERN

from .shopify_support import (
    APP,
    CALLBACK,
    CLIENT_ID,
    GQL_PATH,
    SHOP,
    T0,
    connect_oauth,
    connect_token,
    make_shopify_rig,
    queued,
    sign_query,
)


def test_shop_addresses_are_normalised_and_custom_domains_refused() -> None:
    assert normalize_shop("Acme") == SHOP
    assert normalize_shop("https://ACME.myshopify.com/admin/products") == SHOP
    assert normalize_shop("acme.myshopify.com") == SHOP
    for bad in ("acme.com", "evil.example/acme.myshopify.com", "acme.myshopify.com.evil.example", "", "-x"):
        assert normalize_shop(bad) is None, bad
    assert SHOP_PATTERN.endswith(r"\.myshopify\.com")


def test_an_install_link_from_the_shopify_admin_is_checked_before_oauth_starts() -> None:
    connector = ShopifyConnector()
    stamp = str(int(T0.timestamp()))
    good = sign_query({"shop": SHOP, "timestamp": stamp, "host": "YWNtZQ"})
    assert connector.validate_install(good, APP, T0 + timedelta(seconds=30)) == SHOP
    with pytest.raises(AuthorizationError):
        connector.validate_install(
            {**good, "shop": "other.myshopify.com"}, APP, T0
        )  # signature no longer matches
    with pytest.raises(AuthorizationError):
        connector.validate_install(sign_query({"shop": "acme.com", "timestamp": stamp}), APP, T0)
    with pytest.raises(AuthorizationError):
        connector.validate_install(good, APP, T0 + timedelta(days=2))  # an old link


def test_oauth_install_uses_minimal_scopes_expiring_offline_tokens_and_the_shop_identity() -> None:
    rig, fake = make_shopify_rig()
    url = rig.engine.begin_oauth(
        "shopify", business_id="biz_1", redirect_uri=CALLBACK, now=T0, params={"shop": SHOP}
    )
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (parts.scheme, parts.netloc, parts.path) == ("https", SHOP, "/admin/oauth/authorize")
    assert query["client_id"] == CLIENT_ID and query["redirect_uri"] == CALLBACK
    assert query["scope"] == ",".join(SCOPES)
    assert (
        "expiring" not in query and "grant_options[]" not in query
    )  # offline; expiring goes in the exchange
    with pytest.raises(AuthorizationError):
        rig.engine.begin_oauth(
            "shopify", business_id="b", redirect_uri=CALLBACK, now=T0, params={"shop": "evil.com"}
        )

    connection = connect_oauth(rig, fake)
    exchange = fake.token_calls[-1]
    assert exchange["grant_type"] == "authorization_code" and exchange["expiring"] == "1"
    assert exchange["client_secret"] == "shp_secret_1"  # client credentials in the body, not Basic
    assert connection.external_id == SHOP and connection.label == "Acme Goods"
    tokens = rig.engine.tokens.vault.load(connection.connection_id)
    assert tokens is not None and tokens.refresh_token == fake.refresh_token and tokens.expires_at is not None
    assert all(r.headers.get("x-shopify-access-token") for r in fake.calls if r.url.path == GQL_PATH)
    assert all(r.headers.get("authorization") is None for r in fake.calls if r.url.path == GQL_PATH)
    assert {j.payload["stream"] for j in queued(rig, "connector_sync")} == {
        "orders",
        "customers",
        "products",
    }


def test_a_callback_with_a_bad_signature_is_refused_before_the_code_is_used() -> None:
    rig, fake = make_shopify_rig()
    url = rig.engine.begin_oauth(
        "shopify", business_id="biz_1", redirect_uri=CALLBACK, now=T0, params={"shop": SHOP}
    )
    state = parse_qs(url.split("?", 1)[1])["state"][0]
    forged = {"code": "code_ok", "shop": SHOP, "state": state, "timestamp": "1", "hmac": "00" * 32}
    with pytest.raises(AuthorizationError):
        rig.engine.finish_oauth("shopify", forged, T0, rig.queue)
    assert fake.token_calls == []
    # the state was not spent: the genuine callback still works
    good = sign_query({"code": "code_ok", "shop": SHOP, "state": state, "timestamp": "1"})
    assert rig.engine.finish_oauth("shopify", good, T0, rig.queue).external_id == SHOP


def test_the_rotating_refresh_token_is_used_once_and_a_dead_one_asks_for_reconnect_once() -> None:
    rig, fake = make_shopify_rig()
    connection = connect_oauth(rig, fake)
    first_refresh = fake.refresh_token
    later = T0 + timedelta(minutes=58)  # inside the 5-minute margin of the 1 h token
    rig.clock.current = later
    report = rig.engine.sync(connection.connection_id, Stream.PRODUCTS, Lane.LIVE, later)
    assert report.status == "ok"
    refresh = fake.token_calls[-1]
    assert refresh["grant_type"] == "refresh_token" and refresh["refresh_token"] == first_refresh
    assert fake.refresh_token != first_refresh  # rotated, and the vault holds the new one
    stored = rig.engine.tokens.vault.load(connection.connection_id)
    assert stored is not None and stored.refresh_token == fake.refresh_token

    fake.revoked = True  # the merchant removed access: the refresh gets 401 invalid_request
    rig.clock.current = later + timedelta(hours=1)
    for _ in range(3):
        rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, rig.clock.current)
    current = rig.store.get_connection(connection.connection_id)
    assert current is not None and current.status is ConnectionStatus.NEEDS_RECONNECT
    reconnect = [a for a in rig.alerts.list_alerts() if a.code == "connector.needs_reconnect"]
    assert len(reconnect) == 1 and "Shopify" in reconnect[0].message


def test_a_custom_app_token_connects_without_oauth_and_never_refreshes() -> None:
    rig, fake = make_shopify_rig()
    connection = connect_token(rig)
    assert connection.external_id == SHOP and connection.label == "Acme Goods"
    report = rig.engine.sync(connection.connection_id, Stream.CUSTOMERS, Lane.LIVE, T0 + timedelta(hours=1))
    assert report.status == "ok"
    # weeks later, after an outage: the long catch-up window becomes a bulk export, still no refresh
    later = rig.engine.sync(connection.connection_id, Stream.ORDERS, Lane.LIVE, T0 + timedelta(days=40))
    assert later.status == "waiting"
    assert fake.token_calls == []
    assert {r.headers.get("x-shopify-access-token") for r in fake.calls if r.url.path == GQL_PATH} == {
        "shpat_live_1"
    }
    with pytest.raises(NeedsReconnect):
        connect_token(rig, token="shpat_wrong")  # a wrong token is answered to the person at once
