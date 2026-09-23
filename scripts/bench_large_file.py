"""Measure the ingest and score paths on a large synthetic file, and print what was measured.

Why this script exists
----------------------
plan §11 makes "large-file handling (streaming, 1M rows in under the time limit on a laptop)"
part of milestone M7's definition of done, and plan §13.3 forbids inventing a number that reaches
a reader. `engine/stages/ingest.py` really does stream - `read_upload` pulls a CSV through
`pandas.read_csv(chunksize=...)` and a Parquet file through `ParquetFile.iter_batches`, folding
every row into the fingerprint as it goes - but "it streams" is an argument about the code, not a
result. Nothing had ever timed it. Until this script is *run*, any row count and any number of
seconds in `README.md` would be a claim; this is the thing that turns them into a measurement, on
a named machine, with the command to reproduce it.

What is measured, and what is only setup
----------------------------------------
Two numbers are the result *for every format measured*, each taken with `time.perf_counter`
around the real engine call:

* **ingest** - `ingest.read_upload` followed by `ingest.profile_dataset`: exactly what the
  pipeline's ingest stage does (`_ScoreFlow._ingest`), including the content fingerprint over
  every row of the file.
* **score** - `Pipeline.run_score` over the same file with the champion: the whole of plan §6.2,
  so ingest again, the schema check, the replay of the training run's transforms, the model, a
  SHAP reason for every row, the bands and actions, and `scores.csv` on disk.

Everything else is **setup and is reported as setup**: generating the file, and training a
champion to score it with. The champion is deliberately trained on a few thousand rows with
`time_limit_minutes: 1` - the question this benchmark answers is what the ingest and score paths
cost at scale, not what AutoGluon costs, and a champion fitted on a sample scores a large file
exactly as a champion fitted on a large one would.

Which format is measured
------------------------
plan §4.1 accepts "CSV (UTF-8, comma separated, header row) or Parquet" and `read_upload` streams
both, so a benchmark that only ever handed it a CSV left half of the accepted input untimed and
`README.md` had to say so. `--format` picks the half: `csv` (the default, so an invocation written
before this option measures exactly what it measured then), `parquet`, or `both`.

`both` is the comparable measurement, and the only one. Two formats timed in one process, on one
machine, over rows generated once and written twice differ by the read path and by nothing else -
not by a busier machine, not by a different day, not by a different draw of rows. The ingests are
timed back to back, before either scoring run, for the same reason: an ingest measured after a
score flow would run in a process carrying that flow's heap, and whichever format went second
would look slower for a reason that has nothing to do with its format. Timing every ingest first
also means the number this script exists to produce is already printed if a long scoring run is
killed at a time limit (plan §13.3: report what was measured).

Two things `both` does not equalise, and neither is a flaw in the measurement. Parquet carries its
own types, so its frame arrives typed while the CSV's is inferred from text - an empty CSV field is
a null, an empty Parquet string stays an empty string - which is a real property of the two formats
and part of what the ingest seconds buy. And `peak memory` is one high-water mark for one process,
so under `both` it belongs to the session - the larger of the two paths - and not to either format
alone; a per-format peak needs a run per format.

Why the file is generated in a separate process
-----------------------------------------------
Peak memory is read from `resource.getrusage(RUSAGE_SELF).ru_maxrss`, which is a *high-water
mark*: it only ever rises, and it cannot be reset. Building a million-row frame in pandas and
rendering it to CSV or to Parquet is itself expensive, so doing it in this process would raise the
mark before the first measured call and the reported peak would be the generator's, not the
engine's. The generator therefore runs in a spawned child process, whose resident set never enters
this process's mark. One child writes every requested format from one generated frame, so
`--format both` compares two files holding the same rows and pays for building them once. Training
stays here on purpose: AutoGluon's imports and its fitted predictor are resident during scoring
too, so they belong inside the number.

`tracemalloc` was the other candidate and is the wrong instrument here: it counts only blocks
allocated through Python's allocator, which misses the pandas, NumPy, Arrow and LightGBM buffers
that hold nearly all of a large frame.

Run it with::

    .venv/bin/python -m scripts.bench_large_file --rows 1000000
    .venv/bin/python -m scripts.bench_large_file --rows 1000000 --format both
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import platform
import resource
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import TYPE_CHECKING, Final, Literal

from tests.fixtures.make_data import GenerationSpec, generate

from engine.config import RunMode, resolve_config
from engine.contracts import ModelStatus, RunState
from engine.jobs import CancelToken, ThreadJobRunner
from engine.pipeline import Pipeline, StageContext
from engine.registry import LocalModelRegistry
from engine.stages import ingest
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.ids import new_run_id
from engine.utils.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engine.config import ResolvedConfig
    from engine.contracts import ModelVersion

COMMAND: Final[str] = "python -m scripts.bench_large_file"

USE_CASE: Final[str] = "targeted-advertisement"
"""The use case the benchmark runs on; `tests/integration/test_score_flow.py` drives the same one."""

PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "converted_30d"

FileFormat = Literal["csv", "parquet"]
"""The two formats plan §4.1 accepts, spelled as `ingest.ReadResult.file_format` spells them."""

FORMAT_CHOICES: Final[dict[str, tuple[FileFormat, ...]]] = {
    "csv": ("csv",),
    "parquet": ("parquet",),
    "both": ("csv", "parquet"),
}
"""What each `--format` value measures, in the order it measures it."""

DEFAULT_FORMAT: Final[str] = "csv"
"""CSV alone, so an invocation written before `--format` existed measures what it always measured."""

FORMAT_LABELS: Final[dict[FileFormat, str]] = {"csv": "CSV", "parquet": "Parquet"}
"""How each format is spelled in the report's prose, where a bare `csv` would read as a suffix."""

