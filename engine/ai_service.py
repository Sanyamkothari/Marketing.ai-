"""The AI service: which language model the whole product talks to, and how that is chosen (DEC-1140).

Marketing AI talks to language models for two different purposes, and each has its own **slot** so it
can be billed, kept private and chosen separately (DEC-1140):

* `product` - **Product AI**, your team's own tool: the Guided-setup chat helper. It sees column names
  and masked samples of the operator's file, and the operator pays for it.
* `deliverable` - **Deliverable AI**, what the customer receives: the document assistant (RAG),
  root-cause summaries, win-back campaign copy, and the judge and guardrail checks over them. The
  customer may choose it, pay for it, or want it kept off third parties.

Neither is the AutoML models. A person connects each under Connections: Amazon Bedrock, OpenAI, Claude,
OpenRouter, Hugging Face, or any OpenAI-compatible server (Ollama, vLLM, LiteLLM, Together, Groq, an
Azure-compatible gateway).

**The record.** One JSON document per slot in the artefact store (`ai_service/<slot>.json`): the
provider, the model names, the address and region, the last test, and - for a provider that needs one -
the API key,
which exists only as a Fernet token made with the Connections key (`engine.connections.store`, the same
key, the same fingerprint, the same wrong-key handling). The key is never returned by any endpoint,
never logged, never in an exception message; a state says `has_key: true` and nothing more.

**The order.** Every language call resolves its client for its slot through :func:`resolve_client`:

1. that slot's saved AI service;
2. for `deliverable` only: else the `product` slot's saved service (one connection is enough to start);
3. else the use case's own `generative.llm.backend: bedrock`, exactly as before this module existed;
4. else the deterministic fake, but only when `MARKETING_AI_ALLOW_FAKE_AI` is on (tests, developer
   targets) - the fake is a test tool and never answers a real person;
5. else :class:`AiNotConnectedError` (naming the slot), which the API turns into `409 AI_NOT_CONNECTED`.

`product` never falls back to `deliverable`: the operator's helper must not spend the customer's
account. A saved service whose key can no longer be read is *not* skipped in favour of the next step:
quietly sending prompts to a different provider than the one that was chosen would be worse than asking
the person to enter the key again.

**Embeddings.** A provider with no embeddings (Claude), or a service saved without an embedding model,
embeds by keyword hash (`engine.llm.keyword_hash_vector`, model id `keyword-hash-v1`): real retrieval
by shared vocabulary, no model involved, and said so on screen.

**Addresses (SSRF).** A base URL must be http(s) with no user info, no query and no fragment; http only
for a loopback host (a local Ollama); link-local, unspecified, multicast and reserved addresses always
(the cloud metadata address included); loopback and private addresses too on a deployment
(`env != local`). Names are resolved and every address checked before a call that goes to the network
(`check_base_url(..., resolve=True)`); redirects are never followed (`engine.llm_http`).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from engine.config import LlmBackend, LlmConfig, config_root, load_yaml
from engine.connections.base import ConnectorError
from engine.connections.store import connections_key, key_fingerprint
from engine.llm import (
    KEYWORD_HASH_MODEL_ID,
    LOCAL_EMBEDDING_MODEL_ID,
    LLMClient,
    LLMCompletion,
    LLMError,
    LocalEmbeddingClient,
    build_client,
    keyword_hash_vector,
)
from engine.llm_http import AnthropicClient, LLMHttpError, OpenAICompatibleClient
from engine.settings import ENV_VARS, Settings, SettingsError
from engine.storage import LocalStorage, Storage, StorageError
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = [
    "KEY_ENV_VAR",
    "PROVIDERS",
    "SLOTS",
    "SLOT_LABELS",
    "TEST_PROMPT",
    "AiNotConnectedError",
    "AiServiceError",
    "AiServiceRecord",
    "AiServiceState",
    "AiServiceStates",
    "AiServiceStore",
    "Candidate",
    "Effective",
    "KeywordEmbeddingClient",
    "LastTest",
    "ModelList",
    "ProviderInfo",
    "ResolvedAi",
    "ServiceInput",
    "TestOutcome",
    "build_state",
    "build_states",
    "candidate_from",
    "check_base_url",
    "effective_service",
    "key_for",
    "key_store_problem",
    "list_models",
    "provider_infos",
    "resolve_client",
    "run_test",
    "save_service",
    "store_key",
    "uses_embedding_model",
    "yaml_bedrock",
]

_LOGGER = get_logger(__name__)

SLOTS: Final[tuple[str, ...]] = ("product", "deliverable")
SLOT_LABELS: Final[dict[str, str]] = {"product": "Product AI", "deliverable": "Deliverable AI"}
Slot = Literal["product", "deliverable"]


def store_key(slot: str) -> str:
    """`ai_service/<slot>.json`, the record of one slot."""
    return f"ai_service/{slot}.json"


KEY_ENV_VAR: Final[str] = ENV_VARS["connections_key"]
TEST_PROMPT: Final[str] = "Reply with the single word: ready"
_MAX_FIELD: Final[int] = 200
_MAX_KEY: Final[int] = 512
_MAX_URL: Final[int] = 512
_REGION: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$")
_NUMERIC_HOST: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fx.]+$", re.IGNORECASE)
_WRITE_LOCK: Final[threading.Lock] = threading.Lock()

Protocol = Literal["bedrock", "openai", "anthropic"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AiServiceError(Exception):
    """A request about the AI service cannot be done. `field` names an input, never its value."""

    def __init__(
        self, status: int, code: str, message: str, *, fix: str | None = None, field: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fix = fix
        self.field = field


class AiNotConnectedError(Exception):
    """No AI service can answer this call. The API says `409 AI_NOT_CONNECTED`."""

    code = "AI_NOT_CONNECTED"

    def __init__(self, slot: str, message: str | None = None, fix: str | None = None) -> None:
        self.slot = slot
        self.message = message or f"No AI service is connected for {SLOT_LABELS[slot]}."
        self.fix = fix or f"Open Connections → AI service → {SLOT_LABELS[slot]} and connect one."
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class ProviderInfo(BaseModel):
    """One card of the picker: what to ask for, where to get it, and how the service is spoken to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    protocol: Protocol
    default_base_url: str | None = None
    needs_key: bool
    needs_base_url: bool = False
    needs_region: bool = False
    key_hint: str = Field(description="What a key looks like, e.g. `sk-or-…`; empty when none is needed.")
    key_help: str = Field(description="One sentence: where to get one.")
    key_prefixes: tuple[str, ...] = Field(default=(), description="Soft check: warn, never block.")
    model_suggestions: tuple[str, ...] = Field(default=(), description="Examples; the field stays editable.")
    embedding_suggestions: tuple[str, ...] = ()
    supports_embeddings: bool
    local_embedding_model: str = Field(
        default=LOCAL_EMBEDDING_MODEL_ID,
        description="The open-source embedding model this server can run itself, offered with every "
        "provider (it needs the `local-embeddings` extra); naming it as `embedding_model` uses it.",
    )
    third_party: bool = Field(description="True when prompts leave the platform's own AWS account.")


