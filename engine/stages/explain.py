"""Explain stage: the global importance chart (M3) and the per-row reasons (M3 train, M4 score).

Explanation is **measurement, never selection**. Nothing this module computes changes the model,
the decision threshold, the calibrator or the feature set: the model is already chosen and already
fitted when the first line here runs, and the artefacts it writes are read by the UI and by nothing
else in the engine (design section 6, plan section 6.3).

> **THE TEST SPLIT IS FINAL-DECISION-ONLY.** :func:`global_importance` reads the hold-out, and may:
> it MEASURES an already-chosen model rather than choosing anything. AutoGluon shuffles one feature
> of the test frame and re-scores it, and the number that comes back is reported, not acted on. No
> feature is dropped, no model is re-ranked and no threshold moves because of anything in this file.

**How many rows get a reason is the caller's choice, never this module's.** Every entry point
takes `max_rows`: :const:`EXPLAIN_MAX_ROWS` for a training run, which explains a sample because the
artefact is a chart, and `None` for a scoring run, because plan section 6.3 wants a reason on every
exported row and a sampled scoring file would reach `scores.csv` with most reason cells empty.
:func:`reason_columns` is the one adapter between this stage and `engine.stages.export`: it joins
the explanations onto the scored frame **by primary key**, never by row position, and refuses a
frame it cannot cover rather than leaving a row unexplained.

**The three tiers, in order (plan section 6.3).** TreeSHAP on the best single tree model; else
KernelSHAP on a sample of about a thousand rows; else a permutation-based local importance that
needs no `shap` at all and therefore always works. A tier that raises is logged at WARNING and the
next one runs; :class:`RowReasons` carries the tier that actually produced the numbers, which is
what the Running screen's detail line names.

**The tiers also run per row, not only per run (DEC-056).** A tier can answer for a whole frame and
still measure *one* row's every contribution as zero - a prediction so saturated that moving a
single feature does not move it is the usual cause - and a zero is not a reason, so that row would
otherwise reach `scores.csv` with empty cells. The tiers therefore run again on exactly those rows,
in the same order, and a row no tier can measure keeps **general** reasons instead: the run's most
important features, carrying that row's own values, with `Direction.NONE`, a zero contribution and
`(general)` in the sentence. `RowReasons.fallback_rows` counts every row that needed either, and
reaches the Output page as `ScoringSummary.rows_with_fallback_reasons`.

**Reason text.** `Reason.text` is built from the feature, its value and the direction, and from
nothing else: `"visits_last_7d ↑ (12)"`, `"plan_tier = basic"`, `"last_contacted_at missing"`.
The plan's example reads `"visits_last_7d ↑ (12 visits)"`; the unit `"visits"` exists in no
config and in no column, so rendering it would be invention (plan section 13.3, DEC-070).

`shap`, `pandas`, `numpy` and `pyarrow` are imported inside function bodies, so importing the
engine stays free of them. That is also why the parquet schema is :func:`row_explanation_schema`, a
cached function, rather than the module-level constant the design sketched: a `pa.Schema` object
cannot exist before `pyarrow` is imported.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Final, Protocol, cast

from engine.config import ModelFamily
from engine.contracts import (
    Direction,
    FeatureImportance,
    FeatureImportanceItem,
    Reason,
    ReasonMethod,
    RowExplanation,
)
from engine.stages.scorer import load_scorer, to_numpy_dtypes
from engine.stages.train import family_for_model_name
from engine.storage import run_key
from engine.utils.ids import seed_from
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import numpy as np
    import numpy.typing as npt
    import pandas as pd
    import pyarrow as pa

    from engine.config import UseCaseConfig
    from engine.storage import Storage

    FloatArray = npt.NDArray[np.float64]

__all__ = [
    "EXPLAIN_MAX_ROWS",
    "GENERAL_SUFFIX",
    "IMPORTANCE_CAPTION",
    "IMPORTANCE_UNAVAILABLE_CAPTION",
    "KERNEL_SHAP_MAX_ROWS",
    "MISSING_VALUE",
    "REASON_COLUMN_PREFIX",
    "ROW_EXPLANATIONS_FILENAME",
    "TOP_FEATURES",
    "ReasonMethod",
    "RowReasons",
    "RowScorer",
    "build_feature_importance",
    "build_row_explanations",
    "explain_detail",
    "fallback_note",
    "format_value",
    "global_importance",
    "read_row_explanations",
    "reason_column_names",
    "reason_columns",
    "reason_text",
    "reasons_for",
    "reasons_for_row",
    "row_explanation_schema",
    "row_reasons",
    "unavailable_importance",
    "with_reason_columns",
    "write_row_explanations",
]

_LOGGER = get_logger(__name__)

TOP_FEATURES: Final[int] = 20
"""Features the global chart keeps, most important first (plan section 6.3)."""

EXPLAIN_MAX_ROWS: Final[int] = 5000
"""The **train** flow's row budget: above it a seeded sample is drawn, in the frame's own row order.

It is a default, not a rule. `max_rows` is a per-call argument everywhere in this module because the
two flows want opposite answers: a training run explains a sample to draw a chart, while a scoring
run must put a reason on every row it exports (plan section 6.3) and so passes `max_rows=None`.
"""

KERNEL_SHAP_MAX_ROWS: Final[int] = 1000
"""Rows the KernelSHAP tier explains - plan section 6.3's "KernelSHAP on a 1,000-row sample".

