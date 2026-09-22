"""Train stage (M3): the AutoGluon fit, the leaderboard, the best model and the baseline.

`train(recipe, ...)` is the **only** entry point to training. It takes a `Recipe` - never a
`UseCaseConfig` - because a recipe is the complete description of a training attempt, which is what
makes "the same recipe on the same data gives the same model" a statement anyone can check. The
recipe is projected into AutoGluon's vocabulary by two pure functions, `autogluon_predictor_kwargs`
and `autogluon_fit_kwargs`, so what was asked for can be read off without running anything.

> **INVARIANT - TEST IS FINAL-DECISION-ONLY.** `parts["test"]` is never `train_data`, never
> `tuning_data`, never a calibration or threshold input and never a ranking key. It is read at
> exactly one place in this module - the `leaderboard(test_df)` call, which fills the reported
> `score_test` column - and the ranking there comes from `score_val`. `train()` refuses with
> `TEST_SPLIT_LEAKED_INTO_FIT` when the frames headed for `fit()` share an index label with the
> test split, so the invariant is enforced and not merely documented.

**Honest determinism (D18).** Every seed AutoGluon exposes is pinned from the recipe
(`ag_args_fit={"random_seed": recipe.seed}`, and a bagged fold *k* then uses
`recipe.seed + k`), and every other choice - families, presets, bagging, the tuning data, the
imbalance handling, the feature list - is a projection of the recipe. The wall-clock `time_limit` is
not something a seed can control: which candidates finish inside the budget depends on machine load,
core count and disk speed, so bit-for-bit reproducibility is **not** claimed. What is claimed, and
what the artefacts support, is: *the same recipe and the same data, with a budget large enough for
the search to run to completion, give the same model; otherwise the same model up to which
candidates the budget admitted, and the leaderboard says which those were.* The budget is part of
the recipe, so two runs with different budgets are different recipes and hash differently, and
`Leaderboard.models_trained` with each entry's `fit_time_seconds` records what the budget bought.

Where AutoGluon 1.6.3 differs from the plan's assumptions, the installed library wins (plan section
13.8) and the difference is marked `D<n>` at the line that handles it. There is no separate table:
each marker is defined where it appears, in the comment or docstring that carries it.

`autogluon`, `sklearn`, `imblearn`, `pandas` and `numpy` are imported inside function bodies, so
importing the engine stays free of heavy libraries.
"""

from __future__ import annotations

import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from engine.config import Imbalance, ModelFamily, ProblemType, get_catalog
from engine.contracts import BestModel, Leaderboard, LeaderboardEntry
from engine.jobs import JobCancelledError
from engine.stages.scorer import (
    AutoGluonScorer,
    BaselineScorer,
    LabelValue,
    TrainError,
    fit_baseline_scorer,
    fit_scorer,
    load_scorer,
    to_numpy_dtypes,
)
from engine.storage import run_key
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from datetime import datetime

    import pandas as pd
    from autogluon.tabular import TabularPredictor

    from engine.config import EvaluationConfig, Recipe
    from engine.jobs import CancelToken
    from engine.storage import Storage


__all__ = [
    "AG_NAME_TO_FAMILY",
    "AG_PROBLEM_TYPE",
    "MODEL_KEY_NAME",
    "AutoGluonScorer",
    "BaselineScorer",
    "TrainError",
    "TrainResult",
    "autogluon_fit_kwargs",
    "autogluon_predictor_kwargs",
    "class_labels",
    "engine_score",
    "family_for_model_name",
    "hyperparameters_summary",
    "load_scorer",
    "predictor_key_for",
    "resolve_imbalance",
    "train",
    "train_detail",
]

_LOGGER = get_logger(__name__)

MODEL_KEY_NAME: Final[str] = "model"
"""The predictor directory's name inside a run.

`contracts.MODEL_DIRECTORY` is `"model/"`, which is the plan section 7 *checklist* name. It is not a
valid storage key: `storage.validate_key` rejects the empty trailing segment. The key form is
`runs/<run_id>/model`, and that is what `BestModel.predictor_key` carries. (D16.)
"""

AG_PROBLEM_TYPE: Final[dict[ProblemType, str]] = {
    ProblemType.BINARY_CLASSIFICATION: "binary",
    ProblemType.REGRESSION: "regression",
}
"""Engine problem type -> AutoGluon's vocabulary. `binary_classification` is rejected by 1.6.3. (D6.)"""

