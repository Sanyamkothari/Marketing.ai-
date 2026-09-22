"""Artefact contracts: one pydantic model per file a run writes (plan section 7).

Every model is frozen, forbids unknown keys, uses tuples for collections and timezone-aware
datetimes, and carries a one-line `Field(description=...)` per field - that text is what
`docs/API.md` prints. Floats destined for the UI are rounded by the producing stage,
percentages are stored as percentages (`*_pct`), shares as fractions, and lists arrive
pre-sorted: the UI renders, it never computes (plan section 2.1, principle 2).

This module imports `engine.config` and is never imported by it (one-way, tested).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from engine.config import (
    Calibration,
    ColumnType,
    Metric,
    ModelFamily,
    PrimaryKey,
    ProblemType,
    Recipe,
    ResolvedConfig,
    RunMode,
    SplitType,
    StrictBase,
    ThresholdMode,
    UseCaseConfig,
    key_columns,
    sole_key,
)

__all__ = [
    "ARTEFACT_REGISTRY",
    "MODEL_DIRECTORY",
    "NO_CHAMPION_AT_DECISION",
    "SCORE_ARTEFACTS",
    "TABULAR_SCHEMAS",
    "TRAIN_ARTEFACTS",
    "VALIDATION_CODES",
    "ActionCount",
    "Artefact",
    "BandCount",
    "BaselineComparison",
    "BaselineMetric",
    "BestModel",
    "CalibrationSummary",
    "CategoryCount",
    "ColumnProfile",
    "ComputeBackend",
    "ComputeInfo",
    "ConfusionMatrix",
    "CostEstimate",
    "DatasetFingerprint",
    "DatasetProfile",
    "DecileBin",
    "DecileLift",
    "Direction",
    "DriftBaseline",
    "DriftReport",
    "DriftStatus",
    "DroppedColumn",
    "EvaluationReport",
    "FairnessGroup",
    "FairnessReport",
    "FeatureBaseline",
    "FeatureDrift",
    "FeatureImportance",
    "FeatureImportanceItem",
    "FeatureSchema",
    "FeatureSchemaColumn",
    "HistogramBin",
    "KpiValue",
    "LLMUsage",
    "Leaderboard",
    "LeaderboardEntry",
    "MetricValue",
    "ModelStatus",
    "ModelVersion",
    "PrepareReport",
    "PrimaryKey",
    "Reason",
    "RowExplanation",
    "RowRemoval",
    "RunError",
    "RunManifest",
    "RunRecord",
    "RunState",
    "RunStatus",
    "ScoreRow",
    "ScoringSummary",
    "Severity",
    "SplitPart",
    "SplitReport",
    "StageKey",
    "StageStatus",
    "SuppressionCount",
    "Transform",
    "ValidationCheck",
    "ValidationReport",
    "artefact_model",
    "dump_artefact",
    "key_columns",
    "load_artefact",
    "scores_csv_columns",
    "sole_key",
]


# ---------------------------------------------------------------------------
# Base and enums
# ---------------------------------------------------------------------------
class Artefact(StrictBase):
    """Base of every artefact model: frozen, `extra="forbid"`, versioned."""

    schema_version: int = Field(default=1, description="Version of the contract the file was written with.")


class RunState(StrEnum):
    """One state vocabulary for runs, stages and jobs (DEC-027). `skipped` is stage-only."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Severity(StrEnum):
    """Severity of a validation check: `error` blocks the run, the rest are shown only."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ModelStatus(StrEnum):
    """Lifecycle of a registered model version (plan section 6.3, register)."""

    CANDIDATE = "candidate"
    PENDING_APPROVAL = "pending_approval"
    CHAMPION = "champion"
    ARCHIVED = "archived"


class DriftStatus(StrEnum):
    """Drift verdict for one feature or a whole scoring run."""

    STABLE = "stable"
    WATCH = "watch"
    DRIFTED = "drifted"


class Direction(StrEnum):
    """Direction of a per-row reason's contribution.

    `NONE` is the direction of a reason that was not measured on this row at all - a general reason,
    carried over from the importance chart because every tier measured this row's contributions as
    zero. It pushed the score neither way, and claiming an arrow would be invention (DEC-056).
    """

    UP = "up"
    DOWN = "down"
    NONE = "none"


class ComputeBackend(StrEnum):
    """What carried a run: this process, or a managed SageMaker job (Phase 4a)."""

    LOCAL = "local"
    SAGEMAKER = "sagemaker"


class ReasonMethod(StrEnum):
    """How one row's reasons were produced.

    The first three are the per-row tiers of plan section 6.3, in the order they are tried.
    `GENERAL` is the floor under them (DEC-056): a row every tier measured as all-zero keeps a
    reason, drawn from the run's global feature importance and labelled as general, rather than
    reaching `scores.csv` with empty cells.
    """

    TREE_SHAP = "TreeSHAP"
    KERNEL_SHAP = "KernelSHAP"
    PERMUTATION = "permutation"
    GENERAL = "general"


class StageKey(StrEnum):
    """Union of the train and score stage sequences (plan sections 6.1 and 6.2, verbatim)."""

    INGEST = "ingest"
    VALIDATE = "validate"
    PREPARE = "prepare"
    SPLIT = "split"
    TRAIN = "train"
    EVALUATE = "evaluate"
    EXPLAIN = "explain"
    REGISTER = "register"
    VALIDATE_AGAINST_SCHEMA = "validate_against_schema"
    PREDICT = "predict"
    EXPLAIN_ROWS = "explain_rows"
    ACTIONS = "actions"
    EXPORT = "export"


# ---------------------------------------------------------------------------
# 5.1 run.json
# ---------------------------------------------------------------------------
class RunError(Artefact):
    """The failure attached to a failed run or stage."""

    code: str = Field(description="Machine-readable failure code, from the validation table or the engine.")
    message: str = Field(description="Business-language explanation of what went wrong.")
    stage: StageKey | None = Field(description="Stage that failed, when one was running.")


class RunRecord(Artefact):
    """`run.json` - the Results summary bar, the three pipeline blocks and Previous runs."""

    run_id: str = Field(description="Sortable run id, also the run directory name and the seed source.")
    use_case_id: str = Field(description="Id of the use case that was run.")
    use_case_name: str = Field(description="Display name, so run history renders without loading configs.")
    mode: RunMode = Field(description="Whether this run trained a model or scored new data.")
    state: RunState = Field(description="Overall run state: pending, running, done, failed or cancelled.")
    created_at: AwareDatetime = Field(description="UTC time the run record was created.")
    started_at: AwareDatetime | None = Field(default=None, description="UTC time the first stage started.")
    finished_at: AwareDatetime | None = Field(
        default=None, description="UTC time the run reached a final state."
    )
    upload_id: str = Field(description="Id of the upload this run consumed.")
    file_name: str = Field(
        description="The user's original file name, shown in the Results bar and run history."
    )
    row_count: int | None = Field(
        default=None, description="Rows in the upload, taken from the dataset profile."
    )
    primary_key: PrimaryKey = Field(description="Column, or columns, identifying each entity.")
    target: str | None = Field(default=None, description="Target column; set for training runs only.")
    problem_type: ProblemType = Field(
        description="Problem type resolved for this run, detected or overridden."
    )
    model_choice: str = Field(
        description="Step-3 choice: the AutoML sentinel or a single model family value."
    )
    model_version_id: str | None = Field(
        default=None, description="Model version produced (train) or used (score)."
    )
    best_model: str | None = Field(default=None, description="Display name of the winning or scoring model.")
    headline_metric: Metric | None = Field(default=None, description="Metric behind the headline score.")
    headline_metric_label: str | None = Field(
        default=None, description="Catalog label of the headline metric."
    )
    headline_score: float | None = Field(
        default=None, description="Test score of the headline metric, rounded."
    )
    champion: bool = Field(default=False, description="Whether this run's model is, or became, the champion.")
    beat_previous_champion: bool = Field(
        default=False, description="Whether this run's model beat the previous champion."
    )
    overrides: dict[str, Any] = Field(
        default_factory=dict, description="Expanded overrides the user sent, in config units."
    )
    artefacts: dict[str, str] = Field(
        default_factory=dict, description="Artefact filename mapped to its storage key."
    )
    error: RunError | None = Field(default=None, description="Failure detail; set when the state is failed.")
    engine_version: str = Field(description="Version of the engine package that produced the run.")


# ---------------------------------------------------------------------------
# 5.2 status.json
# ---------------------------------------------------------------------------
class StageStatus(Artefact):
    """One engine stage on the Running screen (the UI groups rows by `group_label`; DEC-020)."""

    key: StageKey = Field(description="Engine stage id (plan sections 6.1 and 6.2).")
    title: str = Field(description="Engine label for logs and docs; the UI never shows it.")
    group_label: str = Field(description="Prototype Running-screen line this stage belongs to.")
    state: RunState = Field(description="Stage state: pending, running, done, failed, cancelled or skipped.")
    detail: str = Field(default="", description="Second line under the row, pre-formatted by the stage.")
    started_at: AwareDatetime | None = Field(default=None, description="UTC time this stage started.")
    ended_at: AwareDatetime | None = Field(default=None, description="UTC time this stage ended.")
    duration_seconds: float | None = Field(default=None, description="Wall-clock duration of the stage.")
    error: RunError | None = Field(default=None, description="Failure detail when this stage failed.")


class RunStatus(Artefact):
    """`status.json` - everything the Running screen polls."""

    run_id: str = Field(description="Run this status belongs to.")
    mode: RunMode = Field(description="Whether the run is training or scoring.")
    state: RunState = Field(description="Overall run state, mirroring the run record.")
    updated_at: AwareDatetime = Field(description="UTC time this status file was last written.")
    stages: tuple[StageStatus, ...] = Field(
        description="The full stage list in execution order, eight for train and seven for score."
    )
    current_stage: StageKey | None = Field(default=None, description="Stage currently running, when any.")
    progress_pct: int = Field(description="Pre-computed completion percentage, done stages over total.")


# ---------------------------------------------------------------------------
# 5.3 profile.json
# ---------------------------------------------------------------------------
class CategoryCount(Artefact):
    """One category of a categorical column and how often it occurs."""

    value: str = Field(description="The category value, stringified.")
    count: int = Field(description="Number of rows holding this value.")
    share: float = Field(description="Share of non-null rows holding this value, 0 to 1.")


class ColumnProfile(Artefact):
    """Everything ingest learned about one column of the uploaded file."""

    name: str = Field(description="Column name exactly as it appears in the file header.")
    position: int = Field(description="Zero-based position of the column in the file.")
    dtype: str = Field(description="Pandas or Arrow dtype as read, for example int64.")
    inferred_type: ColumnType = Field(description="Engine-level type inferred for this column.")
    null_count: int = Field(description="Number of null or empty values.")
    null_rate: float = Field(description="Share of rows that are null, 0 to 1.")
    distinct_count: int = Field(description="Number of distinct non-null values.")
    is_unique: bool = Field(description="Whether every non-null value is distinct.")
    is_constant: bool = Field(description="Whether the column holds a single distinct value.")
    sample_values: tuple[str, ...] = Field(
        description="Up to five stringified example values; never written to the log."
    )
    minimum: float | None = Field(default=None, description="Smallest value; numeric columns only.")
    maximum: float | None = Field(default=None, description="Largest value; numeric columns only.")
    mean: float | None = Field(default=None, description="Arithmetic mean; numeric columns only.")
    top_categories: tuple[CategoryCount, ...] = Field(
        default=(), description="Ten most frequent values; categorical columns only."
    )
    looks_like_id: bool = Field(
        description="Distinct count is close to the row count and this is not the primary key."
    )
    looks_like_time: bool = Field(description="Name matches the time-like pattern or values parse as dates.")
    pii_kinds: tuple[str, ...] = Field(
        default=(), description="Names of the PII detectors that matched; never the matched values."
    )


class DatasetFingerprint(Artefact):
    """Identifies the exact dataset a run consumed (DEC-042).

    Saved at ingest and carried on every run, so a later reader can tell whether
    two runs saw the same data. Without it, comparing two runs' metrics is
    guesswork: a score only means something next to the data that produced it.
    """

    hash: str = Field(description="Hex digest of the dataset content and shape.")
    algorithm: str = Field(description="Digest algorithm, so the scheme can change without ambiguity.")
    n_rows: int = Field(description="Number of data rows covered by the digest.")
    columns: tuple[str, ...] = Field(description="Column names, in file order, covered by the digest.")


class DatasetProfile(Artefact):
    """`profile.json` - the Setup preview and the Data page."""

    upload_id: str = Field(description="Id of the upload that was profiled.")
    file_name: str = Field(description="The user's original file name.")
    file_size_bytes: int = Field(description="Size of the uploaded file in bytes.")
    file_format: Literal["csv", "parquet"] = Field(description="Format the file was read as.")
    delimiter: str | None = Field(description="Delimiter sniffed for CSV; null for Parquet.")
    encoding: str = Field(description="Character encoding the file was decoded with.")
    row_count: int = Field(description="Number of data rows in the file.")
    column_count: int = Field(description="Number of columns in the file.")
    columns: tuple[ColumnProfile, ...] = Field(description="One profile per column, in file order.")
    primary_key_candidates: tuple[str, ...] = Field(
        description="Unique, non-null columns, hint-name matches first."
    )
    time_column_candidates: tuple[str, ...] = Field(
        description="Columns that look like time columns and parse as dates."
    )
    target_candidate: str | None = Field(
        description="Column matching the use case's configured target column."
    )
    preview_rows: tuple[tuple[str, ...], ...] = Field(
        description="First five rows, stringified, for the Setup preview table."
    )
    missing_value_rate_pct: float = Field(
        description="Share of missing cells over the whole table, as a percentage."
    )
    fingerprint: DatasetFingerprint = Field(
        description="Identity of this exact dataset, for comparing runs against each other."
    )
    profiled_at: AwareDatetime = Field(description="UTC time the file was profiled.")


# ---------------------------------------------------------------------------
# 5.4 validation.json
# ---------------------------------------------------------------------------
VALIDATION_CODES: Final[frozenset[str]] = frozenset(
    {
        "PK_MISSING",
        "PK_NOT_UNIQUE",
        "PK_NULLS",
        "TARGET_MISSING",
        "TARGET_NOT_BINARY",
        "TARGET_CONSTANT",
        "TARGET_TOO_FEW_POSITIVES",
        "TARGET_IMBALANCE_SEVERE",
        "ROWS_TOO_FEW",
        "LEAKAGE_SUSPECTED",
        "TIME_COLUMN_MISSING",
        "TIME_COLUMN_UNPARSEABLE",
        "HIGH_NULL_COLUMN",
        "CONSTANT_COLUMN",
        "HIGH_CARDINALITY_ID_LIKE",
        "PII_DETECTED",
        "SCHEMA_MISMATCH",
        "CONSENT_COLUMN_MISSING",
        "SUPPRESSION_COLUMN_MISSING",
    }
)
"""The plan section 6.3 validation codes plus `SUPPRESSION_COLUMN_MISSING` (warning; DEC-030)."""


class ValidationCheck(Artefact):
    """One row of the validation table, already interpolated for the user."""

    code: str = Field(description="Validation table code, for example PK_NOT_UNIQUE.")
    severity: Severity = Field(description="Whether this check blocks the run or is only reported.")
    message: str = Field(description="Business-language message, with the numbers already filled in.")
    suggestion: str = Field(default="", description="What the user can do about it.")
    column: str | None = Field(default=None, description="Column the check is about, when it is about one.")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Machine-readable numbers behind the message."
    )
    acknowledgeable: bool = Field(
        default=False, description="Whether the UI may offer to acknowledge this check."
    )
    acknowledged: bool = Field(
        default=False, description="Whether the user acknowledged it through the run overrides."
    )

    @model_validator(mode="after")
    def _known_code(self) -> ValidationCheck:
        if self.code not in VALIDATION_CODES:
            raise ValueError(f"unknown validation code {self.code!r}; known codes: {_known_codes()}")
        return self


def _known_codes() -> str:
    return ", ".join(sorted(VALIDATION_CODES))


class ValidationReport(Artefact):
    """`validation.json` - every check that ran, errors first."""

    run_id: str | None = Field(default=None, description="Run id, or null when an upload is validated alone.")
    upload_id: str = Field(description="Upload the checks ran against.")
    mode: RunMode = Field(description="Whether the checks are the train or the score set.")
    checks: tuple[ValidationCheck, ...] = Field(
        description="All checks: errors, then warnings, then info, in validation-table order."
    )
    error_count: int = Field(description="Number of blocking errors, excluding acknowledged ones.")
    warning_count: int = Field(description="Number of warnings.")
    passed: bool = Field(description="True when no blocking error remains, so a run may start.")
    validated_at: AwareDatetime = Field(description="UTC time the checks were run.")


# ---------------------------------------------------------------------------
# 5.5 prepare.json
# ---------------------------------------------------------------------------
class DroppedColumn(Artefact):
    """One column removed before training, and why."""

    name: str = Field(description="Name of the dropped column.")
    reason: Literal["user_excluded", "constant", "high_null", "id_like", "pii", "leakage"] = Field(
        description="Why the column was dropped."
    )
    detail: str = Field(default="", description="One line of extra context, for example the null rate.")


class RowRemoval(Artefact):
    """Rows removed by one prepare rule."""

    reason: Literal["duplicate", "consent_false", "missing_target", "outlier", "missing_values"] = Field(
        description="Why these rows were removed."
    )
    rows: int = Field(description="Number of rows removed for this reason.")


class Transform(Artefact):
    """One transform fitted on the training data and replayed at score time."""

    order: int = Field(description="Position of this transform in the replay order.")
    kind: Literal[
        "fill_median", "fill_mode", "clip_percentile", "redact", "cast", "dedupe", "consent_filter"
    ] = Field(description="Kind of transform applied.")
    columns: tuple[str, ...] = Field(description="Columns the transform was applied to.")
    parameters: dict[str, float | str | bool] = Field(
        default_factory=dict, description="Fitted parameters, so scoring replays the transform identically."
    )


class PrepareReport(Artefact):
    """`prepare.json` - what preparation changed, so scoring can repeat it."""

    run_id: str = Field(description="Run this report belongs to.")
    rows_in: int = Field(description="Rows read into the prepare stage.")
    rows_out: int = Field(description="Rows left after preparation.")
    columns_in: int = Field(description="Columns read into the prepare stage.")
    columns_out: int = Field(description="Columns left after preparation.")
    feature_columns: tuple[str, ...] = Field(description="Exact ordered feature list handed to training.")
    dropped_columns: tuple[DroppedColumn, ...] = Field(description="Columns dropped, with reasons.")
    row_removals: tuple[RowRemoval, ...] = Field(description="Row removals grouped by reason.")
    transforms: tuple[Transform, ...] = Field(description="Transforms in replay order.")
    pii_columns: tuple[str, ...] = Field(default=(), description="Columns the PII detectors matched.")
    consent_column: str | None = Field(default=None, description="Consent column applied, when configured.")
    consent_rows_removed: int = Field(default=0, description="Rows removed because consent was not given.")
    detail: str = Field(description="Pre-formatted Running-screen line for the preparation step.")
    prepared_at: AwareDatetime = Field(description="UTC time preparation finished.")


# ---------------------------------------------------------------------------
# 5.6 split.json
# ---------------------------------------------------------------------------
class SplitPart(Artefact):
    """One part of the split and its shape."""

    name: Literal["train", "validation", "test"] = Field(description="Which part of the split this is.")
    rows: int = Field(description="Rows in this part.")
    share: float = Field(description="Share of the prepared rows in this part, 0 to 1.")
    positive_rows: int | None = Field(default=None, description="Positive rows; classification only.")
    positive_rate: float | None = Field(
        default=None, description="Positive rate 0 to 1; classification only."
    )
    start_date: AwareDatetime | None = Field(default=None, description="Earliest timestamp; time-based only.")
    end_date: AwareDatetime | None = Field(default=None, description="Latest timestamp; time-based only.")


class SplitReport(Artefact):
    """`split.json` - how the data was divided into train, validation and test."""

    run_id: str = Field(description="Run this report belongs to.")
    type: SplitType = Field(description="Split strategy that was used.")
    time_column: str | None = Field(description="Column ordering a time-based split.")
    group_column: str | None = Field(description="Column whose groups are kept in one part.")
    parts: tuple[SplitPart, ...] = Field(description="Train, validation and test, in that order.")
    validation_fraction: float = Field(description="Configured validation share, as a fraction.")
    test_fraction: float = Field(description="Configured test share, as a fraction.")
    train_cutoff: AwareDatetime | None = Field(
        default=None, description="Last training timestamp; time-based only."
    )
    test_cutoff: AwareDatetime | None = Field(
        default=None, description="First test timestamp; time-based only."
    )
    seed: int = Field(description="Random seed derived from the run id, so the split is reproducible.")
    detail: str = Field(description="Pre-formatted Running-screen line for the split step.")
    split_at: AwareDatetime = Field(description="UTC time the split was made.")


# ---------------------------------------------------------------------------
# 5.7 leaderboard.json, best_model.json
# ---------------------------------------------------------------------------
class LeaderboardEntry(Artefact):
    """One trained model on the leaderboard."""

    rank: int = Field(description="Rank on the validation score, one is best.")
    model_name: str = Field(description="AutoGluon model name, for example LightGBM_BAG_L1.")
    family: ModelFamily | None = Field(description="Engine family; null for the ensemble.")
    family_label: str | None = Field(description="Catalog label of the family, for the Model page.")
    validation_score: float = Field(description="Score on the validation split, in the chosen metric.")
    test_score: float | None = Field(description="Score on the test split, when computed.")
    fit_time_seconds: float = Field(description="Seconds spent fitting this model.")
    predict_time_seconds: float = Field(description="Seconds spent predicting the validation split.")
    is_ensemble: bool = Field(description="Whether this entry is a weighted ensemble of other models.")
    stack_level: int = Field(description="Stacking level the model was trained at.")


class Leaderboard(Artefact):
    """`leaderboard.json` - every model AutoGluon trained, best first."""

    run_id: str = Field(description="Run this leaderboard belongs to.")
    metric: Metric = Field(description="Metric every score is measured in.")
    metric_label: str = Field(description="Catalog label of the metric.")
    greater_is_better: bool = Field(description="Whether a higher score is a better score.")
    entries: tuple[LeaderboardEntry, ...] = Field(description="Leaderboard rows, sorted by rank.")
    best_model_name: str = Field(description="AutoGluon name of the winning model.")
    models_trained: int = Field(description="How many models were trained.")
    time_limit_seconds: int = Field(description="Training time limit that applied to the search.")
    presets: str = Field(description="AutoGluon presets the search strategy mapped to.")


class BestModel(Artefact):
    """`best_model.json` - the winning model, ready for the Results summary bar."""

    run_id: str = Field(description="Run this model belongs to.")
    model_name: str = Field(description="AutoGluon name of the winning model.")
    family: ModelFamily | None = Field(description="Engine family; null for the ensemble.")
    is_ensemble: bool = Field(description="Whether the winner is an ensemble.")
    ensemble_members: tuple[str, ...] = Field(default=(), description="Member model names of the ensemble.")
    display_name: str = Field(
        description="Name shown to the user, for example Ensemble (XGBoost + LightGBM)."
    )
    metric: Metric = Field(description="Metric the scores below are measured in.")
    metric_label: str = Field(description="Catalog label of the metric.")
    validation_score: float = Field(description="Score on the validation split.")
    test_score: float = Field(description="Score on the test split, the headline number.")
    hyperparameters: dict[str, Any] = Field(
        default_factory=dict, description="Hyperparameters as reported by AutoGluon."
    )
    hyperparameters_summary: str = Field(description="One-line hyperparameter summary for the Model page.")
    training_rows: int = Field(description="Rows the model was fitted on.")
    feature_count: int = Field(description="Number of features the model was fitted on.")
    fit_time_seconds: float = Field(description="Seconds spent fitting the winning model.")
    predictor_key: str = Field(description="Storage key of the saved AutoGluon predictor directory.")
    trained_at: AwareDatetime = Field(description="UTC time training finished.")


# ---------------------------------------------------------------------------
# 5.8 evaluation.json and friends
# ---------------------------------------------------------------------------
class MetricValue(Artefact):
    """One evaluated metric, ready to render."""

    id: Metric = Field(description="Metric id from the catalog.")
    label: str = Field(description="Catalog label of the metric.")
    value: float = Field(description="Value on the test split, rounded.")
    greater_is_better: bool = Field(description="Whether a higher value is a better value.")


class CalibrationSummary(Artefact):
    """How probabilities were calibrated and what it bought."""

    method: Calibration = Field(description="Calibration method applied.")
    brier_before: float | None = Field(description="Brier score before calibration.")
    brier_after: float | None = Field(description="Brier score after calibration.")
    fitted_on: Literal["validation"] = Field(
        default="validation", description="Split the calibrator was fitted on."
    )


class EvaluationReport(Artefact):
    """`evaluation.json` - the test-split verdict (plan section 6.3, evaluate)."""

    run_id: str = Field(description="Run this evaluation belongs to.")
    problem_type: ProblemType = Field(description="Problem type the metrics belong to.")
    rows_evaluated: int = Field(description="Rows in the test split.")
    positive_rate: float | None = Field(description="Positive rate of the test split, 0 to 1.")
    primary_metric: Metric = Field(description="Metric the model was optimised for.")
    primary_metric_label: str = Field(description="Catalog label of the primary metric.")
    headline_score: float = Field(description="Primary metric on the test split, the headline number.")
    metrics: tuple[MetricValue, ...] = Field(
        description="Every catalog metric valid for this problem type, in catalog order."
    )
    extra_metrics: dict[str, float] = Field(
        default_factory=dict, description="Named scores outside the metric enum, such as r2 or accuracy."
    )
    threshold: float = Field(description="Decision threshold the classification metrics were computed at.")
    threshold_mode: ThresholdMode = Field(description="How the threshold was chosen.")
    threshold_detail: str = Field(description="One line explaining the threshold, for the Model page.")
    calibration: CalibrationSummary | None = Field(
        description="Calibration summary; null when calibration was off."
    )
    evaluated_at: AwareDatetime = Field(description="UTC time evaluation finished.")


class ConfusionMatrix(Artefact):
    """`confusion_matrix.json` - the four-cell grid the Model page draws."""

    run_id: str = Field(description="Run this matrix belongs to.")
    threshold: float = Field(description="Decision threshold the matrix was computed at.")
    positive_label: str = Field(description="Label shown for the positive class.")
    negative_label: str = Field(description="Label shown for the negative class.")
    true_positive: int = Field(description="Positives predicted positive.")
    false_negative: int = Field(description="Positives predicted negative.")
    false_positive: int = Field(description="Negatives predicted positive.")
    true_negative: int = Field(description="Negatives predicted negative.")
    total: int = Field(description="Rows in the matrix.")
    cells: tuple[int, int, int, int] = Field(
        description="The grid in render order: true positive, false negative, false positive, true negative."
    )
    precision: float = Field(description="Precision at this threshold.")
    recall: float = Field(description="Recall at this threshold.")
    specificity: float = Field(description="Specificity at this threshold.")
    f1: float = Field(description="F1 score at this threshold.")

    @model_validator(mode="before")
    @classmethod
    def _default_cells(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "cells" not in data:
            keys = ("true_positive", "false_negative", "false_positive", "true_negative")
            if all(key in data for key in keys):
                filled = dict(data)
                filled["cells"] = tuple(data[key] for key in keys)
                return filled
        return data

    @model_validator(mode="after")
    def _cells_match_counts(self) -> ConfusionMatrix:
        expected = (self.true_positive, self.false_negative, self.false_positive, self.true_negative)
        if self.cells != expected:
            raise ValueError(f"cells must be (tp, fn, fp, tn) = {expected}, got {self.cells}")
        return self


class DecileBin(Artefact):
    """One decile of the score distribution."""

    decile: int = Field(description="Decile number, one holds the highest scores.")
    label: str = Field(description="Label of the decile, D1 to D10.")
    rows: int = Field(description="Rows in this decile.")
    score_min: float = Field(description="Lowest score in this decile.")
    score_max: float = Field(description="Highest score in this decile.")
    mean_score: float = Field(description="Mean score in this decile.")
    positives: int | None = Field(default=None, description="Actual positives; classification only.")
    actual_rate_pct: float | None = Field(
        default=None, description="Actual positive rate as a percentage; classification only."
    )
    actual_mean: float | None = Field(default=None, description="Mean actual value; regression only.")
    predicted_mean: float | None = Field(default=None, description="Mean predicted value; regression only.")
    lift: float | None = Field(default=None, description="Lift of this decile over the base rate.")
    cumulative_lift: float | None = Field(default=None, description="Lift of this decile and the ones above.")
    cumulative_capture_pct: float | None = Field(
        default=None, description="Share of all positives captured down to this decile, as a percentage."
    )


class DecileLift(Artefact):
    """`decile_lift.json` - the Output page's decile chart, pre-computed."""

    run_id: str = Field(description="Run this table belongs to.")
    mode: Literal["classification", "regression"] = Field(
        description="Which family of numbers the bins hold."
    )
    base_rate_pct: float | None = Field(
        description="Overall positive rate as a percentage; classification only."
    )
    bins: tuple[DecileBin, ...] = Field(description="Exactly ten bins, highest-scoring decile first.")
    values: tuple[float, ...] = Field(
        description="The ten numbers the bar chart draws, highest decile first."
    )
    unit: Literal["x", "%"] = Field(description="Unit the drawn values are in.")
    caption: str = Field(description="Caption under the chart.")
    computed_at: AwareDatetime = Field(description="UTC time the table was computed.")