PROVIDERS: Final[dict[str, ProviderInfo]] = {
    p.id: p
    for p in (
        ProviderInfo(
            id="bedrock",
            label="Amazon Bedrock",
            protocol="bedrock",
            needs_key=False,
            needs_region=True,
            key_hint="",
            key_help="No key: Bedrock uses the AWS sign-in of this computer or role (Connections → AWS).",
            supports_embeddings=True,
            third_party=False,
        ),
        ProviderInfo(
            id="openai",
            label="OpenAI",
            protocol="openai",
            default_base_url="https://api.openai.com/v1",
            needs_key=True,
            key_hint="sk-…",
            key_help="Create a key at platform.openai.com under API keys; it starts with sk-.",
            key_prefixes=("sk-",),
            supports_embeddings=True,
            third_party=True,
        ),
        ProviderInfo(
            id="anthropic",
            label="Claude (Anthropic)",
            protocol="anthropic",
            default_base_url="https://api.anthropic.com",
            needs_key=True,
            key_hint="sk-ant-…",
            key_help="Create a key in the Anthropic Console under API keys; it starts with sk-ant-.",
            key_prefixes=("sk-ant-",),
            supports_embeddings=False,
            third_party=True,
        ),
        ProviderInfo(
            id="openrouter",
            label="OpenRouter",
            protocol="openai",
            default_base_url="https://openrouter.ai/api/v1",
            needs_key=True,
            key_hint="sk-or-…",
            key_help="Create a key at openrouter.ai under Keys; it starts with sk-or-.",
            key_prefixes=("sk-or-",),
            supports_embeddings=True,
            third_party=True,
        ),
        ProviderInfo(
            id="huggingface",
            label="Hugging Face",
            protocol="openai",
            default_base_url="https://router.huggingface.co/v1",
            needs_key=True,
            key_hint="hf_…",
            key_help="Create an access token at huggingface.co under Settings → Access Tokens; it starts with hf_.",
            key_prefixes=("hf_",),
            supports_embeddings=False,
            third_party=True,
        ),
        ProviderInfo(
            id="openai_compatible",
            label="Other / local",
            protocol="openai",
            needs_key=False,
            needs_base_url=True,
            key_hint="",
            key_help="Type the server's web address (for example Ollama, vLLM, LiteLLM, Together or Groq); add a key only if it asks for one.",
            supports_embeddings=True,
            third_party=True,
        ),
    )
}
"""The registry. Model examples are data (`configs/ai_service.yaml`), not code (DEC-204)."""

SUGGESTIONS_FILENAME: Final[str] = "ai_service.yaml"


def provider_infos(root: Path | None = None) -> tuple[ProviderInfo, ...]:
    """Every provider, with the model examples of `configs/ai_service.yaml` (none when it is absent)."""
    try:
        document = load_yaml(config_root(root) / SUGGESTIONS_FILENAME)
    except Exception:  # a missing or unreadable file only means no examples
        document = {}
    raw = document.get("suggestions")
    suggestions = raw if isinstance(raw, dict) else {}
    out: list[ProviderInfo] = []
    for info in PROVIDERS.values():
        entry = suggestions.get(info.id)
        entry = entry if isinstance(entry, dict) else {}
        out.append(
            info.model_copy(
                update={
                    "model_suggestions": _strings(entry.get("models")),
                    "embedding_suggestions": _strings(entry.get("embeddings")),
                }
            )
        )
    return tuple(out)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())[:10]


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
def _url_error(message: str) -> AiServiceError:
    return AiServiceError(422, "AI_BASE_URL_INVALID", message, field="base_url")


