"""Frequency: plan weeks, not messages (plan-v3 decision 7).

Two parts, both opt-in (`build_agent_policy(frequency=...)`); with them off nothing here is called.

**Tolerance from look-alikes (`LookalikeHazard`).** `HazardModel` learns the chance a message loses the
contact (an unsubscribe or a complaint) per channel and messages this week, the same for everyone.
`LookalikeHazard` keeps that curve and multiplies it by how tired people with this profile are:

    rate(customer, channel, n) = hazard(channel, n) x crowd(profile) x person(customer)

* `crowd(profile)` is a Poisson regression with a log link on every send the agent has seen the outcome
  of (lost the contact or not), with the hazard curve as the offset, so it learns only what the profile
  adds to the curve. The features are the look-alike profile (`lookalike.profile_of`, nightly features
  included) minus the weekly count, which the curve already carries. A heavy ridge keeps a few dozen
  events from drawing a confident picture, and the multiplier is kept within `[1/max, max]`.
* `person(customer)` is a Gamma-Poisson factor on the customer's own record: their events over the
  events the crowd expected for their sends, shrunk by `person_strength` expected events. It starts at
  1 and moves as their record grows.
* Below `min_events` lost contacts the multiplier is 1 and the answer is the plain hazard's, exactly.

The attention price (`runtime.agent.ContactPrice`) reads it through `rate_for(state, channel, n)`; it
reads the plain `rate(channel, n)` of a `HazardModel` as before.

**Three-week plans (`FrequencyPlanner`).** For each customer, a small fixed set of contact plans over
the next 21 days is compared by total expected value minus the tiredness each message causes: one
message now; one now and one in 10 days; nothing for 14 days, then one; nothing. A message of a plan is
the customer's best option at that moment (the same bids the auction weighs), worth

    urgency x (uplift x value x freshness - offer cost - channel cost) - attention price

discounted by `weekly_discount` per week of waiting. A later message is priced with the hazard the
earlier ones raised: the messages counted against it are the customer's week now, fading by
`exp(-days / memory_days)`, plus each earlier message of the plan, fading the same way, so a message
sent 10 days ago still leaves the next one dearer. Both the freshness (how well a message does at that
count) and the hazard are read at that fractional count. The first message of any plan is priced
exactly as the auction prices it now.

Today's decision follows the best plan's first step, and the plan is made again every day from the
customer's state, so it bends when something happens (a purchase changes the bids; a moment raises
urgency; a reply changes the record). The first step is applied as a veto only: when the best plan
waits (nothing now, a message later) the customer is left alone today (`rules.FREQUENCY_PLAN`). When
the best plan is to send, the auction goes on as before, with its price, restraint, Thompson draw and
exploration floor; when no plan with a message beats nothing, the auction goes on too, so exploration
is not cut where no plan is worth anything. The hard rules and the weekly caps run before and after
and stay a backstop. A customer inside a journey is not planned: the journey runs its steps before the
auction, so it follows the journey.

Numbers here are design defaults (`memory_days`, `weekly_discount`, the plans, the ridge); none was
tuned to a dataset. See docs/v3/FREQUENCY.md.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Bid, Channel, CustomerState
from autonomy.decide import rules
from autonomy.decide.lookalike import BASE_NAMES, Profile, profile_of
from autonomy.decide.models import HAZARD_BASE_RATE, HAZARD_GROWTH, HAZARD_MAX_CONTACTS, HazardModel

Vec = npt.NDArray[np.float64]

# --- tolerance from look-alikes -------------------------------------------------------------------


@dataclass(frozen=True)
class ToleranceConfig:
    """Tunable numbers of `LookalikeHazard`, each a documented default and none tuned to a seed."""

    min_events: float = 20.0
    """Lost contacts (summed weight) needed before the crowd's picture is used at all."""
    slope_penalty: float = 10.0
    """Ridge on each standardised feature's effect on the log of the rate (prior sd about 0.3)."""
    intercept_penalty: float = 2.0
    """Ridge on the overall level against the hazard curve (the curve is learned from the same sends)."""
    person_strength: float = 5.0
    """The person's factor is Gamma(k, k) with k = this many expected events: a customer moves from the
    crowd as far as their own events (against what the crowd expected) outweigh it."""
    max_multiplier: float = 8.0
    """The crowd's and the person's multipliers are each kept within [1 / this, this]."""
    refit_growth: float = 0.2
    refit_min_new: int = 200
    """Refit the crowd when the sends have grown by this share and by at least this many."""
    max_rows: int = 60_000
    """Sends kept, newest first: bounds the memory and the cost of a refit."""
    steps: int = 8
    """Newton steps of a refit."""
    min_feature_share: float = 0.05
    """A nightly feature is used when this share of the sends carry it."""

    def __post_init__(self) -> None:
        if self.min_events <= 0 or self.slope_penalty <= 0 or self.intercept_penalty <= 0:
            raise ValueError("need min_events, slope_penalty and intercept_penalty > 0")
        if self.person_strength <= 0 or self.max_multiplier < 1 or self.max_rows < 1 or self.steps < 1:
            raise ValueError("need person_strength > 0, max_multiplier >= 1, max_rows >= 1 and steps >= 1")