AG_NAME_TO_FAMILY: Final[dict[str, ModelFamily]] = {
    "XGBoost": ModelFamily.XGBOOST,
    "LightGBM": ModelFamily.LIGHTGBM,
    "RandomForest": ModelFamily.RANDOM_FOREST,
    "LinearModel": ModelFamily.LOGISTIC_REGRESSION,
    "CatBoost": ModelFamily.CATBOOST,
    "NeuralNetTorch": ModelFamily.NEURAL_NET,
}
"""Fitted model name -> engine family. `LR` fits a model AutoGluon calls `LinearModel`. (D4.)"""

_NAME_SUFFIX = re.compile(r"(_BAG|_FULL|_L\d+|_r\d+)+$")
"""Decorations AutoGluon appends to a fitted model's name; bare names appear without bagging. (D5.)"""

_TRIAL_SUFFIX = re.compile(r"/[^/]+$")
r"""What HPO appends to every trial it fits: `LightGBM_BAG_L1/T3` (DEC-073).

It is stripped **before** :data:`_NAME_SUFFIX`, because it sits outside the bagging decorations
rather than inside them. Missing it would be quiet rather than loud: `family_for_model_name` would
return `None` for every model of a tuned run, which costs the leaderboard its family column and
costs the explain stage its TreeSHAP tier, since that tier finds the best *tree* model by family.

It matches any trailing path segment rather than `T\d+` alone, because the trial name is the HPO
backend's to choose: the local scheduler numbers them `T1`, `T2`, while the Ray backend that
`NN_TORCH` asks for names them after its own trial id. A model name has no other use for a slash,
so the wider pattern costs nothing and covers a backend this engine has not met yet.
"""

FAMILIES_WITHOUT_SEARCH_SPACE: Final[frozenset[ModelFamily]] = frozenset({ModelFamily.RANDOM_FOREST})
"""Families the installed AutoGluon has no default search space for, so HPO cannot tune them.

`model_search.tuning_trials` asks for `n` trials per family, and a family whose default search
space is empty has nothing to vary: AutoGluon fits it once, under its bare name and with its
default hyperparameters, and the setting does nothing for it. Measured against AutoGluon 1.6.3,
which fits `RandomForest` - no `/T1` suffix, one model - where the same call gives `LightGBM/T1..Tn`
with different learning rates (DEC-073).

It is a measured constant rather than a runtime probe because reading a model class's default
search space means reaching past the public API, and a wrong answer there would be a silent one.
`tests/unit/test_train.py` checks it against the installed package instead, so this set is a claim
the suite keeps honest rather than a guess frozen into the engine.
"""

_ENSEMBLE_PREFIX: Final[str] = "WeightedEnsemble"
_MAX_ENSEMBLE_LABELS: Final[int] = 3
_SMOTE_MAX_GROWTH: Final[int] = 4
_AUTO_IMBALANCE_RATE: Final[float] = 0.10
_SCORE_DECIMALS: Final[int] = 6
_TIME_DECIMALS: Final[int] = 3


@dataclass(frozen=True)
class TrainResult:
    """What the train stage hands the pipeline. Not an artefact - the artefacts are inside it."""

    leaderboard: Leaderboard
    best: BestModel
    model: AutoGluonScorer
    baseline: BaselineScorer | None
    predictor_key: str
    detail: str


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def predictor_key_for(run_id: str) -> str:
    """The storage key of the predictor directory: `runs/<run_id>/model`, with no trailing slash."""
    return run_key(run_id, MODEL_KEY_NAME)


def engine_score(raw: float, *, greater_is_better: bool) -> float:
    """AutoGluon's score in the catalog's orientation.

    Every 1.6.3 scorer reports `greater_is_better=True` and sign-flips the error metrics, so a
    leaderboard RMSE of 0.5067 reads `-0.506669`. Every score read out of AutoGluon goes through
    here, so no artefact ever carries a negative RMSE. (D8.)
    """
    return float(raw) if greater_is_better else -float(raw)


def family_for_model_name(name: str) -> ModelFamily | None:
    """The engine family of a fitted model, or `None` for the weighted ensemble or an unknown name.

    Bagging and stacking decorate the name (`LightGBM_BAG_L1`, `XGBoost_BAG_L1_FULL`); without
    bagging the names are bare (`LightGBM`, `LinearModel`); HPO adds a trial suffix on top of
    either (`LightGBM_BAG_L1/T3`). Every decoration is stripped before the lookup. (D5, DEC-073.)
    """
    if name.startswith(_ENSEMBLE_PREFIX):
        return None
    return AG_NAME_TO_FAMILY.get(_NAME_SUFFIX.sub("", _TRIAL_SUFFIX.sub("", name)))


def _label_value(value: object) -> LabelValue:
    """A class label as a plain Python scalar, so it survives the `scorer.json` round trip."""
    item = value.item() if hasattr(value, "item") else value
    if isinstance(item, bool | int | float | str):
        return item
    return str(item)


