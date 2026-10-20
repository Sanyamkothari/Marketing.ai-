"""OAuth: state and PKCE, the single-use callback, code exchange, refresh, revoked versus transient."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from autonomy.connectors import (
    AppCredentials,
    AuthorizationDenied,
    AuthorizationError,
    ConnectorHttp,
    ConnectorStore,
    HttpPolicy,
    ManualClock,
    NeedsReconnect,
    OAuthClient,
    OAuthProvider,
    RemoteError,
    TokenSet,
    TransientFailure,
    create_connector_tables,
    pkce_challenge,
)
from autonomy.connectors.oauth import (
    MemoryAuthStateStore,
    begin_authorization,
    finish_authorization,
    parse_token_response,
)
from autonomy.store import memory_engine

from .base_support import APP, DEMO_PROVIDER, T0, FakePlatform

SHOPIFY_LIKE = OAuthProvider(
    name="Shop",
    authorize_url="https://{shop}/admin/oauth/authorize",
    token_url="https://{shop}/admin/oauth/access_token",
    scopes=("read_orders", "read_customers"),
    scope_separator=",",
    client_auth="body",
    revoked_errors=frozenset({"invalid_grant", "invalid_request"}),
    param_patterns=(("shop", r"[a-z0-9][a-z0-9-]*\.myshopify\.com"),),
    extra_token_params=(("expiring", "1"),),
)


def counter():
    values = iter(f"rand{i:02d}" * 6 for i in range(100))
    return lambda n: next(values)


def factory(platform: FakePlatform, clock: ManualClock):
    transport = platform.transport()
    return lambda base: ConnectorHttp(HttpPolicy(base_url=base), clock, transport=transport)


def test_pkce_challenge_is_unpadded_base64url_sha256() -> None:
    verifier = "dBjftJeZ4CVP-mJ92K9mU8EqzKnYj0gxs1pNc23bYUM"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert pkce_challenge(verifier) == expected
    assert "=" not in expected and "+" not in expected and "/" not in expected


def test_begin_builds_the_consent_url_with_state_and_s256() -> None:
    states = MemoryAuthStateStore()
    url, pending = begin_authorization(
        DEMO_PROVIDER,
        APP,
        states,
        kind="demo",
        business_id="b1",
        redirect_uri="https://a.example/cb",
        now=T0,
        random_token=counter(),
    )
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://auth.example/oauth/authorize?")
    assert query["state"] == pending.state and query["client_id"] == "client_1"
    assert query["code_challenge_method"] == "S256"
    assert pending.code_verifier is not None and query["code_challenge"] == pkce_challenge(
        pending.code_verifier
    )
    assert query["scope"] == "orders:read customers:read"
    assert pending.expires_at == T0 + timedelta(minutes=10)
    assert "code_verifier" not in repr(pending) or pending.code_verifier not in repr(pending)


def test_a_url_slot_is_filled_only_with_a_value_matching_its_pattern() -> None:
    states = MemoryAuthStateStore()
    url, pending = begin_authorization(
        SHOPIFY_LIKE,
        APP,
        states,
        kind="shop",
        business_id="b1",
        redirect_uri="https://a.example/cb",
        now=T0,
        params={"shop": "acme.myshopify.com"},
    )
    assert url.startswith("https://acme.myshopify.com/admin/oauth/authorize?")
    assert "scope=read_orders%2Cread_customers" in url
    assert "expiring" not in url  # Shopify takes it in the code-exchange POST, not here
    assert pending.code_verifier is None  # no PKCE on this platform
    for bad in ("evil.example", "acme.myshopify.com.evil.example", "169.254.169.254", ""):
        with pytest.raises(AuthorizationError):
            begin_authorization(
                SHOPIFY_LIKE,
                APP,
                states,
                kind="shop",
                business_id="b1",
                redirect_uri="x",
                now=T0,
                params={"shop": bad},
            )


@pytest.mark.parametrize("store_kind", ["memory", "sql"])
def test_the_callback_state_is_single_use_and_expires(store_kind: str) -> None:
    if store_kind == "sql":
        db = memory_engine()
        create_connector_tables(db)
        states: MemoryAuthStateStore | ConnectorStore = ConnectorStore(db)
    else:
        states = MemoryAuthStateStore()
    _, pending = begin_authorization(
        DEMO_PROVIDER, APP, states, kind="demo", business_id="b1", redirect_uri="r", now=T0
    )
    got, code = finish_authorization(
        {"state": pending.state, "code": "c1"}, states, T0 + timedelta(minutes=1)
    )
    assert got == pending and code == "c1"
    with pytest.raises(AuthorizationError, match="expired or was already used"):
        finish_authorization({"state": pending.state, "code": "c1"}, states, T0 + timedelta(minutes=1))
    _, late = begin_authorization(
        DEMO_PROVIDER, APP, states, kind="demo", business_id="b1", redirect_uri="r", now=T0
    )
    with pytest.raises(AuthorizationError):
        finish_authorization({"state": late.state, "code": "c"}, states, T0 + timedelta(minutes=11))
    with pytest.raises(AuthorizationError):
        finish_authorization({"code": "c"}, states, T0)


def test_a_declined_consent_is_its_own_error() -> None:
    states = MemoryAuthStateStore()
    _, pending = begin_authorization(
        DEMO_PROVIDER, APP, states, kind="demo", business_id="b1", redirect_uri="r", now=T0
    )
    with pytest.raises(AuthorizationDenied):
        finish_authorization({"state": pending.state, "error": "access_denied"}, states, T0)


def test_exchange_sends_basic_auth_and_the_verifier_and_ignores_unknown_fields() -> None:
    platform, clock = FakePlatform(), ManualClock(T0)
    states = MemoryAuthStateStore()
    _, pending = begin_authorization(
        DEMO_PROVIDER, APP, states, kind="demo", business_id="b1", redirect_uri="https://a.example/cb", now=T0
    )
    tokens = OAuthClient(DEMO_PROVIDER, APP, factory(platform, clock)).exchange(pending, "code_ok", T0)
    form = platform.token_calls[0]
    assert form["code_verifier"] == pending.code_verifier
    assert form["redirect_uri"] == "https://a.example/cb" and "client_secret" not in form
    assert tokens.access_token == "tok_2" and tokens.refresh_token == "ref_2"
    assert tokens.expires_at == T0 + timedelta(hours=1)
    assert "tok_2" not in repr(tokens) and "ref_2" not in repr(tokens)


def test_a_bad_code_is_not_retried_forever() -> None:
    platform, clock = FakePlatform(), ManualClock(T0)
    states = MemoryAuthStateStore()
    _, pending = begin_authorization(
        DEMO_PROVIDER, APP, states, kind="demo", business_id="b1", redirect_uri="r", now=T0
    )
    with pytest.raises(NeedsReconnect):
        OAuthClient(DEMO_PROVIDER, APP, factory(platform, clock)).exchange(pending, "code_bad", T0)
    assert len(platform.token_calls) == 1


def test_refresh_rotates_or_keeps_the_refresh_token() -> None:
    platform, clock = FakePlatform(), ManualClock(T0)
    client = OAuthClient(DEMO_PROVIDER, APP, factory(platform, clock))
    old = TokenSet(
        access_token="tok_1", refresh_token="ref_1", expires_at=T0, obtained_at=T0 - timedelta(days=3)
    )
    fresh = client.refresh(old, T0, {})
    assert (
        fresh.refresh_token == "ref_2" and fresh.obtained_at == old.obtained_at and fresh.refreshed_at == T0
    )
    platform.rotate_refresh = False
    kept = client.refresh(fresh, T0 + timedelta(hours=1), {})
    assert kept.refresh_token == "ref_2" and kept.access_token == "tok_3"


def test_refresh_revoked_versus_transient() -> None:
    platform, clock = FakePlatform(), ManualClock(T0)
    client = OAuthClient(DEMO_PROVIDER, APP, factory(platform, clock))
    tokens = TokenSet(access_token="a", refresh_token="ref_1", obtained_at=T0)
    platform.revoked = True
    with pytest.raises(NeedsReconnect, match="invalid_grant"):
        client.refresh(tokens, T0, {})

    def shopify_dead(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_request"})

    dead = OAuthClient(
        SHOPIFY_LIKE,
        APP,
        lambda b: ConnectorHttp(HttpPolicy(base_url=b), clock, transport=httpx.MockTransport(shopify_dead)),
    )
    with pytest.raises(NeedsReconnect):
        dead.refresh(tokens, T0, {"shop": "acme.myshopify.com"})

    down = OAuthClient(
        DEMO_PROVIDER,
        APP,
        lambda b: ConnectorHttp(
            HttpPolicy(base_url=b), clock, transport=httpx.MockTransport(lambda r: httpx.Response(503))
        ),
    )
    with pytest.raises(TransientFailure):
        down.refresh(tokens, T0, {})

    odd = OAuthClient(
        DEMO_PROVIDER,
        APP,
        lambda b: ConnectorHttp(
            HttpPolicy(base_url=b),
            clock,
            transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_scope"})),
        ),
    )
    with pytest.raises(RemoteError):
        odd.refresh(tokens, T0, {})


def test_body_client_auth_puts_credentials_in_the_form() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        assert request.url.host == "acme.myshopify.com"
        return httpx.Response(200, json={"access_token": "shpat_x", "scope": "read_orders"})

    clock = ManualClock(T0)
    client = OAuthClient(
        SHOPIFY_LIKE,
        APP,
        lambda b: ConnectorHttp(HttpPolicy(base_url=b), clock, transport=httpx.MockTransport(handler)),
    )
    states = MemoryAuthStateStore()
    _, pending = begin_authorization(
        SHOPIFY_LIKE,
        APP,
        states,
        kind="shop",
        business_id="b",
        redirect_uri="r",
        now=T0,
        params={"shop": "acme.myshopify.com"},
    )
    tokens = client.exchange(pending, "c", T0)
    assert seen[0]["client_id"] == "client_1" and seen[0]["client_secret"] == "secret_1"
    assert seen[0]["expiring"] == "1"  # expiring offline tokens are asked for at the exchange
    assert tokens.expires_at is None and tokens.refresh_token is None  # a non-expiring offline token


def test_token_response_parsing_is_defensive() -> None:
    with pytest.raises(RemoteError):
        parse_token_response({"token": "x"}, T0)
    parsed = parse_token_response(
        {"access_token": "a", "expires_in": "3600", "refresh_token_expires_in": 7776000, "shop": "s"}, T0
    )
    assert parsed.expires_at == T0 + timedelta(hours=1)
    assert parsed.refresh_expires_at == T0 + timedelta(days=90)
    assert parsed.extra == {"shop": "s"}


def test_app_credentials_come_from_named_env_vars_and_hide_the_secret() -> None:
    assert AppCredentials.env_names("klaviyo") == (
        "AUTONOMY_KLAVIYO_CLIENT_ID",
        "AUTONOMY_KLAVIYO_CLIENT_SECRET",
    )
    app = AppCredentials.from_env(
        "klaviyo", {"AUTONOMY_KLAVIYO_CLIENT_ID": "id", "AUTONOMY_KLAVIYO_CLIENT_SECRET": "s3"}
    )
    assert app is not None and "s3" not in repr(app)
    assert AppCredentials.from_env("klaviyo", {"AUTONOMY_KLAVIYO_CLIENT_ID": "id"}) is None
