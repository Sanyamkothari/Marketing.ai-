"""Measure one whole onboarding build on synthetic raw client tables, and print what was measured.

Why this script exists
----------------------
Phase 2 plan section 11 makes "200k customers, 5M usage events, 12 monthly snapshots and 60
features build in under 5 minutes on a laptop" part of milestone M14's definition of done, and plan
section 13.3 forbids inventing a number that reaches a reader. `engine/onboarding/build.py` pushes
the work into DuckDB - the mapped tables are registered as views and every feature is one grouped
point-in-time join - but "DuckDB does the join" is an argument about the code, not a result.
Nothing had ever timed a build. Until this script is *run*, any row count and any number of seconds
in `README.md` would be a claim; this is the thing that turns them into a measurement, on a named
machine, with the command to reproduce it.

What is measured, and what is only setup
----------------------------------------
One number is the result: `build_dataset` over the recipe below, taken with `time.perf_counter`
around the call. That is the whole of plan section 6.5 - the mappings and their transforms, the
snapshot spine, one query per event role, the label, the assembled frame, the leak probe, the
onboarding and Phase 1 checks, and `dataset.parquet` on disk. Nothing is stubbed and no stage is
skipped. The per-stage breakdown printed under it is not a second stopwatch: it is
`build_status.json`, the document the Build screen polls, read back after the build with the
`duration_seconds` the build itself recorded.

Everything else is **setup and is reported as setup**: generating the six client tables, profiling
them, detecting their roles and suggesting their mappings. Those are the Sources and Mapping
screens, they happen once per client and a human sits between them and the build, so folding them
into the build's seconds would answer a question nobody asked.

The recipe is not hand-written. The role of each table comes from `detect_roles`, its mapping from
`suggested_mapping_spec`, and the features from `suggested_features` - the same three functions the
UI drives - so what is timed is a build of the recipe this engine would actually propose for these
tables, not one tuned to be fast. `--features` slices that suggestion; the library generates far
more than the target's sixty.

Why setup runs in a separate process
------------------------------------
Peak memory is read from `resource.getrusage(RUSAGE_SELF).ru_maxrss`, which is a *high-water mark*:
it only ever rises, and it cannot be reset. Building five million usage rows in pandas and writing
them to CSV is itself expensive, so doing it in this process would raise the mark before the
measured call and the reported peak would be the generator's, not the build's. Generation,
profiling and mapping therefore run in a spawned child process, which leaves its `SourceSpec` and
`MappingSpec` documents in storage exactly as the Sources and Mapping screens would; this process
reads them back and builds. The child's resident set never enters this process's mark.

`tracemalloc` was the other candidate and is the wrong instrument here: it counts only blocks
allocated through Python's allocator, which misses the pandas, Arrow and DuckDB buffers that hold
nearly all of a build.

Run it with::

    .venv/bin/python -m scripts.bench_onboarding --customers 200000 --usage-rows 5000000
"""

from __future__ import annotations

import argparse
import multiprocessing
import shutil
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Final

from tests.fixtures.raw.make_raw import DATA_END, DEFAULT_SEED, USAGE_FILE, make_raw

from engine.config import RunMode, StandardType, get_roles, load_use_case
from engine.onboarding.build import build_dataset
from engine.onboarding.datasets import LocalDatasetRegistry
from engine.onboarding.features import suggested_features
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import FileSourceReader
from engine.onboarding.specs import (
    BuildStatus,
    FeatureSpec,
    MappingSpec,
    OnboardingSpec,
    SnapshotSpec,
    SourceSpec,
)
from engine.storage import LocalStorage
from engine.utils.logging import configure_logging
from engine.utils.time import utc_now
from scripts.bench_large_file import machine_line, peak_memory_mb

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engine.config import UseCaseConfig
    from engine.onboarding.specs import BuildReport

COMMAND: Final[str] = "python -m scripts.bench_onboarding"

USE_CASE: Final[str] = "telco-churn"
"""The use case the benchmark runs on; `tests/integration/test_onboarding_flow.py` drives the same
one, and `tests/fixtures/raw/make_raw.py` writes the tables a telecom client would hand over."""

CLIENT: Final[str] = "cl_bench"

SOURCES_PREFIX: Final[str] = "sources"