_DROPPED = frozenset({"contacts_7d"})
"""Base columns the tolerance leaves out: the weekly count is the hazard curve's own axis."""


@dataclass
class _Send:
    customer_id: str
    profile: Profile
    channel: Channel
    contacts: int
    weight: float
    lost: bool = False


@dataclass(frozen=True)
class _Crowd:
    names: tuple[str, ...]
    """Nightly feature names, in column order after the base columns."""
    keep: npt.NDArray[np.intp]
    mean: Vec
    std: Vec
    beta: Vec
    """Intercept first, then one slope per kept column."""


class LookalikeHazard(HazardModel):
    """`HazardModel` whose answer depends on who is asked (see the module docstring).

    The plain counts (`observe_send`, `observe_event`, `rate`) work as in `HazardModel` and are what
    the caller keeps feeding. In addition `record_send` remembers a delivered message with the
    profile it was decided on, and `record_loss` marks that it cost the contact; `fit` learns the
    crowd's picture from them and the customers' own factors; `rate_for` is the personal rate.
    """

    def __init__(
        self,
        config: ToleranceConfig | None = None,
        *,
        base_rate: float = HAZARD_BASE_RATE,
        growth: float = HAZARD_GROWTH,
        max_contacts: int = HAZARD_MAX_CONTACTS,
    ) -> None:
        super().__init__(base_rate=base_rate, growth=growth, max_contacts=max_contacts)
        self.config = config or ToleranceConfig()
        # HazardModel keeps its cell counts in `_sends` and `_events`; the personal rows live apart.
        self._rows: dict[str, _Send] = {}
        self._crowd: _Crowd | None = None
        self._people: dict[str, float] = {}
        self._rows_at_fit = 0
        self._last: tuple[CustomerState, float] | None = None

    def record_send(
        self,
        key: str,
        customer_id: str,
        profile: Profile,
        channel: Channel,
        contacts_7d: int,
        weight: float = 1.0,
    ) -> None:
        """A delivered message and the profile it was decided on, under `key` (its decision id)."""
        if weight > 0 and key not in self._rows:
            self._rows[key] = _Send(customer_id, profile, channel, self._cell(contacts_7d), weight)

    def record_loss(self, key: str) -> None:
        """That message lost the contact."""
        row = self._rows.get(key)
        if row is not None:
            row.lost = True

    # --- reading ----------------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Whether the crowd's picture is in use (enough lost contacts have been seen)."""
        return self._crowd is not None

    @property
    def rows(self) -> int:
        return len(self._rows)

    def multiplier(self, state: CustomerState) -> float:
        """What this customer's tiredness is against the plain curve: the crowd's factor for their
        profile times their own; 1 until the crowd's picture is ready and for an unknown customer."""
        crowd = self._crowd
        if crowd is None:
            return 1.0
        hit = self._last
        if hit is not None and hit[0] is state:
            return hit[1]
        cap = self.config.max_multiplier
        crowd_part = float(self._crowd_factor(crowd, [profile_of(state)])[0])
        value = crowd_part * self._people.get(state.customer_id, 1.0)
        value = min(max(value, 1.0 / cap), cap)
        self._last = (state, value)
        return value

    def rate_for(self, state: CustomerState, channel: Channel, contacts_7d: int) -> float:
        """The chance the next message on `channel` loses *this* customer, given this week's count."""
        base = self.rate(channel, contacts_7d)
        if self._crowd is None:
            return base
        return min(base * self.multiplier(state), 0.5)

    # --- learning ---------------------------------------------------------------------------------

    def fit(self) -> bool:
        """Learn the crowd's picture (when the sends have grown) and every customer's own factor.

        Returns whether the crowd was refitted. With fewer than `min_events` lost contacts nothing is
        used and every answer is the plain curve's."""
        cfg = self.config
        if len(self._rows) > cfg.max_rows:
            drop = len(self._rows) - cfg.max_rows
            for key in list(self._rows)[:drop]:
                del self._rows[key]
        rows = list(self._rows.values())
        events = sum(r.weight for r in rows if r.lost)
        if events < cfg.min_events:
            self._crowd, self._people, self._last = None, {}, None
            self._rows_at_fit = len(rows)
            return False
        fresh = len(rows) - self._rows_at_fit
        refit = self._crowd is None or fresh >= max(cfg.refit_min_new, cfg.refit_growth * self._rows_at_fit)
        if refit:
            self._crowd = self._refit(rows)
            self._rows_at_fit = len(rows)
        self._people = self._person_factors(rows)
        self._last = None
        return refit

    def _base(self, rows: Sequence[_Send]) -> Vec:
        return np.asarray([self.rate(r.channel, r.contacts) for r in rows], dtype=np.float64)

    def _raw(self, profiles: Sequence[Profile], names: Sequence[str]) -> Vec:
        keep = [i for i, n in enumerate(BASE_NAMES) if n not in _DROPPED]
        raw = np.stack([p.base[keep] for p in profiles])
        if names:
            extra = np.full((len(profiles), len(names)), np.nan)
            for j, name in enumerate(names):
                for i, p in enumerate(profiles):
                    got = p.extra.get(name)
                    if got is not None:
                        extra[i, j] = got
            raw = np.hstack([raw, extra])
        return raw

    def _design(self, crowd: _Crowd, profiles: Sequence[Profile]) -> Vec:
        raw = self._raw(profiles, crowd.names)
        raw = np.where(np.isnan(raw), crowd.mean, raw)
        z = np.clip((raw[:, crowd.keep] - crowd.mean[crowd.keep]) / crowd.std[crowd.keep], -4.0, 4.0)
        return np.asarray(np.hstack([np.ones((len(profiles), 1)), z]), dtype=np.float64)

    def _crowd_factor(self, crowd: _Crowd, profiles: Sequence[Profile]) -> Vec:
        cap = math.log(self.config.max_multiplier)
        eta = self._design(crowd, profiles) @ crowd.beta
        return np.asarray(np.exp(np.clip(eta, -cap, cap)), dtype=np.float64)

    def _refit(self, rows: Sequence[_Send]) -> _Crowd:
        cfg = self.config
        counts: dict[str, int] = {}
        for r in rows:
            for name in r.profile.extra:
                counts[name] = counts.get(name, 0) + 1
        names = tuple(sorted(n for n, c in counts.items() if c >= cfg.min_feature_share * len(rows)))
        raw = self._raw([r.profile for r in rows], names)
        known = ~np.isnan(raw)
        count = np.maximum(known.sum(axis=0), 1)
        mean = np.asarray(np.where(known, raw, 0.0).sum(axis=0) / count, dtype=np.float64)
        dev = np.where(known, raw - mean, 0.0)
        std = np.asarray(np.sqrt((dev * dev).sum(axis=0) / count), dtype=np.float64)
        keep = np.flatnonzero(std > 1e-6)
        std = np.where(std > 1e-6, std, 1.0)
        shell = _Crowd(names, keep, mean, std, np.zeros(1 + len(keep)))
        x = self._design(shell, [r.profile for r in rows])
        w = np.asarray([r.weight for r in rows], dtype=np.float64)
        y = np.asarray([1.0 if r.lost else 0.0 for r in rows], dtype=np.float64)
        exposure = w * self._base(rows)
        pen = np.full(x.shape[1], cfg.slope_penalty)
        pen[0] = cfg.intercept_penalty
        cap = math.log(cfg.max_multiplier)
        beta = np.zeros(x.shape[1])
        for _ in range(cfg.steps):
            mu = exposure * np.exp(np.clip(x @ beta, -cap, cap))
            grad = x.T @ (w * y - mu) - pen * beta
            hess = (x * mu[:, None]).T @ x + np.diag(pen)
            try:
                step = np.linalg.solve(hess, grad)
            except np.linalg.LinAlgError:
                break
            beta = beta + np.clip(step, -1.0, 1.0)
            if float(np.max(np.abs(step))) < 1e-6:
                break
        return _Crowd(names, keep, mean, std, beta)

    def _person_factors(self, rows: Sequence[_Send]) -> dict[str, float]:
        crowd = self._crowd
        if crowd is None:
            return {}
        cfg = self.config
        crowd_mult = self._crowd_factor(crowd, [r.profile for r in rows])
        expected = self._base(rows) * crowd_mult
        events: dict[str, float] = {}
        due: dict[str, float] = {}
        for r, e in zip(rows, expected, strict=True):
            due[r.customer_id] = due.get(r.customer_id, 0.0) + r.weight * float(e)
            if r.lost:
                events[r.customer_id] = events.get(r.customer_id, 0.0) + r.weight
        k = cfg.person_strength
        cap = cfg.max_multiplier
        return {
            cid: min(max((k + events.get(cid, 0.0)) / (k + total), 1.0 / cap), cap)
            for cid, total in due.items()
        }


