"""Hashing, signatures and the hash-chained ledger that make a mandate tamper-evident.

**The body hash.** A mandate's body is everything except `signers` and `status`, dumped to JSON
mode and written as canonical JSON (keys sorted, no whitespace, UTF-8, tuples as lists, times in
UTC ISO 8601). `body_hash` is its SHA-256. Change one limit, one channel or one date and the hash
changes, so every signature over the old hash stops verifying.

**Signatures.** A signer signs `signature_message(...)`: the canonical JSON of the body hash plus the
person, role, key id, algorithm and time. Two algorithms are supported:

* **Ed25519** (`Ed25519Signer`) when the `cryptography` package is importable. Verifiers hold only
  public keys, so the server that checks a signature cannot forge one. This is the target.
* **HMAC-SHA256** (`HmacSigner`), stdlib only, with one secret per signer. The light v2 CI and any
  install without `cryptography` use it. Its weakness: whoever verifies holds the secret and could
  sign too. *Upgrade path:* install `cryptography` (already pinned for v1), issue each signer an
  Ed25519 key (on their device or in a KMS), register the public key in the `KeyRing` with
  `add_ed25519`, and new signatures carry `algorithm="ed25519"`. Nothing else changes: the
  signature record, the message and the ledger are the same, and old HMAC signatures stay
  checkable for as long as their secrets are kept.

**Keys belong to people.** A `KeyRing` binds each key id to one person and the roles they may sign
in. A signature verifies only when its key is known, its `person_id` is that key's owner, its role
is one the key may use, and the bytes check out. Without this binding one person with two keys
could pass the two-person rule alone.

**The ledger.** `MandateLedger` is append-only and hash-chained, the approach of v1's
`engine/agentic/ledger.py`, reimplemented here with the stdlib so the v2 core stays light:

    entry_hash = sha256( prev_hash || canonical({seq, kind, actor, at, mandate_id, version,
                                                  body_hash, mandate}) )

Every entry records a full snapshot of the mandate at that moment (created, signed, activated,
revoked, ...). Editing any field of any entry changes its hash, which then no longer matches the
next entry's `prev_hash`; `verify_chain()` walks the chain and names the first bad entry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Any, Final, Literal, Protocol

from pydantic import AwareDatetime, Field, field_validator

from autonomy.contracts import Contract
from autonomy.mandate.models import (
    Mandate,
    MandateSignature,
    MandateStatus,
    SignatureAlgorithm,
    SignaturePolicy,
    SignerRole,
    to_utc,
)

__all__ = [
    "BAD_SIGNATURE",
    "BODY_HASH_MISMATCH",
    "GENESIS_HASH",
    "ChainReport",
    "Ed25519Signer",
    "HmacSigner",
    "KeyRing",
    "LedgerEntry",
    "LedgerKind",
    "MandateLedger",
    "Signer",
    "body_hash",
    "canonical_bytes",
    "canonical_json",
    "ed25519_available",
    "mandate_body",
    "sign_mandate",
    "signature_message",
    "verify_entries",
    "verify_mandate",
    "verify_signature",
    "with_signature",
]

GENESIS_HASH: Final[str] = "0" * 64

# Codes `verify_mandate` returns besides the policy's own.
BAD_SIGNATURE = "bad_signature"
BODY_HASH_MISMATCH = "body_hash_mismatch"

_BODY_EXCLUDES: Final[frozenset[str]] = frozenset({"signers", "status"})


# --- canonical JSON --------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """`value` as plain JSON types; anything without one defined spelling is refused."""
    if isinstance(value, Contract):
        return _plain(value.model_dump(mode="json"))
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON has no spelling for NaN or infinity")
        return value
    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, datetime):
        return to_utc(value).isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError("canonical JSON keys are text")
            out[k] = _plain(v)
        return out
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    raise TypeError(f"{type(value).__name__} has no canonical JSON form")


def canonical_json(value: Any) -> str:
    """Keys sorted, no insignificant whitespace, UTF-8 as itself. The one form every hash uses."""
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mandate_body(mandate: Mandate) -> dict[str, Any]:
    """The signed part of a mandate: every field except `signers` and `status`."""
    dumped = mandate.model_dump(mode="json", exclude=set(_BODY_EXCLUDES))
    plain: dict[str, Any] = _plain(dumped)
    return plain


def body_hash(mandate: Mandate) -> str:
    """SHA-256 of the canonical body: what every signature on this version covers."""
    return _sha256(canonical_bytes(mandate_body(mandate)))


def signature_message(
    *,
    body_hash_hex: str,
    person_id: str,
    role: SignerRole,
    key_id: str,
    algorithm: SignatureAlgorithm,
    signed_at: datetime,
) -> bytes:
    """The exact bytes a signer signs. Binding role, person and time stops a signature being reused."""
    return canonical_bytes(
        {
            "purpose": "autonomy.mandate.v1",
            "body_hash": body_hash_hex,
            "person_id": person_id,
            "role": role.value,
            "key_id": key_id,
            "algorithm": algorithm.value,
            "signed_at": to_utc(signed_at).isoformat(),
        }
    )


# --- signers and the key ring -----------------------------------------------------------------------


class Signer(Protocol):
    """Holds one private key (or secret) and signs bytes with it."""

    @property
    def key_id(self) -> str: ...

    @property
    def algorithm(self) -> SignatureAlgorithm: ...

    def sign(self, message: bytes) -> str: ...


def ed25519_available() -> bool:
    """True when the `cryptography` package (and so Ed25519) can be imported here."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    except ImportError:
        return False
    return True