This tier is the one whose cost is genuinely per row: every row costs about `2048 + 2 * features`
model evaluations over the background sample, measured at roughly a fifth of a second per row for a
twenty-feature model, so a hundred thousand rows would take hours rather than minutes. When a caller
has asked for every row the tier therefore **declines above this cap** instead of sampling behind the
caller's back, and the permutation tier - one batched prediction per feature, tens of microseconds a
row - explains all of them. `RowReasons.method` names whichever tier ran, so the Running screen and
the detail line say so.
"""

KERNEL_BACKGROUND_ROWS: Final[int] = 50
"""Background rows KernelSHAP integrates over. More is slower without being more informative."""

PERMUTATION_FEATURES: Final[int] = 10
"""Features the permutation tier perturbs: the top ones by global importance, `K + 1` predictions."""

ROW_EXPLANATIONS_FILENAME: Final[str] = "row_explanations.parquet"

REASON_COLUMN_PREFIX: Final[str] = "reason_"
"""Reason slots are `reason_1 .. reason_n`, exactly as `engine.contracts.scores_csv_columns` names."""

MISSING_VALUE: Final[str] = "missing"
"""How a null reads in `Reason.value`, and the word `reason_text` uses for an absent value."""

UP_ARROW: Final[str] = "↑"
DOWN_ARROW: Final[str] = "↓"

IMPORTANCE_CAPTION: Final[str] = "Permutation importance on the test split (%)"
"""The caption names the method that actually ran, never one the engine did not use (DEC-067)."""

IMPORTANCE_UNAVAILABLE_CAPTION: Final[str] = "Feature importance could not be computed for this model."

TREE_FAMILIES: Final[frozenset[ModelFamily]] = frozenset(
    {ModelFamily.XGBOOST, ModelFamily.LIGHTGBM, ModelFamily.RANDOM_FOREST, ModelFamily.CATBOOST}
)
"""Families TreeSHAP can explain exactly. Anything else falls through to the next tier."""

_SHARE_DECIMALS: Final[int] = 1
_IMPORTANCE_DECIMALS: Final[int] = 6
_CONTRIBUTION_DECIMALS: Final[int] = 6
_SCORE_DECIMALS: Final[int] = 6
_VALUE_DECIMALS: Final[int] = 4
_MIN_IMPORTANCE_SECONDS: Final[float] = 30.0
_MAX_IMPORTANCE_SECONDS: Final[float] = 300.0
_IMPORTANCE_BUDGET_SHARE: Final[float] = 0.1
_NUM_SHUFFLE_SETS: Final[int] = 5
_UNRANKED: Final[int] = 1_000_000

GENERAL_SUFFIX: Final[str] = "(general)"
"""What a general reason's sentence ends with, so a reader can tell it from a measured one."""


class RowScorer(Protocol):
    """What the reason tiers need of a fitted model: its features, its score, its predictor.

    `engine.stages.scorer.AutoGluonScorer` satisfies it. Stating it as a protocol is what lets the
    tiers be exercised without AutoGluon, and keeps this module from reaching into the scorer for
    anything beyond the predictor the two SHAP tiers genuinely need.
    """

    @property
    def feature_columns(self) -> tuple[str, ...]: ...

    @property
    def predictor(self) -> object: ...

    def score(self, frame: pd.DataFrame) -> pd.Series: ...


class _Predictor(Protocol):
    """The slice of AutoGluon's `TabularPredictor` this stage uses, named so the casts are honest."""

    model_best: str

    def feature_importance(
        self,
        data: pd.DataFrame,
        *,
        model: str,
        subsample_size: int,
        num_shuffle_sets: int,
        include_confidence_band: bool,
        silent: bool,
        time_limit: float,
    ) -> pd.DataFrame: ...

    def transform_features(self, data: pd.DataFrame) -> pd.DataFrame: ...

    def predict_proba(
        self, data: pd.DataFrame, *, as_multiclass: bool, transform_features: bool
    ) -> object: ...

    def model_names(self) -> list[str]: ...

    def leaderboard(self, *, display: bool) -> pd.DataFrame: ...


class _TreeModel(Protocol):
    """An AutoGluon model wrapper: the fitted estimator plus the preprocessing it expects."""

    model: object

    def preprocess(self, data: pd.DataFrame) -> object: ...


@dataclass(frozen=True)
class RowReasons:
    """The per-row reasons and the tier that produced them.

    `row_reasons` returns the explanations alone, as the design's signature says; the pipeline calls
    :func:`reasons_for` instead when it needs `method` for the Running screen's detail line.

    `method` is the **primary** tier: the first one that produced contributions for any row.
    Individual rows may carry a later tier's reasons, or general ones, when the primary tier
    measured every one of their contributions as zero; `fallback_rows` counts exactly those, and
    reaches the Output page as `ScoringSummary.rows_with_fallback_reasons` (DEC-056).
    """

    explanations: tuple[RowExplanation, ...]
    method: ReasonMethod
    fallback_rows: int = 0


@dataclass(frozen=True)
class _Contributions:
    """One tier's output: the rows it explained and a signed contribution per row and column."""

    rows: pd.DataFrame
    values: pd.DataFrame
    method: ReasonMethod


@dataclass(frozen=True)
class _TierOutcome:
    """A tier's contributions plus which positions of the frame it was handed they cover."""

    positions: tuple[int, ...]
    found: _Contributions


# ---------------------------------------------------------------------------
# Global importance - permutation on the test split
# ---------------------------------------------------------------------------
def global_importance(
    predictor_key: str,
    test: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    storage: Storage,
) -> FeatureImportance:
    """Permutation feature importance on the test split, top 20 with normalised shares.

    A failure here is never fatal: a run that produced a real model is not discarded because an
    explanation timed out, so the stage logs a WARNING and returns an empty chart (DEC-068).
    """
    started = time.perf_counter()
    try:
        scorer = load_scorer(predictor_key, storage)
        predictor = cast(_Predictor, scorer.predictor)
        features = [column for column in scorer.feature_columns if column in test.columns]
        target = scorer.target_column
        if target not in test.columns:
            _LOGGER.warning(
                "explain: the hold-out frame carries no target column, so permutation importance "
                "cannot be measured; the chart is empty"
            )
            return unavailable_importance(run_id)
        # TEST IS FINAL-DECISION-ONLY. Reading the hold-out here is legitimate because this
        # MEASURES a model that was already chosen on the validation split. It selects nothing:
        # no feature is dropped, no model re-ranked and no threshold moved by what comes back.
        # AutoGluon shuffles a feature and re-scores, which is why the label column must be there.
        frame = to_numpy_dtypes(test[[*features, target]], features)
        raw = predictor.feature_importance(
            data=frame,
            model=predictor.model_best,
            subsample_size=min(EXPLAIN_MAX_ROWS, len(frame)),
            num_shuffle_sets=_NUM_SHUFFLE_SETS,
            include_confidence_band=True,
            silent=True,
            time_limit=_importance_time_limit(config),
        )
        importance = build_feature_importance(raw, run_id=run_id)
    except Exception:
        _LOGGER.warning("explain: permutation importance failed; the chart is empty", exc_info=True)
        return unavailable_importance(run_id)
    log_stage(_LOGGER, "explain.importance", rows=len(test), seconds=time.perf_counter() - started)
    return importance


