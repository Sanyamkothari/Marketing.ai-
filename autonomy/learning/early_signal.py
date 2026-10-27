"""Early signals: a customer's 90-day margin forecast from their first week (plan-v3 decision 8).

Waiting 90 days to learn whether a customer was worth having is too slow for an experiment. This
module predicts, from what a customer did in one week (a second visit, a second order, the size of
the basket, a reply), the margin they will bring in the following 90 days. It is trained on the
shop's own history and it is switched on only when it has shown, on past groups of customers, that
its forecasts land close to what really happened and tell customers apart.

**Starts and cut-offs.** Every example has a start (its anchor) and a cut-off `T`, the anchor plus
`signal_days`. Features read the week `[anchor, T]`, plus four facts from before the anchor (had
the customer been seen before, their margin in the `horizon_days` before it, how long since they
were last seen and since they were first seen); the label is the margin earned in `(T, T + horizon_days]`. The two windows are
complements of one boundary, the same rule `engine.onboarding.labels.future_window_clause` writes
for the v1 engine, so an event at exactly `T` is counted in the features and never in the label.

For training the starts are each customer's first own event (signup, visit or purchase; messages we
sent do not start the clock) and, every `cutoff_every_days`, a past cut-off for every customer whose
first week was already over by then. The second kind is what a test needs: an existing customer
entering a win-back or repeat test starts their week when they enter it, not when they first
arrived, and the model has to have learned what such a week says. At forecast time the caller gives
the start per customer (`anchors`, e.g. when each customer entered the test), so the forecast reads
only behaviour from after that start.

**Censoring.** A window that is not over has no label yet. The end of the data is the latest event
in the whole log (or the `as_of` the caller gives), not each customer's own last event: a customer
whose own last event is old is exactly the one who left, so a per-customer end would define the
answer in terms of itself. An example whose label window runs past the end of the data gets `nan`
for a label and is never trained on or scored against. A start whose week is not over has no
features yet and is not turned into an example at all. Events repeated with the same `event_id` are
counted once.

**The backtest.** Examples are grouped by the month of their start. For each past month, a model is
fitted using only examples whose label windows had closed by the time that month's weeks were over
(so nothing from after the forecast is used), and its forecast of the month's mean margin is
compared with the month's actual mean. A month counts as within the band when the forecast is
within `band` of the actual (relative, with a floor from the training labels so a near-zero month
does not divide by zero). Months whose windows are not all over, that are too small, or that have
too little earlier history, are listed as skipped with the reason, not scored. The model is used
only when at least `min_cohorts` months were scored, at least `min_share_within` of them are within
the band, and the customer-level forecasts over the scored months correlate with what happened by
at least `min_correlation`: a model that gets each month's average right but cannot tell one
customer from another would say nothing about an agent group against a holdout.

The model is two ridge regressions on ten features, numpy only: one for starts with no history (a
new customer's first week) and one for starts of customers seen before, since what a week says
differs between the two. It is opt-in: with
`EarlySignalConfig.enabled` false (the default) `fit` returns a model that is not usable, and every
number in the agent is unchanged.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
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
    "DEFAULT_MARGIN_RATE",
    "FEATURE_NAMES",
    "Backtest",
    "CohortResult",
    "EarlyRead",
    "EarlySignal",
    "EarlySignalConfig",
    "Examples",
    "RidgeModel",
    "SplitModel",
    "backtest",
    "build_examples",
    "early_read",
    "fit",
    "fit_ridge",
    "fit_split",
]

FloatArray = npt.NDArray[np.float64]

DAY: Final[float] = 86400.0
DEFAULT_MARGIN_RATE: Final[float] = 0.3
"""The margin share assumed when the shop has not set one (`EarlySignalConfig.margin_rate` is None)."""

ANCHOR_TYPES: Final[frozenset[EventType]] = frozenset({EventType.SIGNUP, EventType.VISIT, EventType.PURCHASE})
"""What the customer did on their own that starts their clock. Messages we sent do not."""

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "second_visit",
    "second_order",
    "log_orders",
    "log_basket",
    "log_week_margin",
    "replied",
    "had_history",
    "log_prior_margin",
    "log_days_since_seen",
    "log_days_known",
)
"""The model's inputs: six read from the week after the start, four from before it."""

HISTORY: Final[int] = FEATURE_NAMES.index("had_history")


