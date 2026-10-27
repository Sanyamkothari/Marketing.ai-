"""The uplift-threshold decision policy: a cross-fitted uplift model, a cost threshold, and a gate.

    policy = fit_uplift_threshold(train, segment_candidates=...)      # on a randomised campaign
    pi = policy.pi(attributes, features, conversion_value)            # one row per customer, one-hot

**What it decides.** For one customer and each message arm k of the campaign, the expected extra profit is

    net_k = uplift_k(x) x value - cost_k

where `uplift_k(x)` is the model's rise in the chance of buying, `value` the margin a purchase by this
customer makes (the per-customer value when it is known before the campaign, as on X5; else the mean margin
of a buyer of arm k), and `cost_k` the arm's channel and offer cost. The customer gets the arm with the
largest `net_k` if that is above 0, else nothing. Deterministic: no exploration, no draw, no multiplier, no
hold. Ties go to the lowest arm, and arm 0 (no message) wins a tie with a net of exactly 0.

**The model** (reused from `autonomy.uplift`, not rewritten): the one-hot customer features of
`FeatureEncoder` and the shared-baseline logistic learner `SharedBaselineLogit`, fitted with K-fold
cross-fitting. Customers are put into K folds by a hash of (seed, id); fold f's model is fitted on the
other K-1 folds and predicts fold f only, so a customer's out-of-fold score never comes from a model
that saw them. Each arm is then calibrated on those out-of-fold scores by `autonomy.uplift.model.calibrate_arm`,
the same function `fit_uplift` calls: the doubly robust pseudo-outcome gives the arm's average effect, and the
slope of the pseudo-outcome on the centred score (shrunk by its own noise) says how far the differences
between customers are real. The score is `ate + slope x (centred, winsorised raw uplift)`, centred on the
mean of the K-model average, which is the scale that is scored. Where the slope is 0 every customer gets
the arm's average effect, and the policy is the best blanket arm.

A customer outside the fitting data (the gate part, the test half) is scored by the average of the K fold
models.

**The gate** (the Scientist's rule, docs/v3/ROOT_CAUSE.md). A model is only worth deploying where it beats
what needs no model. The train data is split by a hash in two: 70% (the fit part) and 30% (the gate part).
Everything the model policy is judged with comes from the fit part: the model, and the value of a buyer.
The comparison rule is chosen on the fit part too, the way the bench's `choose_simple` does it: each
blanket policy (send to none, send to all of an arm) by its doubly robust value on the whole fit part, and
the segment rule's columns by its value on a 30% validation slice of the fit part after fitting on the
rest. On the gate part, which nothing above saw, the model policy and that one rule are compared row by
row: the doubly robust terms of the bench's estimator (`autonomy.proof.replay`, reward model
`per_action_mean_model`) of the model minus those of the rule, with a 95% interval. The model is deployed
only if the LOWER end of that paired difference is above 0. Otherwise the policy is the rule (a segment
rule is refitted on all of train). `UpliftThresholdPolicy.gate` says which branch it took and why; the
model refitted on all of train is kept either way (`UpliftThresholdPolicy.ungated`), so the method itself
can be scored next to the gated output.

Standard library, numpy, pandas and the repo's own packages only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.proof import ope
from autonomy.proof.replay import LoggedBandit, SegmentPolicy, fit_segment_policy, per_action_mean_model
from autonomy.uplift.features import BEHAVIOUR, FeatureEncoder
from autonomy.uplift.learner import ArmModel, SharedBaselineLogit, UpliftLearner
from autonomy.uplift.model import ArmFit, calibrate_arm

__all__ = [
    "CrossFitUplift",
    "GateResult",
    "UpliftData",
    "UpliftThresholdPolicy",
    "customer_value",
    "decide_arms",
    "dr_terms",
    "expected_net",
    "fit_uplift_threshold",
    "fold_of_ids",
    "one_hot",
    "parse_gate_line",
    "value_per_conversion",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

FOLDS = 5
"""Cross-fitting folds."""
GATE_SHARE = 0.3
"""Share of train held out for the gate."""
VALIDATION_SHARE = 0.3
"""Share of the fit part the segment rule's columns are chosen on (as the bench's `choose_simple`)."""
MIN_ROWS = 400
"""Fewest customers a model is fitted on (below this the policy is the best simple rule)."""
MIN_ARM_ROWS = 40
"""Fewest customers an arm (and the control) needs in the part a model is fitted on."""
FOLD_SALT = "autonomy-uplift-threshold-v1:fold"
GATE_SALT = "autonomy-uplift-threshold-v1:gate"
VALIDATION_SALT = "autonomy-uplift-threshold-v1:validation"
UNGATED = "uplift model (ungated)"
"""The bench's row name for the model policy whatever the gate said."""