def _importance_time_limit(config: UseCaseConfig) -> float:
    """A tenth of the training budget, held between 30 and 300 seconds."""
    budget = config.model_search.time_limit_minutes * 60.0 * _IMPORTANCE_BUDGET_SHARE
    return min(_MAX_IMPORTANCE_SECONDS, max(_MIN_IMPORTANCE_SECONDS, budget))


def unavailable_importance(run_id: str) -> FeatureImportance:
    """The empty chart: no items, and a caption that says so rather than an invented number."""
    return FeatureImportance(
        run_id=run_id,
        method="permutation",
        top_n=0,
        items=(),
        caption=IMPORTANCE_UNAVAILABLE_CAPTION,
    )


def build_feature_importance(raw: pd.DataFrame, *, run_id: str) -> FeatureImportance:
    """Turn AutoGluon's importance frame into the artefact: top 20, clipped, shares summing to 100.

    `raw` is indexed by feature name with an `importance` column and, when the confidence band was
    asked for, `stddev` and `p_value` (D13). A negative importance - a feature the model is better
    off without - is real, and is carried through as measured; its **share** is clipped to zero,
    because a negative bar on a normalised chart means nothing.
    """
    if len(raw) == 0 or "importance" not in raw.columns:
        return unavailable_importance(run_id)
    ordered = raw.sort_values("importance", ascending=False, kind="stable").head(TOP_FEATURES)
    importances = [_finite(value) for value in ordered["importance"].tolist()]
    clipped = [0.0 if value is None else max(value, 0.0) for value in importances]
    shares = _normalised_shares(clipped, math.fsum(clipped))
    stddevs = _optional_column(ordered, "stddev")
    p_values = _optional_column(ordered, "p_value")
    items = tuple(
        FeatureImportanceItem(
            rank=position + 1,
            feature=str(name),
            importance=round(importances[position] or 0.0, _IMPORTANCE_DECIMALS),
            share_pct=shares[position],
            stddev=_rounded(stddevs[position], _IMPORTANCE_DECIMALS),
            p_value=_rounded(p_values[position], _IMPORTANCE_DECIMALS),
        )
        for position, name in enumerate(ordered.index.tolist())
    )
    return FeatureImportance(
        run_id=run_id,
        method="permutation",
        top_n=len(items),
        items=items,
        caption=IMPORTANCE_CAPTION,
    )


def _normalised_shares(clipped: Sequence[float], total: float) -> list[float]:
    """Percentages that sum to exactly 100.0, the last item absorbing the rounding residue.

    Every importance clipped to zero means nothing to normalise over, so every share is 0.0: the
    honest answer, and the one the contract's `share_pct` can hold.
    """
    if total <= 0.0:
        return [0.0] * len(clipped)
    shares = [round(value / total * 100.0, _SHARE_DECIMALS) for value in clipped]
    shares[-1] = round(100.0 - math.fsum(shares[:-1]), _SHARE_DECIMALS)
    return shares


def _optional_column(frame: pd.DataFrame, name: str) -> list[float | None]:
    """One column as plain floats, or a column of `None` when the frame does not carry it."""
    if name not in frame.columns:
        return [None] * len(frame)
    return [_finite(value) for value in frame[name].tolist()]


def _scalar(value: object) -> object:
    """The plain Python value behind a numpy or pandas scalar, and anything else unchanged."""
    return value.item() if hasattr(value, "item") else value


def _number(value: object) -> int | float | None:
    """`value` as an `int` or a `float`, numpy scalars included; `None` when it is neither.

    A boolean is not a number here, whatever Python thinks: `True` is a category, and a reason
    about it reads `"opted_in = true"` rather than as an arrow and a 1.
    """
    item = _scalar(value)
    if isinstance(item, bool):
        return None
    if isinstance(item, int | float):
        return item
    return None


def _finite(value: object) -> float | None:
    """`value` as a float, or `None` when it is null, infinite or not a number at all."""
    number = _number(value)
    if number is None:
        return None
    as_float = float(number)
    return as_float if math.isfinite(as_float) else None


def _rounded(value: float | None, decimals: int) -> float | None:
    return None if value is None else round(value, decimals)


# ---------------------------------------------------------------------------
# Per-row reasons - the documented fallback chain
# ---------------------------------------------------------------------------
def row_reasons(
    predictor_key: str,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    storage: Storage,
    max_rows: int | None,
) -> tuple[RowExplanation, ...]:
    """The top `reasons_per_row` reasons for each row of `frame`, from the best tier available.

    `max_rows` has no default on purpose. This is the entry point both flows call, and the answer
    differs between them: the train flow passes :const:`EXPLAIN_MAX_ROWS` and explains a sample,
    while the score flow passes `None` and explains every row, because every row it exports needs a
    reason. A default here would silently make one of the two wrong.

    The seed comes from `predictor_key`, which carries the run id, so the sample a run draws is the
    same in every process and on every machine. A caller that already holds the scorer - the train
    pipeline does - calls :func:`reasons_for` instead and keeps the tier name for the detail line.
    """
    scorer = load_scorer(predictor_key, storage)
    seed = seed_from(predictor_key)
    return reasons_for(
        scorer, frame, config, primary_key=primary_key, seed=seed, max_rows=max_rows
    ).explanations