# --- three-week plans -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanSpec:
    """One contact plan: a name and the days (from today) on which it sends a message."""

    name: str
    days: tuple[float, ...]


NOTHING = "nothing"
NOW = "now"
NOW_AND_10D = "now_and_10d"
WAIT_14D = "wait_14d"
DEFAULT_PLANS: tuple[PlanSpec, ...] = (
    PlanSpec(NOTHING, ()),
    PlanSpec(NOW, (0.0,)),
    PlanSpec(NOW_AND_10D, (0.0, 10.0)),
    PlanSpec(WAIT_14D, (14.0,)),
)
"""The fixed set compared for every customer. Ties go to the earlier plan in this order."""


@dataclass(frozen=True)
class PlanConfig:
    """Tunable numbers of `FrequencyPlanner`: design defaults, none tuned to a dataset."""

    plans: tuple[PlanSpec, ...] = DEFAULT_PLANS
    horizon_days: float = 21.0
    memory_days: float = 10.0
    """A message counted against a later one fades by `exp(-days / memory_days)`."""
    weekly_discount: float = 0.98
    """A message a week later is worth this much less: purchases and moments do not wait."""

    def __post_init__(self) -> None:
        if not self.plans or self.horizon_days <= 0 or self.memory_days <= 0:
            raise ValueError("need plans, horizon_days > 0 and memory_days > 0")
        if not 0 < self.weekly_discount <= 1:
            raise ValueError("weekly_discount is in (0, 1]")
        for plan in self.plans:
            if list(plan.days) != sorted(plan.days) or any(d < 0 or d > self.horizon_days for d in plan.days):
                raise ValueError(f"plan {plan.name!r}: days must rise and lie within the horizon")
        if len({p.name for p in self.plans}) != len(self.plans):
            raise ValueError("plan names must be unique")


