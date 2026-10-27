"""The uplift-threshold decision policy: a cross-fitted uplift model, a cost threshold, and a gate.

    policy = fit_uplift_threshold(train, segment_candidates=...)      # on a randomised campaign
    pi = policy.pi(attributes, features)                              # one row per customer, one-hot

**What it decides.** For one customer and each message arm k of the campaign, the expected extra profit is

    net_k = uplift_k(x) x value_k - cost_k

where `uplift_k(x)` is the model's rise in the chance of buying, `value_k` the mean margin of a buyer of
arm k, and `cost_k` the arm's channel and offer cost. The customer gets the arm with the largest `net_k`
if that is above 0, else nothing. Deterministic: no exploration, no draw, no multiplier, no hold. Ties go
to the lowest arm, and arm 0 (no message) wins a tie with a net of exactly 0.

**The model** (reused from `autonomy.uplift`, not rewritten): the one-hot customer features of
`FeatureEncoder` and the shared-baseline logistic learner `SharedBaselineLogit`, fitted with K-fold
cross-fitting. Customers are put into K folds by a hash of (seed, id); fold f's model is fitted on the
other K-1 folds and predicts fold f only, so a customer's out-of-fold score never comes from a model
that saw them. Each arm is then calibrated on those out-of-fold scores exactly as `fit_uplift` does it: the
doubly robust pseudo-outcome gives the arm's average effect, and the slope of the pseudo-outcome on the
centred score (`grouped_slope`, shrunk by its own noise) says how far the differences between customers are
real. The score is `ate + slope x (centred, winsorised raw uplift)`. Where the slope is 0 every customer gets
the arm's average effect, and the policy is the best blanket arm.

A customer outside the fitting data (the test half) is scored by the average of the K fold models.

**The gate** (the Scientist's rule, docs/v3/ROOT_CAUSE.md). A model is only worth deploying where it beats
what needs no model. The train data is split by a hash in two: 70% fits a model, 30% is held out. On the
held-out part, with the doubly robust estimator the bench uses (`autonomy.proof.replay.evaluate`, reward
model `per_action_mean_model`), the model policy's value gets a 95% interval, and so does each simple rule:
send to none, send to all of each arm, and the segment rule (fitted on the 70% for each candidate column
set). The model is deployed only if the LOWER end of its interval is above the best simple rule's point
value on that same held-out part. Otherwise the policy is that best simple rule (a segment rule is refitted
on all of train). `UpliftThresholdPolicy.gate` says which branch it took and why.

Standard library, numpy, pandas and the repo's own packages only.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.proof.replay import LoggedBandit, SegmentPolicy, evaluate, fit_segment_policy
from autonomy.uplift.features import BEHAVIOUR, FeatureEncoder
from autonomy.uplift.learner import ArmModel, SharedBaselineLogit, UpliftLearner
from autonomy.uplift.model import grouped_slope, shrunk_slope

__all__ = [
    "CrossFitUplift",
    "GateResult",
    "UpliftData",
    "UpliftThresholdPolicy",
    "decide_arms",
    "expected_net",
    "fit_uplift_threshold",
    "fold_of_ids",
    "one_hot",
    "value_per_conversion",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

FOLDS = 5
"""Cross-fitting folds."""
GATE_SHARE = 0.3
"""Share of train held out for the gate."""
MIN_ROWS = 400
"""Fewest customers a model is fitted on (below this the policy is the best simple rule)."""
MIN_ARM_ROWS = 40
"""Fewest customers an arm (and the control) needs in the part a model is fitted on."""
WINSOR = 0.01
FOLD_SALT = "autonomy-uplift-threshold-v1:fold"
GATE_SALT = "autonomy-uplift-threshold-v1:gate"
CONFIDENCE = 0.95


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

    def __post_init__(self) -> None:
        n = len(self.arm)
        for name in ("customer_ids", "propensity", "converted", "margin", "features", "attributes"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} must have one entry per customer ({n})")
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


# --- the cross-fitted model ----------------------------------------------------------------------


@dataclass(frozen=True)
class _ArmCalibration:
    ate: float
    ate_se: float
    slope: float
    slope_se: float
    slope_used: float
    centre: float
    low: float
    high: float

    def apply(self, raw: FloatArray) -> FloatArray:
        c = np.clip(raw - self.centre, self.low, self.high)
        out: FloatArray = np.clip(self.ate + self.slope_used * c, -1.0, 1.0)
        return out


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
        calibration: tuple[_ArmCalibration, ...],
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
        calibration = tuple(_calibrate(k, oof_raw, mu0, mu, data) for k in range(k_arms))
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
        total = np.zeros((len(attributes), self.n_arms))
        for model in self.models:
            m0, mk = model.predict(x)
            total += mk - m0[:, None]
        out: FloatArray = total / len(self.models)
        return out

    def score(self, attributes: Sequence[Mapping[str, str]]) -> FloatArray:
        """The calibrated uplift (customers x arms) of customers the models were not fitted on."""
        raw = self.raw(attributes)
        out = np.empty_like(raw)
        for k, cal in enumerate(self.calibration):
            out[:, k] = cal.apply(raw[:, k])
        return out


def _calibrate(
    k: int, oof_raw: FloatArray, mu0: FloatArray, mu: FloatArray, data: UpliftData
) -> _ArmCalibration:
    """One arm's calibration from the out-of-fold predictions (see the module docstring)."""
    n = data.n
    y, prop = data.converted, data.propensity
    tau = oof_raw[:, k]
    in_k = data.arm == k + 1
    control = data.arm == 0
    phi = tau.copy()
    phi[in_k] += (y[in_k] - mu[in_k, k]) / prop[in_k]
    phi[control] -= (y[control] - mu0[control]) / prop[control]
    ate = float(phi.mean())
    ate_se = float(phi.std(ddof=1) / math.sqrt(n))
    centre = float(tau.mean())
    low, high = (float(v) for v in np.quantile(tau - centre, [WINSOR, 1.0 - WINSOR]))
    c = np.clip(tau - centre, low, high)
    b, b_se = grouped_slope(c, phi)
    return _ArmCalibration(
        ate=ate,
        ate_se=ate_se,
        slope=b,
        slope_se=b_se,
        slope_used=shrunk_slope(b, b_se),
        centre=centre,
        low=low,
        high=high,
    )


