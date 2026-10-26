"""The Gym world: simulated customers over time, driven by the planted truth.

`GymWorld` is an `autonomy.interfaces.Environment` (so the reference loop and the real runtime can
drive it) and a `GymEnvironment` (so the scoreboard can read its truth and its rule breaks).

* `reset(seed)` plants a fresh truth and returns the history before decision day 0: signups,
  consents, purchases over `history_days`, a few organic visits and (when the spec asks for it) the
  client's past campaigns with their reactions, so a state folder builds realistic customer states.
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

Realism, and why (wave 3a). Each change is how real customers and real client data behave, not a
way to make any policy pass:

* **Unsubscribes and complaints carry their cost.** Every UNSUBSCRIBE event an `act` causes carries
  `attrs["lost_value"]`: the customer's lost contact value on that channel (the margin later messages
  would have made; see `autonomy.gym.truth`). Every COMPLAINT event carries `attrs["lost_value"]` =
  `complaint_cost`, the deliverability damage, on top of the unsubscribe that comes with it. Summing
  `lost_value` over a group's UNSUBSCRIBE and COMPLAINT events gives the realised counterpart of what
  the truth charges in `expected_net`. These attrs are for the scoreboard: a real ESP does not tell a
  business what an unsubscribe cost, so a policy under test must not read them.
* **The history holds past marketing** (when `history_sends_per_week` > 0; every library scenario).
  A real client's CRM holds months of campaigns with their sends, clicks, unsubscribes and the
  purchases they caused, and that is what an agent learns from before day 1. The logging policy is a
  plain marketer's, randomised so the data supports off-policy learning:

  - one campaign a week, on Tuesdays at 10:00 in each customer's own time zone;
  - each customer who can be reached (consented, signed up, not unsubscribed) is included with
    probability `history_sends_per_week` x 1.5 if they bought in the 60 days before the campaign day
    (days counted from `start`, as in the history), x 0.75 otherwise (capped at 0.95): engaged
    customers are mailed more, as marketers do. The policy only looks at what was known before the
    campaign day, so the logged probabilities can be rebuilt from the history itself;
  - channel: WhatsApp with probability 0.25 when both are open, otherwise the open one;
  - offer: none with probability 0.6, the rest split evenly between the world's other offers; the
    theme follows the offer (savings for money off, convenience for free shipping, reminder else)
    and the content id is `gym_<channel>_<offer>_<theme>`.

  Every past SEND carries `attrs["logged_probability"]` = inclusion x channel x offer probability of
  what was sent, plus `logging_policy` and `campaign_id`; its `decision_id` is the campaign's, which
  no decision log holds (imported history never does). Past UNSUBSCRIBE and COMPLAINT events carry no
  `lost_value`: the history is not scored, and that attr is derived from the planted truth (a dog's
  is 0), so in a history every policy reads it would name the sleeping dogs and leak their uplift. Customers left out of a campaign have
  probability 1 - inclusion of what happened to them (nothing). Past sends follow exactly the model
  of decision-period sends (lift, fatigue, hour factor, discount redemption, clicks, unsubscribes,
  complaints), at daily resolution: a send's lift starts the day after it and lasts `effect_days`
  days. A past send's effect still running on day 0 carries on into the decision period, and its
  unsubscribes stay in force.
* **Moments** (when the spec plants them: `cart_rate_per_day`, `refill_share`). The planted cart times
  become CART events (with an item id and the customer's favourite category) and the refill days
  become PURCHASE events with an item id, in the history and while the world runs. A contact's
  lift is multiplied while a cart is fresh or a refill is due (see `autonomy.gym.truth`); the
  customer's own purchase rate is not changed.
* **Sleeping dogs leave faint clues.** People annoyed by marketing ignore messages, come in through
  direct visits rather than links, and now and then unsubscribe from one channel. From each dog's
  planted onset (see `autonomy.gym.truth`; `sleeping_dog_signal` sets how far back it lies) the
  history shows exactly that: their past sends earn no lift, dent their own buying and almost never
  get a click, they unsubscribe `sleeping_dog_unsubscribe_multiplier` times as often, and they make
  extra direct visits (`DOG_DIRECT_VISIT_BOOST` x the usual visit rate over the annoyed stretch).
  Plenty of ordinary customers also click little or visit a lot, so the clue is a tilt, not a
  label; with signal 0 the history of a dog is distributed exactly like anyone else's.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

import numpy as np

from autonomy.contracts import (
    Action,
    Channel,
    ContentItem,
    Decision,
    DecisionKind,
    Event,
    EventType,
    OfferKind,
    OfferOption,
    Theme,
    stable_id,
)
from autonomy.contracts.events import AttrValue
from autonomy.gym.api import GroundTruth, ScenarioSpec
from autonomy.gym.baselines import in_quiet_hours
from autonomy.gym.truth import (
    CATEGORIES,
    CHANNEL_INDEX,
    CHANNELS,
    DEFAULT_OFFERS,
    DEFAULT_START,
    KIND_INDEX,
    THEME_INDEX,
    BoolArray,
    FloatArray,
    IntArray,
    PlantedTruth,
)

SOURCE = "gym"
DELIVERY_DELAY = timedelta(minutes=1)
UNSUBSCRIBE_DELAY = timedelta(hours=2)
FAVOURITE_CATEGORY_SHARE = 0.7
HISTORY_VISITS_MEAN = 3.0
REFILL_VALUE_SHARE = 0.6  # a refill order is this share of the average order value
CLICK_FLOOR = 0.02  # clicks from people the message does not move
CLICK_PER_EXTRA_PURCHASE = 3.0  # click chance added per expected extra purchase the contact causes
CLICK_CEILING = 0.8
SLEEPING_DOG_CLICK = 0.005
_AOV_SHAPE = 2.0  # gamma shape of order values; the scale keeps the mean at aov_mean

# The history's logging policy (see the module docstring).
LOGGING_POLICY = "gym_history_v1"
HISTORY_CAMPAIGN_WEEKDAY = 1  # Tuesday
HISTORY_SEND_HOUR = 10  # local time
HISTORY_RECENT_DAYS = 60
HISTORY_RECENT_FACTOR = 1.5
HISTORY_LAPSED_FACTOR = 0.75
HISTORY_MAX_INCLUSION = 0.95
HISTORY_WHATSAPP_SHARE = 0.25
HISTORY_NO_OFFER_SHARE = 0.6
DOG_DIRECT_VISIT_BOOST = 0.75  # extra direct visits of an annoyed dog, as a multiple of the usual rate
_HISTORY_THEME: dict[OfferKind, Theme] = {
    OfferKind.PERCENT_OFF: Theme.SAVINGS,
    OfferKind.AMOUNT_OFF: Theme.SAVINGS,
    OfferKind.FREE_SHIPPING: Theme.CONVENIENCE,
}


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


class _PastSends(NamedTuple):
    """One past campaign's sends, kept until their effect ends (daily resolution)."""

    idx: IntArray
    lift: FloatArray  # added daily purchase probability while the effect lasts; 0 if annoyed
    annoyed: BoolArray
    offer: IntArray  # position in the world's offer list
    offer_cost: FloatArray
    first_day: int  # first day (from start) the effect is on
    end_day: int  # first day it is off


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
        self._moments_before: list[Event] = []
        self._moments_by_hour: dict[int, list[Event]] = {}
        self._plant_moment_events()

    def _plant_moment_events(self) -> None:
        """The planted carts and refill purchases as events: before day 0 for the history, the rest by hour."""
        t = self.planted
        if not t.moments_on:
            return
        events: list[tuple[float, Event]] = []
        for i, cid in enumerate(t.customer_ids):
            fav = CATEGORIES[int(t.favourite[i])]
            for k, sec in enumerate(t.cart_s[i].tolist()):
                if math.isfinite(sec):
                    ts = self.start + timedelta(seconds=round(sec))
                    item = f"sku_{fav}_{(i + k) % 5}"
                    events.append((sec, self._moment_event(("cart", cid, k), cid, ts, fav, item)))
            for k, sec in enumerate(t.refill_s[i].tolist()):
                if math.isfinite(sec):
                    ts = self.start + timedelta(seconds=round(sec))
                    value = max(0.01, round(self.spec.aov_mean * REFILL_VALUE_SHARE, 2))
                    bought = self._purchase(("refill", cid, k), cid, ts, value, int(t.favourite[i]))
                    events.append((sec, bought.model_copy(update={"item_id": f"sku_{fav}_refill"})))
        for sec, event in sorted(events, key=lambda pair: (pair[0], pair[1].event_id)):
            if sec < 0:
                self._moments_before.append(event)
            else:
                self._moments_by_hour.setdefault(int(sec // 3600), []).append(event)

    def _moment_event(
        self, key: tuple[object, ...], cid: str, ts: datetime, category: str, item: str
    ) -> Event:
        return Event(
            event_id=stable_id("gev", self.seed, *key),
            customer_id=cid,
            type=EventType.CART,
            ts=ts,
            source=SOURCE,
            category=category,
            item_id=item,
        )

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
        past = _PastMarketing(self) if spec.history_sends_per_week > 0 and hist > 0 else None
        last_buy = np.full(t.n, -np.inf)
        for d in range(-hist, 0):
            day_s = d * 86_400.0
            # At-risk customers used to buy more: their rate falls from 3x to 1x over the history.
            frac = (d + hist) / hist
            rate = t.daily_rate * np.where(at_risk, 3.0 - 2.0 * frac, 1.0)
            u = rng.random(t.n)
            second = rng.random(t.n)
            value = rng.gamma(_AOV_SHAPE, spec.aov_mean / _AOV_SHAPE, t.n)
            category = self._categories(rng)
            p = rate if past is None else past.purchase_prob(d, rate)
            buyers = np.flatnonzero((u < p) & (t.signup_s < day_s))
            for i in buyers.tolist():
                cid = t.customer_ids[i]
                ts = self.start + timedelta(seconds=day_s + int(second[i] * 86_400))
                offer = None if past is None else past.redemption(i, d, float(u[i]))
                out.append(
                    self._purchase(("hist", cid, d), cid, ts, float(value[i]), int(category[i]), offer=offer)
                )
            if past is not None:
                # Before today's purchases are noted: the campaign goes out at 10:00 and a marketer
                # cannot condition on purchases made later that day (the logging policy must not
                # look ahead, or its logged probabilities would depend on the outcome).
                out.extend(past.campaign(d, last_buy))
            last_buy[buyers] = d

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
        out.extend(self._direct_visits_of_annoyed_dogs())
        out.extend(self._moments_before)
        if past is not None:
            past.carry_over()
        # A past send's click or unsubscribe can land after day 0 when `start` is not a midnight; it
        # then arrives with the first `advance`, never in the history.
        self._pending.extend(e for e in out if e.ts >= self.start)
        out = [e for e in out if e.ts < self.start]
        out.sort(key=lambda e: (e.ts, e.event_id))
        return out

    def _direct_visits_of_annoyed_dogs(self) -> list[Event]:
        """Extra organic visits over the stretch of the history in which a sleeping dog was annoyed.

        Drawn from their own stream, so with no dogs (or signal 0) the history is exactly as before."""
        t = self.planted
        hist_s = self.spec.history_days * 86_400.0
        lo = np.maximum(t.signup_s, -hist_s)
        annoyed_from = np.maximum(lo, np.minimum(t.dog_onset_s, 0.0))
        length = np.maximum(-lo, 1.0)
        annoyed_len = np.where(t.sleeping_dog & (lo < -60.0), np.maximum(-annoyed_from, 0.0), 0.0)
        mean = HISTORY_VISITS_MEAN * DOG_DIRECT_VISIT_BOOST * annoyed_len / length
        vrng = np.random.default_rng([self.seed, 7])
        counts = vrng.poisson(mean)
        where = vrng.random((t.n, int(counts.max()) if t.n else 0))
        out: list[Event] = []
        for i in np.flatnonzero(counts).tolist():
            cid = t.customer_ids[i]
            a = float(annoyed_from[i])
            for k in range(int(counts[i])):
                ts = self.start + timedelta(seconds=math.floor(a + float(where[i, k]) * (-a)))
                out.append(self._event(("dvisit", cid, k), cid, EventType.VISIT, ts))
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
        annoyed: list[bool] = ev.annoyed.tolist()
        p_unsub: list[float] = ev.p_unsubscribe.tolist()
        lost_value: list[float] = ev.lost_value.tolist()
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
                    _Activation(i, lift, annoyed[k], a.offer_id, offer_cost[k], h0 + 24 * days)
                )
                if annoyed[k]:
                    t.note_dent(i, h0 + 24 * days)

            # Clicks: mostly from the people the message moves, timed by their best hour.
            roll = rolls[k]
            if roll[0] < click_probability(
                lift, annoyed[k], days, floor=spec.click_floor, slope=spec.click_per_extra_purchase
            ):
                delay = click_delay(roll[1], hour_factor[k], best_hour[k], local_hour[k])
                self._outbound(d, ch, EventType.CLICK, sent + delay, {})

            # Unsubscribes (and some complaints) grow with the contacts already sent this week; each
            # carries what it cost (see the module docstring).
            if roll[2] < p_unsub[k]:
                at = sent + UNSUBSCRIBE_DELAY + timedelta(seconds=round(roll[3] * 3600.0))
                t.note_unsubscribe(i, ch, at)
                self._outbound(d, ch, EventType.UNSUBSCRIBE, at, {"lost_value": round(lost_value[k], 4)})
                if roll[4] < spec.complaint_share_of_unsubscribes:
                    self._outbound(
                        d,
                        ch,
                        EventType.COMPLAINT,
                        at + DELIVERY_DELAY,
                        {"lost_value": round(spec.complaint_cost, 4)},
                    )

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
        moments = self._moments_by_hour.get(h, [])
        if buyers.size == 0:
            return list(moments)
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
        return [*moments, *out]

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


def click_probability(
    lift_per_day: float,
    annoyed: bool,
    effect_days: int,
    *,
    floor: float = CLICK_FLOOR,
    slope: float = CLICK_PER_EXTRA_PURCHASE,
) -> float:
    """Chance a delivered message gets a click: `floor` from anyone, plus `slope` per extra purchase
    it causes (mostly the people it moves); an annoyed sleeping dog almost never clicks. The defaults
    are the Gym's original constants; a scenario can set both (`ScenarioSpec.click_floor`)."""
    if annoyed:
        return SLEEPING_DOG_CLICK if floor > 0 else 0.0
    return min(CLICK_CEILING, floor + slope * lift_per_day * effect_days)


def click_delay(roll: float, hour_factor: float, best_hour: int, local_hour: float) -> timedelta:
    """When the click comes: soon when sent near their best hour, else at their usual reading hour."""
    hf = max(hour_factor, 1e-9)
    if roll < hf:  # seen straight away
        return timedelta(seconds=round(roll / hf * 3600.0) + 60)
    return timedelta(hours=(best_hour - local_hour) % 24.0, minutes=15)


class _PastMarketing:
    """The client's own campaigns during the history, under the logging policy in the module docstring.

    `GymWorld._history` walks the history day by day; each day it asks this for the purchase
    probabilities (own rate, dented for annoyed dogs, plus the lift of past sends still in effect),
    which purchases redeem a past offer, and, on campaign days, the campaign's events. At the end,
    `carry_over` hands the effects still running on day 0 to the decision-period simulation.
    Randomness comes from its own stream (one block per campaign day), so turning past marketing on
    leaves the organic draws of the history untouched.
    """

    def __init__(self, world: GymWorld) -> None:
        self.world = world
        t = world.planted
        n = t.n
        self.offer_ids: list[str] = list(t.offers)
        others = max(len(self.offer_ids) - 1, 0)
        probs = [
            (
                (HISTORY_NO_OFFER_SHARE if others else 1.0)
                if oid == "none"
                else (1.0 - HISTORY_NO_OFFER_SHARE) / others
            )
            for oid in self.offer_ids
        ]
        if "none" not in self.offer_ids:  # every offer list holds "none"; guard anyway
            probs = [1.0 / len(probs)] * len(probs)
        self.offer_prob = np.array(probs, dtype=np.float64)
        offers = [t.offer(o) for o in self.offer_ids]
        self.offer_kind = np.array([KIND_INDEX[o.kind] for o in offers], dtype=np.int64)
        self.offer_cost_of = np.array([t.offer_cost(o) for o in offers], dtype=np.float64)
        self.content_ids: dict[tuple[Channel, int], str] = {}
        self.theme_of: dict[tuple[Channel, int], int] = {}
        for ch in (Channel.EMAIL, Channel.WHATSAPP):
            for k, o in enumerate(offers):
                theme = _HISTORY_THEME.get(o.kind, Theme.REMINDER)
                content_id = f"gym_{ch.value}_{o.offer_id}_{theme.value}"
                self.content_ids[(ch, k)] = content_id
                known = t.theme_of(content_id)
                self.theme_of[(ch, k)] = -1 if known is None else THEME_INDEX[known]
        self.lift = np.zeros(n, dtype=np.float64)
        self.dent_until = np.full(n, -(10**9), dtype=np.int64)
        self.offer_until = np.full(n, -(10**9), dtype=np.int64)
        self.offer = np.zeros(n, dtype=np.int64)
        self.offer_cost = np.zeros(n, dtype=np.float64)
        self.sends: list[_PastSends] = []
        self._base = np.zeros(n, dtype=np.float64)
        self._dog_on = np.zeros(n, dtype=np.bool_)

    # --- the day's purchases ---------------------------------------------------------------------

    def purchase_prob(self, day: int, rate: FloatArray) -> FloatArray:
        """Purchase probability of every customer on `day`, with past sends' effects applied."""
        for s in self.sends:
            if s.first_day == day:
                live = ~s.annoyed
                self.lift[s.idx[live]] += s.lift[live]
                dented = s.idx[s.annoyed]
                self.dent_until[dented] = np.maximum(self.dent_until[dented], s.end_day)
                paid = s.offer_cost > 0
                self.offer_until[s.idx[paid]] = s.end_day
                self.offer[s.idx[paid]] = s.offer[paid]
                self.offer_cost[s.idx[paid]] = s.offer_cost[paid]
            if s.end_day == day:
                live = ~s.annoyed
                self.lift[s.idx[live]] -= s.lift[live]
        self._dog_on = self.dent_until > day
        self._base = np.where(self._dog_on, rate * self.world.planted.dog_multiplier(), rate)
        return np.asarray(self._base + np.maximum(self.lift, 0.0), dtype=np.float64)

    def redemption(self, i: int, day: int, u: float) -> tuple[str, float] | None:
        """The offer a purchase on `day` redeems, as in the decision period: every buyer while it is
        open with cannibalisation, otherwise only the buyers the message created."""
        if self.offer_until[i] <= day:
            return None
        caused = u >= float(self._base[i]) and not bool(self._dog_on[i])
        if not (self.world.spec.discount_redeemed_by_sure_things or caused):
            return None
        return self.offer_ids[int(self.offer[i])], float(self.offer_cost[i])

    # --- a campaign ------------------------------------------------------------------------------

    def campaign(self, day: int, last_buy: FloatArray) -> list[Event]:
        """The weekly campaign's sends and reactions, if `day` is a campaign day."""
        w = self.world
        t = w.planted
        spec = w.spec
        date = (w.start + timedelta(days=day)).date()
        if date.weekday() != HISTORY_CAMPAIGN_WEEKDAY:
            return []
        send_s_by_tz = np.array(
            [
                t.seconds(datetime.combine(date, time(HISTORY_SEND_HOUR), tzinfo=ZoneInfo(z)))
                for z in t.tz_names
            ],
            dtype=np.float64,
        )
        t_s = send_s_by_tz[t.tz_idx]
        email, wa = CHANNEL_INDEX[Channel.EMAIL], CHANNEL_INDEX[Channel.WHATSAPP]
        open_to = t.consent & (t.unsub_at > t_s[:, None]) & (t.signup_s < t_s)[:, None]
        by_email, by_wa = open_to[:, email], open_to[:, wa]
        recent = (day - last_buy) <= HISTORY_RECENT_DAYS
        p_include = np.minimum(
            spec.history_sends_per_week * np.where(recent, HISTORY_RECENT_FACTOR, HISTORY_LAPSED_FACTOR),
            HISTORY_MAX_INCLUSION,
        )
        draws = np.random.default_rng([w.seed, 6, day + spec.history_days]).random((t.n, 8))
        include = (by_email | by_wa) & (draws[:, 0] < p_include) & (t_s < 0)
        both = by_email & by_wa
        use_wa = np.where(both, draws[:, 1] < HISTORY_WHATSAPP_SHARE, by_wa & ~by_email)
        p_channel = np.where(both, np.where(use_wa, HISTORY_WHATSAPP_SHARE, 1 - HISTORY_WHATSAPP_SHARE), 1.0)
        cum = np.cumsum(self.offer_prob)
        offer = np.minimum(np.searchsorted(cum, draws[:, 2] * cum[-1], side="right"), len(cum) - 1)
        sel = np.flatnonzero(include).astype(np.int64)
        if sel.size == 0:
            return []
        ch_idx = np.where(use_wa[sel], wa, email).astype(np.int64)
        off = offer[sel].astype(np.int64)
        theme = np.array(
            [self.theme_of[(CHANNELS[c], k)] for c, k in zip(ch_idx.tolist(), off.tolist(), strict=True)],
            dtype=np.int64,
        )
        channel_cost = np.array([spec.channel_cost.get(CHANNELS[c], 0.0) for c in ch_idx.tolist()])
        ev = t.evaluate_rows(
            sel,
            ch_idx,
            self.offer_kind[off],
            theme,
            self.offer_cost_of[off],
            np.asarray(channel_cost, dtype=np.float64),
            t_s[sel],
            np.full(sel.size, float(HISTORY_SEND_HOUR)),
        )
        t.note_contacts(sel, t_s[sel])
        logged = p_include[sel] * p_channel[sel] * self.offer_prob[off]
        days = spec.effect_days
        first, end = day + 1, day + 1 + days
        self.sends.append(
            _PastSends(
                idx=sel,
                lift=np.asarray(ev.lift_per_day, dtype=np.float64),
                annoyed=ev.annoyed,
                offer=off,
                offer_cost=np.asarray(ev.offer_cost, dtype=np.float64),
                first_day=first,
                end_day=end,
            )
        )
        for i in sel[ev.annoyed].tolist():
            t.note_dent(i, end * 24)

        campaign_id = f"hist_{date:%Y%m%d}"
        out: list[Event] = []
        rows = zip(
            sel.tolist(),
            ch_idx.tolist(),
            off.tolist(),
            logged.tolist(),
            ev.lift_per_day.tolist(),
            ev.annoyed.tolist(),
            ev.hour_factor.tolist(),
            ev.p_unsubscribe.tolist(),
            draws[sel].tolist(),
            strict=True,
        )
        for i, c, k, prob, lift, annoyed, hf, p_unsub, roll in rows:
            cid = t.customer_ids[i]
            ch = CHANNELS[c]
            sent = w.start + timedelta(seconds=round(float(t_s[i])))
            decision_id = stable_id("hdec", w.seed, cid, day)

            sent_events: list[tuple[EventType, datetime, dict[str, AttrValue]]] = [
                (
                    EventType.SEND,
                    sent,
                    {
                        "offer_id": self.offer_ids[k],
                        "content_id": self.content_ids[(ch, k)],
                        "logged_probability": round(prob, 6),
                        "logging_policy": LOGGING_POLICY,
                        "campaign_id": campaign_id,
                    },
                ),
                (EventType.DELIVER, sent + DELIVERY_DELAY, {}),
            ]
            if roll[3] < click_probability(
                lift, annoyed, days, floor=spec.click_floor, slope=spec.click_per_extra_purchase
            ):
                best = int(t.best_hour[i])
                delay = click_delay(roll[4], hf, best, float(HISTORY_SEND_HOUR))
                sent_events.append((EventType.CLICK, sent + delay, {}))
            if roll[5] < p_unsub:
                at = sent + UNSUBSCRIBE_DELAY + timedelta(seconds=round(roll[6] * 3600.0))
                t.note_unsubscribe(i, ch, at)
                # No `lost_value` here (see the module docstring): it would name the dogs.
                sent_events.append((EventType.UNSUBSCRIBE, at, {}))
                if roll[7] < spec.complaint_share_of_unsubscribes:
                    sent_events.append((EventType.COMPLAINT, at + DELIVERY_DELAY, {}))
            for kind, ts, attrs in sent_events:
                key = (f"h{kind.value}", cid, day)
                out.append(w._event(key, cid, kind, ts, channel=ch, decision_id=decision_id, attrs=attrs))
        return out

    # --- into the decision period ----------------------------------------------------------------

    def carry_over(self) -> None:
        """Hand every past send's effect still running on day 0 to the hourly simulation. From day 0
        every sleeping dog is annoyed, so a dog's running effect becomes a dent, as after any contact."""
        w = self.world
        t = w.planted
        for s in self.sends:
            if s.end_day <= 0:
                continue
            end_h = s.end_day * 24
            for i, lift, cost, k in zip(
                s.idx.tolist(), s.lift.tolist(), s.offer_cost.tolist(), s.offer.tolist(), strict=True
            ):
                if t.sleeping_dog[i]:
                    w._dog_until[i] = max(int(w._dog_until[i]), end_h)
                    t.note_dent(i, end_h)
                elif lift > 0:
                    w._lift[i] += lift
                    w._expiries.setdefault(end_h, []).append((i, lift))
                if cost > 0:
                    w._offer_until[i] = max(int(w._offer_until[i]), end_h)
                    w._offer_id[i] = self.offer_ids[k]
                    w._offer_cost[i] = cost