class BaselineMetric(Artefact):
    """One line of the model-versus-baseline table."""

    id: Metric = Field(description="Metric id from the catalog.")
    label: str = Field(description="Catalog label of the metric.")
    model_value: float = Field(description="The trained model's value.")
    baseline_value: float | None = Field(description="The baseline model's value.")
    delta: float | None = Field(description="Model value minus baseline value.")
    model_better: bool | None = Field(description="Whether the model beats the baseline here.")


class BaselineComparison(Artefact):
    """`baseline.json` - the separately trained baseline the Model page compares against."""

    run_id: str = Field(description="Run this comparison belongs to.")
    baseline_name: str = Field(description="Display name of the baseline model.")
    baseline_family: ModelFamily = Field(description="Engine family the baseline was trained with.")
    rows: tuple[BaselineMetric, ...] = Field(description="One row per line of the comparison table.")
    model_beats_baseline: bool = Field(description="Whether the model wins on the primary metric.")


class FairnessGroup(Artefact):
    """One group of the sensitive column."""

    value: str = Field(description="Value of the sensitive column identifying this group.")
    rows: int = Field(description="Rows in this group.")
    positive_rate: float = Field(description="Predicted positive rate in this group, 0 to 1.")
    recall: float | None = Field(description="Recall within this group.")
    precision: float | None = Field(description="Precision within this group.")


