"""The decision ledger (design §3.2, invariant I5): chained, signed, append-only, and its tamper detection.

Every test edits a real SQLite ledger the way an attacker with database access would - an UPDATE, a DELETE -
and requires `verify` (and the `python -m engine.agentic.ledger verify` CLI) to name the first bad entry.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from engine.agentic.canonical import canonical_json
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import (
    BODY_KEYS,
    GENESIS_HASH,
    SIGNED_KINDS,
    DecisionLedger,
    FakeSigner,
    FileKeySigner,
    compute_entry_hash,
    main,
    signer_for_data_dir,
    verify_entries,
    verify_ledger,
)
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY = "a" * 64
T0 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)


@pytest.fixture
def signer(tmp_path: Path) -> FileKeySigner:
    return signer_for_data_dir(tmp_path)


def _fill(ledger: DecisionLedger, count: int = 6) -> None:
    kinds = (
        "proposal.drafted",
        "evidence.label",
        "proposal.refused",
        "proposal.approved",
        "test_plan.frozen",
    )
    for index in range(count):
        ledger.append(
            kinds[index % len(kinds)],
            "agent:test" if index % 2 else "u_asha",
            {"proposal_id": f"prop_{index:04d}", "codes": ["POLICY_BUDGET_CAP"], "status": "pending"},
            at=T0 + timedelta(minutes=index),
        )


def _sql(engine: Engine, statement: str, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params)


def test_entries_chain_from_genesis_and_carry_the_policy_version(
    engine: Engine, signer: FileKeySigner
) -> None:
    ledger = DecisionLedger(engine, signer=signer, policy_version=POLICY)
    first = ledger.append("proposal.drafted", "agent:x", {"proposal_id": "prop_1"}, at=T0)
    second = ledger.append("evidence.label", "agent:x", {"evidence_id": "ev_0123456789abcdef"}, at=T0)
    assert (first.seq, first.prev_hash) == (1, GENESIS_HASH)
    assert (second.seq, second.prev_hash) == (2, first.entry_hash)
    assert first.body["policy_version"] == POLICY
    assert first.signature is None and second.signature is not None and second.key_id == signer.key_id
    expected = compute_entry_hash(
        seq=1,
        prev_hash=GENESIS_HASH,
        kind="proposal.drafted",
        actor="agent:x",
        created_at=T0,
        body=first.body,
    )
    assert first.entry_hash == expected
    head = ledger.head()
    assert head is not None and head.entry_hash == second.entry_hash
    assert [entry.seq for entry in ledger.entries()] == [1, 2]
    assert ledger.has_evidence("ev_0123456789abcdef") and not ledger.has_evidence("ev_ffffffffffffffff")
    assert verify_ledger(engine, signer).ok


def test_the_signed_kinds_are_approvals_test_plans_and_evidence_labels() -> None:
    assert {"proposal.approved", "test_plan.frozen", "evidence.label"} == SIGNED_KINDS


def test_a_signed_kind_without_a_signer_is_refused(engine: Engine) -> None:
    ledger = DecisionLedger(engine, signer=None, policy_version=POLICY)
    with pytest.raises(AgenticError) as caught:
        ledger.append("proposal.approved", "u_ravi", {"proposal_id": "prop_1"})
    assert caught.value.code == "LEDGER_SIGNATURE_INVALID"


@pytest.mark.parametrize(
    "body",
    [
        {"customer_id": "C-1001"},  # a row's key under a new name
        {"proposal_id": {"nested": "row"}},  # a structure smuggled under a known key
        {"codes": [{"customer_id": "C-1"}]},
    ],
)
def test_no_row_level_data_reaches_a_body(engine: Engine, body: dict[str, Any]) -> None:
    ledger = DecisionLedger(engine, signer=FakeSigner(), policy_version=POLICY)
    with pytest.raises(ValueError):
        ledger.append("proposal.drafted", "agent:x", body)
    assert "customer_id" not in BODY_KEYS


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("body", '{"policy_version":"' + POLICY + '","proposal_id":"prop_9999"}'),
        ("kind", "proposal.approved"),
        ("actor", "u_mallory"),
        ("created_at", "2027-01-01 00:00:00.000000"),
        ("prev_hash", "f" * 64),
        ("entry_hash", "e" * 64),
    ],
)
def test_editing_any_field_of_any_entry_is_named(
    engine: Engine, signer: FileKeySigner, column: str, value: str
) -> None:
    ledger = DecisionLedger(engine, signer=signer, policy_version=POLICY)
    _fill(ledger)
    _sql(engine, f"UPDATE agentic_decision_log SET {column} = :value WHERE seq = 3", value=value)
    report = verify_ledger(engine, signer)
    assert not report.ok
    assert report.bad_seq == 3 or (column == "entry_hash" and report.bad_seq in (3, 4))
    assert report.code in {"LEDGER_CHAIN_BROKEN", "LEDGER_SIGNATURE_INVALID"}


def test_rehashing_an_edited_entry_breaks_the_next_link(engine: Engine, signer: FileKeySigner) -> None:
    """A careful forger recomputes the edited entry's hash; the next entry still points at the old one."""
    ledger = DecisionLedger(engine, signer=signer, policy_version=POLICY)
    _fill(ledger)
    forged_body = {"codes": [], "policy_version": POLICY, "proposal_id": "prop_0001", "status": "approved"}
    row = next(entry for entry in ledger.entries() if entry.seq == 2)
    forged_hash = compute_entry_hash(
        seq=2,
        prev_hash=row.prev_hash,
        kind=row.kind,
        actor=row.actor,
        created_at=row.created_at,
        body=forged_body,
    )
    _sql(
        engine,
        "UPDATE agentic_decision_log SET body = :body, entry_hash = :hash WHERE seq = 2",
        body=canonical_json(forged_body),
        hash=forged_hash,
    )
    report = verify_ledger(engine, signer)
    assert not report.ok and report.bad_seq == 2 and report.code == "LEDGER_SIGNATURE_INVALID"


