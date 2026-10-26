"""The slow models of plan-v3 decision 1: leaving risk and 90-day value, from the customer profile.

* **Leaving risk** (`churn_risk`): the chance a customer who has been buying stops buying in the next
  `risk_window_days` (60). A regularised logistic regression.
* **90-day value** (`clv_90d`): what the customer brings in the next `value_window_days` (90), in
  margin where the shop reports it. A ridge regression.

Both learn from the profile (`autonomy.profile`, 52 numbers) at past month-ends, with labels read from
the window after each month-end, so what a model learned from is what the agent is fed.

**Point in time.** A training row's features are `profile_for(events, month_end)`: no event after the
month-end is read (`autonomy.profile.facts.gather` drops them). Its label is read only from events in
the window after the month-end. Nothing after the `as_of` of the fit is read at all, so a fit run today
and a fit run on the same log with later events appended and the same `as_of` are identical.

**Censoring.** A month-end whose window is not over at `as_of` (`month_end + window > as_of`) has no
label yet: it is left out of that model's rows, never counted as "stopped buying" or "worth nothing".
The end of the data is the `as_of` the caller gives, not each customer's own last event: a customer
whose last event is old is exactly the one who left, so a per-customer end would define the answer in
terms of itself.

**Who is in the rows.** Risk is asked of customers who have been buying: at least one order in the
`active_days` (180) before the month-end. Someone who stopped long ago is not "about to leave". Value is
asked of every customer with at least one order. Others get no score, and bidders read a missing score
as "not known".

**The held-out check.** The last month-end with a closed window is held out. The rows used to train
are those whose own window closed by the held-out month-end (the purge: a training label must not look
past the moment the held-out rows were taken, or it would peek at the held-out period), so the check
is a model trained on the past and asked about a later month. Risk reports the AUC (the chance a
customer who left scores higher than one who stayed), value reports the root-mean-square error against
the error of guessing the training mean (`skill` = 1 - model / guess). A model is used only when its
check passes the bar in `SlowModelConfig`; a model that passes is refit on every closed row. Too few
rows, too few of either class, or fewer than two month-ends all fail with the reason in the check.

Numpy only. Pure: no clock, no randomness, no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Event, EventType
from autonomy.profile import NAMES, training_rows

__all__ = [
    "CHURN_RISK",
    "CLV_90D",
    "Clv90Model",
    "LeavingRiskModel",
    "RiskCheck",
    "SlowModelConfig",
    "SlowModels",
    "TrainingSet",
    "ValueCheck",
    "auc",
    "build_training_set",
    "month_ends",
]

CHURN_RISK = "churn_risk"
CLV_90D = "clv_90d"
"""The names under which the models write `CustomerState.scores`."""

_DAY = timedelta(days=1)
_ORDERED = "buy_ordered"
_RECENCY = "buy_days_since_last_order"
_FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class SlowModelConfig:
    """The numbers of the slow models. The defaults are the plan's (60-day risk, 90-day value)."""

    risk_window_days: int = 60
    value_window_days: int = 90
    active_days: int = 180
    """Risk is asked only of customers with an order in this many days before the month-end."""
    risk_ridge: float = 1.0
    """Prior precision of each standardised coefficient of the logistic regression."""
    value_ridge: float = 10.0
    """Ridge penalty of the value regression, on standardised features and a standardised target."""
    value_cap_quantile: float = 0.99
    """Training targets are capped at this quantile, so one huge order does not set the fit."""
    min_rows: int = 150
    """Training rows needed (after the purge) before a model is trusted at all."""
    min_holdout_rows: int = 40
    min_class: int = 10
    """Risk needs at least this many customers who left and who stayed among the training rows (a
    quarter of it among the held-out rows)."""
    min_auc: float = 0.60
    """The bar for leaving risk: held-out AUC at or above it."""
    min_value_skill: float = 0.05
    """The bar for 90-day value: held-out error at least this much below the guess of the mean."""
    rounds: int = 30

    def __post_init__(self) -> None:
        if min(self.risk_window_days, self.value_window_days, self.active_days) < 1:
            raise ValueError("windows must be at least one day")
        if self.risk_ridge <= 0 or self.value_ridge <= 0:
            raise ValueError("ridge penalties must be positive")
        if not 0.5 <= self.value_cap_quantile <= 1:
            raise ValueError("value_cap_quantile must be in [0.5, 1]")