class FairnessReport(Artefact):
    """`fairness.json` - reported only, never blocking."""

    run_id: str = Field(description="Run this report belongs to.")
    column: str | None = Field(description="Sensitive column, when one was configured.")
    evaluated: bool = Field(description="Whether the fairness check actually ran.")
    reason_not_evaluated: str | None = Field(description="Why the check did not run.")
    groups: tuple[FairnessGroup, ...] = Field(description="Groups, largest first.")
    max_positive_rate_gap: float | None = Field(description="Largest positive-rate gap.")
    max_recall_gap: float | None = Field(description="Largest recall gap.")
    max_precision_gap: float | None = Field(description="Largest precision gap.")
    note: str = Field(
        default="Reported only; fairness never blocks a run.",
        description="Standing note shown with the table.",
    )


# ---------------------------------------------------------------------------
# 5.9 feature_importance.json, row_explanations.parquet
# ---------------------------------------------------------------------------
class FeatureImportanceItem(Artefact):
    """One feature on the global importance chart."""

    rank: int = Field(description="Rank by importance, one is most important.")
    feature: str = Field(description="Feature name.")
    importance: float = Field(description="Raw importance as reported by the method.")
    share_pct: float = Field(description="Importance as a percentage, normalised over the returned rows.")
    stddev: float | None = Field(default=None, description="Standard deviation of the importance estimate.")
    p_value: float | None = Field(default=None, description="P-value of the permutation importance estimate.")


