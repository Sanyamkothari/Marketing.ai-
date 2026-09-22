"""A finished predictive run, fabricated: every artefact a generative flow reads, and no model.

The Phase 3a flows all begin the same way - they open a run that has already finished and read
what it left behind. Producing such a run for real means training with AutoGluon, which is
``@pytest.mark.slow`` and takes minutes, so a generative test that wanted one honestly would either
leave ``make test`` or leave the fast suite. This module is the third answer: it writes a run
directory that is valid against the real contracts, in milliseconds, with no model anywhere.

Design notes
------------
*Config driven.* Nothing here branches on a use-case id. The columns come from
``template.columns``, the score column from ``actions.score_field``, the bands and their actions
from ``actions.bands``, the suppression rules from ``actions.suppression`` and
``governance.consent_column``, and the number of reasons from ``evaluation.reasons_per_row`` -
exactly as :mod:`tests.fixtures.make_data` reads them, and for the same reason: a new use case must
need no change here.

*The engine's own stages do the parts whose meaning matters.* Banding, suppression precedence and
the control-group draw are :func:`engine.stages.actions.apply_actions`; the reasons are
:func:`engine.stages.explain.build_row_explanations`; the score files and the summary are
:mod:`engine.stages.export`; drift is measured by :func:`engine.stages.score.compute_drift` against
a baseline :func:`engine.stages.register.drift_baseline` built from a second draw. A second
implementation of any of those would drift away from the one under test, and a generative flow that
quietly ignored a suppressed row would then still pass.

*Deterministic.* Every draw comes from a :class:`numpy.random.Generator` seeded by a BLAKE2b digest
of ``(seed, use_case_id, purpose, column)``, as ``make_data`` does. The run id and the upload id are
digests of the whole spec, and no clock is read anywhere: a timestamp is either the one the spec
carries or the day after the newest date in the rows that spec generated. So the same spec writes
byte-identical files, which is what lets a test pin a value rather than a shape.

*A reason is a reason.* Each feature gets one seeded weight and each row a standardised signal from
its own cell, and the row's score is those contributions summed and squashed. So a reason really
does explain the score it sits next to, a row above the column's mean contributes in the opposite
direction to a row below it - which is why both directions always occur - and the root-cause
evidence pack aggregating them is aggregating something real.

*Nothing fabricated is dressed as a measurement.* The run never ran, so its duration is ``0.0`` and
:class:`engine.contracts.CostEstimate` says in words that nothing was executed rather than
estimating a cost nobody paid; every stage is ``done`` with no start, end or duration, because the
contract makes those optional precisely so an unmeasured time can be absent; ``headline_score`` is
null; ``metrics`` carries only counts this module actually counted; and ``model_display_name`` is
deliberately not a name AutoGluon can produce.

*What it deliberately does not write.* The evaluation family of a training run - ``split.json``,
``leaderboard.json``, ``best_model.json``, ``evaluation.json``, ``confusion_matrix.json``,
``decile_lift.json``, ``baseline.json``, ``fairness.json``, ``schema.json``,
``drift_baseline.json`` and the predictor directory - is absent in both modes, because every one of
them states how good a model is and this module fits none. An invented ROC-AUC is the one kind of
number the project refuses to write down (plan §13.3), and a test that needs a real one belongs in
the slow suite. ``feature_importance.json`` and ``row_explanations.parquet`` are written in both
modes even though the score flow writes only the second, because a generative flow reads them
together and pointing one at two runs to assemble a single evidence pack would be testing the test.
No model version is registered either: a flow that needs a registry row seeds one the way
``tests/integration/test_api_runs.py::seed_model`` does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np
import numpy.typing as npt
import pandas as pd

import engine
from engine.config import ColumnRole, ColumnType, RunMode, load_use_case, resolve_config
from engine.contracts import (
    CostEstimate,
    DatasetProfile,
    DriftReport,
    FeatureImportance,
    PrepareReport,
    RowExplanation,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    StageStatus,
    ValidationReport,
    dump_artefact,
    load_artefact,
    scores_csv_columns,
)
from engine.pipeline import GROUP_LABELS, STAGE_TITLES, stages_for
from engine.stages import actions as actions_stage
from engine.stages import explain, export, ingest, register, score
from engine.storage import run_key, upload_key
from engine.utils.ids import new_model_id, seed_from
from engine.utils.text import humanise_count
from tests.fixtures.make_data import CLEAN, SCORING, GenerationSpec, generate
from tests.fixtures.make_docs import COMPLAINT_TEXT_COLUMN, generate_complaints

if TYPE_CHECKING:
    from pydantic import BaseModel

    from engine.config import TemplateColumn, UseCaseConfig
    from engine.storage import Storage

__all__ = [
    "COST_BASIS",
    "DEFAULT_POSITIVE_RATE",
    "DEFAULT_ROWS",
    "DEFAULT_SEED",
    "FALLBACK_CREATED_AT",
    "MODEL_DISPLAY_NAME",
    "SCORE_LOGIT_SD",
    "SOURCE_FILENAME",
    "RunSpec",
    "complaint_column",
    "feature_columns",
    "primary_key_of",
    "source_key",
    "target_of",
    "write_run",
]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_ROWS: Final[int] = 1_000
"""Enough rows that every configured band, the suppression rules and the holdout all have some."""

DEFAULT_SEED: Final[int] = 20_260_923
DEFAULT_POSITIVE_RATE: Final[float] = 0.12

SCORE_LOGIT_SD: Final[float] = 2.5
"""Spread of the score in logit units, before the intercept moves its mean onto `positive_rate`.

