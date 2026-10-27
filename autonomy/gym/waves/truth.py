"""The hidden truth of a world: what a message really does to each customer's buying.

A message sent on day D changes the customer's buying on days D+1 .. D+14. `rel` is the customer's relative lift
(+0.07: +7% of their 14-day purchase rate) after channel and fatigue, and `weight_j` the front-loaded day weights
(`EFFECT_WEIGHTS`). Nothing is multiplied onto a click rate and no click enters the formula.

* A **positive** lift adds `rel x base_rate x 14 x weight_j` to the customer's daily chance of an extra purchase
  (additive; `base_rate` is the customer's own purchases per day before the cutoff, shrunk toward the population's).
  An extra purchase is worth the customer's own mean receipt before the cutoff.
* A **negative** lift (sleeping dogs) removes each of the customer's real receipts on day D+j with probability
  `|rel| x 14 x weight_j`, so a dog who buys more loses more and a dog who buys nothing after the cutoff loses nothing.

`rel` comes from a **small random tree** over the customer's real, point-in-time features
(`plant_effect`): depth-3 splits at random features and random quantiles, one lift per leaf, scaled so the
average relative lift is the world's `mean_lift`. It is not linear in any one feature and is fixed by the
world's seed. A world may flip some leaves negative (sleeping dogs) and add a personal factor no feature
explains. A policy never reads it; only the oracle and the scoring do.

Three repeated-send effects sit on top, none of them in the agent's model family:

* **fatigue**: a positive lift is multiplied by `fatigue ** k`, `k` = messages in the previous 14 days;
* **opt-outs**: the chance a message ends in an unsubscribe is `base x (1 + slope x messages in the previous 7
  days)`, and costs `UNSUBSCRIBE_COST` (plus the complaint share);
* **pull-forward**: after a positive effect, each real receipt on the 30 days after the window is removed with
  probability `pull_forward x rel x 14 / 30`, so a 7-day or 14-day reading of a message overstates what it earned.

The truth holds the two per-customer, per-day arrays the world draws from (`incremental`: the chance of an extra
purchase; `removal`: the chance a real receipt is taken out), and values a contact by what it adds to them: the
margin of its extra purchases, less the margin of the real receipts it is expected to remove (`at_stake`: the margin
of the customer's real receipts that day; past the end of the data, `base_rate x own mean receipt x margin`).
Both chances are capped at 1 (0.95 for extra purchases) the way the world caps them, so the world's realised
purchases are draws whose expectation is exactly the truth, customer by customer, with no sampling in the score.
`WavesTruth` is a `GroundTruth` and a `BatchTruth`, so the Gym's oracle runs on it unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import Action, Channel
from autonomy.gym.waves.config import (
    COMPLAINT_COST,
    COMPLAINT_SHARE,
    EFFECT_DAYS,
    EFFECT_WEIGHTS,
    HORIZON_DAYS,
    MARGIN,
    PULL_FORWARD_DAYS,
    TAIL_DAYS,
    UNSUBSCRIBE_COST,
    WorldSpec,
)
from autonomy.gym.waves.data import SECONDS_PER_DAY, WavesData

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

CHANNELS: tuple[Channel, ...] = (Channel.SMS, Channel.EMAIL)
CHANNEL_INDEX: dict[Channel, int] = {c: i for i, c in enumerate(CHANNELS)}
CUM_WEIGHTS: FloatArray = np.concatenate([[0.0], np.cumsum(EFFECT_WEIGHTS)])
"""`CUM_WEIGHTS[h]`: share of a message's effect that has landed h days after the send (h = 0..14)."""
MAX_UNSUBSCRIBE = 0.5
MAX_INCREMENTAL_PROB = 0.95
"""The world draws at most one extra purchase a day, with at most this chance."""
WEIGHTS: FloatArray = np.array(EFFECT_WEIGHTS, dtype=np.float64)
OFFSETS: IntArray = np.arange(1, TAIL_DAYS + 1, dtype=np.int64)
"""Days after the send a contact can touch: 1..14 (the window), then 15..44 (the pull-forward payback)."""