def reasons_for(
    scorer: RowScorer,
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
    seed: int,
    importance: FeatureImportance | None = None,
    max_rows: int | None = EXPLAIN_MAX_ROWS,
    kernel_max_rows: int | None = KERNEL_SHAP_MAX_ROWS,
) -> RowReasons:
    """Run the tiers in order and turn the first one that works into `RowExplanation`s.

    `max_rows` is how many rows the caller wants explained: `None` means every one of them, which is
    what the score flow asks for. `kernel_max_rows` is the same choice for the KernelSHAP tier
    alone, which is the only expensive-per-row tier; `None` there lifts its cap too. The default
    pair is the train flow's: a five-thousand-row sample, of which KernelSHAP would explain a
    thousand.

    `importance` is the global chart, when one was computed: it settles the tie-break between two
    equally strong contributions, chooses which features the permutation tier perturbs, and ranks
    the general reasons of the last resort below. It orders an explanation and does nothing else -
    least of all change the model.

    **No row is left without a reason (DEC-056).** A tier can measure every one of a row's
    contributions as zero - a saturated prediction that no single feature moves is the usual
    cause - and :func:`reasons_for_row` will not invent a reason out of a zero. Rather than export
    that row with empty cells, the tiers run again *on those rows alone*, in order; and if the last
    one still measures nothing, the row keeps general reasons taken from the importance chart,
    marked `ReasonMethod.GENERAL`, with no direction and a contribution of zero, because that is
    what was actually measured. `RowReasons.fallback_rows` counts every row that needed any of
    this, so the number is reported rather than hidden.
    """
    started = time.perf_counter()
    features = [column for column in scorer.feature_columns if column in frame.columns]
    rank = _importance_rank(importance)
    if len(frame) == 0:
        return RowReasons(explanations=(), method=ReasonMethod.PERMUTATION)
    sample = _sample_rows(frame, max_rows, seed)
    limit = config.evaluation.reasons_per_row

    explained: dict[int, RowExplanation] = {}
    blank: dict[int, RowExplanation] = {}
    strength: dict[str, float] = {}
    primary: ReasonMethod | None = None
    pending = list(range(len(sample)))

    attempted = False
    tiers = _tier_calls(scorer, features, seed, rank, kernel_max_rows, sample, every_row=max_rows is None)
    for tier in tiers:
        if not pending:
            break
        outcome = tier(sample.iloc[pending], not attempted)
        if outcome is None:
            continue  # the tier declined outright; the next one sees the same rows
        attempted = True  # whether or not it moved anything: the rows below are now a RETRY
        found = outcome.found
        totals = _aggregate_columns(found.values, _source_map(_names(found.values), features))
        _accumulate_strength(strength, totals)
        covered = [pending[position] for position in outcome.positions]
        still: list[int] = []
        for position, explanation in zip(
            covered,
            _explanations_from(found, totals, scorer, primary_key=primary_key, limit=limit, rank=rank),
            strict=True,
        ):
            if explanation.reasons:
                # `primary` names a tier some row really carries, so the detail line's two clauses
                # cannot contradict each other (DEC-056).
                if primary is None:
                    primary = found.method
                explained[position] = explanation
            else:
                blank[position] = explanation
                still.append(position)
        pending = still

    # Every row that is still blank, whether the last tier covered it or sampled past it. A row a
    # later tier never looked at is NOT explained - re-exporting the earlier tier's empty tuple
    # would put back exactly the blank cells this function exists to remove - so the whole
    # unrescued set goes through the general fallback together (DEC-056).
    unrescued = sorted(set(blank) - set(explained))
    if unrescued:
        _LOGGER.warning(
            "explain: %d of %d rows measured every contribution as zero; falling back to general "
            "reasons from the importance chart",
            len(unrescued),
            len(sample),
        )
        for position, explanation in zip(
            unrescued,
            _general_explanations(
                sample.iloc[unrescued],
                scorer,
                _general_ranking(importance, strength, features),
                primary_key=primary_key,
                limit=limit,
            ),
            strict=True,
        ):
            explained[position] = explanation

    method = ReasonMethod.PERMUTATION if primary is None else primary
    explanations = tuple(explained[position] for position in sorted(explained))
    # A row is on a fallback when its reasons did not come from the run's own tier, and a general
    # row always is: nothing about it was measured on the row, whatever `method` ended up naming.
    fallback_rows = sum(
        1
        for explanation in explanations
        if explanation.method is not method or explanation.method is ReasonMethod.GENERAL
    )
    log_stage(_LOGGER, "explain.reasons", rows=len(explanations), seconds=time.perf_counter() - started)
    return RowReasons(explanations=explanations, method=method, fallback_rows=fallback_rows)


def _names(values: pd.DataFrame) -> list[str]:
    """Contribution-frame column names as strings."""
    return [str(name) for name in values.columns]


def _explanations_from(
    found: _Contributions,
    totals: Mapping[str, list[float]],
    scorer: RowScorer,
    *,
    primary_key: str,
    limit: int,
    rank: Mapping[str, int],
) -> tuple[RowExplanation, ...]:
    """One tier's contributions turned into explanations, stamped with the tier that measured them."""
    scores = [float(value) for value in scorer.score(found.rows).tolist()]
    return build_row_explanations(
        found.rows,
        found.values,
        scores,
        features=list(totals),
        primary_key=primary_key,
        limit=limit,
        rank=rank,
        method=found.method,
        totals=totals,
    )


def _tier_calls(
    scorer: RowScorer,
    features: Sequence[str],
    seed: int,
    rank: Mapping[str, int],
    kernel_max_rows: int | None,
    sample: pd.DataFrame,
    *,
    every_row: bool,
) -> tuple[Callable[[pd.DataFrame, bool], _TierOutcome | None], ...]:
    """The three per-row tiers as callables, in the order plan section 6.3 tries them.

    Each takes the rows to explain and whether it is the **first** tier to answer for this run, and
    returns the positions *within the frame it was handed* that it covered - so the loop above can
    tell a row a tier explained from one it never looked at.

    Two things differ on a retry, and both follow from what a retry is (DEC-056):

    * **KernelSHAP does not retry.** Its sampling is a coverage budget for a whole run, not a
      per-row one, and at about a fifth of a second a row it would spend minutes re-measuring rows
      a better tier already measured as zero - very likely to zero again, since the two tiers see
      the same saturated prediction. The permutation tier, which is tens of microseconds a row and
      always works, is the right rescuer.
    * **The permutation tier keeps the whole sample as its reference population.** Its replacement
      value is the column's median or mode, and taken over the pending rows alone that is degenerate
      exactly when it matters most: one pending row would be replaced by its own value, making the
      tier a guaranteed no-op and sending a rescuable row to the general fallback. `reference` is
      therefore always the full sample.
    """

    def whole(
        call: Callable[[pd.DataFrame], _Contributions | None],
    ) -> Callable[[pd.DataFrame, bool], _TierOutcome | None]:
        def run(rows: pd.DataFrame, _first: bool) -> _TierOutcome | None:
            found = call(rows)
            return None if found is None else _TierOutcome(tuple(range(len(rows.index))), found)

        return run

    def kernel(rows: pd.DataFrame, first: bool) -> _TierOutcome | None:
        if not first:
            return None
        positions = _kernel_positions(len(rows.index), kernel_max_rows, seed, every_row=every_row)
        if positions is None:
            return None
        picked = rows if len(positions) == len(rows.index) else rows.iloc[list(positions)]
        found = _kernel_shap(scorer, picked, features, seed)
        return None if found is None else _TierOutcome(positions, found)

    return (
        whole(lambda rows: _tree_shap(scorer, rows, features)),
        kernel,
        whole(lambda rows: _permutation_contributions(scorer, rows, features, rank, reference=sample)),
    )