def test_deleting_an_entry_is_named(engine: Engine, signer: FileKeySigner) -> None:
    ledger = DecisionLedger(engine, signer=signer, policy_version=POLICY)
    _fill(ledger)
    _sql(engine, "DELETE FROM agentic_decision_log WHERE seq = 4")
    report = verify_ledger(engine, signer)
    assert not report.ok and report.bad_seq == 5 and report.code == "LEDGER_CHAIN_BROKEN"
    assert "missing" in (report.reason or "")


def test_a_signature_from_another_key_or_a_stripped_one_is_refused(engine: Engine, tmp_path: Path) -> None:
    signer = signer_for_data_dir(tmp_path)
    ledger = DecisionLedger(engine, signer=signer, policy_version=POLICY)
    _fill(ledger)
    other = FileKeySigner(tmp_path / "other.pem")
    report = verify_ledger(engine, other)
    assert not report.ok and report.code == "LEDGER_SIGNATURE_INVALID" and report.bad_seq == 2
    _sql(engine, "UPDATE agentic_decision_log SET signature = NULL WHERE seq = 2")
    report = verify_ledger(engine, signer)
    assert not report.ok and report.code == "LEDGER_SIGNATURE_INVALID" and report.bad_seq == 2


def test_the_file_key_is_private_and_stable(tmp_path: Path) -> None:
    first = signer_for_data_dir(tmp_path)
    path = tmp_path / "agentic" / "ledger_signing_key.pem"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = signer_for_data_dir(tmp_path)
    assert again.key_id == first.key_id and first.key_id.startswith("ed25519:")
    signature = first.sign(b"x" * 64)
    assert again.verify(first.key_id, b"x" * 64, signature)
    assert not again.verify(first.key_id, b"y" * 64, signature)
    with pytest.raises(AgenticError):
        FileKeySigner(tmp_path / "absent.pem", create=False)


def test_the_cli_exits_non_zero_naming_the_first_bad_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    ledger = DecisionLedger(engine, signer=signer_for_data_dir(tmp_path), policy_version=POLICY)
    _fill(ledger)
    assert main(["verify", "--data-dir", str(tmp_path)]) == 0
    assert "ok: 6 entries" in capsys.readouterr().out
    _sql(engine, "UPDATE agentic_decision_log SET actor = 'u_mallory' WHERE seq = 5")
    assert main(["verify", "--data-dir", str(tmp_path)]) == 1
    assert "LEDGER_CHAIN_BROKEN: entry seq=5" in capsys.readouterr().err


def test_the_module_runs_as_a_command(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)
    _fill(DecisionLedger(engine, signer=signer_for_data_dir(tmp_path), policy_version=POLICY))
    _sql(engine, "UPDATE agentic_decision_log SET body = '{}' WHERE seq = 1")
    completed = subprocess.run(
        [sys.executable, "-m", "engine.agentic.ledger", "verify", "--data-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert completed.returncode == 1
    assert "entry seq=1" in completed.stderr


def _chain(count: int) -> list[Any]:
    """`count` unsigned entries as rows, chained, built without a database."""
    from types import SimpleNamespace

    rows: list[Any] = []
    prev = GENESIS_HASH
    for seq in range(1, count + 1):
        body = {"policy_version": POLICY, "proposal_id": f"prop_{seq:08d}", "codes": ["POLICY_BUDGET_CAP"]}
        created = T0 + timedelta(seconds=seq)
        entry_hash = compute_entry_hash(
            seq=seq, prev_hash=prev, kind="proposal.refused", actor="agent:x", created_at=created, body=body
        )
        rows.append(
            SimpleNamespace(
                seq=seq,
                prev_hash=prev,
                entry_hash=entry_hash,
                kind="proposal.refused",
                actor="agent:x",
                body=canonical_json(body),
                signature=None,
                key_id=None,
                created_at=created,
            )
        )
        prev = entry_hash
    return rows


def _best_of_three(rows: list[Any]) -> float:
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        assert verify_entries(rows, None).ok
        timings.append(time.perf_counter() - start)
    return min(timings)


def test_verification_is_linear_in_the_entries() -> None:
    """Design §11: verify is a single pass. 50,000 entries take at most ~15x what 5,000 take."""
    small, large = _chain(5_000), _chain(50_000)
    ratio = _best_of_three(large) / _best_of_three(small)
    assert ratio < 15, ratio


def test_an_append_takes_milliseconds(engine: Engine) -> None:
    """Design §11 budget: an append under 5 ms (best of three batches of 50, after a warm-up)."""
    ledger = DecisionLedger(engine, signer=FakeSigner(), policy_version=POLICY)
    for index in range(20):
        ledger.append("proposal.drafted", "agent:x", {"proposal_id": f"prop_w{index}"})
    timings = []
    for batch in range(3):
        start = time.perf_counter()
        for index in range(50):
            ledger.append("evidence.label", "agent:x", {"evidence_id": f"ev_{batch:02d}{index:014d}"})
        timings.append((time.perf_counter() - start) / 50)
    assert min(timings) < 0.005, timings
    assert verify_ledger(engine, FakeSigner()).ok