class FeatureImportance(Artefact):
    """`feature_importance.json` - the global explanation of the model."""

    run_id: str = Field(description="Run this explanation belongs to.")
    method: Literal["permutation", "shap"] = Field(description="How importance was computed.")
    computed_on: Literal["test"] = Field(default="test", description="Split importance was computed on.")
    top_n: int = Field(description="How many features were kept, at most twenty.")
    items: tuple[FeatureImportanceItem, ...] = Field(description="Features, most important first.")
    caption: str = Field(description="Caption under the chart, naming the method.")


class Reason(Artefact):
    """One per-row reason behind a score."""

    feature: str = Field(description="Feature the reason is about.")
    value: str = Field(description="The row's value for that feature, stringified.")
    contribution: float = Field(description="Signed contribution of this feature to the score.")
    direction: Direction = Field(description="Whether the feature pushed the score up or down.")
    text: str = Field(description="Ready-to-render sentence for this reason.")


class RowExplanation(Artefact):
    """Row schema of `row_explanations.parquet` - the top reasons per scored row."""

    primary_key: str = Field(description="Primary-key value of the explained row.")
    score: float = Field(description="Score the reasons explain.")
    reasons: tuple[Reason, ...] = Field(description="Top reasons, strongest contribution first.")
    method: ReasonMethod = Field(
        default=ReasonMethod.PERMUTATION,
        description=(
            "Tier that produced this row's reasons. A value other than the run's own "
            "RowReasons.method means this row needed a fallback, which is what "
            "ScoringSummary.rows_with_fallback_reasons counts."
        ),
    )


