"""Tokens: encrypted at rest, refreshed before expiry by one worker, kept alive, revoked handled once."""

from __future__ import annotations

from datetime import timedelta

import pytest

from autonomy.connectors import (
    Connection,
    ConnectionStatus,
    ConnectorHttp,
    HttpPolicy,
    MemoryTokenVault,
    NeedsReconnect,
    OAuthClient,
    SqlTokenVault,
    TokenManager,
    TokenSet,
    TransientFailure,
)
from autonomy.connectors.tokens import VaultUnreadable

from .base_support import APP, DEMO_PROVIDER, T0, FakePlatform, TestCipher, connect, make_rig


def conn() -> Connection:
    return Connection(
        connection_id="conn_1", kind="demo", business_id="b", label="Demo", created_at=T0, updated_at=T0
    )


def manager(platform: FakePlatform, *, holder: str = "w1", rig=None):
    rig = rig or make_rig(platform)
    transport = platform.transport()
    client = OAuthClient(
        DEMO_PROVIDER,
        APP,
        lambda base: ConnectorHttp(HttpPolicy(base_url=base), rig.clock, transport=transport),
    )
    vault = MemoryTokenVault()
    revoked: list[str] = []
    tm = TokenManager(
        vault, rig.store, lambda c: client, holder=holder, on_revoked=lambda c, e, now: revoked.append(e.code)
    )
    return tm, vault, rig, revoked


def tokens(expires_in: timedelta, refreshed: timedelta = timedelta(0)) -> TokenSet:
    return TokenSet(
        access_token="tok_1",
        refresh_token="ref_1",
        expires_at=T0 + expires_in,
        obtained_at=T0 - timedelta(days=1),
        refreshed_at=T0 - refreshed,
    )


def test_a_token_far_from_expiry_is_used_as_is() -> None:
    platform = FakePlatform()
    tm, vault, _, _ = manager(platform)
    vault.save("conn_1", tokens(timedelta(minutes=30), refreshed=timedelta(minutes=30)), T0)
    assert tm.access_token(conn(), T0).access_token == "tok_1"
    assert platform.token_calls == []


def test_a_token_near_expiry_is_refreshed_and_saved() -> None:
    platform = FakePlatform()
    tm, vault, _, _ = manager(platform)
    vault.save("conn_1", tokens(timedelta(minutes=3), refreshed=timedelta(minutes=57)), T0)
    fresh = tm.access_token(conn(), T0)
    assert fresh.access_token == "tok_2" and vault.load("conn_1") == fresh
    assert fresh.refresh_token == "ref_2"  # rotated, and the rotated one is what is stored
    assert len(platform.token_calls) == 1


def test_a_second_worker_does_not_spend_a_rotating_refresh_token_twice() -> None:
    platform = FakePlatform()
    rig = make_rig(platform)
    tm_a, vault, _, _ = manager(platform, rig=rig, holder="w1")
    tm_b = TokenManager(vault, rig.store, tm_a.oauth_for, holder="w2")
    stale = tokens(timedelta(minutes=3), refreshed=timedelta(minutes=57))
    vault.save("conn_1", stale, T0)
    seen_by_b = vault.load("conn_1")
    tm_a.access_token(conn(), T0)
    # B read the token before A refreshed; inside its lease it re-reads and uses A's token.
    assert seen_by_b is not None
    result = tm_b._refresh(conn(), tm_a.oauth_for(conn()), seen_by_b, T0, force=False)  # type: ignore[arg-type]
    assert result.access_token == "tok_2"
    assert len(platform.token_calls) == 1


def test_a_busy_lease_uses_the_current_token_or_asks_to_retry() -> None:
    platform = FakePlatform()
    tm, vault, rig, _ = manager(platform)
    assert rig.store.acquire_lease("token:conn_1", "other-worker", T0, timedelta(seconds=60))
    vault.save("conn_1", tokens(timedelta(minutes=3)), T0)
    assert tm.access_token(conn(), T0).access_token == "tok_1"  # still valid for 3 minutes
    vault.save("conn_1", tokens(timedelta(minutes=-1)), T0)
    with pytest.raises(TransientFailure) as caught:
        tm.access_token(conn(), T0)
    assert caught.value.retry_at == T0 + timedelta(seconds=5)
    assert platform.token_calls == []
    # the other worker's lease runs out: we may refresh
    assert tm.access_token(conn(), T0 + timedelta(seconds=61)).access_token == "tok_2"


def test_a_revoked_refresh_token_calls_the_hook_and_never_loops() -> None:
    platform = FakePlatform(revoked=True)
    tm, vault, _, revoked = manager(platform)
    vault.save("conn_1", tokens(timedelta(minutes=1), refreshed=timedelta(minutes=59)), T0)
    with pytest.raises(NeedsReconnect):
        tm.access_token(conn(), T0)
    assert revoked == ["connector.needs_reconnect"]
    assert len(platform.token_calls) == 1


def test_no_saved_tokens_means_reconnect() -> None:
    tm, _, _, revoked = manager(FakePlatform())
    with pytest.raises(NeedsReconnect):
        tm.access_token(conn(), T0)
    assert revoked


def test_keep_alive_refreshes_idle_connections_before_the_90_day_revocation() -> None:
    platform = FakePlatform()
    tm, vault, _, _ = manager(platform)
    vault.save("conn_1", tokens(timedelta(days=-20), refreshed=timedelta(days=29)), T0)
    assert not tm.keep_alive(conn(), T0)
    assert tm.keep_alive(conn(), T0 + timedelta(days=1, minutes=1))
    assert vault.load("conn_1").refreshed_at == T0 + timedelta(days=1, minutes=1)  # type: ignore[union-attr]
    assert not tm.keep_alive_due(conn(), T0 + timedelta(days=2))