SOURCE_FILENAME: Final[str] = "source.json"
MAPPING_FILENAME: Final[str] = "mapping.json"

USAGE_SOURCE_ID: Final[str] = f"src_{Path(USAGE_FILE).stem}"
"""The source the plan's "5M usage events" is about; every source id is `src_` plus the file stem."""

DEFAULT_CUSTOMERS: Final[int] = 2_000
DEFAULT_USAGE_ROWS: Final[int] = 50_000
"""`make_raw`'s own defaults: a run that finishes in seconds, so the script is usable as a check
that the build still works at all rather than only as the M14 measurement."""

TARGET_CUSTOMERS: Final[int] = 200_000
TARGET_USAGE_ROWS: Final[int] = 5_000_000
TARGET_SNAPSHOTS: Final[int] = 12
TARGET_FEATURES: Final[int] = 60
TARGET_SECONDS: Final[float] = 300.0
"""plan section 11's M14 numbers: the size and the time the definition of done names."""

PEAK_MEMORY_BASIS: Final[str] = (
    "resource.getrusage(RUSAGE_SELF).ru_maxrss - the high-water mark of this process's resident "
    "set size, so it covers every allocation pandas, Arrow and DuckDB made during the build, not "
    "only Python-allocator blocks. Generating, profiling and mapping the tables ran in a separate "
    "process and is excluded."
)


# ---------------------------------------------------------------------------
# Setup (a separate process; see the module docstring)
# ---------------------------------------------------------------------------
def write_sources(root: Path, customers: int, usage_rows: int, seed: int) -> None:
    """Generate the client's tables under `root`, then profile, role-detect and map each one.

    The `SourceSpec` and `MappingSpec` that come out are written into storage beside the CSVs, which
    is how they cross the process boundary: they are documents the real flow saves anyway, so
    nothing here is a benchmark-only channel.

    This is a module-level function rather than a closure because the spawned child process has to
    import it by name.
    """
    from engine.onboarding.sources import profile_source
    from engine.stages.ingest import read_upload

    storage = LocalStorage(root)
    config = load_use_case(USE_CASE)
    tables = make_raw(
        storage.local_path(SOURCES_PREFIX), customers=customers, usage_rows=usage_rows, seed=seed
    )
    for path in tables.paths:
        source_id = f"src_{path.stem}"
        key = f"{SOURCES_PREFIX}/{path.name}"
        read = read_upload(storage, key)
        if read.fingerprint is None:
            raise RuntimeError(f"{path.name} was read without a fingerprint; its lineage would be a guess")
        profile = profile_source(
            read.frame,
            config,
            source_id=source_id,
            client_id=CLIENT,
            file_name=path.name,
            file_format=read.file_format,
            file_size_bytes=storage.size_bytes(key),
            delimiter=read.delimiter,
            encoding=read.encoding,
            row_count=read.row_count,
            fingerprint=read.fingerprint,
        )
        if not profile.role_candidates:
            raise RuntimeError(f"no role was detected for {path.name}; there is no recipe to build")
        role = profile.role_candidates[0].role
        source = SourceSpec(
            source_id=source_id,
            client_id=CLIENT,
            file_name=path.name,
            storage_key=key,
            file_format=read.file_format,
            role=role,
            rows=read.row_count,
            columns=tuple(str(name) for name in read.frame.columns),
            fingerprint=read.fingerprint,
            created_at=utc_now(),
        )
        mapping = suggested_mapping_spec(
            profile, config, role=role, use_case=USE_CASE, mapping_id=f"map_{path.stem}"
        ).with_hash()
        storage.write_model(f"{SOURCES_PREFIX}/{source_id}/{SOURCE_FILENAME}", source)
        storage.write_model(f"{SOURCES_PREFIX}/{source_id}/{MAPPING_FILENAME}", mapping)


def prepare_sources(storage: LocalStorage, *, customers: int, usage_rows: int, seed: int) -> float:
    """Run the setup in a spawned child process; returns the wall-clock seconds it took.

    A child that dies takes the benchmark with it: half-written tables would silently turn the
    measurement into a measurement of something else.
    """
    started = perf_counter()
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=write_sources, args=(storage.root, customers, usage_rows, seed))
    child.start()
    child.join()
    if child.exitcode != 0:
        raise RuntimeError(f"the source generator exited with code {child.exitcode}; nothing was built")
    return perf_counter() - started


