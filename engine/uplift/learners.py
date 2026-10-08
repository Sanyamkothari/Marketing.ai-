"""Uplift meta-learners: S, T and X, over LightGBM or AutoGluon (plan B §3).

An uplift model answers a different question from a propensity model. A propensity model says who
will convert; an uplift model says who converts **because** they were treated, which is the
difference between two outcome probabilities - one in the world where the customer is contacted and
one where they are not. No row ever shows both, so every learner here estimates the two worlds
separately and subtracts:

* **S-learner** - one classifier on the features *plus the treatment flag*; the uplift is the
  prediction with the flag set to 1 minus the prediction with it set to 0. Cheap, but a tree model
  is free to ignore the flag when the effect is small next to the baseline, so the S-learner tends
  to shrink uplift towards zero. It is here because it is the honest baseline.
* **T-learner** - two classifiers, `mu0` on the control rows and `mu1` on the treated rows; the
  uplift is `mu1(x) - mu0(x)`. Each model is fitted on half the data and their errors do not
  cancel, so the difference is noisy where either arm is thin.
* **X-learner** (Künzel et al. 2019) - fits `mu0` and `mu1` as the T-learner does, then *imputes*
  a per-row effect for each arm with the other arm's model (`D1 = y - mu0(x)` on the treated,
  `D0 = mu1(x) - y` on the control), regresses those effects on the features (`tau1`, `tau0`) and
  blends the two regressors as `e * tau0(x) + (1 - e) * tau1(x)`. The weight `e` is the treated
  share, a constant, because assignment is random (the `TREATMENT_NOT_RANDOM` check exists to
  refuse the data where it is not). The imputed effects are learned directly, which is why the
  X-learner is the default: it is the most accurate of the three when one arm is much smaller than
  the other, which is the usual shape of a campaign with a hold-out.

`p_treated` and `p_control` are always `mu1`/`mu0` (the S-learner's two counterfactual predictions),
because the segments need the control probability to tell a *sure thing* from a *lost cause*.

**Explanations are measurement.** :meth:`UpliftModel.contributions` explains the model's own
predicted uplift. For an X-learner on LightGBM the uplift is a fixed linear blend of two tree
ensembles, so exact TreeSHAP of each, blended with the same weights, *is* the exact SHAP of the
uplift: each row's contributions plus the expected value sum to the prediction (to float precision).
The S- and T-learners' uplift is a difference of two *classifier probabilities*, and TreeSHAP on a
LightGBM classifier is additive in log-odds, not in probability; AutoGluon ensembles have no
TreeSHAP at all. For those the uplift is explained through a **surrogate**: a LightGBM regressor
fitted to the model's predicted uplift on the rows being explained, and TreeSHAP of that. The
surrogate's R² against the predicted uplift is recorded on the model as `explanation_fidelity` so
the page can say how faithful the explanation is instead of implying it is exact.

**Persistence.** :func:`save_model` writes one pickle. A LightGBM learner is entirely inside it; an
AutoGluon learner's sub-predictors are directories that AutoGluon manages itself, so they are copied
into the save directory beside the pickle and re-attached by :func:`load_model` - a model directory
is self-contained and can be moved.

`numpy`, `pandas`, `lightgbm` and `autogluon` are imported inside function bodies so that
`import engine` stays fast.
"""

from __future__ import annotations

import math
import pickle
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol, Self

from engine.uplift.config import UpliftBaseModel, UpliftLearner
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    FloatArray = npt.NDArray[np.float64]
    IntArray = npt.NDArray[np.int_]

__all__ = [
    "AUTOGLUON_PRESETS",
    "EXPLANATION_EXACT",
    "EXPLANATION_SURROGATE",
    "MODEL_FILENAME",
    "ArmPrediction",
    "MultiArmUpliftModel",
    "SLearner",
    "TLearner",
    "UpliftModel",
    "UpliftPrediction",
    "XLearner",
    "lightgbm_params",
    "load_model",
    "make_learner",
    "make_multi_arm_learner",
    "save_model",
]

_LOGGER = get_logger(__name__)

MODEL_FILENAME: Final[str] = "uplift_learner.pkl"
"""The pickle :func:`save_model` writes; AutoGluon sub-predictors sit beside it, one directory each."""

AUTOGLUON_PRESETS: Final[str] = "medium_quality"
"""`autogluon_fast`: AutoGluon's fast preset. Every sub-model shares the run's time budget."""

EXPLANATION_EXACT: Final[str] = "tree_shap"
"""`explanation_method` of an X-learner on LightGBM: exact TreeSHAP of the predicted uplift."""

EXPLANATION_SURROGATE: Final[str] = "surrogate_tree_shap"
"""`explanation_method` of every other learner: TreeSHAP of a LightGBM surrogate of the uplift."""

