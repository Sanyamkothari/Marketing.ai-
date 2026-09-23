"""Configuration schema, loading, merging and the advanced-settings schema. Single source of truth."""

from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import importlib.util
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from engine.settings import DEFAULT_CONFIG_DIR, ENV_VARS, settings
from engine.uplift.config import (  # Phase 3b (DEC-601); imports nothing from engine
    UPLIFT_OVERRIDABLE_PATHS,
    UpliftConfig,
)


class ConfigError(Exception):
    """Every config problem. `code` is machine-readable, `message` is for humans, `path` is the dotted path or file."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if path is None else f"{code}: {message} (at {path})")
        self.code = code
        self.message = message
        self.path = path


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        use_enum_values=False,
        str_strip_whitespace=True,
        populate_by_name=True,
        protected_namespaces=(),
    )


# Public alias: engine.contracts and api.schemas inherit from it so there is one definition.
StrictBase = _Base


# ---------------------------------------------------------------------------
# The primary key: one column in Phase 1, possibly several from Phase 2 on
# ---------------------------------------------------------------------------
PrimaryKey = str | list[str]
"""A row identifier: one column name, or several that identify a row together.

The wide type is here from the start, on every contract the value travels through, because those
contracts live in shared append-only files: a branch that needed to widen them later could not.
Phase 1 only ever sets a single column, and `str` stays a legal value forever, so nothing in this
phase changes shape.

This is the one place the codebase's "collections are tuples" rule yields (`engine/contracts.py`
docstring). The cross-branch contract spells the type `str | list[str]`, three branches are written
against that spelling, and the serialised form is a JSON array either way. Read the value through
:func:`key_columns` rather than an `isinstance` check and the distinction stops mattering.
"""


def key_columns(primary_key: PrimaryKey) -> tuple[str, ...]:
    """The key's columns, in order, whether it was spelled as one name or as several."""
    return (primary_key,) if isinstance(primary_key, str) else tuple(primary_key)


def sole_key(primary_key: PrimaryKey, *, what: str = "This") -> str:
    """The single column of `primary_key`, or a `ConfigError` when it names several.

    Composite keys are Phase 2 behaviour. Until the stages that join, deduplicate and export on the
    key can carry more than one column, a composite key is refused here - at the boundary, with a
    message saying what to do - rather than silently reduced to its first column, which would join
    the wrong rows and report success.
    """
    columns = key_columns(primary_key)
    if len(columns) == 1:
        return columns[0]
    joined = ", ".join(columns) if columns else "(none)"
    raise ConfigError(
        "COMPOSITE_KEY_NOT_SUPPORTED",
        f"{what} needs a single primary-key column and was given {len(columns)}: {joined}. "
        "Combine them into one column before uploading, or pick the one that identifies a row on its own.",
        path="primary_key",
    )


# ---------------------------------------------------------------------------
# 4.1 Enums
# ---------------------------------------------------------------------------
class AiType(StrEnum):
    PREDICTIVE = "predictive"
    GENERATIVE = "generative"
    HYBRID = "hybrid"


class ProblemType(StrEnum):
    BINARY_CLASSIFICATION = "binary_classification"
    REGRESSION = "regression"
    FORECASTING = "forecasting"
    CLUSTERING = "clustering"
    UPLIFT = "uplift"  # Phase 3b (DEC-601): one member, announced in docs/CROSS_BRANCH_REQUESTS.md


class MissingValues(StrEnum):
    AUTO = "auto"
    FILL = "fill"
    DROP_ROWS = "drop_rows"


class Outliers(StrEnum):
    CLIP = "clip"
    REMOVE_ROWS = "remove_rows"
    KEEP = "keep"


class PiiHandling(StrEnum):
    REDACT = "redact"
    DROP_COLUMNS = "drop_columns"
    KEEP = "keep"


class SplitType(StrEnum):
    RANDOM_STRATIFIED = "random_stratified"
    TIME_BASED = "time_based"


class CategoricalEncoding(StrEnum):
    AUTO = "auto"
    ONE_HOT = "one_hot"
    TARGET = "target"
    ORDINAL = "ordinal"


class NumericScaling(StrEnum):
    AUTO = "auto"
    STANDARD = "standard"
    MIN_MAX = "min_max"
    NONE = "none"


class TextHandling(StrEnum):
    IGNORE = "ignore"
    TFIDF = "tfidf"
    EMBEDDINGS = "embeddings"


class FeatureSelection(StrEnum):
    IMPORTANCE = "importance"
    CORRELATION_FILTER = "correlation_filter"
    PCA = "pca"
    NONE = "none"


