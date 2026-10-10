"""The decision ledger: append-only, hash-chained, signed (Plan K M132, design §3.2; invariant I5).

Every proposal drafted or refused, every approval and rejection, every frozen test plan and every evidence
label a tool hands out is one row of `agentic_decision_log` in the platform database. Nothing here updates
or deletes a row; "append-only" is a property of the class, which has no other write.

**The chain.** Each entry carries `prev_hash`, the `entry_hash` of the entry before it (64 zeros for the
first), and its own `entry_hash`:

    entry_hash = sha256( prev_hash ‖ canonical({seq, kind, actor, created_at, body}) )

so editing any field of any entry - its body, its kind, who it names, when - changes its hash, which no
longer matches the next entry's `prev_hash`. `body` is canonical JSON (`engine.agentic.canonical`) and
always carries `policy_version`, the sha256 of `configs/agentic/policy.yaml` in force when it was written.

**Signatures.** Approvals, frozen test plans and evidence labels (`SIGNED_KINDS`) are signed over their
`entry_hash` by a :class:`Signer`: an Ed25519 file key in development (:class:`FileKeySigner`, generated once
into the data folder with 0600 permissions), AWS KMS on a client deployment later, and :class:`FakeSigner` in
tests. `key_id` names the key, so a verifier knows which public key to check with.

**No row-level data.** A body holds ids, digests, codes, counts and the policy version - never a customer id,
a cell value or a person's free text (an approver's reason is kept as its digest). :data:`BODY_KEYS` is the
closed list of keys a body may use and a value is a scalar or a list of scalars, so a row cannot be smuggled
in under a new key. Erasure therefore never has to touch the ledger, and never breaks the chain.

**Verification.** ``python -m engine.agentic.ledger verify`` walks the chain in `seq` order, in batches (linear
in the entries), recomputes every hash and checks every signature. On the first bad entry it prints the code
(`LEDGER_CHAIN_BROKEN` or `LEDGER_SIGNATURE_INVALID`) and the entry's `seq` and exits 1.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import AwareDatetime, Field
from sqlalchemy import Column, DateTime
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from engine.agentic.canonical import canonical_bytes, canonical_json, sha256_hex
from engine.agentic.errors import AgenticError
from engine.config import StrictBase

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

__all__ = [
    "BODY_KEYS",
    "DECISION_LOG_TABLE",
    "GENESIS_HASH",
    "SIGNED_KINDS",
    "SIGNING_KEY_FILE",
    "DecisionLedger",
    "DecisionLogEntry",
    "FakeSigner",
    "FileKeySigner",
    "LedgerEntry",
    "Signer",
    "VerifyReport",
    "compute_entry_hash",
    "main",
    "signer_for_data_dir",
    "verify_entries",
    "verify_ledger",
]

DECISION_LOG_TABLE: Final[str] = "agentic_decision_log"
GENESIS_HASH: Final[str] = "0" * 64
SIGNING_KEY_FILE: Final[str] = "agentic/ledger_signing_key.pem"
"""Where the development Ed25519 key lives, under the data folder (0600)."""

# The kinds this package writes. A kind is a dotted name; new milestones add theirs.
PROPOSAL_DRAFTED: Final[str] = "proposal.drafted"
PROPOSAL_REFUSED: Final[str] = "proposal.refused"
PROPOSAL_APPROVED: Final[str] = "proposal.approved"
PROPOSAL_REJECTED: Final[str] = "proposal.rejected"
PROPOSAL_EXPIRED: Final[str] = "proposal.expired"
APPROVAL_REFUSED: Final[str] = "approval.refused"
EXECUTION_FAILED: Final[str] = "proposal.execution_failed"
TEST_PLAN_FROZEN: Final[str] = "test_plan.frozen"
EVIDENCE_LABEL: Final[str] = "evidence.label"

SIGNED_KINDS: Final[frozenset[str]] = frozenset({PROPOSAL_APPROVED, TEST_PLAN_FROZEN, EVIDENCE_LABEL})
"""Approvals, test plans and evidence labels are signed (design §3.2)."""

BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "policy_version",
        "proposal_id",
        "proposal_kind",
        "payload_digest",
        "evidence_id",
        "evidence_ids",
        "tool",
        "label",
        "input_digest",
        "output_digest",
        "artefact_refs",
        "status",
        "codes",
        "rules",
        "proposed_by",
        "decided_by",
        "reason_digest",
        "campaign_id",
        "run_id",
        "use_case_id",
        "plan_hash",
        "plan_version",
        "holdout_scope",
        "holdout_epoch",
        "holdout_salt_id",
        "result_ref",
    }
)
"""The only keys a body may hold: ids, digests, codes and labels, never a row (see the module docstring)."""

_BATCH: Final[int] = 10_000


class DecisionLogEntry(SQLModel, table=True):
    """One ledger entry. Alembic's `0009_agentic_ledger` creates the table on Postgres."""

    __tablename__ = DECISION_LOG_TABLE

    seq: int = SQLField(primary_key=True)
    prev_hash: str
    entry_hash: str = SQLField(index=True, unique=True)
    kind: str = SQLField(index=True)
    actor: str
    body: str
    signature: str | None = None
    key_id: str | None = None
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))


