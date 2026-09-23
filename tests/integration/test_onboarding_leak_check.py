"""M55, ruling R1 on DEC-096, end to end: the full leak check through `build_dataset`.

Two things are shown here with real files, real mappings and the whole build:

1. **The reason R1 keeps the full check (DEC-872).** A join-key bug makes one customer read another
   customer's events with no time bound. It is compiled by the real query builder and passes the
   real SQL guard (`assert_point_in_time` is left in place). The narrowed check, the default,
   rebuilds only the customers given future-dated events plus 500 others. It misses the bug, and
   the build *passes and writes a dataset*. The full check rebuilds every snapshot row, reports
   FUTURE_EVENTS_LEAKED and writes nothing. So does a first build of the recipe, whatever was asked.
2. **The nightly full check (DEC-873).** `test_the_full_check_passes_on_the_benchmark_tables` is
   marked `slow`, so `make test-all` (the nightly workflow) runs it and `make test` does not. It
   builds the M14/M37 benchmark tables (`scripts/bench_onboarding.py`, `tests/fixtures/raw/make_raw.py`)
   with `full_leak_check=True` and asserts that every snapshot row was probed and nothing leaked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.config import RunMode, StandardType, get_roles, load_use_case
from engine.contracts import Severity
from engine.onboarding import features as feature_engine
from engine.onboarding.build import _PROBE_CONTROL_ENTITIES, _PROBE_ROWS, build_dataset
from engine.onboarding.datasets import DATASET_FRAME_FILENAME, LocalDatasetRegistry, dataset_key
from engine.onboarding.features import POINT_IN_TIME_MARKER
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    AggFunction,
    BuildReport,
    ColumnTransform,
    DecidedBy,
    FeatureDef,
    FeatureSpec,
    MappingColumn,
    MappingSpec,
    OnboardingSpec,
    SnapshotDefinition,
    SnapshotMode,
    SourceSpec,
    TransformKind,
)
from engine.stages.ingest import dataset_fingerprint
from engine.storage import LocalStorage
from tests.fixtures.raw.make_raw import DEFAULT_SEED

pytestmark = [pytest.mark.integration]

USE_CASE = "telco-churn"
CLIENT = "cl_leak"

CUSTOMERS = 1_200
"""Enough rows for the Phase 1 checks to let a scoring build through, and more customers than the
narrowed probe rebuilds (the one given future rows plus `_PROBE_CONTROL_ENTITIES`)."""

INJECTED = CUSTOMERS
"""Customer 1,200 owns the first rows of the activity file, so every future row the probe adds is its."""

VICTIM = INJECTED - 1
"""The customer the join-key bug points at `INJECTED`: given no future row, and last but one in the
customer file, so it is not among the first 500 customers the narrowed probe takes as controls."""

DATA_START = date(2024, 1, 1)


# ---------------------------------------------------------------------------
# The client's files: integer customer ids, one activity log
# ---------------------------------------------------------------------------
def _customers() -> pd.DataFrame:
    keys = range(1, CUSTOMERS + 1)
    return pd.DataFrame(
        {
            "Cust ID": list(keys),
            "SIGNUP_DT": [(date(2023, 1, 1) + timedelta(days=key % 300)).isoformat() for key in keys],
        }
    )


def _activity() -> pd.DataFrame:
    """The injected customer's busy history first, then a few events for everyone else, by date."""
    busy = [(INJECTED, DATA_START + timedelta(days=index % 170)) for index in range(_PROBE_ROWS + 20)]
    rest = [
        (key, DATA_START + timedelta(days=(key * 7 + visit * 41) % 180))
        for key in range(1, CUSTOMERS)
        for visit in range(key % 4 + 1)
    ]
    rest.sort(key=lambda row: (row[1], row[0]))
    rows = busy + rest
    return pd.DataFrame(
        {
            "Cust ID": [key for key, _ in rows],
            "Event Date": [day.isoformat() for _, day in rows],
            "Event Type": ["login" if key % 2 else "recharge" for key, _ in rows],
        }
    )


def _cast(to: StandardType) -> ColumnTransform:
    return ColumnTransform(kind=TransformKind.CAST, to_type=to)


def _columns(pairs: list[tuple[str, str, ColumnTransform | None]]) -> tuple[MappingColumn, ...]:
    return tuple(
        MappingColumn(
            source=source, standard=standard, transform=transform, confidence=1.0, decided_by=DecidedBy.USER
        )
        for source, standard, transform in pairs
    )


MAPPINGS: dict[str, tuple[str, tuple[MappingColumn, ...]]] = {
    "src_customers": (
        "entity",
        _columns([("Cust ID", "entity_key", None), ("SIGNUP_DT", "signup_date", _cast(StandardType.DATE))]),
    ),
    "src_activity": (
        "activity",
        _columns(
            [
                ("Cust ID", "entity_key", None),
                ("Event Date", "event_time", _cast(StandardType.DATE)),
                ("Event Type", "event_type", _cast(StandardType.CATEGORICAL)),
            ]
        ),
    ),
}