# ---------------------------------------------------------------------------
# 5.10 drift_baseline.json, drift.json
# ---------------------------------------------------------------------------
class HistogramBin(Artefact):
    """One bin of a numeric feature's training distribution."""

    lower: float = Field(description="Lower edge of the bin.")
    upper: float = Field(description="Upper edge of the bin.")
    count: int = Field(description="Training rows in this bin.")
    share: float = Field(description="Share of training rows in this bin, 0 to 1.")


class FeatureBaseline(Artefact):
    """The training distribution of one feature, kept for drift comparison."""

    feature: str = Field(description="Feature name.")
    kind: Literal["numeric", "categorical"] = Field(description="Which kind of summary is stored.")
    null_rate: float = Field(description="Share of nulls at training time, 0 to 1.")
    bins: tuple[HistogramBin, ...] = Field(default=(), description="Histogram bins; numeric features only.")
    categories: tuple[CategoryCount, ...] = Field(
        default=(), description="Frequency table; categorical features only."
    )
    mean: float | None = Field(default=None, description="Training mean; numeric features only.")
    std: float | None = Field(default=None, description="Training standard deviation; numeric features only.")


class DriftBaseline(Artefact):
    """`drift_baseline.json` - the training distributions scoring runs compare against."""

    run_id: str = Field(description="Training run that produced this baseline.")
    model_version_id: str = Field(description="Model version this baseline was stored with.")
    rows: int = Field(description="Training rows the baseline was computed from.")
    bin_count: int = Field(description="Number of histogram bins used for numeric features.")
    features: tuple[FeatureBaseline, ...] = Field(description="One entry per feature, in fit order.")
    created_at: AwareDatetime = Field(description="UTC time the baseline was stored.")


