"""The Gym world: simulated customers over time, driven by the planted truth.

`GymWorld` is an `autonomy.interfaces.Environment` (so the reference loop and the real runtime can
drive it) and a `GymEnvironment` (so the scoreboard can read its truth and its rule breaks).

* `reset(seed)` plants a fresh truth and returns the history before decision day 0: signups,
  consents, purchases over `history_days` and a few organic visits, so a state folder builds
  realistic customer states.
* `act(decisions)` carries out ACT decisions at their `send_at`: SEND then DELIVER (or FAIL when the
  customer cannot receive it), a click for some responders, and sometimes an unsubscribe or a
  complaint. It records every rule break (no consent, unsubscribed, local quiet hours) and logs every
  contact, including how many contacts the customer had in 7 days. Going over the weekly cap is the
  policy's rule, not the world's, so it is logged and counted but is not a violation here.
* `advance(until)` simulates purchases hour by hour, vectorised over customers: each customer's own
  rate (with drift), plus the lift of every contact still in effect, or the dent a contact leaves in
  a sleeping dog. A purchase made while a discount is active carries `attrs["offer_id"]`.

Randomness is drawn in blocks that do not depend on what the policy does (one block per simulated
day, one per `act` call and round, each seeded from the world seed), so two policies run on the same seed see
the same organic purchases. Every event id is a `stable_id`, so runs repeat exactly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import numpy as np

from autonomy.contracts import (
    Action,
    Channel,
    ContentItem,
    Decision,
    DecisionKind,
    Event,
    EventType,
    OfferOption,
    stable_id,
)
from autonomy.contracts.events import AttrValue
from autonomy.gym.api import GroundTruth, ScenarioSpec
from autonomy.gym.baselines import in_quiet_hours
from autonomy.gym.truth import (
    CATEGORIES,
    CHANNELS,
    DEFAULT_OFFERS,
    DEFAULT_START,
    FloatArray,
    IntArray,
    PlantedTruth,
)

SOURCE = "gym"
DELIVERY_DELAY = timedelta(minutes=1)
UNSUBSCRIBE_DELAY = timedelta(hours=2)
FAVOURITE_CATEGORY_SHARE = 0.7
HISTORY_VISITS_MEAN = 3.0
CLICK_FLOOR = 0.02  # clicks from people the message does not move
CLICK_PER_EXTRA_PURCHASE = 3.0  # click chance added per expected extra purchase the contact causes
CLICK_CEILING = 0.8
SLEEPING_DOG_CLICK = 0.005
_AOV_SHAPE = 2.0  # gamma shape of order values; the scale keeps the mean at aov_mean


@dataclass(frozen=True)
class ContactRecord:
    """One contact the policy made, with what the truth knew about it at the time."""

    decision_id: str
    customer_id: str
    channel: Channel
    offer_id: str
    content_id: str | None
    send_at: datetime
    contacts_7d: int
    """Contacts to this customer in the 7 days up to and including this one."""
    over_cap: bool
    """More than the weekly cap: the policy's rule broken, but not a world violation."""
    sleeping_dog: bool
    expected_net: float
    """The truth's expected net value of this contact when it was sent."""
    delivered: bool
    violations: tuple[str, ...]


class _Activation(NamedTuple):
    i: int
    lift_per_day: float
    sleeping_dog: bool
    offer_id: str
    offer_cost: float
    end_hour: int


class _DayDraws(NamedTuple):
    u: FloatArray  # (24, n) uniforms that decide purchases
    second: FloatArray  # (24, n) position within the hour
    value: FloatArray  # (n,) order value if they buy that day
    category: IntArray  # (n,)