FEATURES = FeatureSpec(
    features=(FeatureDef(name="activity_count", role="activity", function=AggFunction.COUNT),),
)


@dataclass(frozen=True)
class Built:
    report: BuildReport
    registry: LocalDatasetRegistry
    dataset_id: str

    @property
    def leaked(self) -> list[str]:
        return [check.code for check in self.report.checks if check.code == "FUTURE_EVENTS_LEAKED"]

    @property
    def wrote_a_dataset(self) -> bool:
        return self.registry.storage.exists(dataset_key(self.dataset_id, DATASET_FRAME_FILENAME))


def _build(root: Path, *, full_leak_check: bool = False, first_build_of_recipe: bool = False) -> Built:
    config = load_use_case(USE_CASE)
    storage = LocalStorage(root)
    sources: list[SourceSpec] = []
    for source_id, frame in (("src_customers", _customers()), ("src_activity", _activity())):
        key = f"sources/{source_id}/raw.csv"
        storage.write_text(key, frame.to_csv(index=False))
        sources.append(
            SourceSpec(
                source_id=source_id,
                client_id=CLIENT,
                file_name=f"{source_id}.csv",
                storage_key=key,
                file_format="csv",
                role=MAPPINGS[source_id][0],
                rows=len(frame),
                columns=tuple(str(name) for name in frame.columns),
                fingerprint=dataset_fingerprint(frame),
                created_at=datetime.now(UTC),
            )
        )
    mappings = tuple(
        MappingSpec(
            mapping_id=f"map_{source_id}",
            client_id=CLIENT,
            source_id=source_id,
            use_case=USE_CASE,
            role=role,
            columns=columns,
            created_at=datetime.now(UTC),
        ).with_hash()
        for source_id, (role, columns) in MAPPINGS.items()
    )
    spec = OnboardingSpec(
        spec_id="spec_leak",
        client_id=CLIENT,
        use_case=USE_CASE,
        entity_source_id="src_customers",
        event_source_ids=("src_activity",),
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in mappings)),
        feature_spec=FEATURES,
        label_spec=None,
        snapshot_spec=SnapshotDefinition(mode=SnapshotMode.SINGLE),
        created_at=datetime.now(UTC),
    ).with_hash()
    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(CLIENT, USE_CASE)
    report = build_dataset(
        spec=spec,
        config=config,
        sources=tuple(sources),
        mappings=mappings,
        reader=FileSourceReader(storage, config),
        registry=registry,
        dataset_id=dataset_id,
        mode=RunMode.SCORE,
        full_leak_check=full_leak_check,
        first_build_of_recipe=first_build_of_recipe,
    )
    return Built(report, registry, dataset_id)


def _linked_account_join(event: str, snapshot: str, *, inclusive: bool) -> str:
    """ "Own events, or the linked account's" (the next id), with the OR left unparenthesised.

    AND binds before OR, so only the own-events branch is bounded and the linked branch reads the
    other customer's whole history. The guard line - `AND e.event_time <= s.snapshot_date` and the
    marker - is exactly what `point_in_time_join_clause` writes, so `assert_point_in_time`, which
    reads the SQL text, accepts every query compiled with it. That is the gap the probe covers.
    """
    bound = "<=" if inclusive else "<"
    return (
        f"{event}.entity_key = {snapshot}.entity_key\n"
        f" AND {event}.event_time {bound} {snapshot}.snapshot_date  {POINT_IN_TIME_MARKER}\n"
        f" OR {event}.entity_key = {snapshot}.entity_key + 1"
    )


@pytest.fixture
def join_key_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one function that writes the join clause is swapped, as a future edit would change it;
    the SQL guard that re-reads every compiled query is left exactly as it is."""
    monkeypatch.setattr(feature_engine, "point_in_time_join_clause", _linked_account_join)


def test_a_sound_recipe_passes_the_full_check(tmp_path: Path) -> None:
    built = _build(tmp_path, full_leak_check=True)
    assert built.report.passed, [c.code for c in built.report.checks if c.severity is Severity.ERROR]
    check = built.report.leak_check
    assert check is not None
    assert (check.scope, check.reason) == ("full", "option")
    assert check.rows_probed == check.rows_total == CUSTOMERS
    assert built.leaked == []


def test_the_narrow_check_misses_the_join_key_bug_and_writes_the_dataset(
    tmp_path: Path, join_key_bug: None
) -> None:
    """DEC-096's stated blind spot, demonstrated: this is why R1 keeps the full check."""
    built = _build(tmp_path)
    check = built.report.leak_check
    assert check is not None
    assert (check.scope, check.reason) == ("narrow", "default")
    assert check.rows_probed == 1 + _PROBE_CONTROL_ENTITIES < check.rows_total
    assert built.leaked == []
    assert built.report.passed, "the narrowed check is expected to miss this bug"
    assert built.wrote_a_dataset
    frame = built.registry.read_frame(built.dataset_id).set_index("entity_key")
    # The victim's count holds the injected customer's whole history on top of its own.
    assert int(frame.loc[VICTIM, "activity_count"]) >= _PROBE_ROWS