class Strategy(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    EXHAUSTIVE = "exhaustive"


class Imbalance(StrEnum):
    AUTO = "auto"
    CLASS_WEIGHTS = "class_weights"
    OVERSAMPLING = "oversampling"
    NONE = "none"


class Metric(StrEnum):
    ROC_AUC = "roc_auc"
    PR_AUC = "pr_auc"
    F1 = "f1"
    RECALL = "recall"
    PRECISION = "precision"
    RMSE = "rmse"
    MAE = "mae"
    AUUC = "auuc"  # Phase 3b (DEC-601): the uplift problem type's only metric, never sent to AutoGluon


class Calibration(StrEnum):
    ISOTONIC = "isotonic"
    PLATT = "platt"
    NONE = "none"


class ThresholdMode(StrEnum):
    AUTO = "auto"
    FIXED = "fixed"
    MANUAL = "manual"


class Retraining(StrEnum):
    MANUAL = "manual"
    ON_DRIFT = "on_drift"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class GenerativeKind(StrEnum):
    """Which generative flow a use case turns on. `none` leaves the whole block inert (DEC-200)."""

    NONE = "none"
    RAG_ASSISTANT = "rag_assistant"
    ROOT_CAUSE_SUMMARY = "root_cause_summary"
    CAMPAIGN_COPY = "campaign_copy"


class LlmBackend(StrEnum):
    """Which client a generative flow calls through: a deterministic fake, or Bedrock (DEC-203)."""

    FAKE = "fake"
    BEDROCK = "bedrock"


class DocumentType(StrEnum):
    """A file type the knowledge base accepts. The value is the extension, so a filename decides."""

    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    TXT = "txt"


class Channel(StrEnum):
    """A channel copy is written for. Nothing is sent: the name selects a prompt and a length limit."""

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class SegmentBy(StrEnum):
    """How a root-cause run divides the scored rows before it explains them."""

    BAND = "band"
    TOP_REASON = "top_reason"


class ModelFamily(StrEnum):
    XGBOOST = "XGBoost"
    LIGHTGBM = "LightGBM"
    RANDOM_FOREST = "RandomForest"
    LOGISTIC_REGRESSION = "LogisticRegression"
    CATBOOST = "CatBoost"
    NEURAL_NET = "NeuralNet"


class ColumnRole(StrEnum):
    PRIMARY_KEY = "primary_key"
    TIME = "time"
    FEATURE = "feature"
    TARGET = "target"
    CONSENT = "consent"
    CONTACT = "contact"


class ColumnType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TEXT = "text"


class UseCaseStatus(StrEnum):
    AVAILABLE = "available"
    PLANNED = "planned"


class RunMode(StrEnum):
    TRAIN = "train"
    SCORE = "score"


class Widget(StrEnum):
    SELECT = "select"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    COLUMN_SELECT = "column-select"
    COLUMN_MULTI_SELECT = "column-multi-select"
    MULTI_SELECT = "multi-select"


class ColumnSource(StrEnum):
    ALL = "all"
    FEATURES = "features"
    TIME_LIKE = "time_like"


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"


# ---------------------------------------------------------------------------
# Labels. Labels never come from config values (DEC-010).
# ---------------------------------------------------------------------------
def _member_key(member: StrEnum) -> tuple[str, str]:
    return (type(member).__name__, str(member))


class _ChoiceLabelMap(Mapping[StrEnum, str]):
    """Enum member -> verbatim UI label.

    Members of different `StrEnum` classes that share a value compare and hash equal
    (`ThresholdMode.MANUAL == Retraining.MANUAL`), so the backing store is keyed by
    (enum class name, value) and a lookup resolves against the member's own class.
    """

    def __init__(self, pairs: Sequence[tuple[StrEnum, str]]) -> None:
        self._labels: dict[tuple[str, str], str] = {_member_key(m): label for m, label in pairs}
        self._members: tuple[StrEnum, ...] = tuple(m for m, _ in pairs)

    def __getitem__(self, key: StrEnum) -> str:
        try:
            return self._labels[_member_key(key)]
        except KeyError:
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[StrEnum]:
        return iter(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def __repr__(self) -> str:
        return f"_ChoiceLabelMap({len(self._members)} members)"


CHOICE_LABELS: Final[Mapping[StrEnum, str]] = _ChoiceLabelMap(
    (
        (MissingValues.AUTO, "Auto"),
        (MissingValues.FILL, "Fill (median / mode)"),
        (MissingValues.DROP_ROWS, "Drop rows"),
        (Outliers.CLIP, "Clip (1st–99th pct)"),
        (Outliers.REMOVE_ROWS, "Remove rows"),
        (Outliers.KEEP, "Keep"),
        (PiiHandling.REDACT, "Redact"),
        (PiiHandling.DROP_COLUMNS, "Drop columns"),
        (PiiHandling.KEEP, "Keep"),
        (SplitType.RANDOM_STRATIFIED, "Random (stratified)"),
        (SplitType.TIME_BASED, "Time-based"),
        (CategoricalEncoding.AUTO, "Auto"),
        (CategoricalEncoding.ONE_HOT, "One-hot"),
        (CategoricalEncoding.TARGET, "Target encoding"),
        (CategoricalEncoding.ORDINAL, "Ordinal"),
        (NumericScaling.AUTO, "Auto"),
        (NumericScaling.STANDARD, "Standard"),
        (NumericScaling.MIN_MAX, "Min-max"),
        (NumericScaling.NONE, "None"),
        (TextHandling.IGNORE, "Ignore"),
        (TextHandling.TFIDF, "TF-IDF"),
        (TextHandling.EMBEDDINGS, "Embeddings"),
        (FeatureSelection.IMPORTANCE, "Importance-based"),
        (FeatureSelection.CORRELATION_FILTER, "Correlation filter"),
        (FeatureSelection.PCA, "PCA (reduces explainability)"),
        (FeatureSelection.NONE, "None"),
        (Strategy.FAST, "Fast"),
        (Strategy.BALANCED, "Balanced"),
        (Strategy.EXHAUSTIVE, "Exhaustive"),
        (Imbalance.AUTO, "Auto"),
        (Imbalance.CLASS_WEIGHTS, "Class weights"),
        (Imbalance.OVERSAMPLING, "Oversampling (SMOTE)"),
        (Imbalance.NONE, "None"),
        (Calibration.ISOTONIC, "Isotonic"),
        (Calibration.PLATT, "Platt"),
        (Calibration.NONE, "None"),
        (ThresholdMode.AUTO, "Auto"),
        (ThresholdMode.FIXED, "0.5"),
        (ThresholdMode.MANUAL, "Manual (below)"),
        (Retraining.MANUAL, "Manual"),
        (Retraining.ON_DRIFT, "On drift"),
        (Retraining.WEEKLY, "Weekly"),
        (Retraining.MONTHLY, "Monthly"),
    )
)

STAGE_MODULE_MAP: Final[Mapping[str, str]] = {
    "ingest": "engine.stages.ingest",
    "validate": "engine.stages.validate",
    "prepare": "engine.stages.prepare",
    "split": "engine.stages.prepare",
    "train": "engine.stages.train",
    "evaluate": "engine.stages.evaluate",
    "explain": "engine.stages.explain",
    "register": "engine.stages.register",
    "validate_against_schema": "engine.stages.validate",
    "predict": "engine.stages.score",
    "explain_rows": "engine.stages.explain",
    "actions": "engine.stages.actions",
    "export": "engine.stages.export",
}

TIME_LIKE_PATTERN: Final[str] = r"date|time|month|week|day|_ts$|_at$"

SCHEMA_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# 4.2 Catalog models
# ---------------------------------------------------------------------------
def dependency_available(module: str) -> bool:
    """`importlib.util.find_spec(module) is not None`. Module-level so tests monkeypatch it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class ModelFamilySpec(_Base):
    autogluon_key: Literal["XGB", "GBM", "RF", "LR", "CAT", "NN_TORCH"]
    label: str
    requires: tuple[str, ...] = ()


class MetricSpec(_Base):
    label: str
    autogluon_name: str
    problem_types: tuple[ProblemType, ...]
    greater_is_better: bool


class ProblemTypeSpec(_Base):
    label: str
    enabled: bool


class AiTypeSpec(_Base):
    marker: Literal["P", "G", "H"]
    stars: str
    label: str


class AutomlChoice(_Base):
    value: str
    label: str


class ColumnNamePatterns(_Base):
    time_like: str
    leakage: str
    leakage_after_target: str

    @field_validator("time_like", "leakage", "leakage_after_target")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError("CATALOG_BAD_REGEX", f"Not a valid regular expression: {exc}.") from exc
        return v


class Catalog(_Base):
    model_families: dict[ModelFamily, ModelFamilySpec]
    strategy_presets: dict[Strategy, str]
    metrics: dict[Metric, MetricSpec]
    problem_types: dict[ProblemType, ProblemTypeSpec]
    ai_types: dict[AiType, AiTypeSpec]
    automl_choice: AutomlChoice
    column_name_patterns: ColumnNamePatterns

    @model_validator(mode="after")
    def _complete(self) -> Self:
        for block, members in (
            ("model_families", tuple(ModelFamily)),
            ("strategy_presets", tuple(Strategy)),
            ("metrics", tuple(Metric)),
            ("problem_types", tuple(ProblemType)),
            ("ai_types", tuple(AiType)),
        ):
            present = getattr(self, block)
            for member in members:
                if member not in present:
                    raise ConfigError(
                        "CATALOG_INCOMPLETE",
                        f"catalog.{block} is missing an entry for {member.value!r}.",
                        path=f"catalog.{block}",
                    )
        return self

    def metrics_for(self, problem_type: ProblemType) -> tuple[Metric, ...]:
        return tuple(m for m, spec in self.metrics.items() if problem_type in spec.problem_types)

    def autogluon_hyperparameter_keys(self, families: Sequence[ModelFamily]) -> tuple[str, ...]:
        return tuple(self.model_families[f].autogluon_key for f in families)

    def family_label(self, family: ModelFamily) -> str:
        return self.model_families[family].label

    def metric_label(self, metric: Metric) -> str:
        return self.metrics[metric].label

    def missing_requirements(self, families: Sequence[ModelFamily]) -> dict[ModelFamily, tuple[str, ...]]:
        """Families whose `requires` modules are not importable (uses dependency_available)."""
        missing: dict[ModelFamily, tuple[str, ...]] = {}
        for family in families:
            absent = tuple(m for m in self.model_families[family].requires if not dependency_available(m))
            if absent:
                missing[family] = absent
        return missing


# ---------------------------------------------------------------------------
# 4.3 Config section models
# ---------------------------------------------------------------------------
class TargetConfig(_Base):
    column: str | None = None
    positive_label: str | int | bool | None = None
    definition: str = ""
    label_source: str = ""


class ValidationConfig(_Base):
    min_rows: Annotated[int, Field(ge=1)] = 1000
    min_positive: Annotated[int, Field(ge=1)] = 200
    max_file_size_mb: Annotated[int, Field(ge=1)] = 2048
    imbalance_warn_min_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.01
    imbalance_warn_max_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.99
    high_null_column_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.60
    leakage_check: bool = True
    leakage_auc_threshold: Annotated[float, Field(ge=0.5, le=1.0)] = 0.98
    acknowledged: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _rates_ordered(self) -> Self:
        if self.imbalance_warn_min_rate >= self.imbalance_warn_max_rate:
            raise ConfigError(
                "VALIDATION_RATES_INVERTED",
                f"imbalance_warn_min_rate ({self.imbalance_warn_min_rate}) must be below "
                f"imbalance_warn_max_rate ({self.imbalance_warn_max_rate}).",
                path="validation.imbalance_warn_min_rate",
            )
        return self

    @field_validator("acknowledged")
    @classmethod
    def _ack_format(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for entry in v:
            if not re.fullmatch(r"^[A-Z_]+(:[^:]+)?$", entry):
                raise ConfigError(
                    "VALIDATION_ACK_MALFORMED",
                    f"{entry!r} is not a valid acknowledgement. Use CODE or CODE:column.",
                    path="validation.acknowledged",
                )
            if entry in seen:
                raise ConfigError(
                    "VALIDATION_ACK_DUPLICATE",
                    f"{entry!r} is acknowledged twice.",
                    path="validation.acknowledged",
                )
            seen.add(entry)
        return v


class PrepareConfig(_Base):
    missing_values: MissingValues = MissingValues.AUTO
    outliers: Outliers = Outliers.CLIP
    pii_handling: PiiHandling = PiiHandling.REDACT
    deduplicate: bool = True
    exclude_columns: tuple[str, ...] = ()

    @field_validator("exclude_columns")
    @classmethod
    def _unique_nonempty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for name in v:
            if not name:
                raise ConfigError(
                    "PREPARE_EXCLUDE_EMPTY",
                    "prepare.exclude_columns contains an empty column name.",
                    path="prepare.exclude_columns",
                )
            if name in seen:
                raise ConfigError(
                    "PREPARE_EXCLUDE_DUPLICATE",
                    f"prepare.exclude_columns lists {name!r} twice.",
                    path="prepare.exclude_columns",
                )
            seen.add(name)
        return v


class SplitConfig(_Base):
    type: SplitType = SplitType.RANDOM_STRATIFIED
    validation_fraction: Annotated[float, Field(ge=0.05, le=0.40)] = 0.15
    test_fraction: Annotated[float, Field(ge=0.05, le=0.40)] = 0.15
    time_column: str | None = None
    group_column: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.validation_fraction + self.test_fraction > 0.60:
            raise ConfigError(
                "SPLIT_FRACTIONS_TOO_LARGE",
                f"validation ({self.validation_fraction}) plus test ({self.test_fraction}) "
                "must leave at least 40% of the rows for training.",
                path="split.test_fraction",
            )
        if self.group_column is not None and self.group_column == self.time_column:
            raise ConfigError(
                "SPLIT_GROUP_EQUALS_TIME",
                f"{self.group_column!r} cannot be both the group column and the time column.",
                path="split.group_column",
            )
        return self


class FeaturesConfig(_Base):
    auto_feature_engineering: bool = True
    categorical_encoding: CategoricalEncoding = CategoricalEncoding.AUTO
    numeric_scaling: NumericScaling = NumericScaling.AUTO
    text_columns: TextHandling = TextHandling.IGNORE
    selection: FeatureSelection = FeatureSelection.IMPORTANCE
    max_features: Annotated[int, Field(ge=10, le=500)] = 100


class ModelSearchConfig(_Base):
    metric: Metric = Metric.ROC_AUC
    metric_choices: tuple[Metric, ...] = (
        Metric.ROC_AUC,
        Metric.PR_AUC,
        Metric.F1,
        Metric.RECALL,
        Metric.PRECISION,
    )
    strategy: Strategy = Strategy.BALANCED
    candidate_pool: tuple[ModelFamily, ...] = (
        ModelFamily.XGBOOST,
        ModelFamily.LIGHTGBM,
        ModelFamily.RANDOM_FOREST,
        ModelFamily.LOGISTIC_REGRESSION,
    )
    candidates: tuple[ModelFamily, ...] = (
        ModelFamily.XGBOOST,
        ModelFamily.LIGHTGBM,
        ModelFamily.RANDOM_FOREST,
        ModelFamily.LOGISTIC_REGRESSION,
    )
    ensemble: bool = True
    tuning_trials: Annotated[int, Field(ge=5, le=500)] = 50
    time_limit_minutes: Annotated[int, Field(ge=1, le=240)] = 30  # 1 for plan §10's smoke run (DEC-036)
    folds: Annotated[int, Field(ge=2, le=10)] = 5  # plan §6.3 `folds` -> AutoGluon num_bag_folds
    imbalance: Imbalance = Imbalance.AUTO

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.candidate_pool:
            raise ConfigError(
                "MODEL_SEARCH_POOL_EMPTY",
                "model_search.candidate_pool must offer at least one model family.",
                path="model_search.candidate_pool",
            )
        if not self.candidates:
            raise ConfigError(
                "MODEL_SEARCH_NO_CANDIDATES",
                "model_search.candidates must select at least one model family.",
                path="model_search.candidates",
            )
        for field_name in ("candidate_pool", "candidates"):
            families: tuple[ModelFamily, ...] = getattr(self, field_name)
            if len(set(families)) != len(families):
                raise ConfigError(
                    "MODEL_SEARCH_DUPLICATE",
                    f"model_search.{field_name} lists the same model family twice.",
                    path=f"model_search.{field_name}",
                )
        outside = [f.value for f in self.candidates if f not in self.candidate_pool]
        if outside:
            raise ConfigError(
                "MODEL_SEARCH_NOT_IN_POOL",
                f"model_search.candidates must be chosen from candidate_pool; {', '.join(outside)} is not in it.",
                path="model_search.candidates",
            )
        if not self.metric_choices:
            raise ConfigError(
                "METRIC_CHOICES_EMPTY",
                "model_search.metric_choices must offer at least one metric.",
                path="model_search.metric_choices",
            )
        if len(set(self.metric_choices)) != len(self.metric_choices):
            raise ConfigError(
                "METRIC_CHOICES_DUPLICATE",
                "model_search.metric_choices lists the same metric twice.",
                path="model_search.metric_choices",
            )
        if self.metric not in self.metric_choices:
            raise ConfigError(
                "METRIC_NOT_IN_CHOICES",
                f"model_search.metric {self.metric.value!r} is not one of metric_choices.",
                path="model_search.metric",
            )
        return self


class ThresholdConfig(_Base):
    mode: ThresholdMode = ThresholdMode.AUTO
    value: Annotated[float, Field(ge=0.01, le=0.99)] = 0.50
    # The share of validation rows `auto` may flag before it falls back to the top decile and says
    # so with THRESHOLD_FALLBACK (DEC-094). `configs/engine.yaml` sets the product default, 0.30, so
    # every shipped use case has it; `None` - what a bare `ThresholdConfig()` built in code gets -
    # is no ceiling, the rule `auto` followed before the ceiling existed. Config-only: not a form
    # control and not a per-run override. Only `auto` reads it.
    max_flagged_rate: Annotated[float, Field(ge=0.01, le=1.0)] | None = None

    @model_validator(mode="after")
    def _fixed_is_half(self) -> Self:
        if self.mode is ThresholdMode.FIXED and self.value != 0.50:
            raise ConfigError(
                "THRESHOLD_FIXED_NOT_HALF",
                f"A fixed decision threshold is 0.5; {self.value} was given. Use mode 'manual' for another number.",
                path="evaluation.threshold.value",
            )
        return self


class EvaluationConfig(_Base):
    calibration: Calibration = Calibration.ISOTONIC
    threshold: ThresholdConfig = ThresholdConfig()
    shap: bool = True
    reasons_per_row: Annotated[int, Field(ge=1, le=5)] = 3
    fairness_column: str | None = None
    champion_min_improvement_pct: Annotated[float, Field(ge=0.0, le=20.0)] = 1.0


class Band(_Base):
    name: Annotated[str, Field(min_length=1, max_length=40)]
    min_score: Annotated[float, Field(ge=0.0, le=1.0)]
    action: Annotated[str, Field(min_length=1, max_length=80)]


class SuppressionConfig(_Base):
    suppress_opted_out: bool = True
    opt_out_column: str | None = "marketing_opt_in"
    suppress_recently_contacted: bool = True
    recently_contacted_column: str | None = "last_contacted_at"
    recently_contacted_days: Annotated[int, Field(ge=1, le=90)] = 14


class ActionsConfig(_Base):
    score_field: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = "propensity"
    bands: tuple[Band, ...] = (
        Band(name="High", min_score=0.80, action="Act now"),
        Band(name="Medium", min_score=0.50, action="Monitor"),
        Band(name="Low", min_score=0.00, action="No action"),
    )
    suppression: SuppressionConfig = SuppressionConfig()
    control_group_fraction: Annotated[float, Field(ge=0.0, le=0.50)] = 0.10

    @field_validator("bands")
    @classmethod
    def _bands(cls, v: tuple[Band, ...]) -> tuple[Band, ...]:
        if len(v) < 2:
            raise ConfigError(
                "BANDS_TOO_FEW",
                "actions.bands needs at least two bands so every score lands in one.",
                path="actions.bands",
            )
        for index in range(1, len(v)):
            previous, current = v[index - 1], v[index]
            if current.min_score >= previous.min_score:
                raise ConfigError(
                    "BANDS_NOT_DESCENDING",
                    f"{current.name}-risk score ({current.min_score:.2f}) must be below "
                    f"{previous.name}-risk score ({previous.min_score:.2f}).",
                    path=f"actions.bands[{index}].min_score",
                )
        if v[-1].min_score != 0.0:
            raise ConfigError(
                "BANDS_LAST_NOT_ZERO",
                f"The last band ({v[-1].name}) must start at 0.0 so every score lands in a band.",
                path=f"actions.bands[{len(v) - 1}].min_score",
            )
        names = [band.name for band in v]
        if len(set(names)) != len(names):
            raise ConfigError(
                "BANDS_DUPLICATE_NAME",
                "actions.bands uses the same band name twice.",
                path="actions.bands",
            )
        return v

    def band_for(self, score: float) -> Band:
        """The first band whose `min_score` the score reaches (pure; M4 uses it)."""
        for band in self.bands:
            if score >= band.min_score:
                return band
        return self.bands[-1]


class MonitoringConfig(_Base):
    drift_psi_threshold: Annotated[float, Field(ge=0.05, le=1.0)] = 0.20
    retraining: Retraining = Retraining.ON_DRIFT
    performance_alert_drop_pct: Annotated[int, Field(ge=1, le=50)] = 5


class GovernanceConfig(_Base):
    retention_days: Annotated[int, Field(ge=0, le=730)] = 90
    consent_column: str | None = None
    approval_required: bool = True


_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""The shape a name must have to be usable as a column or as a `{{placeholder}}` in a template."""


# ---------------------------------------------------------------------------
# 4.5b The generative block (Phase 3a). Inert unless `kind` names a flow (DEC-200).
#
# One block, not three: the plan sketched `generative:` in engine.yaml but the root-cause and copy
# blocks at the top level of a use-case file, and two spellings of one thing would need two merge
# paths and would leave `extra="forbid"` rejecting whichever spelling a reader guessed wrong
# (DEC-200). Every sub-block is named for the capability it configures, never for the use case that
# turns it on, because an engine that knows a use-case id has started branching on one (plan
# section 2.1, principle 1). Nothing here reaches the predictive stages: a generative flow reads a
# finished run's artefacts, it never joins the pipeline that wrote them.
# ---------------------------------------------------------------------------
FAKE_MODEL_ID: Final[str] = "fake"
"""What the deterministic fake calls itself in an artefact.

The three model ids default to `""` because a real one is deployment data (DEC-204), but an
artefact still has to say what answered, and `""` says nothing. Under the fake backend that answer
is "the fake", written down as such, so a usage record read later cannot be mistaken for a record
of a model that was never called.
"""


class LlmConfig(_Base):
    backend: LlmBackend = LlmBackend.FAKE
    region: Annotated[str, Field(min_length=1)] = "ap-south-1"
    generation_model_id: str = ""
    judge_model_id: str = ""
    embedding_model_id: str = ""
    temperature: Annotated[float, Field(ge=0.0, le=1.0)] = 0.20
    max_output_tokens: Annotated[int, Field(ge=1, le=8192)] = 800
    timeout_s: Annotated[int, Field(ge=1, le=600)] = 60
    max_retries: Annotated[int, Field(ge=0, le=5)] = 2

    @model_validator(mode="after")
    def _check(self) -> Self:
        # A model id is deployment data, never a literal in code (DEC-204), so the only place this
        # can be caught is here - and it is caught at configuration time rather than three stages
        # into a run that was always going to fail.
        if self.backend is LlmBackend.BEDROCK:
            missing = [
                name
                for name in ("generation_model_id", "judge_model_id", "embedding_model_id")
                if not getattr(self, name).strip()
            ]
            if missing:
                raise ConfigError(
                    "LLM_MODEL_ID_MISSING",
                    f"generative.llm.backend is 'bedrock', so {', '.join(missing)} must name a model.",
                    path=f"generative.llm.{missing[0]}",
                )
        return self

    @property
    def generation_model(self) -> str:
        """The id generation calls are made against, with the fake named when it is the backend."""
        return self._resolve(self.generation_model_id or self.judge_model_id)

    @property
    def judge_model(self) -> str:
        """The id judging calls are made against; falls back to the generating one when unset."""
        return self._resolve(self.judge_model_id or self.generation_model_id)

    @property
    def embedding_model(self) -> str:
        """The id embedding calls are made against."""
        return self._resolve(self.embedding_model_id)

    def _resolve(self, model_id: str) -> str:
        if model_id:
            return model_id
        # `bedrock` with a blank id never gets here: `_check` refuses that document outright.
        return FAKE_MODEL_ID


class BudgetConfig(_Base):
    max_cost_usd_per_run: Annotated[float, Field(ge=0.0)] = 2.00
    max_calls_per_run: Annotated[int, Field(ge=1)] = 500
    cache: bool = True


class RagConfig(_Base):
    chunk_tokens: Annotated[int, Field(ge=50, le=2000)] = 500
    chunk_overlap: Annotated[float, Field(ge=0.0, le=0.5)] = 0.15
    top_k: Annotated[int, Field(ge=1, le=50)] = 6
    min_similarity: Annotated[float, Field(ge=0.0, le=1.0)] = 0.25
    mmr_lambda: Annotated[float, Field(ge=0.0, le=1.0)] = 0.70
    answer_language: Annotated[str, Field(pattern=r"^(auto|[a-z]{2})$")] = "auto"
    refusal_message: Annotated[str, Field(min_length=1)] = (
        "I don't have that information in the documents I've been given. Please contact support."
    )


class KnowledgeBaseConfig(_Base):
    accepted_types: tuple[DocumentType, ...] = (
        DocumentType.PDF,
        DocumentType.DOCX,
        DocumentType.MD,
        DocumentType.TXT,
    )
    max_docs: Annotated[int, Field(ge=1)] = 200
    max_mb: Annotated[int, Field(ge=1)] = 200

    @field_validator("accepted_types")
    @classmethod
    def _types(cls, v: tuple[DocumentType, ...]) -> tuple[DocumentType, ...]:
        if not v:
            raise ConfigError(
                "KNOWLEDGE_BASE_NO_TYPES",
                "generative.knowledge_base.accepted_types must accept at least one file type.",
                path="generative.knowledge_base.accepted_types",
            )
        return v


class ReferenceSetConfig(_Base):
    """The Q&A file an index is graded against.

    Named for what it is rather than `evaluation`, which already means the model's evaluation on
    `UseCaseConfig`; one word cannot carry both without a reader having to ask which (DEC-200).
    """

    question_column: Annotated[str, Field(min_length=1)] = "question"
    reference_column: Annotated[str, Field(min_length=1)] = "reference_answer"
    refusal_column: Annotated[str, Field(min_length=1)] = "expect_refusal"
    pass_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75


class RootCauseConfig(_Base):
    segment_by: SegmentBy = SegmentBy.BAND
    max_segments: Annotated[int, Field(ge=1, le=20)] = 6
    reasons_per_segment: Annotated[int, Field(ge=1, le=20)] = 5
    complaint_samples_per_segment: Annotated[int, Field(ge=0, le=100)] = 20
    complaint_text_column: str | None = None
    tone: Annotated[str, Field(min_length=1)] = "neutral_business"
    require_human_review: bool = False


class CopyLimits(_Base):
    sms_chars: Annotated[int, Field(ge=1, le=1600)] = 160
    whatsapp_chars: Annotated[int, Field(ge=1, le=4096)] = 1024
    email_subject_chars: Annotated[int, Field(ge=1, le=200)] = 60
    email_body_words: Annotated[int, Field(ge=1, le=1000)] = 150


class RequiredLines(_Base):
    sms: Annotated[str, Field(min_length=1)] = "Reply STOP to opt out"
    whatsapp: Annotated[str, Field(min_length=1)] = "Reply STOP to opt out"
    email: Annotated[str, Field(min_length=1)] = "unsubscribe_link"

    def for_channel(self, channel: Channel) -> str:
        """The line a message on `channel` must end with (pure; the copywriter and guardrails share it)."""
        return {Channel.SMS: self.sms, Channel.WHATSAPP: self.whatsapp, Channel.EMAIL: self.email}[channel]


class CampaignCopyConfig(_Base):
    """Spelled in full because a pydantic field called `copy` shadows `BaseModel.copy` (DEC-201)."""

    variants_per_band: Annotated[int, Field(ge=1, le=4)] = 2
    channels: tuple[Channel, ...] = (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP)
    bands_to_write: tuple[str, ...] = ("High", "Medium")
    allowed_fields: tuple[str, ...] = (
        "first_name",
        "plan_type",
        "tenure_months",
        "last_offer",
        "band",
    )
    banned_claims: tuple[str, ...] = ()
    tone: Annotated[str, Field(min_length=1)] = "warm_concise"
    brand_name: str = ""
    require_human_review: bool = True
    limits: CopyLimits = CopyLimits()
    required_lines: RequiredLines = RequiredLines()

    @field_validator("channels")
    @classmethod
    def _channels(cls, v: tuple[Channel, ...]) -> tuple[Channel, ...]:
        if not v:
            raise ConfigError(
                "COPY_NO_CHANNELS",
                "generative.campaign_copy.channels must name at least one channel.",
                path="generative.campaign_copy.channels",
            )
        if len(set(v)) != len(v):
            raise ConfigError(
                "COPY_DUPLICATE_CHANNEL",
                "generative.campaign_copy.channels names the same channel twice.",
                path="generative.campaign_copy.channels",
            )
        return v

    @field_validator("allowed_fields")
    @classmethod
    def _allowed_fields(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # Every entry becomes a `{{name}}` placeholder rendered by jinja2; one that is not an
        # identifier could not be rendered, and an empty list means no template can say anything.
        if not v:
            raise ConfigError(
                "COPY_NO_ALLOWED_FIELDS",
                "generative.campaign_copy.allowed_fields must list at least one field.",
                path="generative.campaign_copy.allowed_fields",
            )
        for name in v:
            if not _IDENTIFIER.fullmatch(name):
                raise ConfigError(
                    "COPY_FIELD_NOT_AN_IDENTIFIER",
                    f"generative.campaign_copy.allowed_fields entry {name!r} is not a usable placeholder name.",
                    path="generative.campaign_copy.allowed_fields",
                )
        return v


class GenerativeConfig(_Base):
    kind: GenerativeKind = GenerativeKind.NONE
    llm: LlmConfig = LlmConfig()
    budget: BudgetConfig = BudgetConfig()
    rag: RagConfig = RagConfig()
    knowledge_base: KnowledgeBaseConfig = KnowledgeBaseConfig()
    reference_set: ReferenceSetConfig = ReferenceSetConfig()
    root_cause: RootCauseConfig = RootCauseConfig()
    campaign_copy: CampaignCopyConfig = CampaignCopyConfig()

    @property
    def enabled(self) -> bool:
        """True when this use case turns a generative flow on; false leaves the block unread."""
        return self.kind is not GenerativeKind.NONE


# ---------------------------------------------------------------------------
# 4.6 KPI formula grammar (DEC-007)
# ---------------------------------------------------------------------------
_KPI_COUNT_ROWS: Final[re.Pattern[str]] = re.compile(r"^count_rows\(\s*\)$")
_KPI_COUNT_BANDS: Final[re.Pattern[str]] = re.compile(r"^count_where_band_in\(\s*(\[.*\])\s*\)$", re.DOTALL)
_KPI_SUM_BANDS: Final[re.Pattern[str]] = re.compile(
    r"^sum_where_band_in\(\s*(\"[^\"]*\"|'[^']*')\s*,\s*(\[.*\])\s*\)$", re.DOTALL
)


def _kpi_band_list(raw: str, formula: str) -> tuple[str, ...]:
    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError) as exc:
        raise ConfigError(
            "KPI_FORMULA_UNPARSEABLE",
            f"{formula!r} does not contain a valid list of band names.",
            path="output.kpi.formula",
        ) from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(b, str) and b for b in parsed):
        raise ConfigError(
            "KPI_FORMULA_UNPARSEABLE",
            f'{formula!r} must name at least one band, e.g. count_where_band_in(["High"]).',
            path="output.kpi.formula",
        )
    bands: list[str] = [str(b) for b in parsed]
    return tuple(bands)


class KpiFormula(_Base):
    fn: Literal["count_rows", "count_where_band_in", "sum_where_band_in"]
    column: str | None = None
    bands: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> KpiFormula:
        formula = text.strip()
        if _KPI_COUNT_ROWS.match(formula):
            return cls(fn="count_rows")
        match = _KPI_COUNT_BANDS.match(formula)
        if match:
            return cls(fn="count_where_band_in", bands=_kpi_band_list(match.group(1), formula))
        match = _KPI_SUM_BANDS.match(formula)
        if match:
            return cls(
                fn="sum_where_band_in",
                column=str(ast.literal_eval(match.group(1))),
                bands=_kpi_band_list(match.group(2), formula),
            )
        raise ConfigError(
            "KPI_FORMULA_UNPARSEABLE",
            f"{text!r} is not a KPI formula. Use count_rows(), count_where_band_in([...]) "
            'or sum_where_band_in("column", [...]).',
            path="output.kpi.formula",
        )


class KpiConfig(_Base):
    label: Annotated[str, Field(min_length=1)]
    formula: Annotated[str, Field(min_length=1)]

    @field_validator("formula")
    @classmethod
    def _parses(cls, v: str) -> str:
        KpiFormula.parse(v)
        return v

    @property
    def parsed(self) -> KpiFormula:
        return KpiFormula.parse(self.formula)


class OutputConfig(_Base):
    kpi: KpiConfig


class ModeCopy(_Base):
    train: Annotated[str, Field(min_length=1)]
    score: Annotated[str, Field(min_length=1)]


class PageTitles(_Base):
    data: Annotated[str, Field(min_length=1)]
    model: Annotated[str, Field(min_length=1)]
    output: Annotated[str, Field(min_length=1)]


class UiConfig(_Base):
    target_label: str = "Target column"
    mode_labels: ModeCopy
    mode_help: ModeCopy
    dataset_hint: ModeCopy
    columns_hint: ModeCopy
    run_button: ModeCopy
    pages: PageTitles


class TemplateColumn(_Base):
    name: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    role: ColumnRole
    type: ColumnType
    description: Annotated[str, Field(min_length=1)]
    examples: tuple[str, str, str, str, str]


class TemplateConfig(_Base):
    columns: tuple[TemplateColumn, ...] = ()

    @field_validator("columns")
    @classmethod
    def _roles(cls, v: tuple[TemplateColumn, ...]) -> tuple[TemplateColumn, ...]:
        names = [column.name for column in v]
        if len(set(names)) != len(names):
            raise ConfigError(
                "TEMPLATE_DUPLICATE_COLUMN",
                "template.columns lists the same column name twice.",
                path="template.columns",
            )
        if not v:
            return v
        keys = [column for column in v if column.role is ColumnRole.PRIMARY_KEY]
        if not keys:
            raise ConfigError(
                "TEMPLATE_NO_PRIMARY_KEY",
                "template.columns needs exactly one column with role 'primary_key'.",
                path="template.columns",
            )
        if len(keys) > 1:
            raise ConfigError(
                "TEMPLATE_MANY_PRIMARY_KEYS",
                f"template.columns has {len(keys)} primary_key columns; exactly one is allowed.",
                path="template.columns",
            )
        targets = [column for column in v if column.role is ColumnRole.TARGET]
        if len(targets) > 1:
            raise ConfigError(
                "TEMPLATE_MANY_TARGETS",
                f"template.columns has {len(targets)} target columns; at most one is allowed.",
                path="template.columns",
            )
        return v

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def by_role(self, role: ColumnRole) -> tuple[TemplateColumn, ...]:
        return tuple(column for column in self.columns if column.role is role)

    @property
    def primary_key(self) -> TemplateColumn | None:
        keys = self.by_role(ColumnRole.PRIMARY_KEY)
        return keys[0] if keys else None


# ---------------------------------------------------------------------------
# 4.4 The top-level documents
# ---------------------------------------------------------------------------
def _catalog_from_context(info: ValidationInfo) -> Catalog | None:
    """The catalog a loader passed as validation context, if it passed one (DEC-038)."""
    context = info.context
    if isinstance(context, Mapping):
        catalog = context.get("catalog")
        if isinstance(catalog, Catalog):
            return catalog
    return None


class UseCaseConfig(_Base):
    """A fully merged, validated use case. This is what the whole engine consumes."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    name: Annotated[str, Field(min_length=1)]
    description: str = ""
    lifecycle_stage: Annotated[str, Field(min_length=1)]
    ai_type: AiType = AiType.PREDICTIVE
    problem_type: ProblemType = ProblemType.BINARY_CLASSIFICATION
    entity: Annotated[str, Field(min_length=1)] = "customer"
    target: TargetConfig = TargetConfig()
    primary_key_hints: tuple[str, ...] = ()
    time_column_hints: tuple[str, ...] = ()
    validation: ValidationConfig = ValidationConfig()
    prepare: PrepareConfig = PrepareConfig()
    split: SplitConfig = SplitConfig()
    features: FeaturesConfig = FeaturesConfig()
    model_search: ModelSearchConfig = ModelSearchConfig()
    evaluation: EvaluationConfig = EvaluationConfig()
    actions: ActionsConfig = ActionsConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    governance: GovernanceConfig = GovernanceConfig()
    generative: GenerativeConfig = GenerativeConfig()
    output: OutputConfig
    ui: UiConfig
    template: TemplateConfig = TemplateConfig()
    # --- Phase 2 (onboarding) ------------------------------------------------------------------
    # The only declaration this branch adds above its PARALLEL_WORK_PROTOCOL.md §4 block, and it is
    # here because it cannot be anywhere else: `_Base` forbids unknown keys, so a use-case YAML
    # cannot carry the sections §3's ownership table assigns to Phase 2 until the model has fields
    # for them. All four are optional and defaulted, so every existing config validates unchanged
    # and no other branch is affected. Their types are defined in the PHASE-2 block at the foot of
    # this file; the cross-field checks live in `engine/onboarding/roles.py`, which this file does
    # not import. Announced in `docs/CROSS_BRANCH_REQUESTS.md`.
    # The annotations are forward references and the defaults are factories, because both name
    # classes defined below; `UseCaseConfig.model_rebuild()` at the foot of the PHASE-2 block
    # resolves them once those classes exist.
    standard_schema: StandardSchemaConfig = Field(default_factory=lambda: StandardSchemaConfig())
    suggested_features: tuple[FeatureDef, ...] = ()
    label: LabelDefinition | None = None
    onboarding: OnboardingConfig = Field(default_factory=lambda: OnboardingConfig())
    # --- Phase 3b (uplift) ---------------------------------------------------------------------
    # The one declaration Phase 3b adds above its block, for Phase 2's reason: `_Base` forbids
    # unknown keys, so a use case cannot carry an `uplift:` section until the model has a field for
    # it. Defaulted, and read only when `problem_type` is `uplift`, so no other path moves. The type
    # lives in `engine/uplift/config.py`, which imports nothing from this file (DEC-601).
    uplift: UpliftConfig = UpliftConfig()

    _catalog: Catalog | None = PrivateAttr(default=None)

    @property
    def catalog(self) -> Catalog:
        """The catalog this document was validated against (DEC-038).

        The loaders validate against the `engine.yaml` of the root they read, so labels, dependency
        checks and the properties below agree with the file that was loaded. A document validated
        without a root (a `UseCaseConfig(...)` literal, or `run_config.json` read back) carries the
        default root's catalog.
        """
        return self._catalog if self._catalog is not None else get_catalog()

    @model_validator(mode="after")
    def _cross_field(self, info: ValidationInfo) -> Self:
        # pydantic runs an after-validator again whenever an existing instance is nested into another
        # model (ResolvedConfig, the API bodies), then with that call's context; a catalog is taken
        # from the context when one is given and kept from the first validation otherwise.
        context_catalog = _catalog_from_context(info)
        if context_catalog is not None:
            self._catalog = context_catalog
        elif self._catalog is None:
            self._catalog = get_catalog()
        if self.ai_type is not AiType.GENERATIVE and self.target.column is None:
            raise ConfigError(
                "TARGET_COLUMN_REQUIRED",
                f"{self.name} predicts something, so target.column must name the outcome column.",
                path="target.column",
            )
        catalog = self.catalog
        allowed_metrics = catalog.metrics_for(self.problem_type)
        if allowed_metrics:
            wrong = [m.value for m in self.model_search.metric_choices if m not in allowed_metrics]
            if wrong:
                raise ConfigError(
                    "METRIC_NOT_FOR_PROBLEM",
                    f"{', '.join(wrong)} cannot score a {self.problem_type.value} problem.",
                    path="model_search.metric_choices",
                )
        self._check_template()
        used = self._reserved_columns()
        for name in self.prepare.exclude_columns:
            if name in used:
                raise ConfigError(
                    "COLUMN_EXCLUDED_AND_USED",
                    f"{name!r} is excluded from the features but is also used as {used[name]}.",
                    path="prepare.exclude_columns",
                )
        kpi = self.output.kpi.parsed
        band_names = {band.name for band in self.actions.bands}
        unknown_bands = [b for b in kpi.bands if b not in band_names]
        if unknown_bands:
            raise ConfigError(
                "KPI_UNKNOWN_BAND",
                f"output.kpi.formula names band(s) {', '.join(unknown_bands)} that actions.bands does not define.",
                path="output.kpi.formula",
            )
        if kpi.column is not None and self.template.columns and kpi.column not in self.template.column_names:
            raise ConfigError(
                "KPI_UNKNOWN_COLUMN",
                f"output.kpi.formula sums {kpi.column!r}, which is not a template column.",
                path="output.kpi.formula",
            )
        self._check_generative(band_names)
        return self

    def _check_generative(self, band_names: set[str]) -> None:
        """Tie the generative block to the rest of the document, or leave it alone when it is off.

        Three things can disagree and only be found out at run time otherwise: a flow switched on
        under `ai_type: predictive`, copy written for a band the actions block does not define, and
        a complaint column that is not in the template.
        """
        generative = self.generative
        if not generative.enabled:
            return
        if self.ai_type is AiType.PREDICTIVE:
            raise ConfigError(
                "GENERATIVE_ON_A_PREDICTIVE_USE_CASE",
                f"generative.kind is {generative.kind.value!r}, so ai_type must be generative or hybrid.",
                path="generative.kind",
            )
        if generative.kind is GenerativeKind.CAMPAIGN_COPY:
            unknown = [name for name in generative.campaign_copy.bands_to_write if name not in band_names]
            if unknown:
                raise ConfigError(
                    "COPY_UNKNOWN_BAND",
                    f"generative.campaign_copy.bands_to_write names band(s) "
                    f"{', '.join(unknown)} that actions.bands does not define.",
                    path="generative.campaign_copy.bands_to_write",
                )
        column = generative.root_cause.complaint_text_column
        if column is not None and self.template.columns and column not in self.template.column_names:
            raise ConfigError(
                "COMPLAINT_COLUMN_MISSING",
                f"generative.root_cause.complaint_text_column {column!r} is not a template column.",
                path="generative.root_cause.complaint_text_column",
            )

    def _check_template(self) -> None:
        if not self.template.columns:
            return
        targets = self.template.by_role(ColumnRole.TARGET)
        target_name = targets[0].name if targets else None
        if target_name != self.target.column:
            raise ConfigError(
                "TEMPLATE_TARGET_MISMATCH",
                f"The template's target column ({target_name or 'none'}) is not "
                f"target.column ({self.target.column or 'none'}).",
                path="template.columns",
            )
        if self.split.type is SplitType.TIME_BASED and self.split.time_column is not None:
            time_names = {column.name for column in self.template.by_role(ColumnRole.TIME)}
            if self.standard_schema.columns:
                # A use case that can be built from raw tables has a second shape besides the
                # prepared file: the built dataset, whose as-of date is its time column by
                # definition. "Use this dataset" splits a periodic one on it (Plan A M35), and a
                # prepared-file template - a public file with no date column, say - need not name
                # it. Whether an upload has the column is `check_time_column_missing`'s job.
                time_names.add(self.standard_schema.snapshot_column)
            if self.split.time_column not in time_names:
                raise ConfigError(
                    "TEMPLATE_TIME_MISSING",
                    f"split.time_column {self.split.time_column!r} is not a template column with role 'time'.",
                    path="split.time_column",
                )
        if (
            self.governance.consent_column is not None
            and self.governance.consent_column not in self.template.column_names
        ):
            raise ConfigError(
                "TEMPLATE_CONSENT_MISSING",
                f"governance.consent_column {self.governance.consent_column!r} is not a template column.",
                path="governance.consent_column",
            )

    def _reserved_columns(self) -> dict[str, str]:
        reserved: dict[str, str] = {}
        for column, role in (
            (self.target.column, "the target column"),
            (self.split.time_column, "the time column"),
            (self.split.group_column, "the group column"),
            (self.evaluation.fairness_column, "the fairness column"),
            (self.governance.consent_column, "the consent column"),
            (self.actions.suppression.opt_out_column, "the opt-out column"),
            (self.actions.suppression.recently_contacted_column, "the recent-contact column"),
        ):
            if column is not None and column not in reserved:
                reserved[column] = role
        return reserved

    @property
    def trainable_in_phase_1(self) -> bool:
        return self.ai_type is not AiType.GENERATIVE and self.catalog.problem_types[self.problem_type].enabled

    @property
    def marker(self) -> Literal["P", "G", "H"]:
        return self.catalog.ai_types[self.ai_type].marker

    @property
    def template_stem(self) -> str:
        return self.id.replace("-", "_")


class IndustryUseCaseRef(_Base):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    status: UseCaseStatus = UseCaseStatus.AVAILABLE
    name: str | None = None
    description: str | None = None


class IndustryStage(_Base):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    name: Annotated[str, Field(min_length=1)]
    ai_type: AiType
    use_cases: tuple[IndustryUseCaseRef, ...]


class IndustryConfig(_Base):
    schema_version: Literal[1]
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    name: Annotated[str, Field(min_length=1)]
    journey_label: str = "Customer Lifecycle"
    stages: tuple[IndustryStage, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        stage_ids: set[str] = set()
        use_case_ids: set[str] = set()
        for stage in self.stages:
            if stage.id in stage_ids:
                raise ConfigError(
                    "INDUSTRY_DUPLICATE_STAGE",
                    f"Stage {stage.id!r} appears twice.",
                    path="stages",
                )
            stage_ids.add(stage.id)
            for ref in stage.use_cases:
                if ref.id in use_case_ids:
                    raise ConfigError(
                        "INDUSTRY_DUPLICATE_USE_CASE",
                        f"Use case {ref.id!r} appears under more than one stage.",
                        path=f"stages.{stage.id}.use_cases",
                    )
                use_case_ids.add(ref.id)
                if ref.status is UseCaseStatus.PLANNED and not (ref.name and ref.description):
                    raise ConfigError(
                        "INDUSTRY_PLANNED_NEEDS_NAME",
                        f"Planned use case {ref.id!r} must carry its own name and description.",
                        path=f"stages.{stage.id}.use_cases.{ref.id}",
                    )
                if ref.status is UseCaseStatus.AVAILABLE and (ref.name or ref.description):
                    raise ConfigError(
                        "INDUSTRY_NAME_ON_AVAILABLE",
                        f"Use case {ref.id!r} is available, so its name and description come from its own file.",
                        path=f"stages.{stage.id}.use_cases.{ref.id}",
                    )
        return self

    def all_refs(self) -> tuple[tuple[IndustryStage, IndustryUseCaseRef], ...]:
        return tuple((stage, ref) for stage in self.stages for ref in stage.use_cases)


class EngineConfig(_Base):
    schema_version: Literal[1]
    catalog: Catalog
    defaults: dict[str, Any]


# ---------------------------------------------------------------------------
# 4.5 Loaders, merge and overrides
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_ROOT: Final[Path] = DEFAULT_CONFIG_DIR
CONFIG_DIR_ENV_VAR: Final[str] = ENV_VARS["config_dir"]

_ENGINE_CACHE: dict[Path, EngineConfig] = {}


def config_root(root: Path | None = None) -> Path:
    """`root` argument, else env `MARKETING_AI_CONFIG_DIR`, else `DEFAULT_CONFIG_ROOT`.

    The environment is read through `engine.settings` rather than directly, so every variable the
    engine honours is listed in one place; `Settings.from_env` resolves a directory it was given
    and leaves the default alone, which is what this function did before.
    """
    if root is not None:
        return Path(root).resolve()
    return settings().config_dir or DEFAULT_CONFIG_ROOT


_SAFE_LOADER: Final[Any] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
"""libyaml's C parser when PyYAML was built with it, else the pure-Python one. Both build the same
objects through `SafeConstructor`; the C one is about eight times faster, which matters once
`GET /industries` validates every use case of every industry on each request (DEC-800)."""


def load_yaml(path: Path) -> dict[str, Any]:
    """`yaml.safe_load` with the three file-level `ConfigError` codes."""
    if not path.is_file():
        raise ConfigError("CONFIG_NOT_FOUND", f"No configuration file at {path}.", path=str(path))
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_SAFE_LOADER)
    except yaml.YAMLError as exc:
        raise ConfigError("CONFIG_YAML_ERROR", f"{path} is not valid YAML: {exc}.", path=str(path)) from exc
    if not isinstance(loaded, dict):
        raise ConfigError(
            "CONFIG_NOT_A_MAPPING",
            f"{path} must contain a mapping of settings, not {type(loaded).__name__}.",
            path=str(path),
        )
    document: dict[str, Any] = loaded
    return document


def load_engine_config(root: Path | None = None) -> EngineConfig:
    """`configs/engine.yaml`, cached per root."""
    base = config_root(root)
    cached = _ENGINE_CACHE.get(base)
    if cached is not None:
        return cached
    document = load_yaml(base / "engine.yaml")
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            "ENGINE_SCHEMA_VERSION",
            f"engine.yaml declares schema_version {version!r}; this engine reads {SCHEMA_VERSION}.",
            path="schema_version",
        )
    try:
        engine_config = EngineConfig.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = _dotted_loc(first["loc"])
        raise ConfigError("CONFIG_INVALID", f"{dotted}: {first['msg']}.", path=dotted) from exc
    _ENGINE_CACHE[base] = engine_config
    return engine_config


def get_catalog(root: Path | None = None) -> Catalog:
    return load_engine_config(root).catalog


#: The industry the overview opens on and `load_industry` reads when given no id. One file per
#: industry sits in `configs/industries/` (DEC-085); telecom is the product's own journey and stays
#: the default, so adding an industry adds a choice rather than changing what a user first sees.
DEFAULT_INDUSTRY: Final[str] = "telecom"


def list_industries(root: Path | None = None) -> tuple[str, ...]:
    base = config_root(root) / "industries"
    if not base.is_dir():
        return ()
    return tuple(sorted(path.stem for path in base.glob("*.yaml")))


def list_use_case_ids(root: Path | None = None) -> tuple[str, ...]:
    base = config_root(root) / "use_cases"
    if not base.is_dir():
        return ()
    ids: list[str] = []
    for path in sorted(base.glob("*.yaml")):
        document = load_yaml(path)
        declared = document.get("id")
        if declared != path.stem.replace("_", "-"):
            raise ConfigError(
                "USE_CASE_ID_MISMATCH",
                f"{path.name} declares id {declared!r}; the file name says {path.stem.replace('_', '-')!r}.",
                path=str(path),
            )
        ids.append(str(declared))
    return tuple(sorted(ids))


def use_case_path(use_case_id: str, root: Path | None = None) -> Path:
    return config_root(root) / "use_cases" / f"{use_case_id.replace('-', '_')}.yaml"


def load_use_case_document(use_case_id: str, root: Path | None = None) -> dict[str, Any]:
    """`deep_merge(engine.defaults, use-case file)` as a raw dict."""
    path = use_case_path(use_case_id, root)
    if not path.is_file():
        raise ConfigError(
            "USE_CASE_NOT_FOUND",
            f"No use case {use_case_id!r} in {path.parent}.",
            path=str(path),
        )
    defaults = load_engine_config(root).defaults
    return deep_merge(defaults, load_yaml(path))


def load_use_case(use_case_id: str, root: Path | None = None) -> UseCaseConfig:
    """The merged, validated use case of `root`, checked against that root's own catalog (DEC-038)."""
    document = load_use_case_document(use_case_id, root)
    config = _validate_use_case(document, catalog=get_catalog(root))
    check_dependencies(config)
    return config


def _validate_use_case(document: Mapping[str, Any], *, catalog: Catalog) -> UseCaseConfig:
    """`UseCaseConfig` validated against `catalog`; pydantic's first error becomes `CONFIG_INVALID`."""
    try:
        return UseCaseConfig.model_validate(dict(document), context={"catalog": catalog})
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = _dotted_loc(first["loc"])
        raise ConfigError("CONFIG_INVALID", f"{dotted}: {first['msg']}.", path=dotted) from exc


def _dotted_loc(loc: Sequence[str | int]) -> str:
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered = f"{rendered}.{part}" if rendered else str(part)
    return rendered


def load_all_use_cases(root: Path | None = None) -> dict[str, UseCaseConfig]:
    return {use_case_id: load_use_case(use_case_id, root) for use_case_id in list_use_case_ids(root)}


def load_industry(industry_id: str = DEFAULT_INDUSTRY, root: Path | None = None) -> IndustryConfig:
    """Validates the file AND the cross-file rules of the industry document."""
    base = config_root(root)
    path = base / "industries" / f"{industry_id}.yaml"
    industry = IndustryConfig.model_validate(load_yaml(path))
    available_ids = set(list_use_case_ids(root))
    for stage, ref in industry.all_refs():
        if ref.status is UseCaseStatus.AVAILABLE:
            if ref.id not in available_ids:
                raise ConfigError(
                    "INDUSTRY_USE_CASE_MISSING",
                    f"{industry_id}.yaml lists {ref.id!r} as available but there is no use-case file for it.",
                    path=str(path),
                )
            config = load_use_case(ref.id, root)
            if config.lifecycle_stage != stage.name:
                raise ConfigError(
                    "INDUSTRY_STAGE_MISMATCH",
                    f"{ref.id!r} has lifecycle_stage {config.lifecycle_stage!r} "
                    f"but sits under stage {stage.name!r}.",
                    path=str(path),
                )
        elif use_case_path(ref.id, root).is_file():
            raise ConfigError(
                "INDUSTRY_PLANNED_BUT_PRESENT",
                f"{ref.id!r} is marked planned but a use-case file exists; mark it available.",
                path=str(path),
            )
    return industry


def check_dependencies(config: UseCaseConfig) -> None:
    """Raise `MODEL_FAMILY_UNAVAILABLE` when a selected model family needs a module that is not installed."""
    catalog = config.catalog
    missing = catalog.missing_requirements(config.model_search.candidates)
    for family in config.model_search.candidates:
        if family in missing:
            raise ConfigError(
                "MODEL_FAMILY_UNAVAILABLE",
                f"The {catalog.family_label(family)} model needs the optional 'nn' extra. "
                "Run `make setup EXTRAS=nn`, or deselect it.",
                path="model_search.candidates",
            )


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Deep for mappings, replace for everything else; neither argument is mutated (DEC-002)."""
    merged: dict[str, Any] = {}
    for key, value in base.items():
        if key in overlay:
            other = overlay[key]
            if isinstance(value, Mapping) and isinstance(other, Mapping):
                merged[key] = deep_merge(value, other)
            else:
                merged[key] = copy.deepcopy(other)
        else:
            merged[key] = copy.deepcopy(value)
    for key, value in overlay.items():
        if key not in base:
            merged[key] = copy.deepcopy(value)
    return merged


_PATH_KEY: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_]*(\[\d+\])?(\.[a-z_][a-z0-9_]*(\[\d+\])?)*$")
_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^([a-z_][a-z0-9_]*)(?:\[(\d+)\])?$")

PathToken = str | int


def _key_tokens(key: str | int) -> tuple[PathToken, ...]:
    if isinstance(key, int):
        return (key,)
    if key.isdigit():
        return (int(key),)
    if not _PATH_KEY.match(key):
        raise ConfigError("OVERRIDE_BAD_PATH", f"{key!r} is not a valid settings path.", path=str(key))
    tokens: list[PathToken] = []
    for segment in key.split("."):
        match = _SEGMENT.match(segment)
        if match is None:  # pragma: no cover - guarded by _PATH_KEY
            raise ConfigError("OVERRIDE_BAD_PATH", f"{key!r} is not a valid settings path.", path=str(key))
        tokens.append(match.group(1))
        if match.group(2) is not None:
            tokens.append(int(match.group(2)))
    return tuple(tokens)


def _render_tokens(tokens: Sequence[PathToken]) -> str:
    rendered = ""
    for token in tokens:
        if isinstance(token, int):
            rendered += f"[{token}]"
        else:
            rendered = f"{rendered}.{token}" if rendered else token
    return rendered


def _collect_override_leaves(
    node: Mapping[Any, Any], prefix: tuple[PathToken, ...], out: list[tuple[tuple[PathToken, ...], Any]]
) -> None:
    for key, value in node.items():
        tokens = prefix + _key_tokens(key)
        if isinstance(value, Mapping) and value:
            _collect_override_leaves(value, tokens, out)
        else:
            out.append((tokens, value))


def expand_paths(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise dotted keys, nested mappings and index patches into one nested document."""
    leaves: list[tuple[tuple[PathToken, ...], Any]] = []
    _collect_override_leaves(patch, (), leaves)
    seen: dict[tuple[PathToken, ...], None] = {}
    for tokens, _ in leaves:
        if tokens in seen:
            raise ConfigError(
                "OVERRIDE_CONFLICTING_PATHS",
                f"{_render_tokens(tokens)!r} is set twice in one request.",
                path=_render_tokens(tokens),
            )
        seen[tokens] = None
    ordered = list(seen)
    for outer in ordered:
        for inner in ordered:
            if outer is not inner and len(outer) < len(inner) and inner[: len(outer)] == outer:
                raise ConfigError(
                    "OVERRIDE_PREFIX_COLLISION",
                    f"{_render_tokens(outer)!r} and {_render_tokens(inner)!r} cannot both be set; "
                    "one contains the other.",
                    path=_render_tokens(outer),
                )
    expanded: dict[str, Any] = {}
    for tokens, value in leaves:
        cursor: dict[Any, Any] = expanded
        for token in tokens[:-1]:
            nxt = cursor.get(token)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[token] = nxt
            cursor = nxt
        cursor[tokens[-1]] = copy.deepcopy(value)
    return expanded


def _walk_leaves(value: Any, prefix: str, out: list[str]) -> None:
    if isinstance(value, Mapping):
        if not value:
            if prefix:
                out.append(prefix)
            return
        for key, item in value.items():
            child = (
                f"{prefix}[{key}]" if isinstance(key, int) else (f"{prefix}.{key}" if prefix else str(key))
            )
            _walk_leaves(item, child, out)
        return
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, Mapping) for item in value):
        for index, item in enumerate(value):
            _walk_leaves(item, f"{prefix}[{index}]", out)
        return
    if prefix:
        out.append(prefix)


