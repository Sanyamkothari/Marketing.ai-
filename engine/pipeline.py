"""The train and score pipelines.

`Pipeline` owns the *order* of the stages, the two documents the Running screen polls
(`status.json` and `run.json`) and the one document a later reader asks questions of
(`run_manifest.json`). It owns no statistics: every number it writes was measured by the stage that
produced it, and every artefact is written by the stage that owns its contract.

Four properties are deliberate, and each is enforced at a named place below rather than merely
documented:

1. **The prepare seam (DEC-046).** `prepare` runs in two phases around the split:
   `prepare_rows` decides everything that needs no fitted statistic, `split_dataset` partitions
   those rows, and `fit_transforms` then fits every statistic on the **training partition only**
   before applying it everywhere. The single-frame `prepare()` convenience is never called here -
   it would fit medians, modes and clip bounds over the validation and test rows too, which is
   exactly the leak DEC-046 removed.
2. **The test split is final-decision-only.** It is measured once, by `evaluate`, for the decision,
   and read twice more for things that select nothing: the leaderboard's reported `score_test`
   (inside the train stage) and the explanation of an already-chosen model. Every site that touches
   it carries a comment saying so.
3. **The champion rule compares like with like.** The incumbent champion is reloaded and re-scored
   **on this run's test split**, through the same `evaluate` call the challenger got, and that fresh
   number - never the score stored with the champion's registry row - is what
   `register.build_model_version` decides on.
4. **A run manifest is written for every run.** The builder is filled as stages complete and
   flushed in a `finally`, so a failed or cancelled run still records the recipe, the fingerprint,
   the seed, whatever was measured and what the attempt cost. A failure inside that write is
   swallowed: a bookkeeping file must never turn a finished run into a failed one.
5. **The score flow replays; it never re-derives.** Preparation is not a decision a scoring run
   gets to make: the transforms are the ones the *training* run fitted and recorded, and
   `score.predict` reads that record and replays it **once**, as it loads the model that requires
   it. So the score flow's `prepare` stage reports and does nothing else - replaying a second time
   would clip to the training percentiles twice and fill with the training medians twice - and
   `prepare.prepare_rows`, which re-derives the column drops from the frame in front of it, is
   never called on a scoring batch at all.

`pandas` and the AutoGluon stack stay out of this module's import graph: the stage modules import
them inside their own function bodies, and this module only passes frames between stages.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Final, TypeVar, cast

from engine import __version__
from engine.aws.metrics import (
    MetricSink,
    NullMetricSink,
    record_job_cost,
    record_run_failed,
    record_run_started,
    record_stage_duration,
)
from engine.aws.run_index import RunIndex, mirror_run
from engine.config import ModelFamily, ResolvedConfig, RunMode, UseCaseConfig, recipe_from_config
from engine.contracts import (
    MODEL_DIRECTORY,
    CostEstimate,
    DatasetFingerprint,
    FeatureSchema,
    ModelStatus,
    RunError,
    RunManifest,
    RunRecord,
    RunState,
    RunStatus,
    StageKey,
    StageStatus,
)
from engine.errors import (
    RUN_BLOCKED_BY_VALIDATION,
    STAGE_FAILED,
    STAGE_OUT_OF_ORDER,
    engine_error,
    run_error,
)
from engine.jobs import CancelToken, JobCancelledError, JobRunner
from engine.registry import ModelRegistry
from engine.stages import (
    actions,
    evaluate,
    explain,
    export,
    ingest,
    prepare,
    register,
    score,
    train,
    validate,
)
from engine.stages.scorer import AutoGluonScorer, BaselineScorer, load_scorer
from engine.storage import Storage, StorageError, release_local, run_key
from engine.utils.ids import seed_from
from engine.utils.logging import bind_log_context, get_logger, log_stage
from engine.utils.text import humanise_count
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pandas as pd
    from pydantic import BaseModel

    from engine.config import Metric, Recipe
    from engine.contracts import (
        BestModel,
        DatasetProfile,
        EvaluationReport,
        FairnessReport,
        FeatureImportance,
        ModelVersion,
        ScoringSummary,
        SplitReport,
    )
    from engine.stages.register import ChampionScore

_LOGGER = get_logger(__name__)

_T = TypeVar("_T")

STATUS_FILENAME: Final[str] = "status.json"
RUN_FILENAME: Final[str] = "run.json"
MANIFEST_FILENAME: Final[str] = "run_manifest.json"
PROFILE_FILENAME: Final[str] = "profile.json"
VALIDATION_FILENAME: Final[str] = "validation.json"
PREPARE_FILENAME: Final[str] = "prepare.json"
SPLIT_FILENAME: Final[str] = "split.json"
LEADERBOARD_FILENAME: Final[str] = "leaderboard.json"
BEST_MODEL_FILENAME: Final[str] = "best_model.json"
EVALUATION_FILENAME: Final[str] = "evaluation.json"
CONFUSION_MATRIX_FILENAME: Final[str] = "confusion_matrix.json"
DECILE_LIFT_FILENAME: Final[str] = "decile_lift.json"
BASELINE_FILENAME: Final[str] = "baseline.json"
FAIRNESS_FILENAME: Final[str] = "fairness.json"
FEATURE_IMPORTANCE_FILENAME: Final[str] = "feature_importance.json"
DRIFT_FILENAME: Final[str] = "drift.json"
SCORING_SUMMARY_FILENAME: Final[str] = "scoring_summary.json"

REASONS_OFF_DETAIL: Final[str] = "per-row reasons turned off"
"""The score flow's explain row when `evaluation.shap` is off; the train flow says the same thing."""

COST_BASIS: Final[str] = "Local ThreadJobRunner; wall-clock seconds of the run's stages. Nothing was billed."
"""Why `CostEstimate.estimated_usd` is null rather than zero: nothing was billed (plan §13.3)."""

FILE_FINGERPRINT_ALGORITHM: Final[str] = "sha256-file"
"""Digest of the uploaded bytes, used only when a run failed before ingest parsed the file.

Named differently from ingest's content digest (`sha256:v1`) on purpose: the two cover different
things, and `n_rows=0` with no columns then reads as "the file was never parsed" rather than as an
empty dataset. (DEC-042.)
"""

UNPARSED_FINGERPRINT_ALGORITHM: Final[str] = "unavailable"
"""The upload could not even be read as bytes; `hash` is empty because no digest was taken."""

_DIGEST_CHUNK_BYTES: Final[int] = 1 << 20

TRAIN_STAGES: Final[tuple[StageKey, ...]] = (
    StageKey.INGEST,
    StageKey.VALIDATE,
    StageKey.PREPARE,
    StageKey.SPLIT,
    StageKey.TRAIN,
    StageKey.EVALUATE,
    StageKey.EXPLAIN,
    StageKey.REGISTER,
)

SCORE_STAGES: Final[tuple[StageKey, ...]] = (
    StageKey.INGEST,
    StageKey.VALIDATE_AGAINST_SCHEMA,
    StageKey.PREPARE,
    StageKey.PREDICT,
    StageKey.EXPLAIN_ROWS,
    StageKey.ACTIONS,
    StageKey.EXPORT,
)

STAGE_TITLES: Final[dict[StageKey, str]] = {
    StageKey.INGEST: "Reading the file",
    StageKey.VALIDATE: "Validating data",
    StageKey.PREPARE: "Preparing features",
    StageKey.SPLIT: "Splitting the data",
    StageKey.TRAIN: "Training candidate models",
    StageKey.EVALUATE: "Evaluating on hold-out set",
    StageKey.EXPLAIN: "Generating explanations",
    StageKey.REGISTER: "Saving model",
    StageKey.VALIDATE_AGAINST_SCHEMA: "Validating columns",
    StageKey.PREDICT: "Scoring rows",
    StageKey.EXPLAIN_ROWS: "Generating reasons",
    StageKey.ACTIONS: "Applying actions",
    StageKey.EXPORT: "Writing results",
}

