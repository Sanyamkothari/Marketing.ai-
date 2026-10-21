"""Customer features for the uplift model: what a `CustomerState` carries, as one-hot columns.

Two sources, both read the same way at fitting time and at decision time:

* **Attributes** in `CustomerState.preferences` (column name -> value as text): what a connector or a
  column mapping folds in as PREFERENCE events (age band, tariff, region, past response...). A key
  whose values read as numbers is cut into quantile bins of the training customers; any other key
  keeps its most common levels, the rest go to "other". Every key also has a "missing" column.
* **Behaviour** as of the moment: purchases so far, revenue so far, days since the last purchase and
  days since the customer was first seen. At fitting time these are rebuilt from the customer's
  events *before the campaign* (`behaviour_from_events`), at decision time read from the state
  (`behaviour_from_state`), so a purchase the campaign caused never becomes a feature of the model
  that is meant to predict it.

Everything is plain Python and numpy; one customer is encoded as the list of its active columns
(`active`), the training matrix as a dense float32 array (`matrix`), from the same function.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Channel, CustomerState, EventType
from autonomy.contracts.events import AttrValue

__all__ = [
    "BEHAVIOUR",
    "EventLike",
    "FeatureEncoder",
    "behaviour_from_events",
    "behaviour_from_state",
]


class EventLike(Protocol):
    """What the uplift model reads of an event. `Event` is one; so is the compact record the agent
    rebuilds from its history digest (`autonomy.learning.calibration.DigestEvent`), which keeps a
    warm start on months of purchases small."""

    @property
    def event_id(self) -> str: ...
    @property
    def type(self) -> EventType: ...
    @property
    def ts(self) -> datetime: ...
    @property
    def channel(self) -> Channel | None: ...
    @property
    def value(self) -> float | None: ...
    @property
    def decision_id(self) -> str | None: ...
    @property
    def attrs(self) -> Mapping[str, AttrValue]: ...


FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

BEHAVIOUR: tuple[str, ...] = ("purchases", "revenue", "days_since_purchase", "tenure_days")
"""Behaviour features, in order (NaN when unknown: no purchase yet, never seen)."""

NUMERIC_BINS = 6
"""Quantile bins per numeric feature (fewer when the values have fewer distinct cut points)."""
MAX_LEVELS = 12
"""Most common levels kept per text feature; the rest share an "other" column."""
MIN_LEVEL_SHARE = 0.002
"""A level is kept only if at least this share of the training customers has it ..."""
MIN_LEVEL_COUNT = 20
"""... and at least this many."""
MIN_KEY_SHARE = 0.01
"""An attribute is used only if at least this share of the training customers has it."""
MAX_KEYS = 64
"""Most attributes used (the most often filled first, then by name)."""
NUMERIC_SHARE = 0.98
"""An attribute is numeric when at least this share of its values read as finite numbers."""

_SECONDS_PER_DAY = 86_400.0


def _number(text: str | None) -> float:
    """The value as a float, NaN when it is not a finite number."""
    if text is None:
        return math.nan
    try:
        v = float(text)
    except (TypeError, ValueError):
        return math.nan
    return v if math.isfinite(v) else math.nan


def behaviour_from_state(state: CustomerState, now: datetime) -> tuple[float, ...]:
    """The behaviour features of a customer at `now`, from their folded state."""
    last = state.last_purchase_at
    first = state.first_seen_at
    return (
        float(state.purchases),
        float(state.revenue),
        (now - last).total_seconds() / _SECONDS_PER_DAY if last is not None else math.nan,
        (now - first).total_seconds() / _SECONDS_PER_DAY if first is not None else math.nan,
    )


def behaviour_from_events(events: Sequence[EventLike], at: datetime) -> tuple[float, ...]:
    """The behaviour features of a customer at `at`, from their events strictly before `at`, counted
    the way the production `StateFolder` counts them (revenue net of refunds), so a customer encodes
    the same from their events at fitting time as from their folded state at decision time."""
    purchases = 0
    revenue = 0.0
    last: datetime | None = None
    first: datetime | None = None
    for e in events:
        if e.ts >= at:
            continue
        if first is None or e.ts < first:
            first = e.ts
        if e.type is EventType.PURCHASE:
            purchases += 1
            revenue += float(e.value or 0.0)
            if last is None or e.ts > last:
                last = e.ts
        elif e.type is EventType.REFUND:
            revenue -= float(e.value or 0.0)
    return (
        float(purchases),
        revenue,
        (at - last).total_seconds() / _SECONDS_PER_DAY if last is not None else math.nan,
        (at - first).total_seconds() / _SECONDS_PER_DAY if first is not None else math.nan,
    )


@dataclass(frozen=True)
class _Numeric:
    name: str
    edges: tuple[float, ...]
    start: int
    """First column: bins `start .. start + len(edges)`, then the missing column."""

    @property
    def width(self) -> int:
        return len(self.edges) + 2

    def column(self, value: float) -> int:
        if math.isnan(value):
            return self.start + len(self.edges) + 1
        lo, hi = 0, len(self.edges)
        while lo < hi:  # bisect_right
            mid = (lo + hi) // 2
            if value < self.edges[mid]:
                hi = mid
            else:
                lo = mid + 1
        return self.start + lo


@dataclass(frozen=True)
class _Text:
    name: str
    levels: tuple[str, ...]
    start: int
    """Columns: one per level, then "other", then missing."""

    @property
    def width(self) -> int:
        return len(self.levels) + 2

    def column(self, value: str | None) -> int:
        if value is None:
            return self.start + len(self.levels) + 1
        for k, level in enumerate(self.levels):
            if value == level:
                return self.start + k
        return self.start + len(self.levels)


def _edges(values: FloatArray, bins: int) -> tuple[float, ...]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return ()
    qs = np.quantile(finite, np.linspace(0.0, 1.0, bins + 1)[1:-1])
    cut = np.unique(qs)
    # A cut at the minimum would leave its lower bin empty.
    cut = cut[cut > finite.min()]
    return tuple(float(c) for c in cut)


class FeatureEncoder:
    """One-hot columns of customer attributes and behaviour, fitted on the training customers.

    Column 0 is an intercept. `blocks` is the number of feature blocks (attributes and behaviour
    features that vary), used to scale the model's penalties."""

    def __init__(
        self, numeric: Sequence[_Numeric], text: Sequence[_Text], behaviour: Sequence[_Numeric]
    ) -> None:
        self._numeric = tuple(numeric)
        self._text = tuple(text)
        self._behaviour = tuple(behaviour)
        widths = [b.width for b in self._numeric] + [b.width for b in self._text]
        widths += [b.width for b in self._behaviour]
        self.width = 1 + sum(widths)
        self.blocks = (
            sum(1 for b in self._numeric if b.edges)
            + len(self._text)
            + sum(1 for b in self._behaviour if b.edges)
        )

    @property
    def names(self) -> tuple[str, ...]:
        """The attribute and behaviour features the encoder reads."""
        names = [b.name for b in self._numeric] + [b.name for b in self._text]
        return tuple(names + [b.name for b in self._behaviour])

    @classmethod
    def fit(
        cls,
        attributes: Sequence[Mapping[str, str]],
        behaviour: FloatArray,
        *,
        use_attributes: bool = True,
    ) -> FeatureEncoder:
        """Choose the columns from the training customers' attributes and behaviour (n x 4)."""
        n = len(attributes)
        numeric: list[_Numeric] = []
        text: list[_Text] = []
        start = 1
        if use_attributes and n:
            filled: Counter[str] = Counter()
            for row in attributes:
                filled.update(k for k, v in row.items() if v is not None and v != "")
            keys = [k for k, c in filled.items() if c >= MIN_KEY_SHARE * n]
            keys.sort(key=lambda k: (-filled[k], k))
            keys = sorted(keys[:MAX_KEYS])
            for key in keys:
                raw = [row.get(key) for row in attributes]
                present = [v for v in raw if v is not None and v != ""]
                values = np.array([_number(v) for v in present], dtype=np.float64)
                if present and np.isfinite(values).mean() >= NUMERIC_SHARE:
                    edges = _edges(values, NUMERIC_BINS)
                    block_n = _Numeric(key, edges, start)
                    numeric.append(block_n)
                    start += block_n.width
                    continue
                counts = Counter(present)
                floor = max(MIN_LEVEL_COUNT, MIN_LEVEL_SHARE * n)
                kept = [lv for lv, c in counts.items() if c >= floor]
                kept.sort(key=lambda lv: (-counts[lv], lv))
                if not kept:
                    continue
                block_t = _Text(key, tuple(sorted(kept[:MAX_LEVELS])), start)
                text.append(block_t)
                start += block_t.width
        beh: list[_Numeric] = []
        for j, name in enumerate(BEHAVIOUR):
            col = behaviour[:, j] if behaviour.size else np.zeros(0)
            block_b = _Numeric(name, _edges(np.asarray(col, dtype=np.float64), NUMERIC_BINS), start)
            beh.append(block_b)
            start += block_b.width
        return cls(numeric, text, beh)

    def active(self, attributes: Mapping[str, str], behaviour: Sequence[float]) -> list[int]:
        """The columns that are 1 for one customer (the intercept first)."""
        cols = [0]
        for bn in self._numeric:
            cols.append(bn.column(_number(attributes.get(bn.name))))
        for bt in self._text:
            raw = attributes.get(bt.name)
            cols.append(bt.column(raw if raw else None))
        for bb, value in zip(self._behaviour, behaviour, strict=True):
            cols.append(bb.column(float(value)))
        return cols

    def matrix(
        self, attributes: Sequence[Mapping[str, str]], behaviour: FloatArray
    ) -> npt.NDArray[np.float32]:
        """The dense one-hot matrix (float32, n x width) of many customers."""
        n = len(attributes)
        x = np.zeros((n, self.width), dtype=np.float32)
        for i in range(n):
            x[i, self.active(attributes[i], behaviour[i])] = 1.0
        return x
