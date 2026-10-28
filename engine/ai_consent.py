"""Consent for every AI call: one record per client, one gate every provider call passes.

**The rule.** A call to a language-model provider that carries a client's data (a case, an upload, a document,
a run, a client id) is made only after someone with the right role has said once, for that client, that the
AI may read it. The yes is kept at `cases/<client_id>/ai_consent.json` with who gave it and when
(:class:`AiConsentStore`); the Data Doctor's "Let the Doctor's AI read masked summaries" button writes it, and
it is the only place consent is recorded.

**The gate.** :func:`engine.ai_service.resolve_client` is how the product gets a provider client, and it hands
back a :class:`ConsentGatedClient`: every ``complete``, ``embed`` and ``count_tokens`` looks for the consent
first and, without it, raises :class:`AiConsentRequiredError` before a byte leaves the server. The caller must
say what the call carries, with a :data:`ConsentScope` and no default, so a new caller cannot skip consent by
forgetting it:

* :class:`ClientData` names the client whose data the call carries.
* :class:`NoClientData` says, in a sentence, why the call carries none (the settings "test connection" call
  sends a fixed greeting). It is the one explicit way out and shows in review.

`tests/unit/architecture/test_ai_consent_gate.py` fails when a module builds a provider client, or a metered
client, without going through this gate. A refusal says the code and a plain sentence; it names no client and
no data, and nothing else is logged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.doctor.case import (
    CASE_FILENAME,
    CASE_PREFIX,
    CLIENT_ID_PATTERN,
    DEFAULT_CLIENT_ID,
    CaseError,
    CaseStore,
)
from engine.llm import LLMError
from engine.storage import Storage, StorageError

if TYPE_CHECKING:
    from engine.llm import LLMClient, LLMCompletion

__all__ = [
    "AI_CONSENT_REQUIRED",
    "AI_CONSENT_REQUIRED_MESSAGE",
    "WORKSPACE_CLIENT",
    "AiConsent",
    "AiConsentRequiredError",
    "AiConsentStore",
    "ClientData",
    "ClientsData",
    "ConsentGatedClient",
    "ConsentScope",
    "NoClientData",
    "client_scope",
    "consent_key",
]

AI_CONSENT_REQUIRED: Final[str] = "AI_CONSENT_REQUIRED"
AI_CONSENT_REQUIRED_MESSAGE: Final[str] = (
    "The AI has not been allowed to read this client's data yet. Allow it once for this client "
    '(Data Doctor, "Let the Doctor\'s AI read masked summaries") and ask again.'
)
CONSENT_FILENAME: Final[str] = "ai_consent.json"
WORKSPACE_CLIENT: Final[str] = DEFAULT_CLIENT_ID
"""The client of work that no case names (an upload, a document index, a run): this deployment's workspace,
the same `default` a case carries when nobody names a client."""


def consent_key(client_id: str) -> str:
    return f"{CASE_PREFIX}{client_id}/{CONSENT_FILENAME}"


class AiConsent(StrictBase):
    """`cases/<client_id>/ai_consent.json`: the client let the AI read masked summaries of its data."""

    client_id: str
    given_by: str = Field(description="Who said yes (user id).")
    given_at: AwareDatetime


class AiConsentStore:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def get(self, client_id: str) -> AiConsent | None:
        if re.fullmatch(CLIENT_ID_PATTERN, client_id) is None:
            return None
        key = consent_key(client_id)
        try:
            return self._storage.read_model(key, AiConsent) if self._storage.exists(key) else None
        except (StorageError, ValueError):
            return None

    def give(self, client_id: str, *, actor: str, at: datetime) -> AiConsent:
        """Record the yes. Asked again, the first one stands (who and when are not rewritten)."""
        if re.fullmatch(CLIENT_ID_PATTERN, client_id) is None:
            raise ValueError("client_id is not a client id")
        found = self.get(client_id)
        if found is not None:
            return found
        consent = AiConsent(client_id=client_id, given_by=actor, given_at=at)
        self._storage.write_model(consent_key(client_id), consent)
        return consent


@dataclass(frozen=True)
class ClientData:
    """The call carries this client's data (masked or not): it needs that client's consent."""

    client_id: str


@dataclass(frozen=True)
class ClientsData:
    """The call carries data that belongs to several clients (an upload that sits in more than one client's case):
    every one of them must have consented."""

    client_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.client_ids:
            raise ValueError("ClientsData needs at least one client")


@dataclass(frozen=True)
class NoClientData:
    """The call carries no client data, and `purpose` says why in a sentence (a fixed greeting, a model list)."""

    purpose: str

    def __post_init__(self) -> None:
        if not self.purpose.strip():
            raise ValueError("NoClientData needs the reason the call carries no client data")


ConsentScope = ClientData | ClientsData | NoClientData


class AiConsentRequiredError(LLMError):
    """The client has not allowed the AI to read its data. Carries the code and a plain sentence, no client id.

    An `LLMError`, so every place that already treats "the AI service did not answer" as "work from the rules"
    (the chats, the review, the explanation) does that here too, and logs the code and nothing else.
    """

    def __init__(self) -> None:
        super().__init__(AI_CONSENT_REQUIRED, AI_CONSENT_REQUIRED_MESSAGE)


def client_scope(
    storage: Storage, *, client_id: str | None = None, upload_ids: Sequence[str] = ()
) -> ClientData | ClientsData:
    """Whose data a call carries, read from the data itself.

    A named `client_id` (a run on a built dataset) is that client. Otherwise the clients are those of every case
    that holds one of `upload_ids`; an upload no case names belongs to the workspace's `default` client.
    """
    if client_id is not None:
        return ClientData(client_id)
    wanted = set(upload_ids)
    found: set[str] = set()
    if wanted:
        cases = CaseStore(storage)
        for key in storage.list_keys(CASE_PREFIX):
            parts = key.split("/")
            if len(parts) != 4 or parts[3] != CASE_FILENAME:
                continue
            try:
                case = cases.get(parts[1], parts[2])
            except CaseError:
                continue
            if wanted.intersection(case.upload_ids()):
                found.add(case.client_id)
    if not found:
        return ClientData(WORKSPACE_CLIENT)
    ordered = tuple(sorted(found))
    return ClientData(ordered[0]) if len(ordered) == 1 else ClientsData(ordered)


class ConsentGatedClient:
    """A provider client that refuses every call carrying a client's data until that client has consented.

    The look is made on each call, before the inner client is touched, so a consent that is not there stops the
    call whoever built the meter around it. :meth:`require` is the same look for a caller that wants to refuse
    early, before it stores anything.
    """

    def __init__(self, inner: LLMClient, *, scope: ConsentScope, storage: Storage | None = None) -> None:
        if isinstance(scope, ClientData | ClientsData) and storage is None:
            raise ValueError("a call that carries a client's data needs the store that holds the consent")
        self._inner = inner
        self._scope = scope
        self._store = AiConsentStore(storage) if storage is not None else None

    @property
    def inner(self) -> LLMClient:
        """The provider client behind the gate (for tests and diagnostics; calls go through the gate)."""
        return self._inner

    @property
    def scope(self) -> ConsentScope:
        return self._scope

    def require(self) -> None:
        """Raise :class:`AiConsentRequiredError` unless the call's client has consented (or it carries none)."""
        if isinstance(self._scope, ClientData | ClientsData):
            assert self._store is not None
            ids = (self._scope.client_id,) if isinstance(self._scope, ClientData) else self._scope.client_ids
            if any(self._store.get(client_id) is None for client_id in ids):
                raise AiConsentRequiredError

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        self.require()
        return self._inner.complete(
            prompt, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
        )

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        self.require()
        return self._inner.embed(texts, model_id=model_id)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        self.require()
        return self._inner.count_tokens(text, model_id=model_id)
