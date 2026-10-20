"""Consent proofs, the registry the gateway checks at send time, and the do-not-disturb registries.

Why this exists. A decision says "contact this person"; the law in most countries says "only if
they agreed, for this purpose, on this channel, and have not taken it back". The dispatcher
already honours the consent flag folded into customer state, but a flag is not proof. A regulator
(India's DPDP Act, the GDPR, the TCPA) asks *when* the person agreed, *where*, to *what*, and what
we kept as evidence. So every send through the gateway carries, or is matched to, a
`ConsentProof`, and the gateway re-checks it at the moment of sending, not when the decision was
made: a STOP that arrived an hour ago wins over a decision made yesterday.

Two kinds of registry live here:

- **`ConsentRegistry`**: proofs we captured (in memory for tests and the Gym, SQL for real runs).
  A proof a caller hands us is trusted only if the registry knows the same consent id with the
  same evidence hash and it has not been revoked. Without a registry the proof is taken at face
  value, which is fine for tests and wrong for production.
- **`DndRegistry`**: numbers whose owners asked for no promotional calls or texts at all. In India
  that is the National Customer Preference Register (NCPR, "DND"): a promotional SMS to a number
  on it is unlawful whatever consent we think we have, unless the person gave explicit consent
  recorded on the operators' DLT platform. The file-backed version reads the scrubbed list the
  operator or an aggregator exports (one number per line, optionally with the blocked
  preference categories).

Addresses are compared after `normalize_address`: phone numbers as digits with a country code,
emails lower-cased, so "+91 98123-45678", "09812345678" and "919812345678" are one person.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field
from sqlalchemy import Column, DateTime, MetaData, String, Table, and_, insert, select, update
from sqlalchemy.engine import Engine

from autonomy.contracts import Channel, Contract

__all__ = [
    "PHONE_CHANNELS",
    "ConsentCheck",
    "ConsentProblem",
    "ConsentProof",
    "ConsentRegistry",
    "DndRegistry",
    "FileDndRegistry",
    "InMemoryConsentRegistry",
    "InMemoryDndRegistry",
    "SqlConsentRegistry",
    "evidence_hash",
    "normalize_address",
    "normalize_msisdn",
]

PHONE_CHANNELS: frozenset[Channel] = frozenset({Channel.SMS, Channel.WHATSAPP})
"""Channels whose address is a phone number."""

DEFAULT_COUNTRY_CODE = "91"
"""The calling code assumed for a bare national number. India first, as the product is."""


# --- addresses -----------------------------------------------------------------------------------


def normalize_msisdn(number: str, default_country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """A phone number as digits with its country code: "+91 98123-45678" -> "919812345678".

    A leading "00" is the international prefix; a single leading "0" is a national trunk prefix;
    a 10-digit number without a code gets `default_country_code`. Anything else is kept as digits.
    """
    digits = re.sub(r"\D", "", number)
    if digits.startswith("00"):
        return digits[2:]
    if number.strip().startswith("+"):
        return digits
    if len(digits) == 11 and digits.startswith("0"):
        return default_country_code + digits[1:]
    if len(digits) == 10:
        return default_country_code + digits
    return digits


def normalize_address(
    channel: Channel, address: str, default_country_code: str = DEFAULT_COUNTRY_CODE
) -> str:
    """The comparable form of an address on a channel (see the module docstring)."""
    if channel in PHONE_CHANNELS:
        return normalize_msisdn(address, default_country_code)
    if channel is Channel.EMAIL:
        return address.strip().lower()
    return address.strip()


def evidence_hash(evidence: bytes | str) -> str:
    """The SHA-256 hex digest we keep instead of the evidence itself (a form post, a recording...)."""
    data = evidence.encode("utf-8") if isinstance(evidence, str) else evidence
    return hashlib.sha256(data).hexdigest()


# --- the proof -----------------------------------------------------------------------------------


class ConsentProof(Contract):
    """One person's agreement to be contacted, as captured, with a hash of the evidence."""

    consent_id: str
    purpose: str = Field(description="What they agreed to, e.g. 'promotional', 'transactional', 'service'")
    channel: Channel
    captured_at: AwareDatetime
    source: str = Field(description="Where it was captured: 'web_form', 'checkout', 'dlt_portal', 'ivr'...")
    evidence_hash: str = Field(description="SHA-256 hex of the evidence; the evidence stays with the client")
    recipient: str | None = Field(default=None, description="The address it covers; None if not recorded")
    customer_id: str | None = None
    expires_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None