@dataclass(frozen=True)
class EarlySignalConfig:
    """The knobs of the early-signal model. All defaults are placeholders to be agreed, not findings."""

    enabled: bool = False
    signal_days: int = 7
    horizon_days: int = 90
    cutoff_every_days: int = 30
    """Training also starts a week at a past cut-off this often, for every customer already known
    then, so the model learns what a week after entering a test says for an existing customer. 0
    trains on first weeks only, and then forecasts are made only for customers with no history."""
    band: float = 0.25
    """A month is within the band when |forecast - actual| / max(|actual|, floor) is at most this."""
    min_share_within: float = 0.8
    """Share of scored months that must be within the band (plan-v3 decision 8 pass test: 80%)."""
    min_cohorts: int = 3
    """Fewest scored months before the backtest can pass at all."""
    min_correlation: float = 0.2
    """Lowest correlation of customer-level forecasts with actuals, over the scored months, that
    passes: below it the model cannot tell customers apart."""
    min_cohort_size: int = 30
    min_train: int = 200
    ridge: float = 1.0
    margin_rate: float | None = None
    """The shop's contribution margin (`RuntimeSettings.margin_rate`), assumed for a purchase or refund
    that carries no margin of its own. None: `DEFAULT_MARGIN_RATE` (0.3), as before."""
    floor_share: float = 0.1
    """The error denominator is at least this share of the mean absolute margin of the training
    labels used for that month."""

    @property
    def margin_share(self) -> float:
        """The margin share used for an event with none: the shop's setting, else `DEFAULT_MARGIN_RATE`."""
        return DEFAULT_MARGIN_RATE if self.margin_rate is None else self.margin_rate

    def __post_init__(self) -> None:
        if self.margin_rate is not None and not 0.0 < self.margin_rate <= 1.0:
            raise ValueError("margin_rate must be in (0, 1]")
        if self.signal_days < 1 or self.horizon_days < 1:
            raise ValueError("signal_days and horizon_days must be at least 1")
        if self.cutoff_every_days < 0:
            raise ValueError("cutoff_every_days must not be negative")
        if self.band <= 0.0:
            raise ValueError("band must be positive")
        if not 0.0 < self.min_share_within <= 1.0:
            raise ValueError("min_share_within must be in (0, 1]")
        if not -1.0 <= self.min_correlation <= 1.0:
            raise ValueError("min_correlation must be in [-1, 1]")
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


