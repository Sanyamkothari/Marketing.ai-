"""Complementary-product recommendations from purchase events, and the next best item for a customer.

The vision's hero example ends with "recommend a complementary product". This module learns which
products go together from nothing but purchase history: two products are related when the same
customers buy them on the same *shopping trip*, i.e. in one basket or within a short window of each
other (a serum this week and a moisturiser next week is one routine, not two unrelated orders).

How it works, in plain terms:

1. **Trips.** A customer's baskets are grouped greedily: a trip starts with a basket and takes in
   every later basket that falls within `window` of that first one. Each trip is a set of items.
2. **Counts.** For every pair of items we count the trips that contain both (`c_ab`), and for every
   item the trips that contain it (`c_a`). Trips with more than `max_trip_items` items (wholesale
   orders) are left out of the pair counts: they relate everything to everything and drown the signal.
3. **Similarity.** Cosine, `c_ab / sqrt(c_a * c_b)` (the default), or lift, `c_ab * N / (c_a * c_b)`.
   Pairs seen on fewer than `min_pair_count` trips are dropped; each item keeps its best `neighbours`.
4. **Scoring a customer.** Every item they own votes for its neighbours, weighted by how recently
   they bought it (a half-life), so last week's serum matters more than last year's candle. The
   item that contributed most becomes the plain-English reason: "Often bought with your Vitamin C serum".
5. **Fallback.** When co-purchase has too little (a new customer, a rare item), we fill with what
   is popular right now: first in the customer's own categories, then anywhere. The three tiers
   never overlap, so every co-purchase suggestion outranks every popular one before diversity.
6. **Diversity.** When categories are known, each repeat of a category multiplies the next item's
   score by `diversity`, so five moisturisers do not crowd out the one cleanser.

Everything is counts and sorting: no randomness, no clock (time is always passed in), no LLM. The
fit is vectorised with numpy and pandas, and a single customer's recommendation is a few array
operations, so scoring thousands of customers takes seconds.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, NamedTuple, Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import Event, EventType

__all__ = [
    "BASKET_COLUMNS",
    "CoPurchaseConfig",
    "CoPurchaseModel",
    "NextItem",
    "Recommendation",
    "RefillWindow",
    "RefillWindowInfo",
    "basket_frame",
    "next_best_item",
    "recommend",
]

BASKET_COLUMNS: tuple[str, ...] = ("customer_id", "item_id", "t", "category", "basket_id")
"""The basket table the model fits on: one row per (customer, item, basket); `t` is Unix seconds.

It differs from `autonomy.catalog.products.PURCHASE_COLUMNS` (days since the epoch, with value and
quantity, no basket) because pairs need baskets and exact times, while cycles need days and value.
"""

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]


class Recommendation(NamedTuple):
    """One suggested product. A plain tuple, so `item_id, score, reason = rec` works."""

    item_id: str
    score: float
    reason: str


@dataclass(frozen=True)
class CoPurchaseConfig:
    """The knobs, with defaults chosen on the online-retail log (see docs/v2/CATALOG.md)."""

    window: timedelta = timedelta(days=30)
    """Baskets this close to a trip's first basket belong to the same trip."""
    metric: Literal["cosine", "lift"] = "cosine"
    min_pair_count: int = 2
    """A pair must be seen on at least this many trips; one coincidence is not a pattern."""
    neighbours: int = 50
    """How many related items each item keeps."""
    max_trip_items: int = 100
    """Trips with more distinct items than this are left out of the pair counts (wholesale orders)."""
    half_life: timedelta = timedelta(days=90)
    """An owned item's vote halves for every `half_life` since it was last bought."""
    popularity_window: timedelta = timedelta(days=90)
    """Popularity, for the fallback, counts distinct buyers in this window before the fit's `as_of`."""
    diversity: float = 0.5
    """Each repeat of a category multiplies the next item's score by this; 1.0 turns diversity off."""

    def __post_init__(self) -> None:
        if self.window < timedelta(0):
            raise ValueError("window cannot be negative")
        if self.min_pair_count < 1 or self.neighbours < 1 or self.max_trip_items < 2:
            raise ValueError("min_pair_count and neighbours must be at least 1, max_trip_items at least 2")
        if self.half_life <= timedelta(0) or self.popularity_window <= timedelta(0):
            raise ValueError("half_life and popularity_window must be positive")
        if not 0.0 < self.diversity <= 1.0:
            raise ValueError("diversity is a multiplier in (0, 1]")