def _is_loopback_name(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost")


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _refused_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, deployed: bool) -> str | None:
    """Why `address` may not be called, or None."""
    if address.is_loopback:  # a local server: fine on a laptop, meaningless (and risky) on a deployment
        return _NOT_ON_A_DEPLOYMENT if deployed else None
    if address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved:
        return "That address is not one an AI service can have (link-local, unspecified or reserved)."
    if deployed and address.is_private:
        return _NOT_ON_A_DEPLOYMENT
    return None


_NOT_ON_A_DEPLOYMENT: Final[str] = (
    "A deployed Marketing AI cannot call a private or local address; use the service's public address."
)


def check_base_url(url: str, *, env: str = "local", resolve: bool = False) -> str:
    """The address, normalised (no trailing slash), or `AiServiceError` 422 naming `base_url`.

    `resolve` also looks the name up and checks every address it gives: a name that points at the
    metadata service, or at a private network on a deployment, is refused before anything is sent.
    A name that does not resolve is allowed here - the call itself will say it cannot reach it.
    """
    text = url.strip()
    if not text or len(text) > _MAX_URL or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise _url_error("The web address is empty, too long, or has spaces in it.")
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise _url_error("That is not a valid web address.") from None
    if parts.scheme not in {"http", "https"} or not host:
        raise _url_error(
            "The web address must start with https:// (or http:// for a server on this computer)."
        )
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _url_error("The web address must not contain a user name or password; use the key field.")
    if parts.query or parts.fragment:
        raise _url_error("The web address must not contain ? or #.")
    if port is not None and port == 0:
        raise _url_error("That is not a valid web address.")
    deployed = env != "local"
    address = _address(host)
    loopback = _is_loopback_name(host) or (address is not None and address.is_loopback)
    if parts.scheme == "http" and not loopback:
        raise _url_error("Only https:// is allowed, except for a server on this computer (localhost).")
    if address is None and _NUMERIC_HOST.fullmatch(host) and any(c.isdigit() for c in host):
        raise _url_error("Write the address as a name or a standard IP address.")
    if deployed and _is_loopback_name(host):
        raise _url_error(
            "A deployed Marketing AI cannot call a local address; use the service's public address."
        )
    if address is not None:
        reason = _refused_address(address, deployed=deployed)
        if reason is not None:
            raise _url_error(reason)
    elif resolve:
        for resolved in _resolve(host, port or (443 if parts.scheme == "https" else 80)):
            reason = _refused_address(resolved, deployed=deployed)
            if reason is not None:
                raise _url_error(reason)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = _address(str(info[4][0]).split("%", 1)[0])
        if address is not None:
            found.append(address)
    return found


# ---------------------------------------------------------------------------
# The stored record
# ---------------------------------------------------------------------------
class LastTest(BaseModel):
    """What the last test of the saved service said. Never contains a key or a provider's own words."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    message: str
    fix: str | None = None
    latency_ms: int | None = None
    tested_at: datetime


class AiServiceRecord(BaseModel):
    """`ai_service/service.json`. `secrets_token` is the only place the key exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    embedding_model: str = ""
    base_url: str = Field(default="", description="What was typed; empty means the provider's own address.")
    region: str = ""
    secrets_token: str | None = None
    key_fingerprint: str | None = None
    last_test: LastTest | None = None
    updated_at: datetime

    @property
    def has_key(self) -> bool:
        return self.secrets_token is not None


def effective_base_url(provider: ProviderInfo, typed: str) -> str:
    """The address a client calls: what was typed, else the provider's own; empty for Bedrock."""
    return typed or provider.default_base_url or ""


def _wrong_key() -> AiServiceError:
    return AiServiceError(
        409,
        "AI_KEY_UNREADABLE",
        "The saved key of the AI service cannot be read: it was saved with a different encryption key "
        f"({KEY_ENV_VAR}).",
        fix="Set the encryption key back to the one used when it was saved, or enter the key again.",
    )