def _accumulate_strength(strength: dict[str, float], totals: Mapping[str, Sequence[float]]) -> None:
    """Add each feature's mean absolute contribution, so a run can rank features it did measure."""
    for feature, values in totals.items():
        if values:
            strength[feature] = strength.get(feature, 0.0) + sum(abs(value) for value in values) / len(values)


def _general_ranking(
    importance: FeatureImportance | None, strength: Mapping[str, float], features: Sequence[str]
) -> tuple[str, ...]:
    """Features most worth naming in a general reason, best first.

    The global chart is the right answer and the usual one. Failing that, the mean absolute
    contribution over the rows this run *did* measure is a real ranking taken from this model. The
    feature order is the floor: still deterministic, and the reason text says it is general either
    way.
    """
    known = set(features)
    if importance is not None and importance.items:
        ordered = [item.feature for item in importance.items if item.feature in known]
        if ordered:
            return tuple(ordered)
    if strength:
        ranked = sorted(strength, key=lambda name: (-strength[name], name))
        if ranked:
            return tuple(ranked)
    return tuple(features)


def _general_explanations(
    rows: pd.DataFrame,
    scorer: RowScorer,
    ranking: Sequence[str],
    *,
    primary_key: str,
    limit: int,
) -> tuple[RowExplanation, ...]:
    """The last resort: the top general features, carrying each row's own value.

    Nothing here is measured on the row, so nothing here claims to be: the contribution is zero
    because zero is what every tier measured, and the direction is `NONE` because the feature moved
    this row's score neither way.
    """
    chosen = [feature for feature in ranking if feature in rows.columns][:limit]
    scores = [float(value) for value in scorer.score(rows).tolist()]
    keys = (
        [str(value) for value in rows[primary_key].tolist()]
        if primary_key in rows.columns
        else [str(index) for index in rows.index]
    )
    values = {feature: rows[feature].tolist() for feature in chosen}
    return tuple(
        RowExplanation(
            primary_key=keys[position],
            score=round(scores[position], _SCORE_DECIMALS),
            reasons=tuple(_general_reason(feature, values[feature][position]) for feature in chosen),
            method=ReasonMethod.GENERAL,
        )
        for position in range(len(rows.index))
    )


def _general_reason(feature: str, value: object) -> Reason:
    """One general reason: this feature matters across the run, and here is this row's value."""
    text = format_value(value)
    return Reason(
        feature=feature,
        value=text,
        contribution=0.0,
        direction=Direction.NONE,
        text=reason_text(
            feature, text, Direction.NONE, numeric=_is_numeric(value), missing=_is_missing(value)
        ),
    )


def explain_detail(importance: FeatureImportance, reasons: RowReasons | None) -> str:
    """The Running-screen line for the explain stage (design section 6.5)."""
    if reasons is None:
        return f"top {len(importance.items)} features · per-row reasons turned off"
    return (
        f"top {len(importance.items)} features · {reasons.method} reasons for "
        f"{len(reasons.explanations)} rows{fallback_note(reasons)}"
    )


def fallback_note(reasons: RowReasons) -> str:
    """The clause that names the rows the primary tier could not explain, or nothing (DEC-056)."""
    if reasons.fallback_rows == 0:
        return ""
    return f" · {reasons.fallback_rows} on fallback reasons"


def _importance_rank(importance: FeatureImportance | None) -> dict[str, int]:
    """Feature -> its rank on the global chart, for the tie-break and the permutation tier."""
    if importance is None:
        return {}
    return {item.feature: item.rank for item in importance.items}


def _sample_positions(rows: int, limit: int | None, seed: int) -> tuple[int, ...]:
    """At most `limit` row positions, seeded and in ascending order; `None` keeps every one."""
    import numpy as np

    if limit is None or rows <= limit:
        return tuple(range(rows))
    picks = np.sort(np.random.default_rng(seed).choice(rows, size=limit, replace=False))
    return tuple(int(pick) for pick in picks)


def _sample_rows(frame: pd.DataFrame, limit: int | None, seed: int) -> pd.DataFrame:
    """At most `limit` rows, seeded and kept in the frame's own row order; `None` keeps every row."""
    positions = _sample_positions(len(frame), limit, seed)
    return frame if len(positions) == len(frame) else frame.iloc[list(positions)]


def _kernel_positions(
    rows: int, kernel_max_rows: int | None, seed: int, *, every_row: bool
) -> tuple[int, ...] | None:
    """The row positions tier 2 would explain, or `None` when the tier must be skipped altogether.

    KernelSHAP costs about a fifth of a second per row for a twenty-feature model - every row is
    `2048 + 2 * features` model evaluations over the background sample - so a scoring file of a
    hundred thousand rows would take hours. A caller that asked for every row cannot be handed a
    sample instead, because the rows left out would reach `scores.csv` with empty reason cells. The
    tier therefore declines, and the permutation tier explains all of them at tens of microseconds
    a row. A caller who does want KernelSHAP over everything passes `kernel_max_rows=None`.

    A sample here is a **coverage** decision, not a failure: the rows it leaves out are ones the
    caller's own `max_rows` budget already said need no reason, so they are dropped from the run
    rather than retried on the next tier (DEC-056). Only a row this tier explained and could not
    move goes on to the next one.
    """
    if kernel_max_rows is None or rows <= kernel_max_rows:
        return tuple(range(rows))
    if every_row:
        _LOGGER.warning(
            "explain: KernelSHAP is capped at %d rows and all %d rows must be explained; using the "
            "permutation tier, which explains every row, rather than leaving rows without a reason",
            kernel_max_rows,
            rows,
        )
        return None
    return _sample_positions(rows, kernel_max_rows, seed)


def _tree_shap(scorer: RowScorer, rows: pd.DataFrame, features: Sequence[str]) -> _Contributions | None:
    """Tier 1: exact TreeSHAP on the best single tree model, or `None` if anything at all fails.

    `predictor._trainer` is private AutoGluon API, which is why the whole tier is guarded: a version
    that moves it loses the exact explanation and gains a slower one, not a failed run (DEC-069).
    """
    import pandas as pd
    import shap

    try:
        predictor = cast(_Predictor, scorer.predictor)
        model = _best_tree_model(predictor)
        if model is None:
            return None
        prepared = predictor.transform_features(rows[list(features)])
        matrix = model.preprocess(prepared)
        values = _shap_matrix(shap.TreeExplainer(model.model).shap_values(matrix))
        columns = _matrix_columns(matrix, prepared, values.shape[1])
        if columns is None:
            return None
        frame = pd.DataFrame(values, index=rows.index, columns=columns)
    except Exception:
        _LOGGER.warning("explain: TreeSHAP is unavailable, falling back", exc_info=True)
        return None
    return _Contributions(rows=rows, values=frame, method=ReasonMethod.TREE_SHAP)