# --- the data ------------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class UpliftData:
    """A randomised campaign, one row per customer. Arm 0 is no message, arm k >= 1 the k-th message."""

    customer_ids: npt.NDArray[Any]
    arm: IntArray
    propensity: FloatArray
    """The design probability of the arm each customer got."""
    converted: FloatArray
    """1.0 if the customer bought in the campaign window, else 0.0."""
    margin: FloatArray
    """The margin the customer's purchase made (0 for a non-buyer), before any contact cost."""
    arm_cost: FloatArray
    """Channel plus offer cost of each arm, arm 0 first (0 for no message)."""
    arm_names: tuple[str, ...]
    """One name per arm, arm 0 first."""
    features: pd.DataFrame
    """Pre-campaign customer columns (what a segment rule reads)."""
    attributes: tuple[Mapping[str, str], ...]
    """The same customers as the text attributes `FeatureEncoder` reads (`CustomerState.preferences`)."""
    conversion_value: FloatArray | None = None
    """The margin a purchase by each customer would make, when it is known before the campaign (X5: the
    margin share of the customer's mean receipt); None: each arm's mean margin of a buyer is used."""

    def __post_init__(self) -> None:
        n = len(self.arm)
        for name in ("customer_ids", "propensity", "converted", "margin", "features", "attributes"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} must have one entry per customer ({n})")
        if self.conversion_value is not None and len(self.conversion_value) != n:
            raise ValueError(f"conversion_value must have one entry per customer ({n})")
        if len(self.arm_cost) != len(self.arm_names):
            raise ValueError("arm_cost and arm_names need one entry per arm, arm 0 included")
        if n and (self.arm.min() < 0 or self.arm.max() >= len(self.arm_names)):
            raise ValueError("arm must index arm_names")

    @property
    def n(self) -> int:
        return len(self.arm)

    @property
    def n_arms(self) -> int:
        """Message arms, not counting arm 0."""
        return len(self.arm_names) - 1

    def profit(self) -> FloatArray:
        out: FloatArray = self.margin - self.arm_cost[self.arm]
        return out

    def take(self, rows: IntArray) -> UpliftData:
        return UpliftData(
            customer_ids=self.customer_ids[rows],
            arm=self.arm[rows],
            propensity=self.propensity[rows],
            converted=self.converted[rows],
            margin=self.margin[rows],
            arm_cost=self.arm_cost,
            arm_names=self.arm_names,
            features=self.features.iloc[rows].reset_index(drop=True),
            attributes=tuple(self.attributes[int(i)] for i in rows),
            conversion_value=None if self.conversion_value is None else self.conversion_value[rows],
        )

    def logged(self, source: str = "uplift_threshold") -> LoggedBandit:
        """The campaign as the off-policy harness reads it, with profit as the reward."""
        return LoggedBandit(
            source=source,
            contexts=self.features.reset_index(drop=True),
            actions=self.arm.astype(np.int64),
            propensity=self.propensity.astype(np.float64),
            reward=self.profit().astype(np.float64),
            action_names=self.arm_names,
            reward_name="profit",
        )


def _unit_hash(ids: Sequence[Any] | npt.NDArray[Any], salt: str) -> FloatArray:
    """A number in [0, 1) per id, from BLAKE2b of `salt:id`: the same on every machine."""
    out = np.empty(len(ids), dtype=np.float64)
    prefix = f"{salt}:".encode()
    for i, cid in enumerate(ids):
        digest = hashlib.blake2b(prefix + str(cid).encode(), digest_size=8).digest()
        out[i] = int.from_bytes(digest, "big") / 2.0**64
    return out


def _split(ids: Sequence[Any] | npt.NDArray[Any], salt: str, share: float) -> tuple[IntArray, IntArray]:
    """(positions with a hash at or above `share`, positions below it)."""
    unit = _unit_hash(ids, salt)
    return (
        np.flatnonzero(unit >= share).astype(np.int64),
        np.flatnonzero(unit < share).astype(np.int64),
    )


