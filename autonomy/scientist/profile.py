"""A compact, numbers-only profile of a shop's data: what the Data Doctor reads.

The Data Doctor is a language model, so what it sees is the part of the data we are willing to put in a
prompt: counts, rates and column names. **No raw rows, no customer ids, no addresses, no free text.**
The only strings in a profile are event type names, currency codes, message (arm) ids and feature
column names; `build_profile` never copies a customer id, an event id or a value of a feature.

What it measures (all from the events; nothing is fitted):

* the size of the history: customers, events by type, days covered, campaigns logged;
* **the latest closed campaign**, found the way `autonomy.uplift.campaign` finds it (SEND events with a
  `campaign_id`, else bursts per channel separated by more than the window). A customer is *sent* when
  they got a send in it, and *unsent* when the history knew them before it started and they got none
  (the inferred control group). The window starts at the customer's first send, or at the campaign's
  median send time for an unsent customer;
* **whether there is a control group or randomisation**: unsent customers exist, every send carries a
  logged design probability strictly between 0 and 1, and (when the design probabilities are fixed) the
  sent / unsent split matches them (a sample-ratio check, `srm_p_value`);
* the click rate, the purchase rate in the window (sent against unsent), the lift with its standard
  error, and how purchases relate to clicks **inside and outside the control**: the purchase rate of
  those who clicked, of those sent who did not, and of the unsent, and `extra_purchases_per_click`
  = lift / click rate with a 95% interval. This is what says whether a click is a usable stand-in for
  a purchase here (it is when each click comes with real extra purchases, and not otherwise);
* the currencies on purchases, the features available (name, kind, distinct values, missing share).

Time and the window are passed in: the same events give the same profile.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from autonomy.contracts import Event, EventType

Z_95 = 1.959963984540054
MIN_CONTROL = 30
"""Fewer unsent customers than this and a control comparison says nothing; no control is claimed."""
DISTINCT_CAP = 1000
"""Distinct values of a feature are reported up to this, so a profile of an id-like column stays small."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Interval(_Frozen):
    estimate: float
    low: float
    high: float


class ArmProfile(_Frozen):
    """One message of the campaign (its `content_id`), and how its recipients did. Numbers only."""

    arm: str
    customers: int
    design_probability: float | None = Field(description="Mean logged design probability, null when none")
    click_rate: float | None = Field(description="Share of recipients who clicked in the window")
    purchase_rate: float = Field(description="Share of recipients who bought in the window")
    mean_order_value: float | None


class ControlProfile(_Frozen):
    unsent_customers: int
    unsent_share: float
    exists: bool = Field(description="Enough unsent customers to compare with (MIN_CONTROL)")
    design_probabilities_logged: bool = Field(description="Every send carries a probability in (0, 1)")
    randomised: bool = Field(description="A control exists and the design probabilities are logged")
    design_is_fixed: bool = Field(description="Each arm's logged probability is constant")
    implied_control_share: float | None = Field(description="1 minus the arms' design probabilities")
    srm_p_value: float | None = Field(description="Sent / unsent split against the design; null if unknown")


class OutcomeProfile(_Frozen):
    window_days: float
    sent_purchase_rate: float
    unsent_purchase_rate: float | None
    lift: float | None = Field(description="Sent minus unsent purchase rate (absolute)")
    lift_se: float | None
    relative_lift: float | None
    purchases: int = Field(description="Purchases in the window, sent and unsent together")
    mean_order_value: float | None


class ClickProfile(_Frozen):
    clicks_logged: bool
    click_rate: float | None = Field(description="Share of sent customers who clicked in the window")
    purchase_rate_clickers: float | None
    purchase_rate_sent_non_clickers: float | None
    purchase_rate_unsent: float | None
    extra_purchases_per_click: Interval | None = Field(
        description="Lift divided by click rate, 95% interval; null without a control or clicks"
    )


class FeatureInfo(_Frozen):
    name: str
    kind: Literal["numeric", "categorical"]
    distinct: int
    missing_share: float