def _best_tree_model(predictor: _Predictor) -> _TreeModel | None:
    """The best-ranked single tree model, unwrapped from its bagged wrapper when it is one."""
    trainer = getattr(predictor, "_trainer", None)
    if trainer is None:
        return None
    for name in _model_names(predictor):
        if family_for_model_name(name) not in TREE_FAMILIES:
            continue
        candidate = cast(_TreeModel, trainer.load_model(name))
        children: Sequence[str] = getattr(candidate, "models", ()) or ()
        load_child = getattr(candidate, "load_child", None)
        if candidate.model is None and children and load_child is not None:
            candidate = cast(_TreeModel, load_child(children[0]))
        if candidate.model is not None:
            return candidate
    return None


def _model_names(predictor: _Predictor) -> list[str]:
    """Fitted model names, best validation score first when the leaderboard can say so."""
    try:
        board = predictor.leaderboard(display=False)
        if "model" in board.columns:
            return [str(name) for name in board["model"].tolist()]
    except Exception:
        _LOGGER.warning("explain: the leaderboard is unavailable; using the fit order", exc_info=True)
    return [str(name) for name in predictor.model_names()]


def _shap_matrix(values: object) -> FloatArray:
    """One `(n, k)` float matrix, whichever shape this `shap` version returned.

    A list of one array per class means the positive class is index 1; a three-dimensional array
    means the class axis is last. Both spellings are in the wild, so both are normalised here.
    """
    import numpy as np

    picked = values[1] if isinstance(values, list) and len(values) > 1 else values
    picked = picked[0] if isinstance(picked, list) else picked
    array = np.asarray(picked, dtype=np.float64)
    if array.ndim == 3:
        array = array[..., 1] if array.shape[2] > 1 else array[..., 0]
    return np.asarray(array, dtype=np.float64)


def _matrix_columns(matrix: object, prepared: pd.DataFrame, width: int) -> list[str] | None:
    """The column names behind a preprocessed matrix, or `None` when they cannot be known."""
    names = getattr(matrix, "columns", None)
    if names is not None and len(names) == width:
        return [str(name) for name in names]
    if len(prepared.columns) == width:
        return [str(name) for name in prepared.columns]
    return None


def _kernel_shap(
    scorer: RowScorer, rows: pd.DataFrame, features: Sequence[str], seed: int
) -> _Contributions | None:
    """Tier 2: KernelSHAP over a background sample, in the model's own numeric space.

    `transform_features=False` on the prediction call is what lets the explainer work in that space,
    so a categorical column does not break the explainer.
    """
    import numpy as np
    import pandas as pd
    import shap

    try:
        predictor = cast(_Predictor, scorer.predictor)
        prepared = predictor.transform_features(rows[list(features)])
        columns = [str(name) for name in prepared.columns]

        def predict(matrix: FloatArray) -> FloatArray:
            block = pd.DataFrame(matrix, columns=columns)
            raw = predictor.predict_proba(block, as_multiclass=False, transform_features=False)
            return np.asarray(np.asarray(raw), dtype=np.float64)

        background = shap.sample(prepared, min(KERNEL_BACKGROUND_ROWS, len(prepared)), random_state=seed)
        values = _shap_matrix(shap.KernelExplainer(predict, background).shap_values(prepared, silent=True))
        if values.shape[1] != len(columns):
            return None
        frame = pd.DataFrame(values, index=rows.index, columns=columns)
    except Exception:
        _LOGGER.warning("explain: KernelSHAP is unavailable, falling back", exc_info=True)
        return None
    return _Contributions(rows=rows, values=frame, method=ReasonMethod.KERNEL_SHAP)


def _permutation_contributions(
    scorer: RowScorer,
    rows: pd.DataFrame,
    features: Sequence[str],
    rank: Mapping[str, int],
    *,
    reference: pd.DataFrame | None = None,
) -> _Contributions:
    """Tier 3: `base - score(row with the feature replaced)`, one batched prediction per feature.

    Always available: it asks the scorer for scores and for nothing else. The replacement value is
    the column's median (numeric) or first mode (anything else) - an approximation of the training
    statistic the prepare report would carry, logged once here so the number is never mistaken for
    a fitted one.

    `reference` is the frame those medians and modes are taken over, and defaults to `rows` itself.
    A retry passes the whole sample instead (DEC-056): taken over the handful of rows still pending,
    the replacement degenerates - a single pending row would be replaced by its own value, making
    every contribution exactly zero and the tier a guaranteed no-op on the one row that most needed
    rescuing.
    """
    import numpy as np
    import pandas as pd

    source = rows if reference is None else reference
    chosen = _permutation_features(features, rank)
    _LOGGER.warning(
        "explain: falling back to permutation reasons over %d features; the replacement values are "
        "medians and modes of %d reference rows, not fitted training statistics",
        len(chosen),
        len(source.index),
    )
    base = np.asarray(scorer.score(rows).to_numpy(), dtype=np.float64)
    contributions: dict[str, FloatArray] = {}
    for feature in chosen:
        replaced = rows.copy()
        column = replaced[feature]
        # Every row takes the replacement: the `where` mask is false everywhere, which is the one
        # spelling of "overwrite this column with one scalar" that keeps the column's own dtype.
        # The scalar comes from `source`, which is the whole sample on a retry (DEC-056).
        replaced[feature] = column.where(_never(column), other=_replacement(source[feature]))
        moved = np.asarray(scorer.score(replaced).to_numpy(), dtype=np.float64)
        contributions[feature] = np.asarray(base - moved, dtype=np.float64)
    frame = pd.DataFrame(contributions, index=rows.index, columns=list(chosen))
    return _Contributions(rows=rows, values=frame, method=ReasonMethod.PERMUTATION)


def _permutation_features(features: Sequence[str], rank: Mapping[str, int]) -> list[str]:
    """The `K` features to perturb: the most important ones, else the first `K` as given."""
    order = {name: position for position, name in enumerate(features)}
    ordered = sorted(features, key=lambda name: (rank.get(name, _UNRANKED), order[name]))
    return ordered[:PERMUTATION_FEATURES]


