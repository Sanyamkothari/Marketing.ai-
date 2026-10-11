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

**One chain per client workspace (Plan K M141, DEC-1408 (c); design §13 q3).** The deployment's own chain is
`agentic_decision_log`, as M132 wrote it. Each client workspace the MCP server serves (`configs/agentic/mcp.yaml`
`mcp.workspaces`) has a chain of its own in `agentic_workspace_log`, keyed by `(workspace, seq)`: its own `seq`
from 1, its own genesis, and the workspace id inside every hashed entry (``{..., "chain": <workspace>}``), so an
entry cannot be moved from one client's chain to another's. A client's receipts are checkpoints of the chain that
holds only that client's entries, so they stay meaningful when one deployment serves several clients, and one
client's verifier never reads another's ids. ``verify --workspace <id>`` walks one workspace's chain;
``verify --against <receipts.json>`` also checks that every receipt the client kept is still in it
(`engine.agentic.receipts`): a history rewritten after a receipt was handed out fails with `RECEIPT_MISMATCH`
even when the rewriter recomputed every hash and signature.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
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
    "CHAIN_PATTERN",
    "DECISION_LOG_TABLE",
    "GENESIS_HASH",
    "SIGNED_KINDS",
    "SIGNING_KEY_FILE",
    "WORKSPACE_LOG_TABLE",
    "DecisionLedger",
    "DecisionLogEntry",
    "FakeSigner",
    "FileKeySigner",
    "LedgerEntry",
    "Signer",
    "VerifyReport",
    "WorkspaceLogEntry",
    "compute_entry_hash",
    "holdout_commitment",
    "main",
    "signer_for_data_dir",
    "verify_entries",
    "verify_ledger",
]

DECISION_LOG_TABLE: Final[str] = "agentic_decision_log"
WORKSPACE_LOG_TABLE: Final[str] = "agentic_workspace_log"
"""Plan K M141 (DEC-1408 (c)): one chain per client workspace, keyed by `(workspace, seq)`."""
CHAIN_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_-]{0,47}$"
"""A workspace id, as a chain is named: lower-case letters, digits, `_` and `-`."""
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
CAMPAIGN_PLAN_EXECUTED: Final[str] = "campaign_plan.executed"
"""Plan K M139: an approved campaign plan carried out (treat list, campaign, frozen test plan)."""

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
        "expires_at",
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
        # Plan K M139 (DEC-1406): the planner's run, the narrative's digest, and what executing a
        # campaign plan made.
        "plan_id",
        "narrative_digest",
        "measure_schedule_id",
        # Plan K M134 (DEC-1405): the compliance gate's evidence label - the decision file's sha256 and the
        # sha256 of the rules it applied (`engine.compliance.handoff`).
        "artefact_sha256",
        "rule_version",
    }
)
"""The only keys a body may hold: ids, digests, codes and labels, never a row (see the module docstring)."""

_BATCH: Final[int] = 10_000
_EVIDENCE_ID: Final[re.Pattern[str]] = re.compile(r"ev_[0-9a-f]{16}")
INDEXED_KEYS: Final[tuple[str, ...]] = ("proposal_id", "evidence_id")
"""Body keys copied into indexed columns of the same name (see `DecisionLogEntry`)."""


def _indexed(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) else None


class DecisionLogEntry(SQLModel, table=True):
    """One ledger entry. Alembic's `0009_agentic_ledger` creates the table on Postgres.

    `proposal_id` and `evidence_id` copy the body's own values into indexed columns, so finding a proposal's
    entries or checking a cited evidence id is an index lookup, not a scan of every body. They are not part
    of `entry_hash` (the body already is), and :func:`verify_entries` refuses an entry whose column disagrees
    with its body, so editing a column to make an evidence id appear shows as `LEDGER_CHAIN_BROKEN`.
    """

    __tablename__ = DECISION_LOG_TABLE

    seq: int = SQLField(primary_key=True)
    prev_hash: str
    entry_hash: str = SQLField(index=True, unique=True)
    kind: str = SQLField(index=True)
    actor: str
    body: str
    proposal_id: str | None = SQLField(default=None, index=True)
    evidence_id: str | None = SQLField(default=None, index=True)
    signature: str | None = None
    key_id: str | None = None
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))