class FeatureDrift(Artefact):
    """Drift of one feature between the training data and the scored file."""

    feature: str = Field(description="Feature name.")
    psi: float = Field(description="Population stability index against the training baseline.")
    status: DriftStatus = Field(description="Verdict for this feature.")
    null_rate_baseline: float = Field(description="Null share at training time, 0 to 1.")
    null_rate_current: float = Field(description="Null share in the scored file, 0 to 1.")


class DriftReport(Artefact):
    """`drift.json` - how far the scored file has moved from the training data."""

    run_id: str = Field(description="Scoring run this report belongs to.")
    baseline_run_id: str = Field(description="Training run whose baseline was used.")
    model_version_id: str = Field(description="Model version that was scored with.")
    threshold: float = Field(description="Configured drift threshold; half of it is the watch level.")
    features: tuple[FeatureDrift, ...] = Field(description="Features, most drifted first.")
    max_psi: float = Field(description="Largest population stability index over all features.")
    drifted_features: tuple[str, ...] = Field(description="Features at or above the threshold.")
    status: DriftStatus = Field(description="Overall verdict for the scoring run.")
    summary: str = Field(description="One-line summary for the Output page.")
    computed_at: AwareDatetime = Field(description="UTC time drift was computed.")


# ---------------------------------------------------------------------------
# 5.11 scoring_summary.json, scores.csv / scores.parquet
# ---------------------------------------------------------------------------
class BandCount(Artefact):
    """How many scored rows fell into one band."""

    name: str = Field(description="Band name from the configuration.")
    action: str = Field(description="Action configured for the band.")
    min_score: float = Field(description="Lowest score that still falls into this band.")
    rows: int = Field(description="Rows in this band.")
    share_pct: float = Field(description="Share of scored rows in this band, as a percentage.")


class ActionCount(Artefact):
    """How many scored rows received one action."""

    action: str = Field(description="Action assigned to the rows.")
    rows: int = Field(description="Rows that received it.")
    share_pct: float = Field(description="Share of scored rows with this action, as a percentage.")


class SuppressionCount(Artefact):
    """How many rows one suppression rule removed from targeting."""

    reason: Literal["opted_out", "recently_contacted", "consent_false"] = Field(
        description="Why the rows were suppressed."
    )
    rows: int = Field(description="Rows suppressed for this reason.")


class KpiValue(Artefact):
    """The headline KPI tile of the Output page, computed from the configured formula."""

    label: str = Field(description="KPI label from the configuration.")
    formula: str = Field(description="KPI formula from the configuration, as written.")
    value: float = Field(description="Computed value of the formula.")
    display: str = Field(description="Pre-formatted value, for example 184K.")


class ScoreRow(Artefact):
    """Row schema of `scores.csv` and `scores.parquet`."""

    primary_key: str = Field(description="Primary-key value, so scores join back to the source system.")
    score: float = Field(description="Model score for this row.")
    band: str = Field(description="Band the score falls into.")
    action: str = Field(description="Action assigned to the row.")
    reasons: tuple[Reason, ...] = Field(description="Top reasons behind the score.")
    suppressed_reason: str | None = Field(
        default=None, description="Why the row was suppressed, when it was."
    )
    control_group: bool = Field(default=False, description="Whether the row was held out as a control.")


class ScoringSummary(Artefact):
    """`scoring_summary.json` - everything the Output page needs without reading the scores file."""

    run_id: str = Field(description="Scoring run this summary belongs to.")
    model_version_id: str = Field(description="Model version the rows were scored with.")
    model_display_name: str = Field(description="Display name of that model.")
    rows_scored: int = Field(description="Rows that were scored.")
    score_field: str = Field(description="Name of the score column in the exported files.")
    score_mean: float = Field(description="Mean score over the scored rows.")
    score_median: float = Field(description="Median score over the scored rows.")
    bands: tuple[BandCount, ...] = Field(description="Band counts, in configured band order.")
    actions: tuple[ActionCount, ...] = Field(description="Action counts, largest first.")
    suppressed: tuple[SuppressionCount, ...] = Field(description="Suppression counts by reason.")
    control_group_rows: int = Field(description="Rows held out as the control group.")
    rows_with_fallback_reasons: int = Field(
        default=0,
        description=(
            "Rows whose reasons did not come from the run's primary explanation tier, because every "
            "feature's contribution on that row measured zero. They carry a later tier's reasons or, "
            "as a floor, general ones from the importance chart (DEC-056)."
        ),
    )
    kpi: KpiValue = Field(description="The configured headline KPI.")
    drift_status: DriftStatus | None = Field(
        default=None, description="Drift verdict, when drift was computed."
    )
    drift_max_psi: float | None = Field(default=None, description="Largest population stability index.")
    drift_summary: str | None = Field(default=None, description="One-line drift summary.")
    files: dict[str, str] = Field(description="Exported filename mapped to its storage key.")
    sample_rows: tuple[ScoreRow, ...] = Field(
        description="First ten scored rows, so the page renders without the export."
    )
    scored_at: AwareDatetime = Field(description="UTC time scoring finished.")


