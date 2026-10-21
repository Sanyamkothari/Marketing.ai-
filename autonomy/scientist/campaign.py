"""A campaign the shop already ran, as the Scientist reads it: one row per customer.

Arm 0 is "no contact"; arms 1..k are messages. Each row says which arm the customer got and the
probability the shop's design gave that arm (`propensity`: a randomised campaign's design, or a
propensity-logged one's logged probabilities; None when nobody logged it, and then nothing can be
proven from the campaign). `converted` and `margin` are what the customer did in the campaign's
window; the reward of a row is its margin minus the contact cost of the arm it got. `features` are
what was known BEFORE the campaign (never an outcome), and `behaviour` is the optional four-column
purchase-history block of `autonomy.uplift.features.BEHAVIOUR`.

Two ways in: `Campaign(...)` directly (a bench or a connector that already has rows), or
`campaign_from_history`, which reads the latest closed campaign out of the shop's events and states
with the same convention the agent's uplift warm start uses (`autonomy.uplift.campaign`).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import Channel, ContentItem, CustomerState, Decision
from autonomy.proof.replay.datasets import LoggedBandit
from autonomy.uplift.campaign import CampaignTable, campaign_table
from autonomy.uplift.features import BEHAVIOUR, EventLike

__all__ = [
    "Campaign",
    "attributes_of",
    "campaign_from_history",
    "campaign_from_table",
    "unit_hash",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

NUMERIC_SHARE = 0.98
"""A text feature is read as a number when at least this share of its values is one."""
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def unit_hash(ids: Sequence[str], salt: str) -> FloatArray:
    """A number in [0, 1) per id, from BLAKE2b of `salt:id`: the same on every run and machine."""
    out = np.empty(len(ids), dtype=np.float64)
    prefix = f"{salt}:".encode()
    for i, cid in enumerate(ids):
        digest = hashlib.blake2b(prefix + str(cid).encode(), digest_size=8).digest()
        out[i] = int.from_bytes(digest, "big") / 2.0**64
    return out


def _cell(v: object) -> str | None:
    """A feature value as the text a `CustomerState.preference` holds (None when missing)."""
    if v is None:
        return None
    if isinstance(v, float | np.floating):
        return format(float(v), ".6g") if math.isfinite(float(v)) else None
    if isinstance(v, int | np.integer):
        return str(int(v))
    text = str(v)
    return text if text and text != "nan" else None


def attributes_of(frame: pd.DataFrame) -> tuple[dict[str, str], ...]:
    """Each row of a feature frame as text, the way a `CustomerState.preferences` holds it."""
    cols = [str(c) for c in frame.columns]
    out: list[dict[str, str]] = []
    for row in frame.itertuples(index=False, name=None):
        prefs: dict[str, str] = {}
        for col, v in zip(cols, row, strict=True):
            text = _cell(v)
            if text is not None:
                prefs[col] = text
        out.append(prefs)
    return tuple(out)


@dataclass(frozen=True, eq=False)
class Campaign:
    """One past campaign, one row per customer (see the module docstring)."""

    customer_ids: tuple[str, ...]
    arm: IntArray
    propensity: FloatArray | None
    arm_names: tuple[str, ...]
    arm_cost: tuple[float, ...]
    converted: FloatArray
    margin: FloatArray
    features: pd.DataFrame
    behaviour: FloatArray | None = None
    times: FloatArray | None = None
    """Unix seconds of each row's contact (or the campaign's reference time for a control row)."""
    randomised: bool = True
    source: str = "campaign"
    started_at: datetime = _EPOCH

    def __post_init__(self) -> None:
        n = len(self.customer_ids)
        if len(self.arm_names) < 2:
            raise ValueError("a campaign needs arm 0 (no contact) and at least one message arm")
        if len(self.arm_cost) != len(self.arm_names):
            raise ValueError("arm_cost needs one value per arm")
        if self.arm_cost[0] != 0.0:
            raise ValueError("arm 0 is 'no contact' and costs nothing")
        if any(not math.isfinite(c) or c < 0.0 for c in self.arm_cost):
            raise ValueError("arm costs must be finite and not negative")
        arrays: tuple[tuple[str, FloatArray | IntArray | None], ...] = (
            ("arm", self.arm),
            ("converted", self.converted),
            ("margin", self.margin),
            ("propensity", self.propensity),
            ("times", self.times),
        )
        for name, arr in arrays:
            if arr is not None and (arr.ndim != 1 or len(arr) != n):
                raise ValueError(f"{name} must hold one value per customer ({n})")
        if len(self.features) != n:
            raise ValueError(f"features must have one row per customer ({n})")
        if self.behaviour is not None and self.behaviour.shape != (n, len(BEHAVIOUR)):
            raise ValueError(f"behaviour must be {n} x {len(BEHAVIOUR)}")
        if n and (self.arm.min() < 0 or self.arm.max() >= len(self.arm_names)):
            raise ValueError("arm must index arm_names")
        if len(set(self.customer_ids)) != n:
            raise ValueError("a customer appears once in a campaign")
        for name, arr in (("converted", self.converted), ("margin", self.margin)):
            if not np.all(np.isfinite(arr)):
                raise ValueError(f"{name} has a missing or infinite value")
        if self.propensity is not None and (
            not np.all(np.isfinite(self.propensity))
            or np.any(self.propensity <= 0)
            or np.any(self.propensity > 1)
        ):
            raise ValueError("every propensity must be a finite number in (0, 1]")

    # --- shape ------------------------------------------------------------------------------------

    @property
    def n(self) -> int:
        return len(self.customer_ids)

    @property
    def n_arms(self) -> int:
        return len(self.arm_names)

    @property
    def identified(self) -> bool:
        """Whether every row's chance of its arm was logged: the only campaigns we can prove from."""
        return self.propensity is not None

    def arm_counts(self) -> IntArray:
        out: IntArray = np.bincount(self.arm, minlength=self.n_arms).astype(np.int64)
        return out

    def reward(self) -> FloatArray:
        """Profit of each row: its margin less the contact cost of the arm it got."""
        cost = np.asarray(self.arm_cost, dtype=np.float64)[self.arm]
        out: FloatArray = self.margin - cost
        return out

    def margin_per_conversion(self, arm: int | None = None) -> float:
        """Mean margin of a customer who converted (in one arm, else in all); 0 when nobody did."""
        mask = self.converted > 0
        if arm is not None:
            mask = mask & (self.arm == arm)
        return float(self.margin[mask].mean()) if mask.any() else 0.0

    # --- views ------------------------------------------------------------------------------------

    def take(self, rows: npt.ArrayLike) -> Campaign:
        """The campaign restricted to these row positions (in the order given)."""
        idx = np.asarray(rows, dtype=np.int64)
        return Campaign(
            customer_ids=tuple(self.customer_ids[int(i)] for i in idx),
            arm=self.arm[idx],
            propensity=None if self.propensity is None else self.propensity[idx],
            arm_names=self.arm_names,
            arm_cost=self.arm_cost,
            converted=self.converted[idx],
            margin=self.margin[idx],
            features=self.features.iloc[idx].reset_index(drop=True),
            behaviour=None if self.behaviour is None else self.behaviour[idx],
            times=None if self.times is None else self.times[idx],
            randomised=self.randomised,
            source=self.source,
            started_at=self.started_at,
        )

    def logged(self, reward_name: str = "profit") -> LoggedBandit:
        """The rows as `autonomy.proof.replay`'s logged bandit (needs logged propensities)."""
        if self.propensity is None:
            raise ValueError("no propensities were logged for this campaign, so it cannot be replayed")
        return LoggedBandit(
            source=self.source,
            contexts=self.features,
            actions=self.arm.astype(np.int64),
            propensity=self.propensity.astype(np.float64),
            reward=self.reward(),
            action_names=self.arm_names,
            reward_name=reward_name,
        )

    @cached_property
    def attributes(self) -> tuple[dict[str, str], ...]:
        """Each row's features as text (what folding one PREFERENCE event per column gives)."""
        return attributes_of(self.features)

    @cached_property
    def behaviour_matrix(self) -> FloatArray:
        """The behaviour block, zeros (a constant, so no column) when the campaign has none."""
        if self.behaviour is not None:
            return self.behaviour
        return np.zeros((self.n, len(BEHAVIOUR)), dtype=np.float64)