def _seconds(ts: datetime) -> int:
    if ts.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return int(ts.timestamp())


def basket_frame(events: Iterable[Event]) -> pd.DataFrame:
    """The basket table from events: PURCHASE events that name an `item_id`, nothing else.

    The basket is the event's `attrs["invoice_no"]` when the source has one (online retail does),
    otherwise its exact time in Unix seconds, since lines of one order share a time. (Seconds, not
    the ISO string: one instant written with two UTC offsets is still one basket.)
    """
    rows: list[tuple[str, str, int, str | None, str]] = []
    for e in events:
        if e.type is not EventType.PURCHASE or e.item_id is None:
            continue
        ts = _seconds(e.ts)
        invoice = e.attrs.get("invoice_no")
        basket = str(invoice) if invoice is not None else str(ts)
        rows.append((e.customer_id, e.item_id, ts, e.category, basket))
    return pd.DataFrame(rows, columns=list(BASKET_COLUMNS))


def _check_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in ("customer_id", "item_id", "t") if c not in frame.columns]
    if missing:
        raise ValueError(f"the basket table lacks {missing}; build it with basket_frame")
    out = frame.copy()
    if "category" not in out.columns:
        out["category"] = None
    if "basket_id" not in out.columns:
        out["basket_id"] = out["t"].astype(str)
    out["customer_id"] = out["customer_id"].astype(str)
    out["item_id"] = out["item_id"].astype(str)
    out["basket_id"] = out["basket_id"].astype(str)
    out["t"] = out["t"].astype(np.int64)
    return out


def _trips(customer: IntArray, basket: IntArray, t: IntArray, window: int) -> IntArray:
    """A trip id per line: baskets within `window` seconds of their trip's first basket share one."""
    baskets = (
        pd.DataFrame({"c": customer, "b": basket, "t": t})
        .groupby(["c", "b"], sort=False)["t"]
        .min()
        .reset_index()
        .sort_values(["c", "t", "b"], kind="mergesort")
    )
    bc = baskets["c"].to_numpy(dtype=np.int64)
    bt = baskets["t"].to_numpy(dtype=np.int64)
    trip = np.empty(len(baskets), dtype=np.int64)
    current, last_c, start = -1, -1, 0
    for i in range(len(baskets)):
        c, ts = int(bc[i]), int(bt[i])
        if c != last_c or ts - start > window:
            current += 1
            last_c, start = c, ts
        trip[i] = current
    baskets["trip"] = trip
    lines = pd.DataFrame({"c": customer, "b": basket})
    merged = lines.merge(baskets[["c", "b", "trip"]], on=["c", "b"], how="left", sort=False)
    return merged["trip"].to_numpy(dtype=np.int64)


@dataclass(frozen=True, eq=False)
class _Neighbours:
    """Each item's related items, best first, stored flat: item a's run is `[ptr[a], ptr[a + 1])`."""

    ptr: IntArray
    item: IntArray
    score: FloatArray

    def of(self, a: int) -> tuple[IntArray, FloatArray]:
        lo, hi = int(self.ptr[a]), int(self.ptr[a + 1])
        return self.item[lo:hi], self.score[lo:hi]