GROUP_LABELS: Final[dict[tuple[RunMode, StageKey], str]] = {
    (RunMode.TRAIN, StageKey.INGEST): "Validating data",
    (RunMode.TRAIN, StageKey.VALIDATE): "Validating data",
    (RunMode.TRAIN, StageKey.PREPARE): "Preparing features",
    (RunMode.TRAIN, StageKey.SPLIT): "Preparing features",
    (RunMode.TRAIN, StageKey.TRAIN): "Training candidate models",
    (RunMode.TRAIN, StageKey.EVALUATE): "Evaluating on hold-out set",
    (RunMode.TRAIN, StageKey.EXPLAIN): "Generating explanations & saving",
    (RunMode.TRAIN, StageKey.REGISTER): "Generating explanations & saving",
    (RunMode.SCORE, StageKey.INGEST): "Validating columns",
    (RunMode.SCORE, StageKey.VALIDATE_AGAINST_SCHEMA): "Validating columns",
    (RunMode.SCORE, StageKey.PREPARE): "Loading champion model",
    (RunMode.SCORE, StageKey.PREDICT): "Scoring rows",
    (RunMode.SCORE, StageKey.EXPLAIN_ROWS): "Generating reasons & actions",
    (RunMode.SCORE, StageKey.ACTIONS): "Generating reasons & actions",
    (RunMode.SCORE, StageKey.EXPORT): "Generating reasons & actions",
}

_PROMOTION_WORDS: Final[dict[ModelStatus, str]] = {
    ModelStatus.PENDING_APPROVAL: "awaiting approval as champion",
    ModelStatus.CHAMPION: "set as champion",
    ModelStatus.CANDIDATE: "kept as candidate",
    ModelStatus.ARCHIVED: "archived",
}

_PROMOTED: Final[frozenset[ModelStatus]] = frozenset({ModelStatus.CHAMPION, ModelStatus.PENDING_APPROVAL})
"""Statuses the champion rule promotes into; `candidate` is the outcome of not promoting."""


def stages_for(mode: RunMode) -> tuple[StageKey, ...]:
    """The engine stages executed in `mode`, in order."""
    return TRAIN_STAGES if mode is RunMode.TRAIN else SCORE_STAGES


def running_rows(mode: RunMode) -> tuple[str, ...]:
    """The distinct Running-screen rows of `mode`, in order: five for train, four for score."""
    rows: list[str] = []
    for key in stages_for(mode):
        label = GROUP_LABELS[(mode, key)]
        if label not in rows:
            rows.append(label)
    return tuple(rows)


@dataclass(frozen=True)
class StageContext:
    """Everything a stage needs; a frozen dataclass because it holds protocol instances."""

    run_id: str
    mode: RunMode
    config: UseCaseConfig
    resolved: ResolvedConfig
    storage: Storage
    registry: ModelRegistry
    cancel: CancelToken
    primary_key: str
    target: str | None
    upload_key: str
    model_version_id: str | None


def _require(value: _T | None, what: str) -> _T:
    """`value`, or an `EngineError` naming what is missing; only a wiring mistake reaches this."""
    if value is None:
        raise engine_error(STAGE_OUT_OF_ORDER, what=what)
    return value


# ---------------------------------------------------------------------------
# status.json
# ---------------------------------------------------------------------------
class _StatusWriter:
    """Holds the run's `RunStatus` and rewrites `status.json` on every transition.

    `Storage.write_model` is atomic, so the Running screen's two-second poll always reads a whole
    document, never a half-written one.
    """

    def __init__(self, storage: Storage, status: RunStatus) -> None:
        self._storage = storage
        self._status = status

    @property
    def status(self) -> RunStatus:
        """The status as last written."""
        return self._status

    def start(self, key: StageKey) -> None:
        """Mark `key` running and write the document."""
        self._apply(key, {"state": RunState.RUNNING, "started_at": utc_now()}, current=key)

    def finish(self, key: StageKey, *, detail: str, seconds: float) -> None:
        """Mark `key` done with its detail line and duration, and write the document."""
        self._apply(
            key,
            {
                "state": RunState.DONE,
                "ended_at": utc_now(),
                "duration_seconds": round(seconds, 3),
                "detail": detail,
            },
            current=None,
        )

    def stop(self, key: StageKey, state: RunState, *, seconds: float, error: RunError | None) -> None:
        """End the run at `key`: that stage takes `state`, every stage still pending is skipped."""
        changes: dict[str, object] = {
            "state": state,
            "ended_at": utc_now(),
            "duration_seconds": round(seconds, 3),
        }
        if error is not None:
            changes["error"] = error
        self._apply(key, changes, current=None, skip_pending=True, run_state=state)

    def _apply(
        self,
        key: StageKey,
        changes: dict[str, object],
        *,
        current: StageKey | None,
        skip_pending: bool = False,
        run_state: RunState | None = None,
    ) -> None:
        rows: list[StageStatus] = []
        for row in self._status.stages:
            if row.key is key:
                rows.append(row.model_copy(update=changes))
            elif skip_pending and row.state is RunState.PENDING:
                rows.append(row.model_copy(update={"state": RunState.SKIPPED}))
            else:
                rows.append(row)
        stages = tuple(rows)
        self._status = self._status.model_copy(
            update={
                "stages": stages,
                "state": _run_state(stages) if run_state is None else run_state,
                "current_stage": current,
                "progress_pct": _progress_pct(stages),
                "updated_at": utc_now(),
            }
        )
        self._storage.write_model(run_key(self._status.run_id, STATUS_FILENAME), self._status)


def _run_state(stages: Sequence[StageStatus]) -> RunState:
    """The run-level state implied by its stages, the same rule the API's `update_stage` uses."""
    states = {row.state for row in stages}
    if RunState.FAILED in states:
        return RunState.FAILED
    if RunState.CANCELLED in states:
        return RunState.CANCELLED
    if states <= {RunState.DONE, RunState.SKIPPED}:
        return RunState.DONE
    if states & {RunState.RUNNING, RunState.DONE}:
        return RunState.RUNNING
    return RunState.PENDING


def _progress_pct(stages: Sequence[StageStatus]) -> int:
    """Completed stages over total, as whole percent.

    Only `done` counts: a skipped stage is work that will never happen, so counting it would let a
    run that failed in its first stage report almost complete. With eight train stages the sequence
    is 12, 25, 38, 50, 62, 75, 88, 100 - Python's `round` is half-even, so the two exact halves
    (12.5 and 62.5) round down; the M3 design's "13 … 63" assumed half-up.
    """
    if not stages:
        return 0
    return round(100 * sum(1 for row in stages if row.state is RunState.DONE) / len(stages))


# ---------------------------------------------------------------------------
# run_manifest.json
# ---------------------------------------------------------------------------
@dataclass
class _ManifestBuilder:
    """Collects `run_manifest.json` as the stages complete, so a failed run still has one.

    Nothing here is computed: each field is set by the stage that learned it. What is missing stays
    missing - an empty metric map rather than a fabricated zero, a null `leaderboard_path` rather
    than a key to a file that was never written.
    """

    run_id: str
    seed: int
    started: float
    fingerprint: DatasetFingerprint | None = None
    recipe: Recipe | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    leaderboard_path: str | None = None
    seconds: dict[str, float] = field(default_factory=dict)

    def record(self, key: StageKey, seconds: float) -> None:
        """Remember what a stage cost, whether it finished, failed or was cancelled."""
        self.seconds[key.value] = round(seconds, 3)

    def add_metrics(self, values: dict[str, float], *, prefix: str = "") -> None:
        """Add measured numbers under their own names, optionally prefixed (`baseline_`, `champion_`)."""
        self.metrics.update({f"{prefix}{name}": float(value) for name, value in values.items()})

    def build(self, fingerprint: DatasetFingerprint) -> RunManifest:
        """The manifest as it stands; `fingerprint` is the fallback when ingest never produced one."""
        return RunManifest(
            run_id=self.run_id,
            recipe=self.recipe,
            dataset_fingerprint=self.fingerprint if self.fingerprint is not None else fingerprint,
            seed=self.seed,
            metrics=dict(self.metrics),
            leaderboard_path=self.leaderboard_path,
            duration_s=round(perf_counter() - self.started, 3),
            cost_estimate=CostEstimate(
                compute_seconds=round(math.fsum(self.seconds.values()), 3),
                estimated_usd=None,
                basis=COST_BASIS,
            ),
            created_at=utc_now(),
        )


def _file_fingerprint(storage: Storage, key: str) -> DatasetFingerprint:
    """A digest of the uploaded bytes, for a run that failed before ingest parsed them.

    A real digest of real bytes, under its own algorithm name, with `n_rows=0` and no columns
    because nothing was ever parsed. When even the bytes cannot be read, the hash is empty under
    `algorithm="unavailable"`: an invented digest would be indistinguishable from a measured one.
    """
    digest = hashlib.sha256()
    try:
        with storage.open_read(key) as handle:
            while chunk := handle.read(_DIGEST_CHUNK_BYTES):
                digest.update(chunk)
    except (StorageError, OSError):
        return DatasetFingerprint(hash="", algorithm=UNPARSED_FINGERPRINT_ALGORITHM, n_rows=0, columns=())
    return DatasetFingerprint(
        hash=digest.hexdigest(), algorithm=FILE_FINGERPRINT_ALGORITHM, n_rows=0, columns=()
    )


