"""Tests for the synthetic raw-table generator itself (Phase 2 plan section 11).

`make_raw.py` is the fixture the onboarding flow is exercised and measured on, so a defect in it
looks like a defect in the engine. Four things are worth holding it to: that a seed really pins
every value (a benchmark that is not reproducible is not a benchmark), that the row counts a caller
asks for are the row counts it gets, that the signal the module docstring says it planted is
genuinely in the files, and that each broken variant carries the one defect it advertises.

The signal check is the one that earns its keep. It re-derives the label the way the engine does -
a customer with no activity in the 60 days after a snapshot has churned - and then measures a
feature the engine would build, `complaints_90d`, on each side of it. Nothing in any table says who
churned, so if this comparison came out flat the fixture would be teaching a model nothing and no
later test would notice.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from engine.stages.ingest import PII_DETECTORS
from tests.fixtures.raw.make_raw import (
    BROKEN,
    DATA_END,
    DUPLICATE_ROWS,
    KEY_DIGITS,
    LABEL_HORIZON_DAYS,
    RawTables,
    main,
    make_ambiguous_dates,
    make_duplicate_entities,
    make_leading_zero_keys,
    make_raw,
    make_unmatched_keys,
    make_unparseable_time,
)

#: The shared clean set. Big enough that the signal comparison has both groups populated, small
#: enough that generating it costs a fraction of a second.
CUSTOMERS = 600
USAGE_ROWS = 6_000

#: Sizes for the tests that only look at a table's shape and so need no statistical power.
SMALL_CUSTOMERS = 80
SMALL_USAGE_ROWS = 400

#: The snapshot the signal check stands at: the latest date whose 60-day outcome window still fits
#: inside the extract, so every churner's silence is observable.
SNAPSHOT = DATA_END - timedelta(days=LABEL_HORIZON_DAYS)
COMPLAINT_WINDOW_DAYS = 90
#: The lift in `complaints_90d` the planted coefficients produce at the default seed. Measured, not
#: chosen: at these sizes the churned group raises 1.51x as many tickets as the retained one.
MIN_COMPLAINT_LIFT = 1.25


@pytest.fixture(scope="module")
def clean(tmp_path_factory: pytest.TempPathFactory) -> RawTables:
    return make_raw(tmp_path_factory.mktemp("clean"), customers=CUSTOMERS, usage_rows=USAGE_ROWS)


def read(path) -> pd.DataFrame:
    """One table as written, every column text: these files have not been mapped or cast yet."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def churned_keys(tables: RawTables) -> set[str]:
    """The entities with no activity in the 60 days after `SNAPSHOT` - the label, re-derived."""
    activity = read(tables.activity)
    when = pd.to_datetime(activity["EVENT_DT"], format="%Y-%m-%d")
    horizon = pd.Timestamp(SNAPSHOT + timedelta(days=LABEL_HORIZON_DAYS))
    alive = set(activity.loc[(when > pd.Timestamp(SNAPSHOT)) & (when <= horizon), "CUST_ID"])
    return set(read(tables.customers)["CUST_ID"]) - alive


