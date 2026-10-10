"""Plan K M131 (DEC-1323) part B and C: the consent basis, operator registration, and the per-channel rules.

TRAI's Third Amendment: an inquiry-based consent lasts 7 days, and a legacy consent counts only when it is
registered on the operators' digital platform. The ledger records `basis` and `operator_registered`;
`ConsentLedger.classify(..., channel=..., basis_rules=...)` applies a region's `ConsentBasisRules` to the
channels it lists, and only to a grant that has not expired. Without rules, or on another channel, the
answer is exactly what it was (`test_consent_ledger.py`, unchanged).
"""

from __future__ import annotations

import time
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, text

from engine.audit.events import principal_hash
from engine.platform_db import sqlite_engine
from engine.privacy.config import PrivacyConfig, load_privacy_config
from engine.privacy.consent import (
    CONSENT_CSV_OPTIONAL,
    ConsentBasisRules,
    ConsentClassification,
    ConsentLedger,
    principal_key,
)
from engine.privacy.contracts import ConsentStatus
from engine.privacy.tables import CONSENT_RECORD_TABLE, ConsentRecordRow
from engine.utils.time import utc_now

CLIENT = "acme"
PURPOSE = "marketing_communication"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SALT = "basis-salt-0001"
REPO_ROOT = Path(__file__).resolve().parents[3]
INDIA = ConsentBasisRules(
    channels=("sms", "call", "ivr"), inquiry_valid_days=7, legacy_requires_operator_registration=True
)
BUCKETS = ("valid", "withdrawn", "expired", "missing", "inquiry_lapsed", "legacy_unregistered")


@pytest.fixture
def ledger(tmp_path: Path) -> ConsentLedger:
    return ConsentLedger(sqlite_engine(tmp_path / "platform.db"), salt=SALT)


@pytest.fixture
def privacy(config_root: Path) -> PrivacyConfig:
    return load_privacy_config(config_root)


def put(
    ledger: ConsentLedger,
    principal: str,
    at: datetime = T0,
    *,
    status: str = "granted",
    basis: str | None = None,
    registered: bool | None = None,
    channel: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    ledger.record(
        client_id=CLIENT,
        principal_id=principal,
        purpose=PURPOSE,
        status=ConsentStatus(status),
        source="test",
        recorded_at=at,
        expires_at=expires_at,
        channel=channel,
        basis=basis,
        operator_registered=registered,
    )


def verdict(
    ledger: ConsentLedger,
    principals: list[str],
    at: datetime,
    channel: str | None = "sms",
    rules: ConsentBasisRules | None = INDIA,
) -> ConsentClassification:
    return ledger.classify(CLIENT, PURPOSE, principals, at, channel=channel, basis_rules=rules)


# ---------------------------------------------------------------------------
# The rules model
# ---------------------------------------------------------------------------
def test_the_rules_are_frozen_normalised_and_hashed_canonically() -> None:
    rules = ConsentBasisRules.model_validate(
        {
            "channels": [" SMS", "call", "ivr", "sms"],
            "inquiry_valid_days": 7,
            "legacy_requires_operator_registration": True,
        }
    )
    assert rules == INDIA
    assert rules.sha256() == INDIA.sha256()
    assert len(rules.sha256()) == 64
    assert rules.sha256() != ConsentBasisRules(channels=("sms",), inquiry_valid_days=7).sha256()
    with pytest.raises(ValueError):
        rules.inquiry_valid_days = 3
    with pytest.raises(ValueError, match="not a channel name"):
        ConsentBasisRules(channels=("e-mail",))
    with pytest.raises(ValueError):
        ConsentBasisRules(channels=("sms",), inquiry_valid_days=0)
    with pytest.raises(ValueError):
        ConsentBasisRules.model_validate({"channels": ["sms"], "invented": 1})


def test_the_rules_apply_only_to_a_listed_channel_never_to_the_all_channel_question() -> None:
    assert INDIA.applies_to("sms") and INDIA.applies_to(" IVR ")
    assert not INDIA.applies_to("email") and not INDIA.applies_to(None)


# ---------------------------------------------------------------------------
# Acceptance 2: inquiry consents
# ---------------------------------------------------------------------------
def test_an_inquiry_grant_is_valid_for_seven_days_on_sms_then_lapses(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="inquiry")
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=6, hours=21)).valid == {"C-1"}  # day 6.9
    just_before = verdict(ledger, ["C-1"], T0 + timedelta(days=7) - timedelta(seconds=1))
    assert just_before.valid == {"C-1"}
    at_seven = verdict(ledger, ["C-1"], T0 + timedelta(days=7))
    assert at_seven.inquiry_lapsed == {"C-1"} and at_seven.valid == frozenset()
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=30)).inquiry_lapsed == {"C-1"}