# --- month-ends and training rows -----------------------------------------------------------------


def month_ends(first: datetime, last: datetime) -> list[datetime]:
    """The last instant (UTC, one microsecond before the next month begins) of every calendar month
    from the month of `first` up to `last`, a month-end after `last` left out."""
    out: list[datetime] = []
    year, month = first.astimezone(UTC).year, first.astimezone(UTC).month
    while True:
        nxt = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
        end = nxt - timedelta(microseconds=1)
        if end > last:
            return out
        out.append(end)
        year, month = nxt.year, nxt.month


@dataclass(frozen=True)
class TrainingSet:
    """Month-end rows with labels, as arrays. Row i is one customer at one month-end."""

    names: tuple[str, ...]
    customers: tuple[str, ...]
    cuts: tuple[datetime, ...]
    """The distinct month-ends, ascending; `cut_of[i]` indexes it."""
    cut_of: npt.NDArray[np.int64]
    x: _FloatArray
    stopped: _FloatArray
    """1.0 if the customer made no order in the risk window, else 0.0; NaN when the window is not over
    (censored) or the customer was not active."""
    value: _FloatArray
    """Margin (revenue where none is reported) in the value window; NaN when the window is not over or
    the customer had never ordered."""
    censored_risk: int = 0
    """Active customer-month-ends whose risk window was not over at `as_of`, left without a label."""
    censored_value: int = 0

    @property
    def rows(self) -> int:
        return int(self.x.shape[0])


def _amount(value: float | None) -> float:
    return float(value) if value is not None and math.isfinite(value) else 0.0


def _margin_rate(events: Sequence[Event]) -> float:
    """Share of order value kept as margin over the purchases that report one; 1 (revenue) if none."""
    kept = total = 0.0
    for e in events:
        if e.type is EventType.PURCHASE and e.margin is not None and math.isfinite(e.margin):
            kept += e.margin
            total += max(_amount(e.value), 0.0)
    return kept / total if total > 0 and kept > 0 else 1.0


