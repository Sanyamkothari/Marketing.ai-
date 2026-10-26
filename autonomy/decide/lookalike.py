"""Look-alike learning: trust the crowd until you know the person (plan-v3 foundation B).

`LookalikeResponseModel` implements `ResponseModel` (`predict`, `treated_rate`, `update`), so it
drops in wherever `BetaResponseModel` does. It answers the same question, "how much does this action
change the chance this customer buys, and how sure are we?", from the customer's profile instead of
a segment label, in three layers:

1. **The crowd.** One logistic model of "buys within the window" learns from every closed outcome
   the agent has seen, given the customer's profile and, for a contacted customer, the action:

       logit p(control)  = baseline(profile)
       logit p(treated)  = baseline(profile) + effect(profile, channel, offer, theme, format)

   Both probabilities are read off separately and uplift is their difference. The baseline learns
   from every row; the effect only from contacted ones, and only the people we never contact (the
   holdout) tell the two apart, so no estimate of uplift exists without a control. The effect's
   profile slopes are penalised hard: heterogeneity is expected to be small and a few hundred
   outcomes cannot tell a 1-point uplift from a 2-point one. The effect has a shared "contacted"
   term plus one per channel, offer, theme and format level; a level never seen is neutral with the
   prior's spread, so new content is neither penalised nor taken as certain. The model keeps a
   posterior (a Laplace approximation, drawn as antithetic samples) rather than a point: its spread
   is what Thompson sampling explores, and it shrinks as rows arrive. Contacted rows are weighted by
   the inverse of their logged propensity (clipped, self-normalised), and no customer's rows weigh
   more than `max_customer_weight` in the fit, so one regular's many windows are not taken for many
   independent customers.
2. **The prior.** Where the crowd has too few rows, or none with a control, the answer is the prior
   model's. Where it has some, the two are combined by precision (inverse variance), so a posterior
   that is still wide counts for little. The prior is whatever model the caller already trusts
   (`BetaResponseModel` by default; the agent's `ClickResponseModel`).
3. **The person.** Each customer carries two small shifts on the logit scale, one on their
   baseline and one on their response to contact. Both start at zero, so a new customer gets the
   crowd's answer exactly. Each is one Newton step from zero on the customer's own residuals
   (outcome minus what the crowd predicted for that row), against a prior whose width is estimated
   from how much customers really differ: a customer with two rows barely moves, one with thirty
   mostly follows their own record. Their posterior variance is part of the spread.

**Evidence.** The model learns from `Outcome`s: a profile taken when the decision was made, the
action (None for a customer we did not contact), whether they bought in the window after, and the
window's bounds. A profile built after its window opened would let the outcome leak into its own
features, so such an outcome is refused, and `fit(now)` uses only windows that had closed by `now`.
`OutcomeBook` turns the agent's decisions and events into outcomes. Nothing here reads the Gym's
truth or any attribute a real deployment would not have.

**Profile.** `profile_of(state)` takes a base set derived from `CustomerState` fields (recency,
purchases, revenue, order value, contacts, per-channel record, offer sensitivity, segment) and,
when `state.features` is non-empty (the nightly profile, foundation A), adds those too.

Cost: a refit is a few Newton steps on a matrix of at most `max_rows` x ~200 columns, run when the
data has grown by a fifth; a prediction is a handful of small matrix-vector products.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Channel, CustomerState, Decision, DecisionKind, Group
from autonomy.decide.models import BetaResponseModel, ResponseModel, ResponseObservation, action_features

Vec = npt.NDArray[np.float64]

# --- the profile ---------------------------------------------------------------------------------

_CHANNELS: tuple[Channel, ...] = (Channel.EMAIL, Channel.WHATSAPP, Channel.SMS)
_SEGMENTS: tuple[str, ...] = ("new", "lapsed", "light", "medium", "heavy")
_RECENCY_CAP_DAYS = 730.0
_ENGAGED_CAP_DAYS = 365.0
OFFER_SENSITIVITY_DEFAULT = 0.3
"""What `offer_sensitivity` reads when the folder has not set it (the folder's own prior)."""

FEATURES: tuple[tuple[str, str], ...] = (
    ("recency", "log of days since the last purchase (two years if none)"),
    ("never_bought", "1 if the customer has not bought yet"),
    ("tenure", "log of days since first seen"),
    ("purchases", "log of orders so far"),
    ("revenue", "log of money spent so far"),
    ("order_value", "log of average order value"),
    ("frequency", "orders per 30 days since first seen"),
    ("contacts_7d", "messages in the last 7 days"),
    ("contacts_30d", "messages in the last 30 days"),
    ("offer_sensitivity", "share of orders made with an offer"),
    ("offer_known", "1 if offer sensitivity comes from their own orders"),
    *((f"segment_{s}", f"1 if the customer is {s}") for s in _SEGMENTS),
    *(
        (f"{ch.value}_{what}", f"{ch.value}: {text}")
        for ch in _CHANNELS
        for what, text in (
            ("delivered", "log of messages delivered"),
            ("click_rate", "clicks per delivered message"),
            ("idle", "log of days since last engaged (a year if never)"),
        )
    ),
)
"""The base profile: (name, plain-language description), in column order."""
BASE_NAMES: tuple[str, ...] = tuple(name for name, _ in FEATURES)


@dataclass(frozen=True, eq=False)
class Profile:
    """A customer as the model sees them at one moment: the base columns and any nightly features."""

    as_of: datetime
    base: Vec
    extra: Mapping[str, float]


def _days(later: datetime, earlier: datetime | None, default: float) -> float:
    if earlier is None:
        return default
    return max((later - earlier).total_seconds() / 86_400.0, 0.0)


def profile_of(state: CustomerState) -> Profile:
    """The profile of a customer's state: everything is as of `state.as_of`, never the clock.

    With nightly features, the profile is stamped with the later of `state.as_of` and
    `state.features_as_of`: features built after a window opened could carry its outcome, so such a
    profile is refused as evidence for that window. With no first-seen date, tenure and frequency are
    unknown (NaN, read as the training average) rather than a brand-new customer's."""
    now = state.as_of
    recency = _days(now, state.last_purchase_at, _RECENCY_CAP_DAYS)
    if state.first_seen_at is None:
        tenure_col = freq = math.nan
        if state.purchases == 0:
            freq = 0.0
    else:
        tenure = _days(now, state.first_seen_at, 0.0)
        tenure_col = math.log1p(tenure)
        freq = state.purchases * 30.0 / max(tenure, 30.0)
    sens = state.offer_sensitivity
    known = sens is not None and math.isfinite(sens)
    row: list[float] = [
        math.log1p(min(recency, _RECENCY_CAP_DAYS)),
        0.0 if state.purchases > 0 and state.last_purchase_at is not None else 1.0,
        tenure_col,
        math.log1p(state.purchases),
        math.log1p(max(state.revenue, 0.0)),
        math.log1p(max(state.avg_order_value, 0.0)),
        freq,
        float(state.contacts_7d),
        float(state.contacts_30d),
        float(sens) if known and sens is not None else OFFER_SENSITIVITY_DEFAULT,
        1.0 if known else 0.0,
        *(1.0 if state.segment == s else 0.0 for s in _SEGMENTS),
    ]
    for ch in _CHANNELS:
        stats = state.channels.get(ch)
        if stats is None:
            row += [0.0, 0.0, math.log1p(_ENGAGED_CAP_DAYS)]
            continue
        idle = min(_days(now, stats.last_engaged_at, _ENGAGED_CAP_DAYS), _ENGAGED_CAP_DAYS)
        row += [math.log1p(stats.delivers), stats.clicks / (stats.delivers + 5.0), math.log1p(idle)]
    extra = {k: float(v) for k, v in state.features.items() if math.isfinite(v)} if state.features else {}
    stamped = now
    if extra and state.features_as_of is not None and state.features_as_of > now:
        stamped = state.features_as_of
    return Profile(as_of=stamped, base=np.asarray(row, dtype=np.float64), extra=extra)


def history_profile(
    customer_id: str,
    at: datetime,
    *,
    first_seen: datetime | None,
    purchases: Iterable[tuple[datetime, float]],
    sends: Iterable[datetime] = (),
    segment: Callable[[CustomerState], str | None] | None = None,
) -> Profile:
    """The profile of a customer as of `at`, rebuilt from their history alone: what they had bought
    and been sent before `at`, nothing after. For past campaigns, whose customers' states exist only
    as of today (and today's would hold the very purchases being predicted).

    A rebuilt profile has no per-channel record or nightly features, only what purchases and sends
    show; those columns read as the training average. `segment` labels the rebuilt state."""
    bought = sorted((t, v) for t, v in purchases if t < at)
    paid = [(t, v) for t, v in bought if v > 0]
    week, month = at - timedelta(days=7), at - timedelta(days=30)
    sent = [t for t in sends if month < t < at]  # strictly before: a send at `at` is the treatment
    state = CustomerState(
        customer_id=customer_id,
        as_of=at,
        first_seen_at=first_seen if first_seen is not None and first_seen <= at else None,
        last_purchase_at=paid[-1][0] if paid else None,
        purchases=len(paid),
        revenue=max(sum(v for _t, v in bought), 0.0),
        contacts_7d=sum(1 for t in sent if t > week),
        contacts_30d=len(sent),
    )
    label = segment(state) if segment is not None else None
    return profile_of(state.model_copy(update={"segment": label}) if label is not None else state)


def stamp(seconds: float) -> datetime:
    """A time from seconds since the epoch (the form `HistoryDigest` keeps)."""
    return datetime.fromtimestamp(seconds, UTC)


# --- evidence -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """One closed window: what the customer looked like, what we did, and whether they bought.

    `action_key` is None for a customer we did not contact (the holdout). `profile` must have been
    taken no later than `window_start`; `window_end` is when the answer was known. `propensity` is the
    logged chance the policy had of taking this action (`Decision.probability`), None when the action
    was randomised or not logged: the crowd's fit weights contacted rows by its inverse."""

    customer_id: str
    profile: Profile
    action_key: str | None
    converted: bool
    window_start: datetime
    window_end: datetime
    weight: float = 1.0
    propensity: float | None = None


@dataclass
class _Row:
    customer_id: str
    profile: Profile
    key: str | None
    y: float
    w: float
    end: datetime
    propensity: float | None = None
    absorbed: bool = False


@dataclass(frozen=True)
class LookalikeConfig:
    """Tunable numbers, each a documented default and none tuned to a seed."""

    min_treated: float = 150.0
    """Contacted outcomes (summed weight) needed before the crowd's fit is used at all."""
    min_control: float = 60.0
    """Not-contacted outcomes needed too: without a control there is no uplift to read."""
    draws: int = 64
    """Posterior samples (in antithetic pairs) the spread is read from."""
    base_penalty: float = 1.0
    """Ridge on the baseline's profile slopes: one standard deviation of a feature moves the logit
    by about one unit, give or take (prior sd 1)."""
    level_penalty: float = 1.0
    """Ridge on the effect of each channel, offer, theme and format level (prior sd 1 on the logit)."""
    slope_penalty: float = 25.0
    """Ridge on how the effect varies with the profile (prior sd 0.2 per standard deviation)."""
    channel_slope_penalty: float = 50.0
    """Ridge on how the effect varies with the profile within one channel (prior sd 0.14)."""
    min_channel_rows: int = 100
    """Contacted rows a channel needs before it gets slopes of its own."""
    refit_growth: float = 0.2
    refit_min_new: int = 100
    """Refit when the data has grown by this share, and by at least this many rows."""
    max_rows: int = 40_000
    """Rows kept, newest first: bounds the memory and the cost of a refit."""
    person_var: float = 0.3
    person_effect_var: float = 0.1
    """Variance (logit scale) of customers' baseline and response shifts before the data says."""
    person_min_customers: int = 30
    """Customers with evidence needed before those variances are estimated from the data."""
    ipw_clip: float = 20.0
    """Largest inverse-propensity weight of a contacted row before the weights are self-normalised."""
    max_customer_weight: float = 4.0
    """Most weight one customer's rows carry in the crowd's fit (in windows). One customer's windows
    are not independent draws (the same person buys, or not, again and again), so a customer decided
    on every day cannot count for more than this many strangers. Their own record (the person's
    shifts) still uses every row."""
    seed: int = 0


def _sigmoid(x: Vec) -> Vec:
    return np.asarray(1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0))), dtype=np.float64)


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(x, 30.0), -30.0)))