def test_an_inquiry_grant_is_still_valid_on_a_channel_the_rules_do_not_list(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="inquiry")
    later = T0 + timedelta(days=30)
    assert verdict(ledger, ["C-1"], later, channel="email").valid == {"C-1"}
    assert verdict(ledger, ["C-1"], later, channel=None).valid == {
        "C-1"
    }, "the all-channel question has no rules"


def test_without_rules_an_inquiry_grant_is_valid_at_day_thirty(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="inquiry")
    later = T0 + timedelta(days=30)
    assert verdict(ledger, ["C-1"], later, rules=None).valid == {"C-1"}
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], later, channel="sms") == {"C-1"}
    assert ledger.valid_consent(CLIENT, PURPOSE, ["C-1"], later, channel="sms", basis_rules=INDIA) == set()


def test_rules_without_an_inquiry_limit_leave_inquiry_grants_alone(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="inquiry")
    rules = ConsentBasisRules(channels=("sms",), legacy_requires_operator_registration=True)
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=400), rules=rules).valid == {"C-1"}


def test_an_inquiry_grant_on_a_channel_record_lapses_too(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="inquiry", channel="sms")
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=8)).inquiry_lapsed == {"C-1"}
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=8), channel="email").missing == {"C-1"}


def test_a_fresh_grant_after_a_lapsed_one_counts_again(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="inquiry")
    put(ledger, "C-1", T0 + timedelta(days=9), basis="inquiry")
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=10)).valid == {"C-1"}
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=17)).inquiry_lapsed == {"C-1"}


# ---------------------------------------------------------------------------
# Acceptance 3: legacy consents, withdrawal, expiry, one bucket each
# ---------------------------------------------------------------------------
def test_a_legacy_grant_counts_only_when_recorded_as_registered(ledger: ConsentLedger) -> None:
    put(ledger, "registered", basis="legacy", registered=True)
    put(ledger, "false", basis="legacy", registered=False)
    put(ledger, "unknown", basis="legacy", registered=None)
    result = verdict(ledger, ["registered", "false", "unknown"], T0 + timedelta(days=400))
    assert result.valid == {"registered"}
    assert result.legacy_unregistered == {"false", "unknown"}, "null is never counted as registered"


def test_a_grant_with_no_basis_or_an_explicit_basis_is_valid_whatever_its_registration(
    ledger: ConsentLedger,
) -> None:
    put(ledger, "none", basis=None, registered=None)
    put(ledger, "explicit", basis="explicit", registered=False)
    put(ledger, "explicit-null", basis="Explicit ", registered=None)
    result = verdict(ledger, ["none", "explicit", "explicit-null"], T0 + timedelta(days=400))
    assert result.valid == {"none", "explicit", "explicit-null"}


