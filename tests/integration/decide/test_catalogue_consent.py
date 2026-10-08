"""The consent ledger per channel (Plan J M99, DEC-1309).

`consent_record.channel` is nullable: null means every channel, so every record stored before M99 keeps
applying to all of them. `ConsentLedger.classify(..., channel=ch)` decides by the latest of a customer's
all-channel records and their records for `ch`; `channel=None` (the scoring gate's all-channel question)
reads the all-channel records only.

The treat-list acceptance tests that used to live here wrote `scores.parquet` by hand with consent columns
a real run never writes (the M99 review's first finding); they are replaced by
`tests/integration/decide/test_channel_consent_real_run.py`, which builds every run with the pipeline.
The migration itself is pinned in `tests/unit/production/test_consent_channel_migration.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import create_engine

from engine.privacy.config import load_privacy_config
from engine.privacy.consent import ConsentLedger, ConsentStatus

pytestmark = pytest.mark.integration

PURPOSE = "marketing_communication"
SALT = "test-salt-12345678"
GRANTED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _ledger(tmp_path: Path) -> ConsentLedger:
    return ConsentLedger(create_engine(f"sqlite:///{tmp_path / 'platform.db'}"), salt=SALT, create=True)


def test_existing_consent_records_apply_to_all_channels(tmp_path: Path) -> None:
    """A record with no channel (every record from before M99) answers for every channel."""
    ledger = _ledger(tmp_path)
    ledger.record(
        client_id="client_1",
        principal_id="user_all",
        purpose=PURPOSE,
        status=ConsentStatus.GRANTED,
        source="test",
        recorded_at=GRANTED,
    )
    for channel in (None, "sms", "email", "whatsapp"):
        assert (
            "user_all" in ledger.classify("client_1", PURPOSE, ["user_all"], GRANTED, channel=channel).valid
        )


def test_a_channel_withdrawal_takes_that_channel_only(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(
        client_id="client_1",
        principal_id="user_all",
        purpose=PURPOSE,
        status=ConsentStatus.GRANTED,
        source="test",
        recorded_at=GRANTED,
    )
    ledger.record(
        client_id="client_1",
        principal_id="user_all",
        purpose=PURPOSE,
        status=ConsentStatus.WITHDRAWN,
        source="sms_optout",
        recorded_at=LATER,
        channel=" SMS ",
    )
    assert "user_all" in ledger.classify("client_1", PURPOSE, ["user_all"], LATER, channel="sms").withdrawn
    assert "user_all" in ledger.classify("client_1", PURPOSE, ["user_all"], LATER, channel="email").valid
    # The scoring gate's all-channel question is answered by the all-channel records: an SMS opt-out
    # does not suppress the customer; it only closes SMS.
    assert "user_all" in ledger.classify("client_1", PURPOSE, ["user_all"], LATER).valid
    assert ledger.history("user_all", client_id="client_1")[-1].channel == "sms"


def test_an_imported_channel_is_stored_in_lower_case_and_empty_means_every_channel(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    text = (
        "principal_id,purpose,status,recorded_at,channel\n"
        f"u1,{PURPOSE},granted,2026-10-01,\n"
        f"u1,{PURPOSE},withdrawn,2026-10-02,Email\n"
    )
    report = ledger.import_csv(text, client_id="client_1", privacy=load_privacy_config())
    assert report.imported and not report.errors
    assert [record.channel for record in ledger.history("u1", client_id="client_1")] == [None, "email"]
    assert "u1" in ledger.classify("client_1", PURPOSE, ["u1"], LATER, channel="email").withdrawn
    assert "u1" in ledger.classify("client_1", PURPOSE, ["u1"], LATER, channel="sms").valid