def precision_blend(m0: float, s0: float, m1: float, s1: float) -> tuple[float, float]:
    """Combine two independent normal estimates by precision: (mean, sd). A zero sd is certain."""
    if s0 <= 1e-9:
        return m0, 0.0
    if s1 <= 1e-9:
        return m1, 0.0
    w0, w1 = 1.0 / (s0 * s0), 1.0 / (s1 * s1)
    return (m0 * w0 + m1 * w1) / (w0 + w1), math.sqrt(1.0 / (w0 + w1))


@dataclass(frozen=True)
class Estimate:
    """What the crowd and the person say about one action, before the prior is blended in."""

    mean: float
    sd: float
    option_sd: float
    treated_rate: float
    control_rate: float


@dataclass(frozen=True)
class CalibrationRow:
    low: float
    high: float
    n: float
    predicted: float
    realised: float


@dataclass(frozen=True)
class CalibrationReport:
    """Predicted against realised purchase rate on outcomes the model had not yet seen when it
    predicted them (rows arrive in time order; each is scored before it is learned from)."""

    treated: tuple[CalibrationRow, ...]
    control: tuple[CalibrationRow, ...]

    @staticmethod
    def _total(rows: tuple[CalibrationRow, ...]) -> tuple[float, float, float]:
        n = sum(r.n for r in rows)
        if n <= 0:
            return 0.0, 0.0, 0.0
        return n, sum(r.n * r.predicted for r in rows) / n, sum(r.n * r.realised for r in rows) / n

    def summary(self) -> dict[str, float]:
        tn, tp, tr = self._total(self.treated)
        cn, cp, cr = self._total(self.control)
        return {
            "treated_n": tn,
            "treated_predicted": tp,
            "treated_realised": tr,
            "control_n": cn,
            "control_predicted": cp,
            "control_realised": cr,
        }