@pytest.mark.parametrize(
    ("full_leak_check", "first_build_of_recipe", "reason"),
    [(True, False, "option"), (False, True, "first_build_of_recipe")],
)
def test_the_full_check_catches_the_join_key_bug_and_writes_nothing(
    tmp_path: Path, join_key_bug: None, full_leak_check: bool, first_build_of_recipe: bool, reason: str
) -> None:
    built = _build(tmp_path, full_leak_check=full_leak_check, first_build_of_recipe=first_build_of_recipe)
    check = built.report.leak_check
    assert check is not None
    assert (check.scope, check.reason) == ("full", reason)
    assert check.rows_probed == check.rows_total == CUSTOMERS
    leaked = [c for c in built.report.checks if c.code == "FUTURE_EVENTS_LEAKED"]
    assert leaked, [c.code for c in built.report.checks]
    assert all(c.severity is Severity.ERROR and not c.acknowledged for c in leaked)
    assert not built.report.passed
    assert not built.wrote_a_dataset


# ---------------------------------------------------------------------------
# Nightly: the full check on the benchmark tables
# ---------------------------------------------------------------------------
NIGHTLY_CUSTOMERS = 20_000
NIGHTLY_USAGE_ROWS = 500_000
"""The size the nightly full check builds at: the M37 benchmark tables, one tenth of the M14 target
in customers and usage rows, with the target's 12 snapshots and 60 features (240,000 snapshot rows).

Not the target itself (200,000 customers, 5,000,000 usage rows): measured on this repository's
4-CPU, 15.7 GiB container that size needs ~1.4 GB of CSVs, a build of about five minutes before the
full check is added, and a peak of about 9.3 GB of memory (docs/PERFORMANCE.md section 6). The
nightly workflow's runner is a standard GitHub-hosted one, the slow suite already takes about a
quarter of its hour, and a build that the runner kills for memory is not a check that runs. The
M37 size is the largest the benchmark has been measured at that fits comfortably (about a minute
to build, 1.2 GB peak). The target size is checked by hand on an idle machine with
`python -m scripts.bench_onboarding --customers 200000 --usage-rows 5000000 --full-leak-check`,
which prints the same `leak check` line this test asserts on (docs/PERFORMANCE.md section 7)."""


@pytest.mark.slow
def test_the_full_check_passes_on_the_benchmark_tables(tmp_path: Path) -> None:
    """R1: the full check runs nightly on the largest practical benchmark fixture (DEC-873).

    The recipe is the benchmark's - the detected roles, the suggested mappings and the first 60
    suggested features, over 12 monthly snapshots - so this is the build `scripts/bench_onboarding.py`
    times, with the full check on. Every snapshot row must be probed and nothing may leak.
    """
    from scripts.bench_onboarding import (
        TARGET_FEATURES,
        TARGET_SNAPSHOTS,
        measure_build,
        onboarding_spec,
        prepare_sources,
        read_specs,
    )

    storage = LocalStorage(tmp_path / "data")
    config = load_use_case(USE_CASE)
    prepare_sources(storage, customers=NIGHTLY_CUSTOMERS, usage_rows=NIGHTLY_USAGE_ROWS, seed=DEFAULT_SEED)
    sources, mappings = read_specs(storage)
    spec = onboarding_spec(
        config,
        sources,
        mappings,
        entity_role=get_roles().entity_role,
        snapshots=TARGET_SNAPSHOTS,
        features=TARGET_FEATURES,
    )

    measurement = measure_build(
        storage, config, spec=spec, sources=sources, mappings=mappings, full_leak_check=True
    )

    report = measurement.report
    errors = [(c.code, c.message) for c in report.checks if c.severity is Severity.ERROR]
    assert report.passed, errors
    check = report.leak_check
    assert check is not None
    assert (check.scope, check.reason) == ("full", "option")
    # The spine: every customer at every snapshot date it had already signed up by.
    assert report.rows_out <= check.rows_total <= NIGHTLY_CUSTOMERS * TARGET_SNAPSHOTS
    assert check.rows_probed == check.rows_total, "the full check must rebuild every snapshot row"
    assert "FUTURE_EVENTS_LEAKED" not in {c.code for c in report.checks}
    print(
        f"\nnightly full leak check: {check.rows_probed:,} snapshot rows probed, "
        f"build {measurement.seconds:.1f} s"
    )