# --- the decision --------------------------------------------------------------------------------


def expected_net(score: FloatArray, value: FloatArray, cost: FloatArray) -> FloatArray:
    """Expected extra profit of each message arm for each customer: `score x value - cost`.

    `score` is customers x arms, `value` the mean margin of a buyer per arm and `cost` the arms' cost
    (arms 1.. only: pass `arm_cost[1:]`)."""
    out: FloatArray = score * value[None, :] - cost[None, :]
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


@dataclass(frozen=True)
class GateResult:
    """Which branch the policy took, and the numbers it was judged on (all on the held-out part of train)."""

    chose: str
    """"model", or "rule:<name>"."""
    lb: float | None
    """The lower end of the model policy's 95% interval (None: no model could be fitted)."""
    model_value: float | None
    best_rule: str
    best_rule_value: float
    values: Mapping[str, tuple[float, float, float]]
    """name -> (point, low, high) of every policy judged, the model's first."""
    gate_rows: int
    fit_rows: int
    reason: str

    def line(self) -> str:
        """The one log line CI parses."""
        lb = "n/a" if self.lb is None else f"{self.lb:.6g}"
        model = "n/a" if self.model_value is None else f"{self.model_value:.6g}"
        return (
            f"UPLIFT_GATE chose={self.chose} lb={lb} best_rule={self.best_rule} "
            f"best_rule_value={self.best_rule_value:.6g} model_value={model} "
            f"gate_rows={self.gate_rows} fit_rows={self.fit_rows} reason={self.reason}"
        )


def _fixed(n: int, actions: int, arm: int) -> FloatArray:
    return one_hot(np.full(n, arm, dtype=np.int64), actions)


def _value(pi: FloatArray, logged: LoggedBandit, name: str) -> tuple[float, float, float]:
    est = evaluate(lambda _l: pi, logged, policy_name=name).dr
    return float(est.point), float(est.low), float(est.high)


def simple_name(names: Sequence[str], arm: int) -> str:
    return "send to none" if arm == 0 else f"send to all: {names[arm]}"


def segment_name(columns: Sequence[str]) -> str:
    return "segment rule (" + "+".join(columns) + ")"