def scores_csv_columns(config: UseCaseConfig, primary_key: str) -> tuple[str, ...]:
    """Header of `scores.csv` / `scores.parquet`, in order.

    `(<primary_key>, <actions.score_field>, "band", "action", "reason_1".."reason_n",
    "suppressed_reason", "control_group")`, where `n` is `evaluation.reasons_per_row`.
    """
    reasons = tuple(f"reason_{i}" for i in range(1, config.evaluation.reasons_per_row + 1))
    return (
        primary_key,
        config.actions.score_field,
        "band",
        "action",
        *reasons,
        "suppressed_reason",
        "control_group",
    )


# ---------------------------------------------------------------------------
# 5.12 Registry record and schema.json
# ---------------------------------------------------------------------------
NO_CHAMPION_AT_DECISION: Final[str] = "__none__"
"""`ModelVersion.measured_against_champion_id` when the use case had no champion to compare with.

A model id is never this string (ids are `m_<use case>_<n>`), so the sentinel keeps "measured
against no champion" distinguishable from "no champion recorded", which a plain null would not:
the first is a decision that must be re-taken once a champion exists, the second is a version
registered before the field did (DEC-047).
"""


class ModelVersion(Artefact):
    """The registry record of one trained model (SQLite, not a run artefact)."""

    model_id: str = Field(description="Unique id of this model version.")
    use_case_id: str = Field(description="Use case the model belongs to.")
    version: int = Field(description="Version number within the use case, starting at one.")
    run_id: str = Field(description="Training run that produced the model.")
    created_at: AwareDatetime = Field(description="UTC time the version was registered.")
    status: ModelStatus = Field(description="Lifecycle status of the version.")
    metric: Metric = Field(description="Metric the scores below are measured in.")
    metric_label: str = Field(description="Catalog label of the metric.")
    test_score: float = Field(description="Test-split score, the number the champion rule compares.")
    validation_score: float | None = Field(default=None, description="Validation-split score.")
    model_display_name: str = Field(description="Display name of the model.")
    schema_key: str = Field(description="Storage key of the saved feature schema.")
    run_config_key: str = Field(description="Storage key of the resolved run configuration.")
    predictor_key: str = Field(description="Storage key of the saved predictor directory.")
    drift_baseline_key: str | None = Field(default=None, description="Storage key of the drift baseline.")
    artefact_keys: dict[str, str] = Field(
        default_factory=dict, description="Artefact filename mapped to its storage key."
    )
    approved_by: str | None = Field(default=None, description="Who approved the version.")
    approved_at: AwareDatetime | None = Field(default=None, description="UTC time of the approval.")
    promoted_at: AwareDatetime | None = Field(
        default=None, description="UTC time the version became champion."
    )
    promoted_by: str | None = Field(default=None, description="Who promoted the version.")
    promotion_note: str | None = Field(default=None, description="Why the version was promoted.")
    previous_champion_id: str | None = Field(
        default=None, description="Version this one replaced as champion."
    )
    improvement_pct: float | None = Field(
        default=None,
        description=(
            "Percentage improvement over the champion named by measured_against_champion_id while "
            "the version waits for approval, and over previous_champion_id once it is champion; "
            "never read it without one of those two, which say which champion it is a percentage of."
        ),
    )
    measured_against_champion_id: str | None = Field(
        default=None,
        description=(
            "Champion the promotion decision and improvement_pct of a version waiting for approval "
            f"were measured against: that champion's model id, {NO_CHAMPION_AT_DECISION!r} when the "
            "use case had no champion at the time, and null for a version registered before this was "
            "recorded. Approval is refused (CHAMPION_CHANGED) when the use case's champion is no "
            "longer the one named here, so no version is ever crowned on a comparison it never had "
            "with the model it would replace; a null is approved without that check, because such a "
            "row does not say what its decision was measured against."
        ),
    )
    engine_version: str = Field(description="Engine version that trained the model.")
    autogluon_version: str = Field(description="AutoGluon version that trained the model.")


class FeatureSchemaColumn(Artefact):
    """One column of the schema a scoring file must match."""

    name: str = Field(description="Column name as used at fit time.")
    inferred_type: ColumnType = Field(description="Engine-level type of the column.")
    required: bool = Field(default=True, description="Whether a scoring file must contain the column.")
    nullable: bool = Field(default=True, description="Whether nulls are accepted in the column.")
    categories: tuple[str, ...] = Field(
        default=(), description="Categories seen at fit time, when few enough."
    )
    minimum: float | None = Field(default=None, description="Smallest value seen at fit time.")
    maximum: float | None = Field(default=None, description="Largest value seen at fit time.")


class FeatureSchema(Artefact):
    """`schema.json` (plan section 4.4) - saved with every model and checked at score time."""

    use_case_id: str = Field(description="Use case the schema belongs to.")
    model_version_id: str = Field(description="Model version the schema was saved with.")
    primary_key: PrimaryKey = Field(description="Primary-key column, or columns.")
    target: str | None = Field(description="Target column; null for scoring-only schemas.")
    problem_type: ProblemType = Field(description="Problem type the model was fitted for.")
    columns: tuple[FeatureSchemaColumn, ...] = Field(
        description="Columns in the exact order used at fit time."
    )
    row_count_at_fit: int = Field(description="Rows the model was fitted on.")
    created_at: AwareDatetime = Field(description="UTC time the schema was written.")


# ---------------------------------------------------------------------------
# 5.13 Registry of artefacts
# ---------------------------------------------------------------------------
# run_manifest.json: one flat, queryable record per run (DEC-042)
# ---------------------------------------------------------------------------
class CostEstimate(Artefact):
    """What a run cost to produce.

    `estimated_usd` is null for a local run rather than zero: nothing was billed,
    and a fabricated zero would be indistinguishable from a real measurement of
    free compute (plan section 13.3).
    """

    compute_seconds: float = Field(description="Wall-clock seconds of compute the run consumed.")
    estimated_usd: float | None = Field(
        default=None, description="Billed cost when the platform reports one; null when nothing was billed."
    )
    basis: str = Field(description="How the estimate was derived, in plain words.")


class LLMUsage(Artefact):
    """What a run spent on language models.

    Counted, never estimated: `calls`, `input_tokens` and `output_tokens` are what the client was
    told by the provider, and `cost_estimate_usd` is `None` unless a real billed figure exists -
    the same rule `CostEstimate.estimated_usd` follows, because a fabricated zero cannot be told
    apart from a measurement of something free. A run that called no model carries `None` for the
    whole object rather than a zeroed one.
    """

    calls: int = Field(description="Completion and embedding requests the run made.")
    input_tokens: int = Field(description="Tokens sent, totalled over every call.")
    output_tokens: int = Field(description="Tokens returned, totalled over every call.")
    cost_estimate_usd: float | None = Field(
        default=None, description="Billed cost when the provider reports one; null when nothing was billed."
    )
    model_ids: tuple[str, ...] = Field(
        default=(), description="Every model the run used, sorted, so a manifest names its sources."
    )