def build_training_set(
    events: Iterable[Event], as_of: datetime, config: SlowModelConfig | None = None
) -> TrainingSet:
    """Month-end rows for both models, read from `events` as they stood at `as_of`.

    An event after `as_of` is dropped before anything is read; a repeated `event_id` counts once. A
    row is made for each month-end with at least one closed window (risk's or value's) and for each
    customer with an event by then; each label is NaN where its window is not over (censored) or the
    customer is not in that model's population. See the module docstring.
    """
    cfg = config or SlowModelConfig()
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    seen: set[str] = set()
    kept: list[Event] = []
    for e in events:
        if e.ts <= as_of and e.event_id not in seen:
            seen.add(e.event_id)
            kept.append(e)
    names = tuple(NAMES)
    empty = TrainingSet(
        names, (), (), np.zeros(0, np.int64), np.zeros((0, len(names))), np.zeros(0), np.zeros(0)
    )
    if not kept:
        return empty
    rate = _margin_rate(kept)
    buys: dict[str, list[tuple[float, float, float]]] = {}
    for e in kept:
        if e.type is EventType.PURCHASE or e.type is EventType.REFUND:
            money = abs(_amount(e.value))
            if e.type is EventType.PURCHASE:
                profit = _amount(e.margin) if e.margin is not None and math.isfinite(e.margin) else money * rate
                buys.setdefault(e.customer_id, []).append((e.ts.timestamp(), money, profit))
            else:
                buys.setdefault(e.customer_id, []).append((e.ts.timestamp(), 0.0, -money * rate))
    stamps: dict[str, _FloatArray] = {}
    paid: dict[str, _FloatArray] = {}
    gain_cum: dict[str, _FloatArray] = {}
    for cid, rows in buys.items():
        rows.sort()
        stamps[cid] = np.array([r[0] for r in rows])
        paid[cid] = np.array([r[1] for r in rows])
        gain_cum[cid] = np.concatenate([[0.0], np.cumsum([r[2] for r in rows])])

    first = min(e.ts for e in kept)
    shortest = min(cfg.risk_window_days, cfg.value_window_days)
    cuts = [c for c in month_ends(first, as_of) if c + shortest * _DAY <= as_of]
    if not cuts:
        return empty
    risk_end = {c: c + cfg.risk_window_days * _DAY <= as_of for c in cuts}
    value_end = {c: c + cfg.value_window_days * _DAY <= as_of for c in cuts}
    index = {c: i for i, c in enumerate(cuts)}
    j_ordered, j_recency = names.index(_ORDERED), names.index(_RECENCY)

    ids: list[str] = []
    cut_of: list[int] = []
    feats: list[list[float]] = []
    stopped: list[float] = []
    value: list[float] = []
    censored_risk = censored_value = 0
    for row in training_rows(kept, cuts, price_context=False):
        f = row.features
        vec = [f[n] for n in names]
        ordered = vec[j_ordered] >= 0.5
        active = ordered and vec[j_recency] <= cfg.active_days
        t0 = row.as_of.timestamp()
        ts, pay = stamps.get(row.customer_id), paid.get(row.customer_id)
        lab_stop = math.nan
        if active:
            if risk_end[row.as_of]:
                lo, hi = _window(ts, t0, cfg.risk_window_days)
                lab_stop = 0.0 if pay is not None and float(pay[lo:hi].sum()) > 0 else 1.0
            else:
                censored_risk += 1
        lab_value = math.nan
        if ordered:
            if value_end[row.as_of]:
                lo, hi = _window(ts, t0, cfg.value_window_days)
                cum = gain_cum.get(row.customer_id)
                lab_value = max(float(cum[hi] - cum[lo]), 0.0) if cum is not None else 0.0
            else:
                censored_value += 1
        if math.isnan(lab_stop) and math.isnan(lab_value):
            continue
        ids.append(row.customer_id)
        cut_of.append(index[row.as_of])
        feats.append(vec)
        stopped.append(lab_stop)
        value.append(lab_value)
    return TrainingSet(
        names,
        tuple(ids),
        tuple(cuts),
        np.array(cut_of, dtype=np.int64),
        np.array(feats, dtype=np.float64).reshape(len(ids), len(names)),
        np.array(stopped, dtype=np.float64),
        np.array(value, dtype=np.float64),
        censored_risk,
        censored_value,
    )


def _window(ts: _FloatArray | None, t0: float, days: int) -> tuple[int, int]:
    """Indices of the purchases in (t0, t0 + days]."""
    if ts is None:
        return 0, 0
    return int(np.searchsorted(ts, t0, side="right")), int(np.searchsorted(ts, t0 + days * 86_400.0, side="right"))


# --- the fits ---------------------------------------------------------------------------------------