class LedgerEntry(StrictBase):
    """An entry as the API and the tools show it."""

    seq: int = Field(description="Strictly increasing, from 1.")
    prev_hash: str = Field(description="`entry_hash` of the entry before (64 zeros for the first).")
    entry_hash: str = Field(
        description="sha256(prev_hash ‖ canonical({seq, kind, actor, created_at, body}))."
    )
    kind: str = Field(
        description="proposal.drafted, proposal.refused, proposal.approved, evidence.label, ..."
    )
    actor: str = Field(description="The principal's user id, or `agent:<name>`.")
    body: dict[str, Any] = Field(description="Ids, digests, codes and the policy version; never a row.")
    signature: str | None = Field(description="Base64 signature over entry_hash, for the signed kinds.")
    key_id: str | None = Field(description="Which key signed it.")
    created_at: AwareDatetime


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------
class Signer(Protocol):
    """Signs an entry hash and checks a signature. KMS, a file key and a fake implement it."""

    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> str: ...

    def verify(self, key_id: str, message: bytes, signature: str) -> bool: ...


class FakeSigner:
    """A deterministic test signer: HMAC-SHA256 under a fixed secret. Never used by the product."""

    def __init__(self, secret: bytes = b"marketing-ai-fake-signer", key_id: str = "fake:test") -> None:
        self._secret = secret
        self._key_id = key_id

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, message: bytes) -> str:
        import hmac

        return hmac.new(self._secret, message, "sha256").hexdigest()

    def verify(self, key_id: str, message: bytes, signature: str) -> bool:
        import hmac

        return key_id == self._key_id and hmac.compare_digest(self.sign(message), signature)


class FileKeySigner:
    """Ed25519 with a PEM private key on disk (development). Created once, 0600, when missing.

    `cryptography` is already pinned (Plan H M80's saved-connection encryption); it is imported here, in
    the functions, so `import engine` does not load it.
    """

    _LOCK: Final = threading.Lock()

    def __init__(self, path: Path, *, create: bool = True) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._path = Path(path)
        with self._LOCK:
            if not self._path.is_file():
                if not create:
                    raise AgenticError(
                        "LEDGER_SIGNATURE_INVALID", f"No ledger signing key at {self._path}.", status=500
                    )
                key = Ed25519PrivateKey.generate()
                pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                self._path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(pem)
        loaded = serialization.load_pem_private_key(self._path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise AgenticError("LEDGER_SIGNATURE_INVALID", f"{self._path} is not an Ed25519 key.", status=500)
        self._key = loaded
        public = loaded.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._public = loaded.public_key()
        self._key_id = "ed25519:" + sha256_hex(public)[:16]

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self._key.sign(message)).decode("ascii")

    def verify(self, key_id: str, message: bytes, signature: str) -> bool:
        from cryptography.exceptions import InvalidSignature

        if key_id != self._key_id:
            return False
        try:
            self._public.verify(base64.b64decode(signature, validate=True), message)
        except (InvalidSignature, ValueError):
            return False
        return True