class ComputeInfo(Artefact):
    """Where a run actually ran, and what that cost.

    `job_arn` and `instance_type` are null for a local run because a local run has neither; the
    fields are not padded with a placeholder. `cost_estimate_usd` follows `CostEstimate`: null
    unless something was billed.
    """

    backend: ComputeBackend = Field(description="Which compute carried the run: local or sagemaker.")
    job_arn: str | None = Field(default=None, description="ARN of the managed job; null for a local run.")
    instance_type: str | None = Field(
        default=None, description="Instance the managed job ran on; null for a local run."
    )
    duration_s: float = Field(description="Wall-clock seconds the compute was occupied.")
    cost_estimate_usd: float | None = Field(
        default=None, description="Billed cost when the platform reports one; null when nothing was billed."
    )


class RunManifest(Artefact):
    """`run_manifest.json` - one flat record per run, written for every run.

    Everything else a run writes is shaped for a screen. This is shaped for a
    reader asking "which choices, on which data, produced which numbers, at what
    cost?" and it is written even when a run fails, carrying whatever is known,
    because a failed attempt is evidence too.
    """

    run_id: str = Field(description="Run this manifest describes.")
    primary_key: PrimaryKey = Field(description="Column, or columns, that identified a row.")
    dataset_id: str | None = Field(
        default=None, description="Onboarded dataset the run consumed; null for a direct upload."
    )
    client_id: str | None = Field(
        default=None, description="Client the dataset belongs to; null when no client was named."
    )
    recipe: Recipe | None = Field(
        default=None,
        description="Training choices; null for a scoring run that did not fit a model.",
    )
    dataset_fingerprint: DatasetFingerprint = Field(description="Identity of the data the run consumed.")
    seed: int = Field(description="Seed that made the run reproducible.")
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Headline numbers, metric id to value; empty when the run produced none.",
    )
    leaderboard_path: str | None = Field(
        default=None, description="Storage key of leaderboard.json; null when no search was run."
    )
    duration_s: float = Field(description="Wall-clock seconds from run start to final state.")
    cost_estimate: CostEstimate = Field(description="What the run cost to produce.")
    llm_usage: LLMUsage | None = Field(
        default=None, description="Language-model usage; null when the run called no model."
    )
    compute: ComputeInfo | None = Field(
        default=None, description="Where the run ran and what it cost; null when nothing recorded it."
    )
    created_at: AwareDatetime = Field(description="UTC time the manifest was written.")


# ---------------------------------------------------------------------------
ARTEFACT_REGISTRY: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "run.json": RunRecord,
        "status.json": RunStatus,
        "run_config.json": ResolvedConfig,
        "profile.json": DatasetProfile,
        "validation.json": ValidationReport,
        "prepare.json": PrepareReport,
        "split.json": SplitReport,
        "leaderboard.json": Leaderboard,
        "best_model.json": BestModel,
        "evaluation.json": EvaluationReport,
        "confusion_matrix.json": ConfusionMatrix,
        "decile_lift.json": DecileLift,
        "baseline.json": BaselineComparison,
        "fairness.json": FairnessReport,
        "feature_importance.json": FeatureImportance,
        "drift_baseline.json": DriftBaseline,
        "drift.json": DriftReport,
        "scoring_summary.json": ScoringSummary,
        "schema.json": FeatureSchema,
        "run_manifest.json": RunManifest,
    }
)
"""Artefact filename -> the model that validates it. `run_config.json` is `engine.config.ResolvedConfig`."""

TABULAR_SCHEMAS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "row_explanations.parquet": RowExplanation,
        "scores.csv": ScoreRow,
        "scores.parquet": ScoreRow,
    }
)
"""Tabular artefact filename -> the model describing ONE ROW of that file."""

MODEL_DIRECTORY: Final[str] = "model/"
"""The saved AutoGluon predictor directory; a directory, so it has no JSON contract."""

TRAIN_ARTEFACTS: Final[frozenset[str]] = frozenset(
    {
        "run.json",
        "status.json",
        "run_config.json",
        "profile.json",
        "validation.json",
        "prepare.json",
        "split.json",
        "leaderboard.json",
        "best_model.json",
        "evaluation.json",
        "confusion_matrix.json",
        "decile_lift.json",
        "baseline.json",
        "fairness.json",
        "feature_importance.json",
        "drift_baseline.json",
        "schema.json",
        "run_manifest.json",
        "row_explanations.parquet",
        MODEL_DIRECTORY,
    }
)
"""Everything a completed training run writes."""

SCORE_ARTEFACTS: Final[frozenset[str]] = frozenset(
    {
        "run_manifest.json",
        "run.json",
        "status.json",
        "run_config.json",
        "profile.json",
        "validation.json",
        "prepare.json",
        "drift.json",
        "scoring_summary.json",
        "row_explanations.parquet",
        "scores.csv",
        "scores.parquet",
    }
)
"""Everything a completed scoring run writes.

`drift.json` is the one conditional member: drift is a reported signal, not a gate, and a run with
no stored baseline, no rows or no comparable features writes no verdict at all rather than an empty
one (DEC-051). Every other name here is written by every scoring run that reaches `done`.
"""


def _known_artefacts() -> str:
    return ", ".join(sorted({*ARTEFACT_REGISTRY, *TABULAR_SCHEMAS, MODEL_DIRECTORY}))


def artefact_model(filename: str) -> type[BaseModel]:
    """The model for an artefact filename; `KeyError` listing the known names for anything else."""
    model = ARTEFACT_REGISTRY.get(filename) or TABULAR_SCHEMAS.get(filename)
    if model is None:
        raise KeyError(f"unknown artefact {filename!r}; known artefacts: {_known_artefacts()}")
    return model


def dump_artefact(model: BaseModel) -> str:
    """The one serialiser for every artefact: indented JSON, aliases, trailing newline."""
    return model.model_dump_json(indent=2, by_alias=True) + "\n"


def load_artefact(filename: str, payload: str | bytes) -> BaseModel:
    """Validate a serialised artefact against the model registered for its filename."""
    return artefact_model(filename).model_validate_json(payload)


# ===========================================================================
# Shared file (PARALLEL_WORK_PROTOCOL.md §4): three branches edit it at once.
# Add code only inside your own block, at its end. Never edit above your
# block, never reorder, never reformat the rest of the file - run `black` on
# what you paste, not on the file, if the formatter would reflow other lines.
# `tests/unit/test_shared_file_markers.py` fails if a block goes missing.
# ===========================================================================

# ---- PHASE-2 (onboarding) — append only below this line ----
# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----