Wide on purpose, and fixed rather than left to follow the number of features: a run whose scores
all landed in one band would let a flow that ignores banding altogether pass its tests.
"""

FALLBACK_CREATED_AT: Final[datetime] = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)
"""When a run is dated if its rows carry no date at all (the public Telco file carries none)."""

SOURCE_FILENAME: Final[str] = "source.csv"
"""The stored name of an upload, spelled the way `api.routes.uploads.source_filename` spells it."""

MODEL_DISPLAY_NAME: Final[str] = "Synthetic ensemble"
"""Deliberately not a name AutoGluon can produce.

A fixture that wrote `WeightedEnsemble_L2` would be claiming a model ran, and a reader of a failing
test would go looking for the predictor that produced it. Nothing was fitted here.
"""

COST_BASIS: Final[str] = (
    "Nothing was executed: this run was fabricated by tests.fixtures.make_run, "
    "so no compute was consumed and nothing was billed."
)
"""Why both `compute_seconds` and `duration_s` are zero and `estimated_usd` is null.

Zero is the measurement here rather than a fabrication - no stage ran, so no stage took time -
and the basis says so in words, because a bare zero next to a null cost is ambiguous (plan §13.3).
"""

_CONTRIBUTION_DECIMALS: Final[int] = 6
"""Matches `engine.stages.explain`, so a reason written here rounds as a real one does."""

_NUMERIC_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.INTEGER, ColumnType.FLOAT})
_DATE_TYPES: Final[frozenset[ColumnType]] = frozenset({ColumnType.DATE, ColumnType.DATETIME})

_INTERCEPT_STEPS: Final[int] = 200
"""Bisection steps for the intercept; the same budget `make_data` uses to hit its positive rate."""


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunSpec:
    """What to fabricate. Two specs that compare equal write byte-identical run directories.

    `positive_rate` means the same thing in both modes - how often the outcome happens - because a
    calibrated model's mean score *is* the base rate: it is the target rate of a training file and
    the mean score of a scoring run.

    `with_reasons` switches per-row reasons off the way `evaluation.shap: false` does: no
    `row_explanations.parquet`, empty reason cells in `scores.csv`, and a feature-importance
    artefact that says it has nothing rather than showing an invented ranking.

    `with_text` fills the use case's `generative.root_cause.complaint_text_column` with the longer
    complaint prose of :mod:`tests.fixtures.make_docs`, which plants PII inside sentences; the
    template's own five example phrases are too short to redact anything out of.

    `created_at` is the run's timestamp, and it is also the reference time the recency suppression
    rule is measured against, exactly as a real run's start time is. Left unset it becomes the
    morning after the newest date the generated rows carry, so a run never claims to have happened
    before the data it read and the recency rule has rows it can find.
    """

    use_case_id: str
    mode: RunMode = RunMode.SCORE
    rows: int = DEFAULT_ROWS
    seed: int = DEFAULT_SEED
    positive_rate: float = DEFAULT_POSITIVE_RATE
    with_reasons: bool = True
    with_text: bool = False
    created_at: datetime | None = None
    config_root: Path | None = None

    def __post_init__(self) -> None:
        if self.rows < 1:
            raise ValueError(f"rows must be >= 1, got {self.rows}")
        if not 0.0 < self.positive_rate < 1.0:
            raise ValueError(f"positive_rate must be in (0, 1), got {self.positive_rate}")
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware; every engine timestamp is UTC")

    @property
    def data_spec(self) -> GenerationSpec:
        """The `make_data` request behind the uploaded rows: a training file carries the target."""
        return GenerationSpec(
            use_case_id=self.use_case_id,
            rows=self.rows,
            seed=self.seed,
            variant=CLEAN if self.mode is RunMode.TRAIN else SCORING,
            positive_rate=self.positive_rate,
            config_root=self.config_root,
        )

    @property
    def file_name(self) -> str:
        """The name the run remembers the upload by; `make_data`'s, so it still says `synthetic`."""
        return self.data_spec.file_name


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def _seed_for(*parts: object) -> int:
    """A stable 64-bit seed from the given parts. Independent of PYTHONHASHSEED and of dict order."""
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _rng(*parts: object) -> np.random.Generator:
    return np.random.default_rng(_seed_for(*parts))


