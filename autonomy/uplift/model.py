"""Fit a per-customer, per-action uplift model on one past campaign, honestly, and score customers with it.

    fit_uplift(table) -> FittedUplift | None          (FittedUplift implements `UpliftScorer`)

**Steps.**

1. *Features*: `FeatureEncoder` on the campaign's customers (attributes and pre-campaign behaviour).
2. *Propensities*: a randomised campaign's are its design; for a propensity-logged one the control
   rows' chance of getting nothing is estimated from the features (a logistic model of being sent,
   cross-fitted), and the outcome models weigh each row by 1 / its propensity (capped at
   `MAX_WEIGHT`).
3. *Balance check*: the sent and the not-sent customers must look alike on every feature column
   (weighted standardised mean difference at most `max(BALANCE_SMD, 4 x its noise)`). If they do not,
   the control group is not a fair comparison (a targeted campaign read as randomised, say) and
   nothing is fitted.
4. *Cross-fitting*: customers are split in two by a hash of their id; the learner (`SharedBaselineLogit`
   by default, or any `UpliftLearner`, e.g. the v1 AutoML bridge) is fitted on each half and predicts
   the other, so every customer has an out-of-fold uplift `tau_oof(x)` per arm.
5. *Calibration, out of sample, per arm*: the doubly robust pseudo-outcome of each customer,
   `phi = tau_oof + 1[arm k] (y - mu_k) / e_k - 1[none] (y - mu_0) / e_0`, has mean the arm's
   average effect `ate` (with its standard error) whatever the model got wrong. The centred
   `tau_oof`, winsorised at its 1st and 99th percentiles, is cut into `CALIBRATION_GROUPS` equal
   groups; the slope `b` of the groups' mean `phi` on their mean `tau_oof` (weighted least squares,
   each group by its precision) says how far the model's differences between customers are real. A
   decile fit, not a per-customer one: with rare purchases a per-customer regression is ruled by the
   few customers with extreme predictions. `b` is shrunk towards zero by its own noise
   (positive-part James-Stein, `b x max(0, 1 - se_b^2 / b^2)`, and 0 when it is negative), so
   features move a customer's score only as far as they predicted lift on customers the model never
   saw.
6. *Score*: `ate + b_shrunk x clip(tau(x) - mean tau)`, where `tau(x)` averages the two half models
   and the clip is the same winsorisation, with sd `sqrt(se_ate^2 + (clip(...) x se_b)^2)`. Its
   average over the campaign's customers is about the measured average effect, so the level is
   calibrated by the experiment, not by a model.

The unit is the chance of buying in the learning window; the bidder turns it into money with its
value model, as it does the response model's uplift. Deterministic: no randomness anywhere.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import numpy.typing as npt

from autonomy.contracts import CustomerState
from autonomy.uplift.campaign import CampaignTable
from autonomy.uplift.features import FeatureEncoder, behaviour_from_state
from autonomy.uplift.learner import ArmModel, SharedBaselineLogit, UpliftLearner, tdot
from autonomy.uplift.score import UpliftScore

__all__ = [
    "ArmReport",
    "FittedUplift",
    "UpliftReport",
    "fit_uplift",
    "grouped_slope",
    "shrunk_slope",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

FOLDS = 2
MAX_WEIGHT = 20.0
PROPENSITY_CLIP = (0.05, 0.95)
BALANCE_SMD = 0.1
MAX_SLOPE = 2.0
CALIBRATION_GROUPS = 10
WINSOR = 0.01
CACHE_SIZE = 8192
_SALT = "autonomy-uplift-v1:"


def fold_of(customer_id: str) -> int:
    """A customer's half for cross-fitting: a fixed hash of their id (same half in every run)."""
    digest = hashlib.blake2b((_SALT + customer_id).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % FOLDS


@dataclass(frozen=True)
class ArmReport:
    """What the campaign showed for one arm."""

    key: str
    treated: int
    control: int
    ate: float
    """Average rise in the chance of buying (doubly robust, out of fold)."""
    ate_se: float
    slope: float
    """How far the model's differences between customers held out of sample (1 = fully)."""
    slope_se: float
    slope_used: float
    """`slope` shrunk by its noise; 0 means every customer gets the average effect."""


@dataclass(frozen=True)
class UpliftReport:
    arms: tuple[ArmReport, ...]
    customers: int
    features: tuple[str, ...]
    balance_smd: float
    """The largest weighted standardised mean difference between sent and not sent."""
    randomised: bool
    explicit_control: bool
    notes: tuple[str, ...]

    def summary(self) -> str:
        parts = [
            f"{a.key}: {a.ate:+.4f} +/- {a.ate_se:.4f} on average, slope {a.slope:.2f} +/- {a.slope_se:.2f} "
            f"(used {a.slope_used:.2f})"
            for a in self.arms
        ]
        return f"uplift from {self.customers} customers; " + "; ".join(parts)


@dataclass(frozen=True)
class _ArmCalibration:
    ate: float
    ate_se: float
    own_se: float
    slope: float
    slope_se: float
    centre: float
    low: float
    high: float


class FittedUplift:
    """A fitted uplift model (implements `autonomy.uplift.score.UpliftScorer`)."""

    def __init__(
        self,
        encoder: FeatureEncoder,
        models: tuple[ArmModel, ...],
        keys: tuple[str, ...],
        calibration: tuple[_ArmCalibration, ...],
        report: UpliftReport,
        n: int,
    ) -> None:
        self.encoder = encoder
        self.models = models
        self.keys = keys
        self._arm = {k: i for i, k in enumerate(keys)}
        self.calibration = calibration
        self.report = report
        self.n = n
        self._cache: dict[Hashable, tuple[float, ...]] = {}

    def raw(self, state: CustomerState, now: datetime) -> tuple[float, ...]:
        """The model's uncalibrated uplift per arm for this customer (both halves averaged)."""
        prefs = state.preferences
        key: Hashable = (
            state.customer_id,
            state.version,
            state.as_of,
            now,
            state.purchases,
            hash(frozenset(prefs.items())),
        )
        found = self._cache.get(key)
        if found is not None:
            return found
        cols = self.encoder.active(prefs, behaviour_from_state(state, now))
        total = [0.0] * len(self.keys)
        for m in self.models:
            mu0, mu = m.predict_row(cols)
            for k in range(len(self.keys)):
                total[k] += mu[k] - mu0
        out = tuple(t / len(self.models) for t in total)
        if len(self._cache) >= CACHE_SIZE:
            self._cache.clear()
        self._cache[key] = out
        return out

    def score(self, state: CustomerState, action_key: str, now: datetime) -> UpliftScore | None:
        k = self._arm.get(action_key)
        if k is None:
            return None
        cal = self.calibration[k]
        c = min(max(self.raw(state, now)[k] - cal.centre, cal.low), cal.high)
        mean = min(max(cal.ate + cal.slope * c, -1.0), 1.0)
        slope_part = (c * cal.slope_se) ** 2
        sd = math.sqrt(cal.ate_se**2 + slope_part)
        own = math.sqrt(cal.own_se**2 + slope_part)
        return UpliftScore(mean=mean, sd=sd, n=self.n, own_sd=own)


def _weighted_smd(
    x: npt.NDArray[np.float32], treated: npt.NDArray[np.bool_], w: FloatArray
) -> tuple[float, str]:
    """Largest |standardised mean difference| over the columns, and which column (as its index)."""
    w1, w0 = w * treated, w * ~treated
    s1, s0 = float(w1.sum()), float(w0.sum())
    if s1 <= 0 or s0 <= 0:
        return math.inf, "no rows"
    m1 = tdot(x, w1) / s1
    m0 = tdot(x, w0) / s0
    pooled = (m1 * (1 - m1) + m0 * (1 - m0)) / 2.0
    share = (m1 * s1 + m0 * s0) / (s1 + s0)
    ok = (share > 0.01) & (share < 0.99) & (pooled > 0)
    ok[0] = False  # the intercept
    if not ok.any():
        return 0.0, "none"
    smd = np.zeros_like(m1)
    smd[ok] = np.abs(m1[ok] - m0[ok]) / np.sqrt(pooled[ok])
    j = int(np.argmax(smd))
    return float(smd[j]), str(j)


def _grouped_slope(c: FloatArray, phi: FloatArray) -> tuple[float, float]:
    """(slope, its standard error) of the groups' mean `phi` on their mean `c`, `c` cut into
    `CALIBRATION_GROUPS` equal groups by rank; weighted least squares, each group by its precision."""
    order = np.argsort(c, kind="stable")
    groups = [g for g in np.array_split(order, CALIBRATION_GROUPS) if g.size > 1]
    if len(groups) < 3:
        return 0.0, math.inf
    mc = np.array([c[g].mean() for g in groups])
    mp = np.array([phi[g].mean() for g in groups])
    var = np.array([phi[g].var(ddof=1) / g.size for g in groups])
    w = 1.0 / np.maximum(var, 1e-18)
    cbar = float((w * mc).sum() / w.sum())
    pbar = float((w * mp).sum() / w.sum())
    sxx = float((w * (mc - cbar) ** 2).sum())
    if sxx <= 0:
        return 0.0, math.inf
    b = float((w * (mc - cbar) * (mp - pbar)).sum() / sxx)
    return b, 1.0 / math.sqrt(sxx)


grouped_slope = _grouped_slope
"""Public name of `_grouped_slope`, for callers that cross-fit with their own number of folds
(`autonomy.decide.uplift_threshold`)."""


def shrunk_slope(b: float, b_se: float) -> float:
    """`b` shrunk towards zero by its own noise (positive-part James-Stein), 0 when it is not positive or
    its error is unknown, capped at `MAX_SLOPE`: the slope `fit_uplift` uses for an arm."""
    if b > 0 and math.isfinite(b_se):
        return min(b * max(0.0, 1.0 - (b_se * b_se) / (b * b)), MAX_SLOPE)
    return 0.0


def _effective(w: FloatArray) -> float:
    s = float(w.sum())
    return s * s / float((w * w).sum()) if s > 0 else 0.0


def fit_uplift(
    table: CampaignTable, *, learner: UpliftLearner | None = None
) -> tuple[FittedUplift | None, list[str]]:
    """Fit the uplift model on a campaign (see the module docstring); None with the reason when the
    campaign cannot support one."""
    notes = list(table.notes)
    fitter: UpliftLearner = learner if learner is not None else SharedBaselineLogit()
    encoder = FeatureEncoder.fit(table.attributes, table.behaviour)
    x = encoder.matrix(table.attributes, table.behaviour)
    arm, y = table.arm, table.y
    folds = np.array([fold_of(c) for c in table.customer_ids], dtype=np.int64)
    treated = arm > 0
    prop = table.propensity.copy()
    missing = ~np.isfinite(prop)
    if missing.any():
        # A propensity-logged campaign's control rows: P(sent | x), cross-fitted.
        sent = treated.astype(np.float64)
        p_sent = np.empty(len(arm))
        for f in range(FOLDS):
            fit_rows, pred_rows = folds != f, folds == f
            m = SharedBaselineLogit().fit(
                x[fit_rows],
                np.zeros(int(fit_rows.sum()), dtype=np.int64),
                sent[fit_rows],
                np.ones(int(fit_rows.sum())),
                0,
                encoder.blocks,
            )
            p_sent[pred_rows] = m.predict(x[pred_rows])[0]
        prop[missing] = 1.0 - np.clip(p_sent[missing], *PROPENSITY_CLIP)
        notes.append("control propensities estimated from the features (logistic, cross-fitted)")
    prop = np.clip(prop, 1e-3, 1.0)
    if table.randomised and not missing.any():
        weight = np.ones(len(arm))
    else:
        weight = np.minimum(1.0 / prop, MAX_WEIGHT)
        weight = weight / weight.mean()

    smd, col = _weighted_smd(x, treated, 1.0 / prop)
    n1, n0 = _effective((1.0 / prop) * treated), _effective((1.0 / prop) * ~treated)
    limit = max(BALANCE_SMD, 4.0 * math.sqrt(1.0 / max(n1, 1.0) + 1.0 / max(n0, 1.0)))
    if smd > limit:
        return None, [
            *notes,
            f"the sent and not-sent customers differ (standardised difference {smd:.2f} > {limit:.2f} on "
            f"feature column {col}): the control group is not a fair comparison, so no uplift model",
        ]

    n, k_arms = len(arm), table.n_arms
    mu0 = np.empty(n)
    mu = np.empty((n, k_arms))
    models: list[ArmModel] = []
    for f in range(FOLDS):
        fit_rows, pred_rows = folds != f, folds == f
        try:
            model = fitter.fit(
                x[fit_rows], arm[fit_rows], y[fit_rows], weight[fit_rows], k_arms, encoder.blocks
            )
        except ValueError as exc:  # a learner that cannot take this campaign says so
            return None, [*notes, f"the uplift learner refused the campaign: {exc}"]
        m0, mk = model.predict(x[pred_rows])
        mu0[pred_rows], mu[pred_rows] = m0, mk
        models.append(model)

    avg = np.zeros((n, k_arms))
    for model in models:
        m0, mk = model.predict(x)
        avg += mk - m0[:, None]
    avg /= len(models)

    arms: list[ArmReport] = []
    cals: list[_ArmCalibration] = []
    control = arm == 0
    for k in range(k_arms):
        tau = mu[:, k] - mu0
        in_k = arm == k + 1
        own_part = np.zeros(n)
        own_part[in_k] = (y[in_k] - mu[in_k, k]) / prop[in_k]
        phi = tau + own_part
        phi[control] -= (y[control] - mu0[control]) / prop[control]
        ate = float(phi.mean())
        ate_se = float(phi.std(ddof=1) / math.sqrt(n))
        own_se = min(float(own_part.std(ddof=1) / math.sqrt(n)), ate_se)
        centre = float(avg[:, k].mean())
        low, high = (float(v) for v in np.quantile(tau - centre, [WINSOR, 1.0 - WINSOR]))
        c = np.clip(tau - centre, low, high)
        b, b_se = _grouped_slope(c, phi)
        used = shrunk_slope(b, b_se)
        used_se = b_se * used / b if used > 0 else 0.0
        arms.append(
            ArmReport(
                key=table.arm_keys[k],
                treated=int(in_k.sum()),
                control=int(control.sum()),
                ate=ate,
                ate_se=ate_se,
                slope=b,
                slope_se=b_se,
                slope_used=used,
            )
        )
        cals.append(
            _ArmCalibration(
                ate=ate,
                ate_se=ate_se,
                own_se=own_se,
                slope=used,
                slope_se=used_se,
                centre=centre,
                low=low,
                high=high,
            )
        )
    report = UpliftReport(
        arms=tuple(arms),
        customers=n,
        features=encoder.names,
        balance_smd=smd,
        randomised=table.randomised and not missing.any(),
        explicit_control=table.explicit_control,
        notes=tuple(notes),
    )
    notes.append(report.summary())
    return FittedUplift(encoder, tuple(models), table.arm_keys, tuple(cals), report, n), notes