# ---------------------------------------------------------------------------
# run.json
# ---------------------------------------------------------------------------
class _RunWriter:
    """Reads `run.json`, keeps it in memory and rewrites it whole.

    `POST /runs` writes the record before the job is submitted, so the normal path reads what the
    API wrote and only changes the fields a run earns. A record that is missing is rebuilt from the
    stage context, so a pipeline driven directly - a test, a script - still records its run.
    """

    def __init__(self, ctx: StageContext) -> None:
        self._ctx = ctx
        self._key = run_key(ctx.run_id, RUN_FILENAME)
        self._record = self._load()

    @property
    def record(self) -> RunRecord:
        """The record as last written."""
        return self._record

    def _load(self) -> RunRecord:
        ctx = self._ctx
        if ctx.storage.exists(self._key):
            return ctx.storage.read_model(self._key, RunRecord)
        config = ctx.config
        metric = config.model_search.metric
        upload = _upload_parts(ctx.upload_key)
        return RunRecord(
            run_id=ctx.run_id,
            use_case_id=config.id,
            use_case_name=config.name,
            mode=ctx.mode,
            state=RunState.PENDING,
            created_at=utc_now(),
            upload_id=upload[0],
            file_name=upload[1],
            primary_key=ctx.primary_key,
            target=ctx.target,
            problem_type=config.problem_type,
            model_choice=config.catalog.automl_choice.value,
            model_version_id=ctx.model_version_id,
            headline_metric=metric,
            headline_metric_label=config.catalog.metric_label(metric),
            overrides=dict(ctx.resolved.overrides_applied),
            engine_version=__version__,
        )

    def update(self, **fields: object) -> RunRecord:
        """Apply `fields` to the record and write it."""
        self._record = self._record.model_copy(update=fields)
        self._ctx.storage.write_model(self._key, self._record)
        return self._record


def _upload_parts(key: str) -> tuple[str, str]:
    """`(upload id, file name)` read off an upload key, for a run record the API did not write."""
    segments = key.split("/")
    upload_id = segments[-2] if len(segments) >= 2 else ""
    return upload_id, segments[-1]


# ---------------------------------------------------------------------------
# The train flow
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _StageOutcome:
    """What a stage body reports: the line the Running screen shows, and the rows it touched."""

    detail: str
    rows: int | None = None


