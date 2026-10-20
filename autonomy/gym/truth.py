"""The planted truth: every simulated customer's hidden parameters, and the exact value of any action.

`PlantedTruth` draws one hidden profile per customer from a `ScenarioSpec` and a seed: persona,
sleeping-dog flag, timezone, channel consents, own purchase rate, per-channel uplift, offer and theme
multipliers, best hour and hour sensitivity. The Gym world (`autonomy.gym.world`) simulates
purchases from exactly these numbers, so `expected_net` here is the true expected value of an action,
not an estimate. The scoreboard uses it as the oracle; the policy under test never sees it.

The model, per customer and per hour (the world simulates hour by hour):

* Doing nothing, they buy at `daily_rate / 24` per hour (times `drift_purchase_multiplier` from
  `drift_day` on).
* A contact that lands adds `lift / 24` per hour for `effect_days` days, starting at the first whole
  hour at or after the send. `lift` = channel uplift x offer multiplier x theme multiplier x hour
  factor x fatigue factor. Overlapping contacts add up.
* A sleeping dog gets no lift: their own rate is multiplied by `1 + sleeping_dog_effect` (so the
  default -0.5 halves it) for `effect_days` days.
* While a discount is active, a buyer redeems it. With `discount_redeemed_by_sure_things` every buyer
  does (the cost of cannibalisation); otherwise only the buyers the contact created do.

* A delivered contact causes an unsubscribe from its channel with probability
  `(unsubscribe_base + unsubscribe_per_weekly_contact x contacts in the last 7 days)`, times
  `sleeping_dog_unsubscribe_multiplier` for an annoyed sleeping dog; `complaint_share_of_unsubscribes`
  of those unsubscribes also complain.

`expected_net` is incremental margin minus expected offer cost minus channel cost, minus the expected
cost of what the contact breaks: unsubscribe probability x the customer's lost contact value on that
channel, plus complaint probability x `complaint_cost`. A dog already dented by an earlier contact only
loses the hours the new contact adds to the dent. It still ignores one second-order effect on purpose:
the overlap with an offer already active from an earlier contact.

Realism, and why (wave 3a):

* **Lost contact value.** Losing a customer's consent on a channel loses the margin every later
  message on it would have made; a business feels that, so the truth charges it. By default each
  customer's own value is the margin of `lost_contact_sends_per_week` plain messages a week (no offer,
  best hour, no fatigue) on that channel over `lost_contact_horizon_days` (180) days at their planted
  uplift, net of channel cost and never below zero; for a sleeping dog it is zero, because messaging
  them never made money. `lost_contact_value` replaces it with one documented figure for everyone.
  A complaint adds `complaint_cost` on top (0 on a bare spec, $5 in the library): mailbox providers
  route a sender's mail to spam once complaints pass about 0.1-0.3% of sends, which costs every later
  message some reach. The scoreboard sees the same money realised, on UNSUBSCRIBE and COMPLAINT
  events (`lost_value`).
* **Sleeping dogs leave clues.** Annoyance with marketing has a start. Each sleeping dog's started at
  `dog_onset_s`, on average `sleeping_dog_signal` x the history length before day 0 (spread 0.5x to
  1.5x, capped at the start of the history). From then on they are a dog in every respect: no lift,
  a dented own rate, almost no clicks and more unsubscribes. Before it they behave like their persona,
  so with signal 0 the history cannot tell a dog from anyone else, as before. By day 0 every dog is
  annoyed. The flag and the onset are never emitted; only the behaviour is.
* **Unsubscribe risk of annoyed people.** People who resent messages unsubscribe and complain more
  when messaged; `sleeping_dog_unsubscribe_multiplier` (3) is that, in the history and after day 0.

Fatigue, dents and consent change as the world runs, so the world reports contacts (`note_contacts`),
dents (`note_dent`) and unsubscribes (`note_unsubscribe`) here, including the past campaigns of the
history; everything else is fixed by `plant`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from autonomy.contracts import NO_OFFER, Action, Channel, ContentItem, OfferKind, OfferOption, Theme
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.scenarios import GYM_OFFERS

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

DEFAULT_START = datetime(2026, 1, 5, tzinfo=UTC)
"""Decision day 0 when the caller passes no start: a fixed Monday, never the clock."""

CHANNELS: tuple[Channel, ...] = tuple(Channel)
OFFER_KINDS: tuple[OfferKind, ...] = tuple(OfferKind)
THEMES: tuple[Theme, ...] = tuple(Theme)
CATEGORIES: tuple[str, ...] = ("apparel", "home", "beauty", "outdoor", "electronics")
_CH = {c: i for i, c in enumerate(CHANNELS)}
_KIND = {k: i for i, k in enumerate(OFFER_KINDS)}
_THEME = {t: i for i, t in enumerate(THEMES)}
CHANNEL_INDEX: dict[Channel, int] = _CH
"""Column of each channel in the per-channel arrays (`consent`, `uplift`, `lost_value`, ...)."""
KIND_INDEX: dict[OfferKind, int] = _KIND
"""Column of each offer kind in `offer_mult`."""
THEME_INDEX: dict[Theme, int] = _THEME
"""Column of each theme in `theme_mult`."""

DEFAULT_OFFERS: tuple[OfferOption, ...] = GYM_OFFERS
"""The scenario library's offers (the same as `autonomy.fakes.samples`), so the sample brief runs in
the Gym unchanged."""

DEFAULT_CHANNEL_UPLIFT: dict[str, dict[Channel, float]] = {
    # Added daily purchase probability while a contact's effect lasts. Loyal customers barely move
    # (they buy anyway); new and at-risk customers move most, and more on WhatsApp than email.
    "researcher": {Channel.EMAIL: 0.004, Channel.WHATSAPP: 0.003, Channel.SMS: 0.002, Channel.PUSH: 0.002},
    "price_sensitive": {
        Channel.EMAIL: 0.002,
        Channel.WHATSAPP: 0.004,
        Channel.SMS: 0.003,
        Channel.PUSH: 0.001,
    },
    "loyal": {Channel.EMAIL: 0.001, Channel.WHATSAPP: 0.001, Channel.SMS: 0.0005, Channel.PUSH: 0.0005},
    "new": {Channel.EMAIL: 0.005, Channel.WHATSAPP: 0.006, Channel.SMS: 0.003, Channel.PUSH: 0.003},
    "at_risk": {Channel.EMAIL: 0.003, Channel.WHATSAPP: 0.006, Channel.SMS: 0.004, Channel.PUSH: 0.002},
}
GENERIC_CHANNEL_UPLIFT: dict[Channel, float] = {Channel.EMAIL: 0.003, Channel.WHATSAPP: 0.004}
"""For a persona the defaults do not know. Channels missing everywhere have no uplift."""

DEFAULT_OFFER_UPLIFT: dict[str, dict[str, float]] = {
    # Multiplier on the channel uplift. Price-sensitive customers respond strongly to money off;
    # loyal customers barely need an offer (so a discount to them is mostly wasted); researchers
    # respond to useful content.
    "researcher": {"percent_off": 1.3, "amount_off": 1.2, "free_shipping": 1.2, "gift": 1.2, "content": 1.5},
    "price_sensitive": {
        "percent_off": 3.0,
        "amount_off": 2.6,
        "free_shipping": 2.0,
        "gift": 1.5,
        "loyalty": 1.1,
        "content": 0.8,
    },
    "loyal": {"percent_off": 1.1, "amount_off": 1.1, "free_shipping": 1.05, "gift": 1.2, "loyalty": 1.5},
    "new": {"percent_off": 1.8, "amount_off": 1.6, "free_shipping": 1.5, "gift": 1.3, "content": 1.3},
    "at_risk": {"percent_off": 2.0, "amount_off": 1.8, "free_shipping": 1.4, "gift": 1.3, "loyalty": 1.3},
}

DEFAULT_THEME_UPLIFT: dict[str, dict[str, float]] = {
    # Multiplier on the channel uplift; 1.0 where not listed.
    "researcher": {"education": 1.6, "quality": 1.3, "social_proof": 1.2, "savings": 0.9, "urgency": 0.7},
    "price_sensitive": {"savings": 1.6, "urgency": 1.3, "quality": 0.9},
    "loyal": {"loyalty": 1.5, "personal": 1.3, "novelty": 1.2, "urgency": 0.8},
    "new": {"social_proof": 1.4, "education": 1.3, "convenience": 1.2},
    "at_risk": {"personal": 1.4, "reminder": 1.3, "savings": 1.2, "novelty": 1.2},
}

FATIGUE_WINDOW = timedelta(days=7)
_CONTACT_SLOTS = 16  # remembered contacts per customer; fatigue is at its floor long before 16 a week
_UPLIFT_SIGMA = 0.6  # customer-to-customer spread of uplift around the persona mean (lognormal)
_RATE_SIGMA = 0.5  # spread of the own purchase rate
_MULT_SIGMA = 0.3  # spread of offer and theme multipliers around the persona value
_HOUR_SD = 1.5  # spread of the best hour around the persona's, in hours
_DEFAULT_RATE = 0.005


def _merge(
    defaults: Mapping[str, Mapping[str, float]], spec: Mapping[str, Mapping[str, float]]
) -> dict[str, dict[str, float]]:
    """The spec's entries laid over the defaults, per (persona, key). An empty spec dict means defaults."""
    out = {p: dict(v) for p, v in defaults.items()}
    for p, v in spec.items():
        out.setdefault(p, {}).update(v)
    return out