def auc(score: _FloatArray, label: _FloatArray) -> float:
    """Chance a randomly chosen positive scores higher than a randomly chosen negative (ties count
    half). 0.5 when either class is empty."""
    positive = label >= 0.5
    n_pos, n_neg = int(positive.sum()), int((~positive).sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    _, inverse, counts = np.unique(score, return_inverse=True, return_counts=True)
    last = np.cumsum(counts)
    ranks = (last - (counts - 1) / 2.0)[inverse]  # average 1-based rank of each tie group
    return float((ranks[positive].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def _standardise(x: _FloatArray) -> tuple[_FloatArray, _FloatArray, _FloatArray]:
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale = np.where(scale > 1e-9, scale, np.inf)  # a constant column becomes all zeros: weight 0
    return (x - mean) / scale, mean, scale


def _fit_logistic(z: _FloatArray, y: _FloatArray, ridge: float, rounds: int) -> _FloatArray:
    """(intercept, weights) of a ridge logistic regression by Newton steps; the intercept is free."""
    design = np.column_stack([np.ones(len(z)), z])
    k = design.shape[1]
    beta = np.zeros(k)
    penalty = np.full(k, ridge)
    penalty[0] = 0.0
    for _ in range(rounds):
        mu = 1.0 / (1.0 + np.exp(-np.clip(design @ beta, -30.0, 30.0)))
        grad = design.T @ (y - mu) - penalty * beta
        hess = design.T @ (design * (mu * (1.0 - mu))[:, None]) + np.diag(penalty) + 1e-6 * np.eye(k)
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if float(np.abs(step).max()) < 1e-8:
            break
    return beta


def _fit_ridge(z: _FloatArray, y: _FloatArray, ridge: float) -> _FloatArray:
    """(intercept, weights) of a ridge regression; the intercept is free."""
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.full(design.shape[1], ridge)
    penalty[0] = 0.0
    return np.linalg.solve(design.T @ design + np.diag(penalty) + 1e-9 * np.eye(design.shape[1]), design.T @ y)


@dataclass(frozen=True)
class LeavingRiskModel:
    """Chance of no order in the next `window_days`, from a profile."""

    names: tuple[str, ...]
    mean: _FloatArray
    scale: _FloatArray
    beta: _FloatArray
    window_days: int

    def predict(self, x: _FloatArray) -> _FloatArray:
        """Rows of profile values (columns in `names` order) -> chance of leaving, in (0, 1)."""
        z = (np.atleast_2d(x) - self.mean) / self.scale
        return np.asarray(1.0 / (1.0 + np.exp(-np.clip(self.beta[0] + z @ self.beta[1:], -30.0, 30.0))))


@dataclass(frozen=True)
class Clv90Model:
    """Margin in the next `window_days`, from a profile."""

    names: tuple[str, ...]
    mean: _FloatArray
    scale: _FloatArray
    beta: _FloatArray
    y_mean: float
    y_scale: float
    window_days: int

    def predict(self, x: _FloatArray) -> _FloatArray:
        z = (np.atleast_2d(x) - self.mean) / self.scale
        return np.asarray(np.maximum(self.y_mean + self.y_scale * (self.beta[0] + z @ self.beta[1:]), 0.0))


def _fit_risk(x: _FloatArray, y: _FloatArray, names: tuple[str, ...], cfg: SlowModelConfig) -> LeavingRiskModel:
    z, mean, scale = _standardise(x)
    return LeavingRiskModel(names, mean, scale, _fit_logistic(z, y, cfg.risk_ridge, cfg.rounds), cfg.risk_window_days)


def _fit_value(x: _FloatArray, y: _FloatArray, names: tuple[str, ...], cfg: SlowModelConfig) -> Clv90Model:
    z, mean, scale = _standardise(x)
    capped = np.minimum(y, float(np.quantile(y, cfg.value_cap_quantile)))
    centre, spread = float(capped.mean()), float(capped.std())
    spread = spread if spread > 1e-9 else 1.0
    beta = _fit_ridge(z, (capped - centre) / spread, cfg.value_ridge)
    return Clv90Model(names, mean, scale, beta, centre, spread, cfg.value_window_days)


# --- the held-out checks -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskCheck:
    """What the held-out check of leaving risk found."""

    passed: bool
    auc: float | None
    train_rows: int
    holdout_rows: int
    holdout_cut: datetime | None
    reason: str
    censored: int = 0
    """Active customer-month-ends without a label because their window was not over."""

    def describe(self) -> str:
        if self.auc is None:
            return f"Leaving risk is not used: {self.reason}."
        verdict = "used" if self.passed else "not used"
        return (
            f"Leaving risk is {verdict}: held-out AUC {self.auc:.3f} on {self.holdout_rows} customers of "
            f"the month ending {self.holdout_cut:%Y-%m-%d}, trained on {self.train_rows} earlier rows"
            + ("" if self.passed else f" ({self.reason})")
            + "."
        )


@dataclass(frozen=True)
class ValueCheck:
    """What the held-out check of 90-day value found."""

    passed: bool
    rmse: float | None
    baseline_rmse: float | None
    skill: float | None
    train_rows: int
    holdout_rows: int
    holdout_cut: datetime | None
    reason: str
    censored: int = 0

    def describe(self) -> str:
        if self.rmse is None or self.baseline_rmse is None or self.skill is None:
            return f"90-day value is not used: {self.reason}."
        verdict = "used" if self.passed else "not used"
        return (
            f"90-day value is {verdict}: held-out error {self.rmse:.2f} against {self.baseline_rmse:.2f} for "
            f"guessing the mean (skill {self.skill:.1%}) on {self.holdout_rows} customers of the month "
            f"ending {self.holdout_cut:%Y-%m-%d}, trained on {self.train_rows} earlier rows"
            + ("" if self.passed else f" ({self.reason})")
            + "."
        )


def _split(
    ts: TrainingSet, labelled: npt.NDArray[np.bool_], window_days: int
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_], datetime | None]:
    """(train mask, held-out mask, held-out month-end): the last month-end with labelled rows is held
    out, and training rows are those whose window closed by that month-end (the purge)."""
    present = sorted({int(c) for c in ts.cut_of[labelled]})
    if len(present) < 2:
        return np.zeros(ts.rows, np.bool_), np.zeros(ts.rows, np.bool_), None
    hold_cut = ts.cuts[present[-1]]
    cuts_ok = np.array([c + window_days * _DAY <= hold_cut for c in ts.cuts], dtype=np.bool_)
    held = labelled & (ts.cut_of == present[-1])
    train = labelled & cuts_ok[ts.cut_of]
    return train, held, hold_cut


@dataclass
class SlowModels:
    """Both slow models with their held-out checks. `fit` trains them; `scores` reads a profile."""

    config: SlowModelConfig = field(default_factory=SlowModelConfig)
    risk: LeavingRiskModel | None = None
    value: Clv90Model | None = None
    risk_check: RiskCheck = field(
        default_factory=lambda: RiskCheck(False, None, 0, 0, None, "not fitted yet")
    )
    value_check: ValueCheck = field(
        default_factory=lambda: ValueCheck(False, None, None, None, 0, 0, None, "not fitted yet")
    )
    fitted_as_of: datetime | None = None

    @property
    def risk_usable(self) -> bool:
        return self.risk is not None and self.risk_check.passed

    @property
    def value_usable(self) -> bool:
        return self.value is not None and self.value_check.passed

    def fit(self, events: Iterable[Event], as_of: datetime) -> None:
        """Train both models on `events` as they stood at `as_of` and check them out of time."""
        data = build_training_set(events, as_of, self.config)
        self.fitted_as_of = as_of
        self.risk, self.risk_check = self._fit_risk(data)
        self.value, self.value_check = self._fit_value(data)

    def _fit_risk(self, data: TrainingSet) -> tuple[LeavingRiskModel | None, RiskCheck]:
        cfg = self.config
        label = data.stopped
        labelled = ~np.isnan(label) if data.rows else np.zeros(0, np.bool_)
        train, held, cut = _split(data, labelled, cfg.risk_window_days)
        n_train, n_held = int(train.sum()), int(held.sum())

        def fail(reason: str, score: float | None = None) -> tuple[LeavingRiskModel | None, RiskCheck]:
            return None, RiskCheck(False, score, n_train, n_held, cut, reason, data.censored_risk)

        if cut is None:
            return fail("fewer than two month-ends have a closed window")
        if n_train < cfg.min_rows or n_held < cfg.min_holdout_rows:
            return fail(f"too few rows ({n_train} to train, {n_held} held out)")
        y_train, y_held = label[train], label[held]
        small = min(int(y_train.sum()), n_train - int(y_train.sum()))
        small_held = min(int(y_held.sum()), n_held - int(y_held.sum()))
        if small < cfg.min_class or small_held < max(cfg.min_class // 4, 1):
            return fail("too few customers who left, or too few who stayed")
        trial = _fit_risk(data.x[train], y_train, data.names, cfg)
        score = auc(trial.predict(data.x[held]), y_held)
        if score < cfg.min_auc:
            return fail(f"below the bar of AUC {cfg.min_auc:.2f}", score)
        final = _fit_risk(data.x[labelled], label[labelled], data.names, cfg)
        return final, RiskCheck(True, score, n_train, n_held, cut, "passed", data.censored_risk)

    def _fit_value(self, data: TrainingSet) -> tuple[Clv90Model | None, ValueCheck]:
        cfg = self.config
        label = data.value
        labelled = ~np.isnan(label) if data.rows else np.zeros(0, np.bool_)
        train, held, cut = _split(data, labelled, cfg.value_window_days)
        n_train, n_held = int(train.sum()), int(held.sum())

        def fail(
            reason: str, rmse: float | None = None, base: float | None = None
        ) -> tuple[Clv90Model | None, ValueCheck]:
            skill = 1.0 - rmse / base if rmse is not None and base is not None and base > 0 else None
            return None, ValueCheck(False, rmse, base, skill, n_train, n_held, cut, reason, data.censored_value)

        if cut is None:
            return fail("fewer than two month-ends have a closed window")
        if n_train < cfg.min_rows or n_held < cfg.min_holdout_rows:
            return fail(f"too few rows ({n_train} to train, {n_held} held out)")
        y_train, y_held = label[train], label[held]
        trial = _fit_value(data.x[train], y_train, data.names, cfg)
        rmse = float(np.sqrt(np.mean((trial.predict(data.x[held]) - y_held) ** 2)))
        base = float(np.sqrt(np.mean((float(y_train.mean()) - y_held) ** 2)))
        skill = 1.0 - rmse / base if base > 0 else 0.0
        if skill < cfg.min_value_skill:
            return fail(f"below the bar of {cfg.min_value_skill:.0%} better than the mean", rmse, base)
        final = _fit_value(data.x[labelled], label[labelled], data.names, cfg)
        return final, ValueCheck(True, rmse, base, skill, n_train, n_held, cut, "passed", data.censored_value)

    def scores(self, features: Mapping[str, float]) -> dict[str, float]:
        """`churn_risk` and `clv_90d` for one profile, for each model that passed its check and whose
        population the customer is in (risk: an order in the last `active_days`; value: any order).
        A feature missing from the profile reads as the training average."""
        out: dict[str, float] = {}
        if not (self.risk_usable or self.value_usable):
            return out
        if features.get(_ORDERED, 0.0) < 0.5:
            return out
        if self.risk is not None and self.risk_usable and features.get(_RECENCY, 0.0) <= self.config.active_days:
            out[CHURN_RISK] = float(self.risk.predict(_vector(self.risk.names, self.risk.mean, features))[0])
        if self.value is not None and self.value_usable:
            out[CLV_90D] = float(self.value.predict(_vector(self.value.names, self.value.mean, features))[0])
        return out

    def describe(self) -> str:
        return f"{self.risk_check.describe()} {self.value_check.describe()}"


def _vector(names: Sequence[str], mean: _FloatArray, features: Mapping[str, float]) -> _FloatArray:
    row = mean.copy()
    for i, name in enumerate(names):
        v = features.get(name)
        if v is not None and math.isfinite(v):
            row[i] = v
    return row