def _never(column: pd.Series) -> pd.Series:
    """An all-false mask over `column`'s index: null and not null at once is nothing."""
    return column.isna() & column.notna()


def _replacement(column: pd.Series) -> object:
    """The column's median (numeric) or first mode (anything else); `None` when it is all null."""
    import pandas as pd

    if pd.api.types.is_numeric_dtype(column) and not pd.api.types.is_bool_dtype(column):
        median = column.median()
        return None if pd.isna(median) else median
    modes = column.mode(dropna=True)
    return None if len(modes) == 0 else modes.iloc[0]


# ---------------------------------------------------------------------------
# Contributions -> reasons
# ---------------------------------------------------------------------------
def build_row_explanations(
    rows: pd.DataFrame,
    contributions: pd.DataFrame,
    scores: Sequence[float],
    *,
    features: Sequence[str],
    primary_key: str,
    limit: int,
    rank: Mapping[str, int] | None = None,
    method: ReasonMethod = ReasonMethod.PERMUTATION,
    totals: Mapping[str, list[float]] | None = None,
) -> tuple[RowExplanation, ...]:
    """One `RowExplanation` per row of `rows`, in the frame's own order.

    Contributions of generated columns are summed back onto the column the user uploaded, so a
    reason always names something the reader recognises: AutoGluon's `snapshot_date.year` and
    `snapshot_date.month` become one `snapshot_date` reason.

    `method` stamps every explanation with the tier that measured it, so a caller that retries a
    later tier on the rows the first one left blank can tell them apart afterwards (DEC-056).
    `totals` is that same aggregation when the caller has already computed it; leaving it `None`
    recomputes it here, which is what every caller outside this module does.
    """
    columns = [str(name) for name in contributions.columns]
    if totals is None:
        totals = _aggregate_columns(contributions, _source_map(columns, features))
    names = list(totals)
    values = {name: (rows[name].tolist() if name in rows.columns else [None] * len(rows)) for name in names}
    keys = [str(value) for value in rows[primary_key].tolist()] if primary_key in rows.columns else None
    return tuple(
        RowExplanation(
            primary_key=keys[position] if keys is not None else str(rows.index[position]),
            score=round(float(scores[position]), _SCORE_DECIMALS),
            reasons=reasons_for_row(
                {name: totals[name][position] for name in names},
                {name: values[name][position] for name in names},
                limit=limit,
                rank=rank,
            ),
            method=method,
        )
        for position in range(len(rows))
    )


def _source_map(columns: Sequence[str], features: Sequence[str]) -> dict[str, str]:
    """Transformed column -> the uploaded column it came from, when it came from one."""
    known = set(features)
    mapped: dict[str, str] = {}
    for column in columns:
        name = str(column)
        head = name.split(".", 1)[0]
        mapped[name] = head if name not in known and head in known else name
    return mapped


def _aggregate_columns(contributions: pd.DataFrame, sources: Mapping[str, str]) -> dict[str, list[float]]:
    """Per source feature, the summed contribution of every column generated from it."""
    import pandas as pd

    totals: dict[str, pd.Series] = {}
    for column in contributions.columns:
        feature = sources.get(str(column), str(column))
        numbers = pd.to_numeric(contributions[column], errors="coerce").fillna(0.0)
        totals[feature] = numbers if feature not in totals else totals[feature] + numbers
    return {feature: [float(value) for value in series.tolist()] for feature, series in totals.items()}


def reasons_for_row(
    contributions: Mapping[str, float],
    values: Mapping[str, object],
    *,
    limit: int,
    rank: Mapping[str, int] | None = None,
) -> tuple[Reason, ...]:
    """The strongest `limit` reasons for one row, strongest contribution first.

    A zero contribution is not a reason: the feature did not move the score, so saying it did would
    be an invented explanation. A row whose contributions are all zero therefore gets no reasons
    **here**, and nothing is ever padded to reach `limit`; :func:`reasons_for` is what turns that
    empty tuple into another tier's measurement or a general reason, so no row reaches `scores.csv`
    unexplained (DEC-056). Two equal contributions are ordered by the feature's global-importance
    rank and then alphabetically, so the list a run produces does not depend on dictionary order.
    """
    ranks: Mapping[str, int] = {} if rank is None else rank
    rounded = [
        (feature, round(float(value), _CONTRIBUTION_DECIMALS)) for feature, value in contributions.items()
    ]
    ordered = sorted(
        (pair for pair in rounded if pair[1] != 0.0),
        key=lambda pair: (-abs(pair[1]), ranks.get(pair[0], _UNRANKED), pair[0]),
    )
    return tuple(_reason(feature, values.get(feature), value) for feature, value in ordered[:limit])


def _reason(feature: str, value: object, contribution: float) -> Reason:
    """One reason, with the ready-to-render sentence the Output page prints."""
    direction = Direction.UP if contribution > 0.0 else Direction.DOWN
    missing = _is_missing(value)
    text = format_value(value)
    return Reason(
        feature=feature,
        value=text,
        contribution=contribution,
        direction=direction,
        text=reason_text(feature, text, direction, numeric=_is_numeric(value), missing=missing),
    )


def reason_text(
    feature: str, value: str, direction: Direction, *, numeric: bool, missing: bool = False
) -> str:
    """The sentence a reason renders as (design section 6.3, DEC-070).

    `missing` is the one addition to the design's signature: a categorical level spelled `"missing"`
    is a value, not an absent one, and the two must not render the same way.

    `Direction.NONE` marks a general reason (DEC-056). It carries no arrow, because no direction was
    measured on this row, and ends in :const:`GENERAL_SUFFIX` so a reader can tell the two apart at
    a glance in `scores.csv`.
    """
    general = direction is Direction.NONE
    tail = f" {GENERAL_SUFFIX}" if general else ""
    if missing or not value:
        return f"{feature} {MISSING_VALUE}{tail}"
    if general:
        return f"{feature} = {value}{tail}"
    if numeric:
        arrow = UP_ARROW if direction is Direction.UP else DOWN_ARROW
        return f"{feature} {arrow} ({value})"
    return f"{feature} = {value}"


def format_value(value: object) -> str:
    """A cell as `Reason.value` stores it: `"true"`, `"12"`, `"3.5"`, `"basic"` or `"missing"`."""
    item = _scalar(value)
    if _is_missing(item):
        return MISSING_VALUE
    if isinstance(item, bool):
        return "true" if item else "false"
    number = _number(item)
    if number is None:
        return str(item).strip()
    if isinstance(number, int):
        return str(number)
    trimmed = f"{number:.{_VALUE_DECIMALS}f}".rstrip("0").rstrip(".")
    # A value smaller than the stored precision reads as zero, never as "-0" or as an empty cell.
    return "0" if trimmed in {"", "-", "-0"} else trimmed