def _mean_one_lognormal(rng: np.random.Generator, sigma: float, size: int) -> FloatArray:
    """Positive noise with mean 1, so persona means stay the population means."""
    return np.asarray(rng.lognormal(-(sigma**2) / 2, sigma, size), dtype=np.float64)


class PlantedTruth:
    """The hidden profile of every customer in one Gym world, and the oracle built on it.

    Arrays are indexed by customer position `i`; `customer_ids[i]` is the id the world uses in events.
    Times inside are seconds since `start` (decision day 0, 00:00).
    """

    def __init__(
        self,
        spec: ScenarioSpec,
        seed: int | None = None,
        *,
        start: datetime = DEFAULT_START,
        offers: Sequence[OfferOption] = DEFAULT_OFFERS,
        content: Sequence[ContentItem] = (),
    ) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self.spec = spec
        self.seed = spec.seed if seed is None else seed
        self.start = start
        self.offers: dict[str, OfferOption] = {o.offer_id: o for o in (NO_OFFER, *offers)}
        self.themes: dict[str, Theme] = {c.content_id: c.theme for c in content}
        n = spec.n_customers
        self.n = n
        self.customer_ids: tuple[str, ...] = tuple(f"gym_{i:06d}" for i in range(n))
        self._index = {cid: i for i, cid in enumerate(self.customer_ids)}
        self.plant(self.seed)

    def plant(self, seed: int) -> None:
        """Draw every customer's hidden profile from `seed` and forget all contacts and unsubscribes.

        The world calls this on `reset(seed)` rather than building a new object, so a policy that
        was handed this truth before the reset (the oracle) still reads the live one.
        """
        spec = self.spec
        n = self.n
        self.seed = seed
        rng = np.random.default_rng([seed, 0])

        # Persona and the flags that hang off it.
        self.personas: tuple[str, ...] = tuple(spec.persona_mix)
        if not self.personas:
            raise ValueError("persona_mix is empty")
        mix = np.array([max(spec.persona_mix[p], 0.0) for p in self.personas], dtype=np.float64)
        if mix.sum() <= 0:
            raise ValueError("persona_mix has no positive share")
        self.persona_idx: IntArray = rng.choice(len(self.personas), size=n, p=mix / mix.sum()).astype(
            np.int64
        )
        self.sleeping_dog: BoolArray = rng.random(n) < spec.sleeping_dog_share

        # Timezone.
        self.tz_names: tuple[str, ...] = tuple(spec.timezones) or ("UTC",)
        self._zones = tuple(ZoneInfo(z) for z in self.tz_names)
        tz_w = np.array([max(spec.timezones.get(z, 1.0), 0.0) for z in self.tz_names], dtype=np.float64)
        tz_w = tz_w / tz_w.sum() if tz_w.sum() > 0 else np.full(len(tz_w), 1.0 / len(tz_w))
        self.tz_idx: IntArray = rng.choice(len(self.tz_names), size=n, p=tz_w).astype(np.int64)

        # Consent per channel; unsubscribes arrive while the world runs.
        self.consent: BoolArray = np.zeros((n, len(CHANNELS)), dtype=np.bool_)
        for ch, share in spec.channel_consent.items():
            self.consent[:, _CH[ch]] = rng.random(n) < share
        self.unsub_at: FloatArray = np.full((n, len(CHANNELS)), np.inf)

        # Own purchase rate.
        persona_rate = np.array(
            [spec.daily_purchase_rate.get(p, _DEFAULT_RATE) for p in self.personas], dtype=np.float64
        )
        self.daily_rate: FloatArray = persona_rate[self.persona_idx] * _mean_one_lognormal(
            rng, _RATE_SIGMA, n
        )

        # Per-channel uplift (added daily purchase probability).
        ch_table = _merge(
            {
                p: {c.value: v for c, v in DEFAULT_CHANNEL_UPLIFT.get(p, GENERIC_CHANNEL_UPLIFT).items()}
                for p in self.personas
            },
            {p: {c.value: v for c, v in d.items()} for p, d in spec.channel_uplift.items()},
        )
        self.uplift: FloatArray = np.zeros((n, len(CHANNELS)), dtype=np.float64)
        for j, ch in enumerate(CHANNELS):
            means = np.array([ch_table[p].get(ch.value, 0.0) for p in self.personas], dtype=np.float64)
            self.uplift[:, j] = np.maximum(means[self.persona_idx], 0.0) * _mean_one_lognormal(
                rng, _UPLIFT_SIGMA, n
            )

        # Offer and theme multipliers on the uplift.
        self.offer_mult = self._multipliers(
            rng, _merge(DEFAULT_OFFER_UPLIFT, spec.offer_uplift), [k.value for k in OFFER_KINDS]
        )
        self.offer_mult[:, _KIND[OfferKind.NONE]] = 1.0
        self.theme_mult = self._multipliers(
            rng, _merge(DEFAULT_THEME_UPLIFT, spec.theme_uplift), [t.value for t in THEMES]
        )

        # Timing.
        persona_hour = np.array([spec.best_hour.get(p, 12) for p in self.personas], dtype=np.float64)
        self.best_hour: IntArray = np.mod(
            np.rint(persona_hour[self.persona_idx] + rng.normal(0.0, _HOUR_SD, n)), 24
        ).astype(np.int64)
        self.hour_sens: FloatArray = np.clip(spec.hour_sensitivity * rng.uniform(0.5, 1.5, n), 0.0, 1.0)

        # What the history needs: when they signed up, and what they usually buy.
        hist = float(spec.history_days)
        is_new = np.array([p == "new" for p in self.personas], dtype=np.bool_)[self.persona_idx]
        old_days = hist * 0.5 + rng.random(n) * (hist * 0.5 + 365.0)
        new_days = rng.uniform(0.05, max(1.0, min(30.0, hist)), n)
        self.signup_s: FloatArray = -np.where(is_new, new_days, old_days) * 86_400.0
        self.favourite: IntArray = rng.integers(0, len(CATEGORIES), n).astype(np.int64)

        # Realism draws come from their own stream, so every array above is the same as before they
        # existed and old seeds plant the same customers.
        extra = np.random.default_rng([seed, 5])
        spread = extra.uniform(0.5, 1.5, n)
        onset_days = np.minimum(spec.sleeping_dog_signal * spread, 1.0) * hist
        self.dog_onset_s: FloatArray = np.where(self.sleeping_dog, -onset_days * 86_400.0, np.inf)
        self.lost_value: FloatArray = self._lost_contact_values()

        # Contact memory for fatigue: a small ring of send times per customer.
        self._contact_s: FloatArray = np.full((n, _CONTACT_SLOTS), -np.inf)
        self._contact_ptr: IntArray = np.zeros(n, dtype=np.int64)
        # The hour (from start) until which a sleeping dog's own rate is already dented.
        self._dent_until_h: FloatArray = np.full(n, -np.inf)

    def _lost_contact_values(self) -> FloatArray:
        """Margin lost per customer and channel when they unsubscribe from it (see the module notes)."""
        spec = self.spec
        if spec.lost_contact_value is not None:
            return np.full((self.n, len(CHANNELS)), float(spec.lost_contact_value), dtype=np.float64)
        sends = spec.lost_contact_sends_per_week * spec.lost_contact_horizon_days / 7.0
        cost = np.array([spec.channel_cost.get(c, 0.0) for c in CHANNELS], dtype=np.float64)
        per_send = self.uplift * spec.effect_days * self.margin_per_purchase - cost[None, :]
        value = np.maximum(per_send, 0.0) * sends
        return np.asarray(np.where(self.sleeping_dog[:, None], 0.0, value), dtype=np.float64)

    def _multipliers(
        self, rng: np.random.Generator, table: Mapping[str, Mapping[str, float]], keys: Sequence[str]
    ) -> FloatArray:
        out = np.ones((self.n, len(keys)), dtype=np.float64)
        for j, key in enumerate(keys):
            means = np.array([table.get(p, {}).get(key, 1.0) for p in self.personas], dtype=np.float64)
            m = means[self.persona_idx]
            noise = _mean_one_lognormal(rng, _MULT_SIGMA, self.n)
            out[:, j] = np.maximum(1.0 + (m - 1.0) * noise, 0.0)
        return out

    # --- lookups -------------------------------------------------------------------------------

    def index(self, customer_id: str) -> int:
        try:
            return self._index[customer_id]
        except KeyError:
            raise KeyError(f"{customer_id!r} is not a customer of this Gym world") from None

    def persona(self, customer_id: str) -> str:
        return self.personas[int(self.persona_idx[self.index(customer_id)])]

    def is_sleeping_dog(self, customer_id: str) -> bool:
        return bool(self.sleeping_dog[self.index(customer_id)])

    def timezone(self, i: int) -> str:
        return self.tz_names[int(self.tz_idx[i])]

    def offer(self, offer_id: str) -> OfferOption:
        try:
            return self.offers[offer_id]
        except KeyError:
            raise KeyError(f"offer {offer_id!r} is unknown to this Gym world; pass it in `offers`") from None

    def offer_cost(self, offer: OfferOption) -> float:
        """Cost to the business per redemption. Uses the offer's own figure; derives one for money-off
        offers that leave it at zero."""
        if offer.cost_per_redemption > 0:
            return offer.cost_per_redemption
        if offer.kind is OfferKind.PERCENT_OFF:
            return offer.amount / 100.0 * self.spec.aov_mean
        if offer.kind is OfferKind.AMOUNT_OFF:
            return offer.amount
        return 0.0

    def theme_of(self, content_id: str | None) -> Theme | None:
        """The content item's theme, or None when the item is unknown (no theme effect)."""
        return None if content_id is None else self.themes.get(content_id)

    @property
    def margin_per_purchase(self) -> float:
        return self.spec.aov_mean * self.spec.margin_rate

    # --- time ----------------------------------------------------------------------------------

    def seconds(self, t: datetime) -> float:
        """Seconds since `start`."""
        return (t - self.start).total_seconds()

    def hour_at_or_after(self, t: datetime) -> int:
        """The index of the first whole hour starting at or after `t`: where a contact's effect begins."""
        return math.ceil(self.seconds(t) / 3600.0 - 1e-9)

    def local_hours(self, t: datetime) -> FloatArray:
        """Each timezone's local clock hour at `t`, with minutes as a fraction."""
        out = np.empty(len(self._zones), dtype=np.float64)
        for k, z in enumerate(self._zones):
            lt = t.astimezone(z)
            out[k] = lt.hour + lt.minute / 60.0
        return out

    def local_time(self, i: int, t: datetime) -> datetime:
        return t.astimezone(self._zones[int(self.tz_idx[i])])

    def drift_hour(self) -> float:
        d = self.spec.drift_day
        return math.inf if d is None else d * 24.0

    def rate_multiplier(self, hour: int) -> float:
        """Multiplier on everyone's own purchase rate in this hour (the drift)."""
        return self.spec.drift_purchase_multiplier if hour >= self.drift_hour() else 1.0

    def dog_multiplier(self) -> float:
        """What a contact does to a sleeping dog's own rate while its effect lasts."""
        return max(0.0, 1.0 + self.spec.sleeping_dog_effect)

    def annoyed(self, idx: IntArray, times_s: FloatArray) -> BoolArray:
        """Sleeping dogs whose annoyance had begun by each time (all of them from day 0 on)."""
        return np.asarray(self.sleeping_dog[idx] & (self.dog_onset_s[idx] <= times_s), dtype=np.bool_)

    def unsubscribe_prob(self, prior_7d: IntArray, annoyed: BoolArray) -> FloatArray:
        """Chance a delivered contact causes an unsubscribe, given contacts already sent this week."""
        spec = self.spec
        p = spec.unsubscribe_base + spec.unsubscribe_per_weekly_contact * prior_7d
        p = p * np.where(annoyed, spec.sleeping_dog_unsubscribe_multiplier, 1.0)
        return np.asarray(np.clip(p, 0.0, 1.0), dtype=np.float64)

    def lost_contact_value(self, customer_id: str, channel: Channel) -> float:
        """Margin lost if this customer unsubscribes from `channel` (truth only; for scoring)."""
        return float(self.lost_value[self.index(customer_id), _CH[channel]])

    # --- dynamic state the world reports -------------------------------------------------------

    def note_contacts(self, idx: IntArray, times_s: FloatArray) -> None:
        """Remember sends (for fatigue). `idx` must not repeat within one call."""
        slot = self._contact_ptr[idx] % _CONTACT_SLOTS
        self._contact_s[idx, slot] = times_s
        self._contact_ptr[idx] += 1

    def note_unsubscribe(self, i: int, channel: Channel, t: datetime) -> None:
        j = _CH[channel]
        self.unsub_at[i, j] = min(float(self.unsub_at[i, j]), self.seconds(t))

    def note_dent(self, i: int, end_hour: int) -> None:
        """A delivered contact dents sleeping dog `i`'s own rate until `end_hour` (hours from start)."""
        self._dent_until_h[i] = max(float(self._dent_until_h[i]), float(end_hour))

    def contacts_7d(self, idx: IntArray, times_s: FloatArray) -> IntArray:
        """Contacts already noted in the 7 days up to each time, including one noted at that very
        instant (a second contact in the same moment is still a second contact)."""
        mem = self._contact_s[idx]
        s = times_s[:, None]
        recent = (mem > s - FATIGUE_WINDOW.total_seconds()) & (mem <= s)
        return np.asarray(recent.sum(axis=1), dtype=np.int64)

    # --- the model, row by row -----------------------------------------------------------------

    def evaluate(self, idx: IntArray, actions: Sequence[Action], times: Sequence[datetime]) -> Evaluation:
        """Everything the model says about sending `actions[k]` to customer `idx[k]` at `times[k]`.

        Fatigue counts the contacts already noted, so evaluate before `note_contacts`.
        """
        rows = len(actions)
        if not rows == len(idx) == len(times):
            raise ValueError("idx, actions and times must have the same length")
        ch = np.empty(rows, dtype=np.int64)
        kind = np.empty(rows, dtype=np.int64)
        theme = np.empty(rows, dtype=np.int64)
        offer_cost = np.empty(rows, dtype=np.float64)
        channel_cost = np.empty(rows, dtype=np.float64)
        t_s = np.empty(rows, dtype=np.float64)
        local = np.empty(rows, dtype=np.float64)
        hours_cache: dict[datetime, FloatArray] = {}
        offer_cache: dict[str, tuple[int, float]] = {}
        for k, (a, t) in enumerate(zip(actions, times, strict=True)):
            ch[k] = _CH[a.channel]
            if a.offer_id not in offer_cache:
                offer = self.offer(a.offer_id)
                offer_cache[a.offer_id] = (_KIND[offer.kind], self.offer_cost(offer))
            kind[k], offer_cost[k] = offer_cache[a.offer_id]
            th = self.theme_of(a.content_id)
            theme[k] = -1 if th is None else _THEME[th]
            channel_cost[k] = self.spec.channel_cost.get(a.channel, 0.0)
            t_s[k] = self.seconds(t)
            if t not in hours_cache:
                hours_cache[t] = self.local_hours(t)
            local[k] = hours_cache[t][self.tz_idx[idx[k]]]
        return self._evaluate(idx, ch, kind, theme, offer_cost, channel_cost, t_s, local)

    def _evaluate(
        self,
        idx: IntArray,
        ch: IntArray,
        kind: IntArray,
        theme: IntArray,
        offer_cost: FloatArray,
        channel_cost: FloatArray,
        t_s: FloatArray,
        local_hour: FloatArray,
    ) -> Evaluation:
        spec = self.spec
        days = spec.effect_days
        # Hour factor: 1 at the best hour, 1 - sensitivity twelve hours away.
        d = np.abs(local_hour - self.best_hour[idx]) % 24.0
        d = np.minimum(d, 24.0 - d)
        hour_factor = 1.0 - self.hour_sens[idx] * d / 12.0
        prior = self.contacts_7d(idx, t_s)
        fatigue = np.maximum(0.0, 1.0 - spec.fatigue_per_weekly_contact * prior)
        theme_mult = np.where(theme >= 0, self.theme_mult[idx, np.maximum(theme, 0)], 1.0)
        dog = self.annoyed(idx, t_s)
        lift = self.uplift[idx, ch] * self.offer_mult[idx, kind] * theme_mult * hour_factor * fatigue
        lift = np.where(dog, 0.0, lift)

        # Expected purchases over the effect window, without and with the contact.
        h0 = np.ceil(t_s / 3600.0 - 1e-9)
        end = h0 + 24.0 * days
        organic = self._organic(idx, h0, end)
        extra = lift * days
        dog_mult = self.dog_multiplier()
        # A dog already dented by an earlier contact only loses the hours the new one adds.
        fresh = self._organic(idx, np.maximum(h0, self._dent_until_h[idx]), end)
        incremental = np.where(dog, (dog_mult - 1.0) * fresh, extra)
        if spec.discount_redeemed_by_sure_things:
            redemptions = np.where(dog, dog_mult * organic, organic + extra)
        else:
            redemptions = np.where(dog, 0.0, extra)
        # What the contact breaks: the channel (lost contact value) and, for complainers, deliverability.
        p_unsub = self.unsubscribe_prob(prior, dog)
        p_complaint = p_unsub * min(max(spec.complaint_share_of_unsubscribes, 0.0), 1.0)
        lost = self.lost_value[idx, ch]
        harm = p_unsub * lost + p_complaint * spec.complaint_cost
        value = incremental * self.margin_per_purchase - redemptions * offer_cost - channel_cost - harm

        consent = self.consent[idx, ch]
        unsubscribed = self.unsub_at[idx, ch] <= t_s
        reachable = consent & ~unsubscribed
        return Evaluation(
            net=np.asarray(np.where(reachable, value, -channel_cost), dtype=np.float64),
            lift_per_day=np.asarray(lift, dtype=np.float64),
            prior_contacts_7d=prior,
            consent=np.asarray(consent, dtype=np.bool_),
            unsubscribed=np.asarray(unsubscribed, dtype=np.bool_),
            local_hour=local_hour,
            hour_factor=np.asarray(hour_factor, dtype=np.float64),
            offer_cost=offer_cost,
            p_unsubscribe=np.asarray(p_unsub, dtype=np.float64),
            lost_value=np.asarray(lost, dtype=np.float64),
            annoyed=np.asarray(dog, dtype=np.bool_),
        )

    def evaluate_rows(
        self,
        idx: IntArray,
        ch: IntArray,
        kind: IntArray,
        theme: IntArray,
        offer_cost: FloatArray,
        channel_cost: FloatArray,
        t_s: FloatArray,
        local_hour: FloatArray,
    ) -> Evaluation:
        """`evaluate` for rows already coded as arrays (channel, offer kind and theme positions, -1 for
        no theme): what the world's history uses to value thousands of past sends at once."""
        return self._evaluate(idx, ch, kind, theme, offer_cost, channel_cost, t_s, local_hour)

    def _organic(self, idx: IntArray, from_h: FloatArray, to_h: FloatArray) -> FloatArray:
        """Expected own purchases between two hours (from start), with the drift where it applies."""
        length = np.maximum(to_h - from_h, 0.0)
        drifted = np.clip(to_h - np.maximum(from_h, self.drift_hour()), 0.0, length)
        mult = self.spec.drift_purchase_multiplier
        return np.asarray(
            self.daily_rate[idx] / 24.0 * ((length - drifted) + drifted * mult), dtype=np.float64
        )

    def net(self, idx: IntArray, action: Action, now: datetime) -> FloatArray:
        """`expected_net` of one action for many customers at once."""
        t = max(now, action.send_at)
        rows = len(idx)
        offer = self.offer(action.offer_id)
        th = self.theme_of(action.content_id)
        ev = self._evaluate(
            idx,
            np.full(rows, _CH[action.channel], dtype=np.int64),
            np.full(rows, _KIND[offer.kind], dtype=np.int64),
            np.full(rows, -1 if th is None else _THEME[th], dtype=np.int64),
            np.full(rows, self.offer_cost(offer), dtype=np.float64),
            np.full(rows, self.spec.channel_cost.get(action.channel, 0.0), dtype=np.float64),
            np.full(rows, self.seconds(t), dtype=np.float64),
            self.local_hours(t)[self.tz_idx[idx]],
        )
        return ev.net

    def expected_net_all(self, action: Action, now: datetime) -> FloatArray:
        """`expected_net` for every customer, in `customer_ids` order: fast enough for a daily oracle."""
        return self.net(np.arange(self.n, dtype=np.int64), action, now)

    # --- GroundTruth ---------------------------------------------------------------------------

    def expected_net(self, customer_id: str, action: Action, now: datetime) -> float:
        """True expected incremental margin minus offer and channel cost, vs not sending this action,
        given the customer's fatigue at the send time (`max(now, action.send_at)`). A send the
        customer cannot receive (no consent, unsubscribed) has no effect and still costs the fee."""
        idx = np.array([self.index(customer_id)], dtype=np.int64)
        return float(self.net(idx, action, now)[0])

    def best_action(self, customer_id: str, candidates: Sequence[Action], now: datetime) -> Action | None:
        """The candidate with the highest `expected_net`, or None when none is above zero."""
        best: Action | None = None
        best_value = 0.0
        for a in candidates:
            v = self.expected_net(customer_id, a, now)
            if v > best_value:
                best, best_value = a, v
        return best


@dataclass(frozen=True)
class Evaluation:
    """The model's view of a batch of (customer, action, time) rows, one array entry per row."""

    net: FloatArray
    """True expected net value of the send (see `PlantedTruth.expected_net`)."""
    lift_per_day: FloatArray
    """Added daily purchase probability while the effect lasts; 0 for sleeping dogs."""
    prior_contacts_7d: IntArray
    consent: BoolArray
    unsubscribed: BoolArray
    local_hour: FloatArray
    hour_factor: FloatArray
    offer_cost: FloatArray
    """Cost per redemption of the row's offer."""
    p_unsubscribe: FloatArray
    """Chance the contact, if delivered, causes an unsubscribe from its channel."""
    lost_value: FloatArray
    """Margin lost if it does (the customer's lost contact value on that channel)."""
    annoyed: BoolArray
    """A sleeping dog whose annoyance had begun by the send time: no lift, a dent, rare clicks."""