_LABEL: Final[str] = "__uplift_target__"
"""The label column handed to AutoGluon; double underscores keep it clear of any real feature."""

_TREATMENT_FEATURE: Final[str] = "__treatment__"
"""The S-learner's extra feature; renamed on the (unlikely) clash with a real column."""


def lightgbm_params(seed: int) -> dict[str, Any]:
    """The LightGBM settings every sub-model and surrogate uses (plan B §3).

    Deliberately modest: 200 trees at a 0.05 learning rate, at least 50 rows per leaf. Uplift is a
    small difference between two large probabilities, and a model that is allowed to carve out tiny
    leaves fits the noise in each arm, which the subtraction then amplifies. `deterministic` and
    `force_row_wise` make the same data and seed give the same trees whatever the thread count.
    """
    return {
        "n_estimators": 200,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 50,
        "random_state": seed,
        "n_jobs": 2,
        "deterministic": True,
        "force_row_wise": True,
        "verbose": -1,
    }


@dataclass(frozen=True)
class UpliftPrediction:
    """Per-row predictions: both counterfactual outcome probabilities and their difference.

    `uplift` is the learner's own estimate. For the S- and T-learners it equals
    `p_treated - p_control`; for the X-learner it does not have to, because the X-learner estimates
    the effect directly rather than by subtracting `mu1` and `mu0`.
    """

    p_treated: FloatArray
    p_control: FloatArray
    uplift: FloatArray


# ---------------------------------------------------------------------------
# Sub-models: one fitted outcome or effect model, LightGBM or AutoGluon
# ---------------------------------------------------------------------------
class _Estimator(Protocol):
    """A fitted sub-model: a positive-class probability for a classifier, a value for a regressor."""

    def predict(self, frame: pd.DataFrame) -> FloatArray: ...


class _LightGBMEstimator:
    """A fitted `LGBMClassifier` or `LGBMRegressor`; pickles as itself."""

    def __init__(self, model: Any, *, classifier: bool) -> None:
        self.model = model
        self.classifier = classifier

    def predict(self, frame: pd.DataFrame) -> FloatArray:
        import numpy as np

        if self.classifier:
            return np.asarray(self.model.predict_proba(frame), dtype=np.float64)[:, 1]
        return np.asarray(self.model.predict(frame), dtype=np.float64)

    def shap(self, frame: pd.DataFrame) -> tuple[FloatArray, float]:
        """Exact TreeSHAP in the model's output space: `(rows x features, expected value)`.

        Only meaningful for a regressor, whose output space is the prediction itself; a classifier's
        contributions are in log-odds and would not add up to a probability.
        """
        import numpy as np

        raw = np.asarray(self.model.predict(frame, pred_contrib=True), dtype=np.float64)
        expected = float(raw[0, -1]) if len(raw) else 0.0
        return raw[:, :-1], expected


class _AutoGluonEstimator:
    """A `TabularPredictor` living in its own directory, `<root>/<name>`.

    The predictor object is not pickled: AutoGluon keeps its models on disk and reloads them by
    path, so the pickle carries only the directory name and :func:`load_model` points it at the
    directory the model was saved into.
    """

    def __init__(self, name: str, *, classifier: bool, root: Path, predictor: Any) -> None:
        self.name = name
        self.classifier = classifier
        self.root: Path | None = root
        self._predictor: Any = predictor

    def __getstate__(self) -> dict[str, object]:
        return {"name": self.name, "classifier": self.classifier}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.name = str(state["name"])
        self.classifier = bool(state["classifier"])
        self.root = None
        self._predictor = None

    @property
    def path(self) -> Path:
        if self.root is None:
            raise RuntimeError(f"AutoGluon sub-model {self.name!r} is not attached to a model directory.")
        return self.root / self.name

    def attach(self, root: Path) -> None:
        """Point the sub-model at `root/<name>`; the predictor is loaded on first use."""
        self.root = root
        self._predictor = None

    def copy_into(self, directory: Path) -> None:
        """Make `directory/<name>` hold this predictor, so `directory` is self-contained."""
        source = self.path
        target = directory / self.name
        if source.resolve() == target.resolve():
            return
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    def predict(self, frame: pd.DataFrame) -> FloatArray:
        import numpy as np

        if self._predictor is None:
            from autogluon.tabular import TabularPredictor

            self._predictor = TabularPredictor.load(str(self.path), verbosity=0)
        if self.classifier:
            proba = self._predictor.predict_proba(frame, as_multiclass=False)
            return np.asarray(proba, dtype=np.float64)
        return np.asarray(self._predictor.predict(frame), dtype=np.float64)