# --- the hidden function ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectPlant:
    """The planted relative lift per customer and how it was made."""

    rel: FloatArray
    """Relative lift of the 14-day purchase rate of one SMS to a rested customer (negative: a sleeping dog)."""
    leaf: IntArray
    """The tree leaf of each customer (all 0 when the world has no tree)."""
    n_leaves: int
    description: tuple[str, ...]
    """One line per split, for the docs and the logs (the policies never see it)."""


def rank_features(features: pd.DataFrame) -> FloatArray:
    """Each feature column as its rank in (0, 1] (ties share their mean rank): the scale the tree splits on."""
    ranks = features.rank(method="average", pct=True)
    return np.asarray(ranks.to_numpy(dtype=np.float64))


def plant_effect(world: WorldSpec, features: pd.DataFrame, seed: int) -> EffectPlant:
    """Draw the world's hidden relative lift for every customer. Fixed by `(world, features, seed)`."""
    n = len(features)
    rng = np.random.default_rng([seed, 31])
    if world.mean_lift == 0.0:
        return EffectPlant(np.zeros(n), np.zeros(n, dtype=np.int64), 1, ("no effect",))
    x = rank_features(features)
    names = list(features.columns)
    leaf = np.zeros(n, dtype=np.int64)
    lines: list[str] = []
    counter = [0]

    def grow(rows: IntArray, depth: int, path: str) -> None:
        if depth == 0 or rows.size < 20:
            leaf[rows] = counter[0]
            counter[0] += 1
            return
        j = int(rng.integers(len(names)))
        thr = float(np.quantile(x[rows, j], rng.uniform(0.3, 0.7)))
        left, right = rows[x[rows, j] <= thr], rows[x[rows, j] > thr]
        if left.size == 0 or right.size == 0:
            leaf[rows] = counter[0]
            counter[0] += 1
            return
        lines.append(f"{path or 'root'}: {names[j]} rank <= {thr:.2f}")
        grow(left, depth - 1, f"{path}L")
        grow(right, depth - 1, f"{path}R")

    grow(np.arange(n, dtype=np.int64), world.depth, "")
    n_leaves = counter[0]
    value = np.exp(rng.normal(0.0, 0.7, n_leaves)) if world.depth > 0 else np.ones(n_leaves)
    mass = np.bincount(leaf, minlength=n_leaves) / n
    if world.negative_share > 0 and n_leaves > 1:
        order = rng.permutation(n_leaves)
        flipped: list[int] = []
        total = 0.0
        for k in order.tolist():
            if abs(total + mass[k] - world.negative_share) <= abs(total - world.negative_share):
                flipped.append(k)
                total += float(mass[k])
        pos_scale = float(np.average(value, weights=mass))
        for k in flipped:
            value[k] = -float(rng.uniform(0.5, 1.5)) * pos_scale
            lines.append(f"leaf {k} is negative (sleeping dogs), {mass[k]:.1%} of customers")
    personal = np.ones(n)
    if world.idiosyncratic_sd > 0:
        sd = world.idiosyncratic_sd
        personal = np.exp(rng.normal(-0.5 * sd * sd, sd, n))
    raw = value[leaf] * personal
    pos, neg = float(raw[raw > 0].sum()), float(-raw[raw < 0].sum())
    if neg > 0.5 * pos:  # keep the average lift positive whatever the draw
        raw[raw < 0] *= 0.5 * pos / neg
    mean = float(raw.mean())
    rel = raw * (world.mean_lift / mean) if mean > 0 else raw
    return EffectPlant(rel.astype(np.float64), leaf, n_leaves, tuple(lines))


# --- what one contact is worth ---------------------------------------------------------------------