def test_a_refresh_moments_ago_is_not_repeated() -> None:
    platform = FakePlatform()
    tm, vault, _, _ = manager(platform)
    vault.save("conn_1", tokens(timedelta(minutes=1), refreshed=timedelta(seconds=3)), T0)
    assert tm.access_token(conn(), T0).access_token == "tok_1"  # Klaviyo allows 10 refreshes a minute
    assert platform.token_calls == []


def test_api_keys_never_refresh_or_expire() -> None:
    tm, vault, _, _ = manager(FakePlatform())
    vault.save("conn_1", TokenSet.api_key("pk_live", T0), T0)
    assert tm.access_token(conn(), T0 + timedelta(days=400)).access_token == "pk_live"


def test_sql_vault_encrypts_and_refuses_another_key() -> None:
    rig = make_rig()
    vault = SqlTokenVault(rig.store, TestCipher())
    vault.save("conn_1", tokens(timedelta(hours=1)), T0)
    stored = rig.store.read_secret("conn_1")
    assert stored is not None and "tok_1" not in stored[0] and "ref_1" not in stored[0]
    assert vault.load("conn_1") == tokens(timedelta(hours=1))
    with pytest.raises(VaultUnreadable, match="different key"):
        SqlTokenVault(rig.store, TestCipher("another-key")).load("conn_1")
    vault.delete("conn_1")
    assert vault.load("conn_1") is None


def test_fernet_cipher_uses_the_v1_credential_store_scheme() -> None:
    fernet = pytest.importorskip("cryptography.fernet")
    pytest.importorskip("yaml")  # engine.connections.store imports engine.settings
    from autonomy.connectors import FernetCipher
    from engine.connections.store import key_fingerprint

    key = fernet.Fernet.generate_key()
    cipher = FernetCipher(key)
    assert cipher.fingerprint == key_fingerprint(key)
    rig = make_rig()
    vault = SqlTokenVault(rig.store, cipher)
    vault.save("conn_1", tokens(timedelta(hours=1)), T0)
    assert vault.load("conn_1") == tokens(timedelta(hours=1))
    with pytest.raises(VaultUnreadable):
        FernetCipher(fernet.Fernet.generate_key()).decrypt(rig.store.read_secret("conn_1")[0])  # type: ignore[index]


def test_end_to_end_the_engine_marks_needs_reconnect_and_alerts_once() -> None:
    platform = FakePlatform()
    rig = make_rig(platform)
    connection = connect(rig)
    platform.revoked = True
    rig.clock.advance(timedelta(hours=2))
    later = rig.clock.now()
    with pytest.raises(NeedsReconnect):
        rig.engine.tokens.access_token(connection, later)
    with pytest.raises(NeedsReconnect):
        rig.engine.tokens.access_token(connection, later)
    status = rig.store.get_connection(connection.connection_id)
    assert status is not None and status.status is ConnectionStatus.NEEDS_RECONNECT
    reconnect = [a for a in rig.alerts.list_alerts() if a.code == "connector.needs_reconnect"]
    assert len(reconnect) == 1 and reconnect[0].severity == "critical" and reconnect[0].auto_paused
    assert "tok_" not in reconnect[0].message


def test_a_forced_refresh_while_another_worker_refreshes_asks_to_retry_not_to_reconnect() -> None:
    platform = FakePlatform()
    tm, vault, rig, revoked = manager(platform)
    vault.save("conn_1", tokens(timedelta(minutes=30), refreshed=timedelta(minutes=30)), T0)
    assert rig.store.acquire_lease("token:conn_1", "other-worker", T0, timedelta(minutes=5))
    # The API just refused tok_1 (the other worker's refresh retired it): handing tok_1 back would
    # end in a second 401, AuthRejected and a false "reconnect" alert.
    with pytest.raises(TransientFailure):
        tm.force_refresh(conn(), T0)
    assert revoked == [] and platform.token_calls == []


def test_a_token_endpoint_hiccup_keeps_using_a_still_valid_token() -> None:
    import httpx

    platform = FakePlatform()
    tm, vault, _rig, revoked = manager(platform)
    vault.save("conn_1", tokens(timedelta(minutes=3), refreshed=timedelta(minutes=57)), T0)
    calls = {"n": 0}
    real = platform._token

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    platform._token = down  # type: ignore[method-assign]
    assert tm.access_token(conn(), T0).access_token == "tok_1"  # good for 3 more minutes
    assert calls["n"] >= 1 and revoked == []
    platform._token = real  # type: ignore[method-assign]
    vault.save("conn_1", tokens(timedelta(minutes=-1), refreshed=timedelta(hours=1)), T0)
    platform._token = down  # type: ignore[method-assign]
    with pytest.raises(TransientFailure):
        tm.access_token(conn(), T0 + timedelta(minutes=1))  # expired: nothing to fall back on


def test_refresh_keeps_account_details_from_the_first_exchange() -> None:
    from autonomy.connectors.oauth import parse_token_response

    first = parse_token_response({"access_token": "a", "refresh_token": "r", "account_id": "K1"}, T0)
    again = parse_token_response({"access_token": "b", "expires_in": 3600}, T0, previous=first)
    assert again.extra == {"account_id": "K1"} and again.refresh_token == "r"