class ConsentProblem(StrEnum):
    """Why a proof does not cover a send. Stable codes: the gateway reports them as refusal reasons."""

    MISSING = "consent_missing"
    CHANNEL_MISMATCH = "consent_channel_mismatch"
    PURPOSE_MISMATCH = "consent_purpose_mismatch"
    RECIPIENT_MISMATCH = "consent_recipient_mismatch"
    NOT_YET_VALID = "consent_not_yet_valid"
    EXPIRED = "consent_expired"
    REVOKED = "consent_revoked"
    UNKNOWN = "consent_unknown"
    EVIDENCE_MISMATCH = "consent_evidence_mismatch"


@dataclass(frozen=True)
class ConsentCheck:
    ok: bool
    problem: ConsentProblem | None = None
    proof: ConsentProof | None = None

    @property
    def reason(self) -> str:
        return "" if self.problem is None else self.problem.value


def check_proof(
    proof: ConsentProof | None,
    *,
    channel: Channel,
    purpose: str,
    recipient: str,
    now: datetime,
    max_age: timedelta | None = None,
) -> ConsentCheck:
    """Does this proof, on its face, cover a `purpose` message on `channel` to `recipient` at `now`?

    `recipient` must already be normalised. `max_age` is a pack's limit on how old a consent may be
    (India's inquiry-based consent lasts 7 days, for example).
    """
    if proof is None:
        return ConsentCheck(False, ConsentProblem.MISSING)
    if proof.channel is not channel:
        return ConsentCheck(False, ConsentProblem.CHANNEL_MISMATCH, proof)
    if proof.purpose != purpose:
        return ConsentCheck(False, ConsentProblem.PURPOSE_MISMATCH, proof)
    if proof.recipient is not None and normalize_address(channel, proof.recipient) != recipient:
        return ConsentCheck(False, ConsentProblem.RECIPIENT_MISMATCH, proof)
    if proof.captured_at > now:
        return ConsentCheck(False, ConsentProblem.NOT_YET_VALID, proof)
    if proof.revoked_at is not None and proof.revoked_at <= now:
        return ConsentCheck(False, ConsentProblem.REVOKED, proof)
    if proof.expires_at is not None and proof.expires_at <= now:
        return ConsentCheck(False, ConsentProblem.EXPIRED, proof)
    if max_age is not None and now - proof.captured_at > max_age:
        return ConsentCheck(False, ConsentProblem.EXPIRED, proof)
    return ConsentCheck(True, None, proof)


# --- the consent registry ------------------------------------------------------------------------


@runtime_checkable
class ConsentRegistry(Protocol):
    """Proofs we captured. The gateway trusts a proof only through this, when one is configured."""

    def record(self, proof: ConsentProof) -> None:
        """Store a proof; a second one with the same consent id replaces the first."""
        ...

    def get(self, consent_id: str) -> ConsentProof | None: ...

    def revoke(self, consent_id: str, at: datetime) -> bool:
        """Mark one proof revoked from `at`; False if we never had it."""
        ...

    def revoke_recipient(self, channel: Channel, recipient: str, at: datetime) -> int:
        """Revoke every live proof for this (normalised) address on this channel; return how many."""
        ...

    def find(self, channel: Channel, recipient: str, purpose: str, now: datetime) -> ConsentProof | None:
        """A live proof for this address, channel and purpose, newest first; None if there is none."""
        ...


def verify_against(
    registry: ConsentRegistry | None,
    proof: ConsentProof | None,
    *,
    channel: Channel,
    purpose: str,
    recipient: str,
    now: datetime,
    max_age: timedelta | None = None,
) -> ConsentCheck:
    """The full check: the proof on its face, then (with a registry) the registry's copy of it.

    With a registry and no proof in the request, a live proof on file for the address counts: a
    decision from the inner loop does not carry one, but we may well hold it.
    """
    if registry is not None:
        if proof is None:
            proof = registry.find(channel, recipient, purpose, now)
        else:
            stored = registry.get(proof.consent_id)
            if stored is None:
                return ConsentCheck(False, ConsentProblem.UNKNOWN, proof)
            if stored.evidence_hash != proof.evidence_hash:
                return ConsentCheck(False, ConsentProblem.EVIDENCE_MISMATCH, proof)
            proof = stored  # the registry's copy carries any revocation
    return check_proof(proof, channel=channel, purpose=purpose, recipient=recipient, now=now, max_age=max_age)