def leaf_paths(document: Mapping[str, Any]) -> tuple[str, ...]:
    """Dotted leaves of a nested or expanded document, with `[i]` for list indices."""
    out: list[str] = []
    _walk_leaves(document, "", out)
    return tuple(out)


IMMUTABLE_PATHS: Final[frozenset[str]] = frozenset(
    {"id", "name", "description", "lifecycle_stage", "ai_type", "entity", "template", "ui", "output"}
)
EXTRA_OVERRIDABLE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "problem_type",
        "target.column",
        "target.positive_label",
        "split.time_column",
        "prepare.exclude_columns",
        "validation.acknowledged",
        "model_search.candidates",
        *UPLIFT_OVERRIDABLE_PATHS,  # Phase 3b (DEC-601): the uplift block is per-run, like target.column
    }
)
_EMPTY_STRING_IS_NONE: Final[frozenset[str]] = frozenset(
    {
        "split.time_column",
        "split.group_column",
        "evaluation.fairness_column",
        "governance.consent_column",
        "target.positive_label",
    }
)


def overridable_paths(config: UseCaseConfig) -> frozenset[str]:
    """Every path `advanced_settings_schema(config)` emits, plus `EXTRA_OVERRIDABLE_PATHS`."""
    schema = advanced_settings_schema(config)
    return frozenset(
        {field.path for stage in schema.stages for field in stage.fields} | EXTRA_OVERRIDABLE_PATHS
    )