def _similarity(
    trip: IntArray, item: IntArray, n_items: int, config: CoPurchaseConfig
) -> tuple[_Neighbours, IntArray, int]:
    """Neighbour lists, per-item trip counts and the number of trips counted, from (trip, item) pairs."""
    pairs = pd.DataFrame({"trip": trip, "item": item}).drop_duplicates()
    size = pairs.groupby("trip")["item"].transform("size").to_numpy(dtype=np.int64)
    counted = pairs[size <= config.max_trip_items]
    n_trips = int(counted["trip"].nunique())
    item_trips = np.bincount(counted["item"].to_numpy(dtype=np.int64), minlength=n_items).astype(np.int64)

    multi = counted[counted.groupby("trip")["item"].transform("size").to_numpy(dtype=np.int64) >= 2]
    joined = multi.merge(multi, on="trip", suffixes=("_a", "_b"))
    a = joined["item_a"].to_numpy(dtype=np.int64)
    b = joined["item_b"].to_numpy(dtype=np.int64)
    keep = a < b
    key = a[keep] * n_items + b[keep]
    pair_keys, pair_counts = np.unique(key, return_counts=True)
    strong = pair_counts >= config.min_pair_count
    uniq = pair_keys[strong].astype(np.int64)
    c_ab = pair_counts[strong].astype(np.float64)
    pa, pb = uniq // n_items, uniq % n_items
    ca, cb = item_trips[pa].astype(np.float64), item_trips[pb].astype(np.float64)
    lift = config.metric == "lift"
    sim: FloatArray = c_ab * float(n_trips) / (ca * cb) if lift else c_ab / np.sqrt(ca * cb)

    # Both directions, then each item's best `neighbours`, ties broken by item code (stable ids).
    src = np.concatenate([pa, pb]).astype(np.int64)
    dst = np.concatenate([pb, pa]).astype(np.int64)
    val = np.concatenate([sim, sim]).astype(np.float64)
    order = np.lexsort((dst, -val, src))
    src, dst, val = src[order], dst[order], val[order]
    starts = np.searchsorted(src, np.arange(n_items + 1), side="left").astype(np.int64)
    rank = np.arange(len(src), dtype=np.int64) - starts[src]
    top = rank < config.neighbours
    src, dst, val = src[top], dst[top], val[top]
    ptr = np.searchsorted(src, np.arange(n_items + 1), side="left").astype(np.int64)
    return _Neighbours(ptr=ptr, item=dst, score=val), item_trips, n_trips