def _live(proof: ConsentProof, now: datetime) -> bool:
    if proof.captured_at > now:
        return False
    if proof.revoked_at is not None and proof.revoked_at <= now:
        return False
    return proof.expires_at is None or proof.expires_at > now


class InMemoryConsentRegistry:
    """A consent registry in a dict. Thread-safe, because API requests arrive on several threads."""

    def __init__(self, proofs: Iterable[ConsentProof] = ()) -> None:
        self._proofs: dict[str, ConsentProof] = {}
        self._lock = threading.Lock()
        for proof in proofs:
            self.record(proof)

    def record(self, proof: ConsentProof) -> None:
        with self._lock:
            self._proofs[proof.consent_id] = proof

    def get(self, consent_id: str) -> ConsentProof | None:
        with self._lock:
            return self._proofs.get(consent_id)

    def revoke(self, consent_id: str, at: datetime) -> bool:
        with self._lock:
            proof = self._proofs.get(consent_id)
            if proof is None:
                return False
            if proof.revoked_at is None or proof.revoked_at > at:
                self._proofs[consent_id] = proof.model_copy(update={"revoked_at": at})
            return True

    def revoke_recipient(self, channel: Channel, recipient: str, at: datetime) -> int:
        with self._lock:
            hits = [
                p
                for p in self._proofs.values()
                if p.channel is channel
                and p.recipient is not None
                and normalize_address(channel, p.recipient) == recipient
                and p.revoked_at is None
            ]
            for proof in hits:
                self._proofs[proof.consent_id] = proof.model_copy(update={"revoked_at": at})
            return len(hits)

    def find(self, channel: Channel, recipient: str, purpose: str, now: datetime) -> ConsentProof | None:
        with self._lock:
            hits = [
                p
                for p in self._proofs.values()
                if p.channel is channel
                and p.purpose == purpose
                and p.recipient is not None
                and normalize_address(channel, p.recipient) == recipient
                and _live(p, now)
            ]
        return max(hits, key=lambda p: (p.captured_at, p.consent_id), default=None)

    def __iter__(self) -> Iterator[ConsentProof]:
        with self._lock:
            return iter(list(self._proofs.values()))