def _coerce_optional_strings(node: Mapping[Any, Any], prefix: str) -> dict[Any, Any]:
    coerced: dict[Any, Any] = {}
    for key, value in node.items():
        child = f"{prefix}[{key}]" if isinstance(key, int) else (f"{prefix}.{key}" if prefix else str(key))
        if isinstance(value, Mapping):
            coerced[key] = _coerce_optional_strings(value, child)
        elif value == "" and child in _EMPTY_STRING_IS_NONE:
            coerced[key] = None
        else:
            coerced[key] = value
    return coerced


def _is_index_patch(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(value) and all(isinstance(key, int) for key in value)


def _patch_list(base: Any, patch: Mapping[int, Any], path: str) -> list[Any]:
    if not isinstance(base, (list, tuple)):
        raise ConfigError(
            "OVERRIDE_INDEX_OUT_OF_RANGE",
            f"{path} is not a list, so it cannot be patched by index.",
            path=path,
        )
    items: list[Any] = [copy.deepcopy(item) for item in base]
    for index, value in patch.items():
        if index < 0 or index >= len(items):
            raise ConfigError(
                "OVERRIDE_INDEX_OUT_OF_RANGE",
                f"{path} has {len(items)} entries, so [{index}] cannot be set.",
                path=f"{path}[{index}]",
            )
        current = items[index]
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            items[index] = _merge_overrides(current, value, f"{path}[{index}]")
        else:
            items[index] = copy.deepcopy(value)
    return items


def _merge_overrides(base: Mapping[str, Any], overlay: Mapping[Any, Any], prefix: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        if key in overlay:
            other = overlay[key]
            child = f"{prefix}.{key}" if prefix else str(key)
            if _is_index_patch(other):
                merged[key] = _patch_list(value, other, child)
            elif isinstance(value, Mapping) and isinstance(other, Mapping):
                merged[key] = _merge_overrides(value, other, child)
            else:
                merged[key] = copy.deepcopy(other)
        else:
            merged[key] = copy.deepcopy(value)
    for key, value in overlay.items():
        if key not in base:
            child = f"{prefix}.{key}" if prefix else str(key)
            if _is_index_patch(value):
                raise ConfigError(
                    "OVERRIDE_INDEX_OUT_OF_RANGE",
                    f"{child} does not exist, so it cannot be patched by index.",
                    path=child,
                )
            merged[str(key)] = copy.deepcopy(value)
    return merged


def apply_overrides(
    base: Mapping[str, Any], overrides: Mapping[str, Any], *, allowed: frozenset[str]
) -> dict[str, Any]:
    """Expand, authorise and merge run overrides into a raw config document (config units only)."""
    expanded = expand_paths(overrides)
    for leaf in leaf_paths(expanded):
        root = leaf.split(".")[0].split("[")[0]
        if root in IMMUTABLE_PATHS:
            raise ConfigError(
                "OVERRIDE_IMMUTABLE_FIELD",
                f"{leaf!r} belongs to the use case, not to a run, and cannot be overridden.",
                path=leaf,
            )
        if leaf not in allowed:
            near = difflib.get_close_matches(leaf, sorted(allowed), n=3, cutoff=0.0)
            raise ConfigError(
                "OVERRIDE_UNKNOWN_PATH",
                f"{leaf!r} is not a setting that can be changed per run. Nearest: {', '.join(near)}.",
                path=leaf,
            )
    coerced = _coerce_optional_strings(expanded, "")
    return _merge_overrides(base, coerced, "")


class RunOverrides(RootModel[dict[str, Any]]):
    """The raw `overrides` object of `POST /runs`, in either wire shape."""

    def expanded(self) -> dict[str, Any]:
        return expand_paths(self.root)

    def touched_paths(self) -> tuple[str, ...]:
        return tuple(sorted(leaf_paths(self.expanded())))


Source = Literal["engine", "use_case", "override", "derived"]
"""Where a leaf of a resolved config came from: a layer of the merge, or the engine itself (DEC-039)."""


class ResolvedConfig(_Base):
    """Serialised verbatim as `run_config.json` (plan section 5).

    `sources` maps every dotted leaf of `config` to the layer that set it; `"derived"` marks a value the
    engine chose after an override, such as the metric choices of a switched problem type (DEC-039).
    """

    schema_version: int
    use_case_id: str
    resolved_at: datetime
    config: UseCaseConfig
    overrides_applied: dict[str, Any]
    sources: dict[str, Source]
    warnings: tuple[str, ...] = ()


def resolve_config(
    use_case_id: str,
    overrides: RunOverrides | Mapping[str, Any] | None = None,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> ResolvedConfig:
    """Engine defaults + use-case file + run overrides -> the one document every run is reproducible from.

    Every validation runs against the catalog of `root` (DEC-038). A `problem_type` override that leaves
    the use case's metric choices unfit for the new type has them re-derived from that catalog before the
    merged document is validated (DEC-039); the touched leaves are `"derived"` in `sources`.
    """
    catalog = get_catalog(root)
    raw = load_use_case_document(use_case_id, root)
    base = _validate_use_case(raw, catalog=catalog)
    patch: Mapping[str, Any] = overrides.root if isinstance(overrides, RunOverrides) else (overrides or {})
    expanded = expand_paths(patch)
    merged = apply_overrides(raw, patch, allowed=overridable_paths(base)) if patch else raw
    warnings: list[str] = []
    document, derived, switch_note = _switch_metrics_for_problem_type(merged, leaf_paths(expanded), catalog)
    if switch_note is not None:
        warnings.append(switch_note)
    final = _validate_use_case(document, catalog=catalog)
    check_dependencies(final)
    if final.split.type is SplitType.TIME_BASED and final.split.time_column is None:
        warnings.append("split.type is time_based and no time column is set yet")
    return ResolvedConfig(
        schema_version=SCHEMA_VERSION,
        use_case_id=final.id,
        resolved_at=now or datetime.now(UTC),
        config=final,
        overrides_applied=expanded,
        sources=_sources(final, root, expanded, derived),
        warnings=tuple(warnings),
    )


def _switch_metrics_for_problem_type(
    document: Mapping[str, Any], override_leaves: Sequence[str], catalog: Catalog
) -> tuple[Mapping[str, Any], tuple[str, ...], str | None]:
    """Re-derive `model_search.metric_choices` (and `metric`) after a run switches the problem type.

    A use case's metric list is written for its own problem type, so once a run overrides
    `problem_type` (plan §6.3 "offer switch to regression", prototype "Metrics and models will switch
    to regression defaults") the list is replaced by the catalog's metrics for the new type, in catalog
    order, and `metric` by the first of them unless the run set `model_search.metric` itself; an
    explicit metric that does not fit is left for validation to reject (DEC-039). Returns the document
    to validate, the dotted paths that were derived and a warning line, or the document untouched.
    """
    raw_problem_type = document.get("problem_type")
    if not isinstance(raw_problem_type, str):
        return document, (), None  # not a problem type at all: validation reports it
    try:
        problem_type = ProblemType(raw_problem_type)
    except ValueError:
        return document, (), None
    allowed = catalog.metrics_for(problem_type)
    search = document.get("model_search")
    if not allowed or not isinstance(search, Mapping):
        return document, (), None
    choices = search.get("metric_choices")
    if not isinstance(choices, (list, tuple)):
        return document, (), None
    try:
        current = tuple(Metric(choice) for choice in choices)
    except ValueError:
        return document, (), None  # not metrics at all: validation reports it
    if all(metric in allowed for metric in current):
        return document, (), None
    switched: dict[str, Any] = {"metric_choices": [metric.value for metric in allowed]}
    derived = ["model_search.metric_choices"]
    note = (
        f"problem_type is {problem_type.value}, so model_search.metric_choices switched to the "
        f"{problem_type.value} defaults ({', '.join(switched['metric_choices'])})"
    )
    if "model_search.metric" not in override_leaves:
        switched["metric"] = allowed[0].value
        derived.append("model_search.metric")
        note += f" and model_search.metric to {allowed[0].value}"
    return deep_merge(document, {"model_search": switched}), tuple(derived), note


def _sources(
    final: UseCaseConfig, root: Path | None, expanded: Mapping[str, Any], derived: Sequence[str] = ()
) -> dict[str, Source]:
    engine_leaves = set(leaf_paths(load_engine_config(root).defaults))
    use_case_leaves = set(leaf_paths(load_yaml(use_case_path(final.id, root))))
    override_leaves = set(leaf_paths(expanded))
    derived_leaves = set(derived)
    sources: dict[str, Source] = {}
    for leaf in leaf_paths(final.model_dump(mode="json")):
        if leaf in derived_leaves:
            sources[leaf] = "derived"
        elif leaf in override_leaves:
            sources[leaf] = "override"
        elif leaf in use_case_leaves or _list_parent(leaf) in use_case_leaves:
            sources[leaf] = "use_case"
        elif leaf in engine_leaves or _list_parent(leaf) in engine_leaves:
            sources[leaf] = "engine"
        else:
            sources[leaf] = "engine"
    return sources


def _list_parent(leaf: str) -> str:
    """`actions.bands[0].name` -> `actions.bands`, so a whole-list layer claims its elements."""
    head, bracket, _ = leaf.partition("[")
    return head if bracket else leaf


# ---------------------------------------------------------------------------
# 4.7 advanced_settings_schema()
# ---------------------------------------------------------------------------
class FieldChoice(_Base):
    value: str | int | float | bool
    label: str
    enabled: bool = True
    help: str | None = None


class VisibleWhen(_Base):
    path: str
    equals: str | int | float | bool


class FieldSpec(_Base):
    path: str
    label: str
    type: FieldType
    widget: Widget
    value: Any = None
    default: Any = None
    choices: tuple[FieldChoice, ...] | None = None
    column_source: ColumnSource | None = None
    empty_label: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    scale: int | None = None
    min_selected: int | None = None
    max_visible: int | None = None
    help: str = ""
    required: bool = True
    advisory: bool = False
    visible_when: VisibleWhen | None = None
    order: int


class StageSpec(_Base):
    number: int
    id: str
    title: str
    summary: str = ""
    summary_template: str
    fields: tuple[FieldSpec, ...]


class AdvancedSettingsSchema(_Base):
    schema_version: int
    use_case_id: str
    ai_type: AiType
    stages: tuple[StageSpec, ...]


_ENUM_FOR_PATH: Final[Mapping[str, type[StrEnum]]] = {
    "prepare.missing_values": MissingValues,
    "prepare.outliers": Outliers,
    "prepare.pii_handling": PiiHandling,
    "split.type": SplitType,
    "features.categorical_encoding": CategoricalEncoding,
    "features.numeric_scaling": NumericScaling,
    "features.text_columns": TextHandling,
    "features.selection": FeatureSelection,
    "model_search.strategy": Strategy,
    "model_search.imbalance": Imbalance,
    "evaluation.calibration": Calibration,
    "evaluation.threshold.mode": ThresholdMode,
    "monitoring.retraining": Retraining,
}

_SUMMARY_TEMPLATES: Final[Mapping[str, str]] = {
    "data_preparation": (
        "Missing: {prepare.missing_values} · outliers: {prepare.outliers}"
        "{?validation.leakage_check: · leakage check} · PII: {prepare.pii_handling}"
        "{?prepare.exclude_columns: · {prepare.exclude_columns|count} excluded}"
    ),
    "data_split": (
        "{split.type} · {split.validation_fraction|pct}% val · {split.test_fraction|pct}% test"
        "{?split.type=time_based:{?split.time_column: · by {split.time_column}}}"
        "{?split.group_column: · grouped by {split.group_column}}"
    ),
    "feature_engineering": (
        "{?features.auto_feature_engineering:Auto features on}"
        "{?!features.auto_feature_engineering:Auto features off}"
        " · encoding {features.categorical_encoding} · text: {features.text_columns}"
        " · selection: {features.selection} · max {features.max_features}"
    ),
    "model_search": (
        "{model_search.strategy} · {model_search.candidates|count} of "
        "{model_search.candidate_pool|count} algorithms{?model_search.ensemble: · ensembling}"
        " · {model_search.tuning_trials} trials · {model_search.time_limit_minutes} min"
        " · {model_search.folds}-fold CV"
    ),
    "evaluation": (
        "{model_search.metric} · calibration {evaluation.calibration}"
        " · threshold {evaluation.threshold.mode}"
        "{?evaluation.shap: · top {evaluation.reasons_per_row} SHAP reasons}"
        "{?evaluation.fairness_column: · fairness by {evaluation.fairness_column}}"
        " · champion if +{evaluation.champion_min_improvement_pct}%"
    ),
    "monitoring": (
        "Drift alert PSI > {monitoring.drift_psi_threshold} · retrain: {monitoring.retraining}"
        " · alert if score drops {monitoring.performance_alert_drop_pct}%"
    ),
    "governance": (
        "Keep uploads {governance.retention_days} days"
        "{?governance.consent_column: · consent: {governance.consent_column}}"
        "{?governance.approval_required: · approval before champion}"
    ),
}

_ACTIONS_SUMMARY_TAIL: Final[str] = (
    "{?actions.suppression.suppress_opted_out: · skip opted-out}"
    "{?actions.suppression.suppress_recently_contacted:"
    " · skip contacted <{actions.suppression.recently_contacted_days}d}"
    " · {actions.control_group_fraction|pct}% control group"
)


def _actions_summary_template(bands: Sequence[Band]) -> str:
    head = " · ".join(f"{band.name} ≥ {{actions.bands[{i}].min_score}}" for i, band in enumerate(bands[:-1]))
    return f"{head}{_ACTIONS_SUMMARY_TAIL}"


def _band_fields(bands: Sequence[Band]) -> tuple[FieldSpec, ...]:
    return tuple(
        FieldSpec(
            path=f"actions.bands[{index}].min_score",
            label=f"{band.name}-risk score ≥",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0.0,
            max=1.0,
            step=0.05,
            order=index + 1,
        )
        for index, band in enumerate(bands[:-1])
    )


ADVISORY_PATHS: Final[frozenset[str]] = frozenset(
    {
        "features.auto_feature_engineering",
        "features.categorical_encoding",
        "features.numeric_scaling",
        "features.text_columns",
        "features.selection",
        "features.max_features",
        "monitoring.retraining",
    }
)
"""Settings the schema still carries but no stage reads yet (DEC-074).

They are real product intentions with a shape already agreed, so removing them would lose the
agreement; leaving them as live controls would promise behaviour the engine does not have. So they
stay, marked: the form renders them disabled under :const:`ADVISORY_NOTE`, and
:attr:`Recipe.recipe_hash` leaves them out, because a setting that changes no model must not make
two identical models look like different recipes.

**Adding a path here is how a setting is parked; removing one is how it ships.** Nothing else needs
to change in either direction - the form, the documentation and the hash all read this set.

Phase 4b shipped two of the original nine (DEC-795): `governance.retention_days` is enforced by the
retention job and `monitoring.performance_alert_drop_pct` by outcome ingestion, both reading the
value in the run's own `run_config.json`, so the control on a run's form is now what applies.
`monitoring.retraining` stays parked on the form: it is live, but read from the use case's
configuration by the managed retraining schedules, not from a run, so a value moved on one run's
form still changes nothing. Neither shipped setting is part of a `Recipe` (monitoring and governance
never were), so shipping them changes no recipe hash.
"""

ADVISORY_NOTE: Final[str] = "Coming later — recorded with the run, not yet applied."
"""What the form prints beside a disabled advisory control, so the reason is on screen (DEC-074)."""


def _numbered(fields: Sequence[FieldSpec], start: int = 1) -> tuple[FieldSpec, ...]:
    """Number a stage's fields in order, and mark the ones no stage reads yet (DEC-074).

    `advisory` is derived here rather than written on each `FieldSpec` so that
    :const:`ADVISORY_PATHS` stays the only place the fact is recorded: a setting cannot be parked
    in the form and still counted in the recipe hash, because both read the same set.
    """
    return tuple(
        field.model_copy(
            update={
                "order": start + index,
                "advisory": field.path in ADVISORY_PATHS,
                "help": ADVISORY_NOTE if field.path in ADVISORY_PATHS else field.help,
            }
        )
        for index, field in enumerate(fields)
    )


def _stage_specs(bands: Sequence[Band]) -> tuple[StageSpec, ...]:
    """The static table of section 2, with one band field per non-floor band of `bands`."""
    stage_1 = (
        FieldSpec(
            path="prepare.missing_values",
            label="Missing values",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=1,
        ),
        FieldSpec(
            path="prepare.outliers", label="Outliers", type=FieldType.STRING, widget=Widget.SELECT, order=2
        ),
        FieldSpec(
            path="prepare.pii_handling",
            label="PII handling",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=3,
        ),
        FieldSpec(
            path="validation.min_positive",
            label="Min positive examples",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=50,
            max=100000,
            step=50,
            order=4,
        ),
        FieldSpec(
            path="prepare.deduplicate",
            label="Remove duplicate rows",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=5,
        ),
        FieldSpec(
            path="validation.leakage_check",
            label="Check for target leakage",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=6,
        ),
        FieldSpec(
            path="prepare.exclude_columns",
            label="Exclude columns from features",
            type=FieldType.ARRAY,
            widget=Widget.COLUMN_MULTI_SELECT,
            column_source=ColumnSource.FEATURES,
            max_visible=14,
            required=False,
            order=7,
        ),
    )
    stage_2 = (
        FieldSpec(
            path="split.type", label="Split type", type=FieldType.STRING, widget=Widget.SELECT, order=1
        ),
        FieldSpec(
            path="split.validation_fraction",
            label="Validation (%)",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0.05,
            max=0.40,
            step=0.05,
            scale=100,
            order=2,
        ),
        FieldSpec(
            path="split.test_fraction",
            label="Test (%)",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0.05,
            max=0.40,
            step=0.05,
            scale=100,
            order=3,
        ),
        FieldSpec(
            path="split.time_column",
            label="Time column",
            type=FieldType.STRING,
            widget=Widget.COLUMN_SELECT,
            column_source=ColumnSource.TIME_LIKE,
            empty_label="Select…",
            required=False,
            visible_when=VisibleWhen(path="split.type", equals=SplitType.TIME_BASED.value),
            order=4,
        ),
        FieldSpec(
            path="split.group_column",
            label="Group column (keep together)",
            type=FieldType.STRING,
            widget=Widget.COLUMN_SELECT,
            column_source=ColumnSource.FEATURES,
            empty_label="None",
            required=False,
            order=5,
        ),
    )
    stage_3 = (
        FieldSpec(
            path="features.auto_feature_engineering",
            label="Automatic feature engineering (dates, ratios, aggregates)",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=1,
        ),
        FieldSpec(
            path="features.categorical_encoding",
            label="Categorical encoding",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=2,
        ),
        FieldSpec(
            path="features.numeric_scaling",
            label="Numeric scaling",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=3,
        ),
        FieldSpec(
            path="features.text_columns",
            label="Text columns",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=4,
        ),
        FieldSpec(
            path="features.selection",
            label="Feature selection",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=5,
        ),
        FieldSpec(
            path="features.max_features",
            label="Max features",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=10,
            max=500,
            step=10,
            order=6,
        ),
    )
    stage_4 = (
        FieldSpec(
            path="model_search.candidates",
            label="",
            type=FieldType.ARRAY,
            widget=Widget.MULTI_SELECT,
            min_selected=1,
            visible_when=VisibleWhen(path="__ui.model", equals="__automl__"),
            order=1,
        ),
        FieldSpec(
            path="model_search.strategy",
            label="Search strategy",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=2,
        ),
        FieldSpec(
            path="model_search.tuning_trials",
            label="Tuning trials",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=5,
            max=500,
            step=10,
            order=3,
        ),
        FieldSpec(
            path="model_search.time_limit_minutes",
            label="Time limit (min)",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=1,
            max=240,
            step=1,
            order=4,
        ),
        FieldSpec(
            path="model_search.folds",
            label="CV folds",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=2,
            max=10,
            step=1,
            order=5,
        ),
        FieldSpec(
            path="model_search.imbalance",
            label="Class imbalance",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=6,
        ),
        FieldSpec(
            path="model_search.ensemble",
            label="Ensemble / stack the best models",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=7,
        ),
    )
    stage_5 = (
        FieldSpec(
            path="model_search.metric",
            label="Optimise for",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=1,
        ),
        FieldSpec(
            path="evaluation.calibration",
            label="Probability calibration",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=2,
        ),
        FieldSpec(
            path="evaluation.threshold.mode",
            label="Decision threshold",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=3,
        ),
        FieldSpec(
            path="evaluation.threshold.value",
            label="Threshold value",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0.01,
            max=0.99,
            step=0.01,
            visible_when=VisibleWhen(path="evaluation.threshold.mode", equals=ThresholdMode.MANUAL.value),
            order=4,
        ),
        FieldSpec(
            path="evaluation.reasons_per_row",
            label="Reasons per row",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=1,
            max=5,
            step=1,
            order=5,
        ),
        FieldSpec(
            path="evaluation.fairness_column",
            label="Fairness check (sensitive column)",
            type=FieldType.STRING,
            widget=Widget.COLUMN_SELECT,
            column_source=ColumnSource.FEATURES,
            empty_label="None",
            required=False,
            order=6,
        ),
        FieldSpec(
            path="evaluation.champion_min_improvement_pct",
            label="Replace champion if better by (%)",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0,
            max=20,
            step=0.5,
            order=7,
        ),
        FieldSpec(
            path="evaluation.shap",
            label="Generate SHAP explanations per row",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=8,
        ),
    )
    stage_6 = (
        *_band_fields(bands),
        FieldSpec(
            path="actions.control_group_fraction",
            label="Control group holdout (%)",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0,
            max=0.50,
            step=0.01,
            scale=100,
            order=0,
        ),
        FieldSpec(
            path="actions.suppression.recently_contacted_days",
            label="Recently contacted (days)",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=1,
            max=90,
            step=1,
            order=0,
        ),
        FieldSpec(
            path="actions.suppression.suppress_opted_out",
            label="Suppress opted-out customers",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=0,
        ),
        FieldSpec(
            path="actions.suppression.suppress_recently_contacted",
            label="Suppress recently contacted",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=0,
        ),
    )
    stage_7 = (
        FieldSpec(
            path="monitoring.drift_psi_threshold",
            label="Drift alert (PSI above)",
            type=FieldType.NUMBER,
            widget=Widget.NUMBER,
            min=0.05,
            max=1.00,
            step=0.05,
            order=1,
        ),
        FieldSpec(
            path="monitoring.retraining",
            label="Retraining",
            type=FieldType.STRING,
            widget=Widget.SELECT,
            order=2,
        ),
        FieldSpec(
            path="monitoring.performance_alert_drop_pct",
            label="Performance alert (% drop)",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=1,
            max=50,
            step=1,
            order=3,
        ),
    )
    stage_8 = (
        FieldSpec(
            path="governance.retention_days",
            label="Data retention (days)",
            type=FieldType.INTEGER,
            widget=Widget.NUMBER,
            min=0,
            max=730,
            step=30,
            order=1,
        ),
        FieldSpec(
            path="governance.consent_column",
            label="Consent column (use rows where true)",
            type=FieldType.STRING,
            widget=Widget.COLUMN_SELECT,
            column_source=ColumnSource.FEATURES,
            empty_label="None",
            required=False,
            order=2,
        ),
        FieldSpec(
            path="governance.approval_required",
            label="Require approval before a model becomes champion",
            type=FieldType.BOOLEAN,
            widget=Widget.CHECKBOX,
            order=3,
        ),
    )
    blocks: tuple[tuple[str, str, tuple[FieldSpec, ...]], ...] = (
        ("data_preparation", "Data preparation", stage_1),
        ("data_split", "Data split", stage_2),
        ("feature_engineering", "Feature engineering", stage_3),
        ("model_search", "Model search", stage_4),
        ("evaluation", "Evaluation & explainability", stage_5),
        ("actions", "Actions & output", stage_6),
        ("monitoring", "Monitoring & retraining", stage_7),
        ("governance", "Governance & privacy", stage_8),
    )
    return tuple(
        StageSpec(
            number=number,
            id=stage_id,
            title=title,
            summary_template=(
                _actions_summary_template(bands) if stage_id == "actions" else _SUMMARY_TEMPLATES[stage_id]
            ),
            fields=_numbered(fields),
        )
        for number, (stage_id, title, fields) in enumerate(blocks, start=1)
    )


FIELD_TABLE: Final[tuple[StageSpec, ...]] = _stage_specs(ActionsConfig().bands)


def _resolve_config_path(config: UseCaseConfig, path: str, code: str) -> Any:
    current: Any = config
    try:
        tokens = _key_tokens(path)
    except ConfigError as exc:
        raise ConfigError(code, f"{path!r} is not a settings path.", path=path) from exc
    for token in tokens:
        try:
            current = current[token] if isinstance(token, int) else getattr(current, token)
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise ConfigError(code, f"{path!r} is not a setting of this configuration.", path=path) from exc
        if current is None and token != tokens[-1]:
            raise ConfigError(code, f"{path!r} is not a setting of this configuration.", path=path)
    return current


def _jsonify(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, _Base):
        return value.model_dump(mode="json")
    return value


def _format_number(value: float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _label_for(value: StrEnum, catalog: Catalog) -> str:
    try:
        return CHOICE_LABELS[value]
    except KeyError:
        pass
    if isinstance(value, Metric):
        return catalog.metric_label(value)
    if isinstance(value, ModelFamily):
        return catalog.family_label(value)
    return value.value


def _display(value: Any, catalog: Catalog) -> str:
    if value is None:
        return ""
    if isinstance(value, StrEnum):
        return _label_for(value, catalog)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, (tuple, list)):
        return " · ".join(_display(item, catalog) for item in value)
    return str(value)


def _raw_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    return str(value)


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, tuple, list, dict)):
        return len(value) > 0
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ConfigError("SUMMARY_TEMPLATE_UNBALANCED", f"Unbalanced braces in {text!r}.")


def _split_condition(body: str) -> tuple[str, str]:
    depth = 0
    for index, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ":" and depth == 0:
            return body[:index], body[index + 1 :]
    raise ConfigError("SUMMARY_TEMPLATE_UNBALANCED", f"Conditional {body!r} has no ':' separator.")


def _apply_filter(value: Any, filter_name: str, catalog: Catalog) -> str:
    if filter_name == "":
        return _display(value, catalog)
    if filter_name == "value":
        return _raw_text(value)
    if filter_name == "count":
        return str(len(value)) if isinstance(value, (tuple, list, str, dict)) else "0"
    if filter_name == "pct":
        return str(round(float(value) * 100))
    if filter_name == "lower":
        return _display(value, catalog).lower()
    raise ConfigError("SUMMARY_UNKNOWN_FILTER", f"{filter_name!r} is not a summary filter.")


def _render_expression(expression: str, config: UseCaseConfig) -> str:
    if expression.startswith("?"):
        condition, body = _split_condition(expression[1:])
        if condition.startswith("!"):
            value = _resolve_config_path(config, condition[1:], "SUMMARY_UNKNOWN_PATH")
            return render_stage_summary(body, config) if not _truthy(value) else ""
        path, equals, literal = condition.partition("=")
        value = _resolve_config_path(config, path, "SUMMARY_UNKNOWN_PATH")
        if equals:
            return render_stage_summary(body, config) if _raw_text(value) == literal else ""
        return render_stage_summary(body, config) if _truthy(value) else ""
    path, _, filter_name = expression.partition("|")
    value = _resolve_config_path(config, path, "SUMMARY_UNKNOWN_PATH")
    return _apply_filter(value, filter_name, config.catalog)


def render_stage_summary(template: str, config: UseCaseConfig) -> str:
    """Render one `summary_template` against a config (the grammar of section 4.7)."""
    parts: list[str] = []
    index = 0
    while index < len(template):
        char = template[index]
        if char == "{":
            end = _matching_brace(template, index)
            parts.append(_render_expression(template[index + 1 : end], config))
            index = end + 1
        else:
            parts.append(char)
            index += 1
    return "".join(parts)


def _family_available(family: ModelFamily, catalog: Catalog, torch_available: bool | None) -> bool:
    for module in catalog.model_families[family].requires:
        available = torch_available if (module == "torch" and torch_available is not None) else None
        if available is None:
            available = dependency_available(module)
        if not available:
            return False
    return True


def _column_choices(
    field: FieldSpec,
    columns: Sequence[str] | None,
    primary_key: str | None,
    target: str | None,
    catalog: Catalog,
) -> tuple[FieldChoice, ...] | None:
    if columns is None:
        return None
    if field.column_source is ColumnSource.FEATURES:
        reserved = {primary_key, target}
        offered = [column for column in columns if column not in reserved]
    elif field.column_source is ColumnSource.TIME_LIKE:
        pattern = re.compile(catalog.column_name_patterns.time_like, re.IGNORECASE)
        offered = [column for column in columns if pattern.search(column)] or list(columns)
    else:
        offered = list(columns)
    return tuple(FieldChoice(value=column, label=column) for column in offered)


def _choices_for(
    field: FieldSpec,
    config: UseCaseConfig,
    catalog: Catalog,
    columns: Sequence[str] | None,
    primary_key: str | None,
    target: str | None,
    torch_available: bool | None,
) -> tuple[FieldChoice, ...] | None:
    if field.path == "model_search.metric":
        return tuple(
            FieldChoice(value=metric.value, label=catalog.metric_label(metric))
            for metric in config.model_search.metric_choices
        )
    if field.path == "model_search.candidates":
        choices: list[FieldChoice] = []
        for family in config.model_search.candidate_pool:
            available = _family_available(family, catalog, torch_available)
            choices.append(
                FieldChoice(
                    value=family.value,
                    label=catalog.family_label(family),
                    enabled=available,
                    help=None if available else "Requires the optional 'nn' extra",
                )
            )
        return tuple(choices)
    if field.widget in (Widget.COLUMN_SELECT, Widget.COLUMN_MULTI_SELECT):
        return _column_choices(field, columns, primary_key, target, catalog)
    enum_type = _ENUM_FOR_PATH.get(field.path)
    if enum_type is None:
        return None
    return tuple(FieldChoice(value=member.value, label=CHOICE_LABELS[member]) for member in enum_type)


def advanced_settings_schema(
    config: UseCaseConfig,
    *,
    columns: Sequence[str] | None = None,
    primary_key: str | None = None,
    target: str | None = None,
    torch_available: bool | None = None,
) -> AdvancedSettingsSchema:
    """Projection of `FIELD_TABLE` onto a resolved config. No branch inspects the use-case id."""
    if config.ai_type is AiType.GENERATIVE:
        return AdvancedSettingsSchema(
            schema_version=SCHEMA_VERSION, use_case_id=config.id, ai_type=config.ai_type, stages=()
        )
    catalog = config.catalog
    stages: list[StageSpec] = []
    for static_stage in _stage_specs(config.actions.bands):
        fields: list[FieldSpec] = []
        for field in static_stage.fields:
            value = _jsonify(_resolve_config_path(config, field.path, "SCHEMA_UNKNOWN_PATH"))
            fields.append(
                field.model_copy(
                    update={
                        "value": value,
                        "default": value,
                        "choices": _choices_for(
                            field, config, catalog, columns, primary_key, target, torch_available
                        ),
                    }
                )
            )
        stages.append(
            static_stage.model_copy(
                update={
                    "fields": tuple(fields),
                    "summary": render_stage_summary(static_stage.summary_template, config),
                }
            )
        )
    return AdvancedSettingsSchema(
        schema_version=SCHEMA_VERSION,
        use_case_id=config.id,
        ai_type=config.ai_type,
        stages=tuple(stages),
    )


# ---------------------------------------------------------------------------
# Recipe: every training choice, in one object (DEC-042)
# ---------------------------------------------------------------------------
class Recipe(_Base):
    """Every choice that determines a trained model, and nothing else.

    `train(recipe)` is the only entry point to training, so this object is the
    complete description of a training attempt. The defining property is:

        the same `Recipe` applied to the same `DatasetFingerprint`
        must produce the same model.

    That is what makes a training run reproducible, and what lets a later layer
    reason about which recipes work without re-reading the whole configuration.

    Evaluation settings are deliberately ABSENT. How a model is measured is not
    a training choice, and the evaluate stage takes only the model, the test
    data and the evaluation config, so that any stored model can be re-scored on
    demand (DEC-043, DEC-044). Actions, monitoring and governance are absent for
    the same reason: they shape what happens to predictions, not how the model
    is fitted.
    """

    use_case_id: str = Field(description="Use case this recipe belongs to.")
    problem_type: ProblemType = Field(description="Learning task the model is fitted for.")
    target: str = Field(description="Column the model learns to predict.")
    primary_key: PrimaryKey = Field(description="Row identifier; never used as a feature.")
    feature_columns: tuple[str, ...] = Field(
        description="Exact ordered feature list handed to training, after exclusions."
    )
    prepare: PrepareConfig = Field(description="Cleaning and exclusion choices applied before fitting.")
    split: SplitConfig = Field(description="How rows are partitioned into train, validation and test.")
    features: FeaturesConfig = Field(description="Encoding, scaling and feature-selection choices.")
    model_search: ModelSearchConfig = Field(description="Metric, strategy, candidate families and budget.")
    seed: int = Field(description="Seed for every stochastic step, derived from the run id.")

    @property
    def recipe_hash(self) -> str:
        """Stable identity of this recipe, for deduplication and comparison.

        Canonical JSON with sorted keys, so two recipes that differ only in field
        order hash alike. Uses sha256 rather than `hash()`, which is salted per
        process and would not survive a restart.

        :const:`ADVISORY_PATHS` are removed before hashing (DEC-074). The hash exists to answer
        "would this produce the same model", and a setting no stage reads cannot change a model.
        Leaving them in would make two runs that fitted byte-identical models hash differently
        merely because someone moved a control that does nothing yet. They stay on the recipe
        itself, and so in `run_config.json` and the run manifest, because what the user chose is
        still worth recording; they are simply not part of its identity.
        """
        payload = self.model_dump(mode="json")
        for path in ADVISORY_PATHS:
            block, _, field = path.partition(".")
            section = payload.get(block)
            if isinstance(section, dict):
                section.pop(field, None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def recipe_from_config(
    config: UseCaseConfig,
    *,
    primary_key: PrimaryKey,
    feature_columns: Sequence[str],
    seed: int,
    target: str | None = None,
) -> Recipe:
    """Project a resolved use-case config into the training choices, and only those.

    `target` overrides `config.target.column` for a run whose target was chosen at
    upload time; it is required when the config carries no target.
    """
    resolved_target = target if target is not None else config.target.column
    if resolved_target is None:
        raise ConfigError(
            "RECIPE_TARGET_REQUIRED",
            "A training recipe needs a target column, but none is configured or supplied.",
            path="target.column",
        )
    return Recipe(
        use_case_id=config.id,
        problem_type=config.problem_type,
        target=resolved_target,
        primary_key=primary_key,
        feature_columns=tuple(feature_columns),
        prepare=config.prepare,
        split=config.split,
        features=config.features,
        model_search=config.model_search,
        seed=seed,
    )


# ===========================================================================
# Shared file (PARALLEL_WORK_PROTOCOL.md §4): three branches edit it at once.
# Add code only inside your own block, at its end. Never edit above your
# block, never reorder, never reformat the rest of the file - run `black` on
# what you paste, not on the file, if the formatter would reflow other lines.
# `tests/unit/test_shared_file_markers.py` fails if a block goes missing.
# ===========================================================================

# ---- PHASE-2 (onboarding) — append only below this line ----
# The onboarding vocabulary: the blocks a use-case YAML and `engine.yaml:defaults` may carry once a
# client's raw tables are onboarded (Phase 2 plan sections 4, 5 and 6). They live in this file rather
# than in `engine/onboarding/specs.py` because `UseCaseConfig` has fields of their types and
# `engine.config` may not import anything of ours; `specs.py` re-exports every name below, so one
# import still serves the whole Phase 2 contract (DEC-100).
#
# `date` is imported here rather than at the top of the file because PARALLEL_WORK_PROTOCOL.md §4
# forbids editing above this marker; the noqa records that it is the protocol, not a preference.
from datetime import date  # noqa: E402


class RoleKind(StrEnum):
    """What a source table is shaped like. `entity`: one row per entity. `event`: many, dated."""

    ENTITY = "entity"
    EVENT = "event"


class StandardType(StrEnum):
    """The type vocabulary of a *standard* column, which is coarser than `ColumnType` on purpose.

    Mapping asks "can this source column mean that standard column?", and for that question
    `integer` and `float` are one answer, not two.
    """

    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"
    DATE = "date"
    TEXT = "text"


class AggFunction(StrEnum):
    """The feature library (plan section 6.2). Users pick from this list; they never write SQL."""

    COUNT = "count"
    SUM = "sum"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"
    STD = "std"
    NUNIQUE = "nunique"
    LATEST = "latest"
    FIRST = "first"
    DAYS_SINCE_LAST = "days_since_last"
    DAYS_SINCE_FIRST = "days_since_first"
    EXISTS = "exists"
    RATIO = "ratio"
    DERIVE = "derive"


class WhereOp(StrEnum):
    """Comparisons a feature or label filter may use (plan section 6.2)."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"


class LabelType(StrEnum):
    """How the target is derived (plan section 5.2). `column` is the Phase 1 "already have it" case."""

    COLUMN = "column"
    EVENT_PRESENCE = "event_presence"
    EVENT_ABSENCE = "event_absence"
    VALUE_THRESHOLD = "value_threshold"


class SnapshotMode(StrEnum):
    """One row per entity (`single`) or one row per entity per date (`periodic`)."""

    SINGLE = "single"
    PERIODIC = "periodic"


class SnapshotFrequency(StrEnum):
    """Spacing of periodic snapshots."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TransformKind(StrEnum):
    """The pure, replayable transforms a mapping may attach to a column (plan section 6.1)."""

    CAST = "cast"
    VALUE_MAP = "value_map"
    NEGATE = "negate"
    SCALE = "scale"
    STRIP = "strip"
    LOWER = "lower"
    LSTRIP_ZEROS = "lstrip_zeros"
    DERIVE = "derive"
    DEDUPE = "dedupe"


class DecidedBy(StrEnum):
    """Who settled a mapping decision. Auto-accepted items are still shown and reversible."""

    AUTO = "auto"
    USER = "user"


_FEATURE_NAME_PATTERN: Final[str] = r"^[a-z_][a-z0-9_]*$"
_STANDARD_NAME_PATTERN: Final[str] = r"^[A-Za-z_][A-Za-z0-9_]*$"

_NO_VALUE_OPS: Final[frozenset[WhereOp]] = frozenset({WhereOp.IS_NULL, WhereOp.NOT_NULL})
"""Operators that take no right-hand side; giving one is a config error, not a silent ignore."""

AGGREGATES_OVER_COLUMN: Final[frozenset[AggFunction]] = frozenset(
    {
        AggFunction.SUM,
        AggFunction.MEAN,
        AggFunction.MIN,
        AggFunction.MAX,
        AggFunction.STD,
        AggFunction.NUNIQUE,
        AggFunction.LATEST,
        AggFunction.FIRST,
    }
)
"""Functions that need a `column` to aggregate. `count`/`exists`/`days_since_*` count rows instead."""

WINDOWLESS_FUNCTIONS: Final[frozenset[AggFunction]] = frozenset({AggFunction.RATIO, AggFunction.DERIVE})
"""Functions whose window lives in their parts (`ratio`) or nowhere (`derive`)."""


class WhereClause(_Base):
    """One filter on an event table: `{column, op, value}` (plan section 6.2)."""

    column: Annotated[str, Field(min_length=1)]
    op: WhereOp
    value: Any = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> Self:
        if self.op in _NO_VALUE_OPS:
            if self.value is not None:
                raise ConfigError(
                    "WHERE_VALUE_NOT_ALLOWED",
                    f"'{self.op.value}' takes no value, but {self.value!r} was given.",
                    path="where.value",
                )
            return self
        if self.value is None:
            raise ConfigError(
                "WHERE_VALUE_REQUIRED",
                f"'{self.op.value}' needs a value to compare {self.column!r} against.",
                path="where.value",
            )
        if self.op is WhereOp.IN and not isinstance(self.value, (list, tuple)):
            raise ConfigError(
                "WHERE_IN_NEEDS_LIST",
                f"'in' compares {self.column!r} against a list of values, not {type(self.value).__name__}.",
                path="where.value",
            )
        if self.op is not WhereOp.IN and isinstance(self.value, (list, tuple)):
            raise ConfigError(
                "WHERE_VALUE_NOT_A_LIST",
                f"'{self.op.value}' compares {self.column!r} against one value; use 'in' for a list.",
                path="where.value",
            )
        return self


class SubAggregation(_Base):
    """One half of a `ratio` feature. Deliberately not recursive: a ratio of ratios is unreadable."""

    function: AggFunction
    column: str | None = None
    window_days: Annotated[int, Field(gt=0)] | None = None
    where: WhereClause | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.function in WINDOWLESS_FUNCTIONS:
            raise ConfigError(
                "SUB_AGGREGATION_NOT_ALLOWED",
                f"'{self.function.value}' cannot be one half of a ratio.",
                path="function",
            )
        if self.function in AGGREGATES_OVER_COLUMN and not self.column:
            raise ConfigError(
                "FEATURE_COLUMN_REQUIRED",
                f"'{self.function.value}' needs the column it aggregates.",
                path="column",
            )
        return self


class FeatureDef(_Base):
    """One feature, in the format `suggested_features` and `feature_spec.json` both use.

    A suggestion and a user-authored feature are the same document, so accepting a suggestion is a
    copy rather than a translation, and the Evolve layer of Phase 5 has one thing to propose edits to.
    """

    name: Annotated[str, Field(pattern=_FEATURE_NAME_PATTERN)]
    role: Annotated[str, Field(min_length=1)]
    function: AggFunction
    column: str | None = None
    window_days: Annotated[int, Field(gt=0)] | None = None
    where: WhereClause | None = None
    of: SubAggregation | None = None
    over: SubAggregation | None = None
    expression: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Self:
        function = self.function
        if function is AggFunction.RATIO:
            if self.of is None or self.over is None:
                raise ConfigError(
                    "FEATURE_RATIO_NEEDS_PARTS",
                    f"Feature {self.name!r} is a ratio, so it needs both 'of' and 'over'.",
                    path="of",
                )
        elif self.of is not None or self.over is not None:
            raise ConfigError(
                "FEATURE_PARTS_NOT_ALLOWED",
                f"Only a ratio has 'of' and 'over'; {self.name!r} is a {function.value}.",
                path="of",
            )
        if function is AggFunction.DERIVE:
            if not self.expression:
                raise ConfigError(
                    "FEATURE_EXPRESSION_REQUIRED",
                    f"Feature {self.name!r} is derived, so it needs an expression.",
                    path="expression",
                )
        elif self.expression is not None:
            raise ConfigError(
                "FEATURE_EXPRESSION_NOT_ALLOWED",
                f"Only a derived feature has an expression; {self.name!r} is a {function.value}.",
                path="expression",
            )
        if function in AGGREGATES_OVER_COLUMN and not self.column:
            raise ConfigError(
                "FEATURE_COLUMN_REQUIRED",
                f"Feature {self.name!r} is a {function.value}, so it needs the column it aggregates.",
                path="column",
            )
        if function in WINDOWLESS_FUNCTIONS and self.window_days is not None:
            raise ConfigError(
                "FEATURE_WINDOW_NOT_ALLOWED",
                f"A {function.value} feature carries no window of its own; {self.name!r} has one.",
                path="window_days",
            )
        return self

    @property
    def windows(self) -> tuple[int | None, ...]:
        """Every window this feature reads, including both halves of a ratio."""
        if self.function is AggFunction.RATIO and self.of is not None and self.over is not None:
            return (self.of.window_days, self.over.window_days)
        return (self.window_days,)


class LabelDefinition(_Base):
    """How the target is derived (plan section 5.2).

    `agent_editable` is `False` and cannot be set to anything else: the Evolve layer of Phase 5 may
    propose feature specs, never label or snapshot specs, because an agent that can redefine churn
    can make any score go up without improving anything (plan section 14).
    """

    name: Annotated[str, Field(pattern=_STANDARD_NAME_PATTERN)]
    type: LabelType
    role: str | None = None
    column: str | None = None
    horizon_days: Annotated[int, Field(gt=0)] | None = None
    where: WhereClause | None = None
    expression: str | None = None
    any_event: bool = Field(default=True, alias="any")
    description: str = ""
    agent_editable: Literal[False] = False

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.type is LabelType.COLUMN:
            if not self.column:
                raise ConfigError(
                    "LABEL_COLUMN_REQUIRED",
                    f"Label {self.name!r} reads an existing column, so it must name it.",
                    path="column",
                )
            for field, value in (("role", self.role), ("expression", self.expression)):
                if value is not None:
                    raise ConfigError(
                        "LABEL_FIELD_NOT_ALLOWED",
                        f"A label read from a column has no {field}; {self.name!r} has one.",
                        path=field,
                    )
            if self.horizon_days is not None:
                raise ConfigError(
                    "LABEL_FIELD_NOT_ALLOWED",
                    f"A label read from a column has no horizon; {self.name!r} has one.",
                    path="horizon_days",
                )
            return self
        if not self.role:
            raise ConfigError(
                "LABEL_ROLE_REQUIRED",
                f"Label {self.name!r} is derived from events, so it must name the role they are in.",
                path="role",
            )
        if self.horizon_days is None:
            raise ConfigError(
                "LABEL_HORIZON_REQUIRED",
                f"Label {self.name!r} looks forward from the snapshot, so it needs a horizon in days.",
                path="horizon_days",
            )
        if self.column is not None:
            raise ConfigError(
                "LABEL_FIELD_NOT_ALLOWED",
                f"Only a label of type 'column' names a column; {self.name!r} is a {self.type.value}.",
                path="column",
            )
        if self.type is LabelType.VALUE_THRESHOLD:
            if not self.expression:
                raise ConfigError(
                    "LABEL_EXPRESSION_REQUIRED",
                    f"Label {self.name!r} tests a condition, so it needs an expression.",
                    path="expression",
                )
        elif self.expression is not None:
            raise ConfigError(
                "LABEL_FIELD_NOT_ALLOWED",
                f"Only a value_threshold label has an expression; {self.name!r} is a {self.type.value}.",
                path="expression",
            )
        return self


class SnapshotDefinition(_Base):
    """Which dates to build rows for (plan section 6.4), and `engine.yaml`'s default for them.

    Like `LabelDefinition`, this is never agent-editable: moving the snapshot dates moves the
    measurement, and a search that may move its own measurement measures nothing.
    """

    mode: SnapshotMode = SnapshotMode.PERIODIC
    frequency: SnapshotFrequency = SnapshotFrequency.MONTHLY
    start: date | None = None
    end: date | None = None
    max_snapshots: Annotated[int, Field(ge=1, le=120)] = 12
    min_history_days: Annotated[int, Field(ge=0)] = 90
    inclusive_snapshot_time: bool = True
    agent_editable: Literal[False] = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ConfigError(
                "SNAPSHOT_RANGE_INVERTED",
                f"The first snapshot ({self.start}) is after the last ({self.end}).",
                path="start",
            )
        return self


class Derivable(_Base):
    """How a standard column can be computed when the client has no column for it."""

    from_role: str = "entity"
    expression: Annotated[str, Field(min_length=1)]


class StandardColumn(_Base):
    """One column of the shape a use case wants, and every way a client might have spelled it."""

    name: Annotated[str, Field(pattern=_STANDARD_NAME_PATTERN)]
    type: StandardType
    required: bool = False
    aliases: tuple[str, ...] = ()
    description: str = ""
    value_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    range: tuple[float, float] | None = None
    derivable: Derivable | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.value_aliases and self.type not in {StandardType.CATEGORICAL, StandardType.BOOLEAN}:
            raise ConfigError(
                "VALUE_ALIASES_NOT_ALLOWED",
                f"Only a categorical or boolean column has value aliases; {self.name!r} is {self.type.value}.",
                path="value_aliases",
            )
        if self.range is not None:
            if self.type is not StandardType.NUMERIC:
                raise ConfigError(
                    "RANGE_NOT_ALLOWED",
                    f"Only a numeric column has a range; {self.name!r} is {self.type.value}.",
                    path="range",
                )
            if self.range[0] > self.range[1]:
                raise ConfigError(
                    "RANGE_INVERTED",
                    f"The range of {self.name!r} starts above where it ends.",
                    path="range",
                )
        return self


class StandardSchemaConfig(_Base):
    """The one-row-per-entity shape a use case wants, in *our* names (plan section 4.2)."""

    entity_key: Annotated[str, Field(pattern=_STANDARD_NAME_PATTERN)] = "entity_key"
    snapshot_column: Annotated[str, Field(pattern=_STANDARD_NAME_PATTERN)] = "snapshot_date"
    columns: tuple[StandardColumn, ...] = ()

    @model_validator(mode="after")
    def _unique_and_disjoint(self) -> Self:
        names = [column.name for column in self.columns]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConfigError(
                "STANDARD_DUPLICATE_COLUMN",
                f"standard_schema.columns lists {', '.join(duplicates)} more than once.",
                path="standard_schema.columns",
            )
        for reserved, what in (
            (self.entity_key, "the entity key"),
            (self.snapshot_column, "the snapshot date"),
        ):
            if reserved in names:
                raise ConfigError(
                    "STANDARD_RESERVED_COLUMN",
                    f"{reserved!r} is {what}, so it must not also be listed as a standard column.",
                    path="standard_schema.columns",
                )
        return self

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def reserved_names(self) -> tuple[str, ...]:
        """The two names the engine owns: they identify a row, they are never features."""
        return (self.entity_key, self.snapshot_column)

    def by_name(self, name: str) -> StandardColumn | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    @property
    def required_columns(self) -> tuple[StandardColumn, ...]:
        return tuple(column for column in self.columns if column.required)


class MappingDefaults(_Base):
    """`engine.yaml:defaults.onboarding.mapping` (plan section 5.3)."""

    auto_accept_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.85
    suggest_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.50
    max_value_levels_for_value_mapping: Annotated[int, Field(ge=1)] = 50

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.suggest_confidence > self.auto_accept_confidence:
            raise ConfigError(
                "MAPPING_CONFIDENCE_INVERTED",
                "suggest_confidence must not be above auto_accept_confidence.",
                path="onboarding.mapping.suggest_confidence",
            )
        return self


class FeatureLibraryDefaults(_Base):
    """The cross-use-case default feature library (plan section 5.1).

    A use case's own `suggested_features` are *named* documents that win over anything generated
    here with the same name, which is how "use-case files add or override" survives the rule that a
    list replaces rather than merges (DEC-002).
    """

    enabled: bool = True
    windows_days: tuple[int, ...] = (7, 30, 90, 180, 365)
    functions: tuple[AggFunction, ...] = (
        AggFunction.COUNT,
        AggFunction.SUM,
        AggFunction.MEAN,
        AggFunction.LATEST,
        AggFunction.DAYS_SINCE_LAST,
    )

    @field_validator("windows_days")
    @classmethod
    def _positive(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if any(window <= 0 for window in v):
            raise ConfigError(
                "WINDOW_NOT_POSITIVE",
                "A window is a number of days before the snapshot, so it must be above zero.",
                path="onboarding.features.library.windows_days",
            )
        return v

    @field_validator("functions")
    @classmethod
    def _generatable(cls, v: tuple[AggFunction, ...]) -> tuple[AggFunction, ...]:
        wrong = [f.value for f in v if f in WINDOWLESS_FUNCTIONS]
        if wrong:
            raise ConfigError(
                "LIBRARY_FUNCTION_NOT_GENERATABLE",
                f"{', '.join(wrong)} needs parts the library cannot guess, so it cannot be generated.",
                path="onboarding.features.library.functions",
            )
        return v


class FeatureDefaults(_Base):
    """`engine.yaml:defaults.onboarding.features` (plan section 5.3)."""

    default_windows_days: tuple[int, ...] = (30, 90, 180)
    max_features: Annotated[int, Field(ge=1)] = 300
    drop_if_null_fraction_above: Annotated[float, Field(ge=0.0, le=1.0)] = 0.98
    library: FeatureLibraryDefaults = FeatureLibraryDefaults()


class LabelDefaults(_Base):
    """`engine.yaml:defaults.onboarding.labels` (plan section 5.3)."""

    drop_censored: bool = True


class OnboardingLimits(_Base):
    """`engine.yaml:defaults.onboarding.limits` (plan section 5.3). Phase 2 reads files, not warehouses."""

    max_source_rows: Annotated[int, Field(ge=1)] = 50_000_000
    max_sources: Annotated[int, Field(ge=1)] = 10


class OnboardingConfig(_Base):
    """`engine.yaml:defaults.onboarding` - every onboarding default, per use case (plan section 5.3)."""

    mapping: MappingDefaults = MappingDefaults()
    features: FeatureDefaults = FeatureDefaults()
    snapshots: SnapshotDefinition = SnapshotDefinition()
    labels: LabelDefaults = LabelDefaults()
    limits: OnboardingLimits = OnboardingLimits()


# --- the role catalogue (configs/roles.yaml) --------------------------------
class RoleSpec(_Base):
    """One role from `configs/roles.yaml`: what a table is, and what it must carry to be usable."""

    kind: RoleKind
    description: str = ""
    required_columns: tuple[str, ...] = ()
    optional_columns: tuple[str, ...] = ()
    typical_columns: tuple[StandardColumn, ...] = ()
    name_tokens: tuple[str, ...] = ()

    @property
    def is_event(self) -> bool:
        return self.kind is RoleKind.EVENT

    @property
    def typical_names(self) -> tuple[str, ...]:
        """Just the names of `typical_columns`, for detection and for messages.

        The definitions themselves carry the aliases that let a mapper recognise `AMT` as `amount`;
        role *detection* only needs to know which vocabulary a table speaks, so it reads this.
        """
        return tuple(column.name for column in self.typical_columns)

    def typical(self, name: str) -> StandardColumn | None:
        """The definition of one role-typical column, or None when this role has no such column."""
        for column in self.typical_columns:
            if column.name == name:
                return column
        return None


class RoleCatalogue(_Base):
    """`configs/roles.yaml`. Engine data like `engine.yaml:catalog`: never merged, never overridable."""

    schema_version: Literal[1]
    roles: dict[str, RoleSpec]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not self.roles:
            raise ConfigError("ROLES_EMPTY", "configs/roles.yaml defines no roles.", path="roles")
        entity = [name for name, spec in self.roles.items() if spec.kind is RoleKind.ENTITY]
        if len(entity) != 1:
            raise ConfigError(
                "ROLES_NEED_ONE_ENTITY",
                f"Exactly one role must have kind 'entity'; found {len(entity)}.",
                path="roles",
            )
        return self

    @property
    def entity_role(self) -> str:
        """The single role whose kind is `entity`."""
        return next(name for name, spec in self.roles.items() if spec.kind is RoleKind.ENTITY)

    @property
    def event_roles(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, spec in self.roles.items() if spec.is_event))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.roles))

    def get(self, role: str) -> RoleSpec | None:
        return self.roles.get(role)

    def require(self, role: str, *, path: str = "role") -> RoleSpec:
        """The role, or a `ConfigError` that lists the ones that exist."""
        spec = self.roles.get(role)
        if spec is None:
            raise ConfigError(
                "ROLE_UNKNOWN",
                f"{role!r} is not a role; configs/roles.yaml defines {', '.join(self.names)}.",
                path=path,
            )
        return spec


