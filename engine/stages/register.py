"""Register stage (M3): build the registry record, the feature schema and the drift baseline.

This module owns **both halves of the drift contract**: it writes `drift_baseline.json` and it
projects a scoring frame onto that baseline for `engine.stages.score.compute_drift`. PSI is
meaningless when the two sides bin differently, so the binning lives in one module and the
consumer imports it rather than re-deriving it.

Binning scheme
--------------
- **Numeric features are binned by quantile (equal frequency), not by equal width.** The edges are
  the `NUMERIC_BIN_COUNT + 1` quantiles of the training column (`numpy.quantile`, linear
  interpolation, the default), de-duplicated. Equal-frequency bins give every bin roughly the same
  training mass, which is what keeps PSI stable on skewed columns: with equal-width bins a column
  such as `avg_monthly_spend` puts 95 % of its rows in one bin, the other bins are empty, and the
  zero-count epsilon then dominates the sum. `bin_count` on the artefact is the *requested* count;
  a column with fewer distinct quantiles (a constant, a highly skewed or a low-cardinality integer
  column) keeps fewer bins after de-duplication.
- **Edges are stored as finite numbers**, the training minimum and maximum being the outermost
  ones, and a bin covers `[lower, upper)` except the last, which covers `[lower, upper]`. Scoring
  data whose range differs is therefore still binnable: a value below the first edge or above the
  last is **clamped into the outermost bin** (the tails are read as open-ended). Clamping keeps PSI
  finite and the artefact strict JSON - `-inf`/`+inf` edges would serialise as `Infinity`, which no
  other JSON reader accepts. A range shift still shows as drift, because the mass piles up in one
  edge bin and empties the others.
- **A degenerate (constant) numeric column** keeps a single zero-width bin `[v, v]`. Zero-width
  bins are matched by equality, not by clamping, so at scoring time a different constant lands in
  the out-of-range slot instead of being silently absorbed.
- **Categorical features keep a frequency table** of every observed level, most frequent first,
  capped at `MAX_CATEGORY_LEVELS` levels; the tail of a higher-cardinality column is summed into
  one `OTHER_CATEGORY` bucket, which is also where unseen levels land at scoring time when the
  bucket exists. (A real level literally called `__other__` would merge with that bucket.)
- **Shares are computed over the non-missing values** of the column, so they sum to one, and what
  is missing is recorded separately as `null_rate`. For a numeric column "missing" means *not a
  finite number* (null, `NaT`, `±inf`); for a categorical one it means null. The same rule is
  applied to the scoring frame, so the two null rates are comparable.
- The projection of a scoring frame onto a baseline feature always yields one share per stored bin
  or level **plus one trailing slot** for mass that belongs to none of them (numeric values outside
  a degenerate bin, categories unseen at training time). Its expected share is zero, so the
  consumer's epsilon turns it into a large, finite PSI contribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from engine.config import ColumnType
from engine.contracts import (
    MODEL_DIRECTORY,
    CategoryCount,
    DriftBaseline,
    FeatureBaseline,
    FeatureSchema,
    FeatureSchemaColumn,
    HistogramBin,
    ModelStatus,
    ModelVersion,
)
from engine.registry import RegistryError, should_promote
from engine.storage import run_key
from engine.utils.ids import new_model_id
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Literal

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import Metric, UseCaseConfig
    from engine.contracts import BestModel
    from engine.pipeline import StageContext
    from engine.registry import ModelRegistry

__all__ = [
    "MAX_CATEGORY_LEVELS",
    "NUMERIC_BIN_COUNT",
    "OTHER_CATEGORY",
    "ChampionScore",
    "build_model_version",
    "drift_baseline",
    "feature_schema",
    "next_version_id",
    "store_model_version",
]

NUMERIC_BIN_COUNT: Final[int] = 10
"""Histogram bins requested for a numeric feature: the training deciles (plan §6.3, register)."""

MAX_CATEGORY_LEVELS: Final[int] = 50
"""Levels kept in a frequency table before the tail is summed into `OTHER_CATEGORY`."""

OTHER_CATEGORY: Final[str] = "__other__"
"""Reserved level holding the tail of a high-cardinality column, and unseen levels at score time."""

SCHEMA_FILENAME: Final[str] = "schema.json"
RUN_CONFIG_FILENAME: Final[str] = "run_config.json"
DRIFT_BASELINE_FILENAME: Final[str] = "drift_baseline.json"

PROMOTED_BY: Final[str] = "engine"
"""Who the registry records as the promoter when the champion rule promotes automatically."""

SHARE_DECIMALS: Final[int] = 6
IMPROVEMENT_DECIMALS: Final[int] = 2


# ---------------------------------------------------------------------------
# The registry record
# ---------------------------------------------------------------------------
def next_version_id(ctx: StageContext) -> tuple[str, int]:
    """The `(model id, version number)` the model of this run gets.

    The version number is the registry's next one for the use case, so the first model of a use
    case is version 1 and every later run of the same use case counts on from there, independently
    of the other use cases. The id is the one the run was started with when the caller fixed it
    (`StageContext.model_version_id`, which `schema.json` is written with), else `m_<use case>_<n>`.
    """
    version = ctx.registry.next_version(ctx.config.id)
    model_id = (
        ctx.model_version_id if ctx.model_version_id is not None else new_model_id(ctx.config.id, version)
    )
    return model_id, version


@dataclass(frozen=True, slots=True)
class ChampionScore:
    """How the incumbent champion did **on this run's own held-out frame**.

    The champion rule compares like with like: a stored `test_score` was measured on whatever test
    split existed when that model was trained, so comparing a new score against it compares two
    different datasets, preprocessings and populations at once. The train flow therefore re-scores
    the current champion on the challenger's test split (`engine.stages.evaluate.evaluate(model,
    test data, evaluation config)`) and hands the result here.

    Exactly one of `test_score` and `unavailable_reason` is set: the score when the champion could
    be re-scored, the reason when it could not (its predictor is missing from storage, or it was
    fitted on a schema this test frame does not satisfy). `metric` is the metric it was re-scored
    on and must be the challenger's; `model_id` is the champion that was re-scored, checked against
    the registry so a stale comparison cannot promote anything.
    """

    model_id: str
    metric: Metric | None = None
    test_score: float | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        scored = self.test_score is not None and self.metric is not None
        if scored == (self.unavailable_reason is not None):
            raise ValueError(
                "A ChampionScore carries either a metric and a test score measured on this run's "
                "test frame, or an unavailable_reason - never both and never neither."
            )


def build_model_version(
    ctx: StageContext,
    best: BestModel,
    schema: FeatureSchema,
    *,
    champion_score: ChampionScore | None = None,
) -> ModelVersion:
    """Assemble the `ModelVersion` record for the model this run produced (plan §6.3, register).

    The record carries the metric the score was measured in and its catalog label, the storage keys
    of the schema, the resolved run configuration, the predictor directory and the drift baseline,
    and a status chosen by the champion rule and the approval setting.

    **The rule compares two scores measured on the same held-out frame.** `best.test_score` is the
    challenger on this run's test split; `champion_score` is the incumbent champion re-scored on
    that same split by the caller. `engine.registry.should_promote` does the arithmetic, unchanged
    and never re-implemented here, but it is handed the *re-measured* champion score rather than
    the one stored with that version: the stored score belongs to an older test set and the plan's
    "beats the champion" is only meaningful within one frame.

    The cases, in the order they are decided:

    - **no champion yet**: promote, nothing to re-score, `champion_score` is ignored;
    - **a champion exists and `champion_score` is None, names another model, or carries an
      `unavailable_reason`** (its predictor is gone, or it cannot score a frame built for a
      different feature schema): the comparison did not happen, so the version stays a `candidate`
      and a WARNING says why. Promoting on an untested comparison would crown a model nobody
      measured; refusing keeps the champion that is known to work, and `POST /models/{id}/promote`
      is still there for a human who has looked;
    - **the two models were optimised for different metrics**: the registry's `METRIC_MISMATCH`
      guard applies, here too the version stays a `candidate` instead of failing the run;
    - **the rule promotes and `governance.approval_required` is set**: the version is
      `pending_approval` and `POST /models/{id}/approve` crowns it; the registry records the
      champion it replaces at that moment, so `previous_champion_id` is still empty here;
    - **the rule promotes and approval is off**: the version is the `champion` straight away, and
      records the champion it replaced and by how much it won.

    `improvement_pct` is filled whenever the version promotes and the improvement is defined (there
    is a re-scored champion and its re-measured score is not exactly zero); it is signed the way the
    metric reads, so a 1 % smaller RMSE is `+1.0`. The keys point at the run directory, which is
    where the training run writes these artefacts; nothing is copied here.
    """
    config = ctx.config
    created_at = utc_now()
    model_id, version = next_version_id(ctx)
    predictor_key = best.predictor_key
    schema_key = run_key(ctx.run_id, SCHEMA_FILENAME)
    run_config_key = run_key(ctx.run_id, RUN_CONFIG_FILENAME)
    drift_baseline_key = run_key(ctx.run_id, DRIFT_BASELINE_FILENAME)
    candidate = ModelVersion(
        model_id=model_id,
        use_case_id=config.id,
        version=version,
        run_id=ctx.run_id,
        created_at=created_at,
        status=ModelStatus.CANDIDATE,
        metric=best.metric,
        metric_label=config.catalog.metric_label(best.metric),
        test_score=best.test_score,
        validation_score=best.validation_score,
        model_display_name=best.display_name,
        schema_key=schema_key,
        run_config_key=run_config_key,
        predictor_key=predictor_key,
        drift_baseline_key=drift_baseline_key,
        artefact_keys={
            SCHEMA_FILENAME: schema_key,
            RUN_CONFIG_FILENAME: run_config_key,
            DRIFT_BASELINE_FILENAME: drift_baseline_key,
            MODEL_DIRECTORY: predictor_key,
        },
        approved_by=None,
        approved_at=None,
        promoted_at=None,
        promoted_by=None,
        promotion_note=None,
        previous_champion_id=None,
        improvement_pct=None,
        engine_version=_engine_version(),
        autogluon_version=_autogluon_version(),
    )
    if schema.model_version_id != candidate.model_id:
        raise ValueError(
            f"The schema was written for model {schema.model_version_id!r} but this run registers "
            f"{candidate.model_id!r}; both must come from next_version_id()."
        )
    champion = ctx.registry.get_champion(config.id)
    promote, improvement_pct = _champion_decision(candidate, champion, champion_score, config)
    if not promote:
        return candidate
    if config.governance.approval_required:
        return candidate.model_copy(
            update={"status": ModelStatus.PENDING_APPROVAL, "improvement_pct": improvement_pct}
        )
    return candidate.model_copy(
        update={
            "status": ModelStatus.CHAMPION,
            "promoted_at": created_at,
            "promoted_by": PROMOTED_BY,
            "promotion_note": _promotion_note(candidate, champion, improvement_pct),
            "previous_champion_id": None if champion is None else champion.model_id,
            "improvement_pct": improvement_pct,
        }
    )


def store_model_version(registry: ModelRegistry, version: ModelVersion) -> ModelVersion:
    """Write the version `build_model_version` decided on to the registry, and return what was stored.

    A `candidate` or a `pending_approval` version is stored as it stands. A version the champion
    rule promoted is stored as a candidate and then promoted, because only `ModelRegistry.promote`
    demotes the incumbent champion in the same transaction (DEC-028): registering a second row with
    `status: champion` would leave the use case with two. The stored record therefore carries the
    registry's own `promoted_at` and the `previous_champion_id` the database saw, which is why this
    function returns it: it, not the argument, is what the run record should link to.
    """
    if version.status is not ModelStatus.CHAMPION:
        return registry.register(version)
    stored = registry.register(
        version.model_copy(
            update={
                "status": ModelStatus.CANDIDATE,
                "promoted_at": None,
                "promoted_by": None,
                "previous_champion_id": None,
            }
        )
    )
    note = version.promotion_note if version.promotion_note is not None else ""
    return registry.promote(stored.model_id, by=version.promoted_by or PROMOTED_BY, note=note)


def _champion_decision(
    candidate: ModelVersion,
    champion: ModelVersion | None,
    champion_score: ChampionScore | None,
    config: UseCaseConfig,
) -> tuple[bool, float | None]:
    """`(promote?, improvement %)` for `candidate`, decided on one frame's scores.

    Neither a metric mismatch nor a champion that could not be re-scored is an error here: both
    mean "this comparison did not happen", which leaves the version a candidate.
    """
    greater_is_better = config.catalog.metrics[candidate.metric].greater_is_better
    rescored = _rescored_champion(candidate, champion, champion_score)
    if champion is not None and rescored is None:
        return False, None
    try:
        promote = should_promote(
            candidate,
            rescored,
            config.evaluation.champion_min_improvement_pct,
            greater_is_better=greater_is_better,
        )
    except RegistryError as error:
        if error.code != "METRIC_MISMATCH":
            raise
        return False, None
    return promote, _improvement_pct(candidate, rescored, greater_is_better=greater_is_better)


def _rescored_champion(
    candidate: ModelVersion, champion: ModelVersion | None, champion_score: ChampionScore | None
) -> ModelVersion | None:
    """The champion carrying the score it achieved on *this* run's test frame, or None.

    None means there is nothing to compare against: either there is no champion at all (the caller
    checks that case separately) or the re-scoring did not happen, which is logged at WARNING with
    the reason, because a promotion decided on an older test set's number is exactly what the
    champion rule must not do.
    """
    if champion is None:
        return None
    logger = get_logger(__name__)
    reason: str
    if champion_score is None:
        reason = "it was not re-scored on this run's test split"
    elif champion_score.model_id != champion.model_id:
        reason = (
            f"the score offered was measured for {champion_score.model_id!r}, "
            f"which is no longer the champion"
        )
    elif champion_score.unavailable_reason is not None:
        reason = champion_score.unavailable_reason
    elif champion_score.metric != candidate.metric:
        reason = (
            f"it was re-scored on {champion_score.metric} but the challenger is measured in "
            f"{candidate.metric}"
        )
    else:
        return champion.model_copy(update={"test_score": champion_score.test_score})
    logger.warning(
        "stage=register promotion=skipped model=%s champion=%s reason=%s",
        candidate.model_id,
        champion.model_id,
        reason,
    )
    return None


def _improvement_pct(
    candidate: ModelVersion, champion: ModelVersion | None, *, greater_is_better: bool
) -> float | None:
    """How much better than the champion the candidate is, in percent of the champion's score.

    `champion` is the incumbent carrying its **re-measured** score, so both numbers come from the
    same test frame. `None` when there is no champion to compare with, or when the re-measured
    score is exactly zero and a relative improvement is undefined (DEC-035). The sign follows the
    metric: lower is better flips it.
    """
    if champion is None or champion.test_score == 0.0:
        return None
    delta = candidate.test_score - champion.test_score
    signed_delta = delta if greater_is_better else -delta
    return round(signed_delta / abs(champion.test_score) * 100.0, IMPROVEMENT_DECIMALS)


def _promotion_note(
    candidate: ModelVersion, champion: ModelVersion | None, improvement_pct: float | None
) -> str:
    """The one line the registry stores about why this version became the champion."""
    if champion is None:
        return f"First model registered for {candidate.use_case_id}."
    measured = "both re-scored on this run's test split"
    if improvement_pct is None:
        return f"Beats {champion.model_id} on {candidate.metric_label}, {measured}."
    return (
        f"Beats {champion.model_id} by {improvement_pct:+.{IMPROVEMENT_DECIMALS}f}% "
        f"on {candidate.metric_label}, {measured}."
    )


def _engine_version() -> str:
    """The engine version that trained the model."""
    import engine

    return engine.__version__


def _autogluon_version() -> str:
    """The installed AutoGluon version, read from the distribution metadata.

    AutoGluon itself is never imported here: `engine.stages.register` must stay importable in
    milliseconds (`tests/integration/test_engine_imports.py`).
    """
    from importlib.metadata import PackageNotFoundError, version

    for distribution in ("autogluon.tabular", "autogluon"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


# ---------------------------------------------------------------------------
# schema.json (plan §4.4)
# ---------------------------------------------------------------------------
def feature_schema(
    train: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    target: str | None,
    model_version_id: str,
) -> FeatureSchema:
    """The exact feature schema the model was fitted on, saved as `schema.json` (plan §4.4).

    `columns` holds the feature columns of `train` in fit order - the primary key, the target and
    every other column the configuration reserves (time, group, fairness, consent, opt-out and
    recent-contact columns, plus `prepare.exclude_columns`) are not features and are left out. Each
    column records its inferred type, whether a scoring file must contain it (every fitted feature
    must), whether nulls were seen at fit time, the observed levels of a categorical column when
    there are few enough of them, and the numeric range.

    `nullable` states what was *observed*, so the score-time check can report a column that has
    suddenly become null; it is information for that check, not a rule this stage enforces.
    """
    columns = tuple(
        _schema_column(train, name) for name in _feature_columns(train, config, excluded=(primary_key,))
    )
    return FeatureSchema(
        use_case_id=config.id,
        model_version_id=model_version_id,
        primary_key=primary_key,
        target=target,
        problem_type=config.problem_type,
        columns=columns,
        row_count_at_fit=len(train.index),
        created_at=utc_now(),
    )


def _schema_column(frame: pd.DataFrame, name: str) -> FeatureSchemaColumn:
    """One `schema.json` column: its type, nullability, levels and range as seen at fit time."""
    inferred_type = _inferred_type(frame, name)
    nullable = bool(frame[name].isna().any())
    categories: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    if _feature_kind(frame, name) == "categorical":
        labels = tuple(sorted(_label_counts(frame, name)))
        categories = labels if len(labels) <= MAX_CATEGORY_LEVELS else ()
    elif inferred_type in {ColumnType.INTEGER, ColumnType.FLOAT}:
        values = _finite_values(frame, name)
        if values.size:
            minimum = float(values.min())
            maximum = float(values.max())
    return FeatureSchemaColumn(
        name=name,
        inferred_type=inferred_type,
        required=True,
        nullable=nullable,
        categories=categories,
        minimum=minimum,
        maximum=maximum,
    )


def _inferred_type(frame: pd.DataFrame, name: str) -> ColumnType:
    """The engine-level type of a fitted column, from the dtype the model was fitted with.

    Only the five types a fitted frame can still carry are produced here: `date` and `text` are
    finer distinctions the ingest stage draws on the *uploaded* file, and a prepared frame no
    longer distinguishes them from `datetime` and `string`.
    """
    import pandas as pd

    dtype = frame[name].dtype
    if pd.api.types.is_bool_dtype(dtype):
        return ColumnType.BOOLEAN
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return ColumnType.DATETIME
    if pd.api.types.is_integer_dtype(dtype):
        return ColumnType.INTEGER
    if pd.api.types.is_float_dtype(dtype):
        return ColumnType.FLOAT
    return ColumnType.STRING


# ---------------------------------------------------------------------------
# drift_baseline.json (plan §6.3, register)
# ---------------------------------------------------------------------------
def drift_baseline(
    train: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    model_version_id: str,
    primary_key: str | None = None,
) -> DriftBaseline:
    """Summarise the training distribution per feature, so scoring runs can measure drift.

    One entry per feature, in fit order, over the same columns as `feature_schema`: a quantile
    histogram for a numeric feature (with its mean and standard deviation) and a frequency table
    for a categorical one, each with the share of the column that was missing. The scheme, and why
    it is the one that makes PSI computable later, is documented at the top of this module.

    `primary_key` is optional so the stub's signature still calls: pass it whenever it is known, so
    the baseline covers exactly the columns `schema.json` lists.
    """
    excluded: tuple[str, ...] = () if primary_key is None else (primary_key,)
    features = tuple(
        _feature_baseline(train, name) for name in _feature_columns(train, config, excluded=excluded)
    )
    return DriftBaseline(
        run_id=run_id,
        model_version_id=model_version_id,
        rows=len(train.index),
        bin_count=NUMERIC_BIN_COUNT,
        features=features,
        created_at=utc_now(),
    )


def _feature_baseline(frame: pd.DataFrame, name: str) -> FeatureBaseline:
    """The training distribution of one column: a quantile histogram or a frequency table."""
    rows = len(frame.index)
    if _feature_kind(frame, name) == "numeric":
        values = _finite_values(frame, name)
        present = int(values.size)
        return FeatureBaseline(
            feature=name,
            kind="numeric",
            null_rate=_rate(rows - present, rows),
            bins=_histogram(values),
            categories=(),
            mean=round(float(values.mean()), SHARE_DECIMALS) if present else None,
            std=round(float(values.std()), SHARE_DECIMALS) if present else None,
        )
    counts = _label_counts(frame, name)
    present = sum(counts.values())
    return FeatureBaseline(
        feature=name,
        kind="categorical",
        null_rate=_rate(rows - present, rows),
        bins=(),
        categories=_category_table(counts, present),
        mean=None,
        std=None,
    )


def _histogram(values: npt.NDArray[np.float64]) -> tuple[HistogramBin, ...]:
    """The quantile histogram of `values`; one zero-width bin when the column is constant."""
    import numpy as np

    if values.size == 0:
        return ()
    edges = np.unique(np.quantile(values, np.linspace(0.0, 1.0, NUMERIC_BIN_COUNT + 1)))
    total = int(values.size)
    if edges.size < 2:
        constant = float(edges[0])
        return (HistogramBin(lower=constant, upper=constant, count=total, share=1.0),)
    counts, _ = np.histogram(values, bins=edges)
    return tuple(
        HistogramBin(
            lower=float(edges[index]),
            upper=float(edges[index + 1]),
            count=int(count),
            share=_rate(int(count), total),
        )
        for index, count in enumerate(counts)
    )


def _category_table(counts: dict[str, int], present: int) -> tuple[CategoryCount, ...]:
    """The frequency table of a categorical column, most frequent first, tail bucketed."""
    if present == 0:
        return ()
    kept = list(counts.items())[:MAX_CATEGORY_LEVELS]
    tail = present - sum(count for _, count in kept)
    if tail > 0:
        merged = dict(kept)
        merged[OTHER_CATEGORY] = merged.get(OTHER_CATEGORY, 0) + tail
        kept = list(merged.items())
    return tuple(
        CategoryCount(value=value, count=count, share=_rate(count, present)) for value, count in kept
    )


# ---------------------------------------------------------------------------
# The projection scoring reads (used by engine.stages.score.compute_drift)
# ---------------------------------------------------------------------------
def _feature_shares(
    feature: FeatureBaseline, frame: pd.DataFrame
) -> tuple[tuple[float, ...], tuple[float, ...], float]:
    """Project `frame` onto one baseline feature: `(expected, actual, null rate)`.

    The two share vectors are aligned and one longer than the stored bins or levels: the last slot
    holds the mass that belongs to none of them (a numeric value outside a degenerate bin, a
    category unseen at training time), and its expected share is zero. Both vectors are empty when
    the baseline holds no distribution at all (a column already entirely null at training time),
    which is the "nothing to compare" case; the current null rate is still measured.

    A feature missing from `frame` is treated exactly like a feature that is present and entirely
    null: no mass anywhere, and a current null rate of one.
    """
    stored = tuple(bin_.share for bin_ in feature.bins) + tuple(
        category.share for category in feature.categories
    )
    if feature.feature not in frame.columns:
        if not stored:
            return (), (), 1.0
        return (*stored, 0.0), (0.0,) * (len(stored) + 1), 1.0
    rows = len(frame.index)
    if feature.kind == "numeric":
        values = _finite_values(frame, feature.feature)
        null_rate = _rate(rows - int(values.size), rows)
        if not stored:
            return (), (), null_rate
        return (*stored, 0.0), _numeric_actual(values, feature.bins), null_rate
    counts = _label_counts(frame, feature.feature)
    null_rate = _rate(rows - sum(counts.values()), rows)
    if not stored:
        return (), (), null_rate
    return (*stored, 0.0), _categorical_actual(counts, feature.categories), null_rate


def _numeric_actual(values: npt.NDArray[np.float64], bins: tuple[HistogramBin, ...]) -> tuple[float, ...]:
    """The share of `values` in each baseline bin, plus the share outside every bin."""
    import numpy as np

    total = int(values.size)
    if total == 0:
        return (0.0,) * (len(bins) + 1)
    edges = np.array([bin_.lower for bin_ in bins] + [bins[-1].upper], dtype=float)
    if edges[0] == edges[-1]:  # a constant baseline: equality, never clamping
        inside = int(np.count_nonzero(values == edges[0]))
        return (_rate(inside, total),) * len(bins) + (_rate(total - inside, total),)
    counts, _ = np.histogram(np.clip(values, edges[0], edges[-1]), bins=edges)
    return (*(_rate(int(count), total) for count in counts), 0.0)


def _categorical_actual(counts: dict[str, int], categories: tuple[CategoryCount, ...]) -> tuple[float, ...]:
    """The share of each baseline level in `counts`, plus the share of the levels not in it."""
    total = sum(counts.values())
    if total == 0:
        return (0.0,) * (len(categories) + 1)
    index_of = {category.value: index for index, category in enumerate(categories)}
    other_index = index_of.get(OTHER_CATEGORY)
    matched = [0] * len(categories)
    unseen = 0
    for label, count in counts.items():
        index = index_of.get(label, other_index)
        if index is None:
            unseen += count
        else:
            matched[index] += count
    return (*(_rate(count, total) for count in matched), _rate(unseen, total))


# ---------------------------------------------------------------------------
# Column helpers shared by the schema, the baseline and the projection
# ---------------------------------------------------------------------------
def _feature_columns(
    frame: pd.DataFrame, config: UseCaseConfig, *, excluded: Iterable[str] = ()
) -> tuple[str, ...]:
    """The feature columns of `frame`, in frame order, with the reserved columns removed.

    Reserved means: the target, the time, group, fairness, consent, opt-out and recent-contact
    columns the configuration names, everything in `prepare.exclude_columns`, and whatever the
    caller adds (the primary key, which the configuration only knows as a hint). Plan §4.1: the
    primary key is never a feature.
    """
    reserved = {
        config.target.column,
        config.split.time_column,
        config.split.group_column,
        config.evaluation.fairness_column,
        config.governance.consent_column,
        config.actions.suppression.opt_out_column,
        config.actions.suppression.recently_contacted_column,
        *config.prepare.exclude_columns,
        *excluded,
    }
    return tuple(str(name) for name in frame.columns if str(name) not in reserved)


def _feature_kind(frame: pd.DataFrame, name: str) -> Literal["numeric", "categorical"]:
    """Whether a column is summarised as a histogram or as a frequency table.

    Booleans are categorical (two levels say more than a two-bin histogram); datetimes are numeric,
    compared as epoch seconds; everything else follows the dtype.
    """
    import pandas as pd

    dtype = frame[name].dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "categorical"
    if pd.api.types.is_numeric_dtype(dtype) or pd.api.types.is_datetime64_any_dtype(dtype):
        return "numeric"
    return "categorical"


def _finite_values(frame: pd.DataFrame, name: str) -> npt.NDArray[np.float64]:
    """Every finite value of a numeric column as float64; datetimes become epoch seconds.

    Nulls, `NaT` and infinities are dropped together - "missing" for a numeric column means "not a
    finite number", one rule for the training frame and the scoring frame alike. A column that does
    not parse as a number at all (a string column where a number is expected) yields no values,
    which the caller reports as a fully null column rather than a crash.
    """
    import numpy as np
    import pandas as pd

    column = frame[name]
    if pd.api.types.is_datetime64_any_dtype(column.dtype):
        # `astype("int64")` turns NaT into the int64 minimum, which is a perfectly finite number
        # and would sit in the first bin; the mask puts the missing value back.
        numeric = column.astype("int64").astype("float64").where(column.notna()) / 1_000_000_000
    else:
        numeric = pd.to_numeric(column, errors="coerce")
    values = np.asarray(numeric.to_numpy(dtype="float64", na_value=np.nan), dtype=np.float64)
    finite: npt.NDArray[np.float64] = values[np.isfinite(values)]
    return finite


def _label_counts(frame: pd.DataFrame, name: str) -> dict[str, int]:
    """How often each level of a categorical column occurs, most frequent first, ties by label.

    Every level is stringified the one way (`str`), in the training frame and the scoring frame
    alike, so `1` and `"1"` are the same level on both sides. Nulls are not counted.
    """
    column = frame[name].dropna()
    counts: dict[str, int] = {}
    for value, count in column.astype(str).value_counts().items():
        counts[str(value)] = int(count)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _rate(part: int, whole: int) -> float:
    """`part / whole` as a rounded share, and zero when there is nothing to divide."""
    if whole <= 0:
        return 0.0
    return round(part / whole, SHARE_DECIMALS)