class WorkspaceLogEntry(SQLModel, table=True):
    """One entry of a client workspace's chain (Plan K M141). Alembic's `0010_agentic_workspace_log` creates the
    table on Postgres. The same columns as `DecisionLogEntry`, with `workspace` beside `seq` in the key: each
    workspace's `seq` counts from 1, and its genesis entry points at 64 zeros."""

    __tablename__ = WORKSPACE_LOG_TABLE

    workspace: str = SQLField(primary_key=True)
    seq: int = SQLField(primary_key=True)
    prev_hash: str
    entry_hash: str = SQLField(index=True, unique=True)
    kind: str = SQLField(index=True)
    actor: str
    body: str
    proposal_id: str | None = SQLField(default=None, index=True)
    evidence_id: str | None = SQLField(default=None, index=True)
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
    chain: str | None = Field(
        default=None,
        description="The client workspace whose chain holds it (Plan K M141); null on the deployment's chain.",
        exclude_if=lambda value: value is None,
    )


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


def holdout_commitment(tool_ctx: Any, campaign_id: str) -> dict[str, Any]:
    """The holdout commitment a `test_plan.frozen` entry records for `campaign_id`: the plan's version, the
    holdout's scope and epoch and, beyond one run, the salt's id. Empty when the campaign is not found.

    Computed here once for every writer of that kind (an approved test plan, `engine.approvals`; an approved
    campaign plan, `engine.agentic.planner.execute_plan`), so their signed entries cannot drift apart.
    """
    campaign = None if tool_ctx.campaigns is None else tool_ctx.campaigns.get(campaign_id)
    if campaign is None:
        return {}
    body: dict[str, Any] = {
        "plan_version": campaign.test_plan_version,
        "holdout_scope": campaign.holdout_scope,
        "holdout_epoch": campaign.holdout_epoch,
    }
    if campaign.holdout_scope != "run" and tool_ctx.platform_engine is not None:
        from engine.holdout.salt import HoldoutLedger, salt_id

        fingerprint = HoldoutLedger(tool_ctx.platform_engine).fingerprint()
        body["holdout_salt_id"] = None if fingerprint is None else salt_id(fingerprint)
    return body


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
    *,
    seq: int,
    prev_hash: str,
    kind: str,
    actor: str,
    created_at: datetime,
    body: Mapping[str, Any],
    chain: str | None = None,
) -> str:
    """The `entry_hash` of an entry with these fields (see the module docstring). An entry of a workspace's chain
    hashes its workspace id too (`chain`); the deployment's chain hashes exactly what M132 hashed."""
    content: dict[str, Any] = {
        "seq": seq,
        "kind": kind,
        "actor": actor,
        "created_at": _stamp(created_at),
        "body": body,
    }
    if chain is not None:
        content["chain"] = chain
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
        chain=getattr(row, "workspace", None),
    )


def _chain_name(chain: str | None) -> str | None:
    if chain is not None and not re.fullmatch(CHAIN_PATTERN, chain):
        raise ValueError(f"A ledger chain is named by a workspace id ({CHAIN_PATTERN}), not {chain!r}.")
    return chain