@dataclass(frozen=True)
class ContactEffects:
    """The exact expected consequences of a batch of contacts (one entry per contact)."""

    rel_eff: FloatArray
    """Relative lift after channel and fatigue."""
    purchases: FloatArray
    """The nominal effect: `rel_eff x base_rate x 14` purchases over the window (negative for a sleeping dog)."""
    extra: FloatArray
    """Change in the daily chance of an extra purchase, per day after the send (contacts x 44; 0 for a dog)."""
    removal: FloatArray
    """Change in the daily chance a real receipt is removed, per day after the send (contacts x 44)."""
    gain: FloatArray
    """Expected margin of the extra purchases the contact adds (RUB)."""
    loss: FloatArray
    """Expected margin of the real receipts it removes: inside the window for a dog, the payback otherwise."""
    gross: FloatArray
    """`gain - loss` (RUB)."""
    gross_7d: FloatArray
    """The same over the first 7 days after the send alone (what a 7-day reading of the contact would credit)."""
    cost: FloatArray
    p_unsubscribe: FloatArray
    unsubscribe_cost: FloatArray
    net: FloatArray
    """gross - cost - unsubscribe_cost."""


@dataclass
class ContactLedger:
    """Every contact a run made, for the exact score: one chunk per `act` round."""

    idx: list[IntArray] = field(default_factory=list)
    channel: list[IntArray] = field(default_factory=list)
    delivered: list[BoolArray] = field(default_factory=list)
    day: list[IntArray] = field(default_factory=list)
    gross: list[FloatArray] = field(default_factory=list)
    gross_7d: list[FloatArray] = field(default_factory=list)
    cost: list[FloatArray] = field(default_factory=list)
    unsubscribe_cost: list[FloatArray] = field(default_factory=list)

    def add(
        self, idx: IntArray, channel: IntArray, delivered: BoolArray, day: IntArray, eff: ContactEffects
    ) -> None:
        self.idx.append(idx)
        self.channel.append(channel)
        self.delivered.append(delivered)
        self.day.append(day)
        self.gross.append(np.where(delivered, eff.gross, 0.0))
        self.gross_7d.append(np.where(delivered, eff.gross_7d, 0.0))
        self.cost.append(eff.cost)
        self.unsubscribe_cost.append(np.where(delivered, eff.unsubscribe_cost, 0.0))

    def merged(self) -> dict[str, FloatArray | IntArray | BoolArray]:
        def cat(parts: Sequence[npt.NDArray[np.generic]], dtype: type) -> npt.NDArray[np.generic]:
            return np.concatenate(parts) if parts else np.zeros(0, dtype=dtype)

        return {
            "idx": cat(self.idx, np.int64),  # type: ignore[dict-item]
            "channel": cat(self.channel, np.int64),  # type: ignore[dict-item]
            "delivered": cat(self.delivered, np.bool_),  # type: ignore[dict-item]
            "day": cat(self.day, np.int64),  # type: ignore[dict-item]
            "gross": cat(self.gross, np.float64),  # type: ignore[dict-item]
            "gross_7d": cat(self.gross_7d, np.float64),  # type: ignore[dict-item]
            "cost": cat(self.cost, np.float64),  # type: ignore[dict-item]
            "unsubscribe_cost": cat(self.unsubscribe_cost, np.float64),  # type: ignore[dict-item]
        }


def cumulative_net_purchases(purchases: float, pull_forward: float, h: int) -> float:
    """Net incremental purchases of one contact `h` days after its send, from its 14-day total, for a customer who
    buys at exactly their base rate (the nominal shape; the truth itself prices the payback on real receipts).

    Days 1..14 follow the front-loaded weights; a positive effect then loses `pull_forward` of its purchases,
    spread evenly over the next 30 days. So the 7-day reading is bigger than the 44-day truth."""
    if h <= 0:
        return 0.0
    inside = purchases * float(CUM_WEIGHTS[min(h, EFFECT_DAYS)])
    if h <= EFFECT_DAYS or purchases <= 0:
        return inside
    return inside - pull_forward * purchases * min(h - EFFECT_DAYS, PULL_FORWARD_DAYS) / PULL_FORWARD_DAYS