DEFAULT_ROWS: Final[int] = 1_000_000
"""plan §11's M7 number: the size the definition of done names."""

TRAIN_ROWS: Final[int] = 4_000
"""Rows the champion is fitted on. Setup, not the measurement - see the module docstring."""

SCORE_SEED: Final[int] = 20270405
TRAIN_SEED: Final[int] = 20260921

TRAIN_OVERRIDES: Final[dict[str, object]] = {
    # plan §10's settings, plus `ensemble: false`: the champion only has to be a real fitted model
    # that reloads and scores, and every second spent here is a second not spent measuring.
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.ensemble": False,
    "model_search.folds": 3,
}

KIB_PER_MB: Final[float] = 1024.0
"""`ru_maxrss` is reported in kibibytes on Linux; this converts it to mebibytes."""

PEAK_MEMORY_BASIS: Final[str] = (
    "resource.getrusage(RUSAGE_SELF).ru_maxrss - the high-water mark of this process's resident "
    "set size, so it covers every allocation pandas, Arrow, AutoGluon and SHAP made, not only "
    "Python-allocator blocks. File generation ran in a separate process and is excluded."
)

PEAK_MEMORY_SHARED: Final[str] = (
    "Both formats were measured in this one process, so this mark is the session's - the larger "
    "of the two paths - and belongs to neither format on its own; a per-format peak needs a run "
    "per format."
)
"""Printed under `--format both`, because one high-water mark cannot be split between two paths."""


# ---------------------------------------------------------------------------
# Generation (a separate process; see the module docstring)
# ---------------------------------------------------------------------------
def write_dataset(rows: int, seed: int, destinations: Sequence[Path]) -> None:
    """Write a `rows`-row scoring file at each of `destinations`, in the format its suffix names.

    The `scoring` variant carries the template's columns **without** the target, which is what a
    real scoring upload looks like and what the score flow has to accept; a file with the answers
    in it would not exercise the same path.

    One `generate` call feeds every destination: under `--format both` the CSV and the Parquet file
    have to hold the *same* rows for the two ingest numbers to be comparable. Which format each
    destination gets is decided by `ingest.file_format_for`, the engine's own rule for what it will
    read that file back as, rather than by a second spelling of the suffixes here.

    This is a module-level function rather than a closure because the spawned child process has to
    import it by name.
    """
    frame = generate(GenerationSpec(USE_CASE, rows=rows, variant="scoring", seed=seed))
    for destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if ingest.file_format_for(destination.name) == "parquet":
            # pyarrow's defaults on purpose - snappy, its own row-group size: what is being timed
            # is the file a user's `to_parquet` hands the engine, not one tuned to be read fast.
            frame.to_parquet(destination, engine="pyarrow", index=False)
        else:
            frame.to_csv(destination, index=False, lineterminator="\n", encoding="utf-8")