# --- from the shop's events and states -----------------------------------------------------------


def _numeric_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Columns that read as numbers (at least `NUMERIC_SHARE` of the filled values) become numeric."""
    out = frame.copy()
    for col in out.columns:
        if pd.api.types.is_numeric_dtype(out[col]):
            continue
        as_num = pd.to_numeric(out[col], errors="coerce")
        filled = out[col].notna().sum()
        if filled and as_num.notna().sum() >= NUMERIC_SHARE * filled:
            out[col] = as_num
    return out


def campaign_from_table(
    table: CampaignTable,
    *,
    arm_cost: Sequence[float],
    margin_rate: float = 1.0,
    source: str = "history",
) -> Campaign:
    """A `CampaignTable` (the history's latest closed campaign) as a `Campaign`.

    `arm_cost[k]` is the cost of contact for arm k (1..n; arm 0 is free), `margin_rate` turns the
    revenue the customers spent into margin. An inferred control row of a propensity-logged campaign
    has no known propensity: the campaign is then returned as not identified (propensity None)."""
    if len(arm_cost) != table.n_arms:
        raise ValueError("arm_cost needs one value per message arm of the campaign")
    cols = sorted({k for row in table.attributes for k in row})
    data: dict[str, list[str | None]] = {c: [row.get(c) for row in table.attributes] for c in cols}
    frame = _numeric_columns(pd.DataFrame(data, index=range(table.n)))
    for j, name in enumerate(BEHAVIOUR):
        frame[f"behaviour_{name}"] = table.behaviour[:, j]
    times = np.full(table.n, table.started_at.timestamp(), dtype=np.float64)
    known = bool(np.all(np.isfinite(table.propensity)))
    return Campaign(
        customer_ids=table.customer_ids,
        arm=table.arm.astype(np.int64),
        propensity=table.propensity.astype(np.float64) if known else None,
        arm_names=("no message", *table.arm_keys),
        arm_cost=(0.0, *(float(c) for c in arm_cost)),
        converted=table.y.astype(np.float64),
        margin=(margin_rate * table.revenue).astype(np.float64),
        features=frame,
        behaviour=table.behaviour.astype(np.float64),
        times=times,
        randomised=table.randomised,
        source=source,
        started_at=table.started_at,
    )


def campaign_from_history(
    events_by_customer: Mapping[str, Sequence[EventLike]],
    states: Mapping[str, CustomerState],
    content_by_id: Mapping[str, ContentItem],
    now: datetime,
    *,
    costs: Mapping[Channel, float],
    margin_rate: float = 1.0,
    horizon_days: float = 7.0,
    decisions: Iterable[Decision] = (),
    exclude: Container[str] = frozenset(),
) -> tuple[Campaign | None, list[str]]:
    """The latest closed past campaign in the shop's history as a `Campaign`, with plain notes.

    Same convention as the agent's uplift warm start (`autonomy.uplift.campaign_table`): logged
    sends only, the control group explicit (holdout 'do nothing' decisions) or inferred. None, with
    the reason in the notes, when the history holds no usable campaign. Pure: time is passed in."""
    table, notes = campaign_table(
        events_by_customer,
        states,
        content_by_id,
        now,
        horizon_days=horizon_days,
        decisions=decisions,
        exclude=exclude,
    )
    if table is None:
        return None, notes
    cost_of = [float(costs.get(table.channel, 0.0))] * table.n_arms
    campaign = campaign_from_table(table, arm_cost=cost_of, margin_rate=margin_rate)
    if not campaign.identified:
        notes = [*notes, "the control rows' propensities are unknown: nothing can be proven from it"]
    return campaign, notes
