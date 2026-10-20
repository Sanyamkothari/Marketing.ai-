"""Purchase cycles: when will this customer buy this item (or category) again?

The refill reminder in the product vision only works if we know, for one customer and one product,
roughly when they will run out. Most customers have bought a given product once or twice, far too
few to estimate their own rhythm, so the model borrows strength (empirical Bayes):

- **The shape.** The gap between two purchases of the same thing is log-normal: on the log scale a
  customer's gaps scatter around their own mean `m_c` with a within-customer spread `sigma`.
- **The hierarchy.** Customers' own means scatter around the item's mean with a between-customer
  spread `tau`; item means are shrunk toward their category's, and categories toward the whole shop.
  A merchant's stated `typical_cycle_days` stands in for the category as the item's anchor.
- **Shrinkage.** A customer with `n` own gaps gets `m_c` estimated as the precision-weighted mix
  `(mu/tau^2 + n * mean_log/sigma^2) / (1/tau^2 + n/sigma^2)`: with no gaps it is the item's typical
  cycle, and with many it is their own. `own_weight` reports how much the customer's own data counted.
- **Customer pace.** Someone who orders anything every fortnight also refills a given item sooner
  than someone who orders twice a year. Each customer's order cadence (any item, shrunk the same
  way) shifts their anchor by `pace_slope` times its deviation from the shop's, a slope fitted
  within items. This is what helps the commonest case most: an item bought only once so far.
- **Not everyone comes back.** A cure-rate mixture: with probability `repeat_prob` the customer is
  on the log-normal cycle; otherwise they rebuy only off-cycle, at a small constant daily
  `background_rate` (someone rediscovering a product months later). That keeps `p14` honest for the
  many one-off buyers, and makes it fall away, rather than stay high, for someone long overdue.
  `repeat_prob` is the share of purchases followed by another within `repeat_horizon_days` (counting
  only purchases with that much history behind them), shrunk item -> category -> shop and then
  raised by shop-wide odds ratios for customers who have bought the thing 2, 3, 4 or 5+ times.

For a given `now` the model reports the date the next purchase is due (`last purchase + median
gap`), `days_until_due` (negative means overdue), `p14` (the chance they buy within the next 14 days,
given they have not bought since: the hazard over the next two weeks), and whether the *refill
window* is open: the time since the last purchase lies between two quantiles of their predicted
gap, by default the 20th and 60th percentiles (from a little before the typical refill to a little
after; on the online-retail log this window holds 20% of item pairs and 1.6x their 14-day rebuy
rate, where a 25th-90th window was no better than chance).

Gaps are counted in whole UTC days (see `autonomy.catalog.products.trips`). Pure and deterministic:
`now` and `as_of` are always passed in, and nothing is random.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import NormalDist
from typing import Any, Final, Literal, TypeAlias

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.catalog.products import (
    EPOCH,
    Catalogue,
    clean_retail_lines,
    from_days,
    purchases_frame,
    repeat_intervals,
    to_days,
    trips,
)
from autonomy.contracts import Event

__all__ = [
    "DEFAULT_CONFIG",
    "LEVEL_KEY",
    "VALIDATION_CUTOFFS",
    "CalibrationBin",
    "CalibrationReport",
    "CycleConfig",
    "CycleEstimate",
    "CycleModel",
    "HoldoutReport",
    "HoldoutSlice",
    "IntervalPrior",
    "Level",
    "ScoreQuality",
    "backtest_last_interval",
    "calibrate_at_cutoff",
    "main",
    "retail_log_purchases",
    "validation_markdown",
]

Level = Literal["item", "category", "order"]
"""What "buying it again" means: the same item, anything in the same category, or any order at all."""

LEVEL_KEY: Final[Mapping[str, str | None]] = {"item": "item_id", "category": "category", "order": None}
"""The purchase-table column each level keys on; `None` means every purchase counts."""

FloatArray: TypeAlias = npt.NDArray[np.float64]
_STD_NORMAL: Final[NormalDist] = NormalDist()
_erf = np.vectorize(math.erf, otypes=[np.float64])


@dataclass(frozen=True)
class CycleConfig:
    """The knobs of the hierarchy. The defaults are what the online-retail validation used.

    Strengths are pseudo-counts: `parent_strength` says how many customers' worth of evidence the
    parent's mean is worth when it is mixed with an item's own customers, `variance_strength` the
    same for the spreads (in degrees of freedom), and `repeat_strength` how many buyers' worth the
    parent's repeat rate is worth. The `default_*` values are the shop-wide prior used before any
    data exists.
    """

    parent_strength: float = 5.0
    merchant_strength: float = 10.0
    variance_strength: float = 5.0
    repeat_strength: float = 10.0
    repeat_horizon_days: float = 120.0
    window_open_quantile: float = 0.2
    window_close_quantile: float = 0.6
    default_cycle_days: float = 30.0
    default_sigma: float = 0.8
    default_tau: float = 0.6
    default_repeat_prob: float = 0.3
    default_repeat_odds_ratio: float = 2.0
    default_background_rate: float = 0.0005
    background_strength_days: float = 2000.0
    trip_buckets: int = 5
    min_sigma: float = 0.15
    min_tau: float = 0.05
    max_pace_slope: float = 1.5
    stated_sigma: float = 0.4
    stated_tau: float = 0.3

    def __post_init__(self) -> None:
        if not 0 < self.window_open_quantile < self.window_close_quantile < 1:
            raise ValueError("window quantiles must satisfy 0 < open < close < 1")
        if self.default_cycle_days <= 0 or self.repeat_horizon_days < 0:
            raise ValueError("default_cycle_days must be positive and repeat_horizon_days not negative")


DEFAULT_CONFIG: Final[CycleConfig] = CycleConfig()
"""The default knobs (frozen, so one shared instance is safe as a default argument)."""


@dataclass(frozen=True)
class IntervalPrior:
    """The population's purchase cycle at one level of the hierarchy (one item, category or shop).

    `mu` is the mean log gap in days among customers, `sigma` the spread of one customer's gaps around
    their own mean, `tau` the spread of customers' means, and `repeat_prob` the chance that a first
    purchase is followed by a second within `CycleConfig.repeat_horizon_days` (an on-cycle rebuy). `source` says where it came from: "item", "category", "shop",
    "catalogue" (the merchant's stated cycle) or "default" (no data at all).
    """

    mu: float
    sigma: float
    tau: float
    repeat_prob: float
    n_pairs: int = 0
    n_intervals: int = 0
    source: str = "default"

    @property
    def median_days(self) -> float:
        return math.exp(self.mu)


@dataclass(frozen=True)
class CycleEstimate:
    """One customer's purchase cycle for one item (or category, or order) at one moment `now`."""

    key: str
    level: str
    now: datetime
    last_purchase_at: datetime
    n_purchases: int = field(metadata={"doc": "Shopping trips (distinct UTC days) up to now"})
    n_own_intervals: int
    own_weight: float = field(metadata={"doc": "0 = all population, 1 = all the customer's own gaps"})
    mu: float = field(metadata={"doc": "Posterior mean of the log gap"})
    sigma: float = field(metadata={"doc": "Predictive spread of the log gap"})
    median_interval_days: float
    mean_interval_days: float
    repeat_prob: float = field(
        metadata={"doc": "Chance they are on the refill cycle at all, before looking at elapsed time"}
    )
    still_likely: float = field(
        metadata={"doc": "Chance they are still on the cycle (not lapsed), given nothing since"}
    )
    elapsed_days: float
    next_due_at: datetime
    days_until_due: float = field(metadata={"doc": "Negative = overdue"})
    p14: float
    window_start_at: datetime
    window_end_at: datetime
    window_open: bool
    prior_source: str
    customer_pace: float = field(
        default=0.0, metadata={"doc": "Log-scale order pace vs the shop; negative = orders more often"}
    )
    background_rate: float = field(default=0.0, metadata={"doc": "Daily rate of a late, off-cycle rebuy"})

    def p_within(self, days: float) -> float:
        """The chance of a purchase in the next `days` days, given none since the last one."""
        out = _p_within(
            np.array([self.mu]),
            np.array([self.sigma]),
            np.array([self.repeat_prob]),
            np.array([self.elapsed_days]),
            days,
            self.background_rate,
        )
        return float(out[0])


# ------------------------------------------------------------------------------------- maths
def _norm_cdf(z: FloatArray) -> FloatArray:
    out: FloatArray = 0.5 * (1.0 + _erf(z / math.sqrt(2.0)))
    return out


def _survival(t: FloatArray, mu: FloatArray, sigma: FloatArray) -> FloatArray:
    """P(gap > t) under the log-normal; 1 for t <= 0."""
    safe = np.where(t > 0, t, 1.0)
    s: FloatArray = 1.0 - _norm_cdf((np.log(safe) - mu) / sigma)
    out: FloatArray = np.where(t > 0, s, 1.0)
    return out


def _p_within(
    mu: FloatArray,
    sigma: FloatArray,
    pi: FloatArray,
    elapsed: FloatArray,
    horizon: float,
    background: FloatArray | float,
) -> FloatArray:
    """The cure-mixture hazard: P(buy in (e, e + h] | nothing by e).

    With chance `pi` the customer is on a log-normal cycle; otherwise they rebuy only off-cycle, at
    the small constant daily `background` rate (someone who rediscovers a product months later).
    """
    s_now = _survival(elapsed, mu, sigma)
    s_later = _survival(elapsed + horizon, mu, sigma)
    b_now = np.exp(-background * np.maximum(elapsed, 0.0))
    b_later = np.exp(-background * np.maximum(elapsed + horizon, 0.0))
    at_risk = (1.0 - pi) * b_now + pi * s_now
    events = pi * (s_now - s_later) + (1.0 - pi) * (b_now - b_later)
    out: FloatArray = np.where(at_risk > 0, events / np.where(at_risk > 0, at_risk, 1.0), 0.0)
    return np.clip(out, 0.0, 1.0)


def _odds(p: float) -> float:
    return p / (1.0 - p)


def _from_odds(o: FloatArray) -> FloatArray:
    out: FloatArray = o / (1.0 + o)
    return out


@dataclass(frozen=True)
class _Predicted:
    """The vectorised prediction for many (customer, key) pairs at once."""

    mu: FloatArray
    sigma: FloatArray
    own_weight: FloatArray
    repeat_prob: FloatArray
    still_likely: FloatArray
    median: FloatArray
    mean: FloatArray
    p14: FloatArray
    window_start: FloatArray
    window_end: FloatArray


# ------------------------------------------------------------------------------------- the model
@dataclass(frozen=True)
class _Raw:
    """Sufficient statistics of one level's log gaps."""

    n_pairs: int
    n_intervals: int
    mean_of_means: float
    ss_within: float
    df_within: int
    var_of_means: float
    mean_inv_n: float
    eligible: int
    repeated: int


class CycleModel:
    """The fitted hierarchy for one level ("item", "category" or "order").

    Fit it once (nightly) on everyone's purchases with `CycleModel.fit`, then ask it about any
    customer with `estimate` (one key) or `estimate_customer` (all the keys one customer bought).
    `CycleModel.from_catalogue` builds a data-free model from the catalogue's typical cycles, for a
    shop with no history yet.
    """

    def __init__(
        self,
        *,
        level: Level,
        config: CycleConfig,
        shop: IntervalPrior,
        groups: Mapping[str, IntervalPrior],
        keys: Mapping[str, IntervalPrior],
        key_group: Mapping[str, str],
        repeat_odds_ratios: Sequence[float],
        mean_interval_days: float,
        median_interval_days: float,
        as_of: datetime | None,
        order_prior: IntervalPrior | None = None,
        pace_slope: float = 0.0,
        background_rates: Sequence[float] = (),
    ) -> None:
        self.level: Level = level
        self.config = config
        self.shop = shop
        self.groups: dict[str, IntervalPrior] = dict(groups)
        self.keys: dict[str, IntervalPrior] = dict(keys)
        self.key_group: dict[str, str] = dict(key_group)
        self.repeat_odds_ratios: tuple[float, ...] = tuple(repeat_odds_ratios) or (1.0,)
        self.mean_interval_days = mean_interval_days
        self.median_interval_days = median_interval_days
        self.as_of = as_of
        self.order_prior: IntervalPrior = order_prior if order_prior is not None else shop
        self.pace_slope = pace_slope
        self.background_rates: tuple[float, ...] = tuple(background_rates) or (0.0,)

    # ------------------------------------------------------------------------------- building
    @classmethod
    def fit(
        cls,
        purchases: pd.DataFrame | Iterable[Event],
        as_of: datetime,
        *,
        level: Level = "item",
        catalogue: Catalogue | None = None,
        config: CycleConfig = DEFAULT_CONFIG,
    ) -> CycleModel:
        """Fit on every purchase at or before `as_of` (later rows are ignored, never peeked at).

        `purchases` is a purchase table (`purchases_frame`) or the events themselves. At the item
        level the catalogue, when given, supplies each item's category (else the events' own) and any
        typical cycle the merchant stated.
        """
        frame = purchases if isinstance(purchases, pd.DataFrame) else purchases_frame(purchases)
        as_of_day = to_days(as_of)
        frame = frame[frame["day"] <= as_of_day]
        trip_table = trips(frame, LEVEL_KEY[level])
        key_group = _key_groups(frame, level, catalogue)

        gaps = repeat_intervals(trip_table)
        gaps["log_gap"] = np.log(gaps["interval_days"].to_numpy(dtype=float))
        pair = (
            gaps.groupby(["customer_id", "key"], sort=True)["log_gap"]
            .agg(["size", "mean", "var"])
            .reset_index()
        )
        pair["ss"] = pair["var"].fillna(0.0) * (pair["size"] - 1)

        rep = _repeat_counts(trip_table, as_of_day, config.repeat_horizon_days)

        default = IntervalPrior(
            mu=math.log(config.default_cycle_days),
            sigma=config.default_sigma,
            tau=config.default_tau,
            repeat_prob=config.default_repeat_prob,
            source="default",
        )

        # Customer pace: a customer who orders anything often also refills each item sooner. Item
        # statistics are taken for a customer of average pace, and `pace_slope` puts it back.
        order_trips = trips(frame, None)
        order_prior = _order_prior(order_trips, default, config)
        pace_slope = 0.0
        if level != "order" and not pair.empty:
            deviation = _pace_series(order_trips, order_prior)
            d = pair["customer_id"].map(deviation).fillna(0.0).to_numpy(dtype=float)
            pace_slope = _pace_slope(pair, d, config.max_pace_slope)
            pair["mean"] = pair["mean"].to_numpy(dtype=float) - pace_slope * d

        all_raw = _raw(pair, rep[rep["trip_no"] == 1])
        shop = _shrink(all_raw, default, config, config.parent_strength, "shop")

        pair["group"] = [key_group.get(k) for k in pair["key"]]
        first = rep[rep["trip_no"] == 1].copy()
        first["group"] = [key_group.get(k) for k in first["key"]]
        groups: dict[str, IntervalPrior] = {}
        for g in sorted(set(key_group.values())):
            raw = _raw(pair[pair["group"] == g], first[first["group"] == g])
            groups[g] = _shrink(raw, shop, config, config.parent_strength, "category")

        stated: dict[str, float] = {}
        if catalogue is not None and level == "item":
            for item in catalogue.given_cycle_items:
                p = catalogue.get(item)
                if p is not None and p.typical_cycle_days is not None:
                    stated[item] = p.typical_cycle_days

        pair_by_key: dict[str, pd.DataFrame] = {str(k): v for k, v in pair.groupby("key", sort=True)}
        first_by_key: dict[str, pd.DataFrame] = {str(k): v for k, v in first.groupby("key", sort=True)}
        all_keys: set[str] = set(pair_by_key) | set(first_by_key) | set(stated)
        if catalogue is not None and level == "item":
            all_keys |= {p.item_id for p in catalogue}
        empty_pair = pair.iloc[0:0]
        empty_first = first.iloc[0:0]
        keys: dict[str, IntervalPrior] = {}
        for k in sorted(all_keys):
            group = key_group.get(k)
            parent = groups[group] if group is not None and group in groups else shop
            strength = config.parent_strength
            if k in stated:
                parent = IntervalPrior(
                    mu=math.log(stated[k]),
                    sigma=parent.sigma,
                    tau=parent.tau,
                    repeat_prob=parent.repeat_prob,
                    source="catalogue",
                )
                strength = config.merchant_strength
            raw = _raw(pair_by_key.get(k, empty_pair), first_by_key.get(k, empty_first))
            keys[k] = _shrink(raw, parent, config, strength, level if level != "order" else "shop")

        odds_ratios = _odds_ratios(rep, config)
        background = _background_rates(trip_table, as_of_day, config)

        intervals = gaps["interval_days"].to_numpy(dtype=float)
        mean_gap = float(intervals.mean()) if intervals.size else config.default_cycle_days
        median_gap = float(np.median(intervals)) if intervals.size else config.default_cycle_days
        return cls(
            level=level,
            config=config,
            shop=shop,
            groups=groups,
            keys=keys,
            key_group=key_group,
            repeat_odds_ratios=odds_ratios,
            mean_interval_days=mean_gap,
            median_interval_days=median_gap,
            as_of=as_of,
            order_prior=order_prior,
            pace_slope=pace_slope,
            background_rates=background,
        )

    @classmethod
    def from_catalogue(cls, catalogue: Catalogue, config: CycleConfig = DEFAULT_CONFIG) -> CycleModel:
        """A data-free item-level model: each item's typical cycle (stated or inferred) as its median.

        A typical cycle is trusted as a fairly tight cycle (`stated_sigma`, `stated_tau`); items
        without one fall back to their category's mean typical cycle, then to the default, with the
        default's wide spreads. Use it before any purchase history exists; `fit` is better as soon as
        it does.
        """
        shop = IntervalPrior(
            mu=math.log(config.default_cycle_days),
            sigma=config.default_sigma,
            tau=config.default_tau,
            repeat_prob=config.default_repeat_prob,
            source="default",
        )
        by_cat: dict[str, list[float]] = {}
        for p in catalogue:
            if p.category is not None and p.typical_cycle_days is not None:
                by_cat.setdefault(p.category, []).append(math.log(p.typical_cycle_days))
        groups = {
            c: IntervalPrior(
                mu=float(np.mean(v)),
                sigma=shop.sigma,
                tau=shop.tau,
                repeat_prob=shop.repeat_prob,
                n_pairs=0,
                source="catalogue",
            )
            for c, v in sorted(by_cat.items())
        }
        keys: dict[str, IntervalPrior] = {}
        key_group: dict[str, str] = {}
        for p in catalogue:
            if p.category is not None:
                key_group[p.item_id] = p.category
            if p.typical_cycle_days is not None:
                keys[p.item_id] = IntervalPrior(
                    mu=math.log(p.typical_cycle_days),
                    sigma=config.stated_sigma,
                    tau=config.stated_tau,
                    repeat_prob=shop.repeat_prob,
                    source="catalogue",
                )
        return cls(
            level="item",
            config=config,
            shop=shop,
            groups=groups,
            keys=keys,
            key_group=key_group,
            repeat_odds_ratios=(1.0,) + (config.default_repeat_odds_ratio,) * (config.trip_buckets - 1),
            mean_interval_days=config.default_cycle_days,
            median_interval_days=config.default_cycle_days,
            as_of=None,
            background_rates=(config.default_background_rate,),
        )

    # ------------------------------------------------------------------------------- reading
    def prior(self, key: str, group: str | None = None) -> IntervalPrior:
        """The population cycle for `key`, falling back to its category, then to the whole shop."""
        if key in self.keys:
            return self.keys[key]
        g = self.key_group.get(key, group)
        if g is not None and g in self.groups:
            return self.groups[g]
        return self.shop

    def estimate(
        self,
        key: str,
        purchase_times: Sequence[datetime],
        now: datetime,
        *,
        group: str | None = None,
        pace: float = 0.0,
    ) -> CycleEstimate | None:
        """One customer's cycle for `key` from their purchase times; `None` if none is at or before now.

        Purchases after `now` are ignored. `group` is the key's category when the model has never
        seen the key (an item new to the catalogue). `pace` is the customer's order pace from
        `pace_of` (0 = a typical customer); `estimate_customer` works it out itself.
        """
        now_day = to_days(now)
        days = sorted(d for d in (to_days(t) for t in purchase_times) if d <= now_day)
        if not days:
            return None
        trip_days: list[float] = []
        trip_index: list[int] = []
        for d in days:
            i = math.floor(d)
            if not trip_index or i != trip_index[-1]:
                trip_index.append(i)
                trip_days.append(d)
        logs = [math.log(b - a) for a, b in pairwise(trip_index)]
        prior = self.prior(key, group)
        pred = self._predict(
            mu0=np.array([prior.mu]),
            sigma=np.array([prior.sigma]),
            tau=np.array([prior.tau]),
            pi1=np.array([prior.repeat_prob]),
            n_trips=np.array([float(len(trip_days))]),
            n_own=np.array([float(len(logs))]),
            mean_log=np.array([float(np.mean(logs)) if logs else 0.0]),
            elapsed=np.array([now_day - trip_days[-1]]),
            pace=np.array([pace]),
        )
        last = from_days(trip_days[-1])
        median = float(pred.median[0])
        elapsed = now_day - trip_days[-1]
        return CycleEstimate(
            key=key,
            level=self.level,
            now=now,
            last_purchase_at=last,
            n_purchases=len(trip_days),
            n_own_intervals=len(logs),
            own_weight=float(pred.own_weight[0]),
            mu=float(pred.mu[0]),
            sigma=float(pred.sigma[0]),
            median_interval_days=median,
            mean_interval_days=float(pred.mean[0]),
            repeat_prob=float(pred.repeat_prob[0]),
            still_likely=float(pred.still_likely[0]),
            elapsed_days=elapsed,
            next_due_at=last + timedelta(days=median),
            days_until_due=median - elapsed,
            p14=float(pred.p14[0]),
            window_start_at=last + timedelta(days=float(pred.window_start[0])),
            window_end_at=last + timedelta(days=float(pred.window_end[0])),
            window_open=bool(pred.window_start[0] <= elapsed <= pred.window_end[0]),
            prior_source=prior.source,
            customer_pace=pace,
            background_rate=self._by_bucket(self.background_rates, len(trip_days)),
        )

    def estimate_customer(
        self, purchases: pd.DataFrame | Iterable[Event], now: datetime
    ) -> dict[str, CycleEstimate]:
        """Every key one customer bought, at `now`. Pass only that customer's purchases."""
        frame = purchases if isinstance(purchases, pd.DataFrame) else purchases_frame(purchases)
        column = LEVEL_KEY[self.level]
        keys = frame[column] if column is not None else pd.Series("*", index=frame.index, dtype=object)
        categories = frame["category"] if "category" in frame.columns else pd.Series(None, index=frame.index)
        times: dict[str, list[datetime]] = {}
        group: dict[str, str | None] = {}
        for k, d, c in zip(keys, frame["day"], categories, strict=True):
            if k is None or (isinstance(k, float) and math.isnan(k)):
                continue
            times.setdefault(str(k), []).append(from_days(float(d)))
            if isinstance(c, str):
                group.setdefault(str(k), c)
        pace = self.pace_of([from_days(float(d)) for d in frame["day"]], now)
        out: dict[str, CycleEstimate] = {}
        for k in sorted(times):
            est = self.estimate(k, times[k], now, group=group.get(k), pace=pace)
            if est is not None:
                out[k] = est
        return out

    def pace_of(self, purchase_times: Sequence[datetime], now: datetime) -> float:
        """A customer's order pace from all their purchase times (any item, at or before `now`).

        It is their shrunk mean log gap between orders minus the shop's: 0 for someone with a single
        order, negative for someone who orders more often than most.
        """
        now_day = to_days(now)
        index = sorted({math.floor(d) for d in (to_days(t) for t in purchase_times) if d <= now_day})
        logs = [math.log(b - a) for a, b in pairwise(index)]
        dev = _pace_from_stats(
            np.array([float(len(logs))]), np.array([float(np.mean(logs)) if logs else 0.0]), self.order_prior
        )
        return float(dev[0])

    def customer_paces(self, purchases: pd.DataFrame, now: datetime) -> pd.Series[Any]:
        """`pace_of` for every customer in a purchase table at once, indexed by customer_id."""
        rows = purchases[purchases["day"] <= to_days(now)]
        return _pace_series(trips(rows, None), self.order_prior)

    def score_trips(
        self,
        trip_table: pd.DataFrame,
        now: datetime,
        horizon_days: float = 14.0,
        pace: pd.Series[Any] | None = None,
    ) -> pd.DataFrame:
        """Vectorised `estimate` for every (customer, key) pair in a trip table (from `trips`).

        Trips after `now` are ignored. The result has one row per pair with the columns `customer_id`,
        `key`, `n_trips`, `n_own`, `last_day`, `elapsed`, `median`, `days_until_due`, `p` (the
        chance within `horizon_days`), `repeat_prob`, `own_weight`, `q10`, `q90`, `window_open`.
        `pace` maps customer_id (or a (customer_id, key) pair) to `pace_of`; missing ones count as
        typical. Backtests use it; it gives the same numbers as `estimate`.
        """
        now_day = to_days(now)
        t = trip_table[trip_table["day"] <= now_day]
        gaps = repeat_intervals(t)
        gaps["log_gap"] = np.log(gaps["interval_days"].to_numpy(dtype=float))
        # List aggregations (not named ones), so the result is a DataFrame for pandas-stubs as well.
        own = (
            gaps.groupby(["customer_id", "key"], sort=True)["log_gap"]
            .agg(["size", "mean"])
            .rename(columns={"size": "n_own", "mean": "mean_log"})
        )
        per = (
            t.groupby(["customer_id", "key"], sort=True)["day"]
            .agg(["size", "last"])
            .rename(columns={"size": "n_trips", "last": "last_day"})
        )
        per = per.join(own, how="left").reset_index()
        out = pd.DataFrame(
            {
                "customer_id": per["customer_id"],
                "key": per["key"],
                "n_trips": per["n_trips"].to_numpy(dtype=float),
                "n_own": per["n_own"].fillna(0.0).to_numpy(dtype=float),
            }
        )
        mean_log = per["mean_log"].fillna(0.0).to_numpy(dtype=float)
        priors = [self.prior(str(k)) for k in out["key"]]
        last = per["last_day"].to_numpy(dtype=float)
        elapsed = now_day - last
        pred = self._predict(
            mu0=np.array([p.mu for p in priors], dtype=float),
            sigma=np.array([p.sigma for p in priors], dtype=float),
            tau=np.array([p.tau for p in priors], dtype=float),
            pi1=np.array([p.repeat_prob for p in priors], dtype=float),
            n_trips=out["n_trips"].to_numpy(dtype=float),
            n_own=out["n_own"].to_numpy(dtype=float),
            mean_log=mean_log,
            elapsed=elapsed,
            horizon=horizon_days,
            pace=_align_pace(pace, out),
        )
        z10, z90 = _STD_NORMAL.inv_cdf(0.1), _STD_NORMAL.inv_cdf(0.9)
        out["last_day"] = last
        out["elapsed"] = elapsed
        out["median"] = pred.median
        out["days_until_due"] = pred.median - elapsed
        out["p"] = pred.p14
        out["repeat_prob"] = pred.repeat_prob
        out["own_weight"] = pred.own_weight
        out["q10"] = np.exp(pred.mu + z10 * pred.sigma)
        out["q90"] = np.exp(pred.mu + z90 * pred.sigma)
        out["window_open"] = (pred.window_start <= elapsed) & (elapsed <= pred.window_end)
        return out

    @staticmethod
    def _by_bucket(values: tuple[float, ...], n_trips: int) -> float:
        return values[min(max(n_trips, 1), len(values)) - 1]

    def _predict(
        self,
        *,
        mu0: FloatArray,
        sigma: FloatArray,
        tau: FloatArray,
        pi1: FloatArray,
        n_trips: FloatArray,
        n_own: FloatArray,
        mean_log: FloatArray,
        elapsed: FloatArray,
        pace: FloatArray,
        horizon: float = 14.0,
    ) -> _Predicted:
        """The shrinkage, the predictive spread, the repeat chance and the hazard, for many pairs."""
        prior_prec = 1.0 / np.square(tau)
        data_prec = n_own / np.square(sigma)
        prec = prior_prec + data_prec
        anchor = mu0 + self.pace_slope * pace
        mu = (anchor * prior_prec + mean_log * data_prec) / prec
        spread = np.sqrt(np.square(sigma) + 1.0 / prec)
        pi1c = np.clip(pi1, 1e-4, 1 - 1e-4)
        ratios = np.array(self.repeat_odds_ratios, dtype=float)
        bucket = np.clip(n_trips.astype(np.int64), 1, ratios.size) - 1
        pi = np.clip(_from_odds(pi1c / (1.0 - pi1c) * ratios[bucket]), 1e-4, 1 - 1e-4)
        s_now = _survival(elapsed, mu, spread)
        rates = np.array(self.background_rates, dtype=float)
        background = rates[np.clip(n_trips.astype(np.int64), 1, rates.size) - 1]
        b_now = np.exp(-background * np.maximum(elapsed, 0.0))
        still = pi * s_now / ((1.0 - pi) * b_now + pi * s_now)
        z_open = _STD_NORMAL.inv_cdf(self.config.window_open_quantile)
        z_close = _STD_NORMAL.inv_cdf(self.config.window_close_quantile)
        return _Predicted(
            mu=mu,
            sigma=spread,
            own_weight=data_prec / prec,
            repeat_prob=pi,
            still_likely=still,
            median=np.exp(mu),
            mean=np.exp(mu + np.square(spread) / 2.0),
            p14=_p_within(mu, spread, pi, elapsed, horizon, background),
            window_start=np.exp(mu + z_open * spread),
            window_end=np.exp(mu + z_close * spread),
        )


def _key_groups(frame: pd.DataFrame, level: Level, catalogue: Catalogue | None) -> dict[str, str]:
    """Each item's category (catalogue first, then the events' most common one). Items only."""
    if level != "item":
        return {}
    out: dict[str, str] = {}
    rows = frame[frame["item_id"].notna() & frame["category"].notna()]
    if not rows.empty:
        counts = (
            pd.DataFrame({"item": rows["item_id"].astype(str), "category": rows["category"].astype(str)})
            .value_counts()
            .reset_index(name="n")
            .sort_values(["item", "n", "category"], ascending=[True, False, True], kind="mergesort")
        )
        for item, cat in zip(counts["item"], counts["category"], strict=True):
            out.setdefault(str(item), str(cat))
    if catalogue is not None:
        for p in catalogue:
            if p.category is not None:
                out[p.item_id] = p.category
    return dict(sorted(out.items()))


def _order_prior(order_trips: pd.DataFrame, default: IntervalPrior, config: CycleConfig) -> IntervalPrior:
    """The shop's order cadence (gaps between any two orders), the anchor for customer pace."""
    gaps = repeat_intervals(order_trips)
    logs = np.log(gaps["interval_days"].to_numpy(dtype=float))
    pair = pd.DataFrame({"customer_id": gaps["customer_id"], "log_gap": logs})
    stats = pair.groupby("customer_id", sort=True)["log_gap"].agg(["size", "mean", "var"])
    stats["ss"] = stats["var"].fillna(0.0) * (stats["size"] - 1)
    empty = pd.DataFrame({"eligible": [], "repeated": []})
    return _shrink(_raw(stats, empty), default, config, config.parent_strength, "shop")


def _pace_from_stats(n: FloatArray, mean_log: FloatArray, prior: IntervalPrior) -> FloatArray:
    """The shrunk deviation of a customer's mean log order gap from the shop's (0 with no gaps)."""
    data_prec = n / prior.sigma**2
    weight = data_prec / (1.0 / prior.tau**2 + data_prec)
    out: FloatArray = np.where(n > 0, weight * (mean_log - prior.mu), 0.0)
    return out


def _pace_series(order_trips: pd.DataFrame, prior: IntervalPrior) -> pd.Series[Any]:
    """`_pace_from_stats` for every customer in an order-level trip table, indexed by customer_id."""
    gaps = repeat_intervals(order_trips)
    logs = pd.Series(np.log(gaps["interval_days"].to_numpy(dtype=float)), index=gaps.index)
    stats = logs.groupby(gaps["customer_id"], sort=True).agg(["size", "mean"])
    dev = _pace_from_stats(stats["size"].to_numpy(dtype=float), stats["mean"].to_numpy(dtype=float), prior)
    customers = sorted(set(order_trips["customer_id"]))
    return pd.Series(dev, index=stats.index, dtype=float).reindex(customers, fill_value=0.0)


def _pace_slope(pair: pd.DataFrame, d: FloatArray, cap: float) -> float:
    """How far an item's log gap moves per unit of customer pace, within items (least squares)."""
    y = pair["mean"].to_numpy(dtype=float)
    keys = pair["key"].to_numpy()
    y_c = y - pd.Series(y).groupby(keys).transform("mean").to_numpy(dtype=float)
    x_c = d - pd.Series(d).groupby(keys).transform("mean").to_numpy(dtype=float)
    sxx = float(np.dot(x_c, x_c))
    if sxx <= 1e-12:
        return 0.0
    return float(min(max(float(np.dot(x_c, y_c)) / sxx, 0.0), cap))


def _odds_ratios(rep: pd.DataFrame, config: CycleConfig) -> tuple[float, ...]:
    """How much likelier another purchase is after 2, 3, ... trips than after the first one.

    Each bucket's repeat rate is shrunk toward the bucket before it, and the ratios never fall: a
    customer who has bought something five times is not less likely to buy it again than after four.
    """
    buckets = np.clip(rep["trip_no"].to_numpy(dtype=np.int64), 1, config.trip_buckets)
    eligible = np.bincount(
        buckets, weights=rep["eligible"].to_numpy(dtype=float), minlength=config.trip_buckets + 1
    )
    repeated = np.bincount(
        buckets, weights=rep["repeated"].to_numpy(dtype=float), minlength=config.trip_buckets + 1
    )
    rs = config.repeat_strength
    rates: list[float] = []
    target = config.default_repeat_prob
    for b in range(1, config.trip_buckets + 1):
        rate = (repeated[b] + rs * target) / (eligible[b] + rs)
        rate = min(max(rate, 1e-4), 1 - 1e-4)
        rates.append(rate)
        target = float(_from_odds(np.array([_odds(rate) * config.default_repeat_odds_ratio]))[0])
    ratios = [_odds(r) / _odds(rates[0]) for r in rates]
    return tuple(float(x) for x in np.maximum.accumulate(np.array(ratios)))


def _background_rates(trip_table: pd.DataFrame, as_of_day: float, config: CycleConfig) -> tuple[float, ...]:
    """The daily rate of off-cycle rebuys (more than `repeat_horizon_days` after a purchase).

    Events over exposure among trips old enough to have passed that horizon without a rebuy, by the
    same trip-count buckets as the repeat odds ratios: someone who bought a thing once comes back to
    it late far less often than a regular does. The shop-wide rate is shrunk toward
    `default_background_rate`, and each bucket toward the shop-wide rate, with
    `background_strength_days` of pseudo-exposure.
    """
    horizon = config.repeat_horizon_days
    grouped = trip_table.groupby(["customer_id", "key"], sort=False)
    nxt = grouped["day"].shift(-1).to_numpy(dtype=float)
    trip_no = (grouped.cumcount() + 1).to_numpy(dtype=np.int64)
    day = trip_table["day"].to_numpy(dtype=float)
    old = day <= as_of_day - horizon
    late = old & ~(nxt - day <= horizon)  # NaN (no rebuy yet) counts as still at risk
    end = np.where(np.isnan(nxt), as_of_day, np.minimum(nxt, as_of_day))
    exposure = np.where(late, np.clip(end - day - horizon, 0.0, None), 0.0)
    events = (late & ~np.isnan(nxt) & (nxt <= as_of_day)).astype(float)
    prior_days = config.background_strength_days
    overall = (float(events.sum()) + config.default_background_rate * prior_days) / (
        float(exposure.sum()) + prior_days
    )
    bucket = np.clip(trip_no, 1, config.trip_buckets)
    ev = np.bincount(bucket, weights=events, minlength=config.trip_buckets + 1)
    ex = np.bincount(bucket, weights=exposure, minlength=config.trip_buckets + 1)
    return tuple(
        float((ev[b] + overall * prior_days) / (ex[b] + prior_days))
        for b in range(1, config.trip_buckets + 1)
    )


def _repeat_counts(trip_table: pd.DataFrame, as_of_day: float, horizon: float) -> pd.DataFrame:
    """Per (key, trip number): how many trips had `horizon` days to be followed, and how many were
    followed within it (on-cycle rebuys; later ones are the background rate's business)."""
    t = trip_table.copy()
    t["trip_no"] = t.groupby(["customer_id", "key"], sort=False).cumcount() + 1
    nxt = t.groupby(["customer_id", "key"], sort=False)["day"].shift(-1)
    t["eligible"] = t["day"] <= as_of_day - horizon
    t["followed"] = (nxt - t["day"] <= horizon).fillna(False).astype(bool) & t["eligible"]
    t = t[t["eligible"]]
    agg = t.groupby(["key", "trip_no"], sort=True).agg(
        eligible=("eligible", "size"), repeated=("followed", "sum")
    )
    return agg.reset_index()


def _raw(pair: pd.DataFrame, first: pd.DataFrame) -> _Raw:
    """Sufficient statistics from per-pair gap summaries and first-trip repeat counts."""
    n = pair["size"].to_numpy(dtype=float)
    means = pair["mean"].to_numpy(dtype=float)
    df = int((n - 1).sum()) if n.size else 0
    return _Raw(
        n_pairs=int(n.size),
        n_intervals=int(n.sum()) if n.size else 0,
        mean_of_means=float(means.mean()) if n.size else 0.0,
        ss_within=float(pair["ss"].sum()) if n.size else 0.0,
        df_within=df,
        var_of_means=float(means.var(ddof=1)) if n.size >= 2 else 0.0,
        mean_inv_n=float((1.0 / n).mean()) if n.size else 0.0,
        eligible=int(first["eligible"].sum()) if not first.empty else 0,
        repeated=int(first["repeated"].sum()) if not first.empty else 0,
    )


def _shrink(
    raw: _Raw, parent: IntervalPrior, config: CycleConfig, strength: float, source: str
) -> IntervalPrior:
    """Mix one level's own statistics with its parent's, each weighted by its evidence."""
    mu = (raw.n_pairs * raw.mean_of_means + strength * parent.mu) / (raw.n_pairs + strength)
    vs = config.variance_strength
    sigma2 = (raw.ss_within + vs * parent.sigma**2) / (raw.df_within + vs)
    sigma = max(math.sqrt(sigma2), config.min_sigma)
    df_tau = max(raw.n_pairs - 1, 0)
    raw_tau2 = max(raw.var_of_means - sigma**2 * raw.mean_inv_n, 0.0) if df_tau > 0 else 0.0
    tau2 = (df_tau * raw_tau2 + vs * parent.tau**2) / (df_tau + vs)
    tau = max(math.sqrt(tau2), config.min_tau)
    rs = config.repeat_strength
    repeat = (raw.repeated + rs * parent.repeat_prob) / (raw.eligible + rs)
    return IntervalPrior(
        mu=mu,
        sigma=sigma,
        tau=tau,
        repeat_prob=min(max(repeat, 1e-4), 1 - 1e-4),
        n_pairs=raw.n_pairs,
        n_intervals=raw.n_intervals,
        source=source if raw.n_pairs or raw.eligible else parent.source,
    )


# ------------------------------------------------------------------------------------- validation
@dataclass(frozen=True)
class HoldoutSlice:
    """Errors for the held-out pairs with a given number of own gaps before the held-out one."""

    own_intervals: str
    n: int
    mae_model: float
    mae_naive: float
    mae_prior_only: float
    mae_own_only: float | None


@dataclass(frozen=True)
class HoldoutReport:
    """Hold out each repeat pair's last purchase and predict its gap from the earlier ones.

    `naive` is the population's average gap (the arithmetic mean of every training gap) for
    everybody; `naive_median` the population's median gap; `prior_only` the model's population cycle
    for the key with no customer data; `own_only` the customer's own mean gap without shrinkage
    (only where they have one). Errors are absolute errors in days of the predicted gap, which is the
    error of the predicted date. `coverage_80` is the share of held-out gaps inside the model's
    10th-90th percentile range.
    """

    level: str
    n_pairs: int
    naive_interval_days: float
    mae_model: float
    mae_naive: float
    mae_naive_median: float
    mae_prior_only: float
    median_ae_model: float
    median_ae_naive: float
    coverage_80: float
    slices: tuple[HoldoutSlice, ...]


def backtest_last_interval(
    purchases: pd.DataFrame,
    *,
    level: Level = "item",
    catalogue: Catalogue | None = None,
    config: CycleConfig = DEFAULT_CONFIG,
) -> HoldoutReport:
    """The refill question asked of the past: from someone's earlier purchases, when was the last one?

    Every (customer, key) pair with at least two trips has its last trip held out. The model is fit
    on everything else (so the held-out gaps are never seen), each pair is predicted from what was
    known at its second-to-last trip (its own earlier trips, and its order pace up to that day), and
    the predicted median gap is compared with the real one. Other customers' gaps used for the
    population cycle may fall later in calendar time than a held-out purchase; the strictly
    forward-in-time check is `calibrate_at_cutoff`.
    """
    column = LEVEL_KEY[level]
    trip_table = trips(purchases, column)
    trip_table["trip_no"] = trip_table.groupby(["customer_id", "key"], sort=False).cumcount() + 1
    count = trip_table.groupby(["customer_id", "key"], sort=False)["day"].transform("size")
    is_last = (trip_table["trip_no"] == count) & (count >= 2)
    held = trip_table[is_last]
    train_trips = trip_table[~is_last]
    if held.empty:
        nan = math.nan
        return HoldoutReport(level, 0, nan, nan, nan, nan, nan, nan, nan, nan, ())

    keep_rows = _rows_of_trips(purchases, train_trips, column)
    as_of = from_days(float(purchases["day"].max()) + 1.0)
    model = CycleModel.fit(keep_rows, as_of, level=level, catalogue=catalogue, config=config)

    # The predicted gap does not depend on `now`, so every pair is scored once, after all its trips.
    prev = train_trips.merge(held[["customer_id", "key"]], on=["customer_id", "key"])
    prev_last = prev.groupby(["customer_id", "key"], sort=True)["day_index"].max().rename("prev_index")
    pace = _pace_asof(trips(keep_rows, None), prev_last, model.order_prior)
    scored = model.score_trips(prev, as_of, pace=pace)
    actual = held.set_index(["customer_id", "key"])["day_index"].rename("held_index")
    scored = scored.join(prev_last, on=["customer_id", "key"]).join(actual, on=["customer_id", "key"])
    gap = (scored["held_index"] - scored["prev_index"]).to_numpy(dtype=float)

    pred = scored["median"].to_numpy(dtype=float)
    priors = np.array([model.prior(str(k)).median_days for k in scored["key"]], dtype=float)
    own_mean = _own_mean_gap(prev)
    own = scored.join(own_mean, on=["customer_id", "key"])["own_mean"].to_numpy(dtype=float)
    naive = model.mean_interval_days
    naive_median = model.median_interval_days
    err = np.abs(pred - gap)
    err_naive = np.abs(naive - gap)
    covered = (scored["q10"].to_numpy(dtype=float) <= gap) & (gap <= scored["q90"].to_numpy(dtype=float))

    n_own = scored["n_own"].to_numpy(dtype=float)
    buckets = np.where(n_own >= 3, "3+", n_own.astype(int).astype(str))
    slices: list[HoldoutSlice] = []
    for b in ("0", "1", "2", "3+"):
        m = buckets == b
        if not m.any():
            continue
        has_own = m & np.isfinite(own)
        slices.append(
            HoldoutSlice(
                own_intervals=b,
                n=int(m.sum()),
                mae_model=float(err[m].mean()),
                mae_naive=float(err_naive[m].mean()),
                mae_prior_only=float(np.abs(priors[m] - gap[m]).mean()),
                mae_own_only=float(np.abs(own[has_own] - gap[has_own]).mean()) if has_own.any() else None,
            )
        )
    return HoldoutReport(
        level=level,
        n_pairs=int(gap.size),
        naive_interval_days=naive,
        mae_model=float(err.mean()) if gap.size else math.nan,
        mae_naive=float(err_naive.mean()) if gap.size else math.nan,
        mae_naive_median=float(np.abs(naive_median - gap).mean()) if gap.size else math.nan,
        mae_prior_only=float(np.abs(priors - gap).mean()) if gap.size else math.nan,
        median_ae_model=float(np.median(err)) if gap.size else math.nan,
        median_ae_naive=float(np.median(err_naive)) if gap.size else math.nan,
        coverage_80=float(covered.mean()) if gap.size else math.nan,
        slices=tuple(slices),
    )


def _align_pace(pace: pd.Series[Any] | None, out: pd.DataFrame) -> FloatArray:
    """Pace per scored row, from a Series keyed by customer_id or by (customer_id, key)."""
    if pace is None:
        return np.zeros(len(out))
    if isinstance(pace.index, pd.MultiIndex):
        idx = pd.MultiIndex.from_arrays([out["customer_id"], out["key"]])
        by_pair: FloatArray = pace.reindex(idx).fillna(0.0).to_numpy(dtype=float)
        return by_pair
    by_customer: FloatArray = out["customer_id"].map(pace).fillna(0.0).to_numpy(dtype=float)
    return by_customer


def _pace_asof(order_trips: pd.DataFrame, moment: pd.Series[Any], prior: IntervalPrior) -> pd.Series[Any]:
    """Each pair's customer pace from the orders up to and including day `moment[(customer, key)]`."""
    gaps = order_trips[["customer_id", "day_index"]].copy()
    prev = gaps.groupby("customer_id", sort=False)["day_index"].shift()
    logs = np.log(np.where(prev.notna(), gaps["day_index"] - prev.fillna(0), 1.0).astype(float))
    gaps["log"] = np.where(prev.notna(), logs, 0.0)
    gaps["n"] = np.where(prev.notna(), 1.0, 0.0)
    gaps["cum_log"] = gaps.groupby("customer_id", sort=False)["log"].cumsum()
    gaps["cum_n"] = gaps.groupby("customer_id", sort=False)["n"].cumsum()
    left = moment.rename("day_index").reset_index().sort_values("day_index", kind="mergesort")
    right = gaps.sort_values("day_index", kind="mergesort")[["customer_id", "day_index", "cum_log", "cum_n"]]
    merged = pd.merge_asof(left, right, on="day_index", by="customer_id", direction="backward")
    n = merged["cum_n"].fillna(0.0).to_numpy(dtype=float)
    mean_log = np.where(n > 0, merged["cum_log"].fillna(0.0).to_numpy(dtype=float) / np.maximum(n, 1.0), 0.0)
    dev = _pace_from_stats(n, mean_log, prior)
    index = pd.MultiIndex.from_arrays([merged["customer_id"], merged["key"]])
    return pd.Series(dev, index=index, dtype=float).sort_index()


def _rows_of_trips(purchases: pd.DataFrame, kept: pd.DataFrame, column: str | None) -> pd.DataFrame:
    """The purchase rows that belong to the kept trips (same customer, key and UTC day)."""
    key = purchases[column].astype(str) if column is not None else pd.Series("*", index=purchases.index)
    idx = pd.MultiIndex.from_arrays(
        [
            purchases["customer_id"].astype(str),
            key,
            np.floor(purchases["day"].to_numpy(dtype=float)).astype(np.int64),
        ]
    )
    keep = pd.MultiIndex.from_arrays([kept["customer_id"], kept["key"], kept["day_index"]])
    mask = idx.isin(keep)
    if column is not None:
        mask = mask & purchases[column].notna().to_numpy(dtype=bool)
    return purchases[mask]


def _own_mean_gap(trip_table: pd.DataFrame) -> pd.Series[Any]:
    gaps = repeat_intervals(trip_table)
    return gaps.groupby(["customer_id", "key"], sort=True)["interval_days"].mean().rename("own_mean")


@dataclass(frozen=True)
class ScoreQuality:
    """How good a set of probabilities is: lower Brier and log loss are better, higher AUC is."""

    brier: float
    log_loss: float
    auc: float
    ece: float
    mean_p: float


@dataclass(frozen=True)
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_p: float
    observed: float


@dataclass(frozen=True)
class CalibrationReport:
    """`p14` scored forward in time: fit before `cutoff`, then did each pair buy in the next 14 days?

    Every (customer, key) pair with a purchase before the cutoff is scored, including the many that
    never buy again, because that is the population `decide()` will read the score for. `naive` is a
    "population average interval" rule with no customer or item information: the shop's repeat rate
    times a constant hazard of one purchase per average gap, `repeat_prob * (1 - exp(-14 / mean gap))`.
    Bins are equal-count bins of the model's `p14`.
    """

    level: str
    cutoff: datetime
    horizon_days: float
    n_pairs: int
    base_rate: float
    model: ScoreQuality
    naive: ScoreQuality
    naive_p: float
    bins: tuple[CalibrationBin, ...]
    window_open_n: int
    window_open_rate: float
    window_closed_rate: float


def calibrate_at_cutoff(
    purchases: pd.DataFrame,
    cutoff: datetime,
    *,
    level: Level = "item",
    horizon_days: float = 14.0,
    catalogue: Catalogue | None = None,
    config: CycleConfig = DEFAULT_CONFIG,
    n_bins: int = 10,
) -> CalibrationReport:
    """Fit on purchases before `cutoff`, score every pair at `cutoff`, compare with what happened."""
    cut = to_days(cutoff)
    before = purchases[purchases["day"] < cut]
    model = CycleModel.fit(before, cutoff, level=level, catalogue=catalogue, config=config)
    column = LEVEL_KEY[level]
    pace = model.customer_paces(before, cutoff)
    scored = model.score_trips(trips(before, column), cutoff, horizon_days, pace=pace)
    after = trips(purchases[(purchases["day"] >= cut) & (purchases["day"] < cut + horizon_days)], column)
    bought = after[["customer_id", "key"]].drop_duplicates()
    bought["y"] = 1.0
    scored = scored.merge(bought, on=["customer_id", "key"], how="left")
    y = scored["y"].fillna(0.0).to_numpy(dtype=float)
    p = scored["p"].to_numpy(dtype=float)
    naive_p = model.shop.repeat_prob * (1.0 - math.exp(-horizon_days / model.mean_interval_days))
    window = scored["window_open"].to_numpy(dtype=bool)
    return CalibrationReport(
        level=level,
        cutoff=cutoff,
        horizon_days=horizon_days,
        n_pairs=int(y.size),
        base_rate=float(y.mean()) if y.size else math.nan,
        model=_quality(p, y, n_bins),
        naive=_quality(np.full_like(y, naive_p), y, n_bins),
        naive_p=naive_p,
        bins=_bins(p, y, n_bins),
        window_open_n=int(window.sum()),
        window_open_rate=float(y[window].mean()) if window.any() else math.nan,
        window_closed_rate=float(y[~window].mean()) if (~window).any() else math.nan,
    )


def _quality(p: FloatArray, y: FloatArray, n_bins: int) -> ScoreQuality:
    if y.size == 0:
        return ScoreQuality(math.nan, math.nan, math.nan, math.nan, math.nan)
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    log_loss = float(-(y * np.log(pc) + (1 - y) * np.log(1 - pc)).mean())
    bins = _bins(p, y, n_bins)
    ece = sum(b.n * abs(b.mean_p - b.observed) for b in bins) / y.size
    return ScoreQuality(
        brier=float(np.square(p - y).mean()),
        log_loss=log_loss,
        auc=_auc(p, y),
        ece=float(ece),
        mean_p=float(p.mean()),
    )


def _auc(p: FloatArray, y: FloatArray) -> float:
    """The chance a random buyer is ranked above a random non-buyer (ties count half)."""
    pos = int(y.sum())
    neg = int(y.size - pos)
    if pos == 0 or neg == 0:
        return math.nan
    ranks = pd.Series(p).rank(method="average").to_numpy(dtype=float)
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def _bins(p: FloatArray, y: FloatArray, n_bins: int) -> tuple[CalibrationBin, ...]:
    if y.size == 0:
        return ()
    order = np.argsort(p, kind="mergesort")
    out: list[CalibrationBin] = []
    for chunk in np.array_split(order, min(n_bins, y.size)):
        if chunk.size == 0:
            continue
        out.append(
            CalibrationBin(
                lo=float(p[chunk].min()),
                hi=float(p[chunk].max()),
                n=int(chunk.size),
                mean_p=float(p[chunk].mean()),
                observed=float(y[chunk].mean()),
            )
        )
    return tuple(out)


# ------------------------------------------------------------------------------------- the report
VALIDATION_CUTOFFS: Final[tuple[datetime, ...]] = (
    datetime(2011, 9, 1, tzinfo=UTC),
    datetime(2011, 10, 1, tzinfo=UTC),
    datetime(2011, 11, 1, tzinfo=UTC),
)
"""Forward-in-time cutoffs for the `p14` calibration on the online-retail log (one year, 2010-11)."""

_REPORT_LEVELS: Final[tuple[Level, ...]] = ("item", "order")


def retail_log_purchases(log: pd.DataFrame) -> pd.DataFrame:
    """The raw online-retail log as a purchase table (`PURCHASE_COLUMNS`), sorted by customer and time.

    The cleaning is `autonomy.catalog.products.clean_retail_lines` (real product sales by known
    customers only); `value` is quantity times unit price. The log has no categories.
    """
    lines = clean_retail_lines(log)
    day = (lines["ts"] - pd.Timestamp(EPOCH)) / pd.Timedelta(days=1)
    frame = pd.DataFrame(
        {
            "customer_id": lines["customer_id"],
            "item_id": lines["item_id"],
            "category": None,
            "day": day.astype(float),
            "value": (lines["quantity"] * lines["price"]).astype(float),
            "quantity": lines["quantity"],
        }
    )
    return frame.sort_values(["customer_id", "day"], kind="mergesort").reset_index(drop=True)


def _f(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:,.{digits}f}"


def _pct(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{100 * x:.1f}%"


def validation_markdown(log: pd.DataFrame | None, sample: pd.DataFrame) -> str:
    """The refill-cycle validation as Markdown: the raw log (when fetched) and the committed sample."""
    out: list[str] = []
    if log is not None:
        out.append(
            f"Raw log: {len(log):,} product lines, {log['customer_id'].nunique():,} customers, "
            f"{log['item_id'].nunique():,} products.\n"
        )
        out.append(
            "**Timing: hold out each repeat pair's last purchase** (MAE in days of the predicted gap)\n"
        )
        out.append(
            "| level | pairs | model | naive: average gap | naive: median gap | item cycle only "
            "| median AE model / naive | 80% interval coverage |\n|---|---:|---:|---:|---:|---:|---:|---:|"
        )
        holdouts = {lv: backtest_last_interval(log, level=lv) for lv in _REPORT_LEVELS}
        for lv, r in holdouts.items():
            out.append(
                f"| {lv} | {r.n_pairs:,} | **{_f(r.mae_model)}** | {_f(r.mae_naive)} "
                f"({_f(r.naive_interval_days)} d) | {_f(r.mae_naive_median)} | {_f(r.mae_prior_only)} | "
                f"{_f(r.median_ae_model)} / {_f(r.median_ae_naive)} | {_pct(r.coverage_80)} |"
            )
        out.append("\n**Item level, by how many own gaps the customer had before the held-out one**\n")
        out.append("| own gaps | pairs | model | naive (average) | item cycle only | own mean only |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for s in holdouts["item"].slices:
            out.append(
                f"| {s.own_intervals} | {s.n:,} | **{_f(s.mae_model)}** | {_f(s.mae_naive)} | "
                f"{_f(s.mae_prior_only)} | {_f(s.mae_own_only)} |"
            )
        out.append(
            "\n**`p14`, forward in time: fit before the cutoff, did the pair buy in the next 14 days?**\n"
        )
        out.append(
            "| level | cutoff | pairs | bought | Brier model / naive | log loss model / naive | AUC "
            "| ECE model / naive | rate, window open / closed |\n|---|---|---:|---:|---:|---:|---:|---:|---:|"
        )
        bins: tuple[CalibrationBin, ...] = ()
        for lv in _REPORT_LEVELS:
            for cut in VALIDATION_CUTOFFS:
                c = calibrate_at_cutoff(log, cut, level=lv)
                if lv == "item" and cut == VALIDATION_CUTOFFS[0]:
                    bins = c.bins
                out.append(
                    f"| {lv} | {cut.date()} | {c.n_pairs:,} | {_pct(c.base_rate)} | "
                    f"{_f(c.model.brier, 4)} / {_f(c.naive.brier, 4)} | "
                    f"{_f(c.model.log_loss, 3)} / {_f(c.naive.log_loss, 3)} | {_f(c.model.auc, 3)} | "
                    f"{_f(c.model.ece, 4)} / {_f(c.naive.ece, 4)} | "
                    f"{_pct(c.window_open_rate)} / {_pct(c.window_closed_rate)} |"
                )
        out.append(
            f"\n**Reliability, item level, cutoff {VALIDATION_CUTOFFS[0].date()}** (ten equal-count bins of `p14`)\n"
        )
        out.append("| mean `p14` | bought | pairs |\n|---:|---:|---:|")
        for b in bins:
            out.append(f"| {_pct(b.mean_p)} | {_pct(b.observed)} | {b.n:,} |")
    r = backtest_last_interval(sample, level="order")
    first = next((s for s in r.slices if s.own_intervals == "0"), None)
    out.append(
        f"\n**Committed `sample.csv`** (order level; no product codes): {r.n_pairs:,} repeat customers, "
        f"MAE {_f(r.mae_model)} days vs {_f(r.mae_naive)} for the average gap. Its in-between order "
        "dates are spaced evenly by the loader, so a customer's own gaps predict the held-out one almost "
        "exactly; only the customers with two orders have a fully real gap"
        + (
            f": {first.n:,} of them, MAE {_f(first.mae_model)} vs {_f(first.mae_naive)}."
            if first is not None
            else "."
        )
    )
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the validation: `python -m autonomy.catalog.cycle` (fetch the raw log first for all of it)."""
    import argparse
    from pathlib import Path

    from autonomy.data.loaders import ONLINE_RETAIL_LOG, load_online_retail_sample, read_online_retail_log

    parser = argparse.ArgumentParser(description="Validate the refill-cycle model on online-retail.")
    parser.add_argument("--log", type=Path, default=ONLINE_RETAIL_LOG, help="the raw online-retail log")
    args = parser.parse_args(argv)
    log = retail_log_purchases(read_online_retail_log(args.log)) if args.log.exists() else None
    sample = purchases_frame(load_online_retail_sample().events)
    print(validation_markdown(log, sample))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