@dataclass
class _Fit:
    """A fitted crowd model and everything needed to read it."""

    raw_names: tuple[str, ...]
    mean: Vec
    std: Vec
    keep: npt.NDArray[np.intp]
    levels: dict[tuple[int, str], int]
    channels: dict[str, int]
    d: int
    beta: Vec
    draws: Vec
    unseen: Vec
    """Prior draws (sd 1/sqrt(level_penalty)) for a level the fit never saw, one column per slot."""
    sigma_base: float
    sigma_effect: float
    n_treated: float
    n_control: float
    rows: int

    @property
    def n_levels(self) -> int:
        return len(self.levels)

    @property
    def contact(self) -> int:
        """Column of the shared effect of contact (any channel, offer, theme and format)."""
        return 1 + self.d

    @property
    def lv(self) -> int:
        """First column of the per-level effects."""
        return 2 + self.d

    @property
    def tau(self) -> int:
        """First column of the effect's profile slopes."""
        return self.lv + self.n_levels

    @property
    def chan(self) -> int:
        """First column of the per-channel profile slopes."""
        return self.tau + self.d

    @property
    def width(self) -> int:
        return self.chan + len(self.channels) * self.d


@dataclass
class _Context:
    """Everything about one customer's state that does not depend on the action, cached."""

    state: CustomerState
    z: Vec
    eta: Vec
    common: Vec
    by_channel: Vec