def signer_for_data_dir(data_dir: Path, *, create: bool = True) -> FileKeySigner:
    """The development signer of a data folder: `<data_dir>/agentic/ledger_signing_key.pem`."""
    return FileKeySigner(Path(data_dir) / SIGNING_KEY_FILE, create=create)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------
def _instant(moment: datetime) -> datetime:
    """UTC, whatever the driver handed back (SQLite returns naive values; see `engine.registry.aware_utc`)."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _stamp(moment: datetime) -> str:
    return _instant(moment).isoformat(timespec="microseconds")


def compute_entry_hash(
    *, seq: int, prev_hash: str, kind: str, actor: str, created_at: datetime, body: Mapping[str, Any]
) -> str:
    """The `entry_hash` of an entry with these fields (see the module docstring)."""
    content = {"seq": seq, "kind": kind, "actor": actor, "created_at": _stamp(created_at), "body": body}
    return sha256_hex(prev_hash.encode("ascii") + canonical_bytes(content))


def _check_body(body: Mapping[str, Any]) -> None:
    unknown = sorted(set(body) - BODY_KEYS)
    if unknown:
        raise ValueError(f"A ledger body may not hold {', '.join(unknown)}: ids, digests and codes only.")
    for key, value in body.items():
        items = value if isinstance(value, list | tuple) else [value]
        for item in items:
            if item is not None and not isinstance(item, str | int | float | bool):
                raise ValueError(f"A ledger body value must be a scalar or a list of scalars ({key}).")


def _view(row: Any) -> LedgerEntry:
    import json

    return LedgerEntry(
        seq=row.seq,
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
        kind=row.kind,
        actor=row.actor,
        body=json.loads(row.body),
        signature=row.signature,
        key_id=row.key_id,
        created_at=_instant(row.created_at),
    )


class DecisionLedger:
    """The append-only ledger on one database. `append` is the only write."""

    _LOCK: Final = threading.Lock()

    def __init__(self, engine: Engine, *, signer: Signer | None, policy_version: str) -> None:
        from engine.platform_db import create_tables

        self._engine = engine
        self._signer = signer
        self._policy_version = policy_version
        create_tables(engine, (DECISION_LOG_TABLE,))

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def append(
        self, kind: str, actor: str, body: Mapping[str, Any], *, at: datetime | None = None
    ) -> LedgerEntry:
        """Add one entry at the head of the chain, signed when its kind is in `SIGNED_KINDS`.

        One short transaction reads the head and inserts the next entry; `seq` is the primary key, so a
        second process that appended in between makes the insert fail and this one reads the new head and
        tries again. SQLAlchemy Core, not the ORM: an append is on every tool call (design budget < 5 ms).
        """
        import json

        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from engine.agentic.canonical import jsonable
        from engine.utils.time import utc_now

        full = jsonable({**dict(body), "policy_version": self._policy_version})
        _check_body(full)
        if kind in SIGNED_KINDS and self._signer is None:
            raise AgenticError(
                "LEDGER_SIGNATURE_INVALID", f"A {kind} entry must be signed and no signer is set.", status=500
            )
        created = _instant(at or utc_now())
        text = canonical_json(full)
        parsed = json.loads(text)
        table = _TABLE()
        for _attempt in range(5):
            try:
                with self._LOCK, self._engine.begin() as connection:
                    head = connection.execute(
                        select(table.c.seq, table.c.entry_hash).order_by(table.c.seq.desc()).limit(1)
                    ).first()
                    seq = 1 if head is None else int(head.seq) + 1
                    prev = GENESIS_HASH if head is None else str(head.entry_hash)
                    entry_hash = compute_entry_hash(
                        seq=seq, prev_hash=prev, kind=kind, actor=actor, created_at=created, body=parsed
                    )
                    signature = key_id = None
                    if kind in SIGNED_KINDS and self._signer is not None:
                        signature = self._signer.sign(entry_hash.encode("ascii"))
                        key_id = self._signer.key_id
                    connection.execute(
                        table.insert().values(
                            seq=seq,
                            prev_hash=prev,
                            entry_hash=entry_hash,
                            kind=kind,
                            actor=actor,
                            body=text,
                            signature=signature,
                            key_id=key_id,
                            created_at=created,
                        )
                    )
            except IntegrityError:  # another process took this seq: read the new head and retry
                continue
            return LedgerEntry(
                seq=seq,
                prev_hash=prev,
                entry_hash=entry_hash,
                kind=kind,
                actor=actor,
                body=parsed,
                signature=signature,
                key_id=key_id,
                created_at=created,
            )
        raise AgenticError("LEDGER_CHAIN_BROKEN", "The ledger could not be appended to.", status=503)

    @staticmethod
    def _head_row(session: Any) -> DecisionLogEntry | None:
        from sqlmodel import col, select

        result: DecisionLogEntry | None = session.exec(
            select(DecisionLogEntry).order_by(col(DecisionLogEntry.seq).desc()).limit(1)
        ).first()
        return result

    def head(self) -> LedgerEntry | None:
        """The newest entry, or None for an empty ledger. Its `entry_hash` is the client's checkpoint."""
        from sqlmodel import Session

        with Session(self._engine) as session:
            row = self._head_row(session)
            return None if row is None else _view(row)

    def has_evidence(self, evidence_id: str) -> bool:
        """Whether a tool handed out `evidence_id` (an `evidence.label` entry names it)."""
        from sqlmodel import Session, col, select

        if not evidence_id.startswith("ev_") or not evidence_id[3:].isalnum() or len(evidence_id) != 19:
            return False
        needle = f'%"evidence_id":"{evidence_id}"%'
        with Session(self._engine) as session:
            row = session.exec(
                select(DecisionLogEntry.seq)
                .where(col(DecisionLogEntry.kind) == EVIDENCE_LABEL)
                .where(col(DecisionLogEntry.body).like(needle))
                .limit(1)
            ).first()
        return row is not None

    def entries(self, *, kind: str | None = None, after_seq: int = 0) -> Iterator[LedgerEntry]:
        """Every entry after `after_seq`, oldest first (of one kind when given), read in batches."""
        for rows in _batches(self._engine, after_seq=after_seq, kind=kind):
            yield from (_view(row) for row in rows)