class DecisionLedger:
    """The append-only ledger on one database. `append` is the only write.

    `chain` names a client workspace's chain (Plan K M141, `agentic_workspace_log`); None, the default, is the
    deployment's own chain (`agentic_decision_log`), exactly as M132 kept it.
    """

    _LOCK: Final = threading.Lock()

    def __init__(
        self, engine: Engine, *, signer: Signer | None, policy_version: str, chain: str | None = None
    ) -> None:
        from engine.platform_db import create_tables

        self._engine = engine
        self._signer = signer
        self._policy_version = policy_version
        self._chain = _chain_name(chain)
        create_tables(engine, (DECISION_LOG_TABLE if chain is None else WORKSPACE_LOG_TABLE,))

    @property
    def policy_version(self) -> str:
        return self._policy_version

    @property
    def chain(self) -> str | None:
        """The workspace whose chain this is; None for the deployment's chain."""
        return self._chain

    @property
    def engine(self) -> Engine:
        """The database the chain lives in (a receipt check and the verifier read it)."""
        return self._engine

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
        table = _table(self._chain)
        chain_values = {} if self._chain is None else {"workspace": self._chain}
        for _attempt in range(5):
            try:
                with self._LOCK, self._engine.begin() as connection:
                    head = connection.execute(
                        _on_chain(select(table.c.seq, table.c.entry_hash), table, self._chain)
                        .order_by(table.c.seq.desc())
                        .limit(1)
                    ).first()
                    seq = 1 if head is None else int(head.seq) + 1
                    prev = GENESIS_HASH if head is None else str(head.entry_hash)
                    entry_hash = compute_entry_hash(
                        seq=seq,
                        prev_hash=prev,
                        kind=kind,
                        actor=actor,
                        created_at=created,
                        body=parsed,
                        chain=self._chain,
                    )
                    signature = key_id = None
                    if kind in SIGNED_KINDS and self._signer is not None:
                        signature = self._signer.sign(entry_hash.encode("ascii"))
                        key_id = self._signer.key_id
                    connection.execute(
                        table.insert().values(
                            **chain_values,
                            seq=seq,
                            prev_hash=prev,
                            entry_hash=entry_hash,
                            kind=kind,
                            actor=actor,
                            body=text,
                            proposal_id=_indexed(parsed, "proposal_id"),
                            evidence_id=_indexed(parsed, "evidence_id"),
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
                chain=self._chain,
            )
        raise AgenticError("LEDGER_CHAIN_BROKEN", "The ledger could not be appended to.", status=503)

    def head(self) -> LedgerEntry | None:
        """The newest entry, or None for an empty ledger. Its `entry_hash` is the client's checkpoint."""
        from sqlalchemy import select

        table = _table(self._chain)
        with self._engine.connect() as connection:
            row = connection.execute(
                _on_chain(select(table), table, self._chain).order_by(table.c.seq.desc()).limit(1)
            ).first()
        return None if row is None else _view(row)

    def find(self, entry_hash: str) -> LedgerEntry | None:
        """The entry of this chain whose `entry_hash` is `entry_hash` (an indexed lookup), or None."""
        from sqlalchemy import select

        table = _table(self._chain)
        with self._engine.connect() as connection:
            row = connection.execute(
                _on_chain(select(table), table, self._chain).where(table.c.entry_hash == entry_hash)
            ).first()
        return None if row is None else _view(row)

    def has_evidence(self, evidence_id: str) -> bool:
        """Whether a tool handed out `evidence_id` (an `evidence.label` entry names it)."""
        return not self.missing_evidence((evidence_id,))

    def missing_evidence(self, evidence_ids: Iterable[str]) -> frozenset[str]:
        """The ids of `evidence_ids` no `evidence.label` entry names: one indexed `IN` query for all of them."""
        from sqlalchemy import select

        wanted = set(evidence_ids)
        shaped = {item for item in wanted if _EVIDENCE_ID.fullmatch(item)}
        if not shaped:
            return frozenset(wanted)
        table = _table(self._chain)
        # Only the evidence_id index is used (the kind is checked here, not in SQL, so no planner can prefer
        # the kind index, under which every evidence label would be read). On a workspace's chain only its own
        # evidence counts: an id another client's agent was handed is not this client's evidence.
        statement = select(table.c.evidence_id, table.c.kind).where(table.c.evidence_id.in_(sorted(shaped)))
        with self._engine.connect() as connection:
            found = {
                str(row.evidence_id)
                for row in connection.execute(_on_chain(statement, table, self._chain))
                if row.kind == EVIDENCE_LABEL
            }
        return frozenset(wanted - found)

    def proposal_entries(self, proposal_id: str) -> tuple[LedgerEntry, ...]:
        """Every entry naming `proposal_id`, oldest first (an indexed lookup)."""
        from sqlalchemy import select

        table = _table(self._chain)
        with self._engine.connect() as connection:
            rows = connection.execute(
                _on_chain(select(table), table, self._chain)
                .where(table.c.proposal_id == proposal_id)
                .order_by(table.c.seq)
            ).all()
        return tuple(_view(row) for row in rows)

    def entries(self, *, kind: str | None = None, after_seq: int = 0) -> Iterator[LedgerEntry]:
        """Every entry after `after_seq`, oldest first (of one kind when given), read in batches."""
        for rows in _batches(self._engine, after_seq=after_seq, kind=kind, chain=self._chain):
            yield from (_view(row) for row in rows)


def _TABLE() -> Any:  # noqa: N802 - the table object, named like the constant it stands for
    return DecisionLogEntry.__table__  # type: ignore[attr-defined]


def _table(chain: str | None) -> Any:
    """The table a chain lives in: the deployment's (`chain` None) or the workspaces' one."""
    return _TABLE() if chain is None else WorkspaceLogEntry.__table__  # type: ignore[attr-defined]


def _on_chain(statement: Any, table: Any, chain: str | None) -> Any:
    """`statement` narrowed to one workspace's chain (unchanged on the deployment's chain)."""
    return statement if chain is None else statement.where(table.c.workspace == chain)


def _batches(
    engine: Engine, *, after_seq: int = 0, kind: str | None = None, chain: str | None = None
) -> Iterator[list[Any]]:
    """Every row after `after_seq` in `seq` order, `_BATCH` at a time, as plain Core rows (no ORM objects)."""
    from sqlalchemy import select

    table = _table(chain)
    last = after_seq
    while True:
        statement = _on_chain(select(table), table, chain).where(table.c.seq > last)
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


_ABSENT: Final = object()


@dataclass
class _Walk:
    expected_seq: int = 1
    prev_hash: str = GENESIS_HASH
    checked: int = 0


def _bad(walk: _Walk, code: str, seq: int, reason: str) -> VerifyReport:
    head = None if walk.prev_hash == GENESIS_HASH else walk.prev_hash
    return VerifyReport(ok=False, entries=walk.checked, head=head, code=code, bad_seq=seq, reason=reason)


def _stored_hash(row: Any) -> str:
    """:func:`compute_entry_hash` of a stored row whose `body` is already canonical JSON.

    The canonical content is ``{"actor":…,"body":…,"created_at":…,"kind":…,"seq":…}`` (keys sorted), so the
    stored body text is spliced in as it is instead of being parsed and written again: verification pays for
    one canonical write per entry, not two. The caller has checked that `row.body` is canonical, which is what
    makes the splice equal to `compute_entry_hash` (a unit test holds the two equal).
    """
    import json

    def text(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    chain = getattr(row, "workspace", None)
    chained = "" if chain is None else f'"chain":{text(chain)},'
    content = (
        f'{{"actor":{text(row.actor)},"body":{row.body},{chained}'
        f'"created_at":{text(_stamp(row.created_at))},"kind":{text(row.kind)},"seq":{int(row.seq)}}}'
    )
    return sha256_hex(row.prev_hash.encode("ascii") + content.encode("utf-8"))


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
        canonical = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except ValueError:
        return _bad(walk, "LEDGER_CHAIN_BROKEN", seq, f"entry {seq}'s body is not canonical JSON.")
    if canonical != row.body or _stored_hash(row) != row.entry_hash:
        return _bad(walk, "LEDGER_CHAIN_BROKEN", seq, f"entry {seq} was changed after it was written.")
    for column in INDEXED_KEYS:
        stored = getattr(row, column, _ABSENT)
        if stored is not _ABSENT and stored != _indexed(body, column):
            return _bad(
                walk, "LEDGER_CHAIN_BROKEN", seq, f"entry {seq}'s {column} column does not match its body."
            )
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


def verify_entries(
    rows: Iterable[Any], verifier: Signer | None, *, start: tuple[int, str] | None = None
) -> VerifyReport:
    """Walk `rows` in order and stop at the first bad one.

    `rows` must be every entry, oldest first - or, with `start=(seq, entry_hash)` (a checkpoint the caller
    trusts), every entry after `seq`, the first of which must point at `entry_hash`.
    """
    walk = _Walk() if start is None else _Walk(expected_seq=start[0] + 1, prev_hash=start[1])
    for row in rows:
        failure = _check_row(walk, row, verifier)
        if failure is not None:
            return failure
    head = None if walk.prev_hash == GENESIS_HASH else walk.prev_hash
    return VerifyReport(ok=True, entries=walk.checked, head=head)


def verify_ledger(
    engine: Engine,
    verifier: Signer | None,
    *,
    from_checkpoint: str | None = None,
    chain: str | None = None,
) -> VerifyReport:
    """:func:`verify_entries` over the database's ledger, read in batches of 10,000.

    `from_checkpoint` is an `entry_hash` the caller already verified (a receipt's `ledger_checkpoint`, or the
    `head` of an earlier report): only the entries after it are walked, and the first of them must point at
    it. A checkpoint that is not in the ledger is `LEDGER_CHAIN_BROKEN`: the ledger was rewritten under it.
    `chain` walks one client workspace's chain (Plan K M141); None walks the deployment's.
    """
    _chain_name(chain)
    start: tuple[int, str] | None = None
    if from_checkpoint is not None:
        from sqlalchemy import select

        table = _table(chain)
        with engine.connect() as connection:
            found = connection.execute(
                _on_chain(select(table.c.seq), table, chain).where(table.c.entry_hash == from_checkpoint)
            ).first()
        if found is None:
            return VerifyReport(
                ok=False,
                entries=0,
                head=None,
                code="LEDGER_CHAIN_BROKEN",
                bad_seq=None,
                reason="The checkpoint is not in the ledger: the ledger was rewritten after it was taken.",
            )
        start = (int(found.seq), from_checkpoint)

    def rows() -> Iterator[Any]:
        for batch in _batches(engine, after_seq=0 if start is None else start[0], chain=chain):
            yield from batch

    report = verify_entries(rows(), verifier, start=start)
    if report.ok and report.head is None and start is not None:
        return report.model_copy(update={"head": start[1]})
    return report


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
        "--from-checkpoint",
        default=None,
        metavar="ENTRY_HASH",
        help="Walk only the entries after this already-verified entry_hash (a receipt's ledger_checkpoint).",
    )
    verify.add_argument(
        "--key",
        type=Path,
        default=None,
        help="The signing key (default: <data dir>/" + SIGNING_KEY_FILE + ").",
    )
    verify.add_argument(
        "--workspace",
        default=None,
        metavar="WORKSPACE",
        help="Walk this client workspace's chain (Plan K M141) instead of the deployment's.",
    )
    verify.add_argument(
        "--against",
        type=Path,
        default=None,
        metavar="RECEIPTS_JSON",
        help=(
            'Receipts the client kept (a JSON list, or {"receipts": [...]}): each must still be in the chain, '
            "else RECEIPT_MISMATCH. A receipt names its chain (ledger_chain); --workspace defaults to it."
        ),
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

    receipts = None
    chain: str | None = args.workspace
    if args.against is not None:
        from engine.agentic.receipts import ReceiptError, chain_of, load_receipts

        try:
            receipts = load_receipts(args.against)
            chain = chain if chain is not None else chain_of(receipts)
        except ReceiptError as exc:
            print(f"{exc.code}: {exc}", file=sys.stderr)
            return 1
    try:
        _chain_name(chain)
    except ValueError as exc:
        print(f"LEDGER_CHAIN_BROKEN: {exc}", file=sys.stderr)
        return 1
    create_tables(engine, (DECISION_LOG_TABLE if chain is None else WORKSPACE_LOG_TABLE,))
    report = verify_ledger(engine, verifier, from_checkpoint=args.from_checkpoint, chain=chain)
    if not report.ok:
        where = "" if report.bad_seq is None else f"entry seq={report.bad_seq}: "
        print(f"{report.code}: {where}{report.reason}", file=sys.stderr)
        return 1
    named = "" if chain is None else f" of workspace {chain}"
    if receipts is not None:
        from engine.agentic.receipts import check_receipts

        # The ledger class of this module: run as `python -m`, it is `__main__`, and importing it again would
        # declare its tables twice.
        checked = check_receipts(
            DecisionLedger(engine, signer=None, policy_version="", chain=chain), receipts
        )
        if not checked.ok:
            print(f"{checked.code}: receipt {checked.bad_receipt}: {checked.reason}", file=sys.stderr)
            return 1
        print(
            f"ok: {report.entries} entries{named}, head {report.head or '-'}; {checked.receipts} receipts match"
        )
        return 0
    print(f"ok: {report.entries} entries{named}, head {report.head or '-'}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `main` and a subprocess test
    raise SystemExit(main())
