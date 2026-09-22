"""Unit tests for `engine.onboarding.sources` (Phase 2 plan section 6.1, M8).

Six data shapes anchor the suite: a clean entity table, an event table with forty rows per key, a
table whose join key carries leading zeros, a table with a genuinely ambiguous date column, a table
with no time column at all, and a family of messy file names. Around them sit `FileSourceReader`
(driven through a real `LocalStorage`, so the `SourceReader` protocol is exercised end to end) and
direct tests of `join_coverage`/`key_format_mismatch`, which no fixture above happens to cover.

No use-case id is hard-coded: `config` is the first id `predictive_use_case_ids()` reports, exactly
as `tests/unit/test_ingest.py` does it, because `profile_source` only ever reads that config's
use-case-independent `.catalog`.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from engine.config import RoleCatalogue, RoleKind, RoleSpec, UseCaseConfig, get_roles, load_use_case
from engine.onboarding.sources import (
    JOIN_COVERAGE_OK,
    FileSourceReader,
    detect_roles,
    join_coverage,
    key_candidates,
    key_format_mismatch,
    profile_source,
    time_candidates,
)
from engine.onboarding.specs import SourceSpec
from engine.stages.ingest import dataset_fingerprint, profile_dataset
from engine.storage import LocalStorage
from tests.fixtures.make_data import predictive_use_case_ids

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def config() -> UseCaseConfig:
    return load_use_case(predictive_use_case_ids()[0])


@pytest.fixture(scope="module")
def roles() -> RoleCatalogue:
    return get_roles()


def entity_frame(n: int = 300) -> pd.DataFrame:
    """A clean customer master: one row per customer, low null rate, an obvious id column."""
    return pd.DataFrame(
        {
            "cust_id": [f"C{i:05d}" for i in range(n)],
            "signup_date": pd.date_range("2020-01-01", periods=n, freq="D").astype(str),
            "plan_type": (["basic", "premium", "standard"] * n)[:n],
            "region": (["north", "south", "east", "west"] * n)[:n],
        }
    )


def bills_frame(n_customers: int = 25, rows_per_customer: int = 40) -> pd.DataFrame:
    """An invoice log: `rows_per_customer` bills per customer, so `rows_per_key` is far above 1."""
    rows = [
        {
            "cust_id": f"C{customer:05d}",
            "invoice_date": f"2024-{(row % 12) + 1:02d}-01",
            "amount": 100.0 + row,
            "due_date": f"2024-{(row % 12) + 1:02d}-15",
            "paid_date": f"2024-{(row % 12) + 1:02d}-20",
            "status": "paid",
        }
        for customer in range(n_customers)
        for row in range(rows_per_customer)
    ]
    return pd.DataFrame(rows)


def profile_of(
    frame: pd.DataFrame, config: UseCaseConfig, *, roles: RoleCatalogue, file_name: str = "data.csv"
):
    return profile_source(
        frame,
        config,
        source_id="src_test",
        client_id="cli_test",
        file_name=file_name,
        roles=roles,
    )


def store_frame(tmp_path: Path, frame: pd.DataFrame, name: str) -> tuple[LocalStorage, str]:
    storage = LocalStorage(tmp_path / "store")
    key = f"clients/cli_test/sources/{name}"
    storage.write_bytes(key, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    return storage, key


def source_spec_for(frame: pd.DataFrame, storage_key: str, *, file_name: str) -> SourceSpec:
    import datetime

    fingerprint = dataset_fingerprint(frame)
    return SourceSpec(
        source_id="src_1",
        client_id="cli_1",
        file_name=file_name,
        storage_key=storage_key,
        file_format="csv",
        role=None,
        rows=len(frame),
        columns=tuple(str(c) for c in frame.columns),
        fingerprint=fingerprint,
        created_at=datetime.datetime.now(datetime.UTC),
    )


# ---------------------------------------------------------------------------
# key_candidates
# ---------------------------------------------------------------------------
def test_key_candidates_ranks_the_clean_id_first(config: UseCaseConfig) -> None:
    profile = profile_dataset(
        entity_frame(),
        config,
        upload_id="u1",
        file_name="customers.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    candidates = key_candidates(profile)
    assert candidates, "an entity table must yield at least one key candidate"
    assert candidates[0].column == "cust_id"
    assert candidates[0].rows_per_key == pytest.approx(1.0)
    assert candidates[0].null_rate == 0.0


def test_key_candidates_rows_per_key_reflects_repetition(config: UseCaseConfig) -> None:
    frame = bills_frame(n_customers=10, rows_per_customer=40)
    profile = profile_dataset(
        frame,
        config,
        upload_id="u2",
        file_name="bills.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    by_name = {c.column: c for c in key_candidates(profile)}
    assert by_name["cust_id"].rows_per_key == pytest.approx(40.0)
    assert by_name["cust_id"].distinct_count == 10


def test_key_candidates_drops_all_null_columns(config: UseCaseConfig) -> None:
    frame = entity_frame(n=50)
    frame["empty_col"] = pd.Series([None] * 50, dtype="object")
    profile = profile_dataset(
        frame,
        config,
        upload_id="u3",
        file_name="customers.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    names = [c.column for c in key_candidates(profile)]
    assert "empty_col" not in names


def test_key_candidates_prefers_low_null_rate_over_id_like_name(config: UseCaseConfig) -> None:
    """A perfectly clean, non-id-named column outranks an id-named column with nulls."""
    n = 200
    frame = pd.DataFrame(
        {
            "clean_ref": [f"R{i:05d}" for i in range(n)],
            "customer_id": [f"C{i:05d}" if i % 10 else None for i in range(n)],
        }
    )
    profile = profile_dataset(
        frame,
        config,
        upload_id="u4",
        file_name="refs.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    candidates = key_candidates(profile)
    assert candidates[0].column == "clean_ref"


# ---------------------------------------------------------------------------
# time_candidates
# ---------------------------------------------------------------------------
def test_time_candidates_empty_when_no_date_column(config: UseCaseConfig) -> None:
    frame = pd.DataFrame({"cust_id": [f"C{i}" for i in range(100)], "plan_type": ["a", "b"] * 50})
    profile = profile_dataset(
        frame,
        config,
        upload_id="u5",
        file_name="no_dates.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    assert time_candidates(frame, profile) == ()


def test_time_candidates_reports_parse_rate_and_range(config: UseCaseConfig) -> None:
    frame = bills_frame(n_customers=5, rows_per_customer=10)
    profile = profile_dataset(
        frame,
        config,
        upload_id="u6",
        file_name="bills.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    candidates = {c.column: c for c in time_candidates(frame, profile)}
    assert "invoice_date" in candidates
    assert candidates["invoice_date"].parse_rate == pytest.approx(1.0)
    assert candidates["invoice_date"].earliest is not None
    assert candidates["invoice_date"].latest is not None
    assert candidates["invoice_date"].earliest <= candidates["invoice_date"].latest


def test_time_candidates_flags_genuine_day_first_ambiguity(config: UseCaseConfig) -> None:
    """ "03/04/2025" reads as two different dates day-first vs month-first; both parse cleanly."""
    frame = pd.DataFrame(
        {
            "id": [f"E{i:04d}" for i in range(60)],
            "event_time": ["03/04/2025"] * 30 + ["07/04/2025"] * 30,
        }
    )
    profile = profile_dataset(
        frame,
        config,
        upload_id="u7",
        file_name="events.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    candidates = {c.column: c for c in time_candidates(frame, profile)}
    assert candidates["event_time"].day_first_ambiguous is True


def test_time_candidates_not_ambiguous_when_day_exceeds_twelve(config: UseCaseConfig) -> None:
    """ "15/04/2025" can only be day-first (no month 15 exists), so the two readings agree."""
    frame = pd.DataFrame(
        {
            "id": [f"E{i:04d}" for i in range(40)],
            "event_time": ["15/04/2025"] * 40,
        }
    )
    profile = profile_dataset(
        frame,
        config,
        upload_id="u8",
        file_name="events.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    candidates = {c.column: c for c in time_candidates(frame, profile)}
    assert candidates["event_time"].day_first_ambiguous is False


def test_time_candidates_not_ambiguous_for_iso_dates(config: UseCaseConfig) -> None:
    frame = bills_frame(n_customers=3, rows_per_customer=5)
    profile = profile_dataset(
        frame,
        config,
        upload_id="u9",
        file_name="bills.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    candidates = {c.column: c for c in time_candidates(frame, profile)}
    assert candidates["invoice_date"].day_first_ambiguous is False


def test_time_candidates_ignores_numeric_columns(config: UseCaseConfig) -> None:
    """A column Phase 1 already typed as numeric is never re-examined as a date (branch 8 before 9)."""
    frame = pd.DataFrame({"amount": [20250101 + i for i in range(50)], "id": [f"C{i}" for i in range(50)]})
    profile = profile_dataset(
        frame,
        config,
        upload_id="u10",
        file_name="amounts.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    assert time_candidates(frame, profile) == ()


def test_time_candidates_below_parse_floor_are_excluded(config: UseCaseConfig) -> None:
    values = ["2024-01-01"] * 50 + ["not a date"] * 60
    frame = pd.DataFrame({"maybe_date": values, "id": [f"C{i}" for i in range(110)]})
    profile = profile_dataset(
        frame,
        config,
        upload_id="u11",
        file_name="mixed.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    assert time_candidates(frame, profile) == ()


# ---------------------------------------------------------------------------
# detect_roles / profile_source
# ---------------------------------------------------------------------------
def test_detects_entity_role_for_customer_master(config: UseCaseConfig, roles: RoleCatalogue) -> None:
    profile = profile_of(entity_frame(), config, roles=roles, file_name="Customer_Master_Jan2024.csv")
    assert profile.role_candidates, "detect_roles must never return an empty tuple"
    assert profile.role_candidates[0].role == "entity"
    assert profile.role_candidates[0].confidence > 0
    assert any("customer" in reason for reason in profile.role_candidates[0].reasons)


def test_detects_bills_role_for_forty_rows_per_key(config: UseCaseConfig, roles: RoleCatalogue) -> None:
    profile = profile_of(bills_frame(), config, roles=roles, file_name="invoices_2024.csv")
    assert profile.role_candidates[0].role == "bills"
    # every other role scores strictly less: the top candidate is not merely present but ahead.
    assert profile.role_candidates[0].confidence > profile.role_candidates[1].confidence


def test_role_candidates_are_confidence_sorted(config: UseCaseConfig, roles: RoleCatalogue) -> None:
    profile = profile_of(bills_frame(), config, roles=roles, file_name="invoices_2024.csv")
    confidences = [c.confidence for c in profile.role_candidates]
    assert confidences == sorted(confidences, reverse=True)


def test_detect_roles_is_callable_standalone_on_a_source_profile(
    config: UseCaseConfig, roles: RoleCatalogue
) -> None:
    """`detect_roles` is one of the free functions the task asks to be unit-testable without a
    `Storage`: build a `SourceProfile` by hand from `profile_dataset` plus `key_candidates` and
    `time_candidates`, then call `detect_roles` directly rather than through `profile_source`."""
    frame = bills_frame(n_customers=6, rows_per_customer=40)
    dataset_profile = profile_dataset(
        frame,
        config,
        upload_id="direct",
        file_name="invoices_2024.csv",
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
    )
    from engine.onboarding.specs import SourceProfile

    source_profile = SourceProfile(
        source_id="direct",
        client_id="cli",
        file_name="invoices_2024.csv",
        profile=dataset_profile,
        key_candidates=key_candidates(dataset_profile),
        time_candidates=time_candidates(frame, dataset_profile),
    )
    candidates = detect_roles(source_profile, roles)
    assert candidates
    assert candidates[0].role == "bills"


@pytest.mark.parametrize(
    "file_name",
    ["CUST-Master (v2).CSV", "billing_Q1_FINAL.csv", "  Invoices 2024  .csv", "TICKETS-export.CSV"],
)
def test_messy_file_names_still_pick_up_a_token(
    config: UseCaseConfig, roles: RoleCatalogue, file_name: str
) -> None:
    """A file name with casing, spacing, punctuation and version tags still matches a role token."""
    frame = entity_frame(n=50) if "cust" in file_name.lower() else bills_frame(n_customers=5)
    profile = profile_of(frame, config, roles=roles, file_name=file_name)
    top = profile.role_candidates[0]
    assert any("file name contains" in reason for reason in top.reasons)


def test_no_time_column_and_no_ratio_match_still_yields_a_weak_entity_signal(config: UseCaseConfig) -> None:
    """No filename match and a rows-per-key ratio (1.3) in the dead zone between the entity (<=1.2)
    and event (>=1.5) thresholds still leaves the entity role with one real signal - the *absence*
    of a dominant timestamp is evidence on its own, not nothing - so this is a genuine low-confidence
    detection, not the empty-list fallback."""
    minimal_roles = RoleCatalogue(
        schema_version=1,
        roles={
            "entity": RoleSpec(kind=RoleKind.ENTITY, name_tokens=(), typical_columns=()),
            "other_event": RoleSpec(kind=RoleKind.EVENT, name_tokens=(), typical_columns=()),
        },
    )
    keys = [f"K{i}" for i in range(100)] + [f"K{i}" for i in range(30)]  # 130 rows, 100 keys: ratio 1.3
    frame = pd.DataFrame({"ref": keys, "note": ["n"] * 130})
    profile = profile_of(frame, config, roles=minimal_roles, file_name="mystery_data.csv")
    assert len(profile.role_candidates) == 1
    assert profile.role_candidates[0].role == "entity"
    assert profile.role_candidates[0].confidence > 0
    assert "no dominant timestamp column" in profile.role_candidates[0].reasons[0]


def test_ties_among_undistinguished_event_roles_resolve_to_other_event(
    config: UseCaseConfig, roles: RoleCatalogue
) -> None:
    """A dated, event-shaped table whose filename and columns name no particular role still lands on
    `other_event`: every event role collects the same ratio-and-time credit, and the tie resolves to
    the role that exists precisely to mean "none of the named ones fit"."""
    frame = bills_frame(n_customers=10, rows_per_customer=6)
    frame = frame.rename(
        columns={
            "invoice_date": "ts",
            "amount": "value",
            "due_date": "d2",
            "paid_date": "d3",
            "status": "flag",
        }
    )
    profile = profile_of(frame, config, roles=roles, file_name="misc_export_2024.csv")
    assert profile.role_candidates[0].role == "other_event"


def test_fallback_prefers_other_event_when_the_catalogue_has_one_and_a_date_column_exists(
    config: UseCaseConfig,
) -> None:
    """Forcing every role to score exactly zero (a dominant date column, but a rows-per-key ratio in
    the dead zone between the entity and event thresholds, and a filename naming nothing) reaches the
    literal fallback branch, which must pick `other_event`, not `entity`, once a date column exists."""
    catalogue = RoleCatalogue(
        schema_version=1,
        roles={
            "entity": RoleSpec(kind=RoleKind.ENTITY, name_tokens=(), typical_columns=()),
            "other_event": RoleSpec(kind=RoleKind.EVENT, name_tokens=(), typical_columns=()),
        },
    )
    keys = [f"K{i}" for i in range(100)] + [f"K{i}" for i in range(30)]  # 130 rows, 100 keys: ratio 1.3
    frame = pd.DataFrame(
        {
            "id": keys,
            "ts": ["2024-01-01"] * 50 + ["2024-01-02"] * 50 + ["2024-01-03"] * 30,
        }
    )
    profile = profile_of(frame, config, roles=catalogue, file_name="data_export_misc.csv")
    assert len(profile.role_candidates) == 1
    candidate = profile.role_candidates[0]
    assert candidate.role == "other_event"
    assert candidate.confidence > 0
    assert "date column" in candidate.reasons[0]


def test_fallback_to_entity_is_honest_when_a_date_column_exists_but_no_event_role_does(
    config: UseCaseConfig,
) -> None:
    """A catalogue with no event role at all must still fall back to `entity`, and the reason must
    not claim there was no date column when there plainly was one (house rule 2)."""
    single_role_catalogue = RoleCatalogue(schema_version=1, roles={"entity": RoleSpec(kind=RoleKind.ENTITY)})
    rows = [{"id": f"K{i}", "ts": f"2024-01-{(j % 28) + 1:02d}"} for i in range(20) for j in range(10)]
    frame = pd.DataFrame(rows)
    profile = profile_of(frame, config, roles=single_role_catalogue, file_name="generic_export.csv")
    assert len(profile.role_candidates) == 1
    candidate = profile.role_candidates[0]
    assert candidate.role == "entity"
    assert "date column" not in candidate.reasons[0]


# ---------------------------------------------------------------------------
# join_coverage / key_format_mismatch
# ---------------------------------------------------------------------------
def test_join_coverage_full_match() -> None:
    entity_keys = pd.Series([f"C{i}" for i in range(100)])
    event_keys = pd.Series([f"C{i}" for i in range(50)])
    assert join_coverage(event_keys, entity_keys) == 1.0


def test_join_coverage_partial_match() -> None:
    entity_keys = pd.Series([f"C{i}" for i in range(50)])
    event_keys = pd.Series([f"C{i}" for i in range(100)])  # half of these ids do not exist
    coverage = join_coverage(event_keys, entity_keys)
    assert coverage == pytest.approx(0.5)


def test_join_coverage_ignores_null_event_keys() -> None:
    entity_keys = pd.Series(["A", "B"])
    event_keys = pd.Series(["A", None, None, None])
    assert join_coverage(event_keys, entity_keys) == 1.0


def test_join_coverage_vacuous_when_no_non_null_event_keys() -> None:
    entity_keys = pd.Series(["A", "B"])
    event_keys = pd.Series([None, None])
    assert join_coverage(event_keys, entity_keys) == 1.0


def test_key_format_mismatch_leading_zeros() -> None:
    entity_keys = pd.Series([str(i) for i in range(1, 101)])
    event_keys = pd.Series([f"{i:06d}" for i in range(1, 101)])
    assert join_coverage(event_keys, entity_keys) < JOIN_COVERAGE_OK
    assert key_format_mismatch(event_keys, entity_keys) == "lstrip_zeros"


def test_key_format_mismatch_case() -> None:
    entity_keys = pd.Series([f"CUST{i}" for i in range(1, 101)])
    event_keys = pd.Series([f"cust{i}" for i in range(1, 101)])
    assert key_format_mismatch(event_keys, entity_keys) == "lower"


def test_key_format_mismatch_whitespace() -> None:
    entity_keys = pd.Series([f"K{i}" for i in range(1, 101)])
    event_keys = pd.Series([f" K{i} " for i in range(1, 101)])
    assert key_format_mismatch(event_keys, entity_keys) == "strip"


def test_key_format_mismatch_none_when_already_covered() -> None:
    entity_keys = pd.Series([f"Y{i}" for i in range(1, 101)])
    event_keys = pd.Series([f"Y{i}" for i in range(1, 91)])
    assert key_format_mismatch(event_keys, entity_keys) is None


def test_key_format_mismatch_none_when_unfixable() -> None:
    entity_keys = pd.Series([f"X{i}" for i in range(1, 101)])
    event_keys = pd.Series([f"totally-different-{i}" for i in range(1, 101)])
    assert key_format_mismatch(event_keys, entity_keys) is None


# ---------------------------------------------------------------------------
# FileSourceReader (SourceReader protocol, through a real LocalStorage)
# ---------------------------------------------------------------------------
def test_file_source_reader_read_returns_the_frame(tmp_path: Path, config: UseCaseConfig) -> None:
    frame = entity_frame(n=20)
    storage, key = store_frame(tmp_path, frame, "customers.csv")
    spec = source_spec_for(frame, key, file_name="customers.csv")
    reader = FileSourceReader(storage, config)
    read_back = reader.read(spec)
    assert list(read_back.columns) == list(frame.columns)
    assert len(read_back) == 20


def test_file_source_reader_profile_returns_a_source_profile_with_roles(
    tmp_path: Path, config: UseCaseConfig
) -> None:
    frame = bills_frame(n_customers=8, rows_per_customer=40)
    storage, key = store_frame(tmp_path, frame, "invoices_2024.csv")
    spec = source_spec_for(frame, key, file_name="invoices_2024.csv")
    reader = FileSourceReader(storage, config)
    profile = reader.profile(spec)
    assert profile.source_id == spec.source_id
    assert profile.client_id == spec.client_id
    assert profile.file_name == "invoices_2024.csv"
    assert profile.role_candidates
    assert profile.role_candidates[0].role == "bills"
    assert profile.key_candidates
    assert profile.time_candidates
    assert profile.profile.row_count == len(frame)


def test_file_source_reader_read_respects_max_rows(tmp_path: Path, config: UseCaseConfig) -> None:
    frame = entity_frame(n=50)
    storage, key = store_frame(tmp_path, frame, "customers.csv")
    spec = source_spec_for(frame, key, file_name="customers.csv")
    reader = FileSourceReader(storage, config)
    read_back = reader.read(spec, max_rows=5)
    assert len(read_back) <= 5


# ---------------------------------------------------------------------------
# profile_source itself
# ---------------------------------------------------------------------------
def test_profile_source_reuses_source_id_as_upload_id(config: UseCaseConfig, roles: RoleCatalogue) -> None:
    profile = profile_source(
        entity_frame(n=30),
        config,
        source_id="src_abc",
        client_id="cli_1",
        file_name="customers.csv",
        roles=roles,
    )
    assert profile.source_id == "src_abc"
    assert profile.profile.upload_id == "src_abc"
    assert profile.role is None
    assert profile.role_decided_by is None


def test_profile_source_defaults_to_the_shipped_role_catalogue(config: UseCaseConfig) -> None:
    """`roles=None` reads `configs/roles.yaml` through `get_roles()`, not an empty catalogue."""
    profile = profile_source(
        entity_frame(n=30),
        config,
        source_id="src_def",
        client_id="cli_1",
        file_name="customers.csv",
    )
    assert profile.role_candidates
    assert profile.role_candidates[0].role in get_roles().names