def _TABLE() -> Any:  # noqa: N802 - the table object, named like the constant it stands for
    return DecisionLogEntry.__table__  # type: ignore[attr-defined]


def _batches(engine: Engine, *, after_seq: int = 0, kind: str | None = None) -> Iterator[list[Any]]:
    """Every row after `after_seq` in `seq` order, `_BATCH` at a time, as plain Core rows (no ORM objects)."""
    from sqlalchemy import select

    table = _TABLE()
    last = after_seq
    while True:
        statement = select(table).where(table.c.seq > last)
        if kind is not None:
            statement = statement.where(table.c.kind == kind)
        statement = statement.order_by(table.c.seq).limit(_BATCH)
        with engine.connect() as connection:
            rows = list(connection.execute(statement).all())
        if not rows:
            return
        yield rows
        last = int(rows[-1].seq)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
class VerifyReport(StrictBase):
    """What `verify` found: every entry good, or the first bad one and why."""

    ok: bool
    entries: int = Field(description="Entries checked (up to and including the first bad one).")
    head: str | None = Field(description="entry_hash of the last good entry; null for an empty ledger.")
    code: str | None = Field(default=None, description="LEDGER_CHAIN_BROKEN or LEDGER_SIGNATURE_INVALID.")
    bad_seq: int | None = Field(default=None, description="seq of the first bad entry.")
    reason: str | None = Field(default=None, description="What is wrong with it, in one sentence.")


@dataclass
class _Walk:
    expected_seq: int = 1
    prev_hash: str = GENESIS_HASH
    checked: int = 0


def _bad(walk: _Walk, code: str, seq: int, reason: str) -> VerifyReport:
    head = None if walk.prev_hash == GENESIS_HASH else walk.prev_hash
    return VerifyReport(ok=False, entries=walk.checked, head=head, code=code, bad_seq=seq, reason=reason)