def fold_of_ids(ids: Sequence[Any] | npt.NDArray[Any], folds: int, seed: int) -> IntArray:
    """Each customer's fold, 0..folds-1: a fixed hash of (seed, id), the same in every run."""
    unit = _unit_hash(ids, f"{FOLD_SALT}:{seed}")
    return np.minimum((unit * folds).astype(np.int64), folds - 1)


def value_per_conversion(data: UpliftData) -> FloatArray:
    """Mean margin of a buyer, per message arm (arm 1 first): that arm's buyers, else all treated buyers,
    else 0."""
    bought = data.converted > 0
    treated = bought & (data.arm > 0)
    pooled = float(data.margin[treated].mean()) if treated.any() else 0.0
    out = np.empty(data.n_arms, dtype=np.float64)
    for k in range(1, data.n_arms + 1):
        rows = bought & (data.arm == k)
        out[k - 1] = float(data.margin[rows].mean()) if rows.any() else pooled
    return out


def customer_value(per_arm: FloatArray, n: int, conversion_value: FloatArray | None) -> FloatArray:
    """The margin of a purchase per customer and message arm (n x arms): the customer's own value when it
    is known (the same for every arm), else the arm's mean margin of a buyer (`value_per_conversion`)."""
    if conversion_value is not None:
        if len(conversion_value) != n:
            raise ValueError(f"conversion_value must have one entry per customer ({n})")
        out: FloatArray = np.repeat(
            np.asarray(conversion_value, dtype=np.float64)[:, None], len(per_arm), axis=1
        )
        return out
    return np.tile(np.asarray(per_arm, dtype=np.float64), (n, 1))


# --- the cross-fitted model ----------------------------------------------------------------------