def _label_key(value: object) -> str:
    """One comparable spelling per label, so `1`, `1.0` and `"1"` are the same class.

    The same normalisation `prepare._positive_mask` uses, so the positive class this stage fits
    against is the one `split.json` counted.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip().lower()


def class_labels(labels: pd.Series) -> tuple[LabelValue, LabelValue]:
    """`(negative, positive)` for a classification target, read from the **training** labels only.

    `Recipe` carries no `target.positive_label` - it is not a training choice - so the positive
    class is detected with the convention `prepare` already uses: `true` over `false`, `1` over `0`,
    `yes` over `no`, and failing all three the rarer label, which is the marketing convention for
    the event being predicted. Reading the training split alone keeps the hold-out out of it.
    """
    counts = labels.dropna().map(_label_key).value_counts()
    keys = list(counts.index)
    if len(keys) != 2:
        raise TrainError(
            "TRAIN_TARGET_NOT_BINARY",
            f"The training rows hold {len(keys)} distinct outcome(s); a classification model needs "
            "exactly two.",
        )
    originals: dict[str, LabelValue] = {}
    for value in labels.dropna().tolist():
        originals.setdefault(_label_key(value), _label_value(value))
    positive_key = next(
        (candidate for candidate in ("true", "1", "yes") if candidate in keys),
        min(keys, key=lambda key: (int(counts[key]), key)),
    )
    negative_key = next(key for key in keys if key != positive_key)
    return originals[negative_key], originals[positive_key]


def resolve_imbalance(recipe: Recipe, *, positive_rate: float | None) -> Imbalance:
    """The imbalance mode that actually applies, with `auto` resolved against the train positive rate.

    Plan section 6.3: `auto` means class weights when the positive rate is under 10 %, nothing
    otherwise. The rate is measured on the **training** split. Regression ignores the setting.
    """
    configured = recipe.model_search.imbalance
    if recipe.problem_type is ProblemType.REGRESSION:
        if configured is not Imbalance.NONE:
            _LOGGER.debug("imbalance=%s ignored: the problem type is regression", configured.value)
        return Imbalance.NONE
    if configured is not Imbalance.AUTO:
        return configured
    if positive_rate is None:
        return Imbalance.NONE
    resolved = Imbalance.CLASS_WEIGHTS if positive_rate < _AUTO_IMBALANCE_RATE else Imbalance.NONE
    _LOGGER.info("imbalance=auto resolved to %s (training positive rate %.4f)", resolved.value, positive_rate)
    return resolved


# ---------------------------------------------------------------------------
# Recipe -> AutoGluon, as two pure projections
# ---------------------------------------------------------------------------
def autogluon_predictor_kwargs(
    recipe: Recipe,
    *,
    path: str,
    positive_class: LabelValue | None = None,
    class_weights: bool = False,
) -> dict[str, Any]:
    """`TabularPredictor(**this)`. Pure: the same recipe and path always give the same dict.

    `groups=` is deliberately absent: the engine hands AutoGluon an explicit `tuning_data`, so
    AutoGluon must not build a grouped validation split of its own. `sample_weight` is a
    *constructor* argument naming a weight column, and `"balance_weight"` is the special string that
    asks for balanced class weights - it is not a `fit()` argument, and no column is added to the
    data. (D7.)
    """
    catalog = get_catalog()
    kwargs: dict[str, Any] = {
        "label": recipe.target,
        "problem_type": AG_PROBLEM_TYPE[recipe.problem_type],  # D6
        "eval_metric": catalog.metrics[recipe.model_search.metric].autogluon_name,
        "path": path,
        "verbosity": 1,
        "sample_weight": "balance_weight" if class_weights else None,  # D7
    }
    if recipe.problem_type is not ProblemType.REGRESSION and positive_class is not None:
        kwargs["positive_class"] = positive_class
    return kwargs


def autogluon_fit_kwargs(recipe: Recipe) -> dict[str, Any]:
    """`predictor.fit(train_data=..., tuning_data=..., **this)`. Pure, and complete on purpose.

    Every preset field the engine cares about is passed explicitly, because a preset that quietly
    turns something on would take over a responsibility that lives elsewhere in the engine:

    * `auto_stack` / `dynamic_stacking` - `good_quality` and `best_quality` set both; stacking would
      multiply the fit time and break plan section 10's one-minute budget for no benefit the
      weighted ensemble does not already give. (D1.)
    * `refit_full` / `set_best_to_refit_full` - `good_quality` sets both, which makes `model_best` a
      `*_FULL` model whose `score_val` is `NaN`. `BestModel.validation_score` is a required float,
      so both are off. (D3.)
    * `use_bag_holdout` - with bagging on and an explicit `tuning_data`, `fit()` raises unless this
      is set. It is therefore true exactly when `ensemble` is. (D2.)
    * `calibrate` / `calibrate_decision_threshold` - both default to `'auto'` and would calibrate
      probabilities and move the decision threshold behind the engine's back. The engine owns both,
      at train time on validation (`engine.stages.scorer.fit_scorer`), so both are off. (D9.)
    * `hyperparameters` - only the requested families, and only the ones whose libraries are
      installed. (D10.)
    * `hyperparameter_tune_kwargs` - `model_search.tuning_trials` trials per family, random search
      on the tree families and bayesian optimisation on the neural one, which is what the installed
      AutoGluon calls `searcher: "auto"`. Left out, HPO does not happen at all and the setting would
      be decoration. (DEC-073.)

    The families are passed as `{key: {}}` rather than with search spaces of our own: an empty dict
    means "this family's default search space", and AutoGluon 1.6.3 carries a real one per family -
    a tuned run fits `LightGBM/T1..Tn` with genuinely different `learning_rate`, `num_leaves` and
    `feature_fraction`. Authoring spaces here would freeze one version's idea of a sensible range
    into the engine, and plan section 13.8 says to follow the installed version instead.
    """
    catalog = get_catalog()
    search = recipe.model_search
    families = available_families(recipe)
    bagging = search.ensemble
    return {
        "presets": catalog.strategy_presets[search.strategy],  # D1
        "hyperparameters": {catalog.model_families[family].autogluon_key: {} for family in families},
        "hyperparameter_tune_kwargs": {  # DEC-073
            "num_trials": search.tuning_trials,
            "scheduler": "local",
            "searcher": "auto",
        },
        "time_limit": search.time_limit_minutes * 60,
        "num_bag_folds": search.folds if bagging else 0,
        "num_stack_levels": 0,
        "fit_weighted_ensemble": bagging,
        "use_bag_holdout": bagging,  # D2
        "auto_stack": False,  # D1
        "dynamic_stacking": False,  # D1
        "refit_full": False,  # D3
        "set_best_to_refit_full": False,  # D3
        "calibrate": False,  # D9
        "calibrate_decision_threshold": False,  # D9
        "raise_on_no_models_fitted": True,
        "num_cpus": "auto",
        "ag_args_fit": {"random_seed": recipe.seed},
    }


def available_families(recipe: Recipe) -> tuple[ModelFamily, ...]:
    """The requested families whose libraries are installed, in the order the recipe lists them.

    A family whose requirement is missing is dropped with a WARNING, because AutoGluon would
    otherwise skip it silently among other candidates - and raise an opaque `RuntimeError` when it
    is the only one. An empty result is refused here, in the engine's own words. (D10.)
    """
    catalog = get_catalog()
    missing = catalog.missing_requirements(recipe.model_search.candidates)
    for family, modules in missing.items():
        _LOGGER.warning("model family %s is unavailable: %s not installed", family.value, ", ".join(modules))
    families = tuple(family for family in recipe.model_search.candidates if family not in missing)
    untuned = [family for family in families if family in FAMILIES_WITHOUT_SEARCH_SPACE]
    if untuned:
        # Same reasoning as the missing-library warning above: the run is still valid, but a setting
        # the user moved is doing nothing for part of it, and silence would be the wrong answer.
        _LOGGER.warning(
            "model_search.tuning_trials=%d does not apply to %s: the installed AutoGluon carries no "
            "default search space for it, so it is fitted once with its defaults (DEC-073)",
            recipe.model_search.tuning_trials,
            ", ".join(family.value for family in untuned),
        )
    if not families:
        wanted = ", ".join(family.value for family in recipe.model_search.candidates)
        raise TrainError(
            "TRAIN_NO_MODEL_FAMILY",
            f"None of the selected algorithms ({wanted}) can run on this installation, because the "
            "libraries they need are not installed.",
        )
    return families


# ---------------------------------------------------------------------------
# Cancellation inside the fit
# ---------------------------------------------------------------------------
_CANCEL_TOKENS: Final[dict[str, CancelToken]] = {}
"""Live cancel tokens, by id. The callback carries the id, never the token: see `_cancellation`."""

_CALLBACK_CLASS: type[Any] | None = None
"""The callback class, built on first use, because `autogluon.core` must not be imported early."""


def _cancel_callback_class() -> type[Any]:
    """The `AbstractCallback` subclass that stops the trainer when the run has been cancelled.

    Two things make this more indirect than it looks, and both were found by running it:

    * `autogluon.core.callbacks` cannot be imported at module level (the engine must import without
      a heavy library), so the class is built on first use and then cached.
    * **AutoGluon pickles its trainer mid-fit, and the trainer holds its callbacks.** A class
      defined inside a function is not picklable by name, and a `CancelToken` wraps a
      `threading.Event`, which is not picklable at all. So the callback stores only an id and
      `__reduce__` rebuilds it through a module-level function. A callback that comes back in
      another process finds no token and is simply inert, which is the right answer: a run cannot
      be cancelled from a process that is not running it.
    """
    global _CALLBACK_CLASS
    if _CALLBACK_CLASS is not None:
        return _CALLBACK_CLASS
    from autogluon.core.callbacks import AbstractCallback

    class CancelCallback(AbstractCallback):  # type: ignore[misc]
        """Stops the trainer before its next model once the run has been cancelled."""

        def __init__(self, token_id: str) -> None:
            super().__init__()
            self.token_id = token_id

        def _before_model_fit(
            self,
            trainer: object,  # noqa: ARG002 - the hook's signature, fixed by AutoGluon
            model: object,  # noqa: ARG002
            time_limit: float | None = None,  # noqa: ARG002
            stack_name: str = "core",  # noqa: ARG002
            level: int = 1,  # noqa: ARG002
        ) -> tuple[bool, bool]:
            token = _CANCEL_TOKENS.get(self.token_id)
            # (early_stop, skip_model): the longest uninterruptible window is one model fit.
            return (token is not None and token.cancelled), False

        def __reduce__(self) -> tuple[Any, ...]:
            return rebuild_cancel_callback, (self.token_id,)

    _CALLBACK_CLASS = CancelCallback
    return CancelCallback


def rebuild_cancel_callback(token_id: str) -> object:
    """Unpickle a cancel callback. Public because a pickle names it; never called by hand."""
    return _cancel_callback_class()(token_id)


@contextmanager
def _cancellation(cancel: CancelToken) -> Iterator[object]:
    """Register `cancel` for the duration of one fit and yield the callback that watches it."""
    token_id = uuid4().hex
    _CANCEL_TOKENS[token_id] = cancel
    try:
        yield _cancel_callback_class()(token_id)
    finally:
        _CANCEL_TOKENS.pop(token_id, None)


# ---------------------------------------------------------------------------
# Leaderboard and best model
# ---------------------------------------------------------------------------
def _seconds(value: Any) -> float:
    """A number AutoGluon reported, as a plain float; an empty cell reads as zero."""
    number = _finite(value)
    return 0.0 if number is None else number


def _finite(value: Any) -> float | None:
    """A float, or `None` when AutoGluon left the cell empty or filled it with `NaN`."""
    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else number


def build_leaderboard(raw: pd.DataFrame, recipe: Recipe, *, run_id: str, best_model_name: str) -> Leaderboard:
    """`leaderboard.json` from `predictor.leaderboard(test_df)`.

    THE TEST SPLIT IS FINAL-DECISION-ONLY: `score_test` is carried into the table because the Model
    page shows it, and it is **reported, never ranked on**. The sort key is the validation score,
    then fit time, then the model name - a total order that cannot depend on the hold-out. (D12.)

    Rows whose `score_val` is `NaN` are dropped: a model with no validation score has not been
    measured on anything this stage is allowed to rank by. (D3's safety net.)
    """
    catalog = get_catalog()
    greater_is_better = catalog.metrics[recipe.model_search.metric].greater_is_better
    entries: list[LeaderboardEntry] = []
    for row in raw.itertuples():
        validation_score = _finite(getattr(row, "score_val", None))
        if validation_score is None:
            continue
        name = str(row.model)
        family = family_for_model_name(name)
        test_score = _finite(getattr(row, "score_test", None))
        entries.append(
            LeaderboardEntry(
                rank=0,
                model_name=name,
                family=family,
                family_label=catalog.family_label(family) if family is not None else None,
                validation_score=round(
                    engine_score(validation_score, greater_is_better=greater_is_better), _SCORE_DECIMALS
                ),
                test_score=(
                    None
                    if test_score is None
                    else round(engine_score(test_score, greater_is_better=greater_is_better), _SCORE_DECIMALS)
                ),
                fit_time_seconds=round(_seconds(getattr(row, "fit_time", None)), _TIME_DECIMALS),
                predict_time_seconds=round(_seconds(getattr(row, "pred_time_val", None)), _TIME_DECIMALS),
                is_ensemble=name.startswith(_ENSEMBLE_PREFIX),
                stack_level=int(_seconds(getattr(row, "stack_level", None))),
            )
        )
    entries.sort(
        key=lambda entry: (
            -entry.validation_score if greater_is_better else entry.validation_score,
            entry.fit_time_seconds,
            entry.model_name,
        )
    )
    ranked = tuple(entry.model_copy(update={"rank": rank}) for rank, entry in enumerate(entries, start=1))
    search = recipe.model_search
    return Leaderboard(
        run_id=run_id,
        metric=search.metric,
        metric_label=catalog.metric_label(search.metric),
        greater_is_better=greater_is_better,
        entries=ranked,
        best_model_name=best_model_name,
        models_trained=len(ranked),
        time_limit_seconds=search.time_limit_minutes * 60,
        presets=catalog.strategy_presets[search.strategy],
    )


def _json_safe(value: object) -> object:
    """A hyperparameter value `json.dumps` accepts; anything else is rendered as its text."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


_SUMMARY_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("depth", ("max_depth", "depth", "num_leaves")),
    ("lr", ("learning_rate", "eta", "lr")),
    ("trees", ("n_estimators", "num_boost_round", "iterations")),
    ("C", ("C",)),
)