class DataProfile(_Frozen):
    """Everything the Data Doctor is allowed to know about the data. See the module docstring."""

    customers: int
    events_total: int
    events_by_type: dict[str, int]
    history_days: float
    campaigns_logged: int
    sends_total: int
    campaign_found: bool = Field(description="A closed campaign with sends was found and analysed")
    campaign_customers_sent: int
    campaign_channels: tuple[str, ...]
    arms: tuple[ArmProfile, ...]
    control: ControlProfile | None
    outcome: OutcomeProfile | None
    clicks: ClickProfile | None
    currencies: dict[str, int]
    purchases_without_currency: int
    mixed_currencies: bool
    features: tuple[FeatureInfo, ...]

    @property
    def arm_ids(self) -> tuple[str, ...]:
        return tuple(a.arm for a in self.arms)

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.features)

    def to_prompt_json(self) -> str:
        """The compact JSON a prompt carries: no spaces, floats rounded to 6 significant digits."""
        return json.dumps(_rounded(self.model_dump(mode="json")), separators=(",", ":"))


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return float(f"{value:.6g}")
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


# --- small statistics (no scipy) ------------------------------------------------------------------


def _gamma_q(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x): series for x < a + 1, continued fraction otherwise."""
    if x <= 0.0:
        return 1.0
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1.0:
        term = total = 1.0 / a
        n = a
        for _ in range(500):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return max(0.0, min(1.0, 1.0 - total * math.exp(log_front)))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return max(0.0, min(1.0, math.exp(log_front) * h))


def chi2_sf(x: float, df: int) -> float:
    """P(chi-square with `df` degrees of freedom >= x)."""
    if df <= 0:
        raise ValueError("df must be positive")
    return _gamma_q(df / 2.0, max(0.0, x) / 2.0)


def srm_p_value(observed: Sequence[int], expected_shares: Sequence[float]) -> float:
    """Chi-square p of observed group counts against the design's shares (shares are normalised)."""
    total = sum(observed)
    shares_sum = sum(expected_shares)
    if len(observed) != len(expected_shares) or len(observed) < 2 or total <= 0 or shares_sum <= 0:
        raise ValueError("need two or more groups with positive counts and shares")
    stat = 0.0
    for o, s in zip(observed, expected_shares, strict=True):
        e = total * s / shares_sum
        if e <= 0:
            if o > 0:
                return 0.0
            continue
        stat += (o - e) ** 2 / e
    return chi2_sf(stat, len(observed) - 1)


def _rate_se(p: float, n: int) -> float:
    return math.sqrt(max(p * (1.0 - p), 0.0) / n) if n > 0 else float("nan")


# --- the campaign ---------------------------------------------------------------------------------


def _bursts(sends: Sequence[Event], gap: timedelta) -> list[list[Event]]:
    """Sends per channel into bursts: a gap longer than `gap` starts a new campaign."""
    by_channel: dict[str, list[Event]] = defaultdict(list)
    for e in sends:
        by_channel[e.channel.value if e.channel else ""].append(e)
    out: list[list[Event]] = []
    for evs in by_channel.values():
        evs.sort(key=lambda e: e.ts)
        cur = [evs[0]]
        for e in evs[1:]:
            if e.ts - cur[-1].ts > gap:
                out.append(cur)
                cur = []
            cur.append(e)
        out.append(cur)
    return out


def _campaigns(sends: Sequence[Event], window: timedelta) -> list[list[Event]]:
    with_id: dict[str, list[Event]] = defaultdict(list)
    without: list[Event] = []
    for e in sends:
        cid = e.attrs.get("campaign_id")
        if cid is None:
            without.append(e)
        else:
            with_id[str(cid)].append(e)
    groups = list(with_id.values())
    if without:
        groups.extend(_bursts(without, window))
    return groups


def _design_probability(e: Event) -> float | None:
    for key in ("logged_probability", "probability"):
        v = e.attrs.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool) and 0.0 < float(v) < 1.0:
            return float(v)
    return None


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def describe_features(columns: Mapping[str, Sequence[object]]) -> tuple[FeatureInfo, ...]:
    """Name, kind, distinct count and missing share of each column. Values are never kept."""
    out: list[FeatureInfo] = []
    for name, values in columns.items():
        n = len(values)
        present = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
        numeric = bool(present) and all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in present
        )
        out.append(
            FeatureInfo(
                name=str(name),
                kind="numeric" if numeric else "categorical",
                distinct=min(len({str(v) for v in present}), DISTINCT_CAP),
                missing_share=(n - len(present)) / n if n else 0.0,
            )
        )
    return tuple(out)