@dataclass(frozen=True, eq=False)
class CoPurchaseModel:
    """A fitted item-item co-purchase model. Build it with `CoPurchaseModel.fit`; it never changes."""

    config: CoPurchaseConfig
    as_of: datetime
    items: tuple[str, ...]
    """Item ids, sorted; an item's position is its code."""
    category: tuple[str | None, ...]
    """Each item's most common category in the training data, or None."""
    names: Mapping[str, str]
    item_trips: IntArray
    n_trips: int
    popularity: FloatArray
    """Distinct buyers in the popularity window, per item."""
    neighbours: _Neighbours
    popular_order: IntArray
    """Item codes, most popular first: recent buyers, then all-time trips, then item id."""
    popular_in: Mapping[str, IntArray]
    """The same order, restricted to each category."""
    code: Mapping[str, int] = field(repr=False)

    @classmethod
    def fit(
        cls,
        purchases: pd.DataFrame | Iterable[Event],
        *,
        as_of: datetime,
        config: CoPurchaseConfig | None = None,
        names: Mapping[str, str] | None = None,
    ) -> CoPurchaseModel:
        """Learn from purchases made at or before `as_of`; later rows are ignored, so nothing leaks."""
        cfg = config or CoPurchaseConfig()
        frame = purchases if isinstance(purchases, pd.DataFrame) else basket_frame(purchases)
        frame = _check_frame(frame)
        cutoff = _seconds(as_of)
        frame = frame[frame["t"].to_numpy(dtype=np.int64) <= cutoff]

        cust_codes, _ = pd.factorize(frame["customer_id"], sort=True)
        item_codes, item_index = pd.factorize(frame["item_id"], sort=True)
        basket_codes, _ = pd.factorize(frame["basket_id"], sort=True)
        items = tuple(str(v) for v in item_index)
        n_items = len(items)
        t = frame["t"].to_numpy(dtype=np.int64)
        item_arr = np.asarray(item_codes, dtype=np.int64)
        cust_arr = np.asarray(cust_codes, dtype=np.int64)

        trip = _trips(cust_arr, np.asarray(basket_codes, dtype=np.int64), t, int(cfg.window.total_seconds()))
        neighbours, item_trips, n_trips = _similarity(trip, item_arr, n_items, cfg)

        recent = t >= cutoff - int(cfg.popularity_window.total_seconds())
        buyers = pd.DataFrame({"c": cust_arr[recent], "i": item_arr[recent]}).drop_duplicates()
        popularity = np.bincount(buyers["i"].to_numpy(dtype=np.int64), minlength=n_items).astype(np.float64)
        all_trips = pd.DataFrame({"trip": trip, "i": item_arr}).drop_duplicates()
        ever = np.bincount(all_trips["i"].to_numpy(dtype=np.int64), minlength=n_items).astype(np.float64)
        popular_order = np.lexsort((np.arange(n_items), -ever, -popularity)).astype(np.int64)

        category: list[str | None] = [None] * n_items
        known = frame["category"].notna().to_numpy(dtype=bool)
        if known.any():
            tally: Counter[tuple[int, str]] = Counter(
                zip(item_arr[known].tolist(), map(str, frame["category"].to_numpy()[known]), strict=True)
            )
            # Each item's most common category; a tie goes to the alphabetically first name.
            for (i, cat), _ in sorted(tally.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1])):
                if category[i] is None:
                    category[i] = cat
        cat_arr = np.array([c if c is not None else "" for c in category], dtype=object)
        popular_in = {
            str(c): popular_order[cat_arr[popular_order] == c] for c in sorted({c for c in category if c})
        }
        return cls(
            config=cfg,
            as_of=as_of,
            items=items,
            category=tuple(category),
            names=dict(names or {}),
            item_trips=item_trips,
            n_trips=n_trips,
            popularity=popularity,
            neighbours=neighbours,
            popular_order=popular_order,
            popular_in=popular_in,
            code={item: i for i, item in enumerate(items)},
        )

    # -- reading the model --------------------------------------------------------------------------
    def name(self, item_id: str) -> str:
        """The product's display name, or its id when no name was given."""
        return self.names.get(item_id, item_id)

    def related(self, item_id: str, k: int = 10) -> list[tuple[str, float]]:
        """The items most often bought with `item_id`, best first; empty for an unknown item."""
        a = self.code.get(item_id)
        if a is None:
            return []
        idx, sc = self.neighbours.of(a)
        return [(self.items[int(i)], float(s)) for i, s in zip(idx[:k], sc[:k], strict=True)]

    def popular(self, k: int, *, exclude: Iterable[str] = ()) -> list[str]:
        """The `k` most popular items right now (as of the fit), leaving out `exclude`."""
        skip = set(exclude)
        out: list[str] = []
        for i in self.popular_order:
            item = self.items[int(i)]
            if item not in skip:
                out.append(item)
                if len(out) == k:
                    break
        return out

    # -- recommending -------------------------------------------------------------------------------
    def recommend(
        self,
        customer_events: Iterable[Event],
        *,
        k: int,
        now: datetime,
        exclude_owned: bool = True,
    ) -> list[Recommendation]:
        """Up to `k` complementary items for one customer, from their own events at or before `now`."""
        cutoff = _seconds(now)
        last: dict[str, int] = {}
        cats: Counter[str] = Counter()
        for e in customer_events:
            if e.type is not EventType.PURCHASE:
                continue
            ts = _seconds(e.ts)
            if ts > cutoff:
                continue
            # A purchase known only by its category still says what the customer shops for, so it
            # steers the popular fallback even though it cannot vote for co-purchase neighbours.
            if e.category:
                cats[e.category] += 1
            if e.item_id is not None:
                last[e.item_id] = max(ts, last.get(e.item_id, ts))
        # No categories on the events: fall back to the categories the model learnt for those items.
        return self.recommend_items(last, k=k, now=now, exclude_owned=exclude_owned, categories=cats or None)

    def recommend_items(
        self,
        last_bought: Mapping[str, int],
        *,
        k: int,
        now: datetime,
        exclude_owned: bool = True,
        categories: Mapping[str, int] | None = None,
    ) -> list[Recommendation]:
        """The same as `recommend`, from {item_id: Unix seconds it was last bought}; used in bulk scoring."""
        if k <= 0:
            return []
        cutoff = _seconds(now)
        half = self.config.half_life.total_seconds()
        owned = [(self.code[i], ts) for i, ts in sorted(last_bought.items()) if i in self.code]
        owned_codes = {c for c, _ in owned}
        if categories is None:
            categories = Counter(cat for c in owned_codes if (cat := self.category[c]) is not None)

        # Every owned item's neighbour run, gathered in one go: run j covers [lo[j], lo[j] + n[j]).
        codes = np.array([c for c, _ in owned], dtype=np.int64)
        ages = np.array([max(cutoff - ts, 0) for _, ts in owned], dtype=np.float64)
        lo = self.neighbours.ptr[codes]
        n = self.neighbours.ptr[codes + 1] - lo
        picks: list[tuple[int, float, str]] = []
        if int(n.sum()) > 0:
            starts = np.repeat(lo - (np.cumsum(n) - n), n)
            at = starts + np.arange(int(n.sum()), dtype=np.int64)
            ci = self.neighbours.item[at]
            cv = self.neighbours.score[at] * np.repeat(0.5 ** (ages / half), n)
            cf = np.repeat(codes, n)
            if exclude_owned:
                mask = ~np.isin(ci, codes)
                ci, cv, cf = ci[mask], cv[mask], cf[mask]
            if len(ci):
                uniq, inv = np.unique(ci, return_inverse=True)
                total = np.bincount(inv, weights=cv, minlength=len(uniq))
                voters = np.bincount(inv, minlength=len(uniq))
                # The strongest single voter is the reason; ties go to the lower item code.
                order = np.lexsort((cf, -cv, inv))
                first = np.ones(len(order), dtype=bool)
                first[1:] = inv[order][1:] != inv[order][:-1]
                best_from = np.empty(len(uniq), dtype=np.int64)
                best_from[inv[order][first]] = cf[order][first]
                rank = np.lexsort((uniq, -total))[: max(k * 5, k + 10)]
                for r in rank:
                    source = self.name(self.items[int(best_from[r])])
                    others = int(voters[r]) - 1
                    reason = f"Often bought with your {source}"
                    if others > 0:
                        reason += f" and {others} other thing{'s' if others > 1 else ''} you bought"
                    picks.append((int(uniq[r]), float(total[r]), reason))

        picks.extend(self._fallback(picks, owned_codes if exclude_owned else set(), k, categories))
        return self._diversify(picks, k)

    def _fallback(
        self,
        picks: Sequence[tuple[int, float, str]],
        skip: set[int],
        k: int,
        categories: Mapping[str, int] | None,
    ) -> list[tuple[int, float, str]]:
        """Popular items, the customer's own categories first, scored strictly below every co-purchase pick."""
        taken = skip | {c for c, _, _ in picks}
        need = k + 5  # a few spare, so diversity has room to choose
        floor = min((s for _, s, _ in picks), default=1.0)
        top = float(self.popularity.max()) if len(self.popularity) and self.popularity.max() > 0 else 1.0
        out: list[tuple[int, float, str]] = []

        # Three tiers that never overlap: co-purchase picks are >= floor, popular items in the
        # customer's own categories fall in [0.25, 0.5] x floor, other popular items in [0.1, 0.2] x floor.
        def take(order: IntArray, reason: str, tier: float) -> None:
            for i in order:
                c = int(i)
                if len(out) >= need:
                    return
                if c in taken:
                    continue
                taken.add(c)
                share = float(self.popularity[c]) / top
                out.append((c, tier * floor * (0.5 + 0.5 * share), reason))

        mine = sorted((categories or {}).items(), key=lambda kv: (-kv[1], kv[0]))
        for cat, _ in mine:
            if cat in self.popular_in:
                take(self.popular_in[cat], f"Popular right now with customers who buy {cat}", 0.5)
        take(self.popular_order, "Popular with other customers right now", 0.2)
        return out

    def _diversify(self, picks: Sequence[tuple[int, float, str]], k: int) -> list[Recommendation]:
        """Greedy re-rank: each repeat of a category scales the next item by `diversity`.

        At each step the best adjusted score wins, and adjusted scores only fall as categories fill,
        so the returned scores never increase down the list.
        """
        pool = sorted(picks, key=lambda p: (-p[1], p[0]))
        if self.config.diversity == 1.0 or not any(self.category[c] for c, _, _ in pool):
            return [Recommendation(self.items[c], s, reason) for c, s, reason in pool[:k]]
        seen: Counter[str] = Counter()
        out: list[Recommendation] = []
        used = [False] * len(pool)
        d = self.config.diversity
        while len(out) < k:
            best, best_score = -1, -math.inf
            for j, (c, s, _) in enumerate(pool):
                if used[j]:
                    continue
                cat = self.category[c]
                adj = s * (d ** seen[cat]) if cat else s
                if adj > best_score:
                    best, best_score = j, adj
            if best < 0:
                break
            used[best] = True
            c, _, reason = pool[best]
            cat = self.category[c]
            if cat:
                seen[cat] += 1
            out.append(Recommendation(self.items[c], best_score, reason))
        return out