def test_legacy_rule_is_off_when_the_region_does_not_require_registration(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", basis="legacy", registered=False)
    rules = ConsentBasisRules(channels=("sms",), inquiry_valid_days=7)
    assert verdict(ledger, ["C-1"], T0 + timedelta(days=400), rules=rules).valid == {"C-1"}


def test_a_later_withdrawal_still_wins_and_an_expired_grant_is_still_expired(ledger: ConsentLedger) -> None:
    put(ledger, "withdrawn-inquiry", T0, basis="inquiry")
    put(ledger, "withdrawn-inquiry", T0 + timedelta(days=2), status="withdrawn", basis="inquiry")
    put(ledger, "withdrawn-legacy", T0, basis="legacy", registered=False)
    put(ledger, "withdrawn-legacy", T0 + timedelta(days=2), status="withdrawn", basis="legacy")
    put(ledger, "expired-inquiry", T0, basis="inquiry", expires_at=T0 + timedelta(days=3))
    put(ledger, "expired-legacy", T0, basis="legacy", registered=False, expires_at=T0 + timedelta(days=3))
    ids = ["withdrawn-inquiry", "withdrawn-legacy", "expired-inquiry", "expired-legacy"]
    result = verdict(ledger, ids, T0 + timedelta(days=20))
    assert result.withdrawn == {"withdrawn-inquiry", "withdrawn-legacy"}
    assert result.expired == {"expired-inquiry", "expired-legacy"}
    assert result.inquiry_lapsed == frozenset() and result.legacy_unregistered == frozenset()


def test_a_withdrawal_recorded_for_the_channel_only_closes_that_channel(ledger: ConsentLedger) -> None:
    put(ledger, "C-1", T0, basis="legacy", registered=True)
    put(ledger, "C-1", T0 + timedelta(days=1), status="withdrawn", channel="sms")
    at = T0 + timedelta(days=5)
    assert verdict(ledger, ["C-1"], at).withdrawn == {"C-1"}
    assert verdict(ledger, ["C-1"], at, channel="call").valid == {"C-1"}


def test_every_principal_is_in_exactly_one_bucket_over_a_mixed_set(ledger: ConsentLedger) -> None:
    """Property-style: a mixed set, on every channel and at several moments, with and without rules."""
    ids: list[str] = []
    for n in range(240):
        key = f"P-{n:03d}"
        ids.append(key)
        kind = n % 12
        base = T0 - timedelta(days=(n * 7) % 30)
        if kind == 0:
            continue  # no record at all
        basis = (None, "explicit", "inquiry", "legacy")[kind % 4]
        registered = (None, True, False)[n % 3]
        put(ledger, key, base, basis=basis, registered=registered, channel=(None, "sms", "email")[n % 3])
        if kind in (5, 6):
            put(ledger, key, base + timedelta(days=2), status="withdrawn", basis=basis)
        if kind in (7, 8):
            put(ledger, key, base + timedelta(days=1), basis=basis, expires_at=base + timedelta(days=6))
    for rules in (INDIA, None):
        for channel in ("sms", "call", "email", None):
            for days in (0, 3, 6, 7, 9, 40):
                result = verdict(ledger, ids, T0 + timedelta(days=days), channel=channel, rules=rules)
                buckets = [getattr(result, name) for name in BUCKETS]
                assert sum(len(bucket) for bucket in buckets) == len(set(ids))
                assert set().union(*buckets) == set(ids)
                for first in range(len(buckets)):
                    for second in range(first + 1, len(buckets)):
                        assert not buckets[first] & buckets[second]
                if rules is None or channel not in (None, "email"):
                    continue
                assert not result.inquiry_lapsed and not result.legacy_unregistered


def test_the_four_older_buckets_are_exactly_todays_when_no_rules_are_given(ledger: ConsentLedger) -> None:
    put(ledger, "a", T0, basis="inquiry")
    put(ledger, "b", T0, basis="legacy", registered=False)
    put(ledger, "c", T0, status="withdrawn")
    result = ledger.classify(CLIENT, PURPOSE, ["a", "b", "c", "d"], T0 + timedelta(days=90), channel="sms")
    assert result.valid == {"a", "b"} and result.withdrawn == {"c"} and result.missing == {"d"}
    assert result.expired == frozenset()
    assert result.inquiry_lapsed == frozenset() and result.legacy_unregistered == frozenset()


# ---------------------------------------------------------------------------
# Recording: unknown basis, normalised basis, the contract
# ---------------------------------------------------------------------------
def test_an_unknown_basis_is_refused_and_a_known_one_is_normalised(ledger: ConsentLedger) -> None:
    with pytest.raises(ValueError, match="basis is not one of"):
        put(ledger, "C-1", basis="verbal")
    assert ledger.history("C-1", client_id=CLIENT) == ()
    put(ledger, "C-1", basis=" Inquiry ", registered=True)
    put(ledger, "C-2")
    (record,) = ledger.history("C-1", client_id=CLIENT)
    assert record.basis is not None and record.basis.value == "inquiry" and record.operator_registered is True
    (plain,) = ledger.history("C-2", client_id=CLIENT)
    assert plain.basis is None and plain.operator_registered is None, "not recorded stays null"


# ---------------------------------------------------------------------------
# Acceptance 4: CSV import
# ---------------------------------------------------------------------------
def test_the_two_columns_are_optional_import_columns() -> None:
    assert {"basis", "operator_registered"} <= set(CONSENT_CSV_OPTIONAL)


def test_the_two_columns_parse_and_are_stored(ledger: ConsentLedger, privacy: PrivacyConfig) -> None:
    rows = [
        "principal_id,purpose,status,recorded_at,basis,operator_registered",
        f"c1,{PURPOSE},granted,2026-10-01,inquiry,",
        f"c2,{PURPOSE},granted,2026-10-01,LEGACY,Yes",
        f"c3,{PURPOSE},granted,2026-10-01,legacy,0",
        f"c4,{PURPOSE},granted,2026-10-01,,TRUE",
        f"c5,{PURPOSE},granted,2026-10-01,explicit,no",
        f"c6,{PURPOSE},granted,2026-10-01, ,1",
    ]
    report = ledger.import_csv("\n".join(rows) + "\n", client_id=CLIENT, privacy=privacy, now=T0)
    assert report.imported and report.rows_imported == 6 and not report.errors
    got = {key: ledger.history(key, client_id=CLIENT)[0] for key in ("c1", "c2", "c3", "c4", "c5", "c6")}
    shown = {
        key: (None if record.basis is None else record.basis.value, record.operator_registered)
        for key, record in got.items()
    }
    assert shown == {
        "c1": ("inquiry", None),
        "c2": ("legacy", True),
        "c3": ("legacy", False),
        "c4": (None, True),
        "c5": ("explicit", False),
        "c6": (None, True),
    }
    assert report.ignored_columns == ()


def test_a_float_written_flag_from_a_parquet_column_with_gaps_is_read_as_one_and_zero(
    ledger: ConsentLedger, privacy: PrivacyConfig
) -> None:
    """A nullable int column becomes float64 and is written as `1.0`/`0.0`/empty; the import must not refuse it."""
    rows = [
        "principal_id,purpose,status,recorded_at,basis,operator_registered",
        f"c1,{PURPOSE},granted,2026-10-01,legacy,1.0",
        f"c2,{PURPOSE},granted,2026-10-01,legacy,0.0",
        f"c3,{PURPOSE},granted,2026-10-01,legacy,",
    ]
    report = ledger.import_csv("\n".join(rows) + "\n", client_id=CLIENT, privacy=privacy, now=T0)
    assert report.imported and report.rows_imported == 3 and not report.errors
    got = [ledger.history(key, client_id=CLIENT)[0].operator_registered for key in ("c1", "c2", "c3")]
    assert got == [True, False, None]


def test_bad_values_are_reported_by_row_and_column_without_quoting_them(
    ledger: ConsentLedger, privacy: PrivacyConfig
) -> None:
    secret_basis, secret_flag = "verbal-promise-9", "maybe-77"
    rows = [
        "principal_id,purpose,status,recorded_at,basis,operator_registered",
        f"c1,{PURPOSE},granted,2026-10-01,{secret_basis},true",
        f"c2,{PURPOSE},granted,2026-10-01,legacy,{secret_flag}",
        f"c3,{PURPOSE},granted,2026-10-01,{secret_basis},{secret_flag}",
        f"c4,{PURPOSE},granted,2026-10-01,legacy,true",
    ]
    report = ledger.import_csv("\n".join(rows) + "\n", client_id=CLIENT, privacy=privacy, now=T0)
    assert [(e.row, e.column, e.code) for e in report.errors] == [
        (2, "basis", "CONSENT_BASIS_INVALID"),
        (3, "operator_registered", "CONSENT_REGISTERED_INVALID"),
        (4, "basis", "CONSENT_BASIS_INVALID"),
        (4, "operator_registered", "CONSENT_REGISTERED_INVALID"),
    ]
    for error in report.errors:
        assert secret_basis not in error.message and secret_flag not in error.message
    assert not report.imported and report.rows_imported == 0, "all or nothing"
    partial = ledger.import_csv(
        "\n".join(rows) + "\n", client_id=CLIENT, privacy=privacy, partial=True, now=T0
    )
    assert partial.rows_imported == 1
    assert ledger.history("c4", client_id=CLIENT)[0].operator_registered is True


def test_a_file_without_the_columns_imports_exactly_as_before(
    ledger: ConsentLedger, privacy: PrivacyConfig
) -> None:
    rows = ["principal_id,purpose,status,recorded_at", f"c1,{PURPOSE},granted,2026-10-01"]
    report = ledger.import_csv("\n".join(rows) + "\n", client_id=CLIENT, privacy=privacy, now=T0)
    assert report.imported and report.errors == () and report.ignored_columns == ()
    record = ledger.history("c1", client_id=CLIENT)[0]
    assert (record.basis, record.operator_registered, record.channel) == (None, None, None)
    assert verdict(ledger, ["c1"], T0 + timedelta(days=100)).valid == {"c1"}


# ---------------------------------------------------------------------------
# Acceptance 5: the schema upgrade
# ---------------------------------------------------------------------------
def _config(path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"), cmd_opts=Namespace(x=[f"url=sqlite:///{path}"]))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def _columns(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {row[1] for row in connection.execute(text(f"PRAGMA table_info({CONSENT_RECORD_TABLE})"))}


def _old_row(engine: Engine, hashed: str, status: str = "granted") -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO consent_record (client_id, principal_hash, purpose, status, source, recorded_at, "
                "created_at) VALUES ('acme', :hash, :purpose, :status, 'csv_import', "
                "'2026-09-01 10:00:00', '2026-09-01 10:00:00')"
            ),
            {"hash": hashed, "purpose": PURPOSE, "status": status},
        )


def test_an_old_sqlite_platform_db_is_upgraded_in_place_and_old_rows_read_back_unrecorded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "platform.db"
    command.upgrade(_config(path), "0007")  # the table as Plan J M99 left it
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert not {"basis", "operator_registered"} & _columns(engine)
        _old_row(engine, principal_hash(principal_key("C-1"), salt=SALT))
        ledger = ConsentLedger(engine, salt=SALT, create=False)  # the scoring seam's way in
        assert {"basis", "operator_registered"} <= _columns(engine)
        (record,) = ledger.history("C-1", client_id=CLIENT)
        assert record.basis is None and record.operator_registered is None
        later = datetime(2027, 6, 1, tzinfo=UTC)
        assert verdict(ledger, ["C-1"], later).valid == {"C-1"}, "unrecorded basis reads as explicit"
        put(ledger, "C-2", T0, basis="inquiry")
        assert verdict(ledger, ["C-2"], T0 + timedelta(days=8)).inquiry_lapsed == {"C-2"}
    finally:
        engine.dispose()


def test_0008_adds_both_columns_keeps_old_rows_and_downgrades_to_0007(tmp_path: Path) -> None:
    path = tmp_path / "chain.db"
    config = _config(path)
    command.upgrade(config, "0007")
    engine = create_engine(f"sqlite:///{path}")
    try:
        _old_row(engine, "hash-1")
        command.upgrade(config, "0008")
        assert {"basis", "operator_registered"} <= _columns(engine)
        with engine.connect() as connection:
            row = connection.execute(text("SELECT basis, operator_registered FROM consent_record")).one()
        assert tuple(row) == (None, None)
        command.downgrade(config, "0007")
        assert not {"basis", "operator_registered"} & _columns(engine)
        assert "channel" in _columns(engine)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM consent_record")).scalar_one() == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Acceptance 8: linear time
# ---------------------------------------------------------------------------
def _bulk(engine: Engine, ledger: ConsentLedger, rows: int) -> list[str]:
    """`rows` grants written in one statement: a quarter each of no basis, explicit, inquiry and legacy."""
    ids = [f"customer-{n}" for n in range(rows)]
    now = utc_now()
    records = [
        {
            "client_id": CLIENT,
            "principal_hash": ledger.hash(key),
            "purpose": PURPOSE,
            "status": "granted",
            "source": "bulk",
            "recorded_at": now - timedelta(days=1 + n % 20),
            "created_at": now,
            "basis": (None, "explicit", "inquiry", "legacy")[n % 4],
            "operator_registered": (None, True, False)[n % 3],
        }
        for n, key in enumerate(ids)
    ]
    with engine.begin() as connection:
        connection.execute(insert(cast(Any, ConsentRecordRow).__table__), records)
    return ids


def _best(
    ledger: ConsentLedger, ids: list[str], rules: ConsentBasisRules | None
) -> tuple[float, ConsentClassification]:
    at = utc_now()
    best = float("inf")
    result: ConsentClassification | None = None
    for _ in range(3):
        start = time.perf_counter()
        result = ledger.classify(CLIENT, PURPOSE, ids, at, channel="sms", basis_rules=rules)
        best = min(best, time.perf_counter() - start)
    assert result is not None
    return best, result


@pytest.mark.slow
def test_classify_with_rules_costs_a_small_multiple_of_classify_without(tmp_path: Path) -> None:
    """Slow because the two classifications of 200k principals (best of three each) take about 40 s."""
    engine = sqlite_engine(tmp_path / "platform.db")
    ledger = ConsentLedger(engine, salt=SALT)
    ids = _bulk(engine, ledger, 200_000)
    without, plain = _best(ledger, ids, None)
    with_rules, ruled = _best(ledger, ids, INDIA)
    assert with_rules <= 2.0 * without + 0.5, f"with rules {with_rules:.2f}s, without {without:.2f}s"
    assert len(plain.valid) == len(ids), "without rules every grant counts"
    assert ruled.inquiry_lapsed and ruled.legacy_unregistered and ruled.valid
    assert len(ruled.valid) + len(ruled.inquiry_lapsed) + len(ruled.legacy_unregistered) == len(ids)