def margin_at_stake(data: WavesData, width: int) -> FloatArray:
    """Per customer and day (0 .. width-1), the margin a fully removed day of buying would cost: the margin of the
    customer's real receipts that day while there is data, then `base_rate x own mean receipt x margin` a day.
    """
    out = np.zeros((data.n, width), dtype=np.float64)
    known = min(data.horizon_days, width)
    day = np.floor(data.post_s / SECONDS_PER_DAY).astype(np.int64)
    keep = day < known
    np.add.at(out, (data.post_idx[keep], day[keep]), MARGIN * data.post_value[keep])
    if known < width:
        out[:, known:] = (MARGIN * data.base_rate * data.value)[:, None]
    return out


def _capped_gain(current: FloatArray, added: FloatArray, cap: float) -> FloatArray:
    """How much `min(current + added, cap)` exceeds `min(current, cap)`, element by element."""
    out: FloatArray = np.minimum(current + added, cap) - np.minimum(current, cap)
    return out


class WavesTruth:
    """The planted truth of one world on one customer set. See the module docstring."""

    def __init__(
        self,
        data: WavesData,
        world: WorldSpec,
        channel_cost: Mapping[Channel, float],
        seed: int,
        *,
        days: int = HORIZON_DAYS,
    ) -> None:
        self.data = data
        self.world = world
        self.channel_cost = np.array([float(channel_cost[c]) for c in CHANNELS], dtype=np.float64)
        self.days = days
        self.n = data.n
        self.customer_ids = data.customer_ids
        self._index = data.index_of()
        self.unit_margin = MARGIN * data.value
        self.base = data.base_rate
        self.width = days + TAIL_DAYS + 7
        """Days the effect arrays cover: every day a contact sent in the run can still touch."""
        self.at_stake = margin_at_stake(data, self.width)
        self.plant(seed)

    def plant(self, seed: int) -> None:
        """(Re)draw the hidden effect from `seed` and forget every contact."""
        self.seed = seed
        self.effect = plant_effect(self.world, self.data.features, seed)
        self.rel = self.effect.rel
        self.counts = np.zeros((self.n, self.days + 3), dtype=np.uint8)
        self.incremental = np.zeros((self.n, self.width), dtype=np.float64)
        """Chance of one extra purchase, per customer and day (the world draws it, capped at 0.95)."""
        self.removal = np.zeros((self.n, self.width), dtype=np.float64)
        """Chance a real receipt is removed, per customer and day (the world draws it, capped at 1)."""
        self.ledger = ContactLedger()

    # --- the GroundTruth surface ---------------------------------------------------------------

    def index(self, customer_id: str) -> int:
        return self._index[customer_id]

    def persona(self, customer_id: str) -> str:
        return "sleeping_dog" if self.rel[self.index(customer_id)] < 0 else "normal"

    def is_sleeping_dog(self, customer_id: str) -> bool:
        return bool(self.rel[self.index(customer_id)] < 0)

    def seconds(self, ts: datetime) -> float:
        return (ts - self.data.start).total_seconds()

    def day_of(self, ts: datetime) -> int:
        return max(0, math.floor(self.seconds(ts) / SECONDS_PER_DAY))

    def _recent(self, idx: IntArray, day: int, span: int) -> FloatArray:
        lo = max(0, day - span + 1)
        return np.asarray(self.counts[idx, lo : day + 1].sum(axis=1), dtype=np.float64)

    def effects(self, idx: IntArray, channel: Channel | IntArray, day: int | IntArray) -> ContactEffects:
        """What a contact on `day` to each customer in `idx` is worth, given the contacts so far."""
        w = self.world
        ch = (
            np.full(idx.size, CHANNEL_INDEX[channel], dtype=np.int64)
            if isinstance(channel, Channel)
            else channel
        )
        days = np.full(idx.size, day, dtype=np.int64) if isinstance(day, int) else day
        k14 = np.empty(idx.size)
        k7 = np.empty(idx.size)
        for d in np.unique(days).tolist():
            rows = np.flatnonzero(days == d)
            k14[rows] = self._recent(idx[rows], int(d), EFFECT_DAYS)
            k7[rows] = self._recent(idx[rows], int(d), 7)
        r = self.rel[idx]
        rel = r * np.where(ch == CHANNEL_INDEX[Channel.EMAIL], w.email_effect, 1.0)
        rel_eff = np.where(rel > 0, rel * w.fatigue**k14, rel)
        purchases = rel_eff * self.base[idx] * EFFECT_DAYS
        pos, neg = np.maximum(rel_eff, 0.0), np.maximum(-rel_eff, 0.0)
        extra = np.zeros((idx.size, OFFSETS.size))
        removal = np.zeros((idx.size, OFFSETS.size))
        extra[:, :EFFECT_DAYS] = (pos * self.base[idx] * EFFECT_DAYS)[:, None] * WEIGHTS[None, :]
        removal[:, :EFFECT_DAYS] = (neg * EFFECT_DAYS)[:, None] * WEIGHTS[None, :]
        removal[:, EFFECT_DAYS:] = (w.pull_forward * pos * EFFECT_DAYS / PULL_FORWARD_DAYS)[:, None]
        rows = idx[:, None]
        cols = np.minimum(days[:, None] + OFFSETS[None, :], self.width - 1)
        unit = self.unit_margin[idx]
        gain_by_day = _capped_gain(self.incremental[rows, cols], extra, MAX_INCREMENTAL_PROB) * unit[:, None]
        loss_by_day = _capped_gain(self.removal[rows, cols], removal, 1.0) * self.at_stake[rows, cols]
        gain, loss = gain_by_day.sum(axis=1), loss_by_day.sum(axis=1)
        gross = gain - loss
        gross_7d = gain_by_day[:, :7].sum(axis=1) - loss_by_day[:, :7].sum(axis=1)
        p_unsub = np.minimum(
            w.unsubscribe_base
            * (1.0 + w.unsubscribe_per_contact * k7)
            * np.where(r < 0, w.dog_unsubscribe_multiplier, 1.0),
            MAX_UNSUBSCRIBE,
        )
        unsub_cost = p_unsub * (UNSUBSCRIBE_COST + COMPLAINT_SHARE * COMPLAINT_COST)
        cost = self.channel_cost[ch]
        return ContactEffects(
            rel_eff,
            purchases,
            extra,
            removal,
            gain,
            loss,
            gross,
            gross_7d,
            cost,
            p_unsub,
            unsub_cost,
            gross - cost - unsub_cost,
        )

    def net(self, idx: IntArray, action: Action, now: datetime) -> FloatArray:  # noqa: ARG002
        """`expected_net` of one action for many customers: the exact expected margin of the contact (all
        14 + 30 days of it) less its cost and its expected opt-out cost, given the contacts made so far."""
        return self.effects(idx, action.channel, self.day_of(action.send_at)).net

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        return float(self.net(np.array([self.index(customer_id)], dtype=np.int64), action, now)[0])

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        best: Action | None = None
        best_v = 0.0
        for a in candidates:
            v = self.expected_net(customer_id, a, now)
            if v > best_v:
                best, best_v = a, v
        return best

    # --- contacts ----------------------------------------------------------------------------------

    def register(
        self, idx: IntArray, channel: IntArray, delivered: BoolArray, day: IntArray
    ) -> ContactEffects:
        """Record a round of contacts (at most one per customer) and return their exact effects, computed
        before this round's own contacts are counted. A delivered contact is added to the arrays the world draws
        from; an undelivered one costs its channel price only."""
        eff = self.effects(idx, channel, day)
        self.ledger.add(idx, channel, delivered, day, eff)
        live = np.flatnonzero(delivered)
        np.add.at(self.counts, (idx[live], np.minimum(day[live], self.counts.shape[1] - 1)), 1)
        rows = idx[live][:, None]
        cols = np.minimum(day[live][:, None] + OFFSETS[None, :], self.width - 1)
        np.add.at(self.incremental, (rows, cols), eff.extra[live])
        np.add.at(self.removal, (rows, cols), eff.removal[live])
        return eff
