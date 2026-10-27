"""`uplift_waves`: the uplift-threshold method (`autonomy.decide.uplift_threshold`) run as repeated waves.

The one-shot bench fits the method once on a past randomised campaign. Here it has to learn its own data:

* **A randomised slice.** Every customer has a fixed weekday turn (a hash of the id). On their turn, with probability
  `slice_share` (10%), a reachable customer joins the slice: they are given no message or one of the messages that
  can reach them, with equal chance (propensity 1/3 with both channels, 1/2 with one; the design probability is
  stored per row), and then left alone for the 14-day outcome window, so the window is clean. Those rows are the
  only data the model is fitted on, so it never learns from its own choices.
* **Weekly refit.** On the first day of each week from the third on, the method is refitted on every slice row
  whose 14-day window has closed: a cross-fitted uplift model, a cost threshold, and the gate against the best
  simple rule (send none, send all of an arm, a segment rule) (`fit_uplift_threshold`). The model decides
  only when the gate says its lower bound beats the best simple rule; otherwise that rule decides.
* **Deployment, every day.** Outside the slice, every customer not in an outcome window and under the weekly cap is
  given what the deployed model or rule says, every day (so "send all SMS" sends as often as the cap allows, like
  `send_all_sms`). An arm the customer cannot receive is replaced by the best one they can (the model) or by no
  message (a rule). Before the first fit, and while no fit has succeeded, the declared default is `weekly_sms`: one
  SMS a week to everyone SMS reaches.
* **Everything it uses is observed**: its features are read from the customer's folded state (purchases, spend,
  recency, tenure, age, gender, recent contacts), its outcomes from the purchase events it is shown.

Adapter choices the method did not ask for, stated so they can be questioned:

* the label is the number of purchases in the 14 days after the turn, divided by a cap and capped at 1 (the
  method's label is a probability; a 0/1 label saturates for frequent buyers). The cap is set at each refit to the
  99th percentile of those counts in the rows fitted on (at least 4), so heavy buyers, who carry the largest effect
  in roubles, are not cut off; a purchase is priced at cap x the customer's own mean margin per purchase;
* an arm's cost is its channel price only;
* it is fitted on the slice only, not on everything observed: the deployed sends are chosen by the policy itself
  (propensity 0 or 1), so they cannot be reweighted into unconfounded data;
* the e-mail arm is only ever randomised to customers e-mail reaches, so when the gate judges a rule or model that
  would e-mail an SMS-only customer, it reads that customer's e-mail value from the model, not from data.

It knows nothing of fatigue or pull-forward, which is the point of the benchmark (`contacts_30d` is a feature, so it
can learn from the slice how recent contacts change the outcome).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

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
MIN_OUTCOME_CAP = 4.0
OUTCOME_QUANTILE = 0.99
MIN_ROWS = 400
DEFAULT_RULE = "weekly_sms"
"""What is deployed before the first successful fit: one SMS a week to everyone SMS reaches."""
DEFAULT_GAP = timedelta(days=7)
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
    propensity: float
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
        self._cap = MIN_OUTCOME_CAP
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
        cap = max(MIN_OUTCOME_CAP, float(math.ceil(np.quantile(counts, OUTCOME_QUANTILE))))
        frame = pd.DataFrame([r.feats for r in closed])
        data = UpliftData(
            customer_ids=np.array([r.cid for r in closed], dtype=object),
            arm=np.array([r.arm for r in closed], dtype=np.int64),
            propensity=np.array([r.propensity for r in closed], dtype=np.float64),
            converted=np.minimum(counts, cap) / cap,
            margin=margin,
            arm_cost=self.arm_cost,
            arm_names=ARM_NAMES,
            features=frame,
            attributes=tuple(r.attrs for r in closed),
            conversion_value=cap * np.array([r.margin_per_purchase for r in closed]),
        )
        try:
            fitted = fit_uplift_threshold(data, segment_candidates=SEGMENT_CANDIDATES, seed=0)
        except ValueError as exc:
            self.fits.append({"day": day, "rows": len(closed), "chose": "no fit", "reason": str(exc)})
            return
        self._fit, self._cap = fitted, cap
        self.fits.append(
            {
                "day": day,
                "rows": len(closed),
                "outcome_cap": cap,
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
        """Plan today: slice assignments for the customers whose turn it is, and the deployed model or rule (the
        default before the first fit) for everyone else who may be contacted today."""
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
        batch: list[tuple[CustomerState, dict[str, str], dict[str, float], float, tuple[int, ...]]] = []
        for s in states:
            cid = s.customer_id
            if s.group is Group.HOLDOUT or s.open_complaint or s.contacts_7d >= cap:
                continue
            if day < self._quarantine.get(cid, -1):
                continue
            channels = s.contactable_channels()
            reach = tuple(a for a in (1, 2) if ARM_CHANNELS[a] in channels)
            if not reach:
                continue
            attrs, feats, per_purchase = self._featurize(s, ctx.now)
            if self._turn_of(cid) == day % self.cadence:
                draw = float(unit_hash([cid], f"{SALT}:slice:{self._world_seed}:{week}")[0])
                if draw < self.slice_share:
                    arms = (0, *reach)
                    pick = float(unit_hash([cid], f"{SALT}:arm:{self._world_seed}:{week}")[0])
                    arm = arms[min(int(pick * len(arms)), len(arms) - 1)]
                    self._rows.append(_Row(cid, day, arm, 1.0 / len(arms), attrs, feats, per_purchase))
                    self.slice_rows += 1
                    self._quarantine[cid] = day + self.outcome_days + 1
                    self._plan[cid] = self._action(arm, s, ctx)
                    continue
            batch.append((s, attrs, feats, per_purchase, reach))
        if not batch:
            return
        if self._fit is None:
            for s, _a, _f, _p, reach in batch:
                recent = s.last_contact_at is not None and ctx.now - s.last_contact_at < DEFAULT_GAP
                if 1 in reach and not recent:
                    self._plan[s.customer_id] = self._action(1, s, ctx)
            return
        attributes = [b[1] for b in batch]
        frame = pd.DataFrame([b[2] for b in batch])
        conv = self._cap * np.array([b[3] for b in batch], dtype=np.float64)
        if self._fit.model is not None:
            nets = self._fit.net(attributes, conv)
            assert nets is not None
            reach_mask = np.array([[1 in b[4], 2 in b[4]] for b in batch], dtype=np.bool_)
            nets = np.where(reach_mask, nets, -np.inf)
            best = nets.argmax(axis=1)
            arms_out = np.where(nets[np.arange(len(batch)), best] > 0, best + 1, 0)
        else:
            arms_out = self._fit.arms(attributes, frame, conv)
        for (s, _a, _f, _p, reach), arm in zip(batch, arms_out.tolist(), strict=True):
            if int(arm) in reach:
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
                else "In an outcome window, at the cap, or no send chosen."
            ),
            policy_version=self.version,
        )

    def diagnostics(self) -> dict[str, object]:
        """What the policy did, for the report: slice size, the default rule, every refit and what the gate chose."""
        return {"slice_rows": self.slice_rows, "default_rule": DEFAULT_RULE, "fits": list(self.fits)}


def uplift_waves_factory(brief: Brief, content: Sequence[ContentItem]) -> PolicyFactory:
    """A fresh policy per run (it learns, so it must not carry anything from one seed to the next)."""

    def factory(world: SimWorld) -> Policy:  # noqa: ARG001
        return UpliftWavesPolicy(brief, content)

    return factory