def _number(value: object) -> str:
    """A hyperparameter number as the Model page prints it: `6`, `0.05`, `400`."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if float(value).is_integer() else f"{value:g}"
    return str(value)


def hyperparameters_summary(hyperparameters: Mapping[str, object], *, ensemble_members: int = 0) -> str:
    """The Model page's one-line summary, for example `"depth 6 · lr 0.05 · 400 trees"`.

    A fixed, config-free renderer over the keys the tree and linear families actually use. An absent
    key produces no part, so no number is ever invented; nothing recognised reads `"default
    hyperparameters"`, which is true rather than empty.
    """
    if ensemble_members:
        return f"weighted ensemble of {ensemble_members} models"
    parts: list[str] = []
    for concept, keys in _SUMMARY_KEYS:
        for key in keys:
            if key in hyperparameters:
                value = _number(hyperparameters[key])
                if concept == "trees":
                    parts.append(f"{value} trees")
                elif key == "num_leaves":
                    parts.append(f"{value} leaves")
                else:
                    parts.append(f"{concept} {value}")
                break
    return " · ".join(parts) if parts else "default hyperparameters"


def _display_name(
    name: str, family: ModelFamily | None, members: Sequence[str], entries: Sequence[LeaderboardEntry]
) -> str:
    """`"XGBoost"`, or `"Ensemble (XGBoost + LightGBM)"` over the members' distinct family labels."""
    catalog = get_catalog()
    if family is not None:
        return catalog.family_label(family)
    if not name.startswith(_ENSEMBLE_PREFIX):
        return name
    order = {entry.model_name: entry.rank for entry in entries}
    labels: list[str] = []
    for member in sorted(members, key=lambda member: order.get(member, len(order) + 1)):
        member_family = family_for_model_name(member)
        if member_family is None:
            continue
        label = catalog.family_label(member_family)
        if label not in labels:
            labels.append(label)
    if not labels:
        return "Ensemble"
    shown = labels[:_MAX_ENSEMBLE_LABELS]
    extra = len(labels) - len(shown)
    joined = " + ".join(shown) + (f" + {extra} more" if extra else "")
    return f"Ensemble ({joined})"