def recommend(
    model: CoPurchaseModel,
    customer_events: Iterable[Event],
    k: int = 5,
    *,
    now: datetime,
    exclude_owned: bool = True,
) -> list[Recommendation]:
    """Up to `k` complementary items for one customer: `[(item_id, score, reason), ...]`, best first."""
    return model.recommend(customer_events, k=k, now=now, exclude_owned=exclude_owned)


# ---------------------------------------------------------------------------
# Refill or complement: the next best item
# ---------------------------------------------------------------------------
class RefillWindow(Protocol):
    """What `next_best_item` needs from the refill-cycle model for one thing the customer buys again.

    `autonomy.catalog.cycle.CycleEstimate` fits as it is: `key` is the item (or category) id, `level`
    says which, `window_open` says whether now is the time to remind, and `days_until_due` (negative
    when overdue) orders several open windows. Only item-level windows can become a refill of "the
    same item"; a category-level window names no product, so it is skipped here.
    """

    @property
    def key(self) -> str: ...

    @property
    def level(self) -> str: ...

    @property
    def window_open(self) -> bool: ...

    @property
    def days_until_due(self) -> float: ...


@dataclass(frozen=True)
class RefillWindowInfo:
    """A ready-made `RefillWindow`, for callers and tests that hold the facts directly."""

    key: str
    window_open: bool
    level: str = "item"
    days_until_due: float = 0.0