def read_specs(storage: LocalStorage) -> tuple[tuple[SourceSpec, ...], tuple[MappingSpec, ...]]:
    """The documents the child left behind, source id order."""
    keys = sorted(key for key in storage.list_keys(SOURCES_PREFIX) if key.endswith(SOURCE_FILENAME))
    sources = tuple(storage.read_model(key, SourceSpec) for key in keys)
    mappings = tuple(
        storage.read_model(key.replace(SOURCE_FILENAME, MAPPING_FILENAME), MappingSpec) for key in keys
    )
    return sources, mappings


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------
def feature_spec(config: UseCaseConfig, mappings: Sequence[MappingSpec], *, count: int) -> FeatureSpec:
    """The first `count` features the engine would offer for these mappings.

    A slice rather than a sample: `suggested_features` puts the use case's own named features first
    and the generated library after them, so a slice is "what a client would accept from the top of
    the list", which is the recipe worth timing.

    Only the columns `configs/roles.yaml` types as numeric are offered to the library, which
    generates a sum and a mean over every column it is handed: `SUM(event_type)` is not a query
    DuckDB will run, and a benchmark that stopped there would be measuring the suggester rather than
    the build. The filter lives here, in a script, because the fix belongs in `engine/` and this
    branch does not own that file.
    """
    roles = get_roles()
    mapped = {
        mapping.role: [
            column.standard
            for column in mapping.columns
            if (typical := roles.require(mapping.role).typical(column.standard)) is not None
            and typical.type is StandardType.NUMERIC
        ]
        for mapping in mappings
    }
    offered = suggested_features(config, mapped)
    if len(offered) < count:
        raise RuntimeError(
            f"these mappings offer {len(offered)} feature(s); --features {count} cannot be honoured"
        )
    return FeatureSpec(features=offered[:count])


def onboarding_spec(
    config: UseCaseConfig,
    sources: Sequence[SourceSpec],
    mappings: Sequence[MappingSpec],
    *,
    entity_role: str,
    snapshots: int,
    features: int,
) -> OnboardingSpec:
    """The recipe: the detected roles, the suggested mappings and features, the use case's label.

    The last snapshot is placed a label horizon before the end of the extract, which is where
    `plan_snapshots` would put it anyway; naming it keeps the benchmark from measuring a build whose
    last date is censored and dropped, so the snapshot count asked for is the snapshot count built.
    """
    label = config.label
    if label is None or label.horizon_days is None:
        raise RuntimeError(f"{USE_CASE} declares no label with a horizon; there is nothing to build")
    entity = [source for source in sources if source.role == entity_role]
    if len(entity) != 1:
        detected = ", ".join(f"{source.source_id}={source.role}" for source in sources)
        raise RuntimeError(f"exactly one entity source is needed; roles detected were {detected}")
    return OnboardingSpec(
        spec_id="spec_bench",
        client_id=CLIENT,
        use_case=USE_CASE,
        entity_source_id=entity[0].source_id,
        event_source_ids=tuple(sorted(source.source_id for source in sources if source.role != entity_role)),
        mapping_ids=tuple(sorted(mapping.mapping_id for mapping in mappings)),
        feature_spec=feature_spec(config, mappings, count=features),
        label_spec=label,
        snapshot_spec=SnapshotSpec(
            end=DATA_END - timedelta(days=label.horizon_days), max_snapshots=snapshots
        ),
        created_at=utc_now(),
    ).with_hash()


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Measurement:
    """One timed build: what it cost, and what came out of it."""

    seconds: float
    report: BuildReport
    status: BuildStatus
    spec: OnboardingSpec

    @property
    def snapshots_built(self) -> int:
        """Snapshot dates that survived; the rest were censored by the label horizon and dropped."""
        return sum(1 for stat in self.report.snapshots if not stat.censored)

    @property
    def features_dropped(self) -> int:
        """Features the build computed and then dropped for nulls, as its own configured limit says."""
        return sum(1 for stat in self.report.features if stat.dropped)

    def rows_from(self, source_id: str) -> int:
        """Rows the build read from one source, as the build counted them."""
        return sum(stat.rows for stat in self.report.sources if stat.source_id == source_id)