class _TrainFlow:
    """One execution of the train flow: the stage bodies, plus the bookkeeping around them.

    State moves between stages on this object rather than through a dictionary, so mypy checks
    every hand-off and an out-of-order stage is a named error rather than a `KeyError`.
    """

    def __init__(self, pipeline: Pipeline, ctx: StageContext) -> None:
        self._ctx = ctx
        self._storage = ctx.storage
        self._seed = seed_from(ctx.run_id)
        self._started = perf_counter()
        self._status = _StatusWriter(ctx.storage, pipeline.initial_status(ctx.run_id, ctx.mode))
        self._run = _RunWriter(ctx)
        self._index = pipeline.run_index
        self._metrics = pipeline.metrics
        self._manifest = _ManifestBuilder(run_id=ctx.run_id, seed=self._seed, started=self._started)
        self._artefacts: dict[str, str] = dict(self._run.record.artefacts)
        for name in (RUN_FILENAME, STATUS_FILENAME, MANIFEST_FILENAME):
            # The three documents the pipeline itself owns; the manifest is written in the `finally`
            # below whatever happens, so the map names it from the start.
            self._artefacts.setdefault(name, run_key(ctx.run_id, name))
        config_key = run_key(ctx.run_id, register.RUN_CONFIG_FILENAME)
        if ctx.storage.exists(config_key):
            # `POST /runs` writes the resolved configuration before the job starts; name it too, so
            # the record lists every file the run directory holds rather than only what this ran.
            self._artefacts.setdefault(register.RUN_CONFIG_FILENAME, config_key)
        # what each stage hands the next one
        self._frame: pd.DataFrame | None = None
        self._profile: DatasetProfile | None = None
        self._rows: pd.DataFrame | None = None
        self._plan: prepare.RowPlan | None = None
        self._recipe: Recipe | None = None
        self._parts: dict[str, pd.DataFrame] | None = None
        self._split: SplitReport | None = None
        self._result: train.TrainResult | None = None
        self._evaluation: EvaluationReport | None = None
        self._importance: FeatureImportance | None = None
        self._reasons: explain.RowReasons | None = None
        self._version: ModelVersion | None = None
        self._champion: ChampionScore | None = None

    # -- the driver ---------------------------------------------------------
    def execute(self) -> RunRecord:
        """Run every stage in order, then write the finished run record.

        The `finally` writes the manifest whatever the outcome, and then releases anything the
        store was mirroring on local disk for this run. On `LocalStorage` the release is a no-op and
        nothing about a local run changes; on a mirrored store it publishes whatever a stage wrote
        through `local_path` and did not itself publish, and reclaims the disk (DEC-311). It runs
        after the manifest because the manifest is written through the store like any other artefact.
        """
        # The context is bound HERE, inside the job body, and not around the `submit` that started
        # it: a `ContextVar` set on the request thread is not copied into a `ThreadPoolExecutor`
        # worker, so a binding made outside would simply not be there when a stage logged (DEC-382).
        with bind_log_context(run_id=self._ctx.run_id):
            record_run_started(self._metrics, use_case_id=self._ctx.config.id)
            try:
                self._run.update(state=RunState.RUNNING, started_at=utc_now())
                for key, body in self._bodies():
                    self._run_stage(key, body)
                return self._complete()
            except JobCancelledError:
                # A cancellation is not a failure. Counting it as one would make an alarm on
                # RunsFailed fire every time somebody changed their mind (DEC-388).
                raise
            except Exception:
                record_run_failed(self._metrics, use_case_id=self._ctx.config.id)
                raise
            finally:
                self._flush_manifest()
                release_local(self._storage, f"runs/{self._ctx.run_id}/")

    def _bodies(self) -> tuple[tuple[StageKey, Callable[[], _StageOutcome]], ...]:
        """The eight stages of plan §6.1, in order, each with the body that runs it."""
        return (
            (StageKey.INGEST, self._ingest),
            (StageKey.VALIDATE, self._validate),
            (StageKey.PREPARE, self._prepare),
            (StageKey.SPLIT, self._split_and_fit),
            (StageKey.TRAIN, self._train),
            (StageKey.EVALUATE, self._evaluate),
            (StageKey.EXPLAIN, self._explain),
            (StageKey.REGISTER, self._register),
        )

    def _run_stage(self, key: StageKey, body: Callable[[], _StageOutcome]) -> None:
        """One stage: cancel, run, cancel, record. Both checkpoints bracket the stage body.

        Checkpoint A is inside the `try` as well, so a run cancelled between two stages still stops
        *at* this one - that stage is recorded `cancelled` with no `started_at`, and everything
        after it `skipped` - rather than escaping with both documents left mid-run.
        """
        started = perf_counter()
        try:
            self._ctx.cancel.raise_if_cancelled()
            self._status.start(key)
            with bind_log_context(stage=key.value):
                outcome = body()
            self._ctx.cancel.raise_if_cancelled()
        except JobCancelledError:
            self._stop(key, RunState.CANCELLED, perf_counter() - started, error=None)
            raise
        except Exception as exc:
            error = run_error(exc, stage=key, title=STAGE_TITLES[key])
            if error.code == STAGE_FAILED:
                _LOGGER.exception("stage=%s failed unexpectedly in run %s", key.value, self._ctx.run_id)
            else:
                _LOGGER.warning("stage=%s failed: %s %s", key.value, error.code, error.message)
            self._stop(key, RunState.FAILED, perf_counter() - started, error=error)
            raise
        seconds = perf_counter() - started
        self._status.finish(key, detail=outcome.detail, seconds=seconds)
        self._manifest.record(key, seconds)
        log_stage(_LOGGER, key.value, rows=outcome.rows, seconds=seconds)
        record_stage_duration(self._metrics, stage=key.value, seconds=seconds)

    def _stop(self, key: StageKey, state: RunState, seconds: float, *, error: RunError | None) -> None:
        """Write both documents before the exception leaves the pipeline, so the UI's next poll sees why."""
        self._manifest.record(key, seconds)
        self._status.stop(key, state, seconds=seconds, error=error)
        self._run.update(state=state, finished_at=utc_now(), error=error, artefacts=dict(self._artefacts))

    def _complete(self) -> RunRecord:
        """The finished run record: the headline numbers, the model version and every artefact key."""
        evaluation = _require(self._evaluation, "the evaluation report")
        result = _require(self._result, "the trained model")
        version = _require(self._version, "the registered model version")
        promoted = version.status in _PROMOTED
        rescored = self._champion is not None and self._champion.test_score is not None
        return self._run.update(
            state=RunState.DONE,
            finished_at=utc_now(),
            row_count=_require(self._profile, "the dataset profile").row_count,
            best_model=result.best.display_name,
            headline_metric=evaluation.primary_metric,
            headline_metric_label=evaluation.primary_metric_label,
            headline_score=evaluation.headline_score,
            model_version_id=version.model_id,
            champion=version.status is ModelStatus.CHAMPION,
            beat_previous_champion=promoted and rescored,
            artefacts=dict(self._artefacts),
            error=None,
        )

    def _flush_manifest(self) -> None:
        """Write `run_manifest.json` last, for every outcome; a failure here never fails the run."""
        try:
            manifest = self._manifest.build(_file_fingerprint(self._storage, self._ctx.upload_key))
            self._storage.write_model(run_key(self._ctx.run_id, MANIFEST_FILENAME), manifest)
            record_job_cost(
                self._metrics,
                manifest.cost_estimate,
                backend="local" if manifest.compute is None else manifest.compute.backend,
            )
            _LOGGER.info(
                "manifest: run=%s recipe=%s fingerprint=%s duration=%.1fs",
                manifest.run_id,
                "none" if manifest.recipe is None else manifest.recipe.recipe_hash[:12],
                manifest.dataset_fingerprint.hash[:12],
                manifest.duration_s,
            )
        except Exception:
            _LOGGER.exception("the run manifest could not be written for run %s", self._ctx.run_id)
            manifest = None
        # The index row is written here, last, because this is the one point at which both `run.json`
        # and the manifest are final - so a row never describes a run that is still moving. It is
        # deliberately last in a second sense too: `mirror_run` never raises, because `run.json` is
        # the record and the row is only an index of it. A run that finished must not be reported as
        # failed because a database was briefly unreachable (DEC-342).
        mirror_run(self._index, self._run.record, manifest)

    def _write(self, filename: str, model: BaseModel) -> str:
        """Write one artefact into the run directory and remember its key for `run.json`."""
        key = run_key(self._ctx.run_id, filename)
        self._storage.write_model(key, model)
        self._artefacts[filename] = key
        return key

    # -- the stages ---------------------------------------------------------
    def _ingest(self) -> _StageOutcome:
        """Read the upload once and profile it; the fingerprint goes straight to the manifest."""
        ctx = self._ctx
        read = ingest.read_upload(self._storage, ctx.upload_key, row_cap=ingest.profile_row_cap(ctx.config))
        record = self._run.record
        profile = ingest.profile_dataset(
            read.frame,
            ctx.config,
            upload_id=record.upload_id,
            file_name=record.file_name,
            file_format=read.file_format,
            file_size_bytes=self._storage.size_bytes(ctx.upload_key),
            delimiter=read.delimiter,
            encoding=read.encoding,
            row_count=read.row_count,
            fingerprint=read.fingerprint,
        )
        self._write(PROFILE_FILENAME, profile)
        self._frame = read.frame
        self._profile = profile
        self._manifest.fingerprint = profile.fingerprint
        return _StageOutcome(ingest.ingest_detail(profile), len(read.frame.index))

    def _validate(self) -> _StageOutcome:
        """Run every check; an error-severity finding stops the run before anything is fitted."""
        ctx = self._ctx
        profile = _require(self._profile, "the dataset profile")
        report = validate.validate_for_training(
            _require(self._frame, "the uploaded rows"),
            ctx.config,
            primary_key=ctx.primary_key,
            target=ctx.target,
            acknowledged=ctx.config.validation.acknowledged,
            upload_id=profile.upload_id,
            run_id=ctx.run_id,
            row_count=profile.row_count,
        )
        self._write(VALIDATION_FILENAME, report)
        if not report.passed:
            # `POST /runs` refuses with 409 before a run is created; this is the defensive twin.
            raise engine_error(RUN_BLOCKED_BY_VALIDATION, stage=StageKey.VALIDATE)
        return _StageOutcome(validate.validation_detail(report), profile.row_count)

    def _prepare(self) -> _StageOutcome:
        """Phase 1 of prepare: every rule that needs no fitted statistic, then the recipe.

        THE PREPARE SEAM (DEC-046). Only the row-level rules run here, because the split has not
        happened yet and no statistic may be fitted before it. `prepare.prepare()` - the
        single-frame convenience - would fit medians, modes and clip bounds across the validation
        and test rows, which is the leak DEC-046 removed; `prepare.json` is therefore written at
        the end of the split stage, where the fitted parameters first exist.
        """
        ctx = self._ctx
        rows, plan = prepare.prepare_rows(
            _require(self._frame, "the uploaded rows"),
            ctx.config,
            primary_key=ctx.primary_key,
            target=ctx.target,
        )
        self._rows = rows
        self._plan = plan
        # The feature list is settled by phase 1, so the recipe exists even if the split fails.
        self._recipe = recipe_from_config(
            ctx.config,
            primary_key=ctx.primary_key,
            feature_columns=plan.feature_columns,
            seed=self._seed,
            target=ctx.target,
        )
        self._manifest.recipe = self._recipe
        _LOGGER.info(
            "prepare: %d rows in, %d rows out, %d columns dropped",
            plan.rows_in,
            len(rows.index),
            len(plan.dropped_columns),
        )
        return _StageOutcome(_row_phase_detail(plan, len(rows.index)), len(rows.index))

    def _split_and_fit(self) -> _StageOutcome:
        """Split the rows, fit every transform on the training partition, then re-slice the parts.

        The order is the whole point of the seam: `fit_index` is the **training** partition's index,
        so a median, a mode or a clip bound can only have seen training rows. The parts are then
        recovered from the fitted frame by index, because phase 2 may drop rows (the `remove_rows`
        outlier mode) and a stale slice would carry rows that no longer exist.
        """
        ctx = self._ctx
        target = _require(ctx.target, "the target column")
        rows = _require(self._rows, "the row-level prepared frame")
        plan = _require(self._plan, "the row-level prepare plan")
        parts, report = prepare.split_dataset(rows, ctx.config, run_id=ctx.run_id, target=target)
        prepared, prepare_report = prepare.fit_transforms(
            rows, ctx.config, plan, run_id=ctx.run_id, fit_index=parts["train"].index
        )
        self._parts = {
            name: prepared.loc[part.index[part.index.isin(prepared.index)]] for name, part in parts.items()
        }
        self._split = report
        self._write(PREPARE_FILENAME, prepare_report)
        self._write(SPLIT_FILENAME, report)
        _LOGGER.info(
            "split: %s train=%d validation=%d test=%d",
            report.type.value,
            len(self._parts["train"].index),
            len(self._parts["validation"].index),
            len(self._parts["test"].index),
        )
        return _StageOutcome(report.detail, len(prepared.index))

    def _train(self) -> _StageOutcome:
        """`train(recipe, …)` - the only entry point to training - then its two artefacts."""
        ctx = self._ctx
        recipe = _require(self._recipe, "the training recipe")
        parts = _require(self._parts, "the split partitions")
        result = train.train(
            recipe,
            parts,
            ctx.config.evaluation,
            run_id=ctx.run_id,
            storage=self._storage,
            cancel=ctx.cancel,
        )
        self._result = result
        self._write(LEADERBOARD_FILENAME, result.leaderboard)
        self._write(BEST_MODEL_FILENAME, result.best)
        self._artefacts[MODEL_DIRECTORY] = result.predictor_key
        self._manifest.leaderboard_path = self._artefacts[LEADERBOARD_FILENAME]
        _LOGGER.info(
            "train: recipe=%s %d candidates, %d models fitted, best=%s",
            recipe.recipe_hash[:12],
            len(recipe.model_search.candidates),
            result.leaderboard.models_trained,
            result.best.model_name,
        )
        return _StageOutcome(result.detail, len(parts["train"].index))

    def _evaluate(self) -> _StageOutcome:
        """Measure the model on the hold-out once, and the baseline on the same frame."""
        ctx = self._ctx
        result = _require(self._result, "the trained model")
        # TEST IS FINAL-DECISION-ONLY: this is the measurement the run is decided on. Nothing here
        # chooses a threshold, a calibrator, a model or a feature - the scorer arrived with all of
        # those settled on the validation split at train time.
        test = _require(self._parts, "the split partitions")["test"]
        report, matrix, lift, fairness = evaluate.evaluate(
            _as_scorer(result.model), test, ctx.config.evaluation, run_id=ctx.run_id
        )
        self._evaluation = report
        self._write(EVALUATION_FILENAME, report)
        if matrix is not None:
            self._write(CONFUSION_MATRIX_FILENAME, matrix)
        self._write(DECILE_LIFT_FILENAME, lift)
        self._write(FAIRNESS_FILENAME, fairness)
        self._manifest.add_metrics(_metric_values(report))
        if result.baseline is not None:
            baseline_report, _, _, _ = evaluate.evaluate(
                _as_scorer(result.baseline), test, ctx.config.evaluation, run_id=ctx.run_id
            )
            comparison = evaluate.compare_to_baseline(
                report,
                baseline_report,
                baseline_name=result.baseline.display_name,
                baseline_family=_baseline_family(result.baseline.family),
            )
            self._write(BASELINE_FILENAME, comparison)
            self._manifest.add_metrics(_metric_values(baseline_report), prefix="baseline_")
        return _StageOutcome(_evaluate_detail(report, fairness), report.rows_evaluated)

    def _explain(self) -> _StageOutcome:
        """The importance chart, and per-row reasons when `evaluation.shap` asks for them."""
        ctx = self._ctx
        result = _require(self._result, "the trained model")
        # TEST IS FINAL-DECISION-ONLY: explaining a model that was already chosen selects nothing -
        # no feature is dropped, no model re-ranked and no threshold moved by what comes back.
        test = _require(self._parts, "the split partitions")["test"]
        importance = explain.global_importance(
            result.predictor_key, test, ctx.config, run_id=ctx.run_id, storage=self._storage
        )
        self._importance = importance
        self._write(FEATURE_IMPORTANCE_FILENAME, importance)
        rows = 0
        if ctx.config.evaluation.shap:
            reasons = explain.reasons_for(
                result.model,
                test,
                ctx.config,
                primary_key=ctx.primary_key,
                seed=self._seed,
                importance=importance,
            )
            self._reasons = reasons
            key = explain.write_row_explanations(
                reasons.explanations, run_id=ctx.run_id, storage=self._storage
            )
            self._artefacts[explain.ROW_EXPLANATIONS_FILENAME] = key
            rows = len(reasons.explanations)
        return _StageOutcome(explain.explain_detail(importance, self._reasons), rows)

    def _register(self) -> _StageOutcome:
        """Schema, drift baseline, the champion re-score and the registry row."""
        ctx = self._ctx
        recipe = _require(self._recipe, "the training recipe")
        result = _require(self._result, "the trained model")
        evaluation = _require(self._evaluation, "the evaluation report")
        parts = _require(self._parts, "the split partitions")
        model_id, version = register.next_version_id(ctx)
        # `schema.json` and `drift_baseline.json` describe the columns the model was FITTED ON, in
        # fit order (design §7.1), so the frame handed to them is narrowed to the recipe's feature
        # list plus the target. The prepared training split still carries the reserved columns - the
        # key, the snapshot date, the consent flag - because the score flow needs them, but a model
        # that never saw a column must not claim it in its schema, and M4 checks a scoring file
        # against exactly this list.
        fitted = parts["train"]
        columns = [name for name in (*recipe.feature_columns, recipe.target) if name in fitted.columns]
        train_frame = fitted[columns]
        schema = register.feature_schema(
            train_frame,
            ctx.config,
            primary_key=ctx.primary_key,
            target=ctx.target,
            model_version_id=model_id,
        )
        self._write(register.SCHEMA_FILENAME, schema)
        baseline = register.drift_baseline(
            train_frame,
            ctx.config,
            run_id=ctx.run_id,
            model_version_id=model_id,
            primary_key=ctx.primary_key,
        )
        self._write(register.DRIFT_BASELINE_FILENAME, baseline)
        champion = self._rescore_champion(recipe.model_search.metric, parts["test"])
        self._champion = champion
        stored = register.store_model_version(
            ctx.registry,
            register.build_model_version(
                ctx, self._best_for_registry(result.best, evaluation), schema, champion_score=champion
            ),
        )
        self._version = stored
        _LOGGER.info(
            "register: %s version %d status=%s champion_score=%s",
            stored.model_id,
            version,
            stored.status.value,
            "none" if champion is None else champion.test_score,
        )
        return _StageOutcome(_register_detail(ctx.config, stored.status), None)

    # -- the champion rule --------------------------------------------------
    def _rescore_champion(self, metric: Metric, test: pd.DataFrame) -> ChampionScore | None:
        """Re-score the reigning champion on **this run's** test split, or say why it could not be.

        THE STORED SCORE IS NEVER READ. `ModelVersion.test_score` on the champion's row was measured
        on whatever hold-out existed when that model was trained; comparing against it would compare
        two different samples of two different datasets. The champion is reloaded with its own
        threshold and calibrator and measured here, through the same `evaluate` call the challenger
        got, on the same frame.

        TEST IS FINAL-DECISION-ONLY: this is a second measurement of the same held-out frame, for
        the same decision. Nothing about the challenger changes because of it.
        """
        ctx = self._ctx
        champion = ctx.registry.get_champion(ctx.config.id)
        if champion is None:
            return None
        try:
            scorer = load_scorer(champion.predictor_key, self._storage)
        except Exception as exc:
            return _unavailable(champion, f"its saved model could not be loaded ({_reason(exc)})")
        usable, why = scorer.can_score(test)
        if not usable:
            return _unavailable(champion, f"it cannot score this run's test split: {why}")
        try:
            report, _, _, _ = evaluate.evaluate(
                _as_scorer(scorer), test, ctx.config.evaluation, run_id=ctx.run_id
            )
        except evaluate.EvaluationError as exc:
            return _unavailable(champion, f"it could not be scored on this run's test split ({exc.code})")
        value = next((item.value for item in report.metrics if item.id is metric), None)
        if value is None:
            return _unavailable(champion, f"{metric.value} has no value on this run's test split")
        self._manifest.add_metrics({metric.value: value}, prefix="champion_")
        _LOGGER.info(
            "register: champion %s re-scored on this run's test split: %s=%s",
            champion.model_id,
            metric.value,
            value,
        )
        return register.ChampionScore(model_id=champion.model_id, metric=metric, test_score=value)

    def _best_for_registry(self, best: BestModel, evaluation: EvaluationReport) -> BestModel:
        """`best_model.json` with the **engine's** hold-out score, which is what the rule compares.

        `BestModel.test_score` is AutoGluon's own measurement, taken while the leaderboard was
        built. The champion can only be re-scored through `evaluate`, so the challenger's number
        must come from there too: one frame, one metric and one measurement path, or the comparison
        is between two different things again. `best_model.json` itself keeps AutoGluon's number -
        the train stage measured it and owns it.
        """
        if evaluation.primary_metric is not best.metric:
            return best
        return best.model_copy(update={"test_score": evaluation.headline_score})


