"""The consent ledger: the latest record decides, expiry, hashing, and the CSV import (DEC-731, 733, 734)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.platform_db import sqlite_engine
from engine.privacy.config import PrivacyConfig, load_privacy_config
from engine.privacy.consent import ConsentLedger
from engine.privacy.contracts import ConsentStatus

CLIENT = "acme"
PURPOSE = "marketing_communication"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def ledger(tmp_path: Path) -> ConsentLedger:
    return ConsentLedger(sqlite_engine(tmp_path / "platform.db"), salt="acme")


@pytest.fixture
def privacy(config_root: Path) -> PrivacyConfig:
    return load_privacy_config(config_root)


def grant(ledger: ConsentLedger, principal: str, at: datetime, **kwargs: object) -> None:
    ledger.record(
        client_id=str(kwargs.get("client_id", CLIENT)),
        principal_id=principal,
        purpose=str(kwargs.get("purpose", PURPOSE)),
        status=ConsentStatus(str(kwargs.get("status", "granted"))),
        source="test",
        recorded_at=at,
        expires_at=kwargs.get("expires_at"),  # type: ignore[arg-type]
    )


def test_no_record_is_no_consent(ledger: ConsentLedger) -> None:
    assert ledger.has_ledger(CLIENT, PURPOSE) is False
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0) == set()


def test_the_latest_record_as_of_the_moment_decides(ledger: ConsentLedger) -> None:
    grant(ledger, "C-1", T0)
    grant(ledger, "C-1", T0 + timedelta(days=10), status="withdrawn")
    grant(ledger, "C-1", T0 + timedelta(days=20))
    assert ledger.has_ledger(CLIENT, PURPOSE) is True
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 + timedelta(days=5)) == {"C-1"}
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 + timedelta(days=15)) == set()
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 + timedelta(days=25)) == {"C-1"}
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 - timedelta(days=1)) == set()


def test_a_tie_at_the_same_instant_goes_to_the_record_written_later(ledger: ConsentLedger) -> None:
    grant(ledger, "C-1", T0)
    grant(ledger, "C-1", T0, status="withdrawn")
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 + timedelta(days=1)) == set()


def test_an_expired_grant_is_not_consent(ledger: ConsentLedger) -> None:
    grant(ledger, "C-1", T0, expires_at=T0 + timedelta(days=30))
    at = T0 + timedelta(days=31)
    verdict = ledger.classify(CLIENT, PURPOSE, ["C-1", "C-2"], at)
    assert verdict.expired == {"C-1"}
    assert verdict.missing == {"C-2"}
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 + timedelta(days=29)) == {"C-1"}


def test_purposes_and_clients_are_separate_ledgers(ledger: ConsentLedger) -> None:
    grant(ledger, "C-1", T0, purpose="account_servicing")
    grant(ledger, "C-1", T0, client_id="other")
    assert ledger.has_ledger(CLIENT, PURPOSE) is False
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], T0 + timedelta(days=1)) == set()
    assert ledger.valid_consent(CLIENT, "account_servicing", ["C-1"], T0 + timedelta(days=1)) == {"C-1"}


def test_numeric_keys_match_their_text(ledger: ConsentLedger) -> None:
    grant(ledger, "1024", T0)
    assert ledger.valid_consent(CLIENT, PURPOSE, [1024, 1024.0, " 1024 "], T0 + timedelta(days=1)) == {"1024"}  # type: ignore[list-item]


def test_the_ledger_never_stores_the_raw_id(ledger: ConsentLedger, tmp_path: Path) -> None:
    grant(ledger, "SENTINEL-PRINCIPAL-42", T0)
    ledger.engine.dispose()
    for path in tmp_path.iterdir():
        assert b"SENTINEL-PRINCIPAL-42" not in path.read_bytes(), path.name
    history = ledger.history("SENTINEL-PRINCIPAL-42")
    assert len(history) == 1
    assert history[0].principal_hash == ledger.hash("SENTINEL-PRINCIPAL-42")


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------
GOOD_CSV = (
    "principal_id,purpose,status,recorded_at,source,expires_at\n"
    "C-1,marketing_communication,granted,2026-01-01,web_form,\n"
    "C-2,marketing_communication,withdrawn,2026-01-02T10:00:00+05:30,call_centre,\n"
    "C-3,marketing_communication,granted,2026-01-03T00:00:00Z,,2026-06-01\n"
)


def test_a_clean_file_is_imported(ledger: ConsentLedger, privacy: PrivacyConfig) -> None:
    report = ledger.import_csv(GOOD_CSV, client_id=CLIENT, privacy=privacy, now=T0 + timedelta(days=30))
    assert report.imported is True
    assert (report.rows_read, report.rows_imported, report.errors) == (3, 3, ())
    at = T0 + timedelta(days=60)
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1", "C-2", "C-3"], at) == {"C-1", "C-3"}
    assert ledger.history("C-3")[0].source == "csv_import"


BAD_CSV = (
    "principal_id,purpose,status,recorded_at,expires_at,notes\n"
    "SECRET-1,marketing_communication,granted,2026-01-01,,x\n"
    "SECRET-2,profiling,granted,2026-01-01,,x\n"
    "SECRET-3,marketing_communication,maybe,2026-01-01,,x\n"
    "SECRET-4,marketing_communication,granted,01/02/2026,,x\n"
    ",marketing_communication,granted,2026-01-01,,x\n"
    "SECRET-6,marketing_communication,granted,2026-01-05,2026-01-01,x\n"
    "SECRET-7,marketing_communication,granted,2099-01-01,,x\n"
    "SECRET-8,marketing_communication,granted\n"
)


def test_errors_are_reported_by_row_and_column_never_by_value(
    ledger: ConsentLedger, privacy: PrivacyConfig
) -> None:
    report = ledger.import_csv(BAD_CSV, client_id=CLIENT, privacy=privacy, now=T0 + timedelta(days=30))
    assert report.imported is False
    assert report.rows_imported == 0
    assert ledger.has_ledger(CLIENT, PURPOSE) is False, "a refused import must write nothing"
    assert report.ignored_columns == ("notes",)
    by_row = {(error.row, error.code) for error in report.errors}
    assert by_row == {
        (3, "CONSENT_PURPOSE_UNKNOWN"),
        (4, "CONSENT_STATUS_INVALID"),
        (5, "CONSENT_TIME_INVALID"),
        (6, "CONSENT_PRINCIPAL_MISSING"),
        (7, "CONSENT_EXPIRY_BEFORE_RECORDED"),
        (8, "CONSENT_TIME_IN_FUTURE"),
        (9, "CONSENT_ROW_SHAPE"),
    }
    rendered = report.model_dump_json()
    for secret in ("SECRET-", "profiling", "maybe", "01/02/2026", "2099"):
        assert secret not in rendered, secret


def test_a_partial_import_writes_only_the_clean_rows(ledger: ConsentLedger, privacy: PrivacyConfig) -> None:
    report = ledger.import_csv(
        BAD_CSV, client_id=CLIENT, privacy=privacy, partial=True, now=T0 + timedelta(days=30)
    )
    assert report.imported is True
    assert report.rows_imported == 1
    assert ledger.valid_consent(CLIENT, PURPOSE, ["SECRET-1"], T0 + timedelta(days=1)) == {"SECRET-1"}


def test_a_file_without_the_required_columns_is_refused(
    ledger: ConsentLedger, privacy: PrivacyConfig
) -> None:
    report = ledger.import_csv(b"customer,flag\nC-1,true\n", client_id=CLIENT, privacy=privacy)
    assert report.imported is False
    assert report.errors[0].code == "CONSENT_CSV_COLUMNS_MISSING"
    assert report.errors[0].row == 1


def test_delete_history_removes_one_principal(ledger: ConsentLedger) -> None:
    grant(ledger, "C-1", T0)
    grant(ledger, "C-2", T0)
    assert ledger.delete_history("C-1") == 1
    assert ledger.history("C-1") == ()
    assert len(ledger.history("C-2")) == 1