# ---------------------------------------------------------------------------
# Determinism and shape
# ---------------------------------------------------------------------------
def test_the_same_seed_writes_byte_identical_tables(tmp_path):
    first = make_raw(tmp_path / "a", customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    second = make_raw(tmp_path / "b", customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    for left, right in zip(first.paths, second.paths, strict=True):
        assert left.name == right.name
        assert left.read_bytes() == right.read_bytes()


def test_a_different_seed_writes_different_tables(tmp_path):
    """Without this, a generator that ignored `seed` entirely would pass the determinism test."""
    first = make_raw(tmp_path / "a", customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS, seed=1)
    second = make_raw(tmp_path / "b", customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS, seed=2)
    for left, right in zip(first.paths, second.paths, strict=True):
        assert left.read_bytes() != right.read_bytes()


@pytest.mark.parametrize("usage_rows", [SMALL_USAGE_ROWS, SMALL_USAGE_ROWS + 7])
def test_row_counts_honour_the_parameters(tmp_path, usage_rows):
    """`usage_rows` is exact even when it does not divide by the customer count."""
    tables = make_raw(tmp_path, customers=SMALL_CUSTOMERS, usage_rows=usage_rows)
    customers = read(tables.customers)
    usage = read(tables.usage)
    assert len(customers) == SMALL_CUSTOMERS
    assert customers["CUST_ID"].is_unique
    assert len(usage) == usage_rows
    assert set(usage["CUST_ID"]) == set(customers["CUST_ID"])


def test_no_table_names_the_outcome(clean):
    """Phase 2's whole point: churn is derived from an absence, never read off a column."""
    for path in clean.paths:
        for column in read(path).columns:
            assert "churn" not in column.lower()


# ---------------------------------------------------------------------------
# The planted signal
# ---------------------------------------------------------------------------
def test_churned_customers_complained_more_in_the_last_90_days(clean):
    churned = churned_keys(clean)
    retained = set(read(clean.customers)["CUST_ID"]) - churned
    # A degenerate split would make the comparison below meaningless while still passing it.
    assert 0.2 < len(churned) / CUSTOMERS < 0.6

    complaints = read(clean.complaints)
    raised = pd.to_datetime(complaints["TICKET_DT"], format="%Y-%m-%d")
    window = complaints.loc[
        (raised > pd.Timestamp(SNAPSHOT - timedelta(days=COMPLAINT_WINDOW_DAYS)))
        & (raised <= pd.Timestamp(SNAPSHOT)),
        "CUST_ID",
    ]
    counts = window.value_counts()
    churned_mean = sum(counts.get(key, 0) for key in churned) / len(churned)
    retained_mean = sum(counts.get(key, 0) for key in retained) / len(retained)
    assert churned_mean > retained_mean * MIN_COMPLAINT_LIFT


def test_remarks_carry_contacts_the_shipped_pii_patterns_recognise(clean):
    """The planted phone numbers and e-mail addresses are shapes the engine's own detectors know.

    `ingest.detect_pii` matches a *whole cell*, so it does not fire on a sentence with a number
    buried in it - the patterns are searched within the text here instead. That is the point of
    planting them: the redaction path for free text has something real to be tried against.
    """
    remarks = read(clean.complaints)["REMARKS"]
    patterns = {detector.kind: detector.value_pattern for detector in PII_DETECTORS}
    for kind in ("email", "phone"):
        pattern = patterns[kind]
        assert pattern is not None
        hits = sum(1 for line in remarks if pattern.search(line) is not None)
        assert hits > len(remarks) * 0.25


# ---------------------------------------------------------------------------
# The broken variants
# ---------------------------------------------------------------------------
def test_unmatched_keys_points_two_activity_rows_in_five_at_nobody(tmp_path):
    tables = make_unmatched_keys(tmp_path, customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    known = set(read(tables.customers)["CUST_ID"])
    keys = read(tables.activity)["CUST_ID"]
    assert 0.35 <= 1.0 - keys.isin(known).mean() <= 0.45


def test_leading_zero_keys_pads_the_usage_table_and_nothing_else(tmp_path):
    tables = make_leading_zero_keys(tmp_path, customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    known = set(read(tables.customers)["CUST_ID"])
    keys = read(tables.usage)["CUST_ID"]
    assert not any(key.startswith("0") for key in known)
    assert all(key.startswith("0") and len(key) == KEY_DIGITS * 2 for key in keys)
    assert {key.lstrip("0") for key in keys} <= known


def test_ambiguous_dates_cannot_be_told_apart_from_month_first(tmp_path):
    tables = make_ambiguous_dates(tmp_path, customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    bills = read(tables.bills)
    for column in ("BILL_DT", "DUE_DT", "PAID_DT"):
        written = [value for value in bills[column] if value]
        assert written
        for value in written:
            day, month, _ = value.split("/")
            assert 1 <= int(day) <= 12
            assert 1 <= int(month) <= 12


def test_duplicate_entities_breaks_one_row_per_entity(tmp_path):
    tables = make_duplicate_entities(tmp_path, customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    keys = read(tables.customers)["CUST_ID"]
    assert len(keys) == SMALL_CUSTOMERS + DUPLICATE_ROWS
    assert int(keys.duplicated().sum()) == DUPLICATE_ROWS


def test_unparseable_time_leaves_no_usage_row_a_date(tmp_path):
    tables = make_unparseable_time(tmp_path, customers=SMALL_CUSTOMERS, usage_rows=SMALL_USAGE_ROWS)
    stamps = read(tables.usage)["USAGE_DT"]
    assert stamps.str.fullmatch(r"FY\d{2}-W\d{2}").all()
    assert pd.to_datetime(stamps, errors="coerce", format="mixed").isna().all()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_the_cli_writes_the_clean_set_and_every_broken_variant(tmp_path):
    exit_code = main(["--out-dir", str(tmp_path), "--customers", "40", "--usage-rows", "200"])
    assert exit_code == 0
    for directory in ("clean", *BROKEN):
        assert (tmp_path / directory / "customers.csv").is_file()


def test_the_cli_rejects_an_unknown_broken_variant(tmp_path):
    argv = ["--out-dir", str(tmp_path), "--customers", "40", "--usage-rows", "200", "--broken", "nope"]
    assert main(argv) == 2