class AiServiceStore:
    """The saved AI services of an artefact store, one record per slot. Only `api_key` decrypts a key."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    def read(self, slot: str) -> AiServiceRecord | None:
        key = store_key(slot)
        try:
            if not self._storage.exists(key):
                return None
            return self._storage.read_model(key, AiServiceRecord)
        except (StorageError, ValueError):
            _LOGGER.warning("ai_service: the saved %s file is unreadable; treating it as not saved", slot)
            return None

    def active(self, slot: str) -> tuple[AiServiceRecord, str] | None:
        """The record a call for `slot` uses: its own, else None."""
        own = self.read(slot)
        if own is not None:
            return own, slot
        return None

    def key_readable(self, record: AiServiceRecord) -> bool:
        if record.secrets_token is None:
            return True
        try:
            self.api_key(record)
        except (AiServiceError, ConnectorError, SettingsError):
            return False
        return True

    def api_key(self, record: AiServiceRecord) -> str | None:
        """The saved key, decrypted; `AiServiceError` `AI_KEY_UNREADABLE` when the encryption key changed."""
        if record.secrets_token is None:
            return None
        try:
            key = connections_key(self._settings, self._storage)
        except SettingsError as exc:
            raise AiServiceError(409, "AI_KEY_STORE_UNAVAILABLE", exc.message) from None
        if record.key_fingerprint is not None and record.key_fingerprint != key_fingerprint(key):
            raise _wrong_key()
        try:
            data = json.loads(Fernet(key).decrypt(record.secrets_token.encode("ascii")))
            value = data["api_key"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise _wrong_key() from None
        return str(value)

    def seal(self, api_key: str) -> tuple[str, str]:
        """(token, fingerprint) for `api_key`."""
        try:
            key = connections_key(self._settings, self._storage)
        except SettingsError as exc:
            raise AiServiceError(409, "AI_KEY_STORE_UNAVAILABLE", exc.message) from None
        token = Fernet(key).encrypt(json.dumps({"api_key": api_key}).encode("utf-8")).decode("ascii")
        return token, key_fingerprint(key)

    def write(self, slot: str, record: AiServiceRecord) -> None:
        with _WRITE_LOCK:
            self._storage.write_model(store_key(slot), record)

    def record_test(self, slot: str, record: AiServiceRecord, result: LastTest) -> AiServiceRecord:
        """Remember `result` as the slot's last test - only if the slot still holds `record`.

        A test can take as long as the provider's timeout, and someone may save another service or
        disconnect the slot meanwhile. The record is re-read under the write lock: a slot that changed
        or went away is left exactly as it now is, because a result about the old settings must not
        overwrite the new ones or bring a deleted service back.
        """
        with _WRITE_LOCK:
            current = self.read(slot)
            if current is None or _without_test(current) != _without_test(record):
                return current if current is not None else record
            updated = current.model_copy(update={"last_test": result})
            self._storage.write_model(store_key(slot), updated)
            return updated

    def delete(self, slot: str) -> None:
        with _WRITE_LOCK:
            if self._storage.exists(store_key(slot)):
                self._storage.delete(store_key(slot))


def _without_test(record: AiServiceRecord) -> AiServiceRecord:
    return record.model_copy(update={"last_test": None})


def key_store_problem(settings: Settings, storage: Storage) -> str | None:
    """Why a key cannot be saved here, or None. Never creates the local key file."""
    if settings.connections_key is not None:
        return None
    if settings.env == "prod" or not isinstance(storage, LocalStorage):
        return (
            f"Saving an AI service needs {KEY_ENV_VAR} to be set. Ask whoever runs Marketing AI to set it "
            "(the AWS deployment does this for you)."
        )
    return None


# ---------------------------------------------------------------------------
# What a request names
# ---------------------------------------------------------------------------
class ServiceInput(BaseModel):
    """Body of PUT, POST /test and POST /models. Every field optional here; each route says which it needs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    model: str | None = None
    embedding_model: str | None = None
    base_url: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A checked description of a service to save, test or list. `api_key` never appears in `repr`."""

    provider: ProviderInfo
    model: str
    embedding_model: str
    base_url: str  # typed: "" = the provider's own
    region: str
    api_key: str | None

    def __repr__(self) -> str:
        return f"Candidate(provider={self.provider.id!r}, model={self.model!r})"

    @property
    def address(self) -> str:
        return effective_base_url(self.provider, self.base_url)


def _text(field: str, value: str | None, *, required: bool = False, limit: int = _MAX_FIELD) -> str:
    text = (value or "").strip()
    if required and not text:
        raise AiServiceError(422, "AI_FIELD_REQUIRED", f"`{field}` is required.", field=field)
    if len(text) > limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise AiServiceError(
            422, "AI_FIELD_INVALID", f"`{field}` is too long or has invalid characters.", field=field
        )
    return text


def _clean_key(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if len(text) > _MAX_KEY or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text):
        raise AiServiceError(
            422,
            "AI_KEY_INVALID",
            "`api_key` must be one line with no spaces, and no longer than 512 characters.",
            field="api_key",
        )
    return text


def candidate_from(
    body: ServiceInput,
    *,
    saved: AiServiceRecord | None,
    settings: Settings,
    partial: bool,
    resolve: bool = False,
    require_model: bool = True,
) -> Candidate:
    """Check `body`. `partial` (test, models) fills omitted fields from the saved (or inherited) service of the same provider."""
    provider_id = (body.provider or "").strip() or (saved.provider if partial and saved else "")
    if not provider_id:
        raise AiServiceError(422, "AI_FIELD_REQUIRED", "`provider` is required.", field="provider")
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise AiServiceError(
            422,
            "AI_PROVIDER_UNKNOWN",
            f"`provider` must be one of: {', '.join(PROVIDERS)}.",
            field="provider",
        )
    same = partial and saved is not None and saved.provider == provider.id

    def pick(given: str | None, kept: str) -> str | None:
        return given if given is not None else (kept if same else None)

    model = _text("model", pick(body.model, saved.model if saved else ""), required=require_model)
    embedding = _text("embedding_model", pick(body.embedding_model, saved.embedding_model if saved else ""))
    typed_url = _text("base_url", pick(body.base_url, saved.base_url if saved else ""), limit=_MAX_URL)
    region = _text("region", pick(body.region, saved.region if saved else ""), limit=32)
    if provider.needs_base_url and not typed_url:
        raise AiServiceError(
            422, "AI_FIELD_REQUIRED", "`base_url` is required for this provider.", field="base_url"
        )
    if provider.protocol == "bedrock":
        typed_url = ""
        if not region:
            raise AiServiceError(
                422, "AI_FIELD_REQUIRED", "`region` is required for Amazon Bedrock.", field="region"
            )
        if not _REGION.fullmatch(region):
            raise AiServiceError(
                422, "AI_FIELD_INVALID", "`region` is not an AWS region name.", field="region"
            )
    else:
        region = ""
        if typed_url:
            typed_url = check_base_url(typed_url, env=settings.env, resolve=resolve)
        elif resolve and provider.default_base_url:
            check_base_url(provider.default_base_url, env=settings.env, resolve=True)
    key = _clean_key(body.api_key)
    if key is not None and not provider.needs_key and provider.protocol == "bedrock":
        raise AiServiceError(
            422,
            "AI_KEY_NOT_USED",
            "Amazon Bedrock uses the AWS sign-in, not a key. Leave `api_key` out.",
            field="api_key",
        )
    if not uses_embedding_model(provider, embedding):
        embedding = ""
    return Candidate(
        provider=provider,
        model=model,
        embedding_model=embedding,
        base_url=typed_url,
        region=region,
        api_key=key,
    )


def key_for(store: AiServiceStore, saved: AiServiceRecord | None, candidate: Candidate) -> str | None:
    """The key to use for `candidate`: the typed one, else the saved one - but only when it is going to
    the same provider at the same address. A saved key never follows a changed address."""
    if candidate.api_key is not None:
        return candidate.api_key
    if (
        saved is not None
        and saved.secrets_token is not None
        and saved.provider == candidate.provider.id
        and effective_base_url(candidate.provider, saved.base_url) == candidate.address
    ):
        return store.api_key(saved)
    return None


def save_service(
    store: AiServiceStore, slot: str, body: ServiceInput, *, settings: Settings, storage: Storage
) -> AiServiceRecord:
    """Validate and save one slot. No network call. The last test is forgotten.

    The slot's own saved key is kept (blank `api_key`) only for the same provider at the same address; a
    deliverable slot never silently borrows the product slot's key by saving.
    """
    problem = key_store_problem(settings, storage)
    if problem is not None:
        raise AiServiceError(409, "AI_SERVICE_LOCKED", problem)
    saved = store.read(slot)
    candidate = candidate_from(body, saved=saved, settings=settings, partial=False)
    token: str | None = None
    fingerprint: str | None = None
    if candidate.api_key is not None:
        token, fingerprint = store.seal(candidate.api_key)
    elif (
        saved is not None
        and saved.secrets_token is not None
        and saved.provider == candidate.provider.id
        and effective_base_url(candidate.provider, saved.base_url) == candidate.address
        and store.key_readable(saved)
    ):
        token, fingerprint = saved.secrets_token, saved.key_fingerprint
    if candidate.provider.needs_key and token is None:
        raise AiServiceError(
            422,
            "AI_KEY_REQUIRED",
            f"{candidate.provider.label} needs a key. Enter it in `api_key`.",
            fix="Enter the key again." if saved is not None and saved.has_key else None,
            field="api_key",
        )
    record = AiServiceRecord(
        provider=candidate.provider.id,
        model=candidate.model,
        embedding_model=candidate.embedding_model,
        base_url=candidate.base_url,
        region=candidate.region,
        secrets_token=token,
        key_fingerprint=fingerprint,
        last_test=None,
        updated_at=utc_now(),
    )
    store.write(slot, record)
    return record


# ---------------------------------------------------------------------------
# The state the API shows
# ---------------------------------------------------------------------------
class AiServiceState(BaseModel):
    """One slot as `GET /ai-service` shows it. Never a key: `has_key` says whether one is saved.

    `source` says where the answer comes from: `saved` (this slot's own record), `inherited` (a
    deliverable slot with no record of its own, following the product slot's), `config` (a use case's
    own `generative.llm.backend: bedrock`, or - only with `MARKETING_AI_ALLOW_FAKE_AI` - the test model,
    `provider: "fake"`) or `none`. `inherits_product` is true for a deliverable slot
    that has no record of its own - it follows the product slot until it is given one (and, while the
    product slot has nothing either, `source` is `none`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: Slot
    connected: bool
    source: Literal["saved", "inherited", "config", "none"]
    provider: str | None = None
    provider_label: str | None = None
    model: str | None = None
    embedding_model: str | None = None
    base_url: str | None = None
    region: str | None = None
    has_key: bool = False
    key_readable: bool = True
    third_party: bool = False
    last_test: LastTest | None = None
    editable: bool = True
    locked_reason: str | None = None
    inherits_product: bool = False


class AiServiceStates(BaseModel):
    """`GET /ai-service`: both slots and the provider registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slots: dict[str, AiServiceState]
    providers: tuple[ProviderInfo, ...]


def yaml_bedrock(root: Path | None) -> LlmConfig | None:
    """The first use case whose own file says `generative.llm.backend: bedrock` (a deployment's choice)."""
    from engine.config import list_use_case_ids, load_use_case_document

    try:
        for use_case_id in list_use_case_ids(root):
            document = load_use_case_document(use_case_id, root)
            block = (document.get("generative") or {}).get("llm") or {}
            if block.get("backend") == LlmBackend.BEDROCK.value:
                return LlmConfig.model_validate(block)
    except Exception as exc:  # a broken use-case file is reported where it is loaded, not here
        log_failure(_LOGGER, "ai_service.yaml_bedrock", exc, level=logging.DEBUG)
    return None


def build_state(
    storage: Storage, settings: Settings, slot: str, *, configured: LlmConfig | None = None
) -> AiServiceState:
    store = AiServiceStore(storage, settings)
    problem = key_store_problem(settings, storage)
    common: dict[str, Any] = {
        "slot": slot,
        "editable": problem is None,
        "locked_reason": problem,
        "inherits_product": False,
    }
    active = store.active(slot)
    if active is not None:
        record, owner = active
        info = PROVIDERS.get(record.provider)
        readable = store.key_readable(record)
        usable = info is not None and readable and (record.has_key or not info.needs_key)
        return AiServiceState(
            connected=usable,
            source="saved" if owner == slot else "inherited",
            provider=record.provider,
            provider_label=info.label if info else record.provider,
            model=record.model or None,
            embedding_model=record.embedding_model or None,
            base_url=(effective_base_url(info, record.base_url) or None) if info else None,
            region=record.region or None,
            has_key=record.has_key,
            key_readable=readable,
            third_party=True if info is None else info.third_party,
            last_test=record.last_test,
            **common,
        )
    if configured is not None:
        return AiServiceState(
            connected=True,
            source="config",
            provider="bedrock",
            provider_label=PROVIDERS["bedrock"].label,
            model=configured.generation_model_id or None,
            embedding_model=configured.embedding_model_id or None,
            region=configured.region,
            third_party=False,
            **common,
        )
    if settings.allow_fake_ai:
        # Tests and developer checks: the test model answers (`resolve_client` step 4), so the slot is
        # usable and the screens that ask `connected` must agree with the chat box.
        return AiServiceState(
            connected=True,
            source="config",
            provider="fake",
            provider_label="Test model",
            third_party=False,
            **common,
        )
    return AiServiceState(connected=False, source="none", **common)


def build_states(storage: Storage, settings: Settings, root: Path | None = None) -> AiServiceStates:
    configured = yaml_bedrock(root)
    return AiServiceStates(
        slots={slot: build_state(storage, settings, slot, configured=configured) for slot in SLOTS},
        providers=provider_infos(root),
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
class KeywordEmbeddingClient:
    """Wraps a client that has no embeddings: `embed` is a keyword hash, everything else is the client's."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        return self._inner.complete(
            prompt, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
        )

    def embed(
        self,
        texts: Sequence[str],
        *,
        model_id: str | None = None,  # noqa: ARG002 - the protocol's signature; a hash has no model
    ) -> tuple[tuple[float, ...], ...]:
        return tuple(keyword_hash_vector(text) for text in texts)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        return self._inner.count_tokens(text, model_id=model_id)


@dataclass(frozen=True)
class Effective:
    """Which service answers a slot, and the `LlmConfig` (model names) every metered call is made against."""

    llm: LlmConfig
    slot: str
    source: Literal["saved", "inherited", "config", "fake"]
    provider: str
    label: str
    third_party: bool


@dataclass(frozen=True)
class ResolvedAi(Effective):
    client: LLMClient


def _saved_effective(
    llm: LlmConfig, slot: str, record: AiServiceRecord, owner: str, store: AiServiceStore, settings: Settings
) -> tuple[Effective, ProviderInfo, str | None]:
    info = PROVIDERS.get(record.provider)
    if info is None:
        raise AiNotConnectedError(slot, "The saved AI service is not one this version knows.")
    key: str | None = None
    if record.has_key:
        try:
            key = store.api_key(record)
        except AiServiceError as exc:
            raise AiNotConnectedError(
                slot,
                "The saved key of the AI service can no longer be read.",
                f"Open Connections → AI service → {SLOT_LABELS[owner]} and enter the key again.",
            ) from exc
    elif info.needs_key:
        raise AiNotConnectedError(slot, "The saved AI service has no key.")
    if info.protocol != "bedrock":
        try:
            check_base_url(
                effective_base_url(info, record.base_url), env=settings.env, resolve=settings.env != "local"
            )
        except AiServiceError as exc:
            raise AiNotConnectedError(
                slot,
                "The saved AI service's web address is not allowed here.",
                f"Open Connections → AI service → {SLOT_LABELS[owner]} and change it.",
            ) from exc
    embedding = (
        record.embedding_model
        if uses_embedding_model(info, record.embedding_model)
        else KEYWORD_HASH_MODEL_ID
    )
    effective_llm = llm.model_copy(
        update={
            "backend": LlmBackend.BEDROCK if info.protocol == "bedrock" else LlmBackend.EXTERNAL,
            "region": record.region or llm.region,
            "generation_model_id": record.model,
            "judge_model_id": record.model,
            "embedding_model_id": embedding,
        }
    )
    effective = Effective(
        llm=effective_llm,
        slot=slot,
        source="saved" if owner == slot else "inherited",
        provider=info.id,
        label=info.label,
        third_party=info.third_party,
    )
    return effective, info, key


def effective_service(llm: LlmConfig, *, slot: str, storage: Storage, settings: Settings) -> Effective | None:
    """Which service would answer a call for `slot` now (no client built), or None."""
    try:
        return _plan(llm, slot, storage, settings)[0]
    except AiNotConnectedError:
        return None


def _plan(
    llm: LlmConfig, slot: str, storage: Storage, settings: Settings
) -> tuple[Effective, ProviderInfo | None, AiServiceRecord | None, str | None]:
    store = AiServiceStore(storage, settings)
    active = store.active(slot)
    if active is not None:
        record, owner = active
        effective, info, key = _saved_effective(llm, slot, record, owner, store, settings)
        return effective, info, record, key
    if llm.backend is LlmBackend.BEDROCK:
        return (
            Effective(
                llm=llm,
                slot=slot,
                source="config",
                provider="bedrock",
                label="Amazon Bedrock",
                third_party=False,
            ),
            None,
            None,
            None,
        )
    if settings.allow_fake_ai and llm.backend is LlmBackend.FAKE:
        return (
            Effective(
                llm=llm, slot=slot, source="fake", provider="fake", label="Test model", third_party=False
            ),
            None,
            None,
            None,
        )
    raise AiNotConnectedError(slot)


def build_service_client(
    info: ProviderInfo,
    *,
    address: str,
    api_key: str | None,
    model: str,
    embedding_model: str,
    region: str,
    timeout_s: int,
    max_retries: int,
    profile: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LLMClient:
    """A client for one provider. Embeddings are the local model when it is named (DEC-1263), else the
    provider's when it has them and one is named, else a keyword hash."""
    from engine.llm import BedrockLLMClient

    client: LLMClient
    if info.protocol == "bedrock":
        client = BedrockLLMClient(
            region=region,
            model_id=model,
            embedding_model_id=embedding_model,
            timeout_s=timeout_s,
            max_retries=max_retries,
            profile=profile,
        )
    else:
        cls = AnthropicClient if info.protocol == "anthropic" else OpenAICompatibleClient
        client = cls(
            base_url=address,
            api_key=api_key or "",
            model_id=model,
            embedding_model_id=embedding_model,
            timeout_s=timeout_s,
            max_retries=max_retries,
            transport=transport,
        )
    if embedding_model == LOCAL_EMBEDDING_MODEL_ID:
        client = LocalEmbeddingClient(client, model_id=embedding_model)
    elif not uses_embedding_model(info, embedding_model):
        client = KeywordEmbeddingClient(client)
    return client


def uses_embedding_model(info: ProviderInfo, embedding_model: str | None) -> bool:
    """Whether `embedding_model` is used as named rather than replaced by the keyword hash.

    The local model runs on this server, so it is usable with every provider - including the ones
    that have no embeddings of their own, which are the ones that need it most (DEC-1263). Any other
    name is used only with a provider that has embeddings.
    """
    if not embedding_model:
        return False
    return embedding_model == LOCAL_EMBEDDING_MODEL_ID or info.supports_embeddings


def resolve_client(
    llm: LlmConfig,
    *,
    slot: str,
    storage: Storage,
    settings: Settings,
    profile: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ResolvedAi:
    """The client for a language call of `slot`, and the `LlmConfig` to meter it with; see the module
    docstring for the order. `AiNotConnectedError` when nothing can answer."""
    effective, info, record, key = _plan(llm, slot, storage, settings)
    client: LLMClient
    if record is not None and info is not None:
        client = build_service_client(
            info,
            address=effective_base_url(info, record.base_url),
            api_key=key,
            model=record.model,
            embedding_model=record.embedding_model,
            region=record.region,
            timeout_s=llm.timeout_s,
            max_retries=llm.max_retries,
            profile=profile,
            transport=transport,
        )
    else:
        client = build_client(llm, profile=profile)
    return ResolvedAi(
        llm=effective.llm,
        slot=effective.slot,
        source=effective.source,
        provider=effective.provider,
        label=effective.label,
        third_party=effective.third_party,
        client=client,
    )


# ---------------------------------------------------------------------------
# Test and list (network; the API runs these off the event loop)
# ---------------------------------------------------------------------------
BEDROCK_FIX: Final[str] = (
    "Check the AWS sign-in (Connections → AWS), that the model is switched on for this region in the AWS "
    "console (Bedrock → Model access), and that the region and model name are right."
)


class TestOutcome(BaseModel):
    """`POST /ai-service/test`. Messages are ours, never the provider's."""

    __test__ = False  # not a pytest class

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    message: str
    fix: str | None = None
    latency_ms: int | None = None
    model: str | None = None
    embeddings_note: str | None = None


class ModelList(BaseModel):
    """`POST /ai-service/models`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    models: list[str] = Field(default_factory=list, max_length=200)
    note: str | None = None


def _problem_text(exc: LLMError, info: ProviderInfo) -> tuple[str, str | None]:
    if isinstance(exc, LLMHttpError):
        return exc.message, exc.fix
    if info.protocol == "bedrock":
        return "Amazon Bedrock could not answer.", BEDROCK_FIX
    return exc.message, None


def run_test(
    candidate: Candidate,
    api_key: str | None,
    *,
    profile: str | None = None,
    timeout_s: int = 20,
    transport: httpx.BaseTransport | None = None,
) -> TestOutcome:
    """One tiny completion (16 tokens). A failed call is `ok: false` with a plain fix, never an exception."""
    info = candidate.provider
    if info.needs_key and not api_key:
        return TestOutcome(
            ok=False,
            message=f"{info.label} needs a key.",
            fix="Enter the key, then test again.",
            model=candidate.model,
        )
    client = build_service_client(
        info,
        address=candidate.address,
        api_key=api_key,
        model=candidate.model,
        embedding_model=candidate.embedding_model,
        region=candidate.region,
        timeout_s=timeout_s,
        max_retries=0,
        profile=profile,
        transport=transport,
    )
    began = time.monotonic()
    try:
        client.complete(TEST_PROMPT, model_id=candidate.model, max_tokens=16, temperature=0.0)
        latency = int((time.monotonic() - began) * 1000)
        message = f"Connected. {info.label} answered with {candidate.model}."
    except LLMError as exc:
        latency = int((time.monotonic() - began) * 1000)
        if exc.code == "LLM_REFUSED":  # reached and answered, with no text in 16 tokens
            message = f"Connected. {info.label} answered, but with no text in the short test."
        else:
            log_failure(_LOGGER, "ai_service.test", exc, level=logging.DEBUG)
            text, fix = _problem_text(exc, info)
            return TestOutcome(ok=False, message=text, fix=fix, latency_ms=latency, model=candidate.model)
    except Exception as exc:  # a bug or an SDK error must not become a stack trace with a key in it
        log_failure(_LOGGER, "ai_service.test", exc)
        return TestOutcome(
            ok=False,
            message="The test could not be completed.",
            fix=BEDROCK_FIX if info.protocol == "bedrock" else "Check the settings and try again.",
            model=candidate.model,
        )
    return TestOutcome(
        ok=True,
        message=message,
        latency_ms=latency,
        model=candidate.model,
        embeddings_note=_embeddings_note(client, candidate),
    )


def _embeddings_note(client: LLMClient, candidate: Candidate) -> str | None:
    info = candidate.provider
    if not uses_embedding_model(info, candidate.embedding_model):
        return "Document assistant will match by keywords."
    if candidate.embedding_model == LOCAL_EMBEDDING_MODEL_ID:
        try:
            client.embed(["ready"], model_id=candidate.embedding_model)
        except LLMError as exc:  # LocalEmbeddingClient raises only coded, plain-language errors
            return f"The local embedding model is not ready: {exc.message}"
        return None
    try:
        client.embed(["ready"], model_id=candidate.embedding_model)
    except LLMError as exc:
        text, fix = _problem_text(exc, info)
        return f"The embedding model did not answer: {text} {fix or ''}".strip()
    except Exception as exc:
        log_failure(_LOGGER, "ai_service.test_embed", exc, level=logging.DEBUG)
        return "The embedding model did not answer."
    return None


def list_models(
    candidate: Candidate,
    api_key: str | None,
    *,
    profile: str | None = None,
    timeout_s: int = 20,
    transport: httpx.BaseTransport | None = None,
) -> ModelList:
    """The model names the service lists (at most 200), or `[]` and a note. Never raises for a provider."""
    info = candidate.provider
    if info.protocol == "bedrock":
        return _bedrock_models(candidate.region, profile)
    if info.needs_key and not api_key:
        return ModelList(note=f"Enter the {info.label} key first; the list is loaded with it.")
    http_client = build_service_client(
        info,
        address=candidate.address,
        api_key=api_key,
        model=candidate.model,
        embedding_model="",
        region="",
        timeout_s=timeout_s,
        max_retries=0,
        transport=transport,
    )
    inner = http_client._inner if isinstance(http_client, KeywordEmbeddingClient) else http_client
    try:
        return ModelList(models=inner.list_models())  # type: ignore[attr-defined]
    except LLMHttpError as exc:
        log_failure(_LOGGER, "ai_service.models", exc, level=logging.DEBUG)
        if exc.kind == "not_found":
            return ModelList(note="This service does not list its models. Type the model name instead.")
        return ModelList(note=f"Could not load the list. {exc.message} {exc.fix}")
    except Exception as exc:
        log_failure(_LOGGER, "ai_service.models", exc, level=logging.DEBUG)
        return ModelList(note="Could not load the list. Type the model name instead.")


def _bedrock_models(region: str, profile: str | None) -> ModelList:
    try:
        import boto3
        from botocore.config import Config

        session = boto3.Session(profile_name=profile, region_name=region)
        client = session.client(
            "bedrock", config=Config(read_timeout=20, connect_timeout=10, retries={"max_attempts": 1})
        )
        rows = client.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", [])
        ids = sorted({str(row["modelId"]) for row in rows if row.get("modelId")})[:200]
    except Exception as exc:  # best effort: no credentials, no permission, no boto3 - all mean "type it"
        log_failure(_LOGGER, "ai_service.bedrock_models", exc, level=logging.DEBUG)
        return ModelList(
            note="Could not load the list from AWS. Check the AWS sign-in, or type the model name instead."
        )
    return ModelList(models=ids)
