"""The repeated-campaign world: real customers, real baseline buying, a hidden message effect.

`WavesWorld` is an `autonomy.interfaces.Environment` and a `GymEnvironment`, so the Gym's own loop
(`autonomy.gym.score.run_policy`) drives it with any policy unchanged.

* **No-message world = the real data.** `advance` replays each customer's real receipts after the cutoff, day by
  day, with their real times and amounts. If nobody is ever contacted, the events are exactly those receipts.
* **A contact** (`act`) is a SEND, then a DELIVER or a FAIL, sometimes a click, sometimes an unsubscribe. The truth
  (`WavesTruth.register`) adds it to two arrays the world draws from each day: the chance of an extra purchase (a
  positive effect) and the chance each real receipt is removed (a sleeping dog's window, or the pull-forward payback
  after a positive effect). Every random number is drawn per customer and day from the world's seed alone, so two
  policies in the same world and seed see the same underlying luck (common random numbers), and the expectation
  of what the world realises is exactly what the truth scores.
* **An extra purchase looks like a real one.** Its event id is a hash like a real receipt's, and its amount and
  time of day are those of one of the customer's own receipts before the cutoff, drawn at random (a random
  receipt of the population for a customer with none), so its expected amount is the customer's mean receipt.
* **Clicks do not depend on the effect.** The click chance depends on the customer's rank in base buying rate and a
  personal engagement, never on how much a message moves them. Because the effect in roubles is proportional to the
  base rate, clicks still rank the value of a contact somewhat (docs/v4/WAVES.md says how much).

The customer's effect starts the day after the send (UTC days) so every day's purchases are fixed before any
decision that could change them is made; `advance` can then simulate whole days.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Channel, Decision, DecisionKind, Event, EventType, stable_id
from autonomy.contracts.events import AttrValue
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.baselines import in_quiet_hours
from autonomy.gym.waves.config import (
    COMPLAINT_COST,
    COMPLAINT_SHARE,
    MARGIN,
    UNSUBSCRIBE_COST,
    WorldSpec,
)
from autonomy.gym.waves.data import SECONDS_PER_DAY, SHOP_TZ, WavesData, unit_hash
from autonomy.gym.waves.truth import CHANNEL_INDEX, CHANNELS, MAX_INCREMENTAL_PROB, WavesTruth

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

SOURCE = "waves"
DELIVERY_DELAY = timedelta(minutes=1)
UNSUBSCRIBE_DELAY = timedelta(hours=2)
ENGAGE_SALT = "autonomy-waves-v1:engage"
CLICK_FLOOR = {Channel.SMS: 0.01, Channel.EMAIL: 0.02}
CLICK_SPAN = {Channel.SMS: 0.07, Channel.EMAIL: 0.12}
CLICK_BASE_WEIGHT = 0.35
"""Share of the click engagement that is the customer's rank in base buying rate (the rest is a personal hash)."""
PURCHASE_PREFIX = "wpur"


def receipt_event_id(transaction_id: str) -> str:
    """The event id of a real receipt: a hash, the same in every world, seed and policy."""
    return stable_id(PURCHASE_PREFIX, "receipt", transaction_id)


def extra_event_id(seed: int, customer_id: str, day: int) -> str:
    """The event id of an extra (message-caused) purchase: a hash of the same shape as a real receipt's."""
    return stable_id(PURCHASE_PREFIX, "extra", seed, customer_id, day)


def click_probability(base_rate: FloatArray, engagement_latent: FloatArray, channel: Channel) -> FloatArray:
    """Chance a delivered message is clicked: a floor plus a share that grows with the customer's rank in
    base buying rate and with a personal engagement. The hidden effect is not an input."""
    rank = np.argsort(np.argsort(base_rate)) / max(len(base_rate) - 1, 1)
    engagement = CLICK_BASE_WEIGHT * rank + (1.0 - CLICK_BASE_WEIGHT) * engagement_latent
    return np.asarray(CLICK_FLOOR[channel] + CLICK_SPAN[channel] * engagement**2, dtype=np.float64)