@dataclass(frozen=True)
class _Fitter:
    """Fits one sub-model with the learner's base model, seed and per-sub-model time budget."""

    base_model: UpliftBaseModel
    seed: int
    time_limit_s: float
    work_dir: Path | None
    prefix: str = ""
    """Plan J M100: put before every AutoGluon sub-model's directory name, so the per-arm learners of one
    model keep their `mu0`, `mu1`, ... apart in one model directory. Empty for a one-treatment model."""

    def fit(self, name: str, frame: pd.DataFrame, target: FloatArray, *, classifier: bool) -> _Estimator:
        if self.base_model is UpliftBaseModel.LIGHTGBM:
            return self._fit_lightgbm(frame, target, classifier=classifier)
        return self._fit_autogluon(f"{self.prefix}{name}", frame, target, classifier=classifier)

    def _fit_lightgbm(self, frame: pd.DataFrame, target: FloatArray, *, classifier: bool) -> _Estimator:
        from lightgbm import LGBMClassifier, LGBMRegressor

        params = lightgbm_params(self.seed)
        model: Any = LGBMClassifier(**params) if classifier else LGBMRegressor(**params)
        model.fit(frame, target.astype(int) if classifier else target)
        return _LightGBMEstimator(model, classifier=classifier)

    def _fit_autogluon(
        self, name: str, frame: pd.DataFrame, target: FloatArray, *, classifier: bool
    ) -> _Estimator:
        from autogluon.tabular import TabularPredictor

        if self.work_dir is None:
            raise RuntimeError("An AutoGluon learner needs a work directory.")
        data = frame.copy()
        data[_LABEL] = target.astype(int) if classifier else target
        predictor = TabularPredictor(
            label=_LABEL,
            problem_type="binary" if classifier else "regression",
            path=str(self.work_dir / name),
            verbosity=0,
            **({"positive_class": 1} if classifier else {}),
        )
        predictor.fit(
            train_data=data,
            presets=AUTOGLUON_PRESETS,
            time_limit=self.time_limit_s,
            raise_on_no_models_fitted=True,
        )
        predictor.save()
        return _AutoGluonEstimator(name, classifier=classifier, root=self.work_dir, predictor=predictor)