class LookalikeResponseModel:
    """Uplift from the customer's profile and the crowd, with a personal correction.

    `prior` is the model to fall back on (default `BetaResponseModel`); `update(observations)` goes
    to it, so the segment-level learning of the existing pipeline keeps working. The profile-level
    learning comes through `observe(outcomes)` and `fit(now)`; see the module docstring.
    """

    def __init__(self, prior: ResponseModel | None = None, *, config: LookalikeConfig | None = None) -> None:
        self.prior: ResponseModel = prior if prior is not None else BetaResponseModel()
        self.config = config if config is not None else LookalikeConfig()
        self._rows: list[_Row] = []
        self._fit_state: _Fit | None = None
        self._rows_at_fit = 0
        self._stats: dict[str, list[float]] = {}
        """customer -> [sum of residuals, sum of variances] over all rows, then over contacted rows."""
        self._stats_t: dict[str, list[float]] = {}
        self._person_cache: dict[str, tuple[float, float, float]] = {}
        self._context: _Context | None = None
        self._preq: list[tuple[float, float, float, bool]] = []
        self.refused = 0
        """Outcomes refused because their profile was taken after their window opened."""

    # -- the ResponseModel protocol --

    def predict(self, state: CustomerState, action_key: str) -> tuple[float, float]:
        """(mean uplift, sd): the prior's where the crowd has too little, else the two combined."""
        m0, s0 = self.prior.predict(state, action_key)
        est = self.estimate(state, action_key)
        if est is None:
            return m0, s0
        return precision_blend(m0, s0, est.mean, est.sd)

    def treated_rate(self, state: CustomerState, action_key: str) -> float:
        """The chance they buy if we take this action: the prior's, moved by the change in uplift."""
        base = self.prior.treated_rate(state, action_key)
        est = self.estimate(state, action_key)
        if est is None:
            return base
        m0 = self.prior.predict(state, action_key)[0]
        mean, _ = self.predict(state, action_key)
        return min(1.0, max(0.0, base + mean - m0))

    def update(self, observations: Iterable[ResponseObservation]) -> None:
        """Segment-level observations go to the prior; profile-level ones come through `observe`."""
        self.prior.update(observations)

    # -- optional hooks the bidders look for --

    def option_std(self, state: CustomerState, action_key: str) -> float:
        """The part of the spread that tells this customer's options apart: the action-specific
        uncertainty of the crowd's effect, blended with the prior's when it has one. What every
        option of one customer shares (the baseline, the person) is left out: drawing it per option
        would shuffle the options without teaching anything."""
        own = getattr(self.prior, "option_std", None)
        s0 = float(own(state, action_key)) if callable(own) else self.prior.predict(state, action_key)[1]
        est = self.estimate(state, action_key)
        if est is None:
            return s0
        return precision_blend(0.0, s0, 0.0, est.option_sd)[1]

    # -- evidence --

    def profile(self, state: CustomerState) -> Profile:
        return profile_of(state)

    @property
    def ready(self) -> bool:
        """Whether the crowd's fit is in use."""
        return self._fit_state is not None

    @property
    def rows(self) -> int:
        return len(self._rows)

    def observe(self, outcomes: Iterable[Outcome]) -> int:
        """Remember closed outcomes; returns how many were taken. They shape the fit at `fit`."""
        taken = 0
        for o in outcomes:
            if o.weight <= 0 or o.profile.as_of > o.window_start:
                if o.weight > 0:
                    self.refused += 1
                continue
            self._rows.append(
                _Row(
                    o.customer_id,
                    o.profile,
                    o.action_key,
                    1.0 if o.converted else 0.0,
                    o.weight,
                    o.window_end,
                    o.propensity,
                )
            )
            taken += 1
        return taken

    def fit(self, now: datetime, *, force: bool = False) -> bool:
        """Fold new outcomes into each customer's record, and refit the crowd if the data has grown.

        Only windows that closed by `now` count. Returns whether the crowd's model was refitted."""
        usable = [r for r in self._rows if r.end <= now]
        fresh = len(usable) - self._rows_at_fit
        grown = self._fit_state is None or fresh >= max(
            self.config.refit_min_new, self.config.refit_growth * self._rows_at_fit
        )
        if not (force or grown):
            self._absorb([r for r in usable if not r.absorbed])
            return False
        if len(usable) > self.config.max_rows:
            drop = len(usable) - self.config.max_rows
            keep = set(map(id, usable[drop:]))
            self._rows = [r for r in self._rows if id(r) in keep or r.end > now]
            usable = usable[drop:]
        crowd = self._crowd_weights(usable)
        contacted = np.asarray([r.key is not None for r in usable], dtype=bool)
        treated = float(crowd[contacted].sum())
        control = float(crowd[~contacted].sum())
        if treated < self.config.min_treated or control < self.config.min_control:
            self._rows_at_fit = len(usable)
            return False
        self._refit(usable, crowd)
        return True

    def _crowd_weights(self, usable: Sequence[_Row]) -> Vec:
        """Each row's weight in the crowd's fit: its own weight, times the clipped inverse of its
        logged propensity for a contacted row (self-normalised, so the contacted rows that logged one
        keep their total), then scaled so no customer's rows weigh more than `max_customer_weight`."""
        cfg = self.config
        w = np.asarray([r.w for r in usable], dtype=np.float64)
        logged = np.asarray([r.key is not None and r.propensity is not None for r in usable], dtype=bool)
        if logged.any():
            props = np.asarray([r.propensity or 1.0 for r in usable], dtype=np.float64)
            ipw = np.minimum(1.0 / np.clip(props, 1e-12, 1.0), cfg.ipw_clip)
            scaled = w[logged] * ipw[logged]
            total = float(scaled.sum())
            if total > 0:
                w[logged] = scaled * float(w[logged].sum()) / total
        ids = {cid: i for i, cid in enumerate(dict.fromkeys(r.customer_id for r in usable))}
        code = np.asarray([ids[r.customer_id] for r in usable], dtype=np.intp)
        per = np.bincount(code, weights=w, minlength=len(ids))
        scale = np.minimum(1.0, cfg.max_customer_weight / np.maximum(per, 1e-12))
        return np.asarray(w * scale[code], dtype=np.float64)

    # -- reading --

    def estimate(self, state: CustomerState, action_key: str) -> Estimate | None:
        """The crowd's estimate for this customer and action with their own record applied, or None
        while the crowd has too little to say."""
        fit = self._fit_state
        if fit is None:
            return None
        ctx = self._context_of(fit, state)
        u, v, var_v = self._person(state.customer_id)
        channel, offer, theme, fmt = action_features(action_key)
        idx = [fit.levels.get((s, val)) for s, val in enumerate((channel, offer, theme, fmt))]
        lv = fit.draws[:, fit.lv : fit.lv + fit.n_levels]
        # A level the fit never saw is neutral on average with the prior's spread, not a sure zero.
        action = np.zeros(fit.draws.shape[0])
        for s, i in enumerate(idx):
            action += lv[:, i] if i is not None else fit.unseen[:, s]
        ci = fit.channels.get(channel)
        if ci is not None:
            action = action + ctx.by_channel[:, ci]
        eta_c = ctx.eta + u
        p_c = _sigmoid(eta_c)
        p_t = _sigmoid(eta_c + fit.draws[:, fit.contact] + ctx.common + action + v)
        gap = p_t - p_c
        mean = float(gap.mean())
        pt = float(p_t.mean())
        sd = math.sqrt(float(gap.var()) + (pt * (1.0 - pt)) ** 2 * var_v)
        option_sd = pt * (1.0 - pt) * float(action.std())
        return Estimate(mean, sd, option_sd, pt, float(p_c.mean()))

    def baseline_rate(self, state: CustomerState) -> float | None:
        """The crowd's chance this customer buys in a window with no contact (their own record in)."""
        fit = self._fit_state
        if fit is None:
            return None
        ctx = self._context_of(fit, state)
        u, _v, _ = self._person(state.customer_id)
        return float(_sigmoid(ctx.eta + u).mean())

    def person(self, customer_id: str) -> tuple[float, float]:
        """(baseline shift, response shift) of one customer on the logit scale; (0, 0) when new."""
        u, v, _ = self._person(customer_id)
        return u, v

    def calibration(self, bins: int = 5) -> CalibrationReport:
        """Predicted against realised purchase rate, on outcomes scored before they were learned."""

        def table(treated: bool) -> tuple[CalibrationRow, ...]:
            rows = [(p, y, w) for p, y, w, t in self._preq if t is treated]
            if not rows:
                return ()
            rows.sort()
            total = sum(w for _p, _y, w in rows)
            out: list[CalibrationRow] = []
            acc: list[tuple[float, float, float]] = []
            seen = 0.0
            for r in rows:
                acc.append(r)
                seen += r[2]
                if seen >= total * (len(out) + 1) / bins or r is rows[-1]:
                    n = sum(w for _p, _y, w in acc)
                    out.append(
                        CalibrationRow(
                            low=acc[0][0],
                            high=acc[-1][0],
                            n=n,
                            predicted=sum(p * w for p, _y, w in acc) / n,
                            realised=sum(y * w for _p, y, w in acc) / n,
                        )
                    )
                    acc = []
            return tuple(out)

        return CalibrationReport(treated=table(True), control=table(False))

    # -- inside --

    def _z(self, fit: _Fit, profiles: Sequence[Profile]) -> Vec:
        raw = np.stack([p.base for p in profiles])
        if fit.raw_names:
            extra = np.full((len(profiles), len(fit.raw_names)), np.nan)
            for j, name in enumerate(fit.raw_names):
                for i, p in enumerate(profiles):
                    got = p.extra.get(name)
                    if got is not None:
                        extra[i, j] = got
            raw = np.hstack([raw, extra])
        raw = np.where(np.isnan(raw), fit.mean, raw)
        z = (raw[:, fit.keep] - fit.mean[fit.keep]) / fit.std[fit.keep]
        return np.asarray(np.clip(z, -4.0, 4.0), dtype=np.float64)

    def _design(self, fit: _Fit, rows: Sequence[_Row]) -> Vec:
        z = self._z(fit, [r.profile for r in rows])
        n, d = z.shape[0], fit.d
        x = np.zeros((n, fit.width))
        x[:, 0] = 1.0
        x[:, 1 : 1 + d] = z
        lv, tau, chan = fit.lv, fit.tau, fit.chan
        by_channel: dict[int, list[int]] = {}
        for i, r in enumerate(rows):
            if r.key is None:
                continue
            x[i, fit.contact] = 1.0
            for s, val in enumerate(action_features(r.key)):
                j = fit.levels.get((s, val))
                if j is not None:
                    x[i, lv + j] = 1.0
            x[i, tau : tau + d] = z[i]
            ci = fit.channels.get(action_features(r.key)[0])
            if ci is not None:
                by_channel.setdefault(ci, []).append(i)
        for ci, members in by_channel.items():
            x[members, chan + ci * d : chan + (ci + 1) * d] = z[members]
        return x

    def _refit(self, usable: Sequence[_Row], crowd: Vec) -> None:
        cfg = self.config
        counts: dict[str, int] = {}
        for r in usable:
            for name in r.profile.extra:
                counts[name] = counts.get(name, 0) + 1
        raw_names = tuple(sorted(n for n, c in counts.items() if c >= 0.05 * len(usable)))
        base = np.stack([r.profile.base for r in usable])
        if raw_names:
            extra = np.full((len(usable), len(raw_names)), np.nan)
            for j, name in enumerate(raw_names):
                for i, r in enumerate(usable):
                    got = r.profile.extra.get(name)
                    if got is not None:
                        extra[i, j] = got
            raw = np.hstack([base, extra])
        else:
            raw = base
        known = ~np.isnan(raw)
        count = np.maximum(known.sum(axis=0), 1)
        filled = np.where(known, raw, 0.0)
        mean = np.asarray(filled.sum(axis=0) / count, dtype=np.float64)
        dev = np.where(known, raw - mean, 0.0)
        std = np.asarray(np.sqrt((dev * dev).sum(axis=0) / count), dtype=np.float64)
        keep = np.flatnonzero(std > 1e-6)
        std = np.where(std > 1e-6, std, 1.0)
        levels: dict[tuple[int, str], int] = {}
        channel_rows: dict[str, int] = {}
        for r in usable:
            if r.key is None:
                continue
            feats = action_features(r.key)
            for s, val in enumerate(feats):
                levels.setdefault((s, val), len(levels))
            channel_rows[feats[0]] = channel_rows.get(feats[0], 0) + 1
        channels = {
            ch: i
            for i, ch in enumerate(sorted(c for c, n in channel_rows.items() if n >= cfg.min_channel_rows))
        }
        d = int(keep.size)
        fit = _Fit(
            raw_names=raw_names,
            mean=mean,
            std=std,
            keep=keep,
            levels=levels,
            channels=channels,
            d=d,
            beta=np.zeros(0),
            draws=np.zeros((0, 0)),
            unseen=np.zeros((0, 0)),
            sigma_base=cfg.person_var,
            sigma_effect=cfg.person_effect_var,
            n_treated=float(sum(c for r, c in zip(usable, crowd, strict=True) if r.key is not None)),
            n_control=float(sum(c for r, c in zip(usable, crowd, strict=True) if r.key is None)),
            rows=len(usable),
        )
        x = self._design(fit, usable)
        y = np.asarray([r.y for r in usable])
        w = crowd
        lam = np.empty(fit.width)
        lam[0] = 1e-3
        lam[1 : 1 + d] = cfg.base_penalty
        lam[fit.contact] = cfg.level_penalty
        lam[fit.lv : fit.tau] = cfg.level_penalty
        lam[fit.tau : fit.chan] = cfg.slope_penalty
        lam[fit.chan :] = cfg.channel_slope_penalty
        beta = np.zeros(fit.width)
        rate = float(np.clip((w * y).sum() / w.sum(), 1e-4, 1 - 1e-4))
        beta[0] = math.log(rate / (1.0 - rate))
        hess = np.eye(fit.width)
        for _ in range(30):
            p = _sigmoid(x @ beta)
            grad = x.T @ (w * (y - p)) - lam * beta
            weight = w * p * (1.0 - p)
            hess = (x * weight[:, None]).T @ x + np.diag(lam)
            step = np.linalg.solve(hess, grad)
            biggest = float(np.abs(step).max())
            if biggest > 4.0:
                step *= 4.0 / biggest
            beta = beta + step
            if biggest < 1e-6:
                break
        p = _sigmoid(x @ beta)
        hess = (x * (w * p * (1.0 - p))[:, None]).T @ x + np.diag(lam)
        chol = np.linalg.cholesky(hess)
        rng = np.random.default_rng(cfg.seed)
        half = rng.standard_normal((max(cfg.draws // 2, 1), fit.width))
        eps = np.vstack([half, -half])
        fit.beta = beta
        fit.draws = beta + np.linalg.solve(chol.T, eps.T).T
        unseen = rng.standard_normal((half.shape[0], 4))
        fit.unseen = np.vstack([unseen, -unseen]) / math.sqrt(cfg.level_penalty)
        self._fit_state = fit
        self._rows_at_fit = len(usable)
        self._context = None
        # The person's record uses each row's own weight: the cap on a customer's total is about
        # what the crowd learns from them, not about what they show of themselves.
        own = np.asarray([r.w for r in usable], dtype=np.float64)
        self._rebuild_people(fit, usable, y, own, p)

    def _rebuild_people(self, fit: _Fit, usable: Sequence[_Row], y: Vec, w: Vec, p: Vec) -> None:
        cfg = self.config
        # p is the crowd's probability for each row's own arm; the person's shifts are measured
        # against it, never against a prediction that already contains them.
        resid = w * (y - p)
        var = w * p * (1.0 - p)
        ids = {cid: i for i, cid in enumerate(dict.fromkeys(r.customer_id for r in usable))}
        code = np.asarray([ids[r.customer_id] for r in usable])
        treated = np.asarray([r.key is not None for r in usable])
        k = len(ids)
        s_all = np.bincount(code, weights=resid, minlength=k)
        v_all = np.bincount(code, weights=var, minlength=k)
        s_t = np.bincount(code, weights=resid * treated, minlength=k)
        v_t = np.bincount(code, weights=var * treated, minlength=k)
        usable_c = v_all > 0
        sigma_base, sigma_effect = cfg.person_var, cfg.person_effect_var
        if int(usable_c.sum()) >= cfg.person_min_customers:
            est = float(((s_all[usable_c] ** 2 - v_all[usable_c]).sum()) / (v_all[usable_c] ** 2).sum())
            sigma_base = min(max(est, 0.02), 2.0)
            has_t = v_t > 0
            if int(has_t.sum()) >= cfg.person_min_customers:
                est_t = float(((s_t[has_t] ** 2 - v_t[has_t]).sum()) / (v_t[has_t] ** 2).sum())
                sigma_effect = min(max(est_t - sigma_base, 0.01), 1.0)
        fit.sigma_base, fit.sigma_effect = sigma_base, sigma_effect
        self._stats = {cid: [float(s_all[i]), float(v_all[i])] for cid, i in ids.items()}
        self._stats_t = {cid: [float(s_t[i]), float(v_t[i])] for cid, i in ids.items()}
        self._person_cache = {}
        for r in usable:
            r.absorbed = True

    def _absorb(self, rows: Sequence[_Row]) -> None:
        """Add rows that arrived since the last refit to their customers' records, scoring each
        against the crowd as it stood (the calibration table) before it is learned from."""
        fit = self._fit_state
        if fit is None or not rows:
            return
        x = self._design(fit, rows)
        p = _sigmoid(x @ fit.beta)
        for r, pi in zip(rows, p, strict=True):
            u, v, _ = self._person(r.customer_id)
            treated = r.key is not None
            shifted = _sig(math.log(pi / (1.0 - pi)) + u + (v if treated else 0.0))
            self._preq.append((shifted, r.y, r.w, treated))
            resid, var = r.w * (r.y - float(pi)), r.w * float(pi) * (1.0 - float(pi))
            a = self._stats.setdefault(r.customer_id, [0.0, 0.0])
            a[0] += resid
            a[1] += var
            if treated:
                b = self._stats_t.setdefault(r.customer_id, [0.0, 0.0])
                b[0] += resid
                b[1] += var
            self._person_cache.pop(r.customer_id, None)
            r.absorbed = True
        if len(self._preq) > 100_000:
            self._preq = self._preq[-100_000:]

    def _person(self, customer_id: str) -> tuple[float, float, float]:
        """(baseline shift, response shift, variance of the response shift) for one customer."""
        found = self._person_cache.get(customer_id)
        if found is not None:
            return found
        fit = self._fit_state
        stats = self._stats.get(customer_id)
        if fit is None or stats is None:
            return 0.0, 0.0, (fit.sigma_effect if fit is not None else self.config.person_effect_var)
        s_all, v_all = stats
        s_t, v_t = self._stats_t.get(customer_id, (0.0, 0.0))
        h = np.array([[v_all + 1.0 / fit.sigma_base, v_t], [v_t, v_t + 1.0 / fit.sigma_effect]])
        cov = np.linalg.inv(h)
        u, v = cov @ np.array([s_all, s_t])
        out = (float(u), float(v), float(cov[1, 1]))
        self._person_cache[customer_id] = out
        return out

    def _context_of(self, fit: _Fit, state: CustomerState) -> _Context:
        ctx = self._context
        if ctx is not None and ctx.state is state:
            return ctx
        z = self._z(fit, [profile_of(state)])[0]
        d = fit.d
        eta = fit.draws[:, 0] + fit.draws[:, 1 : 1 + d] @ z
        tau = fit.tau
        common = fit.draws[:, tau : tau + d] @ z
        c = len(fit.channels)
        chan = fit.chan
        by_channel = np.zeros((fit.draws.shape[0], c))
        for ci in range(c):
            lo = chan + ci * d
            by_channel[:, ci] = fit.draws[:, lo : lo + d] @ z
        ctx = _Context(state, z, eta, common, by_channel)
        self._context = ctx
        return ctx


# --- turning decisions and events into outcomes ---------------------------------------------------


class WindowLookup(Protocol):
    """What `OutcomeBook.close` reads of one customer's events (`autonomy.learning.observations.Timeline`)."""

    @property
    def sent_at(self) -> Mapping[str, datetime]: ...

    @property
    def undelivered(self) -> frozenset[str]: ...

    def bought(self, start: datetime, end: datetime) -> bool: ...

    def sends_between(self, start: datetime, end: datetime) -> int: ...


@dataclass
class _Pending:
    decision: Decision
    profile: Profile
    key: str | None


class OutcomeBook:
    """Remembers the agent's decisions with the profile they were made on, and closes them into
    `Outcome`s once their window has passed.

    A contacted decision (agent or baseline group, with its action key) closes `horizon` after the
    send; a holdout "do nothing" closes `horizon` after the decision and counts only if nothing was
    sent to the customer in between. Like the existing learner, consecutive control windows of one
    customer overlap, so each is weighted by the share not covered by their previous one; so are
    consecutive contacted windows (a follow-up sent inside a window is in both). A contacted outcome
    carries the decision's logged probability, which the crowd's fit weights by. The profile is the
    one the decision was made on, so what the model learns from is what it saw.

    The book lives in memory: a process that rebuilds the policy (the runtime does, on a changed
    brief or content library) starts with an empty book and an empty crowd."""

    def __init__(self, horizon: timedelta, *, max_age: timedelta | None = None) -> None:
        if horizon <= timedelta(0):
            raise ValueError("horizon must be positive")
        self.horizon = horizon
        self.max_age = max_age if max_age is not None else 4 * horizon
        self._pending: dict[str, _Pending] = {}
        self._control_start: dict[str, datetime] = {}
        self._treated_start: dict[str, datetime] = {}
        self.dropped: dict[str, int] = {}
        """Decisions closed without an outcome, by reason: `never_sent`, `undelivered`, `contaminated`, `stale`."""

    def __len__(self) -> int:
        return len(self._pending)

    def _drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def remember(self, decision: Decision, profile: Profile, key: str | None) -> bool:
        """Keep a decision whose window we can learn from; returns whether it was kept."""
        if decision.kind is DecisionKind.NOTHING:
            if decision.group is not Group.HOLDOUT:
                return False
            key = None
        elif decision.group not in (Group.AGENT, Group.BASELINE) or key is None or decision.action is None:
            return False
        self._pending.setdefault(decision.decision_id, _Pending(decision, profile, key))
        return True

    def forget(self, decision_id: str) -> None:
        """Drop a kept decision that is being made again (the experiments' wrapper re-decides a moment)."""
        self._pending.pop(decision_id, None)

    def close(self, now: datetime, timeline_of: Callable[[str], WindowLookup | None]) -> list[Outcome]:
        """The outcomes whose window has ended by `now`. Decisions of customers the lookup does not
        know stay until `max_age`."""
        out: list[Outcome] = []
        controls: list[tuple[_Pending, bool]] = []
        for did, pend in sorted(
            self._pending.items(), key=lambda kv: (kv[1].decision.customer_id, kv[1].decision.made_at, kv[0])
        ):
            d = pend.decision
            tl = timeline_of(d.customer_id)
            if tl is None:
                if now - d.made_at > self.max_age:
                    del self._pending[did]
                    self._drop("stale")
                continue
            if d.kind is DecisionKind.NOTHING:
                end = d.made_at + self.horizon
                if end > now:
                    continue
                del self._pending[did]
                if tl.sends_between(d.made_at, end) > 0:
                    self._drop("contaminated")
                    continue
                controls.append((pend, tl.bought(d.made_at, end)))
                continue
            assert d.action is not None
            sent = tl.sent_at.get(did)
            start = sent if sent is not None else d.action.send_at
            end = start + self.horizon
            if end > now:
                if now - d.made_at > self.max_age:
                    del self._pending[did]
                    self._drop("stale")
                continue
            del self._pending[did]
            if sent is None or did in tl.undelivered:
                self._drop("never_sent" if sent is None else "undelivered")
                continue
            weight = self._overlap(self._treated_start, d.customer_id, start)
            out.append(
                Outcome(
                    d.customer_id,
                    pend.profile,
                    pend.key,
                    tl.bought(start, end),
                    start,
                    end,
                    weight,
                    d.probability,
                )
            )
        for pend, converted in controls:
            d = pend.decision
            end = d.made_at + self.horizon
            weight = self._overlap(self._control_start, d.customer_id, d.made_at)
            out.append(Outcome(d.customer_id, pend.profile, None, converted, d.made_at, end, weight))
        return out

    def _overlap(self, starts: dict[str, datetime], customer_id: str, start: datetime) -> float:
        """The share of this window not covered by the customer's previous one of the same kind."""
        last = starts.get(customer_id)
        starts[customer_id] = max(start, last) if last is not None else start
        if last is None:
            return 1.0
        return min(1.0, abs((start - last).total_seconds()) / self.horizon.total_seconds())


__all__ = [
    "BASE_NAMES",
    "FEATURES",
    "CalibrationReport",
    "CalibrationRow",
    "Estimate",
    "LookalikeConfig",
    "LookalikeResponseModel",
    "Outcome",
    "OutcomeBook",
    "Profile",
    "WindowLookup",
    "history_profile",
    "precision_blend",
    "profile_of",
    "stamp",
]
