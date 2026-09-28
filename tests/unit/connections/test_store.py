"""Saved connections and their encrypted secrets (Plan H M80, DEC-1101), and the form check."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from engine.connections.base import ConnectorError
from engine.connections.registry import CONNECTORS, catalogue, check_form, check_name
from engine.connections.store import KEY_FILENAME, ConnectionStore, connections_key
from engine.settings import Settings, SettingsError
from engine.storage import LocalStorage

PASSWORD = "correct-horse-battery-staple"  # secret-scan: allow (a fake password for the encryption tests)


def store_at(root: Path, **settings: object) -> ConnectionStore:
    return ConnectionStore(LocalStorage(root), Settings(**settings))  # type: ignore[arg-type]


def make(store: ConnectionStore) -> object:
    return store.create(
        kind="postgres",
        name="CRM",
        config={"host": "db.example.com", "database": "crm", "user": "reader"},
        secrets={"password": PASSWORD},
    )


def test_a_secret_is_never_plaintext_on_disk_and_decrypts_back(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = make(store)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes(), path
    on_disk = json.loads((tmp_path / "connections" / f"{record.connection_id}.json").read_text())  # type: ignore[attr-defined]
    assert on_disk["secret_names"] == ["password"] and on_disk["secrets_token"]
    assert "password" not in on_disk["config"]
    assert store.secrets(store.get(record.connection_id)) == {"password": PASSWORD}  # type: ignore[attr-defined]


def test_a_generated_local_key_is_kept_owner_only_and_reused(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    make(store)
    key_file = tmp_path / KEY_FILENAME
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    first = key_file.read_bytes()
    make(store_at(tmp_path))
    assert key_file.read_bytes() == first


def test_the_configured_key_wins_and_a_bad_one_is_named(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    assert connections_key(Settings(connections_key=SecretStr(key)), LocalStorage(tmp_path)) == key.encode()
    assert not (tmp_path / KEY_FILENAME).exists()
    with pytest.raises(SettingsError) as caught:
        connections_key(Settings(connections_key=SecretStr("too-short")), LocalStorage(tmp_path))
    assert caught.value.env_var == "MARKETING_AI_CONNECTIONS_KEY"


def test_a_generated_deployment_secret_derives_a_stable_key(tmp_path: Path) -> None:
    """What AWS Secrets Manager generates (letters and digits, no `=`) is not a Fernet key; the key is
    derived from it, the same on every container, so a deployment can supply it (DEC-1120)."""
    generated = "Ab3" * 16  # 48 letters and digits, as `exclude_punctuation` generates
    key = connections_key(Settings(connections_key=SecretStr(generated)), LocalStorage(tmp_path))
    Fernet(key)  # a valid key
    assert key != generated.encode()
    again = connections_key(Settings(connections_key=SecretStr(f" {generated}\n")), LocalStorage(tmp_path))
    assert again == key
    other = connections_key(Settings(connections_key=SecretStr("Ab4" * 16)), LocalStorage(tmp_path))
    assert other != key
    first = ConnectionStore(
        LocalStorage(tmp_path),
        Settings(env="prod", connections_key=SecretStr(generated), cors_origins=("https://app.example.com",)),
    )
    record = make(first)
    second = ConnectionStore(LocalStorage(tmp_path), Settings(connections_key=SecretStr(generated)))
    assert second.secrets(second.get(record.connection_id))["password"] == PASSWORD  # type: ignore[attr-defined]
    with pytest.raises(SettingsError):  # too short to be a generated secret, and not a Fernet key
        connections_key(Settings(connections_key=SecretStr("Ab3" * 5)), LocalStorage(tmp_path))


def test_production_refuses_to_save_a_secret_without_a_key(tmp_path: Path) -> None:
    store = store_at(tmp_path, env="prod", cors_origins=("https://app.example.com",))
    with pytest.raises(SettingsError) as caught:
        make(store)
    assert caught.value.code == "SETTING_REQUIRED" and "MARKETING_AI_CONNECTIONS_KEY" in caught.value.message
    assert (
        not list((tmp_path / "connections").glob("*.json")) if (tmp_path / "connections").exists() else True
    )
    # A connection with no secret (Amazon S3 on the machine's own sign-in) needs no key at all.
    assert store.create(kind="s3", name="S3", config={"bucket": "b"}, secrets={}).secrets_token is None


def test_a_wrong_key_is_a_clear_error_not_garbage(tmp_path: Path) -> None:
    first = Settings(connections_key=SecretStr(Fernet.generate_key().decode()))
    second = Settings(connections_key=SecretStr(Fernet.generate_key().decode()))
    record = make(ConnectionStore(LocalStorage(tmp_path), first))
    other = ConnectionStore(LocalStorage(tmp_path), second)
    with pytest.raises(ConnectorError) as caught:
        other.secrets(other.get(record.connection_id))  # type: ignore[attr-defined]
    assert caught.value.code == "CONNECTION_SECRETS_UNREADABLE" and caught.value.status == 409
    assert "MARKETING_AI_CONNECTIONS_KEY" in caught.value.message and "enter its password" in (
        caught.value.fix or ""
    )
    assert not other.readable(other.get(record.connection_id))  # type: ignore[attr-defined]


def test_an_update_keeps_blank_secrets_and_replaces_given_ones(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = make(store)
    kept = store.update(record, name="CRM 2", config=record.config, secrets={})  # type: ignore[attr-defined]
    assert store.secrets(kept) == {"password": PASSWORD} and kept.name == "CRM 2"
    changed = store.update(kept, name="CRM 2", config=kept.config, secrets={"password": "new"})
    assert store.secrets(changed) == {"password": "new"}
    cleared = store.update(
        changed, name="CRM 2", config=changed.config, secrets={}, drop_secrets=frozenset({"password"})
    )
    assert cleared.secrets_token is None and cleared.secret_names == ()


def test_ids_that_are_not_connection_ids_are_not_found(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for bad in ("../../etc/passwd", "c_123", "", "c_zzzzzzzzzzzz"):
        with pytest.raises(ConnectorError) as caught:
            store.get(bad)
        assert caught.value.status == 404


# --- the form -----------------------------------------------------------------------------------------
def test_the_catalogue_lists_every_kind_and_the_ai_service_card() -> None:
    kinds = [k.kind for k in catalogue()]
    assert kinds == [*CONNECTORS, "ai_service"]
    ai = catalogue()[-1]
    assert not ai.creatable and ai.screen == "#/generative/connection"


def test_the_form_check_coerces_defaults_and_names_fields_never_values() -> None:
    info = CONNECTORS["postgres"].info()
    config, secrets = check_form(
        info, {"host": " db ", "database": "crm", "user": "r", "port": "6543"}, {"password": "pw"}
    )
    assert config == {"host": "db", "database": "crm", "user": "r", "port": 6543, "sslmode": "prefer"}
    assert secrets == {"password": "pw"}
    with pytest.raises(ConnectorError) as caught:
        check_form(info, {"host": "db", "database": "crm", "user": "r", "password": "leak-me"}, {})
    assert "leak-me" not in caught.value.message and "password" in caught.value.message
    with pytest.raises(ConnectorError) as missing:
        check_form(info, {"host": "db", "database": "crm", "user": "r"}, {})
    assert missing.value.message == "Password is needed."
    # On an update, a saved password counts.
    check_form(
        info,
        {"host": "db", "database": "crm", "user": "r"},
        {"password": ""},
        kept_secrets=frozenset({"password"}),
    )
    for bad in ({"port": "99999"}, {"port": "abc"}, {"sslmode": "maybe"}):
        with pytest.raises(ConnectorError):
            check_form(info, {"host": "db", "database": "crm", "user": "r", **bad}, {"password": "pw"})
    assert check_name(None, info) == "PostgreSQL" and check_name("  Mine ", info) == "Mine"