def measure_build(
    storage: LocalStorage,
    config: UseCaseConfig,
    *,
    spec: OnboardingSpec,
    sources: Sequence[SourceSpec],
    mappings: Sequence[MappingSpec],
) -> Measurement:
    """Time one `build_dataset`, then read back the status document it wrote.

    The status is read rather than re-timed because the build already recorded each stage's
    `duration_seconds` as it ran; a second stopwatch around the same stages would be a different
    number pretending to be the same one.
    """
    registry = LocalDatasetRegistry(storage)
    dataset_id = registry.new_dataset_id(CLIENT, USE_CASE)
    started = perf_counter()
    report = build_dataset(
        spec=spec,
        config=config,
        sources=tuple(sources),
        mappings=tuple(mappings),
        reader=FileSourceReader(storage, config),
        registry=registry,
        dataset_id=dataset_id,
        mode=RunMode.TRAIN,
    )
    seconds = perf_counter() - started
    return Measurement(seconds, report, registry.read_status(dataset_id), spec)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def verdict_lines(measurement: Measurement) -> tuple[str, str]:
    """The target, and whether this run met it - in that order, and never more than this run saw.

    A run smaller than the target is not a failed run and is not a passed one: it measured
    something else, and so did a run whose checks refused to write a dataset. Saying so is the only
    honest verdict available in either case, because nothing here can extrapolate one size to
    another or time a build that did not finish its job.
    """
    target = (
        f"{TARGET_CUSTOMERS:,} customers · {TARGET_USAGE_ROWS:,} usage rows · "
        f"{TARGET_SNAPSHOTS} snapshots · {TARGET_FEATURES} features in under {TARGET_SECONDS:,.0f} s"
    )
    if not measurement.report.passed:
        blocking = measurement.report.error_count
        return target, f"NOT ANSWERED - the build found {blocking} blocking error(s) and wrote no dataset"
    short = [
        name
        for name, seen, wanted in (
            ("customers", measurement.rows_from(measurement.spec.entity_source_id), TARGET_CUSTOMERS),
            ("usage rows", measurement.rows_from(USAGE_SOURCE_ID), TARGET_USAGE_ROWS),
            ("snapshots", measurement.snapshots_built, TARGET_SNAPSHOTS),
            ("features", len(measurement.report.features), TARGET_FEATURES),
        )
        if seen < wanted
    ]
    if short:
        return target, f"NOT ANSWERED - this run was smaller than the target ({', '.join(short)})"
    met = "MET" if measurement.seconds < TARGET_SECONDS else "NOT MET"
    return target, f"{met} - built at or above every target size in {measurement.seconds:,.1f} s"