class WavesWorld:
    """See the module docstring."""

    spec: ScenarioSpec

    def __init__(
        self,
        spec: ScenarioSpec,
        data: WavesData,
        world: WorldSpec,
        *,
        contact_cap: int = 2,
        quiet_start_hour: int = 21,
        quiet_end_hour: int = 9,
    ) -> None:
        self.spec = spec
        self.data = data
        self.world = world
        self.contact_cap = contact_cap
        self.quiet_start_hour = quiet_start_hour
        self.quiet_end_hour = quiet_end_hour
        self.start = data.start
        self.n = data.n
        self.days = spec.days
        self.truth = WavesTruth(data, world, spec.channel_cost, spec.seed, days=spec.days)
        self._zone = ZoneInfo(SHOP_TZ)
        latent = unit_hash(data.customer_ids, ENGAGE_SALT)
        self.p_click = np.vstack([click_probability(data.base_rate, latent, c) for c in CHANNELS])
        self._cost = {c: float(spec.channel_cost.get(c, 0.0)) for c in CHANNELS}
        self._width = self.truth.width
        self._end_s = self.days * SECONDS_PER_DAY
        # each customer's own receipts before the cutoff, to draw an extra purchase's amount and time of day from
        self._hist_order = np.argsort(data.hist_idx, kind="stable").astype(np.int64)
        self._hist_start = np.searchsorted(
            data.hist_idx[self._hist_order], np.arange(self.n + 1), side="left"
        )
        self._start_run(spec.seed)

    # --- set-up ----------------------------------------------------------------------------------

    def _start_run(self, seed: int) -> None:
        self.seed = seed
        n = self.n
        self._now = self.start
        self._next_day = 0
        self._pending: list[Event] = []
        self._violations: list[str] = []
        self._act_calls = 0
        self._unsub_s = np.full((n, len(CHANNELS)), np.inf)
        self._thin = np.random.default_rng([seed, 12]).random(len(self.data.post_idx))
        self._post_day_edges = np.searchsorted(
            self.data.post_s, np.arange(self.days + 2, dtype=np.float64) * SECONDS_PER_DAY, side="left"
        )
        self.margin_in_run = np.zeros(n)
        self.send_cost_in_run = np.zeros(n)
        self.lost_in_run = np.zeros(n)
        self.real_receipts_removed = 0
        self.incremental_purchases = 0

    def reset(self, seed: int) -> Sequence[Event]:
        """Plant a new hidden effect from `seed` and return the history before decision day 0."""
        self.truth.plant(seed)
        self._start_run(seed)
        return self._history()

    @property
    def customer_ids(self) -> tuple[str, ...]:
        return self.data.customer_ids

    def now(self) -> datetime:
        return self._now

    def violations(self) -> list[str]:
        return list(self._violations)

    # --- history -----------------------------------------------------------------------------------

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
            event_id=stable_id("wev", *key),
            customer_id=cid,
            type=kind,
            ts=ts,
            source=SOURCE,
            channel=channel,
            decision_id=decision_id,
            attrs=attrs or {},
        )

    def _purchase(self, event_id: str, cid: str, ts: datetime, value: float) -> Event:
        value = max(0.01, round(float(value), 2))
        return Event(
            event_id=event_id,
            customer_id=cid,
            type=EventType.PURCHASE,
            ts=ts,
            source=SOURCE,
            value=value,
            margin=round(value * MARGIN, 2),
        )

    def _history(self) -> list[Event]:
        d = self.data
        out: list[Event] = []
        for i, cid in enumerate(d.customer_ids):
            signed = self.start + timedelta(seconds=float(d.signup_s[i]))
            out.append(
                self._event(("signup", cid), cid, EventType.SIGNUP, signed, attrs={"timezone": SHOP_TZ})
            )
            out.append(
                self._event(
                    ("consent", cid, "sms"),
                    cid,
                    EventType.CONSENT_GRANTED,
                    signed + DELIVERY_DELAY,
                    channel=Channel.SMS,
                )
            )
            if d.email_consent[i]:
                out.append(
                    self._event(
                        ("consent", cid, "email"),
                        cid,
                        EventType.CONSENT_GRANTED,
                        signed + DELIVERY_DELAY,
                        channel=Channel.EMAIL,
                    )
                )
            for key, value in (("age", format(float(d.age[i]), ".0f")), ("gender", d.gender[i])):
                out.append(
                    self._event(
                        ("pref", cid, key),
                        cid,
                        EventType.PREFERENCE,
                        signed + 2 * DELIVERY_DELAY,
                        attrs={"key": key, "value": value},
                    )
                )
        for i, s, v, rid in zip(
            d.hist_idx.tolist(), d.hist_s.tolist(), d.hist_value.tolist(), d.hist_id, strict=True
        ):
            ts = self.start + timedelta(seconds=s)
            out.append(self._purchase(receipt_event_id(rid), d.customer_ids[i], ts, v))
        out.sort(key=lambda e: (e.ts, e.event_id))
        return out

    # --- time --------------------------------------------------------------------------------------

    def advance(self, until: datetime) -> Sequence[Event]:
        """Simulate every day that starts before `until`; return all events before `until`.

        A day is simulated whole, and its events from `until` on are held back for the next call. That is
        safe because no contact decided from `now` on changes a day that has started: effects begin the day
        after the send."""
        if until <= self._now:
            return []
        target = math.ceil((until - self.start).total_seconds() / SECONDS_PER_DAY - 1e-9)
        while self._next_day < target:
            self._pending.extend(self._simulate_day(self._next_day))
            self._next_day += 1
        out = [e for e in self._pending if e.ts < until]
        self._pending = [e for e in self._pending if e.ts >= until]
        self._now = until
        out.sort(key=lambda e: (e.ts, e.event_id))
        return out

    def _simulate_day(self, d: int) -> list[Event]:
        data = self.data
        out: list[Event] = []
        rng = np.random.default_rng([self.seed, 11, d])
        u = rng.random(self.n)
        second = rng.random(self.n)
        in_run = d < self.days
        if in_run:
            lo, hi = int(self._post_day_edges[d]), int(self._post_day_edges[d + 1])
            rows = np.arange(lo, hi)
            if rows.size:
                owners = data.post_idx[rows]
                removed = self._thin[rows] < self.truth.removal[owners, d]
                self.real_receipts_removed += int(removed.sum())
                for r, gone in zip(rows.tolist(), removed.tolist(), strict=True):
                    if gone:
                        continue
                    i = int(data.post_idx[r])
                    ts = self.start + timedelta(seconds=float(data.post_s[r]))
                    ev = self._purchase(
                        receipt_event_id(data.post_id[r]), data.customer_ids[i], ts, float(data.post_value[r])
                    )
                    out.append(ev)
                    if data.post_s[r] < self._end_s:
                        self.margin_in_run[i] += ev.margin or 0.0
        if d < self._width:
            p = np.minimum(self.truth.incremental[:, d], MAX_INCREMENTAL_PROB)
            for i in np.flatnonzero(u < p).tolist():
                cid = data.customer_ids[i]
                value, time_of_day = self._extra_receipt(i, float(second[i]))
                secs = d * SECONDS_PER_DAY + time_of_day
                ev = self._purchase(
                    extra_event_id(self.seed, cid, d), cid, self.start + timedelta(seconds=secs), value
                )
                out.append(ev)
                self.incremental_purchases += 1
                if secs < self._end_s:
                    self.margin_in_run[i] += ev.margin or 0.0
        return out

    def _extra_receipt(self, i: int, draw: float) -> tuple[float, float]:
        """Amount and second of the UTC day of an extra purchase by customer `i`: those of one of their own receipts
        before the cutoff, picked by `draw` in [0, 1) (of any customer's, when they had none; then the amount is
        the population's mean receipt, which is the customer's `value`)."""
        d = self.data
        lo, hi = int(self._hist_start[i]), int(self._hist_start[i + 1])
        if hi > lo:
            k = int(self._hist_order[lo + min(int(draw * (hi - lo)), hi - lo - 1)])
        elif len(d.hist_s):
            k = min(int(draw * len(d.hist_s)), len(d.hist_s) - 1)
        else:
            return float(d.value[i]), float(int(draw * SECONDS_PER_DAY))
        return float(d.hist_value[k]), float(int(d.hist_s[k] % SECONDS_PER_DAY))

    # --- acting ------------------------------------------------------------------------------------

    def act(self, decisions: Sequence[Decision]) -> None:
        """Carry out ACT decisions at `max(send_at, now)`; NOTHING decisions are ignored."""
        self._act_calls += 1
        rows: list[tuple[Decision, int, datetime]] = []
        for d in decisions:
            if d.kind is not DecisionKind.ACT or d.action is None:
                continue
            rows.append((d, self.truth.index(d.customer_id), max(d.action.send_at, self._now)))
        rounds: list[list[tuple[Decision, int, datetime]]] = []
        seen: dict[int, int] = {}
        for row in rows:
            r = seen.get(row[1], 0)
            seen[row[1]] = r + 1
            if r == len(rounds):
                rounds.append([])
            rounds[r].append(row)
        for r, batch in enumerate(rounds):
            rng = np.random.default_rng([self.seed, 2, self._act_calls, r])
            self._act_round(batch, rng.random((self.n, 5)))

    def _act_round(self, batch: list[tuple[Decision, int, datetime]], draws: FloatArray) -> None:
        d = self.data
        t = self.truth
        idx = np.array([b[1] for b in batch], dtype=np.int64)
        known = [b[0].action.channel in CHANNEL_INDEX for b in batch if b[0].action is not None]
        ch = np.array(
            [CHANNEL_INDEX.get(b[0].action.channel, 0) for b in batch if b[0].action is not None],
            dtype=np.int64,
        )
        secs = np.array([t.seconds(b[2]) for b in batch], dtype=np.float64)
        day = np.maximum(np.floor(secs / SECONDS_PER_DAY), 0).astype(np.int64)
        consent = np.where(ch == CHANNEL_INDEX[Channel.SMS], d.sms_consent[idx], d.email_consent[idx])
        consent &= np.array(known, dtype=np.bool_)
        unsubscribed = self._unsub_s[idx, ch] <= secs
        delivered = consent & ~unsubscribed
        eff = t.register(idx, ch, delivered, day)
        rolls = draws[idx]
        for k, (dec, i, sent) in enumerate(batch):
            action = dec.action
            assert action is not None
            cid = dec.customer_id
            channel = action.channel
            problems: list[str] = []
            utc = f"{sent:%Y-%m-%d %H:%M} UTC"
            if channel not in CHANNEL_INDEX or not consent[k]:
                problems.append(
                    f"{cid}: {channel.value} contact at {utc} without consent (decision {dec.decision_id})"
                )
            elif unsubscribed[k]:
                problems.append(
                    f"{cid}: {channel.value} contact at {utc} after they unsubscribed (decision {dec.decision_id})"
                )
            if in_quiet_hours(sent.astimezone(self._zone).hour, self.quiet_start_hour, self.quiet_end_hour):
                problems.append(
                    f"{cid}: {channel.value} contact at {utc} inside quiet hours (decision {dec.decision_id})"
                )
            self._violations.extend(problems)
            attrs: dict[str, AttrValue] = {"offer_id": action.offer_id}
            if action.content_id is not None:
                attrs["content_id"] = action.content_id
            self._outbound(dec, channel, EventType.SEND, sent, attrs)
            if secs[k] < self._end_s:
                self.send_cost_in_run[i] += self._cost.get(channel, 0.0)
            if not delivered[k]:
                reason = "no_consent" if not consent[k] else "unsubscribed"
                self._outbound(dec, channel, EventType.FAIL, sent + DELIVERY_DELAY, {"reason": reason})
                continue
            self._outbound(dec, channel, EventType.DELIVER, sent + DELIVERY_DELAY, {})
            roll = rolls[k]
            if roll[0] < self.p_click[ch[k], i]:
                delay = timedelta(minutes=2, seconds=round(float(roll[1]) * 6 * 3600))
                self._outbound(dec, channel, EventType.CLICK, sent + delay, {})
            if roll[2] < eff.p_unsubscribe[k]:
                at = sent + UNSUBSCRIBE_DELAY + timedelta(seconds=round(float(roll[3]) * 3600.0))
                self._unsub_s[i, ch[k]] = min(self._unsub_s[i, ch[k]], t.seconds(at))
                self._outbound(dec, channel, EventType.UNSUBSCRIBE, at, {"lost_value": UNSUBSCRIBE_COST})
                if t.seconds(at) < self._end_s:
                    self.lost_in_run[i] += UNSUBSCRIBE_COST
                if roll[4] < COMPLAINT_SHARE:
                    self._outbound(
                        dec, channel, EventType.COMPLAINT, at + DELIVERY_DELAY, {"lost_value": COMPLAINT_COST}
                    )
                    if t.seconds(at) < self._end_s:
                        self.lost_in_run[i] += COMPLAINT_COST

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

    # --- what the scoreboard reads -------------------------------------------------------------------

    def measured_net(self) -> FloatArray:
        """Each customer's realised net over the decision days: purchase margin (real and incremental) less
        message costs and opt-out costs, all counted only up to day `days`. What a business would book."""
        return np.asarray(self.margin_in_run - self.send_cost_in_run - self.lost_in_run, dtype=np.float64)