def _digest(*parts: object, length: int) -> str:
    """The hex suffix of a fabricated id: short, stable and derived from the whole spec."""
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=length // 2).hexdigest()


def _spec_parts(spec: RunSpec, created_at: datetime) -> tuple[object, ...]:
    """Everything that makes two specs different, so two of them cannot share an id."""
    return (
        spec.use_case_id,
        spec.mode.value,
        spec.rows,
        spec.seed,
        spec.positive_rate,
        spec.with_reasons,
        spec.with_text,
        created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Reading the configuration
# ---------------------------------------------------------------------------
def primary_key_of(config: UseCaseConfig) -> str:
    """The template's primary-key column, which is the column the run is keyed on."""
    key = config.template.primary_key
    if key is None:  # pragma: no cover - every predictive template declares one
        raise ValueError(f"{config.id} has no primary_key column in its template")
    return key.name


def target_of(config: UseCaseConfig) -> str | None:
    """The template's target column, or `None` for a template that declares none."""
    targets = config.template.by_role(ColumnRole.TARGET)
    return targets[0].name if targets else None


def _feature_template_columns(config: UseCaseConfig) -> tuple[TemplateColumn, ...]:
    """The template's feature columns minus the free-text ones, in template order.

    Plan §5 marks free text "ignored by the Phase 1 model", which is also why it is the column a
    root-cause summary reads *instead* of a reason. A reason about a paragraph of prose would be
    neither.
    """
    return tuple(
        column for column in config.template.by_role(ColumnRole.FEATURE) if column.type is not ColumnType.TEXT
    )


def feature_columns(config: UseCaseConfig) -> tuple[str, ...]:
    """The names of the columns a score is explained by, in template order."""
    return tuple(column.name for column in _feature_template_columns(config))


def complaint_column(config: UseCaseConfig) -> str | None:
    """The free-text column a root-cause summary reads, or `None` when the use case configures none.

    Read from `generative.root_cause.complaint_text_column` and checked against the template, so a
    use case that names a column it does not have is a configuration error rather than a fixture
    that silently writes nothing.
    """
    name = config.generative.root_cause.complaint_text_column
    if name is None:
        return None
    if name not in {column.name for column in config.template.columns}:
        raise ValueError(
            f"{config.id} configures generative.root_cause.complaint_text_column={name!r}, "
            f"which its template does not declare."
        )
    return name


def source_key(record: RunRecord) -> str:
    """Where the rows this run consumed are stored, so a flow can read them back by the record."""
    return upload_key(record.upload_id, SOURCE_FILENAME)


# ---------------------------------------------------------------------------
# The uploaded rows
# ---------------------------------------------------------------------------
def _uploaded_frame(spec: RunSpec, config: UseCaseConfig) -> pd.DataFrame:
    """The rows the run consumed: `make_data`'s frame, with the complaint column filled on request."""
    frame = generate(spec.data_spec)
    if not spec.with_text:
        return frame
    column = complaint_column(config)
    if column is None:
        raise ValueError(
            f"with_text is set but {config.id} configures no "
            f"generative.root_cause.complaint_text_column to put the text in."
        )
    complaints = generate_complaints(
        len(frame.index), seed=spec.seed, key_prefix=_key_prefix(frame[primary_key_of(config)])
    )
    filled = frame.copy()
    # The complaint frame's own keys are discarded and the text is attached by position, one
    # complaint per row. `make_docs` numbers its keys sequentially while a generated primary key is
    # a seeded draw out of a wider range, so position is the only join the two really have - and it
    # is the right one, since `complaint_text_column` means "this row's latest complaint".
    filled[column] = complaints[COMPLAINT_TEXT_COLUMN].to_numpy()
    return filled


def _key_prefix(keys: pd.Series) -> str:
    """The non-numeric head of a primary key, so borrowed keys are shaped like the run's own."""
    first = str(keys.iloc[0]) if len(keys.index) else ""
    return first.rstrip("0123456789")


def _resolved_created_at(spec: RunSpec, config: UseCaseConfig, frame: pd.DataFrame) -> datetime:
    """The run's timestamp: the spec's, or the midnight after the newest date the rows carry."""
    if spec.created_at is not None:
        return spec.created_at
    newest: pd.Timestamp | None = None
    for column in config.template.columns:
        if column.role not in (ColumnRole.TIME, ColumnRole.CONTACT) or column.name not in frame.columns:
            continue
        parsed = pd.to_datetime(frame[column.name], errors="coerce", utc=True, format="mixed")
        if parsed.notna().any():
            latest = parsed.max()
            newest = latest if newest is None else max(newest, latest)
    if newest is None:
        return FALLBACK_CREATED_AT
    return newest.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


# ---------------------------------------------------------------------------
# Scores and reasons
# ---------------------------------------------------------------------------
def _sigmoid(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return 0.5 * (1.0 + np.tanh(0.5 * values))


def _solve_intercept(logits: npt.NDArray[np.float64], mean_score: float) -> float:
    """Bisect for the intercept that makes the mean score equal `mean_score` (as `make_data` does)."""
    low, high = -40.0, 40.0
    for _ in range(_INTERCEPT_STEPS):
        middle = (low + high) / 2.0
        if float(np.mean(_sigmoid(logits + middle))) < mean_score:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _standardise(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Zero mean, unit spread; a constant column becomes zeros and so contributes no reason at all."""
    spread = float(np.std(values))
    if not np.isfinite(spread) or spread == 0.0:
        return np.zeros_like(values)
    return (values - float(np.mean(values))) / spread


def _signal(
    frame: pd.DataFrame, column: TemplateColumn, *, spec: RunSpec, use_case_id: str
) -> npt.NDArray[np.float64]:
    """One column as a standardised number line, according to the type its template declares.

    A number is itself, a date is days since the epoch, and everything else - a category, a boolean
    spelled `true`/`false`, a blank - is mapped onto a seeded number per distinct level. The
    template's declared type decides, never the cells, so a category whose levels happen to be
    digits keeps the meaning the use case gave it. Nulls sit at the column's own mean, the one
    position that says nothing about the row.
    """
    if column.type in _NUMERIC_TYPES:
        numbers = pd.to_numeric(frame[column.name], errors="coerce")
    elif column.type in _DATE_TYPES:
        stamps = pd.to_datetime(frame[column.name], errors="coerce", utc=True, format="mixed")
        numbers = (stamps - pd.Timestamp("1970-01-01", tz=UTC)).dt.days
    else:
        numbers = _level_numbers(frame[column.name], spec=spec, use_case_id=use_case_id, column=column.name)
    values = numbers.astype("float64")
    filled = np.asarray(values.fillna(values.mean()).to_numpy(), dtype=np.float64)
    return _standardise(filled)


def _level_numbers(values: pd.Series, *, spec: RunSpec, use_case_id: str, column: str) -> pd.Series:
    """A seeded number per distinct level, so a category orders the same way in every process."""
    text = values.astype("string").fillna("")
    levels = sorted(set(text.tolist()))
    draws = _rng(spec.seed, use_case_id, "level", column).normal(0.0, 1.0, len(levels))
    lookup = dict(zip(levels, draws.tolist(), strict=True))
    return text.map(lookup).astype("float64")


def _contributions(spec: RunSpec, config: UseCaseConfig, frame: pd.DataFrame) -> pd.DataFrame:
    """One signed contribution per row per feature, summing to the logit behind the row's score.

    Each feature gets one seeded weight, scaled so the whole logit has :data:`SCORE_LOGIT_SD`
    however many features the use case has, and each row its standardised signal for that feature.
    A row above the column's mean therefore contributes the opposite way to a row below it, which
    is what guarantees both directions occur in the file the evidence pack aggregates.
    """
    columns = [column for column in _feature_template_columns(config) if column.name in frame.columns]
    if not columns:  # pragma: no cover - every predictive template declares features
        raise ValueError(f"{config.id} has no feature columns to explain a score with")
    weights = _rng(spec.seed, config.id, "feature_weight").normal(0.0, 1.0, len(columns))
    norm = float(np.sqrt(np.sum(np.square(weights))))
    scaled = weights * (SCORE_LOGIT_SD / norm) if norm > 0.0 else weights
    built = {
        column.name: np.round(
            scaled[position] * _signal(frame, column, spec=spec, use_case_id=config.id),
            _CONTRIBUTION_DECIMALS,
        )
        for position, column in enumerate(columns)
    }
    return pd.DataFrame(built, index=frame.index)


def _scores(spec: RunSpec, contributions: pd.DataFrame) -> npt.NDArray[np.float64]:
    """The score of every row: its contributions summed and squashed onto the configured mean."""
    logits = contributions.to_numpy(dtype=np.float64).sum(axis=1)
    return _sigmoid(logits + _solve_intercept(logits, spec.positive_rate))


def _feature_importance(contributions: pd.DataFrame, *, run_id: str, with_reasons: bool) -> FeatureImportance:
    """The global chart: the mean absolute contribution of each feature, normalised by the engine.

    With reasons switched off there is nothing to average, and the artefact says so through
    `engine.stages.explain.unavailable_importance` rather than showing a ranking of zeros.
    """
    if not with_reasons:
        return explain.unavailable_importance(run_id)
    raw = pd.DataFrame(
        {"importance": contributions.abs().mean(axis=0).to_numpy(dtype=np.float64)},
        index=list(contributions.columns),
    )
    return explain.build_feature_importance(raw, run_id=run_id)


def _explanations(
    frame: pd.DataFrame,
    contributions: pd.DataFrame,
    scores: npt.NDArray[np.float64],
    *,
    config: UseCaseConfig,
    importance: FeatureImportance,
) -> tuple[RowExplanation, ...]:
    """Every row explained, by the engine's own builder, so the parquet is what a real run writes."""
    return explain.build_row_explanations(
        frame,
        contributions,
        scores.tolist(),
        features=list(contributions.columns),
        primary_key=primary_key_of(config),
        limit=config.evaluation.reasons_per_row,
        rank={item.feature: item.rank for item in importance.items},
    )


# ---------------------------------------------------------------------------
# The artefacts
# ---------------------------------------------------------------------------
def _write(
    storage: Storage, artefacts: dict[str, str], *, run_id: str, filename: str, model: BaseModel
) -> str:
    """Serialise, validate through the contract registered for `filename`, then write.

    Validating before the bytes land is the point of this module: a contract that gains a field or
    renames one breaks the fixture here, on the line that writes the file, rather than three
    modules away in a generative test that cannot say what went wrong.
    """
    payload = dump_artefact(model)
    load_artefact(filename, payload)
    key = run_key(run_id, filename)
    storage.write_text(key, payload)
    artefacts[filename] = key
    return key


def _profile(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    spec: RunSpec,
    upload_id: str,
    file_size_bytes: int,
    created_at: datetime,
) -> DatasetProfile:
    """The real ingest profile of the real rows, with the clock reading replaced by the spec's."""
    profile = ingest.profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name=spec.file_name,
        file_format="csv",
        file_size_bytes=file_size_bytes,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame.index),
        fingerprint=ingest.dataset_fingerprint(frame),
    )
    return profile.model_copy(update={"profiled_at": created_at})


def _validation(spec: RunSpec, *, run_id: str, upload_id: str, created_at: datetime) -> ValidationReport:
    """An empty, passing report: no check ran, so none is recorded and none can have failed.

    Running the real checks would be the other honest answer, and a worse one here - the leakage
    check fits a cross-validated baseline, which is exactly the minute this module exists to avoid,
    and a fabricated file large enough to clear `validation.min_rows` for every use case would make
    every generative test slower to prove something no generative flow reads.
    """
    return ValidationReport(
        run_id=run_id,
        upload_id=upload_id,
        mode=spec.mode,
        checks=(),
        error_count=0,
        warning_count=0,
        passed=True,
        validated_at=created_at,
    )


def _prepare_report(
    frame: pd.DataFrame, config: UseCaseConfig, *, run_id: str, created_at: datetime
) -> PrepareReport:
    """What preparation did, which here is nothing: the rows are written as they were generated."""
    features = tuple(name for name in feature_columns(config) if name in frame.columns)
    rows = len(frame.index)
    columns = len(frame.columns)
    return PrepareReport(
        run_id=run_id,
        rows_in=rows,
        rows_out=rows,
        columns_in=columns,
        columns_out=columns,
        feature_columns=features,
        dropped_columns=(),
        row_removals=(),
        transforms=(),
        pii_columns=(),
        consent_column=config.governance.consent_column,
        consent_rows_removed=0,
        detail=(
            f"{humanise_count(rows)} rows · {humanise_count(len(features))} features · "
            f"no transform was fitted"
        ),
        prepared_at=created_at,
    )


def _status(spec: RunSpec, *, run_id: str, created_at: datetime) -> RunStatus:
    """Every stage of the mode, all `done`, none of them timed.

    A stage that never ran has no duration, and `StageStatus` makes the three time fields optional
    for exactly that case. Filling them with a share of an invented total would make a Running
    screen out of a run that never started.
    """
    stages = tuple(
        StageStatus(
            key=key,
            title=STAGE_TITLES[key],
            group_label=GROUP_LABELS[(spec.mode, key)],
            state=RunState.DONE,
        )
        for key in stages_for(spec.mode)
    )
    return RunStatus(
        run_id=run_id,
        mode=spec.mode,
        state=RunState.DONE,
        updated_at=created_at,
        stages=stages,
        current_stage=None,
        progress_pct=100,
    )


def _manifest(
    profile: DatasetProfile,
    *,
    run_id: str,
    primary_key: str,
    metrics: dict[str, float],
    created_at: datetime,
) -> RunManifest:
    """The flat record of the run. `recipe` is null: nothing was fitted, so there is no recipe."""
    return RunManifest(
        run_id=run_id,
        primary_key=primary_key,
        recipe=None,
        dataset_fingerprint=profile.fingerprint,
        seed=seed_from(run_id),
        metrics=metrics,
        leaderboard_path=None,
        duration_s=0.0,
        cost_estimate=CostEstimate(compute_seconds=0.0, estimated_usd=None, basis=COST_BASIS),
        created_at=created_at,
    )


def _record(
    spec: RunSpec,
    config: UseCaseConfig,
    profile: DatasetProfile,
    *,
    run_id: str,
    upload_id: str,
    model_version_id: str,
    artefacts: dict[str, str],
    created_at: datetime,
) -> RunRecord:
    """The finished run record. `headline_score` stays null: no model quality was measured here."""
    return RunRecord(
        run_id=run_id,
        use_case_id=config.id,
        use_case_name=config.name,
        mode=spec.mode,
        state=RunState.DONE,
        created_at=created_at,
        started_at=created_at,
        finished_at=created_at,
        upload_id=upload_id,
        file_name=spec.file_name,
        row_count=profile.row_count,
        primary_key=primary_key_of(config),
        target=target_of(config) if spec.mode is RunMode.TRAIN else None,
        problem_type=config.problem_type,
        model_choice=config.catalog.automl_choice.value,
        model_version_id=model_version_id,
        best_model=MODEL_DISPLAY_NAME,
        overrides={},
        artefacts=dict(artefacts),
        error=None,
        engine_version=engine.__version__,
    )


def _drift(
    spec: RunSpec,
    config: UseCaseConfig,
    frame: pd.DataFrame,
    *,
    run_id: str,
    baseline_run_id: str,
    model_version_id: str,
    created_at: datetime,
) -> DriftReport | None:
    """PSI of the scored rows against a baseline built from a second, independent draw.

    The baseline is not stored: it belongs to the training run this module does not fabricate, and
    that is also why it carries its own `baseline_run_id` - reusing this run's id would claim the
    file had been compared with itself. The numbers are genuinely measured, by the engine's own
    comparison, between two draws that differ only in their seed.

    Both sides are narrowed to the feature columns first, because a real baseline is built from the
    *prepared* training frame and prepare has already dropped the free text by then. Left in, a
    column whose every cell is a different sentence would report a large PSI in every run - a
    finding about free text, not about the data - and would drown the features that mean something.
    """
    baseline_spec = replace(spec, seed=_seed_for(spec.seed, "drift_baseline"), created_at=created_at)
    features = [name for name in feature_columns(config) if name in frame.columns]
    baseline = register.drift_baseline(
        _uploaded_frame(baseline_spec, config)[features],
        config,
        run_id=baseline_run_id,
        model_version_id=model_version_id,
        primary_key=primary_key_of(config),
    )
    report = score.compute_drift(baseline, frame[features], config, run_id=run_id)
    if report is None:  # pragma: no cover - a fabricated run always has rows and features
        return None
    return report.model_copy(update={"computed_at": created_at})


def _check_scores_csv(
    storage: Storage, key: str, config: UseCaseConfig, *, primary_key: str, rows: int
) -> None:
    """The exported table has the configured header and one line per row, or the fixture fails here.

    `ScoreRow` describes a row of this file with its reasons as objects, which the flat CSV cannot
    carry; the ten rows that keep them are validated through it inside `scoring_summary.json`. What
    is left to check is the shape the header promises, and it is checked against
    `engine.contracts.scores_csv_columns` rather than a list spelled again here.
    """
    lines = storage.read_text(key).splitlines()
    header = tuple(lines[0].split(",")) if lines else ()
    expected = scores_csv_columns(config, primary_key)
    if header != expected:
        raise ValueError(f"scores.csv header is {header}, the contract asks for {expected}")
    if len(lines) - 1 != rows:
        raise ValueError(f"scores.csv holds {len(lines) - 1} rows, the run scored {rows}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def write_run(storage: Storage, spec: RunSpec) -> str:
    """Write a finished run of `spec` into `storage` and return its run id.

    Every artefact is validated through the contract registered for its filename before it is
    written. The rows are stored where an upload would be, at
    `upload_key(record.upload_id, SOURCE_FILENAME)` (:func:`source_key`), so a flow that needs the
    text column or the uploaded values can read them back from the run record alone.
    """
    config = load_use_case(spec.use_case_id, spec.config_root)
    frame = _uploaded_frame(spec, config)
    created_at = _resolved_created_at(spec, config, frame)
    parts = _spec_parts(spec, created_at)
    run_id = f"r_{created_at:%Y%m%d}_{_digest(*parts, length=8)}"
    upload_id = f"u_{_digest('upload', *parts, length=12)}"
    baseline_run_id = f"r_{created_at:%Y%m%d}_{_digest('baseline', *parts, length=8)}"
    model_version_id = new_model_id(config.id, 1)
    primary_key = primary_key_of(config)

    source = upload_key(upload_id, SOURCE_FILENAME)
    storage.write_text(source, frame.to_csv(index=False, lineterminator="\n"))

    artefacts: dict[str, str] = {
        name: run_key(run_id, name) for name in ("run.json", "status.json", "run_manifest.json")
    }
    # `run_config.json` is produced honestly, by the resolver a real run uses, and everything below
    # reads `resolved.config` rather than the loaded one - the same document, since no override is
    # applied, but the one the run says it is reproducible from.
    resolved = resolve_config(spec.use_case_id, root=spec.config_root, now=created_at)
    config = resolved.config
    _write(storage, artefacts, run_id=run_id, filename="run_config.json", model=resolved)

    profile = _profile(
        frame,
        config,
        spec=spec,
        upload_id=upload_id,
        file_size_bytes=storage.size_bytes(source),
        created_at=created_at,
    )
    _write(storage, artefacts, run_id=run_id, filename="profile.json", model=profile)
    _write(
        storage,
        artefacts,
        run_id=run_id,
        filename="validation.json",
        model=_validation(spec, run_id=run_id, upload_id=upload_id, created_at=created_at),
    )
    _write(
        storage,
        artefacts,
        run_id=run_id,
        filename="prepare.json",
        model=_prepare_report(frame, config, run_id=run_id, created_at=created_at),
    )

    contributions = _contributions(spec, config, frame)
    scores = _scores(spec, contributions)
    importance = _feature_importance(contributions, run_id=run_id, with_reasons=spec.with_reasons)
    _write(storage, artefacts, run_id=run_id, filename="feature_importance.json", model=importance)

    explanations: tuple[RowExplanation, ...] = ()
    if spec.with_reasons:
        explanations = _explanations(frame, contributions, scores, config=config, importance=importance)
        key = explain.write_row_explanations(explanations, run_id=run_id, storage=storage)
        # Reading it straight back is what validates every row of the parquet through its contract.
        explain.read_row_explanations(key, storage=storage)
        artefacts[explain.ROW_EXPLANATIONS_FILENAME] = key

    metrics: dict[str, float] = {}
    if spec.mode is RunMode.SCORE:
        metrics = _write_score_artefacts(
            storage,
            artefacts,
            frame=frame,
            config=config,
            spec=spec,
            explanations=explanations,
            scores=scores,
            run_id=run_id,
            baseline_run_id=baseline_run_id,
            model_version_id=model_version_id,
            primary_key=primary_key,
            created_at=created_at,
        )

    _write(
        storage,
        artefacts,
        run_id=run_id,
        filename="status.json",
        model=_status(spec, run_id=run_id, created_at=created_at),
    )
    _write(
        storage,
        artefacts,
        run_id=run_id,
        filename="run_manifest.json",
        model=_manifest(
            profile, run_id=run_id, primary_key=primary_key, metrics=metrics, created_at=created_at
        ),
    )
    _write(
        storage,
        artefacts,
        run_id=run_id,
        filename="run.json",
        model=_record(
            spec,
            config,
            profile,
            run_id=run_id,
            upload_id=upload_id,
            model_version_id=model_version_id,
            artefacts=artefacts,
            created_at=created_at,
        ),
    )
    return run_id


def _write_score_artefacts(
    storage: Storage,
    artefacts: dict[str, str],
    *,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    spec: RunSpec,
    explanations: tuple[RowExplanation, ...],
    scores: npt.NDArray[np.float64],
    run_id: str,
    baseline_run_id: str,
    model_version_id: str,
    primary_key: str,
    created_at: datetime,
) -> dict[str, float]:
    """The four documents only a scoring run has, and the counts its manifest records.

    Bands, suppression and the control group are applied by `engine.stages.actions.apply_actions`
    and counted by `engine.stages.export`, so a suppressed row keeps its score and its band and
    loses only its action, the precedence between the three suppression rules is the engine's, and
    the holdout is the same per-customer hash a real run draws. `created_at` is the reference time
    of the recency rule, which is what a real run passes its start time for.
    """
    scored = frame.copy()
    scored[config.actions.score_field] = scores
    if explanations:
        scored = explain.with_reason_columns(explanations, scored, config, primary_key=primary_key)
    banded = actions_stage.apply_actions(
        scored, config, run_id=run_id, primary_key=primary_key, now=created_at
    )
    files = export.write_scores(banded, config, run_id=run_id, primary_key=primary_key, storage=storage)
    artefacts.update(files)
    _check_scores_csv(
        storage, files[export.SCORES_CSV], config, primary_key=primary_key, rows=len(banded.index)
    )

    drift = _drift(
        spec,
        config,
        frame,
        run_id=run_id,
        baseline_run_id=baseline_run_id,
        model_version_id=model_version_id,
        created_at=created_at,
    )
    metrics: dict[str, float] = {}
    if drift is not None:
        _write(storage, artefacts, run_id=run_id, filename="drift.json", model=drift)
        metrics["drift_max_psi"] = drift.max_psi

    summary = export.summarise(
        banded,
        config,
        run_id=run_id,
        model_version_id=model_version_id,
        model_display_name=MODEL_DISPLAY_NAME,
        primary_key=primary_key,
        drift=drift,
        files=files,
        kpi_source=frame,
        scored_at=created_at,
    )
    _write(storage, artefacts, run_id=run_id, filename="scoring_summary.json", model=summary)
    metrics["rows_scored"] = float(summary.rows_scored)
    return metrics