class CrossFitUplift:
    """K fold models and their per-arm calibration, fitted on one campaign (`CrossFitUplift.fit`).

    `fit_rows[f]` are the positions (in the data it was fitted on) of the customers fold model f saw;
    `fold` is every fitted customer's fold. `out_of_fold()` scores each fitted customer with the one
    model that did not see them; `score(...)` scores any other customer with all K."""

    def __init__(
        self,
        encoder: FeatureEncoder,
        models: tuple[ArmModel, ...],
        fold: IntArray,
        calibration: tuple[ArmFit, ...],
        oof_raw: FloatArray,
        notes: tuple[str, ...],
    ) -> None:
        self.encoder = encoder
        self.models = models
        self.fold = fold
        self.fit_rows: tuple[IntArray, ...] = tuple(
            np.flatnonzero(fold != f).astype(np.int64) for f in range(len(models))
        )
        self.calibration = calibration
        self._oof_raw = oof_raw
        self.notes = notes

    @property
    def n_arms(self) -> int:
        return len(self.calibration)

    @classmethod
    def fit(
        cls,
        data: UpliftData,
        *,
        folds: int = FOLDS,
        seed: int = 0,
        learner: UpliftLearner | None = None,
    ) -> CrossFitUplift | None:
        """The model, or None when the campaign cannot support one (too few customers, an arm or the
        control too small, or a learner that refuses it); `fit_or_reason` also says why."""
        model, _ = cls.fit_or_reason(data, folds=folds, seed=seed, learner=learner)
        return model

    @classmethod
    def fit_or_reason(
        cls,
        data: UpliftData,
        *,
        folds: int = FOLDS,
        seed: int = 0,
        learner: UpliftLearner | None = None,
    ) -> tuple[CrossFitUplift | None, str]:
        if folds < 2:
            raise ValueError("cross-fitting needs at least 2 folds")
        k_arms = data.n_arms
        if data.n < MIN_ROWS:
            return None, f"only {data.n} customers to fit on (needs {MIN_ROWS})"
        counts = np.bincount(data.arm, minlength=k_arms + 1)
        small = [data.arm_names[a] for a in range(k_arms + 1) if counts[a] < MIN_ARM_ROWS]
        if small:
            return None, f"too few customers in {', '.join(small)} (needs {MIN_ARM_ROWS} each)"
        fitter: UpliftLearner = learner if learner is not None else SharedBaselineLogit()
        behaviour = np.full((data.n, len(BEHAVIOUR)), np.nan)
        encoder = FeatureEncoder.fit(data.attributes, behaviour)
        x = encoder.matrix(data.attributes, behaviour)
        fold = fold_of_ids(data.customer_ids, folds, seed)
        if len(np.unique(fold)) < folds:
            return None, "a fold came out empty"
        y, arm = data.converted, data.arm
        weight = np.ones(data.n)
        mu0 = np.empty(data.n)
        mu = np.empty((data.n, k_arms))
        models: list[ArmModel] = []
        for f in range(folds):
            fit_rows, pred_rows = fold != f, fold == f
            try:
                model = fitter.fit(
                    x[fit_rows], arm[fit_rows], y[fit_rows], weight[fit_rows], k_arms, encoder.blocks
                )
            except ValueError as exc:
                return None, f"the uplift learner refused the campaign: {exc}"
            m0, mk = model.predict(x[pred_rows])
            mu0[pred_rows], mu[pred_rows] = m0, mk
            models.append(model)
        oof_raw = mu - mu0[:, None]
        # the K-model average on the fitted customers: only its mean is used (the calibration's centre, on
        # the scale `score` reads), as in `fit_uplift`
        avg = _averaged_uplift(models, x, k_arms)
        control = arm == 0
        calibration = tuple(
            calibrate_arm(oof_raw[:, k], avg[:, k], mu0, mu[:, k], y, arm == k + 1, control, data.propensity)
            for k in range(k_arms)
        )
        notes = tuple(
            f"{data.arm_names[k + 1]}: ate {c.ate:+.5f} +/- {c.ate_se:.5f}, slope {c.slope:.2f} +/- "
            f"{c.slope_se:.2f} (used {c.slope_used:.2f})"
            for k, c in enumerate(calibration)
        )
        return cls(encoder, tuple(models), fold, calibration, oof_raw, notes), ""

    def out_of_fold_raw(self) -> FloatArray:
        """The uncalibrated out-of-fold uplift of each fitted customer (n x arms)."""
        return self._oof_raw.copy()

    def out_of_fold(self) -> FloatArray:
        """The calibrated uplift of each fitted customer (n x arms), from the model that never saw them."""
        out = np.empty_like(self._oof_raw)
        for k, cal in enumerate(self.calibration):
            out[:, k] = cal.apply(self._oof_raw[:, k])
        return out

    def raw(self, attributes: Sequence[Mapping[str, str]]) -> FloatArray:
        """The uncalibrated uplift of customers outside the fitting data, the K models averaged."""
        behaviour = np.full((len(attributes), len(BEHAVIOUR)), np.nan)
        x = self.encoder.matrix(attributes, behaviour)
        return _averaged_uplift(self.models, x, self.n_arms)

    def score(self, attributes: Sequence[Mapping[str, str]]) -> FloatArray:
        """The calibrated uplift (customers x arms) of customers the models were not fitted on."""
        raw = self.raw(attributes)
        out = np.empty_like(raw)
        for k, cal in enumerate(self.calibration):
            out[:, k] = cal.apply(raw[:, k])
        return out


def _averaged_uplift(models: Sequence[ArmModel], x: npt.NDArray[np.float32], k_arms: int) -> FloatArray:
    total = np.zeros((x.shape[0], k_arms))
    for model in models:
        m0, mk = model.predict(x)
        total += mk - m0[:, None]
    out: FloatArray = total / len(models)
    return out


# --- the decision --------------------------------------------------------------------------------


def expected_net(score: FloatArray, value: FloatArray, cost: FloatArray) -> FloatArray:
    """Expected extra profit of each message arm for each customer: `score x value - cost`.

    `score` is customers x arms; `value` the margin of a purchase, either per arm (the mean margin of a
    buyer) or per customer and arm (`customer_value`); `cost` the arms' cost (arms 1.. only: pass
    `arm_cost[1:]`)."""
    v = value if value.ndim == 2 else value[None, :]
    out: FloatArray = score * v - cost[None, :]
    return out


def decide_arms(net: FloatArray) -> IntArray:
    """Per customer the arm (1..) with the largest net if that is above 0, else 0. Deterministic."""
    if net.shape[1] == 0:
        return np.zeros(net.shape[0], dtype=np.int64)
    best = np.argmax(net, axis=1)
    top = net[np.arange(net.shape[0]), best]
    return np.where(top > 0.0, best + 1, 0).astype(np.int64)


def one_hot(arm: IntArray, n_actions: int) -> FloatArray:
    """The arms as a probability matrix (customers x actions), one 1 per row."""
    pi = np.zeros((len(arm), n_actions), dtype=np.float64)
    pi[np.arange(len(arm)), arm] = 1.0
    return pi


