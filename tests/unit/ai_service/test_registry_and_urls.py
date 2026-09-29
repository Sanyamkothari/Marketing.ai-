"""The provider registry and the address rules (DEC-1140, DEC-1141)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from engine.ai_service import PROVIDERS, AiServiceError, check_base_url, provider_infos


def test_the_six_providers_and_their_protocols() -> None:
    assert {p.id: p.protocol for p in PROVIDERS.values()} == {
        "bedrock": "bedrock",
        "openai": "openai",
        "anthropic": "anthropic",
        "openrouter": "openai",
        "huggingface": "openai",
        "openai_compatible": "openai",
    }


def test_default_addresses_keys_and_third_party_flags() -> None:
    assert PROVIDERS["openai"].default_base_url == "https://api.openai.com/v1"
    assert PROVIDERS["anthropic"].default_base_url == "https://api.anthropic.com"
    assert PROVIDERS["openrouter"].default_base_url == "https://openrouter.ai/api/v1"
    assert PROVIDERS["huggingface"].default_base_url == "https://router.huggingface.co/v1"
    assert PROVIDERS["openai_compatible"].default_base_url is None
    assert PROVIDERS["openai_compatible"].needs_base_url and not PROVIDERS["openai_compatible"].needs_key
    assert not PROVIDERS["bedrock"].needs_key and PROVIDERS["bedrock"].needs_region
    assert [p.id for p in PROVIDERS.values() if not p.third_party] == ["bedrock"]
    assert PROVIDERS["anthropic"].supports_embeddings is False
    assert PROVIDERS["anthropic"].key_prefixes == ("sk-ant-",)
    assert PROVIDERS["openrouter"].key_prefixes == ("sk-or-",)
    assert PROVIDERS["huggingface"].key_prefixes == ("hf_",)
    for provider in PROVIDERS.values():
        assert provider.key_help and provider.label
        assert provider.needs_key == bool(provider.key_hint) or provider.id == "openai_compatible"


def test_suggestions_come_from_the_config_file(config_root: Path, tmp_path: Path) -> None:
    infos = {p.id: p for p in provider_infos(config_root)}
    assert infos["openai"].model_suggestions and infos["bedrock"].embedding_suggestions
    assert infos["anthropic"].embedding_suggestions == ()
    empty = {p.id: p for p in provider_infos(tmp_path)}  # no file: no examples, no failure
    assert all(not p.model_suggestions for p in empty.values())


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "https://api.example.com/v1/",
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000",
        "http://[::1]:8000/v1",
    ],
)
def test_good_addresses_are_kept_without_a_trailing_slash(url: str) -> None:
    assert check_base_url(url) == url.rstrip("/")


PASSWORD = "hunt" + "er2"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://example.com",
        "example.com",
        "http://example.com/v1",  # http only for this computer
        "https://user:" + PASSWORD + "@example.com/v1",
        "https://example.com/v1?key=abc",
        "https://example.com/v1#x",
        "https://exa mple.com",
        "http://169.254.169.254/latest/meta-data",
        "https://169.254.169.254/",
        "https://[::ffff:169.254.169.254]/",
        "https://[fe80::1]/",
        "https://0.0.0.0/",
        "https://2852039166/",  # the metadata address written as one number
        "https://0xA9FEA9FE/",
        "https://" + "a" * 600 + ".com",
    ],
)
def test_bad_addresses_are_refused_naming_the_field(url: str) -> None:
    with pytest.raises(AiServiceError) as caught:
        check_base_url(url)
    assert caught.value.status == 422 and caught.value.field == "base_url"
    assert caught.value.code == "AI_BASE_URL_INVALID"
    assert PASSWORD not in caught.value.message


def test_a_deployment_refuses_local_and_private_addresses() -> None:
    for url in (
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000",
        "https://10.1.2.3/v1",
        "https://192.168.0.5/v1",
        "https://172.16.0.9/v1",
        "https://[::1]/v1",
        "https://app.localhost/v1",
    ):
        check_base_url(url.replace("http://", "http://") if url.startswith("http://") else url, env="local")
        with pytest.raises(AiServiceError):
            check_base_url(url, env="prod")
        with pytest.raises(AiServiceError):
            check_base_url(url, env="dev")
    assert check_base_url("https://api.openai.com/v1", env="prod") == "https://api.openai.com/v1"


def _resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    def fake(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_a_name_that_resolves_to_the_metadata_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to(monkeypatch, "169.254.169.254")
    check_base_url("https://evil.example.com/v1")  # a literal check alone cannot know
    for env in ("local", "prod"):
        with pytest.raises(AiServiceError):
            check_base_url("https://evil.example.com/v1", env=env, resolve=True)


def test_a_name_that_resolves_to_a_private_address_is_refused_only_on_a_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolves_to(monkeypatch, "93.184.216.34", "10.0.0.7")
    assert check_base_url("https://llm.corp.example/v1", env="local", resolve=True)
    with pytest.raises(AiServiceError):
        check_base_url("https://llm.corp.example/v1", env="prod", resolve=True)


def test_a_name_that_does_not_resolve_is_left_to_the_call_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object, **__: object) -> list[object]:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert check_base_url("https://nowhere.example.com/v1", env="prod", resolve=True)
