"""The gate at scale (Plan K M134; design §11: 1 million customers x 3 channels in under 60 s).

`decide` is timed on 1,000,000 customers, each with three planned channels (sms, email, whatsapp), against a
contactability file of the same customers in another order, opt-in grants for a third of them and the
per-message verdicts of three actions - every input a hand-off gives it. The best of three runs at each size,
the two sizes taking turns, is held under the budget and against a 100,000-customer run: linear work takes
about ten times as long for ten times the customers, quadratic work about a hundred times.

The second test times the whole hand-off the treat list calls, `gate_treat_list` (the first review: the timing
covered `decide` alone): the rules and message checks, the opt-in lookup over a real SQLite consent ledger holding
WhatsApp grants for a third of the customers, `decide`, the choice of channel, the withheld counts, the parquet
file and its sha256, the signed ledger entry and the summary - at 1,000,000 and at 100,000 customers, the same way.
"""

from __future__ import annotations

import io
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from engine.compliance.gate import GateRules, MessageVerdict, decide

ROWS = 1_000_000
SMALL_ROWS = 100_000
BUDGET_SECONDS = 60.0
CHANNELS = ("sms", "email", "whatsapp")
ACTIONS = ("offer_a", "offer_b", "offer_c")


def _inputs(
    rows: int, seed: int
) -> tuple[pa.Table, pa.Table, pa.Table, dict[tuple[str, str], MessageVerdict]]:
    rng = np.random.default_rng(seed)
    ids = np.char.add("C-", np.arange(rows).astype(str)).astype(object)
    customers = pa.table(
        {
            "customer_id": pa.array(ids, pa.string()),
            "action_id": pa.array(np.asarray(ACTIONS, dtype=object)[rng.integers(0, 3, rows)], pa.string()),
        }
    )
    order = rng.permutation(rows)
    contactability = pa.table(
        {
            "customer_id": pa.array(ids[order], pa.string()),
            "contactable_sms": pa.array(rng.random(rows) < 0.8),
            "contactable_email": pa.array(rng.random(rows) < 0.7),
        }
    )
    granted = rng.random(rows) < 0.3
    opt_ins = pa.table(
        {
            "customer_id": pa.array(ids[granted], pa.string()),
            "channel": pa.array(["whatsapp"] * int(granted.sum()), pa.string()),
            "consent_seq": pa.array(np.arange(int(granted.sum()), dtype=np.int64)),
        }
    )
    messages = {
        (action, channel): MessageVerdict(
            template_mismatch=action == "offer_b" and channel == "sms",
            url_not_whitelisted=action == "offer_c" and channel == "sms",
            catalogue_ref=f"{'a' * 64}#{action}",
        )
        for action in ACTIONS
        for channel in CHANNELS
    }
    return customers, contactability, opt_ins, messages


def _decide(rows: int, seed: int) -> float:
    customers, contactability, opt_ins, messages = _inputs(rows, seed)
    start = time.perf_counter()
    table = decide(
        customers,
        key_columns=("customer_id",),
        planned={None: (), **dict.fromkeys(ACTIONS, CHANNELS)},
        rules=GateRules(
            rule_version="r" * 64,
            purpose="marketing_communication",
            contactability_ref="channel_contactability.parquet#sha256=" + "c" * 64,
            opt_in_channels=("whatsapp",),
        ),
        contactability=contactability,
        opt_ins=opt_ins,
        messages=messages,
    )
    elapsed = time.perf_counter() - start
    assert table.num_rows == 3 * rows
    allowed = np.asarray(table["allowed"].to_numpy(zero_copy_only=False), dtype=bool)
    assert 0 < int(allowed.sum()) < 3 * rows
    return elapsed


@pytest.mark.slow
def test_a_million_customers_on_three_channels_are_decided_within_the_budget_and_linearly() -> None:
    small = large = float("inf")
    for _ in range(3):
        small = min(small, _decide(SMALL_ROWS, 5))
        large = min(large, _decide(ROWS, 6))
    print(
        f"\n[Perf] compliance gate decide: {ROWS:,} customers x {len(CHANNELS)} channels {large:.2f}s "
        f"(budget {BUDGET_SECONDS:.0f}s); {SMALL_ROWS:,} customers {small:.2f}s"
    )
    assert large < BUDGET_SECONDS, f"{ROWS:,} customers took {large:.2f}s"
    assert large < 30 * max(small, 0.05), "ten times the customers took far more than ten times as long"


def test_a_hundred_thousand_customers_on_three_channels_take_seconds_not_minutes() -> None:
    """The fast suite's guard: the 100k run alone, well inside a tenth of the 1M budget."""
    elapsed = min(_decide(SMALL_ROWS, 7) for _ in range(2))
    print(f"\n[Perf] compliance gate decide: {SMALL_ROWS:,} customers x 3 channels {elapsed:.2f}s")
    assert elapsed < BUDGET_SECONDS / 10


