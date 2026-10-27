"""`uplift_waves`: the uplift-threshold method (`autonomy.decide.uplift_threshold`) run as repeated waves.

The one-shot bench fits the method once on a past randomised campaign. Here it has to learn its own data:

* **Turns.** Every customer has a fixed weekday turn (a hash of the id). On their turn they are considered once;
  nobody is contacted more than once a week by this policy.
* **A randomised slice.** On a customer's turn, with probability `slice_share` (10%) they join the slice, if both
  channels can reach them: they are given no message, an SMS or an e-mail with equal chance (propensity 1/3) and
  then left alone for the 14-day outcome window, so the window is clean. Those rows are the only data the model is
  fitted on, so it never learns from its own choices.
* **Weekly refit.** On the first day of each week from the third on, the method is refitted on every slice row
  whose 14-day window has closed: a cross-fitted uplift model, a cost threshold, and the gate against the best
  simple rule (send none, send all of an arm, a segment rule) (`fit_uplift_threshold`). The model decides
  only when the gate says its lower bound beats the best simple rule; otherwise that rule decides. Before the
  first fit the policy sends nothing outside the slice.
* **Everything it uses is observed**: its features are read from the customer's folded state (purchases, spend,
  recency, tenure, age, gender, recent contacts), its outcomes from the purchase events it is shown.

Choices the method did not ask for, stated so they can be questioned: the outcome is the number of purchases in
the 14 days after the turn, capped at 4 and divided by 4 (the method's label is a probability, and a 0/1 label
saturates for frequent buyers), priced at 4 x the customer's own mean margin per purchase; the cost of an arm is
its channel price only. It knows nothing of fatigue or pull-forward, which is the point of the benchmark.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import (
    NO_OFFER,
    Action,
    Brief,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Format,
    Group,
    Reward,
    stable_id,
)
from autonomy.decide.uplift_threshold import UpliftData, UpliftThresholdPolicy, fit_uplift_threshold
from autonomy.gym.baselines import next_local_time, send_hour, usable_content, weekly_cap
from autonomy.gym.score import PolicyFactory, SimWorld
from autonomy.gym.waves.config import CHANNEL_COST, EFFECT_DAYS
from autonomy.gym.waves.data import unit_hash
from autonomy.interfaces import Policy

FloatArray = npt.NDArray[np.float64]

SALT = "autonomy-waves-v1:uplift"
ARM_CHANNELS: tuple[Channel | None, ...] = (None, Channel.SMS, Channel.EMAIL)
ARM_NAMES: tuple[str, ...] = ("no message", "sms", "email")
OUTCOME_CAP = 4.0
MIN_ROWS = 400
SEGMENT_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("recency_days",),
    ("n_purchases",),
    ("mean_receipt",),
    ("age",),
    ("tenure_days",),
    ("n_purchases", "mean_receipt"),
    ("recency_days", "n_purchases"),
)


@dataclass
class _Row:
    """One slice assignment: who, when, which arm, and what was known at the time."""

    cid: str
    day: int
    arm: int
    attrs: dict[str, str]
    feats: dict[str, float]
    margin_per_purchase: float


def _text(v: float) -> str:
    return format(float(v), ".6g")


class UpliftWavesPolicy:
    """See the module docstring."""

    version = "uplift-waves-v1"

    def __init__(
        self,
        brief: Brief,
        content: Sequence[ContentItem],
        *,
        slice_share: float = 0.10,
        cadence_days: int = 7,
        outcome_days: int = EFFECT_DAYS,
        first_fit_day: int = 21,
        hour: int = 14,
        costs: Mapping[Channel, float] | None = None,
    ) -> None:
        self.brief = brief
        self.content = tuple(content)
        self.slice_share = slice_share
        self.cadence = cadence_days
        self.outcome_days = outcome_days
        self.first_fit_day = first_fit_day
        self.hour = hour
        cost = dict(CHANNEL_COST if costs is None else costs)
        self.arm_cost = np.array([0.0, cost[Channel.SMS], cost[Channel.EMAIL]], dtype=np.float64)
        self._start: datetime | None = None
        self._buys: dict[str, list[tuple[int, float]]] = {}
        self._rows: list[_Row] = []
        self._quarantine: dict[str, int] = {}
        self._turn: dict[str, int] = {}
        self._plan: dict[str, Action | None] = {}
        self._plan_day = -1
        self._fit_day = -1
        self._fit: UpliftThresholdPolicy | None = None
        self._world_seed = 0
        self._margin_sum = 0.0
        self._margin_n = 0
        self.fits: list[dict[str, object]] = []
        """One entry per refit: day, rows used, what the gate chose and why (for the report)."""
        self.slice_rows = 0

    # --- what it observes --------------------------------------------------------------------------

    def nightly(
        self,
        decisions: Sequence[Decision],  # noqa: ARG002
        events: Mapping[str, Sequence[Event]],
        now: datetime,
    ) -> None:
        """Learn from the purchases the day brought (the Gym calls this every simulated morning)."""
        if self._start is None:
            self._start = now
        start = self._start
        for cid, evs in events.items():
            for e in evs:
                if e.type is not EventType.PURCHASE:
                    continue
                margin = float(e.margin or 0.0)
                self._margin_sum += margin
                self._margin_n += 1
                if e.ts >= start:
                    self._buys.setdefault(cid, []).append(
                        (math.floor((e.ts - start).total_seconds() / 86_400.0), margin)
                    )

    def learn(self, rewards: Sequence[Reward], decisions: Mapping[str, Decision]) -> None:  # noqa: ARG002
        return None

    # --- features ------------------------------------------------------------------------------------

    def _population_margin(self) -> float:
        return self._margin_sum / self._margin_n if self._margin_n else 0.0

    def _featurize(self, s: CustomerState, now: datetime) -> tuple[dict[str, str], dict[str, float], float]:
        recency = (
            (now - s.last_purchase_at).total_seconds() / 86_400.0 if s.last_purchase_at is not None else 999.0
        )
        tenure = (now - s.first_seen_at).total_seconds() / 86_400.0 if s.first_seen_at is not None else 0.0
        mean_receipt = s.revenue / s.purchases if s.purchases else 0.0
        age = float(s.preferences.get("age", "nan"))
        gender = s.preferences.get("gender", "U")
        feats = {
            "recency_days": recency,
            "n_purchases": float(s.purchases),
            "mean_receipt": mean_receipt,
            "spend": s.revenue,
            "age": age,
            "tenure_days": tenure,
            "contacts_30d": float(s.contacts_30d),
            "gender": {"F": 0.0, "M": 1.0}.get(gender, 0.5),
        }
        attrs = {k: _text(v) for k, v in feats.items() if math.isfinite(v)}
        attrs["gender"] = gender
        per_purchase = s.margin / s.purchases if s.purchases else self._population_margin()
        return attrs, feats, per_purchase

    # --- the week ------------------------------------------------------------------------------------

    def _turn_of(self, cid: str) -> int:
        t = self._turn.get(cid)
        if t is None:
            t = int(unit_hash([cid], f"{SALT}:turn")[0] * self.cadence)
            self._turn[cid] = t
        return t

    def _outcome(self, row: _Row) -> tuple[float, float]:
        """Purchases and margin in the days after the turn (day + 1 .. day + outcome_days)."""
        lo, hi = row.day + 1, row.day + self.outcome_days
        count, margin = 0, 0.0
        for d, m in self._buys.get(row.cid, ()):
            if lo <= d <= hi:
                count += 1
                margin += m
        return float(count), margin

    def _refit(self, day: int) -> None:
        closed = [r for r in self._rows if r.day + self.outcome_days < day]
        if len(closed) < MIN_ROWS:
            return
        counts = np.zeros(len(closed))
        margin = np.zeros(len(closed))
        for k, r in enumerate(closed):
            counts[k], margin[k] = self._outcome(r)
        frame = pd.DataFrame([r.feats for r in closed])
        data = UpliftData(
            customer_ids=np.array([r.cid for r in closed], dtype=object),
            arm=np.array([r.arm for r in closed], dtype=np.int64),
            propensity=np.full(len(closed), 1.0 / len(ARM_NAMES)),
            converted=np.minimum(counts, OUTCOME_CAP) / OUTCOME_CAP,
            margin=margin,
            arm_cost=self.arm_cost,
            arm_names=ARM_NAMES,
            features=frame,
            attributes=tuple(r.attrs for r in closed),
            conversion_value=OUTCOME_CAP * np.array([r.margin_per_purchase for r in closed]),
        )
        try:
            fitted = fit_uplift_threshold(data, segment_candidates=SEGMENT_CANDIDATES, seed=0)
        except ValueError as exc:
            self.fits.append({"day": day, "rows": len(closed), "chose": "no fit", "reason": str(exc)})
            return
        self._fit = fitted
        self.fits.append(
            {
                "day": day,
                "rows": len(closed),
                "chose": fitted.gate.chose,
                "lb": fitted.gate.lb,
                "model_contact": fitted.gate.model_contact,
                "reason": fitted.gate.reason,
            }
        )

    def _action(self, arm: int, state: CustomerState, ctx: DecisionContext) -> Action | None:
        channel = ARM_CHANNELS[arm]
        if channel is None:
            return None
        for brief in ctx.briefs:
            if channel not in brief.envelope.channels:
                continue
            h = send_hour(self.hour, brief.envelope)
            if h is None:
                continue
            item = next(
                (c for c in usable_content(ctx.content, brief, channel) if c.offer_id == NO_OFFER.offer_id),
                None,
            )
            return Action(
                goal_id=brief.goal_id,
                channel=channel,
                offer_id=NO_OFFER.offer_id,
                content_id=item.content_id if item is not None else None,
                format=item.format if item is not None else Format.TEXT,
                send_at=next_local_time(ctx.now, state.timezone, h),
            )
        return None

    def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
        """Plan today for the customers whose turn it is: slice assignments and the deployed rule or model."""
        if self._start is None:
            self._start = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = math.floor((ctx.now - self._start).total_seconds() / 86_400.0)
        self._world_seed = ctx.seed // 100_003
        self._plan, self._plan_day = {}, day
        if day % 7 == 0 and day >= self.first_fit_day and day != self._fit_day:
            self._fit_day = day
            self._refit(day)
        cap = weekly_cap(ctx.briefs)
        week = day // self.cadence
        batch: list[tuple[CustomerState, dict[str, str], dict[str, float], float]] = []
        for s in states:
            cid = s.customer_id
            if self._turn_of(cid) != day % self.cadence:
                continue
            if s.group is Group.HOLDOUT or s.open_complaint or s.contacts_7d >= cap:
                continue
            reach = set(s.contactable_channels())
            if not (Channel.SMS in reach or Channel.EMAIL in reach):
                continue
            if day < self._quarantine.get(cid, -1):
                continue
            attrs, feats, per_purchase = self._featurize(s, ctx.now)
            both = Channel.SMS in reach and Channel.EMAIL in reach
            draw = unit_hash([cid, cid], f"{SALT}:slice:{self._world_seed}:{week}")
            if both and draw[0] < self.slice_share:
                arm = int(unit_hash([cid], f"{SALT}:arm:{self._world_seed}:{week}")[0] * len(ARM_NAMES))
                arm = min(arm, len(ARM_NAMES) - 1)
                self._rows.append(_Row(cid, day, arm, attrs, feats, per_purchase))
                self.slice_rows += 1
                self._quarantine[cid] = day + self.outcome_days + 1
                self._plan[cid] = self._action(arm, s, ctx)
            elif self._fit is not None:
                batch.append((s, attrs, feats, per_purchase))
        if not batch or self._fit is None:
            return
        attributes = [b[1] for b in batch]
        frame = pd.DataFrame([b[2] for b in batch])
        conv = OUTCOME_CAP * np.array([b[3] for b in batch], dtype=np.float64)
        if self._fit.model is not None:
            nets = self._fit.net(attributes, conv)
            assert nets is not None
            reach_mask = np.array(
                [
                    [Channel.SMS in b[0].contactable_channels(), Channel.EMAIL in b[0].contactable_channels()]
                    for b in batch
                ]
            )
            nets = np.where(reach_mask, nets, -np.inf)
            best = nets.argmax(axis=1)
            arms = np.where(nets[np.arange(len(batch)), best] > 0, best + 1, 0)
        else:
            arms = self._fit.arms(attributes, frame, conv)
        for (s, _a, _f, _p), arm in zip(batch, arms.tolist(), strict=True):
            channel = ARM_CHANNELS[int(arm)]
            if channel is not None and channel in s.contactable_channels():
                self._plan[s.customer_id] = self._action(int(arm), s, ctx)

    def decide(self, state: CustomerState, ctx: DecisionContext) -> Decision:
        action = self._plan.get(state.customer_id) if self._plan_day >= 0 else None
        acting = action is not None and state.group is not Group.HOLDOUT
        return Decision(
            decision_id=stable_id("dec", state.customer_id, ctx.now.isoformat(), self.version),
            customer_id=state.customer_id,
            made_at=ctx.now,
            kind=DecisionKind.ACT if acting else DecisionKind.NOTHING,
            action=action if acting else None,
            group=state.group,
            probability=1.0,
            reason=(
                f"Wave: {action.channel.value}."
                if acting and action is not None
                else "Not this customer's turn, or no send chosen."
            ),
            policy_version=self.version,
        )

    def diagnostics(self) -> dict[str, object]:
        """What the policy did, for the report: slice size, every refit and what the gate chose."""
        return {"slice_rows": self.slice_rows, "fits": list(self.fits)}


def uplift_waves_factory(brief: Brief, content: Sequence[ContentItem]) -> PolicyFactory:
    """A fresh policy per run (it learns, so it must not carry anything from one seed to the next)."""

    def factory(world: SimWorld) -> Policy:  # noqa: ARG001
        return UpliftWavesPolicy(brief, content)

    return factory