def generate_dataset(rows: int, seed: int, destinations: Sequence[Path]) -> float:
    """Generate every requested file in one spawned child; returns the wall-clock seconds it took.

    The child's resident set is not this process's, so the peak memory the benchmark reports stays
    a statement about the engine. A child that dies takes the benchmark with it: a truncated or
    missing file would silently turn the measurement into a measurement of something else, and
    under `--format both` that is true of either file, so every destination is checked.
    """
    started = perf_counter()
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=write_dataset, args=(rows, seed, tuple(destinations)))
    child.start()
    child.join()
    if child.exitcode != 0:
        raise RuntimeError(f"the data generator exited with code {child.exitcode}; no file was measured")
    missing = [str(path) for path in destinations if not path.is_file()]
    if missing:
        raise RuntimeError(f"the data generator wrote no file at {', '.join(missing)}")
    return perf_counter() - started


# ---------------------------------------------------------------------------
# Setup: a real champion, trained small
# ---------------------------------------------------------------------------
def train_champion(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    *,
    rows: int,
    jobs: ThreadJobRunner,
) -> tuple[ModelVersion, float]:
    """Run the whole train flow on a small sample and approve the result into the champion.

    `approval_required` is on by default, so the first model of a use case lands as
    `pending_approval` and the score flow would refuse to find a champion; approving it here is
    the same step a human takes in the UI.
    """
    started = perf_counter()
    frame = generate(GenerationSpec(USE_CASE, rows=rows, variant="clean", seed=TRAIN_SEED))
    key = upload_key("u_bench_train", "source.csv")
    storage.write_text(key, frame.to_csv(index=False, lineterminator="\n"))
    run_id = new_run_id()
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    record = Pipeline(storage, registry, jobs).run_train(
        StageContext(
            run_id=run_id,
            mode=RunMode.TRAIN,
            config=resolved.config,
            resolved=resolved,
            storage=storage,
            registry=registry,
            cancel=CancelToken(),
            primary_key=[PRIMARY_KEY],
            target=TARGET,
            upload_key=key,
            model_version_id=None,
        )
    )
    if record.state is not RunState.DONE:
        raise RuntimeError(f"the setup training run did not finish: {record.error}")
    version = registry.get(record.model_version_id or "")
    if version.status is ModelStatus.PENDING_APPROVAL:
        version = registry.approve(version.model_id, by=COMMAND)
    return version, perf_counter() - started


# ---------------------------------------------------------------------------
# The two measurements
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Measurement:
    """One timed stage: which stage, over which format, what it cost, and for how many rows."""

    stage: str
    file_format: FileFormat
    seconds: float
    rows: int

    @property
    def name(self) -> str:
        """`ingest csv`, `score parquet`: with two formats in one run the stage alone is ambiguous."""
        return f"{self.stage} {self.file_format}"

    @property
    def rows_per_second(self) -> float:
        """Rows divided by seconds; zero when the stage was too fast to have divided by."""
        return self.rows / self.seconds if self.seconds > 0 else 0.0


def measurement_line(item: Measurement) -> str:
    """One measurement as a single line, for the progress log and for the table's row."""
    return f"{item.name} {item.seconds:,.1f} s · {item.rows_per_second:,.0f} rows/sec · {item.rows:,} rows"