# ---------------------------------------------------------------------------
# The whole hand-off: gate_treat_list
# ---------------------------------------------------------------------------
RUN = "r_20261011_0c134b01"
CLIENT = "acme"
SALT = "gate-scale-salt-0001"
USE_CASE = "win-back-campaign"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
CATALOGUE = "actions:\n" + "".join(
    f"  - action_id: {action}\n    label: Offer {action[-1]}\n    channels: [whatsapp, sms, email]\n"
    for action in ACTIONS
)


def _hand_off_world(tmp_path: Path, config_root: Path) -> tuple[Path, Path]:
    """A config root with the gate on and the catalogue above; a data folder whose `platform.db` holds a
    WhatsApp grant of its own for every third of the 1,000,000 customers."""
    from sqlalchemy import insert

    from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
    from engine.privacy.config import load_privacy_config
    from engine.privacy.consent import ConsentLedger
    from engine.privacy.tables import ConsentRecordRow

    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    (root / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    (root / "compliance" / "compliance.yaml").write_text(
        "compliance:\n  gate:\n    enabled: true\n", encoding="utf-8"
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    purpose = load_privacy_config(root).purpose_for(USE_CASE)
    assert purpose is not None
    ledger = ConsentLedger(sqlite_engine(data_dir / PLATFORM_DB_FILENAME), salt=SALT)
    at = NOW - timedelta(days=5)
    rows = [
        {
            "client_id": CLIENT,
            "principal_hash": ledger.hash(f"C-{position}"),
            "purpose": purpose,
            "status": "granted",
            "source": "test",
            "recorded_at": at,
            "created_at": at,
            "channel": "whatsapp",
        }
        for position in range(0, ROWS, 3)
    ]
    with ledger.engine.begin() as connection:
        connection.execute(insert(ConsentRecordRow.__table__), rows)  # type: ignore[attr-defined]
    return root, data_dir


def _hand_off(root: Path, data_dir: Path, rows: int, seed: int) -> float:
    from engine.agentic.ledger import DecisionLedger, signer_for_data_dir
    from engine.compliance.handoff import gate_treat_list
    from engine.decide.catalogue import catalogue_sha256, clear_catalogue_cache
    from engine.platform_db import sqlite_engine
    from engine.storage import LocalStorage, run_key

    rng = np.random.default_rng(seed)
    ids = np.char.add("C-", np.arange(rows).astype(str)).astype(object)
    storage = LocalStorage(data_dir)
    order = rng.permutation(rows)
    contactability = pd.DataFrame(
        {
            "customer_id": ids[order],
            "contactable_sms": rng.random(rows) < 0.8,
            "contactable_email": rng.random(rows) < 0.7,
        }
    )
    buffer = io.BytesIO()
    contactability.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, "channel_contactability.parquet"), buffer.getvalue())
    clear_catalogue_cache()
    ledger = DecisionLedger(
        sqlite_engine(data_dir / f"ledger_{rows}_{seed}.db"),
        signer=signer_for_data_dir(data_dir),
        policy_version="p" * 64,
    )
    start = time.perf_counter()
    outcome = gate_treat_list(
        storage,
        run_id=RUN,
        use_case_id=USE_CASE,
        client_id=CLIENT,
        key_frame=pd.DataFrame({"customer_id": ids}),
        treat=np.ones(rows, dtype=bool),
        channel=np.full(rows, "whatsapp", dtype=object),
        actions=np.asarray(ACTIONS, dtype=object)[rng.integers(0, 3, rows)],
        planned_by_action=dict.fromkeys(ACTIONS, ("whatsapp", "sms", "email")),
        configured=("sms", "email"),
        stamp_sha256=catalogue_sha256(root),
        config_root=root,
        ledger=ledger,
        at=NOW,
    )
    elapsed = time.perf_counter() - start
    summary = outcome.summary
    assert summary.customers == rows and summary.rows == 3 * rows
    opted = (np.arange(rows) % 3) == 0
    assert int((outcome.channel == "whatsapp").sum()) == int(opted.sum()), "WhatsApp only to the opted in"
    assert 0 < summary.withheld_rows < rows and summary.rerouted_rows > 0
    return elapsed


@pytest.mark.slow
def test_a_million_customers_go_through_the_whole_hand_off_within_the_budget_and_linearly(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MARKETING_AI_PRIVACY_SALT", SALT)
    root, data_dir = _hand_off_world(tmp_path, config_root)
    small = large = float("inf")
    for attempt in range(3):
        small = min(small, _hand_off(root, data_dir, SMALL_ROWS, 10 + attempt))
        large = min(large, _hand_off(root, data_dir, ROWS, 20 + attempt))
    print(
        f"\n[Perf] compliance gate hand-off: {ROWS:,} customers x {len(CHANNELS)} channels {large:.2f}s "
        f"(budget {BUDGET_SECONDS:.0f}s); {SMALL_ROWS:,} customers {small:.2f}s"
    )
    assert large < BUDGET_SECONDS, f"{ROWS:,} customers took {large:.2f}s"
    assert large < 30 * max(small, 0.05), "ten times the customers took far more than ten times as long"