def _as_scorer(model: AutoGluonScorer | BaselineScorer) -> evaluate.Scorer:
    """The fitted scorer as the evaluate stage's `Scorer`, which it satisfies but cannot prove.

    `evaluate.Scorer` declares its eight members as variables, while `scorer._FittedScorer`
    implements every one of them as a read-only property over the persisted `ScorerState` - so a
    structural check calls them settable-versus-read-only and refuses, although the objects match
    at runtime (`test_scorer.py` asserts `isinstance(scorer, Scorer)` for both scorers). Declaring
    those protocol members as properties in `engine/stages/evaluate.py` is the real fix; that file
    belongs to another owner, so the mismatch is narrowed to this one function rather than spread
    over three call sites.
    """
    return cast("evaluate.Scorer", model)


def _baseline_family(family: ModelFamily | None) -> ModelFamily:
    """The family the baseline table names; the baseline scorer always carries one in practice."""
    return ModelFamily.LOGISTIC_REGRESSION if family is None else family


def _unavailable(champion: ModelVersion, reason: str) -> ChampionScore:
    """A `ChampionScore` that carries why the incumbent could not be re-scored, logged at WARNING."""
    _LOGGER.warning("register: champion %s was not re-scored: %s", champion.model_id, reason)
    return register.ChampionScore(model_id=champion.model_id, unavailable_reason=reason)