class GymWorld:
    """A simulated shop. See the module docstring for what each call does."""

    spec: ScenarioSpec
    truth: GroundTruth

    def __init__(
        self,
        spec: ScenarioSpec,
        *,
        start: datetime = DEFAULT_START,
        offers: Sequence[OfferOption] = DEFAULT_OFFERS,
        content: Sequence[ContentItem] = (),
        quiet_start_hour: int = 21,
        quiet_end_hour: int = 9,
        contact_cap: int = 2,
    ) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware; the Gym never reads the clock")
        self.spec = spec
        self.start = start
        self.quiet_start_hour = quiet_start_hour
        self.quiet_end_hour = quiet_end_hour
        self.contact_cap = contact_cap
        self.planted = PlantedTruth(spec, spec.seed, start=start, offers=offers, content=content)
        self.truth = self.planted
        self._start_run(spec.seed)

    # --- set-up --------------------------------------------------------------------------------

    def _start_run(self, seed: int) -> None:
        """Clear the world's own state; the truth must already be planted from `seed`."""
        self.seed = seed
        planted = self.planted
        n = planted.n
        self._now = self.start
        self._hour = 0  # the next hour to simulate, counted from start
        self._pending: list[Event] = []
        self._activations: dict[int, list[_Activation]] = {}
        self._expiries: dict[int, list[tuple[int, float]]] = {}
        self._lift = np.zeros(n, dtype=np.float64)
        self._dog_until = np.full(n, -1, dtype=np.int64)
        self._offer_until = np.full(n, -1, dtype=np.int64)
        self._offer_cost = np.zeros(n, dtype=np.float64)
        self._offer_id: list[str] = ["none"] * n
        self._contacts: list[ContactRecord] = []
        self._violations: list[str] = []
        self._act_calls = 0
        self._day: tuple[int, _DayDraws] | None = None

    def reset(self, seed: int) -> Sequence[Event]:
        """Plant a new truth from `seed` and return the history before decision day 0, in time order.

        The truth is re-planted in place, so `truth` stays the same object: a policy handed it before
        the reset (the oracle) reads the new customers, not the old ones."""
        self.planted.plant(seed)
        self._start_run(seed)
        return self._history()

    @property
    def customer_ids(self) -> tuple[str, ...]:
        return self.planted.customer_ids

    def now(self) -> datetime:
        return self._now

    # --- what the scoreboard reads -------------------------------------------------------------

    def violations(self) -> list[str]:
        return list(self._violations)

    @property
    def contacts(self) -> tuple[ContactRecord, ...]:
        return tuple(self._contacts)

    def over_cap_contacts(self) -> int:
        """Contacts that took a customer past `contact_cap` contacts in 7 days."""
        return sum(1 for c in self._contacts if c.over_cap)

    def sleeping_dogs_contacted_share(self) -> float:
        """Share of planted sleeping dogs contacted at least once (0 when there are none)."""
        dogs = int(self.planted.sleeping_dog.sum())
        if dogs == 0:
            return 0.0
        hit = {c.customer_id for c in self._contacts if c.sleeping_dog}
        return len(hit) / dogs

    # --- history -------------------------------------------------------------------------------

    def _history(self) -> list[Event]:
        t = self.planted
        spec = self.spec
        rng = np.random.default_rng([self.seed, 3])
        out: list[Event] = []
        for i, cid in enumerate(t.customer_ids):
            signed = self.start + timedelta(seconds=round(float(t.signup_s[i])))
            out.append(
                self._event(
                    ("signup", cid),
                    cid,
                    EventType.SIGNUP,
                    signed,
                    attrs={"timezone": t.timezone(i)},
                )
            )
            for j in np.flatnonzero(t.consent[i]).tolist():
                ch = CHANNELS[j]
                out.append(
                    self._event(
                        ("consent", cid, ch.value),
                        cid,
                        EventType.CONSENT_GRANTED,
                        signed + DELIVERY_DELAY,
                        channel=ch,
                    )
                )

        hist = spec.history_days
        at_risk = np.array([p == "at_risk" for p in t.personas], dtype=np.bool_)[t.persona_idx]
        for d in range(-hist, 0):
            day_s = d * 86_400.0
            # At-risk customers used to buy more: their rate falls from 3x to 1x over the history.
            frac = (d + hist) / hist
            rate = t.daily_rate * np.where(at_risk, 3.0 - 2.0 * frac, 1.0)
            u = rng.random(t.n)
            second = rng.random(t.n)
            value = rng.gamma(_AOV_SHAPE, spec.aov_mean / _AOV_SHAPE, t.n)
            category = self._categories(rng)
            buyers = np.flatnonzero((u < rate) & (t.signup_s < day_s))
            for i in buyers.tolist():
                cid = t.customer_ids[i]
                ts = self.start + timedelta(seconds=day_s + int(second[i] * 86_400))
                out.append(self._purchase(("hist", cid, d), cid, ts, float(value[i]), int(category[i])))

        visits = rng.poisson(HISTORY_VISITS_MEAN, t.n)
        where = rng.random((t.n, int(visits.max()) if t.n else 0))
        for i in np.flatnonzero(visits).tolist():
            lo = max(float(t.signup_s[i]), -hist * 86_400.0)
            if lo >= -60.0:
                continue
            cid = t.customer_ids[i]
            for k in range(int(visits[i])):
                ts = self.start + timedelta(seconds=math.floor(lo + float(where[i, k]) * (-lo)))
                out.append(self._event(("visit", cid, k), cid, EventType.VISIT, ts))
        out.sort(key=lambda e: (e.ts, e.event_id))
        return out

    def _categories(self, rng: np.random.Generator) -> IntArray:
        t = self.planted
        other = rng.integers(0, len(CATEGORIES), t.n)
        keep = rng.random(t.n) < FAVOURITE_CATEGORY_SHARE
        return np.asarray(np.where(keep, t.favourite, other), dtype=np.int64)

    # --- acting --------------------------------------------------------------------------------

    def act(self, decisions: Sequence[Decision]) -> None:
        """Carry out ACT decisions at `max(send_at, now)`; NOTHING decisions are ignored."""
        self._act_calls += 1
        rows: list[tuple[Decision, Action, int, datetime]] = []
        for d in decisions:
            if d.kind is not DecisionKind.ACT or d.action is None:
                continue
            a = d.action
            t = max(a.send_at, self._now)
            rows.append((d, a, self.planted.index(d.customer_id), t))
        # A customer contacted twice in one batch: handle the second contact after the first is
        # noted, so its fatigue counts the first. Each round has every customer at most once.
        rounds: list[list[tuple[Decision, Action, int, datetime]]] = []
        seen: dict[int, int] = {}
        for row in rows:
            r = seen.get(row[2], 0)
            seen[row[2]] = r + 1
            if r == len(rounds):
                rounds.append([])
            rounds[r].append(row)
        for r, batch in enumerate(rounds):
            # Per customer: click, click time, unsubscribe, unsubscribe time, complaint. A fresh block
            # per round, so a second contact in one batch does not reuse the first one's rolls.
            rng = np.random.default_rng([self.seed, 2, self._act_calls, r])
            self._act_round(batch, rng.random((self.planted.n, 5)))

    def _act_round(self, batch: list[tuple[Decision, Action, int, datetime]], draws: FloatArray) -> None:
        t = self.planted
        spec = self.spec
        days = spec.effect_days
        idx = np.array([r[2] for r in batch], dtype=np.int64)
        times = [r[3] for r in batch]
        ev = t.evaluate(idx, [r[1] for r in batch], times)
        t.note_contacts(idx, np.array([t.seconds(x) for x in times], dtype=np.float64))
        # Plain lists: indexing numpy arrays one element at a time is slow in a Python loop.
        consent: list[bool] = ev.consent.tolist()
        unsubscribed: list[bool] = ev.unsubscribed.tolist()
        prior: list[int] = ev.prior_contacts_7d.tolist()
        net: list[float] = ev.net.tolist()
        lifts: list[float] = ev.lift_per_day.tolist()
        local_hour: list[float] = ev.local_hour.tolist()
        hour_factor: list[float] = ev.hour_factor.tolist()
        offer_cost: list[float] = ev.offer_cost.tolist()
        dogs: list[bool] = t.sleeping_dog[idx].tolist()
        best_hour: list[int] = t.best_hour[idx].tolist()
        rolls: list[list[float]] = draws[idx].tolist()
        for k, (d, a, i, sent) in enumerate(batch):
            cid = d.customer_id
            ch = a.channel
            utc = sent.astimezone(UTC)
            problems: list[str] = []
            if not consent[k]:
                problems.append(
                    f"{cid}: {ch.value} contact at {utc:%Y-%m-%d %H:%M} UTC without consent "
                    f"(decision {d.decision_id})"
                )
            elif unsubscribed[k]:
                problems.append(
                    f"{cid}: {ch.value} contact at {utc:%Y-%m-%d %H:%M} UTC after they unsubscribed "
                    f"(decision {d.decision_id})"
                )
            if self._quiet(int(local_hour[k])):
                problems.append(
                    f"{cid}: {ch.value} contact at {t.local_time(i, sent):%H:%M} local time, inside quiet "
                    f"hours {self.quiet_start_hour:02d}:00-{self.quiet_end_hour:02d}:00 "
                    f"(decision {d.decision_id})"
                )
            delivered = consent[k] and not unsubscribed[k]
            weekly = prior[k] + 1
            self._contacts.append(
                ContactRecord(
                    decision_id=d.decision_id,
                    customer_id=cid,
                    channel=ch,
                    offer_id=a.offer_id,
                    content_id=a.content_id,
                    send_at=sent,
                    contacts_7d=weekly,
                    over_cap=weekly > self.contact_cap,
                    sleeping_dog=dogs[k],
                    expected_net=net[k],
                    delivered=delivered,
                    violations=tuple(problems),
                )
            )
            self._violations.extend(problems)

            attrs: dict[str, AttrValue] = {"offer_id": a.offer_id}
            if a.content_id is not None:
                attrs["content_id"] = a.content_id
            self._outbound(d, ch, EventType.SEND, sent, attrs)
            if not delivered:
                reason = "no_consent" if not consent[k] else "unsubscribed"
                self._outbound(d, ch, EventType.FAIL, sent + DELIVERY_DELAY, {"reason": reason})
                continue
            self._outbound(d, ch, EventType.DELIVER, sent + DELIVERY_DELAY, {})

            lift = lifts[k]
            if days > 0:
                # Never an hour already simulated (advance runs the hour that holds `now`).
                h0 = max(t.hour_at_or_after(sent), self._hour)
                self._activations.setdefault(h0, []).append(
                    _Activation(i, lift, dogs[k], a.offer_id, offer_cost[k], h0 + 24 * days)
                )
                if dogs[k]:
                    t.note_dent(i, h0 + 24 * days)

            # Clicks: mostly from the people the message moves, timed by their best hour.
            roll = rolls[k]
            if dogs[k]:
                p_click = SLEEPING_DOG_CLICK
            else:
                p_click = min(CLICK_CEILING, CLICK_FLOOR + CLICK_PER_EXTRA_PURCHASE * lift * days)
            if roll[0] < p_click:
                hf = max(hour_factor[k], 1e-9)
                if roll[1] < hf:  # seen straight away
                    delay = timedelta(seconds=round(roll[1] / hf * 3600.0) + 60)
                else:  # opened at their usual hour
                    delay = timedelta(hours=(best_hour[k] - local_hour[k]) % 24.0, minutes=15)
                self._outbound(d, ch, EventType.CLICK, sent + delay, {})

            # Unsubscribes (and some complaints) grow with the contacts already sent this week.
            if roll[2] < spec.unsubscribe_base + spec.unsubscribe_per_weekly_contact * prior[k]:
                at = sent + UNSUBSCRIBE_DELAY + timedelta(seconds=round(roll[3] * 3600.0))
                t.note_unsubscribe(i, ch, at)
                self._outbound(d, ch, EventType.UNSUBSCRIBE, at, {})
                if roll[4] < spec.complaint_share_of_unsubscribes:
                    self._outbound(d, ch, EventType.COMPLAINT, at + DELIVERY_DELAY, {})

    def _quiet(self, hour: int) -> bool:
        return in_quiet_hours(hour, self.quiet_start_hour, self.quiet_end_hour)

    def _outbound(
        self, d: Decision, channel: Channel, kind: EventType, ts: datetime, attrs: dict[str, AttrValue]
    ) -> None:
        self._pending.append(
            self._event(
                (kind.value, d.decision_id),
                d.customer_id,
                kind,
                ts,
                channel=channel,
                decision_id=d.decision_id,
                attrs=attrs,
            )
        )

    # --- time ----------------------------------------------------------------------------------

    def advance(self, until: datetime) -> Sequence[Event]:
        """Simulate every hour that starts before `until`; return all events before `until`.

        When `until` falls inside an hour, that whole hour is simulated now and its purchases from
        `until` on are held back for the next call. That is safe because a contact sent at or after
        `until` only takes effect from the next whole hour, and it means no event ever arrives after
        a later `advance` with a timestamp before an earlier `until`.
        """
        if until <= self._now:
            return []
        target = math.ceil(self.planted.seconds(until) / 3600.0 - 1e-9)
        while self._hour < target:
            self._pending.extend(self._simulate_hour(self._hour))
            self._hour += 1
        out = [e for e in self._pending if e.ts < until]
        self._pending = [e for e in self._pending if e.ts >= until]
        self._now = until
        out.sort(key=lambda e: (e.ts, e.event_id))
        return out

    def _draws(self, day: int) -> _DayDraws:
        if self._day is None or self._day[0] != day:
            rng = np.random.default_rng([self.seed, 1, day])
            n = self.planted.n
            draws = _DayDraws(
                u=rng.random((24, n)),
                second=rng.random((24, n)),
                value=rng.gamma(_AOV_SHAPE, self.spec.aov_mean / _AOV_SHAPE, n),
                category=self._categories(rng),
            )
            self._day = (day, draws)
        return self._day[1]

    def _simulate_hour(self, h: int) -> list[Event]:
        t = self.planted
        for a in self._activations.pop(h, []):
            if a.sleeping_dog:
                self._dog_until[a.i] = max(int(self._dog_until[a.i]), a.end_hour)
            else:
                self._lift[a.i] += a.lift_per_day
                self._expiries.setdefault(a.end_hour, []).append((a.i, a.lift_per_day))
            if a.offer_cost > 0:
                self._offer_until[a.i] = a.end_hour
                self._offer_id[a.i] = a.offer_id
                self._offer_cost[a.i] = a.offer_cost
        for i, lift in self._expiries.pop(h, []):
            self._lift[i] -= lift

        day, hour = divmod(h, 24)
        draws = self._draws(day)
        base = t.daily_rate * (t.rate_multiplier(h) / 24.0)
        dog_on = self._dog_until > h
        p = np.where(dog_on, base * t.dog_multiplier(), base) + np.maximum(self._lift, 0.0) / 24.0
        u = draws.u[hour]
        buyers = np.flatnonzero(u < p)
        if buyers.size == 0:
            return []
        sure_things_redeem = self.spec.discount_redeemed_by_sure_things
        out: list[Event] = []
        for i in buyers.tolist():
            cid = t.customer_ids[i]
            ts = self.start + timedelta(seconds=h * 3600 + int(draws.second[hour, i] * 3600))
            redeem = False
            if self._offer_until[i] > h:
                caused_by_contact = bool(u[i] >= base[i]) and not bool(dog_on[i])
                redeem = sure_things_redeem or caused_by_contact
            out.append(
                self._purchase(
                    ("buy", cid, h),
                    cid,
                    ts,
                    float(draws.value[i]),
                    int(draws.category[i]),
                    offer=(self._offer_id[i], float(self._offer_cost[i])) if redeem else None,
                )
            )
        return out

    # --- events --------------------------------------------------------------------------------

    def _purchase(
        self,
        key: tuple[object, ...],
        cid: str,
        ts: datetime,
        value: float,
        category: int,
        offer: tuple[str, float] | None = None,
    ) -> Event:
        value = max(0.01, round(value, 2))
        attrs: dict[str, AttrValue] = {}
        if offer is not None:
            attrs = {"offer_id": offer[0], "offer_cost": round(offer[1], 2)}
        return Event(
            event_id=stable_id("gev", self.seed, *key),
            customer_id=cid,
            type=EventType.PURCHASE,
            ts=ts,
            source=SOURCE,
            value=value,
            margin=round(value * self.spec.margin_rate, 2),
            category=CATEGORIES[category],
            attrs=attrs,
        )

    def _event(
        self,
        key: tuple[object, ...],
        cid: str,
        kind: EventType,
        ts: datetime,
        *,
        channel: Channel | None = None,
        decision_id: str | None = None,
        attrs: dict[str, AttrValue] | None = None,
    ) -> Event:
        return Event(
            event_id=stable_id("gev", self.seed, *key),
            customer_id=cid,
            type=kind,
            ts=ts,
            source=SOURCE,
            channel=channel,
            decision_id=decision_id,
            attrs=attrs or {},
        )