# --- the gate ------------------------------------------------------------------------------------


def dr_terms(logged: LoggedBandit, pi: FloatArray, q: FloatArray) -> FloatArray:
    """The per-row doubly robust terms of policy `pi` on `logged` with reward model `q`: their mean is the
    harness's DR value (`autonomy.proof.replay.evaluate`), and two policies' terms on the same rows give
    their paired difference."""
    rows = np.arange(logged.n_rows)
    w = pi[rows, logged.actions] / logged.propensity
    out: FloatArray = (pi * q).sum(axis=1) + w * (logged.reward - q[rows, logged.actions])
    return out


def _interval(terms: FloatArray) -> tuple[float, float, float]:
    est = ope.mean_interval(terms)
    return float(est.point), float(est.low), float(est.high)


@dataclass(frozen=True)
class GateResult:
    """Which branch the policy took, and the numbers it was judged on (all on the gate part of train)."""

    chose: str
    """"model", or "rule:<name>"."""
    lb: float | None
    """The lower end of the 95% interval of the paired difference, model minus `best_rule`, on the gate
    part (None: no model could be fitted). The model is deployed only when it is above 0."""
    diff: tuple[float, float, float] | None
    """(point, low, high) of that paired difference."""
    model_value: float | None
    """The model policy's DR value on the gate part."""
    model_contact: float | None
    """Share of the gate part the model policy messages."""
    best_rule: str
    """The comparison rule, chosen on the fit part (as the bench's `choose_simple`)."""
    best_rule_value: float
    """Its DR value on the gate part."""
    values: Mapping[str, tuple[float, float, float]]
    """name -> (point, low, high) on the gate part of every policy judged, the model's first. Only the model
    and `best_rule` are compared; the others are for the report."""
    chosen_on: Mapping[str, float]
    """name -> the fit-part value each rule was ranked by when `best_rule` was chosen."""
    gate_rows: int
    fit_rows: int
    reason: str

    def line(self) -> str:
        """The one log line CI parses: `key=value` pairs, string values JSON-quoted."""

        def num(v: float | None) -> str:
            return "n/a" if v is None else f"{v:.6g}"

        return (
            f"UPLIFT_GATE chose={json.dumps(self.chose)} lb={num(self.lb)} "
            f"diff={num(None if self.diff is None else self.diff[0])} "
            f"best_rule={json.dumps(self.best_rule)} best_rule_value={num(self.best_rule_value)} "
            f"model_value={num(self.model_value)} model_contact={num(self.model_contact)} "
            f"gate_rows={self.gate_rows} fit_rows={self.fit_rows} reason={json.dumps(self.reason)}"
        )


_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse_gate_line(line: str) -> dict[str, str]:
    """The `key=value` pairs of an `UPLIFT_GATE` line (`GateResult.line`), quoted values unquoted."""
    if not line.startswith("UPLIFT_GATE "):
        raise ValueError("not an UPLIFT_GATE line")
    out: dict[str, str] = {}
    for key, raw in _PAIR.findall(line[len("UPLIFT_GATE ") :]):
        out[key] = str(json.loads(raw)) if raw.startswith('"') else raw
    return out


def _fixed(n: int, actions: int, arm: int) -> FloatArray:
    return one_hot(np.full(n, arm, dtype=np.int64), actions)


def _dr_value(logged: LoggedBandit, pi: FloatArray) -> float:
    return float(dr_terms(logged, pi, per_action_mean_model(logged)).mean())


def simple_name(names: Sequence[str], arm: int) -> str:
    return "send to none" if arm == 0 else f"send to all: {names[arm]}"


def segment_name(columns: Sequence[str]) -> str:
    return "segment rule (" + "+".join(columns) + ")"


