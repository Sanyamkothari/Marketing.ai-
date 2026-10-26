"""Customer types: groups of customers who look alike, each with a plain label (plan-v3 decision 6).

The copy is written per type, not per customer: a shop with a million customers has 50-300 types,
so the model writes a few thousand drafts a week instead of millions.

A customer's profile is a short row of numbers: a base set read from `CustomerState` itself
(how long since the last purchase, how many purchases, spend, order value, how much of an offer the
person needs, how often we contacted them, how many channels reach them) plus the nightly profile
`CustomerState.features` when it is there (plan-v3 foundation A). Columns are log-scaled where they
are skewed, standardised, and clipped, then grouped with k-means. numpy only, and the same states
and seed always give the same types.

How many types: aim for `members_per_type` customers a type, never fewer than `min_types` or more
than `max_types`, and never so many that the average type has under `min_members_floor` customers
(a small shop gets fewer types, because it has fewer customers to tell apart). The centres are
fitted on a seeded sample of at most `fit_sample` customers; everyone else is assigned to the nearest
centre in chunks, so a million customers cost seconds, not a distance matrix of a million rows.

Each type carries a plain-English label ("lapsed, repeat buyer, high spend, needs a nudge") built from
where its typical member sits among all customers, never from one customer's data. That label is
all the model writer is told about the type.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from autonomy.content.config import CopyConfig
from autonomy.contracts import CustomerState

Matrix = NDArray[np.float64]

BASE_NAMES: tuple[str, ...] = (
    "recency_days",
    "tenure_days",
    "purchases",
    "revenue",
    "avg_order_value",
    "offer_sensitivity",
    "contacts_30d",
    "contactable_channels",
    "has_purchase",
)
"""The base columns, in order. Always present, built from `CustomerState` fields only."""

TRAIT_NAMES: tuple[str, ...] = (
    "recency_days",
    "purchases",
    "revenue",
    "avg_order_value",
    "offer_sensitivity",
)
"""The base columns a type is summarised by for the scoring model (its z-scores at the type's centre)."""

NEVER_PURCHASED_DAYS = 1000.0
"""Recency stand-in for someone who never bought; the log scale keeps it from stretching the column."""

CLIP = 5.0
"""Standardised columns are clipped to +-CLIP so one extreme customer cannot own a centre."""

CHUNK = 8192
"""Rows per distance block when assigning customers to centres."""


def _days(later: datetime, earlier: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / 86400.0)


def base_row(state: CustomerState) -> list[float]:
    """The raw base columns of `BASE_NAMES` for one customer, relative to the state's own `as_of`."""
    recency = _days(state.as_of, state.last_purchase_at) if state.last_purchase_at else NEVER_PURCHASED_DAYS
    tenure = _days(state.as_of, state.first_seen_at) if state.first_seen_at else 0.0
    return [
        min(recency, NEVER_PURCHASED_DAYS),
        tenure,
        float(state.purchases),
        float(state.revenue),
        float(state.avg_order_value),
        0.5 if state.offer_sensitivity is None else float(state.offer_sensitivity),
        float(state.contacts_30d),
        float(len(state.contactable_channels())),
        1.0 if state.purchases > 0 else 0.0,
    ]


_LOG_COLUMNS = frozenset({0, 1, 2, 3, 4, 6})
"""Indices into `BASE_NAMES` that are skewed counts, days or money: log1p-scaled before standardising."""


def _base_matrix(states: Sequence[CustomerState]) -> Matrix:
    raw = np.array([base_row(s) for s in states], dtype=np.float64).reshape(len(states), len(BASE_NAMES))
    for col in _LOG_COLUMNS:
        raw[:, col] = np.log1p(np.maximum(raw[:, col], 0.0))
    return raw


def _profile_matrix(states: Sequence[CustomerState], names: Sequence[str], fill: Sequence[float]) -> Matrix:
    out = np.empty((len(states), len(names)), dtype=np.float64)
    for j, name in enumerate(names):
        for i, s in enumerate(states):
            v = s.features.get(name)
            out[i, j] = fill[j] if v is None or not math.isfinite(v) else float(v)
    return out


def _chunks(n: int) -> list[tuple[int, int]]:
    return [(a, min(a + CHUNK, n)) for a in range(0, n, CHUNK)]


def _nearest(x: Matrix, centres: Matrix) -> NDArray[np.int64]:
    """The index of the nearest centre for every row, computed in blocks."""
    c2 = (centres**2).sum(axis=1)
    out = np.empty(len(x), dtype=np.int64)
    for a, b in _chunks(len(x)):
        block = x[a:b]
        d = (block**2).sum(axis=1)[:, None] - 2.0 * block @ centres.T + c2[None, :]
        out[a:b] = np.argmin(d, axis=1)
    return out


def _min_dist2(x: Matrix, centres: Matrix) -> NDArray[np.float64]:
    c2 = (centres**2).sum(axis=1)
    out = np.empty(len(x), dtype=np.float64)
    for a, b in _chunks(len(x)):
        block = x[a:b]
        d = (block**2).sum(axis=1)[:, None] - 2.0 * block @ centres.T + c2[None, :]
        out[a:b] = np.maximum(d.min(axis=1), 0.0)
    return out


def _kmeans_pp(x: Matrix, k: int, rng: np.random.Generator) -> Matrix:
    """k-means++ seeding: each new centre is drawn with chance proportional to its squared distance."""
    n = len(x)
    centres = np.empty((k, x.shape[1]), dtype=np.float64)
    centres[0] = x[int(rng.integers(n))]
    closest = np.maximum(((x - centres[0]) ** 2).sum(axis=1), 0.0)
    for j in range(1, k):
        total = float(closest.sum())
        pick = int(rng.integers(n)) if total <= 0.0 else int(rng.choice(n, p=closest / total))
        centres[j] = x[pick]
        closest = np.minimum(closest, ((x - centres[j]) ** 2).sum(axis=1))
    return centres


def _lloyd(x: Matrix, centres: Matrix, iterations: int) -> tuple[Matrix, NDArray[np.int64]]:
    k = len(centres)
    labels = _nearest(x, centres)
    for _ in range(iterations):
        counts = np.bincount(labels, minlength=k)
        sums = np.zeros_like(centres)
        np.add.at(sums, labels, x)
        new = centres.copy()
        filled = counts > 0
        new[filled] = sums[filled] / counts[filled, None]
        for j in np.flatnonzero(~filled):  # an empty centre moves to the point farthest from its centre
            far = int(np.argmax(((x - new[labels]) ** 2).sum(axis=1)))
            new[j] = x[far]
            labels[far] = j
        moved = float(np.abs(new - centres).max())
        centres = new
        labels = _nearest(x, centres)
        if moved < 1e-6:
            break
    return centres, labels


@dataclass(frozen=True)
class CustomerType:
    """One group of look-alike customers."""

    type_id: str
    label: str
    size: int
    traits: tuple[float, ...]
    summary: dict[str, float] = field(default_factory=dict)

    @property
    def note(self) -> str:
        """What the model writer is told about the type: the label only, no customer data."""
        return self.label


def choose_k(n: int, cfg: CopyConfig) -> int:
    """How many types for `n` customers (see the module docstring)."""
    if n < 1:
        raise ValueError("no customers to group")
    wanted = min(cfg.max_types, max(cfg.min_types, n // cfg.members_per_type))
    return max(1, min(wanted, n // cfg.min_members_floor))


class CustomerTypes:
    """Fitted types. Build with `CustomerTypes.fit`; read with `assign` and `assign_many`."""

    def __init__(
        self,
        *,
        profile_names: tuple[str, ...],
        fill: tuple[float, ...],
        mean: Matrix,
        scale: Matrix,
        weights: Matrix,
        centres: Matrix,
        types: tuple[CustomerType, ...],
    ) -> None:
        self.profile_names = profile_names
        self._fill = fill
        self._mean = mean
        self._scale = scale
        self._weights = weights
        self._centres = centres
        self.types = types
        self._index = {t.type_id: t for t in types}

    # --- fitting -----------------------------------------------------------------------------

    @classmethod
    def fit(cls, states: Sequence[CustomerState], config: CopyConfig | None = None) -> CustomerTypes:
        """Group `states` into types. Deterministic for the same states, order and `config.seed`."""
        cfg = config or CopyConfig()
        n_all = len(states)
        k = choose_k(n_all, cfg)
        rng = np.random.default_rng(cfg.seed)
        if n_all > cfg.fit_sample:
            pick = np.sort(rng.choice(n_all, size=cfg.fit_sample, replace=False))
            sample = [states[int(i)] for i in pick]
        else:
            sample = list(states)
        k = min(k, len(sample))

        names, fill = _profile_columns(sample, cfg.max_profile_features)
        raw = np.hstack([_base_matrix(sample), _profile_matrix(sample, names, fill)])
        mean = raw.mean(axis=0)
        std = raw.std(axis=0)
        scale = np.where(std > 1e-9, std, 1.0)
        n_base, n_prof = len(BASE_NAMES), len(names)
        w_prof = min(1.0, math.sqrt(n_base / n_prof)) if n_prof else 1.0
        weights = np.concatenate([np.ones(n_base), np.full(n_prof, w_prof)]).astype(np.float64)
        z = np.clip((raw - mean) / scale, -CLIP, CLIP) * weights

        centres, labels = _lloyd(z, _kmeans_pp(z, k, rng), cfg.kmeans_iterations)
        types, centres = _describe(sample, raw, z, centres, labels)
        return cls(
            profile_names=names,
            fill=fill,
            mean=mean,
            scale=scale,
            weights=weights,
            centres=centres,
            types=types,
        )

    # --- reading -----------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.types)

    def get(self, type_id: str) -> CustomerType:
        return self._index[type_id]

    def _z(self, states: Sequence[CustomerState]) -> Matrix:
        raw = np.hstack([_base_matrix(states), _profile_matrix(states, self.profile_names, self._fill)])
        return np.clip((raw - self._mean) / self._scale, -CLIP, CLIP) * self._weights

    def assign_many(self, states: Sequence[CustomerState]) -> list[str]:
        """The type id of every customer, in order."""
        if not states:
            return []
        ids = [t.type_id for t in self.types]
        out: list[str] = []
        for a, b in _chunks(len(states)):
            nearest = _nearest(self._z(states[a:b]), self._centres)
            out.extend(ids[int(i)] for i in nearest)
        return out

    def assign(self, state: CustomerState) -> str:
        return self.assign_many([state])[0]

    def counts(self, states: Sequence[CustomerState]) -> dict[str, int]:
        """How many of `states` fall in each type (every type is listed, possibly with 0)."""
        out = {t.type_id: 0 for t in self.types}
        for type_id in self.assign_many(states):
            out[type_id] += 1
        return out


def _profile_columns(
    sample: Sequence[CustomerState], limit: int
) -> tuple[tuple[str, ...], tuple[float, ...]]:
    """The profile feature names used (present on at least half the sample), with a fill value each."""
    if limit == 0 or not sample:
        return (), ()
    seen: dict[str, list[float]] = {}
    for s in sample:
        for name, value in s.features.items():
            if math.isfinite(value):
                seen.setdefault(name, []).append(float(value))
    covered = [(len(v), name) for name, v in seen.items() if len(v) * 2 >= len(sample)]
    covered.sort(key=lambda c: (-c[0], c[1]))
    names = tuple(name for _, name in covered[:limit])
    return names, tuple(float(np.median(seen[name])) for name in names)


def _tercile_word(value: float, low: float, high: float, words: tuple[str, str, str]) -> str:
    if low >= high:
        return words[1]
    if value <= low:
        return words[0]
    if value >= high:
        return words[2]
    return words[1]


def _describe(
    sample: Sequence[CustomerState],
    raw: Matrix,
    z: Matrix,
    centres: Matrix,
    labels: NDArray[np.int64],
) -> tuple[tuple[CustomerType, ...], Matrix]:
    """Order the types (largest first), drop any that ended up empty, and give each a plain label."""
    k = len(centres)
    counts = np.bincount(labels, minlength=k)
    n_base = len(BASE_NAMES)
    q_lo = np.quantile(raw[:, :n_base], 1 / 3, axis=0)
    q_hi = np.quantile(raw[:, :n_base], 2 / 3, axis=0)
    col = {name: i for i, name in enumerate(BASE_NAMES)}
    sens_known = sum(s.offer_sensitivity is not None for s in sample) * 2 >= len(sample)

    order = sorted(
        (j for j in range(k) if counts[j] > 0),
        key=lambda j: (-int(counts[j]), tuple(round(float(v), 9) for v in centres[j])),
    )
    trait_cols = [col[name] for name in TRAIT_NAMES]
    rows: list[tuple[int, NDArray[np.int64], Matrix, str, str]] = []
    for j in order:
        members = np.flatnonzero(labels == j)
        med = np.median(raw[members, :n_base], axis=0)

        def word(name: str, words: tuple[str, str, str], med: Matrix = med) -> str:
            c = col[name]
            return _tercile_word(float(med[c]), float(q_lo[c]), float(q_hi[c]), words)

        parts: list[str] = []
        if med[col["has_purchase"]] < 0.5:
            parts.append("never bought")
        else:
            parts.append(word("recency_days", ("recent buyer", "cooling", "lapsed")))
            parts.append(word("purchases", ("few orders", "repeat buyer", "frequent buyer")))
            parts.append(word("revenue", ("low spend", "mid spend", "high spend")))
        if sens_known:
            parts.append(word("offer_sensitivity", ("pays full price", "needs a nudge", "deal seeker")))
        favourite = _dominant_category([sample[int(i)] for i in members])
        if favourite:
            parts.append(f"mostly buys {favourite}")
        # Said only when two types would otherwise read the same: how long ago they last bought and
        # how long they have been customers, as the type's typical (median) member.
        detail = f"last order about {round(float(np.expm1(med[col['recency_days']])))} days ago"
        if med[col["has_purchase"]] < 0.5:
            detail = f"customers for about {round(float(np.expm1(med[col['tenure_days']])))} days"
        rows.append((j, members, med, ", ".join(parts), detail))

    base_count: dict[str, int] = {}
    for *_, base, _detail in rows:
        base_count[base] = base_count.get(base, 0) + 1
    seen: dict[str, int] = {}
    kept: list[CustomerType] = []
    for rank, (j, members, med, base, detail) in enumerate(rows):
        label = f"{base}, {detail}" if base_count[base] > 1 else base
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} ({seen[label]})"
        kept.append(
            CustomerType(
                type_id=f"ct{rank:03d}",
                label=label,
                size=int(counts[j]),
                traits=tuple(round(float(z[members][:, c].mean()), 6) for c in trait_cols),
                summary={
                    name: round(
                        (
                            float(np.expm1(med[col[name]]))
                            if col[name] in _LOG_COLUMNS
                            else float(med[col[name]])
                        ),
                        4,
                    )
                    for name in BASE_NAMES
                },
            )
        )
    return tuple(kept), centres[order]


def _dominant_category(members: Sequence[CustomerState]) -> str | None:
    """A top category that at least 60% of the members share, or None."""
    tally: dict[str, int] = {}
    for s in members:
        if s.top_categories:
            tally[s.top_categories[0]] = tally.get(s.top_categories[0], 0) + 1
    if not tally:
        return None
    name, hits = max(sorted(tally.items()), key=lambda kv: kv[1])
    return name if hits * 10 >= len(members) * 6 and len(members) >= 5 else None