def measure_ingest(
    storage: LocalStorage, key: str, resolved: ResolvedConfig, *, file_format: FileFormat
) -> Measurement:
    """Time `read_upload` + `profile_dataset` - the pipeline's ingest stage, call for call.

    This is `_ScoreFlow._ingest` without the artefact write: the same `profile_row_cap`, the same
    single streaming pass, and the same whole-file fingerprint, so the seconds are the seconds the
    stage really spends. The profile is returned to nothing and dropped, so the frame it was built
    from is released before the score flow reads the file again.

    Nothing here branches on the format. `read_upload` chooses the CSV reader or the Parquet reader
    from the key's suffix exactly as `POST /uploads` does, and `read.file_format` goes straight into
    the profile, so the Parquet path is timed through the same two calls and not a variant of them;
    `file_format` is carried only to label the result.
    """
    config = resolved.config
    started = perf_counter()
    read = ingest.read_upload(storage, key, row_cap=ingest.profile_row_cap(config))
    profile = ingest.profile_dataset(
        read.frame,
        config,
        upload_id="u_bench_score",
        file_name=PurePosixPath(key).name,
        file_format=read.file_format,
        file_size_bytes=storage.size_bytes(key),
        delimiter=read.delimiter,
        encoding=read.encoding,
        row_count=read.row_count,
        fingerprint=read.fingerprint,
    )
    seconds = perf_counter() - started
    return Measurement("ingest", file_format, seconds, profile.row_count)