def _reason(exc: Exception) -> str:
    """The code of a coded engine exception, for a note that names a cause without a traceback."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else type(exc).__name__


def _metric_values(report: EvaluationReport) -> dict[str, float]:
    """Every number the evaluation measured, by name: the catalog metrics plus the extras."""
    values = {item.id.value: item.value for item in report.metrics}
    values.update(report.extra_metrics)
    return values


def _row_phase_detail(plan: prepare.RowPlan, rows: int) -> str:
    """The Running line for phase 1 of prepare; the fitted phase's own line comes from `split`."""
    features = len(plan.feature_columns)
    segments = [
        f"{humanise_count(rows)} rows ready",
        f"{features} {'feature' if features == 1 else 'features'}",
    ]
    dropped = len(plan.dropped_columns)
    if dropped:
        segments.append(f"{dropped} {'column' if dropped == 1 else 'columns'} dropped")
    return " · ".join(segments)


def _evaluate_detail(report: EvaluationReport, fairness: FairnessReport) -> str:
    """The Running line for evaluate (M3 design §5.10): headline, optimisation and calibration."""
    calibration = "no" if report.calibration is None else report.calibration.method.value
    line = (
        f"{report.primary_metric_label} {report.headline_score:.2f} · optimised for "
        f"{report.primary_metric_label} · {calibration} calibration"
    )
    if fairness.evaluated and fairness.column is not None:
        line += f" · fairness by {fairness.column}: reported"
    return line


def _register_detail(config: UseCaseConfig, status: ModelStatus) -> str:
    """The Running line for register (M3 design §7.5), including the honest `kept as candidate`."""
    reasons = f"top {config.evaluation.reasons_per_row} SHAP reasons · " if config.evaluation.shap else ""
    return f"{reasons}{_PROMOTION_WORDS[status]} · drift baseline stored"