def _features(
    week: Sequence[Event], before: Sequence[Event], anchor: float, config: EarlySignalConfig
) -> list[float]:
    """The ten inputs: six from the week (already cut to `[anchor, T]`), four from `before` it."""
    rate = config.margin_share
    days = {int(e.ts.timestamp() // DAY) for e in week if e.type in (EventType.VISIT, EventType.PURCHASE)}
    buys = [e for e in week if e.type is EventType.PURCHASE]
    value = sum(e.value or 0.0 for e in buys)
    basket = value / len(buys) if buys else 0.0
    margin = sum(_margin_of(e, rate) for e in week)
    replied = any(e.type is EventType.REPLY for e in week)
    seen = [e.ts.timestamp() for e in before if e.type in ANCHOR_TYPES]
    since = anchor - config.horizon_days * DAY
    prior = sum(_margin_of(e, rate) for e in before if e.ts.timestamp() >= since)
    return [
        1.0 if len(days) >= 2 else 0.0,
        1.0 if len(buys) >= 2 else 0.0,
        math.log1p(len(buys)),
        math.log1p(max(basket, 0.0)),
        math.log1p(max(margin, 0.0)),
        1.0 if replied else 0.0,
        1.0 if seen else 0.0,
        math.log1p(max(prior, 0.0)),
        math.log1p((anchor - seen[-1]) / DAY) if seen else 0.0,
        math.log1p((anchor - seen[0]) / DAY) if seen else 0.0,
    ]


@dataclass(frozen=True)
class Examples:
    """One row per start whose week is over: features, and a label unless censored.

    Built for training, a customer can have several rows (their first week and later cut-offs).
    """

    customer_ids: tuple[str, ...]
    anchor_ts: FloatArray
    cutoff_ts: FloatArray
    features: FloatArray
    labels: FloatArray
    """The margin in `(T, T + horizon]`, or nan when that window runs past `data_end_ts`."""
    data_end_ts: float
    horizon_days: int
    open_week: int = 0
    """Starts left out because their week was not over at the end of the data."""

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
    events: Iterable[Event],
    config: EarlySignalConfig,
    *,
    as_of: datetime | None = None,
    anchors: Mapping[str, datetime] | None = None,
) -> Examples:
    """Turn an event log into features at each start's cut-off and censored 90-day labels.

    Events after `as_of` are ignored, and `as_of` is the end of the data; without it the end is the
    latest event. Features see events in `[anchor, T]` and four facts from before the anchor; the
    label sees `(T, T + horizon]`. Without `anchors` the starts are the training ones: each
    customer's first own event and, every `cutoff_every_days` from the start of the log, a cut-off
    for every customer whose first week was over by then. With `anchors`, each listed customer gets
    one start at the given time (a customer with no events gets a row of zeros), and customers not
    listed are left out. An event repeated with the same `event_id` is counted once.
    """
    cutoff = as_of.timestamp() if as_of is not None else math.inf
    by_customer: dict[str, list[Event]] = defaultdict(list)
    seen_ids: set[str] = set()
    data_start, data_end = math.inf, -math.inf
    for e in events:
        if e.event_id in seen_ids:
            continue
        seen_ids.add(e.event_id)
        ts = e.ts.timestamp()
        if ts > cutoff:
            continue
        by_customer[e.customer_id].append(e)
        data_start = min(data_start, ts)
        data_end = max(data_end, ts)
    if as_of is not None:
        data_end = cutoff
    span = config.signal_days * DAY
    horizon = config.horizon_days * DAY
    step = config.cutoff_every_days * DAY
    grid: list[float] = []
    if anchors is None and step > 0 and math.isfinite(data_start):
        c = data_start + step
        while c + span <= data_end:
            grid.append(c)
            c += step
    ids: list[str] = []
    starts: list[float] = []
    cutoffs: list[float] = []
    rows: list[list[float]] = []
    labels: list[float] = []
    open_week = 0
    for cid in sorted(by_customer if anchors is None else anchors):
        evs = sorted(by_customer.get(cid, ()), key=lambda e: (e.ts, e.event_id))
        stamps = [e.ts.timestamp() for e in evs]
        if anchors is not None:
            begins = [anchors[cid].timestamp()]
        else:
            own = [t for e, t in zip(evs, stamps, strict=True) if e.type in ANCHOR_TYPES]
            if not own:
                continue
            begins = [own[0], *(c for c in grid if c - span >= own[0])]
        for anchor in begins:
            t = anchor + span
            if t > data_end:
                open_week += 1
                continue
            lo, hi = bisect_left(stamps, anchor), bisect_right(stamps, t)
            ids.append(cid)
            starts.append(anchor)
            cutoffs.append(t)
            rows.append(_features(evs[lo:hi], evs[:lo], anchor, config))
            if t + horizon > data_end:
                labels.append(math.nan)
            else:
                end = bisect_right(stamps, t + horizon)
                labels.append(sum(_margin_of(e, config.margin_share) for e in evs[hi:end]))
    width = len(FEATURE_NAMES)
    return Examples(
        customer_ids=tuple(ids),
        anchor_ts=np.array(starts, dtype=np.float64),
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
class SplitModel:
    """One ridge model for starts with no history and one for customers seen before."""

    new: RidgeModel
    known: RidgeModel

    def predict(self, x: FloatArray) -> FloatArray:
        x = np.asarray(x, dtype=np.float64)
        out: FloatArray = np.where(x[:, HISTORY] > 0.0, self.known.predict(x), self.new.predict(x))
        return out


def fit_split(x: FloatArray, y: FloatArray, alpha: float) -> SplitModel:
    """Fit each part on its own rows; a part with fewer than two rows uses a model fitted on all."""
    has = x[:, HISTORY] > 0.0
    both = fit_ridge(x, y, alpha)
    new = fit_ridge(x[~has], y[~has], alpha) if int((~has).sum()) >= 2 else both
    known = fit_ridge(x[has], y[has], alpha) if int(has.sum()) >= 2 else both
    return SplitModel(new=new, known=known)


@dataclass(frozen=True)
class CohortResult:
    """One month of starts: the forecast made before their windows closed, and what happened."""

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

    @property
    def within(self) -> int:
        """How many scored months were within the band."""
        return sum(1 for r in self.cohorts if r.within_band)


def _train_mask(ex: Examples, forecast_at: float) -> npt.NDArray[np.bool_]:
    """Rows whose label window had closed by `forecast_at`: all that could be known then."""
    mask: npt.NDArray[np.bool_] = ex.labelled & (ex.close_ts <= forecast_at)
    return mask


def backtest(ex: Examples, config: EarlySignalConfig) -> Backtest:
    """Forecast each past month with a model that saw only what was closed by then, and score it."""
    months = np.array([_month(t) for t in ex.anchor_ts]) if len(ex.anchor_ts) else np.array([], dtype=str)
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
        floor = config.floor_share * float(np.abs(ex.labels[train]).mean())
        model = fit_split(ex.features[train], ex.labels[train], config.ridge)
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
    elif corr is None or corr < config.min_correlation:
        shown = "not defined" if corr is None else f"{corr:.2f}"
        passed, reason = False, (
            f"its forecasts did not tell customers apart (correlation with what happened {shown}; "
            f"at least {config.min_correlation:.2f} is needed)"
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
    model: SplitModel | None
    backtest: Backtest | None
    n_train: int
    usable: bool
    reason: str

    def forecasts(
        self,
        events: Iterable[Event],
        *,
        as_of: datetime | None = None,
        anchors: Mapping[str, datetime] | None = None,
    ) -> dict[str, float]:
        """Forecast margin in the 90 days after a week, per customer whose week is over.

        With `anchors` (e.g. when each customer entered a test) the week starts there, so only what
        the customer did after that start is read. Without it the week is the customer's first one.
        Customers seen before their start are left out when the model was trained on first weeks
        only (`cutoff_every_days=0`). Empty when the model is not usable, so nothing downstream
        changes while it is off.
        """
        if not self.usable or self.model is None:
            return {}
        if anchors is None:
            ex = build_examples(events, self.config, as_of=as_of, anchors=None)
            first: dict[str, int] = {}
            for i, cid in enumerate(ex.customer_ids):
                first.setdefault(cid, i)
            keep = np.array(sorted(first.values()), dtype=np.int64)
        else:
            ex = build_examples(events, self.config, as_of=as_of, anchors=anchors)
            keep = np.arange(len(ex.customer_ids), dtype=np.int64)
            if self.config.cutoff_every_days == 0:
                keep = keep[ex.features[keep, HISTORY] == 0.0]
        if not len(keep):
            return {}
        pred = self.model.predict(ex.features[keep])
        return {ex.customer_ids[i]: float(p) for i, p in zip(keep.tolist(), pred, strict=True)}


def fit(events: Iterable[Event], config: EarlySignalConfig, *, as_of: datetime | None = None) -> EarlySignal:
    """Backtest on the shop's own history and, if it passes and the config is on, fit the final model
    on every start whose window is over."""
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
    model = fit_split(ex.features[train], ex.labels[train], config.ridge)
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
    cohorts_within: int = 0
    """Of the backtest's scored months, how many were within the band."""
    horizon_days: int = 90
    signal_days: int = 7
    notes: tuple[str, ...] = field(default=())


def early_read(
    signal: EarlySignal,
    events: Iterable[Event],
    entered: Mapping[str, datetime],
    group_of: Mapping[str, Group],
    *,
    as_of: datetime | None = None,
) -> EarlyRead | None:
    """The early answer: how the agent group's forecast 90-day margin compares with the holdout's.

    `entered` says when each customer entered the test (their first decision, or when they were
    assigned to the holdout). Each forecast reads the customer's first `signal_days` after that
    moment and nothing before it except the four history facts, so what the agent did can show up;
    customers whose week in the test is not over yet are not counted. None when the model is not
    usable or either group has fewer than `MIN_GROUP` forecast customers. The interval covers the
    spread between customers only; the model's own error is what the backtest band reports, and the
    real 30/60/90-day numbers confirm.
    """
    if not signal.usable or signal.backtest is None:
        return None
    wanted = {c: t for c, t in entered.items() if group_of.get(c) in (Group.AGENT, Group.HOLDOUT)}
    forecasts = signal.forecasts(events, as_of=as_of, anchors=wanted)
    agent = np.array([v for c, v in sorted(forecasts.items()) if group_of.get(c) is Group.AGENT])
    holdout = np.array([v for c, v in sorted(forecasts.items()) if group_of.get(c) is Group.HOLDOUT])
    if len(agent) < MIN_GROUP or len(holdout) < MIN_GROUP:
        return None
    diff = welch_difference(agent, holdout)
    if diff is None:
        return None
    notes: list[str] = []
    waiting = len(wanted) - len(forecasts)
    if waiting:
        notes.append(f"{waiting:,} customers in the test are not counted yet: no forecast for them.")
    bt = signal.backtest
    return EarlyRead(
        n_agent=len(agent),
        n_holdout=len(holdout),
        agent_mean=float(agent.mean()),
        holdout_mean=float(holdout.mean()),
        difference=diff,
        band=bt.band,
        share_within=bt.share_within,
        cohorts_checked=len(bt.cohorts),
        cohorts_within=bt.within,
        horizon_days=signal.config.horizon_days,
        signal_days=signal.config.signal_days,
        notes=tuple(notes),
    )