@dataclass
class UpliftThresholdPolicy:
    """A fitted policy: the cost threshold on a model, or a simple rule (see the module docstring)."""

    gate: GateResult
    value: FloatArray
    cost: FloatArray
    arm_names: tuple[str, ...]
    model: CrossFitUplift | None = None
    """The deployed model (set when the gate chose it)."""
    belief: CrossFitUplift | None = None
    """The model the expected nets are read from: the deployed one, else the one fitted on the gate's fit part."""
    rule_arm: int | None = None
    rule_segment: SegmentPolicy | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def n_actions(self) -> int:
        return len(self.arm_names)

    def net(self, attributes: Sequence[Mapping[str, str]]) -> FloatArray | None:
        """The expected net of each message arm (customers x arms) from the model the policy believes, or None."""
        if self.belief is None:
            return None
        return expected_net(self.belief.score(attributes), self.value, self.cost)

    def arms(self, attributes: Sequence[Mapping[str, str]], features: pd.DataFrame) -> IntArray:
        """The arm each customer gets."""
        n = len(attributes)
        if self.model is not None:
            return decide_arms(expected_net(self.model.score(attributes), self.value, self.cost))
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

    def pi(self, attributes: Sequence[Mapping[str, str]], features: pd.DataFrame) -> FloatArray:
        """The policy as a probability matrix (customers x actions): one 1 per row."""
        return one_hot(self.arms(attributes, features), self.n_actions)


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
    value = value_per_conversion(train)
    cost = train.arm_cost[1:].astype(np.float64)
    unit = _unit_hash(train.customer_ids, f"{GATE_SALT}:{seed}")
    gate_pos = np.flatnonzero(unit < gate_share).astype(np.int64)
    fit_pos = np.flatnonzero(unit >= gate_share).astype(np.int64)
    if len(gate_pos) < 2 or len(fit_pos) < 2:
        raise ValueError("the train data is too small to hold out a gate part")
    fit_part, gate_part = train.take(fit_pos), train.take(gate_pos)
    log_fit, log_gate = fit_part.logged(), gate_part.logged()
    actions = len(train.arm_names)

    # the rules, judged on the gate part
    rules: dict[str, FloatArray] = {}
    segment_fits: dict[str, SegmentPolicy] = {}
    for arm in range(actions):
        rules[simple_name(train.arm_names, arm)] = _fixed(gate_part.n, actions, arm)
    for cols in segment_candidates:
        pol = fit_segment_policy(log_fit, segment_columns=list(cols))
        name = segment_name(cols)
        segment_fits[name] = pol
        rules[name] = pol(log_gate)
    values: dict[str, tuple[float, float, float]] = {}
    for name, pi_rule in rules.items():
        values[name] = _value(pi_rule, log_gate, name)
    best_rule = max(values, key=lambda key: values[key][0])
    best_value = values[best_rule][0]

    # the model, fitted on the fit part, judged on the gate part it never saw
    gate_model, why = CrossFitUplift.fit_or_reason(fit_part, folds=folds, seed=seed, learner=learner)
    lb: float | None = None
    model_value: float | None = None
    chose = f"rule:{best_rule}"
    reason = why
    if gate_model is not None:
        net = expected_net(gate_model.score(gate_part.attributes), value, cost)
        pi_model = one_hot(decide_arms(net), actions)
        point, low, high = _value(pi_model, log_gate, "model")
        values = {"model": (point, low, high), **values}
        lb, model_value = low, point
        if low > best_value:
            chose = "model"
            reason = "the model's lower bound is above the best simple rule's value"
        else:
            reason = "the model's lower bound is not above the best simple rule's value"
    gate = GateResult(
        chose=chose,
        lb=lb,
        model_value=model_value,
        best_rule=best_rule,
        best_rule_value=best_value,
        values=values,
        gate_rows=gate_part.n,
        fit_rows=fit_part.n,
        reason=reason,
    )
    policy = UpliftThresholdPolicy(gate=gate, value=value, cost=cost, arm_names=train.arm_names)
    policy.belief = gate_model
    if chose == "model":
        final, why_final = CrossFitUplift.fit_or_reason(train, folds=folds, seed=seed, learner=learner)
        if final is not None:
            policy.model = policy.belief = final
            policy.notes.extend(final.notes)
            return policy
        policy.notes.append(f"the model could not be refitted on all of train ({why_final}); using the rule")
        policy.gate = replace(gate, chose=f"rule:{best_rule}", reason=why_final)
    elif gate_model is not None:
        policy.notes.extend(gate_model.notes)
    # the best simple rule, on all of train
    if best_rule in segment_fits:
        cols = next(c for c in segment_candidates if segment_name(c) == best_rule)
        policy.rule_segment = fit_segment_policy(train.logged(), segment_columns=list(cols))
    else:
        policy.rule_arm = next(a for a in range(actions) if simple_name(train.arm_names, a) == best_rule)
    return policy