class HmacSigner:
    """HMAC-SHA256 under one signer's own secret. The stdlib fallback; see the module docstring."""

    def __init__(self, key_id: str, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("an HMAC signing secret needs at least 16 bytes")
        self._key_id = key_id
        self._secret = secret

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def algorithm(self) -> SignatureAlgorithm:
        return SignatureAlgorithm.HMAC_SHA256

    def sign(self, message: bytes) -> str:
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()


class Ed25519Signer:
    """Ed25519 from a 32-byte seed. Needs `cryptography`; imported here so the core never loads it."""

    def __init__(self, key_id: str, seed: bytes) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if len(seed) != 32:
            raise ValueError("an Ed25519 seed is 32 bytes")
        self._key_id = key_id
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def algorithm(self) -> SignatureAlgorithm:
        return SignatureAlgorithm.ED25519

    def public_key_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        raw: bytes = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return raw

    def sign(self, message: bytes) -> str:
        sig: bytes = self._key.sign(message)
        return base64.b64encode(sig).decode("ascii")


@dataclass(frozen=True)
class _Key:
    person_id: str
    roles: frozenset[SignerRole]
    algorithm: SignatureAlgorithm
    material: bytes  # the HMAC secret, or the Ed25519 public key


@dataclass
class KeyRing:
    """What a verifier knows: each key id, the one person it belongs to and the roles it may sign in."""

    _keys: dict[str, _Key] = field(default_factory=dict)

    def add_hmac(self, key_id: str, secret: bytes, *, person_id: str, roles: Iterable[SignerRole]) -> None:
        self._add(key_id, _Key(person_id, frozenset(roles), SignatureAlgorithm.HMAC_SHA256, secret))

    def add_ed25519(
        self, key_id: str, public_key: bytes, *, person_id: str, roles: Iterable[SignerRole]
    ) -> None:
        if len(public_key) != 32:
            raise ValueError("an Ed25519 public key is 32 bytes")
        self._add(key_id, _Key(person_id, frozenset(roles), SignatureAlgorithm.ED25519, public_key))

    def _add(self, key_id: str, key: _Key) -> None:
        if key_id in self._keys:
            raise ValueError(f"key {key_id!r} is already registered")
        self._keys[key_id] = key

    def owner(self, key_id: str) -> str | None:
        key = self._keys.get(key_id)
        return key.person_id if key is not None else None

    def verify(self, sig: MandateSignature) -> bool:
        """True when the key is known, belongs to the person, may sign in the role, and the bytes check."""
        key = self._keys.get(sig.key_id)
        if key is None or key.person_id != sig.person_id or sig.role not in key.roles:
            return False
        if key.algorithm is not sig.algorithm:
            return False
        message = signature_message(
            body_hash_hex=sig.body_hash,
            person_id=sig.person_id,
            role=sig.role,
            key_id=sig.key_id,
            algorithm=sig.algorithm,
            signed_at=sig.signed_at,
        )
        if key.algorithm is SignatureAlgorithm.HMAC_SHA256:
            expected = hmac.new(key.material, message, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, sig.signature)
        return _ed25519_verify(key.material, message, sig.signature)


def _ed25519_verify(public_key: bytes, message: bytes, signature: str) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False  # cannot check it here, so it does not count
    try:
        raw = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(raw, message)
    except (InvalidSignature, ValueError):
        return False
    return True


# --- signing a mandate --------------------------------------------------------------------------------


def sign_mandate(
    mandate: Mandate, signer: Signer, *, person_id: str, role: SignerRole, now: datetime
) -> MandateSignature:
    """One person's signature over this mandate's current body."""
    h = body_hash(mandate)
    at = to_utc(now)
    message = signature_message(
        body_hash_hex=h,
        person_id=person_id,
        role=role,
        key_id=signer.key_id,
        algorithm=signer.algorithm,
        signed_at=at,
    )
    return MandateSignature(
        person_id=person_id,
        role=role,
        key_id=signer.key_id,
        algorithm=signer.algorithm,
        body_hash=h,
        signed_at=at,
        signature=signer.sign(message),
    )


def verify_signature(mandate: Mandate, sig: MandateSignature, keys: KeyRing) -> bool:
    """True when `sig` is genuine and covers this mandate's body as it is now."""
    return sig.body_hash == body_hash(mandate) and keys.verify(sig)


def verify_mandate(mandate: Mandate, policy: SignaturePolicy, keys: KeyRing) -> list[str]:
    """Every reason this mandate's signatures do not authorise it; empty when they do.

    A signature over another body is `body_hash_mismatch`; one that fails the key check is
    `bad_signature`. Only signatures that pass both count towards the policy, whose unmet codes
    (missing roles, the two-person rule) follow.
    """
    h = body_hash(mandate)
    codes: list[str] = []
    good: list[MandateSignature] = []
    for s in mandate.signers:
        if s.body_hash != h:
            codes.append(BODY_HASH_MISMATCH)
        elif not keys.verify(s):
            codes.append(BAD_SIGNATURE)
        else:
            good.append(s)
    codes.extend(policy.unmet(mandate, tuple(good)))
    return list(dict.fromkeys(codes))


def with_signature(
    mandate: Mandate, sig: MandateSignature, *, policy: SignaturePolicy, keys: KeyRing
) -> Mandate:
    """The mandate with one more verified signature, active once the policy is met.

    Refuses a signature that does not verify against this body, a second signature by the same
    person in the same role, and any change to a revoked or expired mandate.
    """
    if mandate.status in (MandateStatus.REVOKED, MandateStatus.EXPIRED):
        raise ValueError(f"a {mandate.status.value} mandate cannot be signed")
    if not verify_signature(mandate, sig, keys):
        raise ValueError("the signature does not verify against this mandate")
    if any(s.person_id == sig.person_id and s.role is sig.role for s in mandate.signers):
        raise ValueError("this person has already signed in this role")
    signed = mandate.model_copy(update={"signers": (*mandate.signers, sig)})
    status = (
        MandateStatus.ACTIVE if not verify_mandate(signed, policy, keys) else MandateStatus.PENDING_SIGNATURES
    )
    return signed.model_copy(update={"status": status})


# --- the ledger -------------------------------------------------------------------------------------------

LedgerKind = Literal["created", "submitted", "signed", "activated", "revoked", "expired", "superseded"]


class LedgerEntry(Contract):
    seq: int = Field(ge=1)
    kind: LedgerKind
    actor: str
    at: AwareDatetime
    mandate_id: str
    version: int = Field(ge=1)
    body_hash: str
    mandate: Mandate = Field(description="The full mandate, signatures and status, at this moment")
    prev_hash: str
    entry_hash: str

    @field_validator("at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)


def compute_entry_hash(
    *,
    prev_hash: str,
    seq: int,
    kind: str,
    actor: str,
    at: datetime,
    mandate_id: str,
    version: int,
    body_hash_hex: str,
    mandate: Mandate,
) -> str:
    content = canonical_bytes(
        {
            "seq": seq,
            "kind": kind,
            "actor": actor,
            "at": at,
            "mandate_id": mandate_id,
            "version": version,
            "body_hash": body_hash_hex,
            "mandate": mandate,
        }
    )
    return _sha256(prev_hash.encode("ascii") + content)


class ChainReport(Contract):
    ok: bool
    entries: int = 0
    first_bad_seq: int | None = None
    problem: str = Field(default="", description="chain_broken, hash_mismatch, seq_gap, body_hash, ...")


def _bad(n_entries: int, seq: int, problem: str) -> ChainReport:
    return ChainReport(ok=False, entries=n_entries, first_bad_seq=seq, problem=problem)


def verify_entries(entries: Sequence[LedgerEntry], keys: KeyRing | None = None) -> ChainReport:
    """Walk the chain in order: links, hashes, sequence numbers, body hashes and versions.

    With `keys`, every signature on every snapshot is checked too. Stops at the first bad entry.
    """
    prev = GENESIS_HASH
    last_version: dict[str, tuple[int, str]] = {}
    for i, e in enumerate(entries, start=1):
        bad = partial(_bad, len(entries), e.seq)
        if e.seq != i:
            return bad("seq_gap")
        if e.prev_hash != prev:
            return bad("chain_broken")
        recomputed = compute_entry_hash(
            prev_hash=e.prev_hash,
            seq=e.seq,
            kind=e.kind,
            actor=e.actor,
            at=e.at,
            mandate_id=e.mandate_id,
            version=e.version,
            body_hash_hex=e.body_hash,
            mandate=e.mandate,
        )
        if recomputed != e.entry_hash:
            return bad("hash_mismatch")
        if e.mandate.mandate_id != e.mandate_id or e.mandate.version != e.version:
            return bad("snapshot_mismatch")
        if body_hash(e.mandate) != e.body_hash:
            return bad("body_hash")
        seen = last_version.get(e.mandate_id)
        if seen is not None:
            v, h = seen
            if e.version < v or (e.version == v and e.body_hash != h):
                return bad("version_regressed")
        last_version[e.mandate_id] = (e.version, e.body_hash)
        if keys is not None and any(not verify_signature(e.mandate, s, keys) for s in e.mandate.signers):
            return bad(BAD_SIGNATURE)
        prev = e.entry_hash
    return ChainReport(ok=True, entries=len(entries))


class MandateLedger:
    """Append-only, hash-chained history of mandates. The class has no update and no delete.

    In memory; a store-backed ledger writes the same `LedgerEntry` rows and verifies them with the
    same `verify_entries`.
    """

    def __init__(self, entries: Iterable[LedgerEntry] = ()) -> None:
        # Loaded as given, without checking, so a stored (possibly tampered) chain can be verified.
        self._entries: list[LedgerEntry] = list(entries)

    def append(self, kind: LedgerKind, mandate: Mandate, *, actor: str, at: datetime) -> LedgerEntry:
        """Record the mandate's state now. A changed body must carry a higher version."""
        h = body_hash(mandate)
        latest = self.latest(mandate.mandate_id)
        if latest is not None:
            same_body = body_hash(latest) == h
            if mandate.version < latest.version or (mandate.version == latest.version and not same_body):
                raise ValueError("a changed mandate needs a higher version")
        seq = len(self._entries) + 1
        prev = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
        when = to_utc(at)
        entry = LedgerEntry(
            seq=seq,
            kind=kind,
            actor=actor,
            at=when,
            mandate_id=mandate.mandate_id,
            version=mandate.version,
            body_hash=h,
            mandate=mandate,
            prev_hash=prev,
            entry_hash=compute_entry_hash(
                prev_hash=prev,
                seq=seq,
                kind=kind,
                actor=actor,
                at=when,
                mandate_id=mandate.mandate_id,
                version=mandate.version,
                body_hash_hex=h,
                mandate=mandate,
            ),
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def head(self) -> str:
        """The latest entry hash: a receipt a client can keep to detect a rewritten history later."""
        return self._entries[-1].entry_hash if self._entries else GENESIS_HASH

    def latest(self, mandate_id: str) -> Mandate | None:
        """The most recent snapshot of this mandate, if it has one."""
        for e in reversed(self._entries):
            if e.mandate_id == mandate_id:
                return e.mandate
        return None

    def history(self, mandate_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries if e.mandate_id == mandate_id]

    def verify_chain(self, keys: KeyRing | None = None) -> ChainReport:
        return verify_entries(self._entries, keys)