# ---------------------------------------------------------------------------
# The learners
# ---------------------------------------------------------------------------
class UpliftModel:
    """The shared life cycle of every meta-learner: validate, fit, predict, explain.

    Subclasses implement `_fit`, `_predict` and `_estimators`; everything a caller touches -
    feature order, the treated share, the explanation and its fidelity - lives here, so the flow can
    treat the three learners alike.
    """

    learner: ClassVar[UpliftLearner]
    _SUBMODELS: ClassVar[int]
    """How many sub-models `fit` trains; the AutoGluon time budget is split evenly across them."""

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        if not time_limit_s > 0.0:
            raise ValueError("time_limit_s must be positive")
        self.base_model = base_model
        self.seed = seed
        self.time_limit_s = time_limit_s
        self.work_dir = work_dir
        self.propensity: float = math.nan
        """Share of training rows that were treated; NaN until `fit`."""
        self.feature_columns: tuple[str, ...] = ()
        self.explanation_fidelity: float | None = None
        """R² of the last explanation's row sums against the predicted uplift; None if undefined."""
        self.explanation_method: str | None = None
        """:data:`EXPLANATION_EXACT` or :data:`EXPLANATION_SURROGATE`, once `contributions` ran."""
        self.fitted = False
        self.submodel_prefix = ""
        """Plan J M100: the AutoGluon directory prefix of this learner's sub-models (one arm of a model of
        several treatments); empty otherwise. Read with a default, so a model pickled before M100 loads."""

    # -- public -------------------------------------------------------------
    def fit(self, X: pd.DataFrame, t: IntArray, y: IntArray) -> Self:  # noqa: N803 - the usual name
        """Fit every sub-model on prepared features `X`, treatment `t` and outcome `y` (both 0/1)."""
        import numpy as np

        started = time.perf_counter()
        treatment, outcome = _validated(X, t, y)
        self.feature_columns = tuple(str(column) for column in X.columns)
        self.propensity = float(treatment.mean())
        fitter = _Fitter(
            base_model=self.base_model,
            seed=self.seed,
            time_limit_s=self.time_limit_s / self._SUBMODELS,
            work_dir=self._autogluon_dir(),
            prefix=getattr(self, "submodel_prefix", ""),
        )
        self._fit(X, treatment, outcome.astype(np.float64), fitter)
        self.fitted = True
        self.explanation_fidelity = None
        self.explanation_method = None
        _LOGGER.info(
            "uplift fit: learner=%s base=%s rows=%d treated=%d features=%d",
            self.learner.value,
            self.base_model.value,
            len(X),
            int(treatment.sum()),
            len(self.feature_columns),
        )
        log_stage(_LOGGER, "uplift_fit", rows=len(X), seconds=time.perf_counter() - started)
        return self

    def predict(self, X: pd.DataFrame) -> UpliftPrediction:  # noqa: N803
        """Both counterfactual probabilities and the uplift for every row of `X`."""
        return self._predict(self._features(X))

    def contributions(self, X: pd.DataFrame) -> tuple[FloatArray, float]:  # noqa: N803
        """SHAP of the predicted uplift: `(rows x features, expected value)`, features in fit order.

        Each row's contributions plus the expected value add up to that row's explained uplift -
        exactly the predicted uplift for an exact explanation, the surrogate's prediction of it
        otherwise. Sets `explanation_method` and `explanation_fidelity` (see the module docstring).
        """
        frame = self._features(X)
        uplift = self._predict(frame).uplift
        return self._surrogate_contributions(frame, uplift)

    # -- for subclasses -----------------------------------------------------
    def _fit(self, X: pd.DataFrame, t: IntArray, y: FloatArray, fitter: _Fitter) -> None:  # noqa: N803
        raise NotImplementedError

    def _predict(self, X: pd.DataFrame) -> UpliftPrediction:  # noqa: N803
        raise NotImplementedError

    def _estimators(self) -> dict[str, _Estimator]:
        raise NotImplementedError

    # -- shared machinery ---------------------------------------------------
    def _autogluon_dir(self) -> Path | None:
        if self.base_model is not UpliftBaseModel.AUTOGLUON_FAST:
            return None
        if self.work_dir is None:
            self.work_dir = Path(tempfile.mkdtemp(prefix="uplift-autogluon-"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir

    def _features(self, X: pd.DataFrame) -> pd.DataFrame:  # noqa: N803
        """`X` restricted to the fit-time features, in fit order; a missing one is a KeyError."""
        if not self.fitted:
            raise RuntimeError("The uplift model has not been fitted.")
        missing = [column for column in self.feature_columns if column not in X.columns]
        if missing:
            raise KeyError(f"Feature columns missing from the frame: {', '.join(missing)}")
        return X[list(self.feature_columns)]

    def _record_fidelity(self, sums: FloatArray, uplift: FloatArray, method: str) -> None:
        self.explanation_method = method
        self.explanation_fidelity = _r_squared(uplift, sums)

    def _surrogate_contributions(self, frame: pd.DataFrame, uplift: FloatArray) -> tuple[FloatArray, float]:
        """TreeSHAP of a LightGBM regressor fitted to the predicted uplift on these rows."""
        from lightgbm import LGBMRegressor

        started = time.perf_counter()
        surrogate = _LightGBMEstimator(LGBMRegressor(**lightgbm_params(self.seed)), classifier=False)
        surrogate.model.fit(frame, uplift)
        matrix, expected = surrogate.shap(frame)
        self._record_fidelity(matrix.sum(axis=1) + expected, uplift, EXPLANATION_SURROGATE)
        log_stage(_LOGGER, "uplift_surrogate_shap", rows=len(frame), seconds=time.perf_counter() - started)
        return matrix, expected


class SLearner(UpliftModel):
    """One classifier with the treatment as a feature; uplift = f(x, 1) - f(x, 0)."""

    learner = UpliftLearner.S_LEARNER
    _SUBMODELS = 1

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__(base_model, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)
        self.treatment_feature = _TREATMENT_FEATURE
        self._model: _Estimator | None = None

    def _fit(self, X: pd.DataFrame, t: IntArray, y: FloatArray, fitter: _Fitter) -> None:  # noqa: N803
        name = _TREATMENT_FEATURE
        while name in self.feature_columns:
            name = f"_{name}_"
        self.treatment_feature = name
        self._model = fitter.fit("s_model", self._with_treatment(X, t), y, classifier=True)

    def _predict(self, X: pd.DataFrame) -> UpliftPrediction:  # noqa: N803
        import numpy as np

        model = _required(self._model)
        p_treated = model.predict(self._with_treatment(X, np.ones(len(X), dtype=int)))
        p_control = model.predict(self._with_treatment(X, np.zeros(len(X), dtype=int)))
        return UpliftPrediction(p_treated=p_treated, p_control=p_control, uplift=p_treated - p_control)

    def _estimators(self) -> dict[str, _Estimator]:
        return {"s_model": _required(self._model)}

    def _with_treatment(self, X: pd.DataFrame, t: IntArray) -> pd.DataFrame:  # noqa: N803
        import numpy as np

        frame = X.copy()
        frame[self.treatment_feature] = np.asarray(t, dtype=np.float64)
        return frame


class TLearner(UpliftModel):
    """Two outcome classifiers, one per arm; uplift = mu1(x) - mu0(x)."""

    learner = UpliftLearner.T_LEARNER
    _SUBMODELS = 2

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__(base_model, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)
        self._mu0: _Estimator | None = None
        self._mu1: _Estimator | None = None

    def _fit(self, X: pd.DataFrame, t: IntArray, y: FloatArray, fitter: _Fitter) -> None:  # noqa: N803
        self._mu0, self._mu1 = _fit_outcome_models(X, t, y, fitter)

    def _predict(self, X: pd.DataFrame) -> UpliftPrediction:  # noqa: N803
        p_treated = _required(self._mu1).predict(X)
        p_control = _required(self._mu0).predict(X)
        return UpliftPrediction(p_treated=p_treated, p_control=p_control, uplift=p_treated - p_control)

    def _estimators(self) -> dict[str, _Estimator]:
        return {"mu0": _required(self._mu0), "mu1": _required(self._mu1)}


class XLearner(UpliftModel):
    """Künzel's X-learner: outcome models per arm, imputed effects, effect regressors, a blend."""

    learner = UpliftLearner.X_LEARNER
    _SUBMODELS = 4

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__(base_model, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)
        self._mu0: _Estimator | None = None
        self._mu1: _Estimator | None = None
        self._tau0: _Estimator | None = None
        self._tau1: _Estimator | None = None

    def _fit(self, X: pd.DataFrame, t: IntArray, y: FloatArray, fitter: _Fitter) -> None:  # noqa: N803
        import numpy as np

        mu0, mu1 = _fit_outcome_models(X, t, y, fitter)
        treated = np.flatnonzero(t == 1)
        control = np.flatnonzero(t == 0)
        x1, x0 = X.iloc[treated], X.iloc[control]
        # Each arm's effect is imputed with the OTHER arm's model, which never saw these rows.
        d1 = y[treated] - mu0.predict(x1)
        d0 = mu1.predict(x0) - y[control]
        self._mu0, self._mu1 = mu0, mu1
        self._tau1 = fitter.fit("tau1", x1, d1, classifier=False)
        self._tau0 = fitter.fit("tau0", x0, d0, classifier=False)

    def _predict(self, X: pd.DataFrame) -> UpliftPrediction:  # noqa: N803
        e = self.propensity
        uplift = e * _required(self._tau0).predict(X) + (1.0 - e) * _required(self._tau1).predict(X)
        return UpliftPrediction(
            p_treated=_required(self._mu1).predict(X),
            p_control=_required(self._mu0).predict(X),
            uplift=uplift,
        )

    def _estimators(self) -> dict[str, _Estimator]:
        return {
            "mu0": _required(self._mu0),
            "mu1": _required(self._mu1),
            "tau0": _required(self._tau0),
            "tau1": _required(self._tau1),
        }

    def contributions(self, X: pd.DataFrame) -> tuple[FloatArray, float]:  # noqa: N803
        """Exact TreeSHAP of the blend on LightGBM; the surrogate on AutoGluon."""
        frame = self._features(X)
        tau0, tau1 = _required(self._tau0), _required(self._tau1)
        if not (isinstance(tau0, _LightGBMEstimator) and isinstance(tau1, _LightGBMEstimator)):
            return super().contributions(frame)
        started = time.perf_counter()
        e = self.propensity
        shap0, base0 = tau0.shap(frame)
        shap1, base1 = tau1.shap(frame)
        # SHAP values are linear in the model: the blend's SHAP is the blend of the SHAPs.
        matrix = e * shap0 + (1.0 - e) * shap1
        expected = e * base0 + (1.0 - e) * base1
        uplift = e * tau0.predict(frame) + (1.0 - e) * tau1.predict(frame)
        self._record_fidelity(matrix.sum(axis=1) + expected, uplift, EXPLANATION_EXACT)
        log_stage(_LOGGER, "uplift_tree_shap", rows=len(frame), seconds=time.perf_counter() - started)
        return matrix, expected


_LEARNERS: Final[dict[UpliftLearner, type[UpliftModel]]] = {
    UpliftLearner.S_LEARNER: SLearner,
    UpliftLearner.T_LEARNER: TLearner,
    UpliftLearner.X_LEARNER: XLearner,
}


# ---------------------------------------------------------------------------
# Plan J M100 (DEC-668, DEC-1310): several treatments against one shared control
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArmPrediction:
    """Per-row predictions of a model of several treatments: one column per treatment, control first out.

    `uplift[:, k-1]` and `p_treated[:, k-1]` are treatment `k`'s (the configured level `k`), each
    against the one `p_control`. :meth:`arm` gives one treatment as an :class:`UpliftPrediction`, so
    `arm(1)` is what a reader of a one-treatment model reads.
    """

    p_control: FloatArray
    p_treated: FloatArray
    uplift: FloatArray

    @property
    def arms(self) -> int:
        return int(self.uplift.shape[1])

    def arm(self, k: int) -> UpliftPrediction:
        """Treatment `k` (1-based) against the control."""
        if not 1 <= k <= self.arms:
            raise ValueError(f"arm must be between 1 and {self.arms}, not {k}.")
        return UpliftPrediction(
            p_treated=self.p_treated[:, k - 1], p_control=self.p_control, uplift=self.uplift[:, k - 1]
        )


def _validated_arms(
    X: pd.DataFrame, arm: IntArray, y: IntArray, arms: int  # noqa: N803 - the usual name
) -> tuple[IntArray, IntArray]:
    """`arm` (codes 0..arms) and `y` checked as :func:`_validated` checks one treatment, for every arm."""
    import numpy as np

    codes = np.asarray(arm)
    outcome = np.asarray(y)
    if codes.ndim != 1 or outcome.ndim != 1 or not len(X) == len(codes) == len(outcome):
        raise ValueError("X, arm and y must have the same number of rows, and arm and y must be 1-D")
    if not np.isin(codes, np.arange(arms + 1)).all():
        raise ValueError(f"arm must contain only the codes 0 (control) to {arms}")
    if not np.isin(outcome, (0, 1)).all():
        raise ValueError("y must contain only 0 and 1")
    codes = codes.astype(int)
    outcome = outcome.astype(int)
    for k in range(arms + 1):
        in_arm = outcome[codes == k]
        label = "control" if k == 0 else f"treatment {k}"
        if len(in_arm) == 0:
            raise ValueError(f"the {label} arm is empty")
        if in_arm.min() == in_arm.max():
            raise ValueError(f"the outcome never varies in the {label} arm")
    return codes, outcome


class MultiArmUpliftModel(UpliftModel):
    """A model of several treatments against one shared control (Plan J M100, DEC-668).

    Fitted with :meth:`fit_arms` on arm codes (0 the control, 1..K the treatments in
    `uplift.treatment_levels` order). :meth:`predict_arms` gives every treatment's uplift; the
    inherited :meth:`predict`, :meth:`contributions`, `propensity` and the explanation fields describe
    the **first** treatment against the control, so every reader of a one-treatment model reads the
    first treatment here exactly as DEC-668 (3) requires.
    """

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        levels: tuple[str, ...],
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        if len(levels) < 3:
            raise ValueError("A model of several treatments needs the control and at least two treatments.")
        super().__init__(base_model, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)
        self.levels = tuple(levels)

    @property
    def arms(self) -> int:
        """K, the number of treatments."""
        return len(self.levels) - 1

    def fit(
        self, X: pd.DataFrame, t: IntArray, y: IntArray  # noqa: N803, ARG002 - the inherited signature
    ) -> Self:
        raise TypeError("A model of several treatments is fitted with fit_arms(X, arm, y).")

    def fit_arms(self, X: pd.DataFrame, arm: IntArray, y: IntArray) -> Self:  # noqa: N803
        """Fit on prepared features, arm codes `0..K` and a 0/1 outcome."""
        started = time.perf_counter()
        codes, outcome = _validated_arms(X, arm, y, self.arms)
        self.feature_columns = tuple(str(column) for column in X.columns)
        first = (codes == 0) | (codes == 1)
        self.propensity = float((codes[first] == 1).mean())
        self._fit_arms(X, codes, outcome)
        self.fitted = True
        self.explanation_fidelity = None
        self.explanation_method = None
        _LOGGER.info(
            "uplift fit: learner=%s base=%s rows=%d arms=%d features=%d",
            self.learner.value,
            self.base_model.value,
            len(X),
            self.arms,
            len(self.feature_columns),
        )
        log_stage(_LOGGER, "uplift_fit_arms", rows=len(X), seconds=time.perf_counter() - started)
        return self

    def predict_arms(self, X: pd.DataFrame) -> ArmPrediction:  # noqa: N803
        """Every treatment's uplift and P(outcome | treated), and the one P(outcome | control)."""
        return self._predict_arms(self._features(X))

    def _predict(self, X: pd.DataFrame) -> UpliftPrediction:  # noqa: N803
        return self._predict_arms(X).arm(1)

    def _fit_arms(self, X: pd.DataFrame, arm: IntArray, y: IntArray) -> None:  # noqa: N803
        raise NotImplementedError

    def _predict_arms(self, X: pd.DataFrame) -> ArmPrediction:  # noqa: N803
        raise NotImplementedError


class _PerArmUplift(MultiArmUpliftModel):
    """T- and X-learners per arm: one one-treatment learner per treatment, fitted on that treatment's
    customers and the shared control's. Each is the module's own :class:`TLearner` or :class:`XLearner`."""

    _ARM_LEARNER: ClassVar[type[UpliftModel]]
    _SUBMODELS = 1

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        levels: tuple[str, ...],
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__(base_model, levels=levels, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)
        self.arm_models: list[UpliftModel] = []

    def _fit_arms(self, X: pd.DataFrame, arm: IntArray, y: IntArray) -> None:  # noqa: N803
        import numpy as np

        work_dir = self._autogluon_dir()
        models: list[UpliftModel] = []
        for k in range(1, self.arms + 1):
            rows = np.flatnonzero((arm == 0) | (arm == k))
            model = self._ARM_LEARNER(
                self.base_model, seed=self.seed, time_limit_s=self.time_limit_s / self.arms, work_dir=work_dir
            )
            model.submodel_prefix = f"arm{k}_"
            model.fit(X.iloc[rows], np.asarray(arm[rows] == k, dtype=np.int_), y[rows])
            models.append(model)
        self.arm_models = models

    def _predict_arms(self, X: pd.DataFrame) -> ArmPrediction:  # noqa: N803
        import numpy as np

        predictions = [model.predict(X) for model in self.arm_models]
        return ArmPrediction(
            p_control=predictions[0].p_control,
            p_treated=np.column_stack([p.p_treated for p in predictions]),
            uplift=np.column_stack([p.uplift for p in predictions]),
        )

    def _estimators(self) -> dict[str, _Estimator]:
        return {
            f"arm{k}_{name}": estimator
            for k, model in enumerate(self.arm_models, start=1)
            for name, estimator in model._estimators().items()
        }

    def contributions(self, X: pd.DataFrame) -> tuple[FloatArray, float]:  # noqa: N803
        """The first treatment's own learner explains its uplift (exact for an X-learner on LightGBM)."""
        if not self.arm_models:
            raise RuntimeError("The uplift model has not been fitted.")
        first = self.arm_models[0]
        result = first.contributions(self._features(X))
        self.explanation_method = first.explanation_method
        self.explanation_fidelity = first.explanation_fidelity
        return result


class MultiArmTLearner(_PerArmUplift):
    """A T-learner per treatment against the shared control."""

    learner = UpliftLearner.T_LEARNER
    _ARM_LEARNER = TLearner


class MultiArmXLearner(_PerArmUplift):
    """An X-learner per treatment against the shared control."""

    learner = UpliftLearner.X_LEARNER
    _ARM_LEARNER = XLearner


class MultiArmSLearner(MultiArmUpliftModel):
    """One classifier with the arm as a feature (one 0/1 column per treatment); treatment `k`'s uplift is
    f(x, arm k) - f(x, control)."""

    learner = UpliftLearner.S_LEARNER
    _SUBMODELS = 1

    def __init__(
        self,
        base_model: UpliftBaseModel,
        *,
        levels: tuple[str, ...],
        seed: int,
        time_limit_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__(base_model, levels=levels, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)
        self.arm_features: tuple[str, ...] = ()
        self._model: _Estimator | None = None

    def _fit_arms(self, X: pd.DataFrame, arm: IntArray, y: IntArray) -> None:  # noqa: N803
        import numpy as np

        names: list[str] = []
        for k in range(1, self.arms + 1):
            name = f"__arm_{k}__"
            while name in self.feature_columns or name in names:
                name = f"_{name}_"
            names.append(name)
        self.arm_features = tuple(names)
        fitter = _Fitter(
            base_model=self.base_model,
            seed=self.seed,
            time_limit_s=self.time_limit_s,
            work_dir=self._autogluon_dir(),
        )
        self._model = fitter.fit("s_model", self._with_arm(X, arm), y.astype(np.float64), classifier=True)

    def _predict_arms(self, X: pd.DataFrame) -> ArmPrediction:  # noqa: N803
        import numpy as np

        model = _required(self._model)
        rows = len(X)
        p_control = model.predict(self._with_arm(X, np.zeros(rows, dtype=np.int_)))
        treated = [
            model.predict(self._with_arm(X, np.full(rows, k, dtype=np.int_))) for k in range(1, self.arms + 1)
        ]
        p_treated = np.column_stack(treated)
        return ArmPrediction(p_control=p_control, p_treated=p_treated, uplift=p_treated - p_control[:, None])

    def _estimators(self) -> dict[str, _Estimator]:
        return {"s_model": _required(self._model)}

    def _with_arm(self, X: pd.DataFrame, arm: IntArray) -> pd.DataFrame:  # noqa: N803
        import numpy as np

        frame = X.copy()
        codes = np.asarray(arm)
        for k, name in enumerate(self.arm_features, start=1):
            frame[name] = np.asarray(codes == k, dtype=np.float64)
        return frame


_MULTI_ARM_LEARNERS: Final[dict[UpliftLearner, type[MultiArmUpliftModel]]] = {
    UpliftLearner.S_LEARNER: MultiArmSLearner,
    UpliftLearner.T_LEARNER: MultiArmTLearner,
    UpliftLearner.X_LEARNER: MultiArmXLearner,
}


def make_multi_arm_learner(
    learner: UpliftLearner,
    base_model: UpliftBaseModel,
    *,
    levels: tuple[str, ...],
    seed: int,
    time_limit_s: float = 600.0,
    work_dir: Path | None = None,
) -> MultiArmUpliftModel:
    """An unfitted model of several treatments (Plan J M100): `levels` is `uplift.treatment_levels`."""
    return _MULTI_ARM_LEARNERS[learner](
        base_model, levels=levels, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir
    )


def make_learner(
    learner: UpliftLearner,
    base_model: UpliftBaseModel,
    *,
    seed: int,
    time_limit_s: float = 600.0,
    work_dir: Path | None = None,
) -> UpliftModel:
    """An unfitted learner. `work_dir` is where AutoGluon sub-predictors go (a temp dir if None).

    `seed` fixes LightGBM's randomness; AutoGluon seeds its own models internally and is not
    bit-reproducible across runs under a wall-clock budget.
    """
    return _LEARNERS[learner](base_model, seed=seed, time_limit_s=time_limit_s, work_dir=work_dir)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_model(model: UpliftModel, directory: Path) -> None:
    """Pickle `model` into `directory`, copying any AutoGluon sub-predictor in beside it."""
    if not model.fitted:
        raise ValueError("Only a fitted uplift model can be saved.")
    directory.mkdir(parents=True, exist_ok=True)
    for estimator in model._estimators().values():
        if isinstance(estimator, _AutoGluonEstimator):
            estimator.copy_into(directory)
    with (directory / MODEL_FILENAME).open("wb") as handle:
        pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)


def load_model(directory: Path) -> UpliftModel:
    """The model :func:`save_model` wrote to `directory`, with its sub-predictors re-attached.

    Unpickling runs code from the file, like every model format the engine loads; the directory is
    the engine's own model store and is trusted exactly as far as that store is.
    """
    with (directory / MODEL_FILENAME).open("rb") as handle:
        model = pickle.load(handle)
    if not isinstance(model, UpliftModel):
        raise TypeError(f"{directory / MODEL_FILENAME} does not hold an uplift model.")
    for estimator in model._estimators().values():
        if isinstance(estimator, _AutoGluonEstimator):
            estimator.attach(directory)
    if model.base_model is UpliftBaseModel.AUTOGLUON_FAST:
        model.work_dir = directory
    return model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _validated(X: pd.DataFrame, t: IntArray, y: IntArray) -> tuple[IntArray, IntArray]:  # noqa: N803
    """`t` and `y` as 0/1 int arrays aligned with `X`; ValueError on anything a learner cannot fit.

    Both arms must be present and each arm's outcome must vary: an arm where nobody (or everybody)
    converted has no outcome model to fit, and inventing one would invent the uplift.
    """
    import numpy as np

    treatment = np.asarray(t)
    outcome = np.asarray(y)
    if treatment.ndim != 1 or outcome.ndim != 1 or not len(X) == len(treatment) == len(outcome):
        raise ValueError("X, t and y must have the same number of rows, and t and y must be 1-D")
    for name, values in (("t", treatment), ("y", outcome)):
        if not np.isin(values, (0, 1)).all():
            raise ValueError(f"{name} must contain only 0 and 1")
    if len({str(column) for column in X.columns}) != X.shape[1]:
        raise ValueError("feature column names must be unique")
    treatment = treatment.astype(int)
    outcome = outcome.astype(int)
    for arm, label in ((1, "treated"), (0, "control")):
        in_arm = outcome[treatment == arm]
        if len(in_arm) == 0:
            raise ValueError(f"the {label} arm is empty")
        if in_arm.min() == in_arm.max():
            raise ValueError(f"the outcome never varies in the {label} arm")
    return treatment, outcome


def _fit_outcome_models(
    X: pd.DataFrame, t: IntArray, y: FloatArray, fitter: _Fitter  # noqa: N803
) -> tuple[_Estimator, _Estimator]:
    """`(mu0, mu1)`: the outcome classifier of the control arm and of the treated arm."""
    import numpy as np

    control = np.flatnonzero(t == 0)
    treated = np.flatnonzero(t == 1)
    mu0 = fitter.fit("mu0", X.iloc[control], y[control], classifier=True)
    mu1 = fitter.fit("mu1", X.iloc[treated], y[treated], classifier=True)
    return mu0, mu1


def _required(estimator: _Estimator | None) -> _Estimator:
    if estimator is None:
        raise RuntimeError("The uplift model has not been fitted.")
    return estimator


def _r_squared(actual: FloatArray, fitted: FloatArray) -> float | None:
    """1 - SS_res / SS_tot, or None when `actual` does not vary (R² is then undefined)."""
    import numpy as np

    if len(actual) < 2:
        return None
    total = float(np.sum((actual - actual.mean()) ** 2))
    if total <= 0.0:
        return None
    residual = float(np.sum((actual - fitted) ** 2))
    return 1.0 - residual / total