def _check_row(walk: _Walk, row: Any, verifier: Signer | None) -> VerifyReport | None:
    import json

    walk.checked += 1
    seq = row.seq
    if seq != walk.expected_seq:
        return _bad(
            walk,
            "LEDGER_CHAIN_BROKEN",
            seq,
            f"entry {seq} follows entry {walk.expected_seq - 1}: one is missing.",
        )
    if row.prev_hash != walk.prev_hash:
        return _bad(walk, "LEDGER_CHAIN_BROKEN", seq, f"entry {seq} does not point at the entry before it.")
    try:
        body = json.loads(row.body)
    except ValueError:
        return _bad(walk, "LEDGER_CHAIN_BROKEN", seq, f"entry {seq}'s body is not JSON.")
    recomputed = compute_entry_hash(
        seq=seq, prev_hash=row.prev_hash, kind=row.kind, actor=row.actor, created_at=row.created_at, body=body
    )
    if recomputed != row.entry_hash or canonical_json(body) != row.body:
        return _bad(walk, "LEDGER_CHAIN_BROKEN", seq, f"entry {seq} was changed after it was written.")
    if row.kind in SIGNED_KINDS or row.signature is not None:
        if row.signature is None or row.key_id is None:
            return _bad(walk, "LEDGER_SIGNATURE_INVALID", seq, f"entry {seq} ({row.kind}) is not signed.")
        if verifier is None or not verifier.verify(row.key_id, row.entry_hash.encode("ascii"), row.signature):
            return _bad(
                walk,
                "LEDGER_SIGNATURE_INVALID",
                seq,
                f"entry {seq}'s signature does not verify with key {row.key_id}.",
            )
    walk.expected_seq = seq + 1
    walk.prev_hash = row.entry_hash
    return None


def verify_entries(rows: Iterable[Any], verifier: Signer | None) -> VerifyReport:
    """Walk `rows` in order (they must be every entry, oldest first) and stop at the first bad one."""
    walk = _Walk()
    for row in rows:
        failure = _check_row(walk, row, verifier)
        if failure is not None:
            return failure
    head = None if walk.prev_hash == GENESIS_HASH else walk.prev_hash
    return VerifyReport(ok=True, entries=walk.checked, head=head)


def verify_ledger(engine: Engine, verifier: Signer | None) -> VerifyReport:
    """:func:`verify_entries` over the database's whole ledger, read in batches of 10,000."""

    def rows() -> Iterator[Any]:
        for batch in _batches(engine):
            yield from batch

    return verify_entries(rows(), verifier)


# ---------------------------------------------------------------------------
# The CLI: python -m engine.agentic.ledger verify
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m engine.agentic.ledger", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="Walk the chain and check every signature.")
    verify.add_argument(
        "--data-dir", type=Path, default=None, help="The data folder (default: the settings')."
    )
    verify.add_argument(
        "--key",
        type=Path,
        default=None,
        help="The signing key (default: <data dir>/" + SIGNING_KEY_FILE + ").",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the deployment's ledger. Exit 0 when every entry is good; 1 naming the first bad entry."""
    from engine.platform_db import platform_engine
    from engine.settings import load_settings

    args = _parser().parse_args(argv)
    settings = load_settings()
    data_dir = args.data_dir if args.data_dir is not None else settings.data_dir
    engine = platform_engine(settings, data_dir=args.data_dir)
    key_path = args.key if args.key is not None else Path(data_dir) / SIGNING_KEY_FILE
    verifier: Signer | None = FileKeySigner(key_path, create=False) if key_path.is_file() else None
    from engine.platform_db import create_tables

    create_tables(engine, (DECISION_LOG_TABLE,))
    report = verify_ledger(engine, verifier)
    if report.ok:
        print(f"ok: {report.entries} entries, head {report.head or '-'}")
        return 0
    print(f"{report.code}: entry seq={report.bad_seq}: {report.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised through `main` and a subprocess test
    raise SystemExit(main())