# ---------------------------------------------------------------------------
# The score flow
# ---------------------------------------------------------------------------
class _ScoreFlow:
    """One execution of the score flow: the stage bodies, plus the bookkeeping around them.

    The bookkeeping - the status document, the run record, the manifest, and the cancel-and-fail
    handling that brackets every stage - is `_TrainFlow`'s, spelled out again here rather than
    hoisted into a shared base so that the train flow this milestone did not touch keeps executing
    exactly the code it shipped with. The two drivers are worth folding together once the file has
    one owner again.

    What differs is the work, and all of it follows from one fact: **a scoring run fits nothing.**

    * Every choice was made at train time. The transforms come from the training run's
      `prepare.json`, the threshold and the calibrator come from `scorer.json`, and the bands come
      from the configuration. This flow replays them and measures what it produced.
    * The model is resolved once, in the validate stage, because that is the stage that needs its
      schema - and every later stage is handed that same version by id, so a file can never be
      checked against one model's columns and then scored by another.
    * The model is *loaded* once, by `predict`, and the explain stage reuses the scorer predict
      returned rather than opening the stored predictor a second time.
    * The rows are replayed once, by `predict`. See `_prepare` for why that leaves this flow's
      prepare stage with nothing to do but report.
    """

    def __init__(self, pipeline: Pipeline, ctx: StageContext) -> None:
        self._ctx = ctx
        self._storage = ctx.storage
        self._seed = seed_from(ctx.run_id)
        self._started = perf_counter()
        self._status = _StatusWriter(ctx.storage, pipeline.initial_status(ctx.run_id, ctx.mode))
        self._run = _RunWriter(ctx)
        self._index = pipeline.run_index
        self._metrics = pipeline.metrics
        self._manifest = _ManifestBuilder(run_id=ctx.run_id, seed=self._seed, started=self._started)
        self._artefacts: dict[str, str] = dict(self._run.record.artefacts)
        for name in (RUN_FILENAME, STATUS_FILENAME, MANIFEST_FILENAME):
            self._artefacts.setdefault(name, run_key(ctx.run_id, name))
        config_key = run_key(ctx.run_id, register.RUN_CONFIG_FILENAME)
        if ctx.storage.exists(config_key):
            self._artefacts.setdefault(register.RUN_CONFIG_FILENAME, config_key)
        # what each stage hands the next one
        self._frame: pd.DataFrame | None = None
        self._profile: DatasetProfile | None = None
        self._version: ModelVersion | None = None
        self._result: score.PredictResult | None = None
        self._scored: pd.DataFrame | None = None
        self._summary: ScoringSummary | None = None

    # -- the driver ---------------------------------------------------------
    def execute(self) -> RunRecord:
        """Run every stage in order, then write the finished run record.

        The `finally` writes the manifest whatever the outcome, and then releases anything the
        store was mirroring on local disk for this run. On `LocalStorage` the release is a no-op and
        nothing about a local run changes; on a mirrored store it publishes whatever a stage wrote
        through `local_path` and did not itself publish, and reclaims the disk (DEC-311). It runs
        after the manifest because the manifest is written through the store like any other artefact.
        """
        # The context is bound HERE, inside the job body, and not around the `submit` that started
        # it: a `ContextVar` set on the request thread is not copied into a `ThreadPoolExecutor`
        # worker, so a binding made outside would simply not be there when a stage logged (DEC-382).
        with bind_log_context(run_id=self._ctx.run_id):
            record_run_started(self._metrics, use_case_id=self._ctx.config.id)
            try:
                self._run.update(state=RunState.RUNNING, started_at=utc_now())
                for key, body in self._bodies():
                    self._run_stage(key, body)
                return self._complete()
            except JobCancelledError:
                # A cancellation is not a failure. Counting it as one would make an alarm on
                # RunsFailed fire every time somebody changed their mind (DEC-388).
                raise
            except Exception:
                record_run_failed(self._metrics, use_case_id=self._ctx.config.id)
                raise
            finally:
                self._flush_manifest()
                release_local(self._storage, f"runs/{self._ctx.run_id}/")

    def _bodies(self) -> tuple[tuple[StageKey, Callable[[], _StageOutcome]], ...]:
        """The seven stages of plan §6.2, in order, each with the body that runs it."""
        return (
            (StageKey.INGEST, self._ingest),
            (StageKey.VALIDATE_AGAINST_SCHEMA, self._validate_against_schema),
            (StageKey.PREPARE, self._prepare),
            (StageKey.PREDICT, self._predict),
            (StageKey.EXPLAIN_ROWS, self._explain_rows),
            (StageKey.ACTIONS, self._actions),
            (StageKey.EXPORT, self._export),
        )

    def _run_stage(self, key: StageKey, body: Callable[[], _StageOutcome]) -> None:
        """One stage: cancel, run, cancel, record. Both checkpoints bracket the stage body."""
        started = perf_counter()
        try:
            self._ctx.cancel.raise_if_cancelled()
            self._status.start(key)
            with bind_log_context(stage=key.value):
                outcome = body()
            self._ctx.cancel.raise_if_cancelled()
        except JobCancelledError:
            self._stop(key, RunState.CANCELLED, perf_counter() - started, error=None)
            raise
        except Exception as exc:
            error = run_error(exc, stage=key, title=STAGE_TITLES[key])
            if error.code == STAGE_FAILED:
                _LOGGER.exception("stage=%s failed unexpectedly in run %s", key.value, self._ctx.run_id)
            else:
                _LOGGER.warning("stage=%s failed: %s %s", key.value, error.code, error.message)
            self._stop(key, RunState.FAILED, perf_counter() - started, error=error)
            raise
        seconds = perf_counter() - started
        self._status.finish(key, detail=outcome.detail, seconds=seconds)
        self._manifest.record(key, seconds)
        log_stage(_LOGGER, key.value, rows=outcome.rows, seconds=seconds)
        record_stage_duration(self._metrics, stage=key.value, seconds=seconds)

    def _stop(self, key: StageKey, state: RunState, seconds: float, *, error: RunError | None) -> None:
        """Write both documents before the exception leaves the pipeline, so the UI's next poll sees why."""
        self._manifest.record(key, seconds)
        self._status.stop(key, state, seconds=seconds, error=error)
        self._run.update(state=state, finished_at=utc_now(), error=error, artefacts=dict(self._artefacts))

    def _complete(self) -> RunRecord:
        """The finished run record: which model scored the file, and every artefact key.

        `headline_score` stays null. A scoring file carries no target, so nothing about model
        quality was measured here; the number the Results bar shows for a scoring run comes from
        `scoring_summary.json`, where every value was counted rather than estimated (plan §13.3).
        """
        version = _require(self._version, "the model version")
        summary = _require(self._summary, "the scoring summary")
        _LOGGER.info(
            "score: run=%s rows_scored=%d model=%s (v%d) status=%s",
            self._ctx.run_id,
            summary.rows_scored,
            version.model_id,
            version.version,
            version.status.value,
        )
        return self._run.update(
            state=RunState.DONE,
            finished_at=utc_now(),
            row_count=_require(self._profile, "the dataset profile").row_count,
            model_version_id=version.model_id,
            best_model=version.model_display_name,
            champion=version.status is ModelStatus.CHAMPION,
            beat_previous_champion=False,
            artefacts=dict(self._artefacts),
            error=None,
        )

    def _flush_manifest(self) -> None:
        """Write `run_manifest.json` last, for every outcome; a failure here never fails the run."""
        try:
            manifest = self._manifest.build(_file_fingerprint(self._storage, self._ctx.upload_key))
            self._storage.write_model(run_key(self._ctx.run_id, MANIFEST_FILENAME), manifest)
            record_job_cost(
                self._metrics,
                manifest.cost_estimate,
                backend="local" if manifest.compute is None else manifest.compute.backend,
            )
            _LOGGER.info(
                "manifest: run=%s recipe=none fingerprint=%s duration=%.1fs",
                manifest.run_id,
                manifest.dataset_fingerprint.hash[:12],
                manifest.duration_s,
            )
        except Exception:
            _LOGGER.exception("the run manifest could not be written for run %s", self._ctx.run_id)
            manifest = None
        # The index row is written here, last, because this is the one point at which both `run.json`
        # and the manifest are final - so a row never describes a run that is still moving. It is
        # deliberately last in a second sense too: `mirror_run` never raises, because `run.json` is
        # the record and the row is only an index of it. A run that finished must not be reported as
        # failed because a database was briefly unreachable (DEC-342).
        mirror_run(self._index, self._run.record, manifest)

    def _write(self, filename: str, model: BaseModel) -> str:
        """Write one artefact into the run directory and remember its key for `run.json`."""
        key = run_key(self._ctx.run_id, filename)
        self._storage.write_model(key, model)
        self._artefacts[filename] = key
        return key

    # -- the stages ---------------------------------------------------------
    def _ingest(self) -> _StageOutcome:
        """Read the upload - every row of it - and profile it.

        EVERY ROW, NOT A SAMPLE. `profile_row_cap` is a *profiling* budget: in train mode, reading
        a prefix of a very large file costs a little accuracy in `profile.json` and nothing else,
        but in score mode the rows above the cap are customers who would never appear in
        `scores.csv` at all. The first pass counts the file exactly even when it keeps only the
        capped prefix, so a truncated read is repeated at that exact count rather than scored as if
        the rest of the file did not exist.
        """
        ctx = self._ctx
        read = ingest.read_upload(self._storage, ctx.upload_key, row_cap=ingest.profile_row_cap(ctx.config))
        if read.truncated:
            _LOGGER.info(
                "ingest: the profiling cap kept %d of %d rows; re-reading the scoring file in full",
                len(read.frame.index),
                read.row_count,
            )
            read = ingest.read_upload(self._storage, ctx.upload_key, row_cap=read.row_count)
        record = self._run.record
        profile = ingest.profile_dataset(
            read.frame,
            ctx.config,
            upload_id=record.upload_id,
            file_name=record.file_name,
            file_format=read.file_format,
            file_size_bytes=self._storage.size_bytes(ctx.upload_key),
            delimiter=read.delimiter,
            encoding=read.encoding,
            row_count=read.row_count,
            fingerprint=read.fingerprint,
        )
        self._write(PROFILE_FILENAME, profile)
        self._frame = read.frame
        self._profile = profile
        self._manifest.fingerprint = profile.fingerprint
        return _StageOutcome(ingest.ingest_detail(profile), len(read.frame.index))

    def _validate_against_schema(self) -> _StageOutcome:
        """Resolve the model version, then check the file against the schema that model was fitted with.

        THE VERSION IS RESOLVED ONCE, HERE, and every later stage is handed that same version by
        id. This is the stage that first needs it - a schema check needs a schema, and the schema
        belongs to a model - and resolving again later would let a champion promoted mid-run leave
        the file checked against one model's columns and scored by another's.

        The rule is `score.resolve_model_version`, the one `POST /runs` calls before it accepts a
        scoring run (DEC-054), so a version this stage refuses is refused in the same words on the
        request. `SCHEMA_MISMATCH` is reported in business language by the validate stage, naming
        the columns; predict's own feature check is the backstop behind it.
        """
        ctx = self._ctx
        profile = _require(self._profile, "the dataset profile")
        version = score.resolve_model_version(
            ctx.config, registry=ctx.registry, model_version_id=ctx.model_version_id
        )
        self._version = version
        schema = self._storage.read_model(version.schema_key, FeatureSchema)
        report = validate.validate_against_schema(
            _require(self._frame, "the uploaded rows"),
            schema,
            primary_key=ctx.primary_key,
            config=ctx.config,
            acknowledged=ctx.config.validation.acknowledged,
            upload_id=profile.upload_id,
            run_id=ctx.run_id,
            row_count=profile.row_count,
        )
        self._write(VALIDATION_FILENAME, report)
        if not report.passed:
            # `POST /runs` refuses with 409 before a run is created; this is the defensive twin.
            raise engine_error(RUN_BLOCKED_BY_VALIDATION, stage=StageKey.VALIDATE_AGAINST_SCHEMA)
        _LOGGER.info(
            "validate_against_schema: %d columns checked against model %s (v%d)",
            len(schema.columns),
            version.model_id,
            version.version,
        )
        return _StageOutcome(validate.validation_detail(report), profile.row_count)

    def _prepare(self) -> _StageOutcome:
        """Score mode's prepare stage: it reports, and it does nothing else.

        NO SECOND REPLAY. The transforms a scoring file needs are the ones the training run fitted
        and recorded, and `score.predict` reads that record and replays it once, as it loads the
        model that requires it. Replaying here as well would clip to the training percentiles twice
        and fill with the training medians twice - idempotent today only by luck, and silently
        wrong for every customer the first time a transform stops being.

        `prepare.prepare_rows` is not an option either, and not only because of the double replay:
        it *re-derives* which columns to drop from the frame in front of it, so a small scoring
        batch would lose genuine features - the ones that happen to be constant or mostly null in
        this batch - and `predict` would then refuse the very frame this stage had just prepared.

        So the row the Running screen shows for this stage says which model will prepare the rows
        and where its transforms come from. `prepare.json` is written one stage later, by the
        stage that actually replays it, from what `predict` returns.
        """
        return _StageOutcome(_replay_detail(_require(self._version, "the model version")))

    def _predict(self) -> _StageOutcome:
        """Replay the recorded transforms, score the rows, and measure drift against the baseline.

        `predict` returns its artefacts instead of writing them, so this is where they enter the
        run directory. `prepare.json` is the *training* run's report unchanged (DEC-049): it is the
        truth about how these rows were prepared, and it is what the Data page of a scoring run
        renders from. `drift.json` is written only when drift was measured - no baseline, no rows
        or no features means "not measured", and an artefact is not invented to say so (DEC-051).

        The version is passed by id rather than resolved again: see `_validate_against_schema`.
        """
        ctx = self._ctx
        version = _require(self._version, "the model version")
        result = score.predict(
            _require(self._frame, "the uploaded rows"),
            ctx.config,
            run_id=ctx.run_id,
            storage=self._storage,
            registry=ctx.registry,
            model_version_id=version.model_id,
        )
        self._result = result
        self._write(PREPARE_FILENAME, result.prepare_report)
        if result.drift is not None:
            self._write(DRIFT_FILENAME, result.drift)
            self._manifest.add_metrics({"max_psi": result.drift.max_psi}, prefix="drift_")
        scored = result.prepared.copy()
        scored[ctx.config.actions.score_field] = result.scores
        self._scored = scored
        _LOGGER.info(
            "predict: %d rows scored by %s (v%d) drift=%s",
            len(scored.index),
            version.model_id,
            version.version,
            "not measured" if result.drift is None else result.drift.status.value,
        )
        return _StageOutcome(result.detail, len(scored.index))

    def _explain_rows(self) -> _StageOutcome:
        """A reason for every scored row, from the model `predict` has already loaded.

        EVERY ROW, NOT A SAMPLE (`max_rows=None`). Plan §6.3 asks for a reason per scored row, and
        `explain.reason_columns` refuses a partly explained frame rather than writing a
        `scores.csv` whose reason cells are mostly empty. The train flow's sample is the other
        answer to the same question, which is why that budget is a per-call argument and has no
        default on the stage entry point.

        THE MODEL IS LOADED ONCE. `explain.row_reasons` would open the stored predictor a second
        time; `PredictResult.scorer` is the one predict loaded, with the threshold and the
        calibrator it was approved with.

        The reason columns are attached here, by `explain`'s own adapter, which joins them onto the
        scored rows **by primary key** rather than by position - so this stage cannot quietly hand
        export a frame whose reasons belong to other customers.

        `evaluation.shap` is honoured, exactly as the train flow honours it: a configuration that
        turned per-row reasons off gets none, no `row_explanations.parquet`, and empty reason cells
        in `scores.csv` - which is what the export stage documents as "the explain stage did not
        run". That is a user's choice to switch the feature off; it is not the sampling this flow
        refuses, which would leave *most* of a file the user did ask reasons for unexplained.
        """
        ctx = self._ctx
        result = _require(self._result, "the output of the predict stage")
        if not ctx.config.evaluation.shap:
            _LOGGER.info("explain_rows: per-row reasons are switched off for this use case")
            return _StageOutcome(REASONS_OFF_DETAIL, 0)
        reasons = explain.reasons_for(
            result.scorer,
            result.prepared,
            ctx.config,
            primary_key=ctx.primary_key,
            seed=self._seed,
            max_rows=None,
        )
        key = explain.write_row_explanations(reasons.explanations, run_id=ctx.run_id, storage=self._storage)
        self._artefacts[explain.ROW_EXPLANATIONS_FILENAME] = key
        self._scored = explain.with_reason_columns(
            reasons.explanations,
            _require(self._scored, "the scored rows"),
            ctx.config,
            primary_key=ctx.primary_key,
        )
        return _StageOutcome(_reasons_detail(reasons), len(reasons.explanations))

    def _actions(self) -> _StageOutcome:
        """Band, action, suppression reason and control-group flag, seeded by the run id."""
        ctx = self._ctx
        banded = actions.apply_actions(
            _require(self._scored, "the scored rows"),
            ctx.config,
            run_id=ctx.run_id,
            primary_key=ctx.primary_key,
        )
        self._scored = banded
        return _StageOutcome(_actions_detail(banded), len(banded.index))

    def _export(self) -> _StageOutcome:
        """`scores.csv`, `scores.parquet` and `scoring_summary.json`.

        The KPI is totalled from the rows **as they were uploaded**, not from the scored frame: a
        column that is also a model feature has been clipped and filled by the replay, and a
        column prepare dropped is not in the scored frame at all, so a total taken there would be
        a number nobody can reconcile with their source system (plan §13.3). `export.summarise`
        refuses a `sum_where_band_in` KPI rather than guess when those rows are not passed, so
        this argument is not optional in practice.
        """
        ctx = self._ctx
        version = _require(self._version, "the model version")
        frame = _require(self._scored, "the scored rows")
        files = export.write_scores(
            frame, ctx.config, run_id=ctx.run_id, primary_key=ctx.primary_key, storage=self._storage
        )
        self._artefacts.update(files)
        summary = export.summarise(
            frame,
            ctx.config,
            run_id=ctx.run_id,
            model_version_id=version.model_id,
            model_display_name=version.model_display_name,
            primary_key=ctx.primary_key,
            drift=_require(self._result, "the output of the predict stage").drift,
            files=files,
            kpi_source=_require(self._frame, "the uploaded rows"),
        )
        self._summary = summary
        self._write(SCORING_SUMMARY_FILENAME, summary)
        self._manifest.add_metrics({"rows_scored": float(summary.rows_scored)})
        return _StageOutcome(_export_detail(summary), summary.rows_scored)