def build_profile(
    events: Iterable[Event],
    *,
    window_days: float = 14.0,
    features: Mapping[str, Sequence[object]] | None = None,
    as_of: datetime | None = None,
) -> DataProfile:
    """The data profile of `events` (see the module docstring). Deterministic; copies no ids or values.

    `as_of` is the moment the data was cut (default: the latest event). A campaign counts as closed
    when its last send plus the window is not after it, so pass it when the history ends before
    "now" (a campaign whose outcomes were logged for exactly the window, say).

    Honesty note for the loop: the per-arm and click rates here are in-sample hints that the Data Doctor
    and Proposer read. The candidates they lead to must be scored on customers or time this profile did
    not see (build the profile from the fit part only, or treat these rates as hints and let only the
    held-out, corrected lower bounds decide), or an arm picked for looking best is judged on its own data.
    """
    evs = list(events)
    window = timedelta(days=window_days)
    by_type = Counter(e.type.value for e in evs)
    customers = {e.customer_id for e in evs}
    history_days = (max(e.ts for e in evs) - min(e.ts for e in evs)).total_seconds() / 86400.0 if evs else 0.0

    currencies: Counter[str] = Counter()
    no_currency = 0
    for e in evs:
        if e.type is EventType.PURCHASE:
            cur = e.attrs.get("currency")
            if isinstance(cur, str) and cur:
                currencies[cur.upper()] += 1
            else:
                no_currency += 1

    sends = [e for e in evs if e.type is EventType.SEND]
    groups = _campaigns(sends, window)
    as_of = as_of if as_of is not None else max((e.ts for e in evs), default=None)
    closed = [g for g in groups if as_of is not None and max(e.ts for e in g) + window <= as_of]
    feats = describe_features(features) if features else ()
    base: dict[str, Any] = {
        "customers": len(customers),
        "events_total": len(evs),
        "events_by_type": dict(sorted(by_type.items())),
        "history_days": round(history_days, 3),
        "campaigns_logged": len(groups),
        "sends_total": len(sends),
        "currencies": dict(sorted(currencies.items())),
        "purchases_without_currency": no_currency,
        "mixed_currencies": len(currencies) > 1,
        "features": feats,
    }
    if not closed:
        return DataProfile(
            **base,
            campaign_found=False,
            campaign_customers_sent=0,
            campaign_channels=(),
            arms=(),
            control=None,
            outcome=None,
            clicks=None,
        )
    campaign = max(closed, key=lambda g: (max(e.ts for e in g), len(g)))
    return _analyse_campaign(evs, campaign, window, window_days, base)


