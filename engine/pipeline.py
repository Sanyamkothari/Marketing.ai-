"""The train and score pipelines.

M1 ships the stage vocabulary, the Running-screen row mapping and the three methods that need no
AutoML (`initial_status`, `status`, `cancel`); `run_train` and `run_score` land in M3 and M4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from engine.config import ResolvedConfig, RunMode, UseCaseConfig
from engine.contracts import RunRecord, RunState, RunStatus, StageKey, StageStatus
from engine.jobs import CancelToken, JobRunner
from engine.registry import ModelRegistry
from engine.storage import Storage, run_key
from engine.utils.time import utc_now

STATUS_FILENAME: Final[str] = "status.json"

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


class Pipeline:
    """Orchestrates the stages of a run and keeps `status.json` current."""

    def __init__(self, storage: Storage, registry: ModelRegistry, jobs: JobRunner) -> None:
        self._storage = storage
        self._registry = registry
        self._jobs = jobs

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
        """Run the train flow of plan §6.1 end to end."""
        raise NotImplementedError("M3")

    def run_score(self, ctx: StageContext) -> RunRecord:
        """Run the score flow of plan §6.2 end to end."""
        raise NotImplementedError("M4")