@dataclass(frozen=True)
class PlanChoice:
    """The result of planning one customer: every plan's value and the best one."""

    best: PlanSpec
    values: tuple[tuple[str, float], ...]
    """(plan name, value) in the order compared."""

    @property
    def wait(self) -> bool:
        """The best plan sends something later and nothing today."""
        return bool(self.best.days) and self.best.days[0] > 0

    @property
    def send_now(self) -> bool:
        return bool(self.best.days) and self.best.days[0] == 0

    @property
    def value(self) -> float:
        return dict(self.values)[self.best.name]


class Pricing(Protocol):
    """What the planner needs of the attention price (`runtime.agent.ContactPrice` implements it)."""

    def lost_value(self, state: CustomerState, bids: Sequence[Bid]) -> dict[Channel, float]: ...

    def price_at(self, state: CustomerState, channel: Channel, contacts: float, lost: float) -> float: ...

    def freshness(self, contacts: float) -> float: ...


@dataclass(frozen=True)
class _Option:
    channel: Channel
    gross: float
    fixed: float
    urgency: float


@dataclass
class FrequencyPlanner:
    """Compares the fixed plans for one customer and says whether today is a day to wait.

    `choose(state, bids)` is pure. `gate(state, bids, now, observe)` is the hook
    `AutonomousPolicy(plan_fn=...)` calls: it returns `rules.FREQUENCY_PLAN` when the best plan waits.
    `last` keeps each customer's latest plan (when it was made, its name, its value) for the
    briefing and the tests.
    """

    pricing: Pricing
    config: PlanConfig = field(default_factory=PlanConfig)
    last: dict[str, tuple[datetime, str, float]] = field(default_factory=dict)

    def choose(self, state: CustomerState, bids: Sequence[Bid]) -> PlanChoice:
        cfg = self.config
        options = [
            _Option(
                b.action.channel,
                max(b.uplift, 0.0) * max(b.value_if_converted, 0.0),
                b.offer_cost + b.channel_cost,
                b.urgency,
            )
            for b in bids
        ]
        if not options:
            return PlanChoice(cfg.plans[0], tuple((p.name, 0.0) for p in cfg.plans))
        lost = self.pricing.lost_value(state, bids)
        now_count = float(state.contacts_7d)
        fresh_now = max(self.pricing.freshness(now_count), 1e-9)
        values: list[tuple[str, float]] = []
        for plan in cfg.plans:
            total = 0.0
            for k, day in enumerate(plan.days):
                count = now_count * math.exp(-day / cfg.memory_days) + sum(
                    math.exp(-(day - earlier) / cfg.memory_days) for earlier in plan.days[:k]
                )
                fresh = max(self.pricing.freshness(count), 0.0) / fresh_now
                best = max(
                    (o.urgency if day == 0 else min(o.urgency, 1.0)) * (o.gross * fresh - o.fixed)
                    - self.pricing.price_at(state, o.channel, count, lost.get(o.channel, 0.0))
                    for o in options
                )
                total += cfg.weekly_discount ** (day / 7.0) * best
            values.append((plan.name, total))
        best_plan = cfg.plans[0]
        best_value = values[0][1]
        for plan, (_name, value) in zip(cfg.plans[1:], values[1:], strict=True):
            if value > best_value + 1e-12:
                best_plan, best_value = plan, value
        return PlanChoice(best_plan, tuple(values))

    def gate(
        self, state: CustomerState, bids: Sequence[Bid], now: datetime, observe: bool = True
    ) -> str | None:
        """`rules.FREQUENCY_PLAN` when the customer's best plan waits, else None."""
        choice = self.choose(state, bids)
        if observe:
            self.last[state.customer_id] = (now, choice.best.name, choice.value)
        return rules.FREQUENCY_PLAN if choice.wait else None


# --- the switch -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FrequencyConfig:
    """`build_agent_policy(frequency=...)`: each part can be set to None to switch it off apart, to
    measure it alone."""

    tolerance: ToleranceConfig | None = field(default_factory=ToleranceConfig)
    plans: PlanConfig | None = field(default_factory=PlanConfig)


def lerp(f: Callable[[int], float], count: float) -> float:
    """`f` at a fractional count, by linear interpolation between the whole counts around it."""
    count = max(count, 0.0)
    low = math.floor(count)
    frac = count - low
    if frac <= 1e-12:
        return f(low)
    return (1.0 - frac) * f(low) + frac * f(low + 1)