def _choose_rule(
    part: UpliftData, segment_candidates: Sequence[Sequence[str]], seed: int
) -> tuple[str, tuple[str, ...] | None, dict[str, float]]:
    """The best simple rule on `part`, chosen as the bench's `choose_simple` chooses it: the segment rule's
    columns by DR on a validation slice after fitting on the rest, each blanket policy by DR on all of
    `part` (it is not fitted, so that is out of sample). Returns (name, segment columns or None, the value
    each candidate was ranked by)."""
    candidates: dict[str, float] = {}
    best_cols: tuple[str, ...] | None = None
    sub_fit, sub_val = _split(part.customer_ids, f"{VALIDATION_SALT}:{seed}", VALIDATION_SHARE)
    if segment_candidates and len(sub_fit) >= 2 and len(sub_val) >= 2:
        log_sub_fit, log_sub_val = part.take(sub_fit).logged(), part.take(sub_val).logged()
        best_val = -np.inf
        for cols in segment_candidates:
            pol = fit_segment_policy(log_sub_fit, segment_columns=list(cols))
            v = _dr_value(log_sub_val, pol(log_sub_val))
            if v > best_val:
                best_val, best_cols = v, tuple(cols)
        assert best_cols is not None
        candidates[segment_name(best_cols)] = best_val
    log_part = part.logged()
    actions = len(part.arm_names)
    for arm in range(actions):
        candidates[simple_name(part.arm_names, arm)] = _dr_value(log_part, _fixed(part.n, actions, arm))
    best = max(candidates, key=lambda key: candidates[key])
    chosen_segment = best_cols if best_cols is not None and best == segment_name(best_cols) else None
    return best, chosen_segment, candidates


@dataclass
class UpliftThresholdPolicy:
    """A fitted policy: the cost threshold on a model, or a simple rule (see the module docstring)."""

    gate: GateResult
    value: FloatArray
    """The mean margin of a buyer per message arm on all of train (used when a customer's own is unknown)."""
    cost: FloatArray
    arm_names: tuple[str, ...]
    model: CrossFitUplift | None = None
    """The deployed model (set when the gate chose it)."""
    ungated: CrossFitUplift | None = None
    """The model refitted on all of train, whatever the gate chose (None when train cannot support one)."""
    rule_arm: int | None = None
    rule_segment: SegmentPolicy | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def n_actions(self) -> int:
        return len(self.arm_names)

    @property
    def belief(self) -> CrossFitUplift | None:
        """The model the expected nets are read from: the deployed one, else the ungated one."""
        return self.model if self.model is not None else self.ungated

    def _model_net(
        self,
        model: CrossFitUplift,
        attributes: Sequence[Mapping[str, str]],
        conversion_value: FloatArray | None,
    ) -> FloatArray:
        value = customer_value(self.value, len(attributes), conversion_value)
        return expected_net(model.score(attributes), value, self.cost)

    def net(
        self, attributes: Sequence[Mapping[str, str]], conversion_value: FloatArray | None = None
    ) -> FloatArray | None:
        """The expected net of each message arm (customers x arms) from the model the policy believes, or None.
        `conversion_value` is each customer's margin of a purchase when it is known."""
        if self.belief is None:
            return None
        return self._model_net(self.belief, attributes, conversion_value)

    def ungated_arms(
        self, attributes: Sequence[Mapping[str, str]], conversion_value: FloatArray | None = None
    ) -> IntArray | None:
        """The arm each customer would get from the model on all of train had the gate not been asked."""
        if self.ungated is None:
            return None
        return decide_arms(self._model_net(self.ungated, attributes, conversion_value))

    def arms(
        self,
        attributes: Sequence[Mapping[str, str]],
        features: pd.DataFrame,
        conversion_value: FloatArray | None = None,
    ) -> IntArray:
        """The arm each customer gets."""
        n = len(attributes)
        if self.model is not None:
            return decide_arms(self._model_net(self.model, attributes, conversion_value))
        if self.rule_segment is not None:
            dummy = LoggedBandit(
                source="uplift_threshold",
                contexts=features.reset_index(drop=True),
                actions=np.zeros(n, dtype=np.int64),
                propensity=np.ones(n),
                reward=np.zeros(n),
                action_names=self.arm_names,
                reward_name="profit",
            )
            return self.rule_segment.actions_for(dummy)
        return np.full(n, self.rule_arm or 0, dtype=np.int64)

    def pi(
        self,
        attributes: Sequence[Mapping[str, str]],
        features: pd.DataFrame,
        conversion_value: FloatArray | None = None,
    ) -> FloatArray:
        """The policy as a probability matrix (customers x actions): one 1 per row."""
        return one_hot(self.arms(attributes, features, conversion_value), self.n_actions)