def print_report(
    *,
    measurement: Measurement,
    setup_seconds: float,
    table_bytes: int,
    seed: int,
    peak_mb: float,
) -> None:
    """Print the summary table. Every number here was measured by this run; none is projected."""
    report = measurement.report
    rule = "=" * 78
    target, verdict = verdict_lines(measurement)
    print(rule)
    print("Onboarding build benchmark (Phase 2 plan §11, milestone M14)")
    print(rule)
    print(f"machine      : {machine_line()}")
    print(f"client tables: {len(report.sources)} CSV · {table_bytes / 1_000_000:.1f} MB · seed {seed}")
    print(f"use case     : {USE_CASE} · passed={report.passed} · errors={report.error_count}")
    print()
    print("setup (NOT part of the measured result)")
    print(f"  generate + profile + map : {setup_seconds:8.1f} s   (separate process; excluded from peak)")
    print()
    print("sources read")
    print(f"  {'source':<18}{'role':<14}{'rows':>14}{'coverage':>12}")
    for stat in report.sources:
        coverage = "-" if stat.join_coverage is None else f"{stat.join_coverage:.3f}"
        print(f"  {stat.source_id:<18}{stat.role:<14}{stat.rows:>14,}{coverage:>12}")
    print()
    print("measured")
    print(f"  {'stage':<26}{'seconds':>12}")
    for stage in measurement.status.stages:
        seconds = "-" if stage.duration_seconds is None else f"{stage.duration_seconds:.1f}"
        print(f"  {stage.key:<26}{seconds:>12}")
    print(f"  {'total (build_dataset)':<26}{measurement.seconds:>12.1f}")
    print()
    print(f"rows out     : {report.rows_out:,}")
    print(f"entities out : {report.entities_out:,}")
    censored = len(report.snapshots) - measurement.snapshots_built
    print(f"snapshots    : {measurement.snapshots_built} built, {censored} censored")
    print(f"features     : {len(report.features)} built, {measurement.features_dropped} dropped for nulls")
    print(f"peak memory  : {peak_mb:,.0f} MB")
    for line in textwrap.wrap(PEAK_MEMORY_BASIS, width=62):
        print(f"               {line}")
    print()
    print(f"target       : {target}")
    print(f"verdict      : {verdict}")
    print(rule)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Generate raw client tables, then measure one whole onboarding build on them.",
    )
    parser.add_argument(
        "--customers",
        type=int,
        default=DEFAULT_CUSTOMERS,
        help=f"customers to generate (default: {DEFAULT_CUSTOMERS}; plan §11 target: {TARGET_CUSTOMERS})",
    )
    parser.add_argument(
        "--usage-rows",
        type=int,
        default=DEFAULT_USAGE_ROWS,
        help=f"usage rows to generate (default: {DEFAULT_USAGE_ROWS}; plan §11 target: {TARGET_USAGE_ROWS})",
    )
    parser.add_argument(
        "--snapshots",
        type=int,
        default=TARGET_SNAPSHOTS,
        help=f"monthly snapshots to build (default: {TARGET_SNAPSHOTS})",
    )
    parser.add_argument(
        "--features",
        type=int,
        default=TARGET_FEATURES,
        help=f"features to build, taken from the top of the suggestion (default: {TARGET_FEATURES})",
    )
    parser.add_argument("--seed", type=int, default=None, help="generator seed (default: make_raw's own)")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where the benchmark's workspace is created (default: the system temp directory)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the generated tables and the built dataset afterwards"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate, map, build, report; returns the process exit code.

    The workspace is a fresh directory *inside* `--out-dir` rather than `--out-dir` itself, so the
    cleanup at the end can never remove something the caller already had there.
    """
    args = _parser().parse_args(list(argv) if argv is not None else None)
    customers: int = args.customers
    usage_rows: int = args.usage_rows
    seed: int = DEFAULT_SEED if args.seed is None else args.seed
    if customers < 1 or usage_rows < customers:
        print("--customers must be at least 1 and --usage-rows at least --customers", file=sys.stderr)
        return 2

    # plan §13 rule 7 has every stage log its rows and its seconds at INFO, and the build does; a
    # benchmark with no handler attached would throw that breakdown away while the build is running,
    # leaving a long silence and no way to see which stage is spending the time.
    configure_logging("INFO")

    out_dir: Path = args.out_dir if args.out_dir is not None else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f"bench-onboarding-{customers}-", dir=out_dir))
    storage = LocalStorage(workspace / "data")
    config = load_use_case(USE_CASE)
    roles = get_roles()
    try:
        print(f"workspace    : {workspace}")
        print(f"generating   : {customers:,} customers · {usage_rows:,} usage rows …", flush=True)
        setup_seconds = prepare_sources(storage, customers=customers, usage_rows=usage_rows, seed=seed)
        sources, mappings = read_specs(storage)
        table_bytes = sum(storage.size_bytes(source.storage_key) for source in sources)
        spec = onboarding_spec(
            config,
            sources,
            mappings,
            entity_role=roles.entity_role,
            snapshots=args.snapshots,
            features=args.features,
        )
        print(
            f"building     : {len(spec.feature_spec.features)} features · "
            f"{spec.snapshot_spec.max_snapshots} snapshots …",
            flush=True,
        )
        measurement = measure_build(storage, config, spec=spec, sources=sources, mappings=mappings)
        print(f"measured     : {measurement.seconds:,.1f} s · {measurement.report.rows_out:,} rows out")
        print()
        print_report(
            measurement=measurement,
            setup_seconds=setup_seconds,
            table_bytes=table_bytes,
            seed=seed,
            peak_mb=peak_memory_mb(),
        )
    finally:
        if args.keep:
            print(f"kept         : {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)
    return 0 if measurement.report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