def _analyse_campaign(
    evs: Sequence[Event],
    campaign: Sequence[Event],
    window: timedelta,
    window_days: float,
    base: dict[str, Any],
) -> DataProfile:
    start = min(e.ts for e in campaign)
    median_send = sorted(e.ts for e in campaign)[len(campaign) // 2]
    first_send: dict[str, Event] = {}
    for e in sorted(campaign, key=lambda e: e.ts):
        first_send.setdefault(e.customer_id, e)
    sent_ids = set(first_send)
    known_before = {e.customer_id for e in evs if e.ts < start}
    unsent_ids = known_before - sent_ids
    channels = tuple(sorted({e.channel.value for e in campaign if e.channel}))

    t0 = {c: first_send[c].ts for c in sent_ids}
    t0.update(dict.fromkeys(unsent_ids, median_send))
    buys: dict[str, list[float]] = defaultdict(list)
    clicked: set[str] = set()
    for e in evs:
        t = t0.get(e.customer_id)
        if t is None or not (t <= e.ts < t + window):
            continue
        if e.type is EventType.PURCHASE:
            buys[e.customer_id].append(float(e.value or 0.0))
        elif e.type is EventType.CLICK and e.customer_id in sent_ids:
            clicked.add(e.customer_id)
    bought = set(buys)
    all_values = [v for vs in buys.values() for v in vs]

    # arms: the content of each customer's first send
    arm_of = {c: str(e.attrs.get("content_id") or "unlabelled") for c, e in first_send.items()}
    probs_by_arm: dict[str, list[float]] = defaultdict(list)
    members: dict[str, list[str]] = defaultdict(list)
    for c, arm in arm_of.items():
        members[arm].append(c)
        p = _design_probability(first_send[c])
        if p is not None:
            probs_by_arm[arm].append(p)
    arms: list[ArmProfile] = []
    for arm in sorted(members):
        ids = members[arm]
        order = [v for c in ids if c in bought for v in buys[c]]
        arms.append(
            ArmProfile(
                arm=arm,
                customers=len(ids),
                design_probability=_mean(probs_by_arm.get(arm, [])),
                click_rate=(
                    (sum(c in clicked for c in ids) / len(ids)) if clicked or _has_clicks(evs) else None
                ),
                purchase_rate=sum(c in bought for c in ids) / len(ids),
                mean_order_value=_mean(order),
            )
        )

    n_t, n_c = len(sent_ids), len(unsent_ids)
    all_logged = all(_design_probability(e) is not None for e in campaign)
    fixed = all_logged and all(
        len(ps) == len(members[a]) and (max(ps) - min(ps)) < 1e-9 for a, ps in probs_by_arm.items()
    )
    implied: float | None = None
    srm: float | None = None
    if fixed and probs_by_arm:
        implied = 1.0 - sum(statistics.fmean(ps) for ps in probs_by_arm.values())
        if implied >= -1e-9:
            implied = max(implied, 0.0)
            names = sorted(members)
            shares = [statistics.fmean(probs_by_arm[a]) for a in names] + [implied]
            counts = [len(members[a]) for a in names] + [n_c]
            keep = [i for i in range(len(shares)) if shares[i] > 0 or counts[i] > 0]
            if len(keep) >= 2:
                srm = srm_p_value([counts[i] for i in keep], [shares[i] for i in keep])
        else:
            implied = None
    exists = n_c >= MIN_CONTROL
    control = ControlProfile(
        unsent_customers=n_c,
        unsent_share=n_c / (n_t + n_c) if n_t + n_c else 0.0,
        exists=exists,
        design_probabilities_logged=all_logged,
        randomised=exists and all_logged,
        design_is_fixed=fixed,
        implied_control_share=implied,
        srm_p_value=srm,
    )

    p_t = len(bought & sent_ids) / n_t if n_t else 0.0
    p_c = len(bought & unsent_ids) / n_c if exists else None
    lift = lift_se = rel = None
    if p_c is not None:
        lift = p_t - p_c
        lift_se = math.sqrt(_rate_se(p_t, n_t) ** 2 + _rate_se(p_c, n_c) ** 2)
        rel = lift / p_c if p_c > 0 else None
    outcome = OutcomeProfile(
        window_days=window_days,
        sent_purchase_rate=p_t,
        unsent_purchase_rate=p_c,
        lift=lift,
        lift_se=lift_se,
        relative_lift=rel,
        purchases=len(all_values),
        mean_order_value=_mean(all_values),
    )

    clicks_logged = _has_clicks(evs)
    click_rate = len(clicked) / n_t if clicks_logged and n_t else None
    non_clickers = sent_ids - clicked
    per_click: Interval | None = None
    if clicks_logged and click_rate and lift is not None and lift_se is not None:
        est = lift / click_rate
        half = Z_95 * lift_se / click_rate
        per_click = Interval(estimate=est, low=est - half, high=est + half)
    clicks = ClickProfile(
        clicks_logged=clicks_logged,
        click_rate=click_rate,
        purchase_rate_clickers=(len(bought & clicked) / len(clicked)) if clicked else None,
        purchase_rate_sent_non_clickers=(
            (len(bought & non_clickers) / len(non_clickers)) if clicks_logged and non_clickers else None
        ),
        purchase_rate_unsent=p_c,
        extra_purchases_per_click=per_click,
    )
    return DataProfile(
        **base,
        campaign_found=True,
        campaign_customers_sent=n_t,
        campaign_channels=channels,
        arms=tuple(arms),
        control=control,
        outcome=outcome,
        clicks=clicks,
    )


def _has_clicks(evs: Sequence[Event]) -> bool:
    return any(e.type is EventType.CLICK for e in evs)


__all__ = [
    "ArmProfile",
    "ClickProfile",
    "ControlProfile",
    "DataProfile",
    "FeatureInfo",
    "Interval",
    "OutcomeProfile",
    "build_profile",
    "chi2_sf",
    "describe_features",
    "srm_p_value",
]