def _replay_detail(version: ModelVersion) -> str:
    """The Running line for score mode's prepare stage: which model, and whose transforms."""
    return (
        f"{version.model_display_name} (v{version.version}) · "
        f"prepared with the transforms its training run recorded"
    )


def _reasons_detail(reasons: explain.RowReasons) -> str:
    """The Running line for explain_rows; `method` names the tier that really produced them."""
    rows = len(reasons.explanations)
    return f"{reasons.method} reasons for {humanise_count(rows)} {'row' if rows == 1 else 'rows'}"


def _actions_detail(frame: pd.DataFrame) -> str:
    """The Running line for actions. Every number is counted off the banded frame."""
    suppressed = int(frame[actions.SUPPRESSED_REASON_COLUMN].notna().sum())
    control = int(frame[actions.CONTROL_GROUP_COLUMN].astype(bool).sum())
    return (
        f"{humanise_count(len(frame.index))} rows banded · "
        f"{humanise_count(suppressed)} suppressed · {humanise_count(control)} held out as control"
    )


def _export_detail(summary: ScoringSummary) -> str:
    """The Running line for export: what was written, and the configured KPI it adds up to."""
    return (
        f"{humanise_count(summary.rows_scored)} rows written to scores.csv · "
        f"{summary.kpi.label} {summary.kpi.display}"
    )


class Pipeline:
    """Orchestrates the stages of a run and keeps `status.json` current."""

    def __init__(
        self,
        storage: Storage,
        registry: ModelRegistry,
        jobs: JobRunner,
        *,
        run_index: RunIndex | None = None,
        metrics: MetricSink | None = None,
    ) -> None:
        self._storage = storage
        self._registry = registry
        self._jobs = jobs
        self._run_index = run_index
        self._metrics = metrics or NullMetricSink()

    @property
    def storage(self) -> Storage:
        """The artefact store this pipeline reads and writes."""
        return self._storage

    @property
    def registry(self) -> ModelRegistry:
        """The model registry this pipeline registers versions in."""
        return self._registry

    @property
    def jobs(self) -> JobRunner:
        """The runner that executes this pipeline off the request thread."""
        return self._jobs

    @property
    def run_index(self) -> RunIndex | None:
        """The index `GET /runs` lists from, or `None` when this deployment enumerates the store."""
        return self._run_index

    @property
    def metrics(self) -> MetricSink:
        """Where this run's measurements go; the null sink unless a deployment asked otherwise."""
        return self._metrics

    def initial_status(self, run_id: str, mode: RunMode) -> RunStatus:
        """The `status.json` a run starts with: every stage pending, no progress yet."""
        stages = tuple(
            StageStatus(
                key=key,
                title=STAGE_TITLES[key],
                group_label=GROUP_LABELS[(mode, key)],
                state=RunState.PENDING,
            )
            for key in stages_for(mode)
        )
        return RunStatus(
            run_id=run_id,
            mode=mode,
            state=RunState.PENDING,
            updated_at=utc_now(),
            stages=stages,
            current_stage=None,
            progress_pct=0,
        )

    def status(self, run_id: str) -> RunStatus:
        """The run's current `status.json`, straight from the artefact store."""
        return self._storage.read_model(run_key(run_id, STATUS_FILENAME), RunStatus)

    def cancel(self, run_id: str) -> bool:
        """Ask the run to stop; True iff it was still pending or running."""
        return self._jobs.cancel(run_id)

    def run_train(self, ctx: StageContext) -> RunRecord:
        """Run the train flow of plan §6.1 end to end.

        `ingest → validate → prepare → split → train → evaluate → explain → register`, with
        `status.json` rewritten at every transition, cancellation checked either side of every
        stage, and `run_manifest.json` written whatever the outcome. A failed or cancelled stage
        leaves both documents consistent and re-raises, which is what makes the job runner record
        the run as terminal.
        """
        return _TrainFlow(self, ctx).execute()

    def run_score(self, ctx: StageContext) -> RunRecord:
        """Run the score flow of plan §6.2 end to end.

        `ingest → validate_against_schema → prepare → predict → explain_rows → actions → export`,
        with `status.json` rewritten at every transition, cancellation checked either side of every
        stage, and `run_manifest.json` written whatever the outcome - the same contract `run_train`
        keeps. Nothing in this flow fits anything: the model, its transforms, its threshold and its
        calibrator were all settled at train time, and the run replays them and counts what came
        out. See `_ScoreFlow` for why the prepare stage writes nothing of its own.
        """
        return _ScoreFlow(self, ctx).execute()