ROLES_FILENAME: Final[str] = "roles.yaml"
_ROLES_CACHE: dict[Path, RoleCatalogue] = {}


def load_role_catalogue(root: Path | None = None) -> RoleCatalogue:
    """`configs/roles.yaml`, cached per root.

    Engine data, like `engine.yaml:catalog`: it is never merged into a use case and a use-case file
    cannot invent, rename or remap a role. A checkout without the file is an error rather than an
    empty catalogue, because "this client has no roles" and "the engine lost its role table" are
    different problems and only one of them is the user's.
    """
    base = config_root(root)
    cached = _ROLES_CACHE.get(base)
    if cached is not None:
        return cached
    document = load_yaml(base / ROLES_FILENAME)
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            "ROLES_SCHEMA_VERSION",
            f"roles.yaml declares schema_version {version!r}; this engine reads {SCHEMA_VERSION}.",
            path="schema_version",
        )
    try:
        catalogue = RoleCatalogue.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = _dotted_loc(first["loc"])
        raise ConfigError("CONFIG_INVALID", f"{dotted}: {first['msg']}.", path=dotted) from exc
    _ROLES_CACHE[base] = catalogue
    return catalogue


def get_roles(root: Path | None = None) -> RoleCatalogue:
    """The role catalogue of `root`; the Phase 2 counterpart of `get_catalog`."""
    return load_role_catalogue(root)


# `UseCaseConfig` declares four fields whose types are defined above in this block, so pydantic left
# them as unresolved forward references when the class was created. Rebuilding here - inside the
# block, after the types exist - completes it, and the models that nest it have to be rebuilt too
# because each cached a reference to the incomplete schema.
UseCaseConfig.model_rebuild()
ResolvedConfig.model_rebuild()

# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----

# ---- PHASE-4B (production) — append only below this line ----
# ---- END PHASE-4B ----
# ---- PHASE-3B (uplift) — append only below this line ----
# Phase 3b keeps its configuration types in `engine/uplift/config.py`, not here: Phase 2's
# `UseCaseConfig.model_rebuild()` above runs before this block, so a forward reference to a class
# defined here would fail it. That module imports nothing from this one (DEC-601).
# ---- END PHASE-3B ----