def _is_missing(value: object) -> bool:
    """True for `None`, `NaN`, `NaT` and pandas' own null, numpy scalars included.

    The three singletons are compared by identity rather than with `pd.isna`, because a null that
    reaches here is one cell of one row: `pd.isna` would answer for an array just as readily, and
    an array's answer is not a boolean.
    """
    item = _scalar(value)
    if item is None:
        return True
    if isinstance(item, float):
        return math.isnan(item)
    if isinstance(item, bool | int | str):
        return False
    import pandas as pd

    return item is pd.NaT or item is pd.NA


def _is_numeric(value: object) -> bool:
    """Whether a reason's value renders with an arrow rather than with an equals sign."""
    return _number(value) is not None


# ---------------------------------------------------------------------------
# Explanations -> the reason columns the export stage reads
# ---------------------------------------------------------------------------
def reason_column_names(config: UseCaseConfig) -> tuple[str, ...]:
    """`("reason_1", .., "reason_n")`, the slots `engine.contracts.scores_csv_columns` also names."""
    return tuple(f"{REASON_COLUMN_PREFIX}{slot}" for slot in range(1, config.evaluation.reasons_per_row + 1))


def reason_columns(
    explanations: Sequence[RowExplanation],
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
) -> pd.DataFrame:
    """The `reason_1 .. reason_n` columns for `frame`, **joined on the primary key**.

    This is the one place the explain stage and the export stage meet. `engine.stages.export` reads
    a `Reason` (or a mapping that validates as one) or a null per cell and refuses anything else, so
    that is exactly what this builds: whole `Reason` objects, never their text, because
    `ScoringSummary.sample_rows` needs the object and half a reason is worse than none.

    The join is by key and never by position: `frame` is the scored frame, whose rows may have been
    reordered by banding or by anything else between the two stages, while `explanations` are in the
    order the tier produced them. A row with fewer reasons than there are slots gets nulls in the
    rest; a row with more keeps the strongest `n`, which is what the header has room for.

    An empty `explanations` means the stage did not run, and every cell is null - export documents
    that as "no reason is invented". Anything in between is a bug in the producing flow: a scoring
    run that explained a *sample* would put most of `scores.csv` in that state, so it is refused
    here, naming the call that fixes it, rather than exported with empty cells.
    """
    import pandas as pd

    names = reason_column_names(config)
    if primary_key not in frame.columns:
        raise ValueError(
            f"The scored frame carries no {primary_key!r} column, so reasons cannot be joined onto it "
            f"by key."
        )
    by_key: dict[str, tuple[Reason, ...]] = {}
    for explanation in explanations:
        if explanation.primary_key in by_key:
            raise ValueError(
                f"Two explanations carry the primary key {explanation.primary_key!r}; a join by key "
                f"needs one explanation per row."
            )
        by_key[explanation.primary_key] = explanation.reasons
    keys = [str(value) for value in frame[primary_key].tolist()]
    _require_full_coverage(keys, by_key)
    columns = {
        name: pd.Series(
            [_reason_slot(by_key.get(key, ()), slot) for key in keys], index=frame.index, dtype="object"
        )
        for slot, name in enumerate(names)
    }
    return pd.DataFrame(columns, index=frame.index, columns=list(names))


def with_reason_columns(
    explanations: Sequence[RowExplanation],
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: str,
) -> pd.DataFrame:
    """`frame` with the reason columns attached, which is what `engine.stages.export` consumes."""
    columns = reason_columns(explanations, frame, config, primary_key=primary_key)
    merged = frame.copy()
    for name in columns.columns:
        merged[name] = columns[name]
    return merged


def _require_full_coverage(keys: Sequence[str], by_key: Mapping[str, tuple[Reason, ...]]) -> None:
    """Every scored row is explained, or none is; a partly explained frame is a wiring mistake."""
    if not by_key:
        return
    missing = [key for key in keys if key not in by_key]
    if missing:
        raise ValueError(
            f"{len(missing)} of {len(keys)} scored rows have no explanation, the first being "
            f"{missing[0]!r}; every exported row needs a reason (plan section 6.3). Explain the "
            f"scoring frame with max_rows=None rather than the train flow's sample."
        )


def _reason_slot(reasons: tuple[Reason, ...], slot: int) -> Reason | None:
    """The reason for one slot, or `None` when this row had fewer reasons than the header has slots."""
    return reasons[slot] if slot < len(reasons) else None


# ---------------------------------------------------------------------------
# row_explanations.parquet
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def row_explanation_schema() -> pa.Schema:
    """The explicit parquet schema, so the file is self-describing and M4 reads it back exactly."""
    import pyarrow as pa

    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("primary_key", pa.string()),
            ("score", pa.float64()),
            ("method", pa.string()),
            (
                "reasons",
                pa.list_(
                    pa.struct(
                        [
                            ("feature", pa.string()),
                            ("value", pa.string()),
                            ("contribution", pa.float64()),
                            ("direction", pa.string()),
                            ("text", pa.string()),
                        ]
                    )
                ),
            ),
        ]
    )


def write_row_explanations(explanations: Sequence[RowExplanation], *, run_id: str, storage: Storage) -> str:
    """Write `row_explanations.parquet` and return its storage key.

    `Storage.open_write` is atomic, so a crashed run never leaves a half-written file behind.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [explanation.model_dump(mode="json") for explanation in explanations]
    table = pa.Table.from_pylist(rows, schema=row_explanation_schema())
    key = run_key(run_id, ROW_EXPLANATIONS_FILENAME)
    with storage.open_write(key) as handle:
        # `pyarrow.parquet` ships no annotations for these two; there is nothing to fix here.
        pq.write_table(table, handle, compression="snappy")  # type: ignore[no-untyped-call]
    return key


def read_row_explanations(key: str, *, storage: Storage) -> tuple[RowExplanation, ...]:
    """Read `row_explanations.parquet` back, every row validated through its contract."""
    import pyarrow.parquet as pq

    with storage.open_read(key) as handle:
        table = pq.read_table(handle)  # type: ignore[no-untyped-call]
    return tuple(RowExplanation.model_validate(row) for row in table.to_pylist())
