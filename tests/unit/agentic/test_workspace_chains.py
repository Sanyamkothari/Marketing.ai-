"""One ledger chain per client workspace, and the receipts a client keeps (Plan K M141, DEC-1408 (c), (d); design
§3.2 client-held checkpoints, §13 q3).

* Each workspace's chain starts from its own genesis at seq 1, beside the deployment's chain and the other
  workspaces', and every lookup (evidence, a proposal's entries, the head) reads its own chain only.
* An entry's hash covers its workspace: an entry moved to another workspace's chain no longer verifies.
* A client's receipts verify against the chain; a history rewritten by someone holding the signing key - every
  hash recomputed and every signature redone, so that plain verification passes - fails against the receipts
  with `RECEIPT_MISMATCH`, through `check_receipts` and through `python -m engine.agentic.ledger verify
  --against`.

On the unchanged code (M132's ledger) there is no chain, no receipt module and no `--against`: these tests fail
there.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from api.mcp.config import WORKSPACE_ID_PATTERN
from engine.agentic.canonical import canonical_json
from engine.agentic.ledger import (
    CHAIN_PATTERN,
    EVIDENCE_LABEL,
    GENESIS_HASH,
    SIGNED_KINDS,
    DecisionLedger,
    FileKeySigner,
    compute_entry_hash,
    main,
    signer_for_data_dir,
    verify_ledger,
)
from engine.agentic.receipts import LedgerReceipt, check_receipts, load_receipts, receipt_for
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

POLICY = "b" * 64
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)


@pytest.fixture
def signer(tmp_path: Path) -> FileKeySigner:
    return signer_for_data_dir(tmp_path)


def _evidence(ledger: DecisionLedger, count: int, tag: str) -> list[Any]:
    return [
        ledger.append(
            EVIDENCE_LABEL,
            f"agent:mcp:{tag}",
            {"evidence_id": f"ev_{tag[:2].encode().hex()}{index:012x}", "tool": "measure", "label": "Causal"},
            at=T0 + timedelta(minutes=index),
        )
        for index in range(count)
    ]


def test_the_workspace_id_pattern_is_the_chain_pattern() -> None:
    assert WORKSPACE_ID_PATTERN == CHAIN_PATTERN


def test_each_workspace_has_its_own_chain_from_its_own_genesis(engine: Engine, signer: FileKeySigner) -> None:
    deployment = DecisionLedger(engine, signer=signer, policy_version=POLICY)
    acme = DecisionLedger(engine, signer=signer, policy_version=POLICY, chain="acme")
    beta = DecisionLedger(engine, signer=signer, policy_version=POLICY, chain="beta")
    _evidence(deployment, 2, "dd")
    acme_entries = _evidence(acme, 3, "aa")
    beta_entries = _evidence(beta, 2, "bb")
    assert [e.seq for e in acme_entries] == [1, 2, 3] and [e.seq for e in beta_entries] == [1, 2]
    assert acme_entries[0].prev_hash == beta_entries[0].prev_hash == GENESIS_HASH
    assert {e.chain for e in acme_entries} == {"acme"}
    for chain, count in ((None, 2), ("acme", 3), ("beta", 2)):
        report = verify_ledger(engine, signer, chain=chain)
        assert report.ok and report.entries == count, (chain, report)
    head = acme.head()
    assert head is not None and head.entry_hash == acme_entries[-1].entry_hash and head.chain == "acme"
    # Every lookup reads its own chain only: one client's evidence is not another's, nor the deployment's.
    acme_id = acme_entries[0].body["evidence_id"]
    assert acme.has_evidence(acme_id)
    assert not beta.has_evidence(acme_id) and not deployment.has_evidence(acme_id)
    assert [e.seq for e in deployment.entries()] == [1, 2], "the deployment's chain is as M132 kept it"
    assert acme.find(beta_entries[0].entry_hash) is None
    with pytest.raises(ValueError, match="workspace id"):
        DecisionLedger(engine, signer=signer, policy_version=POLICY, chain="Not A Workspace")


def test_the_hash_covers_the_workspace(engine: Engine, signer: FileKeySigner) -> None:
    acme = DecisionLedger(engine, signer=signer, policy_version=POLICY, chain="acme")
    entry = _evidence(acme, 1, "aa")[0]
    fields = {
        "seq": 1,
        "prev_hash": GENESIS_HASH,
        "kind": entry.kind,
        "actor": entry.actor,
        "created_at": entry.created_at,
        "body": entry.body,
    }
    assert entry.entry_hash == compute_entry_hash(**fields, chain="acme")
    assert entry.entry_hash != compute_entry_hash(**fields, chain="beta") != compute_entry_hash(**fields)
    # Moving the entry to another workspace's chain (an UPDATE of its workspace) breaks it there.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agentic_workspace_log SET workspace = 'beta' WHERE workspace = 'acme'")
        )
    moved = verify_ledger(engine, signer, chain="beta")
    assert not moved.ok and moved.code == "LEDGER_CHAIN_BROKEN" and moved.bad_seq == 1


def test_the_stored_body_is_hashed_as_written_on_a_workspace_chain() -> None:
    from engine.agentic.ledger import _stored_hash

    body = {"policy_version": POLICY, "evidence_id": "ev_0123456789abcdef", "tool": "measure"}
    row = SimpleNamespace(
        seq=4, prev_hash="2" * 64, kind="evidence.label", actor="agent:mcp:x", created_at=T0,
        body=canonical_json(body), workspace="acme-2",
    )  # fmt: skip
    assert _stored_hash(row) == compute_entry_hash(
        seq=4, prev_hash="2" * 64, kind="evidence.label", actor="agent:mcp:x", created_at=T0, body=body,
        chain="acme-2",
    )  # fmt: skip


def _rewrite(engine: Engine, signer: FileKeySigner, chain: str) -> None:
    """Rewrite a workspace's history as someone holding the database and the signing key could: change entry 1's
    actor, then recompute every hash after it and redo every signature, so the chain is consistent again."""
    with engine.begin() as connection:
        rows = (
            connection.execute(
                text("SELECT * FROM agentic_workspace_log WHERE workspace = :c ORDER BY seq"), {"c": chain}
            )
            .mappings()
            .all()
        )
        prev = GENESIS_HASH
        for row in rows:
            actor = "agent:mcp:someone-else" if row["seq"] == 1 else row["actor"]
            created = row["created_at"]
            created = datetime.fromisoformat(created) if isinstance(created, str) else created
            entry_hash = compute_entry_hash(
                seq=row["seq"], prev_hash=prev, kind=row["kind"], actor=actor, created_at=created,
                body=json.loads(row["body"]), chain=chain,
            )  # fmt: skip
            signature = signer.sign(entry_hash.encode("ascii")) if row["kind"] in SIGNED_KINDS else None
            connection.execute(
                text(
                    "UPDATE agentic_workspace_log SET actor = :actor, prev_hash = :prev, entry_hash = :hash, "
                    "signature = :sig WHERE workspace = :c AND seq = :seq"
                ),
                {
                    "actor": actor,
                    "prev": prev,
                    "hash": entry_hash,
                    "sig": signature,
                    "c": chain,
                    "seq": row["seq"],
                },
            )
            prev = entry_hash


def test_receipts_verify_and_a_rewritten_history_fails_against_them(
    engine: Engine, signer: FileKeySigner, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    acme = DecisionLedger(engine, signer=signer, policy_version=POLICY, chain="acme")
    entries = _evidence(acme, 4, "aa")
    receipts = [receipt_for(entry, engine) for entry in entries[1:3]]
    assert receipts[0].ledger_chain == "acme" and receipts[0].ledger_seq == 2
    assert receipts[0].holdout_salt_fingerprint is None and receipts[0].holdout_salt_note
    path = tmp_path / "receipts.json"
    # A client keeps whatever it was given: here the whole receipt objects, with an extra field.
    path.write_text(
        json.dumps({"receipts": [{**r.model_dump(mode="json"), "evidence_id": "ev_x"} for r in receipts]}),
        encoding="utf-8",
    )
    kept = load_receipts(path)
    assert check_receipts(acme, kept).ok
    assert main(["verify", "--data-dir", str(tmp_path), "--against", str(path)]) == 0
    assert "2 receipts match" in capsys.readouterr().out

    _rewrite(engine, signer, "acme")
    assert verify_ledger(engine, signer, chain="acme").ok, "the rewrite is consistent: plain verify passes"
    checked = check_receipts(acme, kept)
    assert not checked.ok and checked.code == "RECEIPT_MISMATCH" and checked.bad_receipt == 0
    assert main(["verify", "--data-dir", str(tmp_path), "--against", str(path)]) == 1
    assert "RECEIPT_MISMATCH: receipt 0" in capsys.readouterr().err


def test_a_receipt_of_another_chain_or_another_seq_does_not_match(
    engine: Engine, signer: FileKeySigner
) -> None:
    acme = DecisionLedger(engine, signer=signer, policy_version=POLICY, chain="acme")
    entry = _evidence(acme, 2, "aa")[1]
    good = receipt_for(entry, engine)
    elsewhere = good.model_copy(update={"ledger_chain": "beta"})
    shifted = good.model_copy(update={"ledger_seq": 1})
    assert not check_receipts(acme, [elsewhere]).ok
    assert not check_receipts(acme, [shifted]).ok
    assert check_receipts(acme, [good]).ok


def test_a_receipts_file_that_does_not_read_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from engine.agentic.receipts import ReceiptError

    path = tmp_path / "receipts.json"
    for content in ("not json", "[]", '[{"ledger_checkpoint": "short"}]'):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ReceiptError):
            load_receipts(path)
    mixed = [
        LedgerReceipt(ledger_chain=chain, ledger_checkpoint="a" * 64).model_dump(mode="json")
        for chain in ("acme", "beta")
    ]
    path.write_text(json.dumps(mixed), encoding="utf-8")
    assert main(["verify", "--data-dir", str(tmp_path), "--against", str(path)]) == 1
    assert "RECEIPT_MISMATCH" in capsys.readouterr().err
