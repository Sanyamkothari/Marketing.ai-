"""Early signals: a customer's 90-day margin forecast from their first week (plan-v3 decision 8).

Waiting 90 days to learn whether a customer was worth having is too slow for an experiment. This
module predicts, from what a customer did in their first week (a second visit, a second order, the
size of the basket, a reply), the margin they will bring in the following 90 days. It is trained on
the shop's own history and it is switched on only when it has shown, on past groups of customers,
that its forecasts land close to what really happened.

**One cut-off per customer.** A customer's anchor is their first own event (signup, visit or
purchase); their cut-off `T` is the anchor plus `signal_days`. Features read events up to and
including `T`; the label is the margin earned in `(T, T + horizon_days]`. The two windows are
complements of one boundary, the same rule `engine.onboarding.labels.future_window_clause` writes
for the v1 engine, so an event at exactly `T` is counted in the features and never in the label.

**Censoring.** A window that is not over has no label yet. The end of the data is the latest event
in the whole log (or the `as_of` the caller gives), not each customer's own last event: a customer
whose own last event is old is exactly the one who left, so a per-customer end would define the
answer in terms of itself. A customer whose label window runs past the end of the data gets `nan`
for a label and is never trained on or scored against. A customer whose first week is not over has
no features yet and is not turned into an example at all.

**The backtest.** Customers are grouped by the month of their anchor. For each past month, a model is
fitted using only customers whose label windows had closed by the time that month's first weeks were
over (so nothing from after the forecast is used), and its forecast of the month's mean margin is
compared with the month's actual mean. A month counts as within the band when the forecast is
within `band` of the actual (relative, with a floor so a near-zero month does not divide by zero).
Months whose windows are not all over, that are too small, or that have too little earlier history,
are listed as skipped with the reason, not scored. The model is used only when at least
`min_share_within` of the scored months are within the band and at least `min_cohorts` were scored.

The model is a ridge regression on six features, numpy only. It is opt-in: with
`EarlySignalConfig.enabled` false (the default) `fit` returns a model that is not usable, and every
number in the agent is unchanged.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Estimate, Event, EventType, Group
from autonomy.proof.estimate import MIN_GROUP, welch_difference

__all__ = [
    "ANCHOR_TYPES",
    "FEATURE_NAMES",
    "Backtest",
    "CohortResult",
    "EarlyRead",
    "EarlySignal",
    "EarlySignalConfig",
    "Examples",
    "RidgeModel",
    "backtest",
    "build_examples",
    "early_read",
    "fit",
    "fit_ridge",
]

FloatArray = npt.NDArray[np.float64]

DAY: Final[float] = 86400.0

ANCHOR_TYPES: Final[frozenset[EventType]] = frozenset({EventType.SIGNUP, EventType.VISIT, EventType.PURCHASE})
"""What the customer did on their own that starts their clock. Messages we sent do not."""

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "second_visit",
    "second_order",
    "log_orders",
    "log_basket",
    "log_week_margin",
    "replied",
)
"""The model's inputs, all read from the first week only."""


@dataclass(frozen=True)
class EarlySignalConfig:
    """The knobs of the early-signal model. All defaults are placeholders to be agreed, not findings."""

    enabled: bool = False
    signal_days: int = 7
    horizon_days: int = 90
    band: float = 0.25
    """A month is within the band when |forecast - actual| / max(|actual|, floor) is at most this."""
    min_share_within: float = 0.8
    """Share of scored months that must be within the band (plan-v3 decision 8 pass test: 80%)."""
    min_cohorts: int = 3
    """Fewest scored months before the backtest can pass at all."""
    min_cohort_size: int = 30
    min_train: int = 200
    ridge: float = 1.0
    margin_rate: float = 0.3
    """Margin share assumed for a purchase or refund that carries no margin of its own."""
    floor_share: float = 0.1
    """The error denominator is at least this share of the mean absolute margin over all labels."""

    def __post_init__(self) -> None:
        if self.signal_days < 1 or self.horizon_days < 1:
            raise ValueError("signal_days and horizon_days must be at least 1")
        if self.band <= 0.0:
            raise ValueError("band must be positive")
        if not 0.0 < self.min_share_within <= 1.0:
            raise ValueError("min_share_within must be in (0, 1]")
        if self.min_cohorts < 1 or self.min_cohort_size < 1 or self.min_train < 2:
            raise ValueError("min_cohorts, min_cohort_size and min_train are too small")
        if self.ridge < 0.0:
            raise ValueError("ridge must not be negative")