@dataclass(frozen=True)
class NextItem:
    """The one product an action should be about (`Action.item_id`), and why, in one plain sentence."""

    item_id: str
    kind: Literal["refill", "complement"]
    reason: str


def next_best_item(
    customer_events: Iterable[Event],
    model: CoPurchaseModel,
    cycle_info: Iterable[RefillWindow],
    *,
    now: datetime,
) -> NextItem | None:
    """Refill the same item when its refill window is open; otherwise suggest a complementary one.

    A refill comes first because it is the customer's own habit, the most likely purchase there is.
    Among several open item-level windows the most due wins (smallest `days_until_due`, then the
    item id, so the choice is stable). With no open window, the top recommendation is used, leaving
    out what the customer already owns; when co-purchase has nothing for this customer that is the
    popular fallback, and its reason says so ("Popular ..."), so callers can tell the two apart.
    None means there is nothing worth suggesting, and the caller should consider doing nothing.
    """
    open_items = sorted(
        (w for w in cycle_info if w.window_open and w.level == "item"),
        key=lambda w: (w.days_until_due, w.key),
    )
    if open_items:
        item = open_items[0].key
        return NextItem(
            item_id=item,
            kind="refill",
            reason=f"Your {model.name(item)} is probably running low: it is about when you usually buy it again",
        )
    recs = model.recommend(customer_events, k=1, now=now)
    if not recs:
        return None
    top = recs[0]
    return NextItem(item_id=top.item_id, kind="complement", reason=top.reason)
