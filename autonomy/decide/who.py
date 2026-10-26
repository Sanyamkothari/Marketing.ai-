"""Who to contact and who to leave alone (plan-v3 decision 1), opt-in.

Three pieces, all behind one switch (`build_agent_policy(who=...)`, `GoalBidder(who=...)`); with it off
every number of the agent is what it was.

* **Leaving risk and 90-day value** (`autonomy.decide.slow_models`): trained on the profile at past
  month-ends, used only after a held-out check passes, written to `CustomerState.scores` as
  `churn_risk` and `clv_90d`.
* **Bidders read them.** Leaving risk raises urgency: the bid of a customer likely to lapse is worth
  `1 + risk_urgency x churn_risk` times as much, and since the whole expected net scales, a message
  that would lose money for them loses more (urgency makes good bids better and bad ones worse). 90-day
  value caps what an action may cost: an option whose expected cost (offer cost x chance of redeeming
  + channel cost) is more than `value_cost_share` of `clv_90d` is not bid. `clv_90d` also reaches the
  attention price, which already reads it (`ValueModel.future_value`).
* **Sleeping dogs by traits** (`TraitHarm`). `ContactHarm` reads each customer's own purchases around
  messages, shrunk towards neighbours along three clues (clicks, direct visits, unsubscribes). A
  customer with a thin record, or none, is left alone only if those neighbours say so. `TraitHarm`
  adds a second neighbourhood: customers who share a few profile traits (how they answer messages,
  how much they need an offer, how many messages they had last month). Each group's records are pooled
  on the same ratio grid, shrunk towards the business's by as much as groups really differ beyond
  their own noise (empirical Bayes, `_spread_between`), and a customer's posterior is their group's
  (themselves left out) times their own record, spread by the person-to-person `tau`. A thin record
  stays near its group; a long one is mostly the person. A customer is left alone when
  `LEAVE_ALONE_CREDIBLE` of that posterior lies below a ratio of 1. A customer with no record of their
  own is read by their group alone. It is added to `leave_alone`, never in place of it.

Numpy only. `WhoBook` is the object the agent and the bidders share; it keeps the events the agent has
been handed (the profile is built from them when the state carries no nightly features) and refits
the slow models when a month-end's window has closed, and the trait groups when the harm model has
been refit and `trait_refit_days` have passed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import numpy.typing as npt

from autonomy.contracts import CustomerState, Event, EventType
from autonomy.decide.bidders import (
    _LOG_GRID,
    _RATIO_GRID,
    _TINY,
    HARM_BLOCK,
    HARM_MIN_CUSTOMERS,
    HARM_RATIO_SD,
    HARM_TAUS,
    LEAVE_ALONE_CREDIBLE,
    ContactHarm,
    _normal_on_grid,
    _own_loglik,
    _spread_between,
)
from autonomy.decide.slow_models import CHURN_RISK, CLV_90D, SlowModelConfig, SlowModels, month_ends
from autonomy.profile import profile_for

__all__ = ["TraitHarm", "TraitKeys", "WhoBook", "WhoConfig"]

_BELOW = _RATIO_GRID < 1.0
_FloatArray = npt.NDArray[np.float64]
_DAY = timedelta(days=1)


@dataclass(frozen=True)
class WhoConfig:
    """The numbers of decision 1. `WhoConfig()` is everything on; each piece has its own off value."""

    slow: SlowModelConfig = field(default_factory=SlowModelConfig)
    models: bool = True
    """Fit the slow models and write their scores. False: the scores of the state are left as they are."""
    risk_urgency: float = 0.5
    """Urgency is multiplied by `1 + risk_urgency x churn_risk` (0: leaving risk does not touch it)."""
    value_cost_share: float | None = 0.25
    """An option costing more than this share of `clv_90d` is not bid (None: no ceiling)."""
    trait_harm: bool = True
    """Pool the harm reading across profile neighbours and leave alone on it."""
    trait_min_group: int = 20
    """A trait group with fewer customers with a record than this is not read."""
    trait_refit_days: int = 7
    load_days: int = 30
    load_cut: int = 5
    """Messages in `load_days` from which a customer counts as heavily contacted ("5 or more last month")."""
    offer_cut: float = 0.5
    """`price_offer_sensitivity` from which a customer counts as a deal buyer."""
    max_profile_age_days: float = 3.0
    """Nightly features older than this are rebuilt from the events the agent holds, when it holds some."""

    def __post_init__(self) -> None:
        if self.risk_urgency < 0:
            raise ValueError("risk_urgency must not be negative")
        if self.value_cost_share is not None and self.value_cost_share <= 0:
            raise ValueError("value_cost_share must be positive (or None for no ceiling)")
        if self.trait_min_group < 1 or self.trait_refit_days < 1:
            raise ValueError("trait_min_group and trait_refit_days must be at least 1")


# --- trait groups ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TraitKeys:
    """How a customer is put in a group: three traits, each cut in two or three.

    * *answers*: never clicked or replied / answers less than the usual engaged customer / more;
    * *offers*: needs a deal to buy (`price_offer_sensitivity` at or above `offer_cut`) or not;
    * *load*: had `load_cut` or more messages in the last `load_days` or fewer.
    """

    engage_cut: float = 0.0
    offer_cut: float = 0.5
    load_cut: int = 5

    def key(self, features: Mapping[str, float], load: int) -> str:
        if features.get("ch_engaged", 0.0) < 0.5:
            answers = "silent"
        else:
            answers = (
                "answers_less" if features.get("ch_click_rate", 0.0) < self.engage_cut else "answers_more"
            )
        offers = "deals" if features.get("price_offer_sensitivity", 0.3) >= self.offer_cut else "full_price"
        return f"{answers}|{offers}|{'heavy' if load >= self.load_cut else 'light'}"


class TraitHarm:
    """The harm reading pooled across customers who share profile traits.

    `fit(records, keys)` takes each customer's record around messages (`ContactHarm.own_records`:
    purchases in a message's week, purchases, that week's share of their time) and their group key.
    Records are read on the ratio grid of `ContactHarm`: buying in a message's week as a multiple of
    buying without one. A group's reading is the sum of its members' log-likelihoods, with a Normal
    prior centred on the business's ratio whose spread is how far groups really differ
    (`_spread_between`), so a group that merely looks low by chance is not believed. A member is read
    against their group with themselves left out, then against their own record, spread by the
    person-to-person `tau` the records make most likely: a thin record stays near the group, a long
    one is mostly the person. A customer with no record is read by their group alone.
    """

    def __init__(
        self,
        *,
        credible: float = LEAVE_ALONE_CREDIBLE,
        min_group: int = 20,
        min_customers: int = HARM_MIN_CUSTOMERS,
        ratio_sd: float = HARM_RATIO_SD,
        taus: Sequence[float] = HARM_TAUS,
        block: int = HARM_BLOCK,
    ) -> None:
        if not 0.5 <= credible < 1 or min_group < 1 or ratio_sd <= 0 or not taus or min(taus) <= 0:
            raise ValueError("credible must be in [0.5, 1); min_group and ratio_sd and taus positive")
        self.credible = credible
        self.min_group = min_group
        self.min_customers = min_customers
        self.ratio_sd = ratio_sd
        self.taus = tuple(float(t) for t in taus)
        self.block = block
        self.tau = 0.0
        self.spread = 0.0
        self.business = 1.0
        self.customers = 0
        self._p: dict[str, float] = {}
        self._group_p: dict[str, float] = {}
        self._group_ratio: dict[str, float] = {}
        self._group_size: dict[str, int] = {}

    @property
    def fitted(self) -> bool:
        return bool(self._group_p)

    def groups(self) -> dict[str, tuple[int, float, float]]:
        """Group key -> (customers with a record, posterior mean ratio, chance the ratio is below 1)."""
        return {
            g: (self._group_size[g], self._group_ratio[g], self._group_p[g]) for g in sorted(self._group_p)
        }

    def p_harm(self, customer_id: str, key: str | None = None) -> float:
        """Chance a message lowers this customer's buying, on their group and their own record. A
        customer without a record is read by the group `key` alone; 0 for a stranger."""
        found = self._p.get(customer_id)
        if found is not None:
            return found
        if key is not None and self._group_size.get(key, 0) >= self.min_group:
            return self._group_p.get(key, 0.0)
        return 0.0

    def leave_alone(self, customer_id: str, at_stake: float, key: str | None = None) -> bool:
        """Whether the people like this customer credibly buy less after a message. Nobody is left
        alone with nothing at stake."""
        return at_stake > 0 and self.p_harm(customer_id, key) >= self.credible

    def left_alone(self) -> int:
        """Customers with a record whom `leave_alone` holds back (given something at stake)."""
        return sum(1 for p in self._p.values() if p >= self.credible)

    def describe(self) -> str:
        if not self.fitted:
            return "Not enough history yet to read who is put off by messages from people like them."
        low = [g for g, (n, _r, p) in self.groups().items() if n >= self.min_group and p >= self.credible]
        return (
            f"{len(low)} of {len(self._group_p)} groups of similar customers credibly buy less after a "
            f"message ({', '.join(low)}); {self.left_alone()} customers are left alone on their group's "
            "evidence and their own record."
            if low
            else f"No group of similar customers credibly buys less after a message ({len(self._group_p)} read)."
        )

    def fit(self, records: Mapping[str, tuple[float, float, float]], keys: Mapping[str, str]) -> None:
        """Read every group and every member. Customers without a key are left out."""
        ids = sorted(c for c in records if c in keys)
        self._p, self._group_p, self._group_ratio, self._group_size = {}, {}, {}, {}
        self.customers = len(ids)
        if len(ids) < self.min_customers:
            return
        hits = np.array([records[c][0] for c in ids], dtype=np.float64)
        trials = np.array([records[c][1] for c in ids], dtype=np.float64)
        share = np.array([records[c][2] for c in ids], dtype=np.float64)
        ok = (trials > 0) & (share > 0) & (share < 1)
        names = sorted({keys[c] for c in ids})
        code = np.array([names.index(keys[c]) for c in ids], dtype=np.int64)
        n, g = len(ids), len(names)

        def rows(a: int, b: int) -> _FloatArray:
            return _own_loglik(hits[a:b], trials[a:b], share[a:b], ok[a:b])

        sums = np.zeros((g, len(_LOG_GRID)))
        for a in range(0, n, self.block):
            b = min(a + self.block, n)
            ll = rows(a, b)
            for k in np.unique(code[a:b]).tolist():
                sums[k] += ll[code[a:b] == k].sum(axis=0)
        self.business = float(_LOG_GRID[int(np.argmax(sums.sum(axis=0)))])
        self.spread = _spread_between(list(sums), self.ratio_sd)
        prior = -0.5 * ((_LOG_GRID - self.business) / self.spread) ** 2
        sizes = np.bincount(code, minlength=g)
        for k, name in enumerate(names):
            score = sums[k] + prior
            post = np.exp(score - score.max())
            post /= post.sum()
            self._group_p[name] = float(post[_BELOW].sum())
            self._group_ratio[name] = float((post * _RATIO_GRID).sum())
            self._group_size[name] = int(sizes[k])
        # each member against their group with themselves left out
        near_mean = np.empty(n)
        near_sd = np.empty(n)
        for a in range(0, n, self.block):
            b = min(a + self.block, n)
            score = sums[code[a:b]] - rows(a, b) + prior[None, :]
            post = np.exp(score - score.max(axis=1, keepdims=True))
            post /= post.sum(axis=1, keepdims=True)
            m = (post * _LOG_GRID[None, :]).sum(axis=1)
            near_mean[a:b] = m
            near_sd[a:b] = np.sqrt(
                np.maximum((post * (_LOG_GRID[None, :] - m[:, None]) ** 2).sum(axis=1), 0.0)
            )
        # the person-to-person spread the records make most likely
        marginal = np.zeros(len(self.taus))
        for a in range(0, n, self.block):
            b = min(a + self.block, n)
            ll = rows(a, b)
            lik = np.exp(ll - ll.max(axis=1, keepdims=True))
            for t, tau in enumerate(self.taus):
                pri = _normal_on_grid(near_mean[a:b], np.sqrt(near_sd[a:b] ** 2 + tau * tau))
                marginal[t] += float(np.log(np.maximum((pri * lik)[ok[a:b]].sum(axis=1), _TINY)).sum())
        best, best_ll = self.taus[0], -math.inf
        for tau, value in zip(self.taus, marginal.tolist(), strict=True):
            if value > best_ll + 1e-9:
                best, best_ll = tau, value
        self.tau = best
        small = sizes[code] < self.min_group
        for a in range(0, n, self.block):
            b = min(a + self.block, n)
            ll = rows(a, b)
            lik = np.exp(ll - ll.max(axis=1, keepdims=True))
            post = _normal_on_grid(near_mean[a:b], np.sqrt(near_sd[a:b] ** 2 + best * best)) * lik
            post /= np.maximum(post.sum(axis=1, keepdims=True), _TINY)
            p = post[:, _BELOW].sum(axis=1)
            for i in range(a, b):
                # A group too small to read is no evidence about its members: `ContactHarm` has them.
                if not small[i]:
                    self._p[ids[i]] = float(p[i - a])


# --- the book the agent and the bidders share ---------------------------------------------------------


class WhoBook:
    """Everything decision 1 needs at decision time (see the module docstring).

    The agent calls `ingest` with the events it is handed, `refresh` once a day, and `score` for each
    customer it decides on; the bidders call `urgency_factor`, `cost_ceiling` and `leave_alone`, which
    read only the state's scores and what the last `refresh` fitted.
    """

    def __init__(self, config: WhoConfig | None = None) -> None:
        self.config = config or WhoConfig()
        self.models = SlowModels(self.config.slow)
        self.traits = TraitHarm(min_group=self.config.trait_min_group)
        self.trait_keys = TraitKeys(offer_cut=self.config.offer_cut, load_cut=self.config.load_cut)
        self.events: dict[str, list[Event]] = {}
        self._seen: set[str] = set()
        self._first: datetime | None = None
        self._refreshed_on: datetime | None = None
        self._closed_cuts = -1
        self._harm_fit: object = None
        self._traits_at: datetime | None = None
        self._keys: dict[str, str] = {}
        self._scored: dict[str, tuple[str, dict[str, float]]] = {}

    # -- events --

    def ingest(self, events_by_customer: Mapping[str, Sequence[Event]], now: datetime) -> None:
        """Keep every event handed over at or before `now`, once by id (the whole log may be repeated)."""
        for cid, events in events_by_customer.items():
            for e in events:
                if e.ts > now or e.event_id in self._seen:
                    continue
                self._seen.add(e.event_id)
                self.events.setdefault(cid, []).append(e)
                if self._first is None or e.ts < self._first:
                    self._first = e.ts

    # -- fitting --

    def refresh(self, now: datetime, harm: ContactHarm | None = None) -> None:
        """Once a calendar day: refit the slow models if a month-end's window has closed since the last
        fit, and the trait groups if the harm model has been refit and `trait_refit_days` have passed."""
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._refreshed_on == day:
            return
        self._refreshed_on = day
        cfg = self.config
        if cfg.models and self._first is not None:
            short = min(cfg.slow.risk_window_days, cfg.slow.value_window_days)
            closed = sum(1 for c in month_ends(self._first, now) if c + short * _DAY <= now)
            if closed != self._closed_cuts:
                self._closed_cuts = closed
                self.models.fit((e for evs in self.events.values() for e in evs), now)
                self._scored.clear()
        if cfg.trait_harm and harm is not None and harm.fit is not None:
            due = self._traits_at is None or now - self._traits_at >= timedelta(days=cfg.trait_refit_days)
            if harm.fit is not self._harm_fit and due:
                self._harm_fit = harm.fit
                self._traits_at = now
                self._fit_traits(harm, now)

    def _fit_traits(self, harm: ContactHarm, now: datetime) -> None:
        records = harm.own_records()
        profiles: dict[str, tuple[dict[str, float], int]] = {}
        for cid in records:
            evs = self.events.get(cid)
            if evs:
                profiles[cid] = (profile_for(evs, now), self._load(evs, now))
        engaged = [f["ch_click_rate"] for f, _ in profiles.values() if f.get("ch_engaged", 0.0) >= 0.5]
        cut = float(np.median(engaged)) if engaged else 0.0
        self.trait_keys = TraitKeys(cut, self.config.offer_cut, self.config.load_cut)
        keys = {cid: self.trait_keys.key(f, load) for cid, (f, load) in profiles.items()}
        self.traits.fit(records, keys)

    def _load(self, events: Sequence[Event], now: datetime) -> int:
        since = now - self.config.load_days * _DAY
        return sum(1 for e in events if e.type is EventType.SEND and since < e.ts <= now)

    # -- one customer --

    def features(self, state: CustomerState, now: datetime) -> Mapping[str, float] | None:
        """This customer's profile: the nightly one when it is fresh, else built from the events held."""
        age = timedelta(days=self.config.max_profile_age_days)
        if state.features and state.features_as_of is not None and now - state.features_as_of <= age:
            return state.features
        evs = self.events.get(state.customer_id)
        if evs:
            return profile_for(evs, now)
        return state.features or None

    def score(self, state: CustomerState, now: datetime) -> dict[str, float]:
        """The state's scores with `churn_risk` and `clv_90d` added where a model passed its check, and
        remembers the customer's trait group for `leave_alone`. The state is not changed."""
        day = now.date().isoformat()
        cached = self._scored.get(state.customer_id)
        if cached is not None and cached[0] == day:
            return {**state.scores, **cached[1]}
        features = self.features(state, now)
        new: dict[str, float] = {}
        if features is not None:
            if self.config.models:
                new = self.models.scores(features)
            if self.config.trait_harm and self.traits.fitted:
                evs = self.events.get(state.customer_id, ())
                self._keys[state.customer_id] = self.trait_keys.key(features, self._load(evs, now))
        self._scored[state.customer_id] = (day, new)
        return {**state.scores, **new}

    # -- what the bidders read --

    def urgency_factor(self, state: CustomerState) -> float:
        """`1 + risk_urgency x churn_risk`; 1 when there is no risk score."""
        risk = state.scores.get(CHURN_RISK)
        if self.config.risk_urgency <= 0 or risk is None or not math.isfinite(risk):
            return 1.0
        return 1.0 + self.config.risk_urgency * min(max(risk, 0.0), 1.0)

    def cost_ceiling(self, state: CustomerState) -> float | None:
        """The most one action for this customer may cost, a share of their 90-day value; None when
        there is no ceiling or no value score."""
        share = self.config.value_cost_share
        clv = state.scores.get(CLV_90D)
        if share is None or clv is None or not math.isfinite(clv) or clv < 0:
            return None
        return share * clv

    def leave_alone(self, customer_id: str, at_stake: float) -> bool:
        """Whether the people like this customer credibly buy less after a message."""
        if not self.config.trait_harm:
            return False
        return self.traits.leave_alone(customer_id, at_stake, self._keys.get(customer_id))

    def describe(self) -> str:
        parts = [self.models.describe()]
        if self.config.trait_harm:
            parts.append(self.traits.describe())
        return " ".join(parts)