def _margin_of(e: Event, rate: float) -> float:
    """What a purchase added (a refund took away) in margin, using `rate` when the event has none."""
    if e.type is EventType.PURCHASE:
        return e.margin if e.margin is not None else (e.value or 0.0) * rate
    if e.type is EventType.REFUND:
        return -abs(e.margin if e.margin is not None else (e.value or 0.0) * rate)
    return 0.0


def _month(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def _features(week: Sequence[Event], rate: float) -> list[float]:
    """The six inputs from a customer's first-week events (already cut at `T`)."""
    days = {int(e.ts.timestamp() // DAY) for e in week if e.type in (EventType.VISIT, EventType.PURCHASE)}
    buys = [e for e in week if e.type is EventType.PURCHASE]
    value = sum(e.value or 0.0 for e in buys)
    basket = value / len(buys) if buys else 0.0
    margin = sum(_margin_of(e, rate) for e in week)
    replied = any(e.type is EventType.REPLY for e in week)
    return [
        1.0 if len(days) >= 2 else 0.0,
        1.0 if len(buys) >= 2 else 0.0,
        math.log1p(len(buys)),
        math.log1p(max(basket, 0.0)),
        math.log1p(max(margin, 0.0)),
        1.0 if replied else 0.0,
    ]


@dataclass(frozen=True)
class Examples:
    """One row per customer whose first week is over: features, and a label unless censored."""

    customer_ids: tuple[str, ...]
    anchor_ts: FloatArray
    cutoff_ts: FloatArray
    features: FloatArray
    labels: FloatArray
    """The margin in `(T, T + horizon]`, or nan when that window runs past `data_end_ts`."""
    data_end_ts: float
    horizon_days: int
    open_week: int = 0
    """Customers left out because their first week was not over at the end of the data."""

    @property
    def labelled(self) -> npt.NDArray[np.bool_]:
        mask: npt.NDArray[np.bool_] = ~np.isnan(self.labels)
        return mask

    @property
    def close_ts(self) -> FloatArray:
        """When each row's label window closes."""
        closed: FloatArray = self.cutoff_ts + self.horizon_days * DAY
        return closed


def build_examples(
    events: Iterable[Event], config: EarlySignalConfig, *, as_of: datetime | None = None
) -> Examples:
    """Turn an event log into features at each customer's cut-off and censored 90-day labels.

    Events after `as_of` are ignored, and `as_of` is the end of the data; without it the end is the
    latest event. Features see events in `[anchor, T]`, the label sees `(T, T + horizon]`.
    """
    cutoff = as_of.timestamp() if as_of is not None else math.inf
    by_customer: dict[str, list[Event]] = defaultdict(list)
    data_end = -math.inf
    for e in events:
        ts = e.ts.timestamp()
        if ts > cutoff:
            continue
        by_customer[e.customer_id].append(e)
        data_end = max(data_end, ts)
    if as_of is not None:
        data_end = cutoff
    ids: list[str] = []
    anchors: list[float] = []
    cutoffs: list[float] = []
    rows: list[list[float]] = []
    labels: list[float] = []
    open_week = 0
    span = config.signal_days * DAY
    horizon = config.horizon_days * DAY
    for cid in sorted(by_customer):
        evs = sorted(by_customer[cid], key=lambda e: e.ts)
        starts = [e.ts.timestamp() for e in evs if e.type in ANCHOR_TYPES]
        if not starts:
            continue
        anchor = starts[0]
        t = anchor + span
        if t > data_end:
            open_week += 1
            continue
        week = [e for e in evs if anchor <= e.ts.timestamp() <= t]
        ids.append(cid)
        anchors.append(anchor)
        cutoffs.append(t)
        rows.append(_features(week, config.margin_rate))
        if t + horizon > data_end:
            labels.append(math.nan)
        else:
            labels.append(
                sum(_margin_of(e, config.margin_rate) for e in evs if t < e.ts.timestamp() <= t + horizon)
            )
    width = len(FEATURE_NAMES)
    return Examples(
        customer_ids=tuple(ids),
        anchor_ts=np.array(anchors, dtype=np.float64),
        cutoff_ts=np.array(cutoffs, dtype=np.float64),
        features=np.array(rows, dtype=np.float64).reshape(len(rows), width),
        labels=np.array(labels, dtype=np.float64),
        data_end_ts=data_end if math.isfinite(data_end) else 0.0,
        horizon_days=config.horizon_days,
        open_week=open_week,
    )


@dataclass(frozen=True)
class RidgeModel:
    """Ridge regression on standardised inputs, with an unpenalised intercept."""

    mean: FloatArray
    scale: FloatArray
    coef: FloatArray
    intercept: float

    def predict(self, x: FloatArray) -> FloatArray:
        z = (np.asarray(x, dtype=np.float64) - self.mean) / self.scale
        out: FloatArray = z @ self.coef + self.intercept
        return out


def fit_ridge(x: FloatArray, y: FloatArray, alpha: float) -> RidgeModel:
    """Fit by the closed form. A constant column gets scale 1 and so a zero coefficient."""
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    z = (x - mean) / scale
    intercept = float(y.mean())
    gram = z.T @ z + alpha * len(y) * np.eye(z.shape[1])
    coef = np.linalg.solve(gram, z.T @ (y - intercept))
    return RidgeModel(mean=mean, scale=scale, coef=coef, intercept=intercept)


@dataclass(frozen=True)
class CohortResult:
    """One month of customers: the forecast made before their windows closed, and what happened."""

    cohort: str
    n: int
    n_train: int
    forecast: float
    actual: float
    error: float
    """|forecast - actual| / max(|actual|, floor)."""
    within_band: bool


@dataclass(frozen=True)
class Backtest:
    cohorts: tuple[CohortResult, ...]
    skipped: tuple[tuple[str, str], ...]
    band: float
    share_within: float
    passed: bool
    reason: str
    correlation: float | None = None
    """Correlation of customer-level forecasts with actuals over the scored months, if defined."""


def _train_mask(ex: Examples, forecast_at: float) -> npt.NDArray[np.bool_]:
    """Rows whose label window had closed by `forecast_at`: all that could be known then."""
    mask: npt.NDArray[np.bool_] = ex.labelled & (ex.close_ts <= forecast_at)
    return mask


def backtest(ex: Examples, config: EarlySignalConfig) -> Backtest:
    """Forecast each past month with a model that saw only what was closed by then, and score it."""
    months = np.array([_month(t) for t in ex.anchor_ts]) if len(ex.anchor_ts) else np.array([], dtype=str)
    labels = ex.labels[ex.labelled]
    floor = config.floor_share * float(np.abs(labels).mean()) if len(labels) else 0.0
    results: list[CohortResult] = []
    skipped: list[tuple[str, str]] = []
    preds: list[FloatArray] = []
    actuals: list[FloatArray] = []
    for month in sorted(set(months.tolist())):
        idx = np.flatnonzero(months == month)
        if not ex.labelled[idx].all():
            skipped.append((month, "windows not over yet"))
            continue
        if len(idx) < config.min_cohort_size:
            skipped.append((month, f"only {len(idx)} customers"))
            continue
        forecast_at = float(ex.cutoff_ts[idx].max())
        train = _train_mask(ex, forecast_at)
        train[idx] = False
        n_train = int(train.sum())
        if n_train < config.min_train:
            skipped.append((month, f"only {n_train} earlier customers with a finished window"))
            continue
        model = fit_ridge(ex.features[train], ex.labels[train], config.ridge)
        pred = model.predict(ex.features[idx])
        actual = ex.labels[idx]
        f, a = float(pred.mean()), float(actual.mean())
        err = abs(f - a) / max(abs(a), floor, 1e-12)
        results.append(CohortResult(month, len(idx), n_train, f, a, err, err <= config.band))
        preds.append(pred)
        actuals.append(actual)
    within = sum(1 for r in results if r.within_band)
    share = within / len(results) if results else 0.0
    corr: float | None = None
    if preds:
        p, y = np.concatenate(preds), np.concatenate(actuals)
        if p.std() > 1e-12 and y.std() > 1e-12:
            corr = float(np.corrcoef(p, y)[0, 1])
    if len(results) < config.min_cohorts:
        passed, reason = False, (
            f"only {len(results)} months could be checked; at least {config.min_cohorts} are needed"
        )
    elif share < config.min_share_within:
        passed, reason = False, (
            f"{within} of {len(results)} months were within {config.band:.0%} of what happened; "
            f"at least {config.min_share_within:.0%} are needed"
        )
    else:
        passed, reason = True, f"{within} of {len(results)} months were within {config.band:.0%}"
    return Backtest(
        cohorts=tuple(results),
        skipped=tuple(skipped),
        band=config.band,
        share_within=share,
        passed=passed,
        reason=reason,
        correlation=corr,
    )


@dataclass(frozen=True)
class EarlySignal:
    """A fitted model, its own backtest, and whether it may be used."""

    config: EarlySignalConfig
    model: RidgeModel | None
    backtest: Backtest | None
    n_train: int
    usable: bool
    reason: str

    def forecasts(self, events: Iterable[Event], *, as_of: datetime | None = None) -> dict[str, float]:
        """Forecast margin in the 90 days after the first week, for every customer whose first week is
        over. Empty when the model is not usable, so nothing downstream changes while it is off."""
        if not self.usable or self.model is None:
            return {}
        ex = build_examples(events, self.config, as_of=as_of)
        if not len(ex.customer_ids):
            return {}
        pred = self.model.predict(ex.features)
        return {cid: float(p) for cid, p in zip(ex.customer_ids, pred, strict=True)}


def fit(events: Iterable[Event], config: EarlySignalConfig, *, as_of: datetime | None = None) -> EarlySignal:
    """Backtest on the shop's own history and, if it passes and the config is on, fit the final model
    on every customer whose window is over."""
    if not config.enabled:
        return EarlySignal(config, None, None, 0, False, "early signals are switched off")
    ex = build_examples(events, config, as_of=as_of)
    bt = backtest(ex, config)
    train = ex.labelled
    n_train = int(train.sum())
    if n_train < config.min_train:
        return EarlySignal(
            config, None, bt, n_train, False, f"only {n_train} customers with a finished window"
        )
    if not bt.passed:
        return EarlySignal(config, None, bt, n_train, False, bt.reason)
    model = fit_ridge(ex.features[train], ex.labels[train], config.ridge)
    return EarlySignal(config, model, bt, n_train, True, bt.reason)


@dataclass(frozen=True)
class EarlyRead:
    """An early read of a test: forecast margin per customer, agent group against the holdout."""

    n_agent: int
    n_holdout: int
    agent_mean: float
    holdout_mean: float
    difference: Estimate
    """Agent minus holdout, per customer, forecast margin with a 95% interval."""
    band: float
    share_within: float
    cohorts_checked: int
    horizon_days: int = 90
    notes: tuple[str, ...] = field(default=())


def early_read(
    signal: EarlySignal, forecasts: Mapping[str, float], group_of: Mapping[str, Group]
) -> EarlyRead | None:
    """The early answer: how the agent group's forecast 90-day margin compares with the holdout's.

    None when the model is not usable or either group has fewer than `MIN_GROUP` forecast customers.
    The interval covers the spread between customers only; the model's own error is what the
    backtest band reports, and the real 30/60/90-day numbers confirm.
    """
    if not signal.usable or signal.backtest is None:
        return None
    agent = np.array([v for c, v in sorted(forecasts.items()) if group_of.get(c) is Group.AGENT])
    holdout = np.array([v for c, v in sorted(forecasts.items()) if group_of.get(c) is Group.HOLDOUT])
    if len(agent) < MIN_GROUP or len(holdout) < MIN_GROUP:
        return None
    diff = welch_difference(agent, holdout)
    if diff is None:
        return None
    return EarlyRead(
        n_agent=len(agent),
        n_holdout=len(holdout),
        agent_mean=float(agent.mean()),
        holdout_mean=float(holdout.mean()),
        difference=diff,
        band=signal.backtest.band,
        share_within=signal.backtest.share_within,
        cohorts_checked=len(signal.backtest.cohorts),
        horizon_days=signal.config.horizon_days,
    )