def train_detail(leaderboard: Leaderboard, best: BestModel, recipe: Recipe) -> str:
    """Running-screen row 3. Every number in it was measured, none is a plan of what would happen."""
    folds = f"{recipe.model_search.folds}-fold CV" if recipe.model_search.ensemble else "held-out validation"
    suffix = " (ensembled)" if best.is_ensemble else ""
    return f"{leaderboard.models_trained} models trained · {folds} · best: {best.display_name}{suffix}"


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------
def _guard_test_split(
    train_frame: pd.DataFrame, tuning_frame: pd.DataFrame, test_frame: pd.DataFrame
) -> None:
    """Refuse to fit when a test row would reach `fit()`.

    THE TEST SPLIT IS FINAL-DECISION-ONLY. This is the enforcement, not the documentation: the two
    frames AutoGluon is about to see must share no index label with the hold-out.
    """
    leaked = len(train_frame.index.intersection(test_frame.index)) + len(
        tuning_frame.index.intersection(test_frame.index)
    )
    if leaked:
        raise TrainError(
            "TEST_SPLIT_LEAKED_INTO_FIT",
            f"{leaked} row(s) of the hold-out set were about to be used for training, which would "
            "make every reported score a self-assessment.",
        )


def _oversample(recipe: Recipe, train_frame: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """SMOTE on the **training** split only; `(frame, applied)`.

    Validation and test are untouched, so every reported score is measured on the real distribution.
    Each guard falls back to class weights with one WARNING rather than failing a run: too few
    minority rows to interpolate between, a null SMOTE cannot interpolate over, or a resampled frame
    so much larger that it would blow the time budget. Synthetic rows carry no primary key and never
    leave this function's caller: they are not scored, not explained and not written anywhere.
    """
    import pandas as pd
    from imblearn.over_sampling import SMOTE, SMOTENC
    from pandas.api.types import is_numeric_dtype

    features = list(recipe.feature_columns)
    labels = train_frame[recipe.target]
    minority = int(min((labels == value).sum() for value in labels.unique()))
    if minority < 2:
        _LOGGER.warning("oversampling skipped: the minority class has %d row(s)", minority)
        return train_frame, False
    if bool(train_frame[features].isna().to_numpy().any()):
        _LOGGER.warning("oversampling skipped: the training features still hold missing values")
        return train_frame, False
    categorical = [
        index for index, column in enumerate(features) if not is_numeric_dtype(train_frame[column])
    ]
    neighbours = min(5, minority - 1)
    sampler = (
        SMOTENC(categorical_features=categorical, random_state=recipe.seed, k_neighbors=neighbours)
        if categorical
        else SMOTE(random_state=recipe.seed, k_neighbors=neighbours)
    )
    resampled_features, resampled_labels = sampler.fit_resample(train_frame[features], labels)
    resampled = pd.concat([resampled_features, resampled_labels.rename(recipe.target)], axis=1)
    if len(resampled) > _SMOTE_MAX_GROWTH * len(train_frame):
        _LOGGER.warning(
            "oversampling skipped: it would grow the training split from %d to %d rows",
            len(train_frame),
            len(resampled),
        )
        return train_frame, False
    _LOGGER.info("oversampling applied: %d training rows became %d", len(train_frame), len(resampled))
    return resampled, True


def train(
    recipe: Recipe,
    parts: Mapping[str, pd.DataFrame],
    evaluation: EvaluationConfig,
    *,
    run_id: str,
    storage: Storage,
    cancel: CancelToken,
    now: datetime | None = None,
) -> TrainResult:
    """Fit the candidates, save the predictor and return the leaderboard, the winner and the scorer.

    THE TEST SPLIT IS FINAL-DECISION-ONLY (DEC-045). `parts["train"]` becomes `train_data` and
    `parts["validation"]` becomes `tuning_data`, so AutoGluon never re-splits and never sees
    `parts["test"]`. The hold-out is read once, below, to fill the leaderboard's reported
    `score_test` column; the ranking and `predictor.model_best` both come from the validation score.

    The threshold and the calibrator are fitted afterwards by `scorer.fit_scorer`, on validation, and
    written to `model/scorer.json` beside the predictor so the model can be reconstructed and
    re-scored later - which is exactly what the champion rule needs.
    """
    import autogluon.tabular
    from autogluon.tabular import TabularPredictor

    cancel.raise_if_cancelled()
    trained_at = utc_now() if now is None else now
    features = list(recipe.feature_columns)
    columns = [*features, recipe.target]
    # D10: an unavailable family is refused here, before a predictor exists and before any fit.
    fit_kwargs = autogluon_fit_kwargs(recipe)

    # AutoGluon 1.6.3 silently drops a pandas nullable column, so every frame it sees is converted
    # to numpy dtypes first; see `scorer.to_numpy_dtypes` for the measurement behind this.
    train_frame = to_numpy_dtypes(parts["train"][columns], features)
    tuning_frame = to_numpy_dtypes(parts["validation"][columns], features)
    test_frame = to_numpy_dtypes(parts["test"][columns], features)
    # TEST IS FINAL-DECISION-ONLY: test_frame exists for the leaderboard's reported score_test and
    # for nothing else. It is never train_data, never tuning_data and never a ranking key.
    _guard_test_split(train_frame, tuning_frame, test_frame)

    classes: tuple[LabelValue, LabelValue] | None = None
    positive_rate: float | None = None
    if recipe.problem_type is not ProblemType.REGRESSION:
        classes = class_labels(train_frame[recipe.target])
        positive_rate = float((train_frame[recipe.target].map(_label_key) == _label_key(classes[1])).mean())
    imbalance = resolve_imbalance(recipe, positive_rate=positive_rate)
    class_weights = imbalance is Imbalance.CLASS_WEIGHTS
    if imbalance is Imbalance.OVERSAMPLING and classes is not None:
        train_frame, applied = _oversample(recipe, train_frame)
        class_weights = not applied

    predictor_key = predictor_key_for(run_id)
    predictor_path = str(storage.local_path(predictor_key))
    predictor = TabularPredictor(
        **autogluon_predictor_kwargs(
            recipe,
            path=predictor_path,
            positive_class=None if classes is None else classes[1],
            class_weights=class_weights,
        )
    )
    _LOGGER.info(
        "train: recipe=%s families=%d budget=%ds presets=%s",
        recipe.recipe_hash[:12],
        len(fit_kwargs["hyperparameters"]),
        fit_kwargs["time_limit"],
        fit_kwargs["presets"],
    )
    try:
        with _cancellation(cancel) as callback:
            predictor.fit(
                train_data=train_frame,
                tuning_data=tuning_frame,
                callbacks=[callback],
                **fit_kwargs,
            )
    except RuntimeError as error:
        if cancel.cancelled:
            raise JobCancelledError("The run was cancelled.") from error
        raise TrainError(
            "TRAIN_NO_MODEL_FITTED",
            "No model could be trained on this data within the time limit. Try a longer time limit "
            "or a different set of algorithms.",
        ) from error
    cancel.raise_if_cancelled()
    predictor.save()

    best_model_name = str(predictor.model_best)
    # TEST IS FINAL-DECISION-ONLY: this is the one place the hold-out is read in this stage, and it
    # fills a reported column. `best_model_name` above is AutoGluon's validation-chosen winner.
    raw_leaderboard = predictor.leaderboard(test_frame, display=False)
    leaderboard = build_leaderboard(raw_leaderboard, recipe, run_id=run_id, best_model_name=best_model_name)
    best = build_best_model(
        predictor,
        leaderboard,
        recipe,
        run_id=run_id,
        training_rows=len(train_frame),
        predictor_key=predictor_key,
        trained_at=trained_at,
    )

    model = fit_scorer(
        predictor,
        recipe,
        evaluation,
        validation=parts["validation"],
        display_name=best.display_name,
        model_name=best.model_name,
        family=best.family,
        classes=classes,
        autogluon_version=str(autogluon.tabular.__version__),
        trained_at=trained_at,
    )
    model.save(storage, predictor_key)
    baseline = fit_baseline_scorer(
        recipe,
        evaluation,
        train=parts["train"],
        validation=parts["validation"],
        classes=classes,
        class_weights=class_weights,
        trained_at=trained_at,
    )
    return TrainResult(
        leaderboard=leaderboard,
        best=best,
        model=model,
        baseline=baseline,
        predictor_key=predictor_key,
        detail=train_detail(leaderboard, best, recipe),
    )


def build_best_model(
    predictor: TabularPredictor,
    leaderboard: Leaderboard,
    recipe: Recipe,
    *,
    run_id: str,
    training_rows: int,
    predictor_key: str,
    trained_at: datetime,
) -> BestModel:
    """`best_model.json` for AutoGluon's validation-chosen winner.

    `model_hyperparameters(name)` returns only the non-defaults, so the full dict comes from
    `model_info(name)["hyperparameters"]`; the ensemble's members come from
    `model_info(name)["stacker_info"]["base_model_names"]`. (D14, D15.)
    """
    name = leaderboard.best_model_name
    entry = next((row for row in leaderboard.entries if row.model_name == name), None)
    if entry is None:
        raise TrainError(
            "TRAIN_NO_VALIDATION_SCORE",
            f"The winning model {name} has no validation score, so it cannot be reported honestly.",
        )
    if entry.test_score is None:
        raise TrainError(
            "TRAIN_NO_TEST_SCORE",
            "The winning model could not be scored on the hold-out set, so the run has no headline "
            "number to report.",
        )
    info: Mapping[str, Any] = predictor.model_info(name)
    stacker = info.get("stacker_info") or {}
    members = tuple(str(member) for member in stacker.get("base_model_names", ()))
    hyperparameters = {
        str(key): _json_safe(value) for key, value in (info.get("hyperparameters") or {}).items()
    }
    catalog = get_catalog()
    return BestModel(
        run_id=run_id,
        model_name=name,
        family=entry.family,
        is_ensemble=entry.is_ensemble,
        ensemble_members=members,
        display_name=_display_name(name, entry.family, members, leaderboard.entries),
        metric=recipe.model_search.metric,
        metric_label=catalog.metric_label(recipe.model_search.metric),
        validation_score=entry.validation_score,
        test_score=entry.test_score,
        hyperparameters=hyperparameters,
        hyperparameters_summary=hyperparameters_summary(
            hyperparameters, ensemble_members=len(members) if entry.is_ensemble else 0
        ),
        training_rows=training_rows,
        feature_count=len(recipe.feature_columns),
        fit_time_seconds=entry.fit_time_seconds,
        predictor_key=predictor_key,
        trained_at=trained_at,
    )