def fit_uplift_threshold(
    train: UpliftData,
    *,
    segment_candidates: Sequence[Sequence[str]] = (),
    folds: int = FOLDS,
    seed: int = 0,
    learner: UpliftLearner | None = None,
    gate_share: float = GATE_SHARE,
) -> UpliftThresholdPolicy:
    """Fit the policy on `train` only (see the module docstring): model or best simple rule, by the gate.

    `segment_candidates` are the column sets the segment rule may use (as the bench's `segment_candidates`);
    none: the rules are send to none and send to all of each arm."""
    cost = train.arm_cost[1:].astype(np.float64)
    fit_pos, gate_pos = _split(train.customer_ids, f"{GATE_SALT}:{seed}", gate_share)
    if len(gate_pos) < 2 or len(fit_pos) < 2:
        raise ValueError("the train data is too small to hold out a gate part")
    fit_part, gate_part = train.take(fit_pos), train.take(gate_pos)
    log_fit, log_gate = fit_part.logged(), gate_part.logged()
    actions = len(train.arm_names)
    q_gate = per_action_mean_model(log_gate)

    # the comparison rule, chosen on the fit part only
    best_rule, best_cols, chosen_on = _choose_rule(fit_part, segment_candidates, seed)

    # every rule on the gate part, for the report (each segment rule fitted on the fit part)
    rule_terms: dict[str, FloatArray] = {}
    for arm in range(actions):
        rule_terms[simple_name(train.arm_names, arm)] = dr_terms(
            log_gate, _fixed(gate_part.n, actions, arm), q_gate
        )
    for cols in segment_candidates:
        pol = fit_segment_policy(log_fit, segment_columns=list(cols))
        rule_terms[segment_name(cols)] = dr_terms(log_gate, pol(log_gate), q_gate)
    values = {name: _interval(t) for name, t in rule_terms.items()}
    best_terms = rule_terms[best_rule]
    best_value = values[best_rule][0]

    # the model, fitted on the fit part, priced with the fit part's value of a buyer, judged on the gate part
    gate_model, why = CrossFitUplift.fit_or_reason(fit_part, folds=folds, seed=seed, learner=learner)
    lb: float | None = None
    diff: tuple[float, float, float] | None = None
    model_value: float | None = None
    model_contact: float | None = None
    chose = f"rule:{best_rule}"
    reason = why
    if gate_model is not None:
        value_fit = customer_value(value_per_conversion(fit_part), gate_part.n, gate_part.conversion_value)
        gate_arms = decide_arms(expected_net(gate_model.score(gate_part.attributes), value_fit, cost))
        model_terms = dr_terms(log_gate, one_hot(gate_arms, actions), q_gate)
        values = {"model": _interval(model_terms), **values}
        diff = _interval(model_terms - best_terms)
        lb, model_value = diff[1], values["model"][0]
        model_contact = float((gate_arms > 0).mean())
        if lb > 0.0:
            chose = "model"
            reason = "the paired difference, model minus the best simple rule, is above 0 at its lower bound"
        else:
            reason = (
                "the paired difference, model minus the best simple rule, is not above 0 at its lower bound"
            )
    gate = GateResult(
        chose=chose,
        lb=lb,
        diff=diff,
        model_value=model_value,
        model_contact=model_contact,
        best_rule=best_rule,
        best_rule_value=best_value,
        values=values,
        chosen_on=chosen_on,
        gate_rows=gate_part.n,
        fit_rows=fit_part.n,
        reason=reason,
    )
    policy = UpliftThresholdPolicy(
        gate=gate, value=value_per_conversion(train), cost=cost, arm_names=train.arm_names
    )
    # the model on all of train: deployed if the gate chose it, kept as the ungated policy either way
    final: CrossFitUplift | None = None
    why_final = why
    if gate_model is not None:
        final, why_final = CrossFitUplift.fit_or_reason(train, folds=folds, seed=seed, learner=learner)
    policy.ungated = final
    if final is not None:
        policy.notes.extend(final.notes)
    if chose == "model":
        if final is not None:
            policy.model = final
            return policy
        policy.notes.append(f"the model could not be refitted on all of train ({why_final}); using the rule")
        policy.gate = replace(gate, chose=f"rule:{best_rule}", reason=why_final)
    # the best simple rule, on all of train
    if best_cols is not None:
        policy.rule_segment = fit_segment_policy(train.logged(), segment_columns=list(best_cols))
    else:
        policy.rule_arm = next(a for a in range(actions) if simple_name(train.arm_names, a) == best_rule)
    return policy