_metadata = MetaData()
consent_proof_table = Table(
    "v2_consent_proof",
    _metadata,
    Column("consent_id", String(200), primary_key=True),
    Column("channel", String(20), nullable=False, index=True),
    Column("recipient", String(320), nullable=True, index=True),
    Column("purpose", String(64), nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("body", String, nullable=False),
)
"""One row per proof. `recipient` is stored normalised so `find` is an indexed equality lookup;
`body` is the proof as JSON, the source of truth the other columns are read from."""


class SqlConsentRegistry:
    """The consent registry in a SQL table (SQLite in tests, Postgres in production).

    The table is created on first use when it is missing. Revocation writes both the column and the
    JSON body, so `get` and `find` agree. Times go through the JSON body, which keeps time zones on
    SQLite (whose DateTime drops them); the columns serve the filters only.
    """

    def __init__(self, engine: Engine, *, create: bool = True) -> None:
        self._engine = engine
        if create:
            _metadata.create_all(engine, tables=[consent_proof_table])

    @staticmethod
    def _row(proof: ConsentProof) -> dict[str, object]:
        recipient = None if proof.recipient is None else normalize_address(proof.channel, proof.recipient)
        return {
            "consent_id": proof.consent_id,
            "channel": proof.channel.value,
            "recipient": recipient,
            "purpose": proof.purpose,
            "captured_at": proof.captured_at,
            "expires_at": proof.expires_at,
            "revoked_at": proof.revoked_at,
            "body": proof.model_dump_json(),
        }

    def record(self, proof: ConsentProof) -> None:
        t = consent_proof_table
        with self._engine.begin() as conn:
            exists = conn.execute(select(t.c.consent_id).where(t.c.consent_id == proof.consent_id)).first()
            row = self._row(proof)
            if exists is None:
                conn.execute(insert(t).values(**row))
            else:
                conn.execute(update(t).where(t.c.consent_id == proof.consent_id).values(**row))

    def get(self, consent_id: str) -> ConsentProof | None:
        t = consent_proof_table
        with self._engine.connect() as conn:
            body = conn.execute(select(t.c.body).where(t.c.consent_id == consent_id)).scalar_one_or_none()
        return None if body is None else ConsentProof.model_validate_json(body)

    def revoke(self, consent_id: str, at: datetime) -> bool:
        proof = self.get(consent_id)
        if proof is None:
            return False
        if proof.revoked_at is None or proof.revoked_at > at:
            self.record(proof.model_copy(update={"revoked_at": at}))
        return True

    def revoke_recipient(self, channel: Channel, recipient: str, at: datetime) -> int:
        t = consent_proof_table
        with self._engine.connect() as conn:
            bodies = conn.execute(
                select(t.c.body).where(
                    and_(t.c.channel == channel.value, t.c.recipient == recipient, t.c.revoked_at.is_(None))
                )
            ).scalars()
            proofs = [ConsentProof.model_validate_json(b) for b in bodies]
        for proof in proofs:
            self.record(proof.model_copy(update={"revoked_at": at}))
        return len(proofs)

    def find(self, channel: Channel, recipient: str, purpose: str, now: datetime) -> ConsentProof | None:
        t = consent_proof_table
        with self._engine.connect() as conn:
            bodies = conn.execute(
                select(t.c.body).where(
                    and_(t.c.channel == channel.value, t.c.recipient == recipient, t.c.purpose == purpose)
                )
            ).scalars()
            proofs = [ConsentProof.model_validate_json(b) for b in bodies]
        live = [p for p in proofs if _live(p, now)]
        return max(live, key=lambda p: (p.captured_at, p.consent_id), default=None)


# --- do-not-disturb registries -------------------------------------------------------------------


@runtime_checkable
class DndRegistry(Protocol):
    """Numbers whose owners refused promotional calls and texts (India: NCPR)."""

    def blocks(self, number: str, category: str | None = None) -> bool:
        """True if `number` (normalised) refuses messages of this preference category.

        A number registered as "fully blocked" refuses every category. One registered for some
        categories only (NCPR's 1-7: banking, real estate, education, health, consumer goods,
        communication/entertainment, tourism) refuses those; with `category=None` any
        registration at all counts, which is the safe reading when the category is unknown.
        """
        ...


FULLY_BLOCKED = "0"
"""NCPR's preference code for "block all commercial communication"."""


class InMemoryDndRegistry:
    """A DND registry from a mapping of number -> blocked categories (empty or {"0"} = all)."""

    def __init__(self, numbers: Mapping[str, Iterable[str]] | Iterable[str] = ()) -> None:
        self._numbers: dict[str, frozenset[str]] = {}
        if isinstance(numbers, Mapping):
            for number, cats in numbers.items():
                self.add(number, cats)
        else:
            for number in numbers:
                self.add(number)

    def add(self, number: str, categories: Iterable[str] = ()) -> None:
        self._numbers[normalize_msisdn(number)] = frozenset(c.strip() for c in categories if c.strip())

    def blocks(self, number: str, category: str | None = None) -> bool:
        cats = self._numbers.get(normalize_msisdn(number))
        if cats is None:
            return False
        if not cats or FULLY_BLOCKED in cats or category is None:
            return True
        return category in cats

    def __len__(self) -> int:
        return len(self._numbers)


class FileDndRegistry(InMemoryDndRegistry):
    """A DND registry read from a text file, as an operator or aggregator exports the scrub list.

    One number per line; `#` starts a comment; a line may add the blocked categories after a comma,
    separated by `;` or spaces (`919812345678,1;3`). A number with no categories is fully blocked.
    `reload()` re-reads the file, so a nightly refresh needs no restart.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.reload()

    def reload(self) -> int:
        """Re-read the file; return how many numbers it lists."""
        numbers: dict[str, frozenset[str]] = {}
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            number, _, rest = line.partition(",")
            digits = normalize_msisdn(number)
            if not digits:
                continue
            numbers[digits] = frozenset(c for c in re.split(r"[;\s]+", rest.strip()) if c)
        self._numbers = numbers
        return len(numbers)