def measure_score(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    *,
    key: str,
    file_format: FileFormat,
    jobs: ThreadJobRunner,
) -> Measurement:
    """Time `Pipeline.run_score` over the same file: the whole of plan §6.2, nothing stubbed.

    `model_version_id=None` on purpose - the flow resolves the champion the way `POST /runs` does,
    so the benchmark measures the path a user's scoring run takes rather than a shortcut into it.
    The champion is the one fitted on the CSV sample whichever format is being scored: a model is
    fitted on the rows a file held, not on the container they arrived in, and re-fitting per format
    would put AutoGluon's variance inside a comparison that is meant to be about reading.
    """
    run_id = new_run_id()
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    context = StageContext(
        run_id=run_id,
        mode=RunMode.SCORE,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=[PRIMARY_KEY],
        target=None,
        upload_key=key,
        model_version_id=None,
    )
    started = perf_counter()
    record = Pipeline(storage, registry, jobs).run_score(context)
    seconds = perf_counter() - started
    if record.state is not RunState.DONE:
        raise RuntimeError(f"the scoring run did not finish: {record.error}")
    return Measurement("score", file_format, seconds, record.row_count or 0)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def peak_memory_mb() -> float:
    """This process's peak resident set size in MiB. See `PEAK_MEMORY_BASIS` for what that covers.

    `ru_maxrss` is in KiB on Linux and in **bytes** on macOS (Plan D M57): the laptop plan §11 names
    is likely a Mac, and reading its bytes as KiB would report a peak 1,024 times too large.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (KIB_PER_MB * 1024) if sys.platform == "darwin" else peak / KIB_PER_MB


def machine_line() -> str:
    """The machine the numbers belong to. A measurement without one is not reproducible."""
    total_kib = 0
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total_kib = int(line.split()[1])
                break
    memory = f"{total_kib / (1024 * 1024):.1f} GiB RAM" if total_kib else "unknown RAM"
    # The CPUs this process may actually run on, not the ones the box has: a container pinned to
    # two cores of a sixteen-core host would otherwise have its numbers read against the wrong
    # machine. `cpu_count` is the fallback where affinity is not exposed.
    affinity = getattr(os, "sched_getaffinity", None)
    cpus = len(affinity(0)) if affinity is not None else (os.cpu_count() or 0)
    return f"{cpus} CPUs · {memory} · Python {platform.python_version()} · {platform.system()}"


def print_report(
    *,
    rows: int,
    files: Mapping[FileFormat, int],
    generate_seconds: float,
    train_seconds: float,
    train_rows: int,
    measurements: Sequence[Measurement],
    peak_mb: float,
) -> None:
    """Print the summary table. Every number here was measured by this run; none is projected."""
    rule = "=" * 78
    print(rule)
    print("Large-file ingest + score benchmark (plan §11, milestone M7)")
    print(rule)
    print(f"machine      : {machine_line()}")
    print(f"rows         : {rows:,}")
    sizes = ", ".join(f"{size / 1_000_000:.1f} MB {FORMAT_LABELS[fmt]}" for fmt, size in files.items())
    print(f"file         : {sizes} · {USE_CASE} · scoring variant")
    print()
    print("setup (NOT part of the measured result)")
    print(f"  generate   : {generate_seconds:8.1f} s   (separate process; excluded from peak memory)")
    print(f"  train      : {train_seconds:8.1f} s   ({train_rows:,} rows, {_overrides_line()})")
    print()
    print("measured")
    print(f"  {'stage':<16}{'seconds':>12}{'rows/sec':>14}{'rows':>14}")
    for item in measurements:
        print(f"  {item.name:<16}{item.seconds:>12.1f}{item.rows_per_second:>14,.0f}{item.rows:>14,}")
    comparison = comparison_lines(measurements, files)
    if comparison:
        print()
        print("parquet against csv (same rows, same process, same machine)")
        for line in comparison:
            print(line)
    print()
    print(f"peak memory  : {peak_mb:,.0f} MB")
    for line in _wrap(PEAK_MEMORY_BASIS, width=62):
        print(f"               {line}")
    if len({item.file_format for item in measurements}) > 1:
        for line in _wrap(PEAK_MEMORY_SHARED, width=62):
            print(f"               {line}")
    print(rule)


def comparison_lines(measurements: Sequence[Measurement], files: Mapping[FileFormat, int]) -> list[str]:
    """Parquet as a multiple of CSV, per stage; empty unless this run measured both formats.

    A ratio, not a "faster" verdict: the two numbers beside it say which way it points, and a ratio
    stays true whichever format wins on the machine the run happened on. Only stages this run timed
    in *both* formats appear, so a half-finished run compares nothing rather than something wrong.
    """
    seconds: dict[str, dict[FileFormat, float]] = {}
    for item in measurements:
        seconds.setdefault(item.stage, {})[item.file_format] = item.seconds
    lines: list[str] = []
    for stage, by_format in seconds.items():
        csv_seconds = by_format.get("csv")
        parquet_seconds = by_format.get("parquet")
        if csv_seconds is None or parquet_seconds is None or csv_seconds <= 0:
            continue
        lines.append(
            _ratio_line(
                stage,
                ratio=parquet_seconds / csv_seconds,
                unit="the seconds",
                detail=f"{parquet_seconds:,.1f} s vs {csv_seconds:,.1f} s",
            )
        )
    csv_bytes = files.get("csv")
    parquet_bytes = files.get("parquet")
    if csv_bytes and parquet_bytes:
        lines.append(
            _ratio_line(
                "file",
                ratio=parquet_bytes / csv_bytes,
                unit="the bytes",
                detail=f"{parquet_bytes / 1_000_000:,.1f} MB vs {csv_bytes / 1_000_000:,.1f} MB",
            )
        )
    return lines


def _ratio_line(label: str, *, ratio: float, unit: str, detail: str) -> str:
    """One comparison row, aligned with the measured table above it."""
    return f"  {label:<16}{ratio:>7.2f}× {unit:<14}({detail})"


def _overrides_line() -> str:
    """The training settings, spelled the way the configuration spells them."""
    return "time_limit_minutes=1, strategy=fast, ensemble=off"


def _wrap(text: str, *, width: int) -> list[str]:
    """`text` broken on spaces at `width`; a local wrap keeps the report's shape in one place."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=(
            "Generate a large synthetic file, then measure the ingest and score paths on it, in "
            "CSV, in Parquet, or in both formats side by side."
        ),
    )
    parser.add_argument(
        "--rows", type=int, default=DEFAULT_ROWS, help=f"rows to generate and score (default: {DEFAULT_ROWS})"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where the benchmark's workspace is created (default: the system temp directory)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the generated file and the run artefacts afterwards"
    )
    parser.add_argument(
        "--format",
        choices=tuple(FORMAT_CHOICES),
        default=DEFAULT_FORMAT,
        help=(
            "which accepted input format to measure: csv, parquet, or both side by side in one "
            f"session (default: {DEFAULT_FORMAT})"
        ),
    )
    parser.add_argument(
        "--train-rows",
        type=int,
        default=TRAIN_ROWS,
        help=f"rows the setup champion is fitted on (default: {TRAIN_ROWS})",
    )
    parser.add_argument("--configs", type=Path, default=None, help="configuration root (default: configs/)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate, ingest, score, report; returns the process exit code.

    The workspace is a fresh directory *inside* `--out-dir` rather than `--out-dir` itself, so the
    cleanup at the end can never remove something the caller already had there.
    """
    args = _parser().parse_args(list(argv) if argv is not None else None)
    rows: int = args.rows
    train_rows: int = args.train_rows
    formats: tuple[FileFormat, ...] = FORMAT_CHOICES[args.format]
    if rows < 1:
        print("--rows must be at least 1", file=sys.stderr)
        return 2

    # plan §13 rule 7 has every stage log its rows and its seconds at INFO, and the pipeline does;
    # a benchmark with no handler attached would throw exactly that breakdown away, leaving two
    # totals and no way to see which stage of plan §6.2 spent them.
    configure_logging("INFO")

    out_dir: Path = args.out_dir if args.out_dir is not None else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f"bench-large-file-{rows}-", dir=out_dir))
    storage = LocalStorage(workspace / "data")
    registry = LocalModelRegistry(workspace / "registry.db")
    jobs = ThreadJobRunner(max_workers=1)

    config_root: Path | None = args.configs
    train_config = resolve_config(USE_CASE, TRAIN_OVERRIDES, root=config_root)
    # The measured half runs on the shipped defaults: the overrides above are model_search only,
    # and a scoring run must be timed as a user's scoring run, not as a tuned one.
    score_config = resolve_config(USE_CASE, root=config_root)

    keys: dict[FileFormat, str] = {fmt: upload_key("u_bench_score", f"source.{fmt}") for fmt in formats}
    sources: dict[FileFormat, Path] = {fmt: storage.local_path(key) for fmt, key in keys.items()}
    try:
        print(f"workspace    : {workspace}")
        print(f"generating   : {rows:,} rows · {', '.join(formats)} …", flush=True)
        generate_seconds = generate_dataset(rows, SCORE_SEED, tuple(sources.values()))
        files: dict[FileFormat, int] = {fmt: path.stat().st_size for fmt, path in sources.items()}
        print(f"training     : champion on {train_rows:,} rows …", flush=True)
        _, train_seconds = train_champion(storage, registry, train_config, rows=train_rows, jobs=jobs)
        # Each measurement is printed the moment it is taken, not only in the table below, and
        # every ingest is taken before any scoring run. A run at a size this machine cannot finish
        # is stopped in the middle of a score stage - by a `timeout`, by an operator, by the OOM
        # killer - and the ingest numbers it *did* measure are a real result that must not die with
        # it (plan §13.3: report what was measured).
        measurements: list[Measurement] = []
        for file_format in formats:
            print(f"measuring    : ingest {file_format} …", flush=True)
            measured = measure_ingest(storage, keys[file_format], score_config, file_format=file_format)
            measurements.append(measured)
            print(f"measured     : {measurement_line(measured)}", flush=True)
        for file_format in formats:
            print(f"measuring    : score {file_format} …", flush=True)
            measured = measure_score(
                storage,
                registry,
                score_config,
                key=keys[file_format],
                file_format=file_format,
                jobs=jobs,
            )
            measurements.append(measured)
            print(f"measured     : {measurement_line(measured)}", flush=True)
        print()
        print_report(
            rows=rows,
            files=files,
            generate_seconds=generate_seconds,
            train_seconds=train_seconds,
            train_rows=train_rows,
            measurements=tuple(measurements),
            peak_mb=peak_memory_mb(),
        )
    finally:
        jobs.shutdown(wait=False)
        if args.keep:
            print(f"kept         : {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
