"""Fit a Gym scenario to a real randomised campaign, so the Gym stops encoding our own beliefs.

The scenarios in `autonomy.gym.scenarios` plant truths we chose (a persona that is "price sensitive",
a message that lifts purchases by 0.4% a day). That tests whether the agent finds what we put there.
A *real-calibrated* scenario instead takes its numbers from a real randomised campaign (a control
group and one or more message arms) and plants those, so the question becomes: does the agent do well
in a world that behaves the way a real one did?

Nothing here knows a dataset. The caller (the bench's fitting script) hands over arrays for the
campaign's TRAIN customers; this module estimates, shrinks and packages them as a `CampaignFit`, and
`scenario_from_fit` turns a fit into a `ScenarioSpec`. The fitted numbers are then pasted as literals
into `autonomy.gym.scenarios` (a scenario never reads data at import time), and the fitting script
has a `--check` that refits and compares them.

What is fitted (everything on the TRAIN half only; `fit_campaign` ignores every other row):

* **Base purchase rate**, per customer group: the control group's chance of buying inside the
  campaign window, turned into the Gym's daily rate (`1 - (1 - p) ** (1 / window)`).
* **Message lift**, per group and per message arm: the extra chance of buying inside the window,
  relative to the base rate it is small (a few percent to a doubling for a cold list) and is NOT the
  Gym's invented 0.4% a day. Becomes the Gym's `channel_uplift` (mean over arms) and, with two arms,
  `theme_uplift` (each arm's lift relative to the mean: the **arm difference**).
* **Click behaviour**: the control group's click-like rate is the Gym's `click_floor` (people the
  message does not move); the extra clicks a message causes per extra purchase it causes is
  `click_per_extra_purchase`. The report also gives the share of a treated customer's clicks the
  message caused, and the click-to-purchase correlation among treated customers, which say how
  weakly clicks track money. A campaign with no click data fits floor 0 and slope 0.
* **Order value** and **contact cost** come from the caller (the data's receipts, or the bench's
  stated economics).

Group estimates are **shrunk** toward the pooled value (DerSimonian-Laird empirical Bayes), because
a lift measured on 3,000 customers with a 0.5% base rate is mostly noise; the report keeps the raw
and the shrunk number side by side.

What is NOT fitted, and why (recorded in `CampaignFit.not_fitted`, never silently):

* **Contact fatigue** and **unsubscribe / complaint rates**: a single-send randomised campaign has
  no repeated contacts and logs no unsubscribes, so these are not identifiable from it. They keep the
  Gym's library values and the fit says so.
* **Send-time effects, offers, sleeping dogs**: no variation in the data; planted as "no effect"
  (hour sensitivity 0, offers add nothing, no sleeping dogs) rather than as a belief.
* **Customer-to-customer spread inside a group**: the Gym's lognormal spread is kept.

No dataset names or dataset-specific code belong in this file.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Channel, OfferKind, Theme
from autonomy.gym.api import PERSONAS, ScenarioSpec

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

ARM_THEMES: tuple[Theme, ...] = (Theme.EDUCATION, Theme.REMINDER)
"""The Gym content themes standing for message arm 1, 2 of a two-arm campaign (both exist as
no-offer e-mail content in the library). The arms' fitted lifts become these themes' multipliers."""

BOOTSTRAP_DRAWS = 400
BOOTSTRAP_SEED = 20261021


@dataclass(frozen=True)
class GroupFit:
    """One customer group's fitted behaviour (probabilities are per campaign window)."""

    name: str
    n: int
    share: float
    control_rate_raw: float
    control_rate: float
    """Chance of buying in the window with no message, shrunk toward the pooled value."""
    arm_lift_raw: tuple[float, ...]
    arm_lift_se: tuple[float, ...]
    arm_lift: tuple[float, ...]
    """Extra chance of buying in the window per message arm, shrunk, never below 0."""


@dataclass(frozen=True)
class ClickFit:
    """How clicks relate to the purchases a message causes."""

    logged: bool
    control_rate: float
    treated_rate: float
    floor: float
    """Chance of a click from someone the message does not move (the control group's rate)."""
    per_extra_purchase: float
    """Extra clicks per extra purchase the message causes (ratio of the two treatment effects)."""
    per_extra_purchase_low: float
    per_extra_purchase_high: float
    caused_share_of_treated_clicks: float
    click_purchase_phi: float
    """Correlation between clicking and buying among treated customers (0 = unrelated)."""


@dataclass(frozen=True)
class CampaignFit:
    """Everything a real-calibrated scenario takes from one randomised campaign."""

    provenance: str
    window_days: int
    n_train: int
    channel: Channel
    arms: tuple[str, ...]
    groups: tuple[GroupFit, ...]
    clicks: ClickFit
    aov_mean: float
    margin_rate: float
    channel_cost: float
    fatigue_per_weekly_contact: float
    not_fitted: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def pooled_lift(self) -> float:
        """Mean extra chance of buying per message over groups and arms, weighted by group share."""
        return sum(g.share * sum(g.arm_lift) / len(g.arm_lift) for g in self.groups)

    def pooled_control_rate(self) -> float:
        return sum(g.share * g.control_rate for g in self.groups)


# --- estimation ----------------------------------------------------------------------------------


def shrink(theta: Sequence[float], se: Sequence[float]) -> tuple[list[float], float]:
    """Empirical-Bayes shrinkage of group estimates toward their precision-weighted mean.

    DerSimonian-Laird: `tau2` is the between-group variance beyond sampling noise (0 when the groups
    differ no more than chance explains, which pulls every group to the pooled value). Returns the
    shrunk estimates and `tau2`."""
    th = np.asarray(theta, dtype=np.float64)
    var = np.maximum(np.asarray(se, dtype=np.float64) ** 2, 1e-18)
    w = 1.0 / var
    pooled = float((w * th).sum() / w.sum())
    k = len(th)
    if k < 2:
        return [float(x) for x in th], 0.0
    q = float((w * (th - pooled) ** 2).sum())
    denom = float(w.sum() - (w**2).sum() / w.sum())
    tau2 = max(0.0, (q - (k - 1)) / denom) if denom > 0 else 0.0
    weight = tau2 / (tau2 + var)
    return [float(x) for x in pooled + weight * (th - pooled)], tau2


def daily_rate(window_probability: float, window_days: float) -> float:
    """The per-day chance that gives `window_probability` of at least one purchase in the window."""
    p = min(max(window_probability, 0.0), 1.0 - 1e-9)
    return float(1.0 - (1.0 - p) ** (1.0 / window_days))


def _binomial_se(p: float, n: int) -> float:
    return math.sqrt(max(p * (1.0 - p), 1e-12) / max(n, 1))


def fit_groups(
    arm: IntArray,
    bought: FloatArray,
    group: npt.NDArray[Any],
    group_names: Sequence[str],
    n_arms: int,
) -> tuple[GroupFit, ...]:
    """Per-group base rate and per-arm lift from rows that are already TRAIN-only.

    `arm` is 0 for the control and 1..n_arms for a message arm; `bought` is 0/1 for buying in the
    window; `group[i]` is the row's group name."""
    raw: list[tuple[str, int, float, float, list[float], list[float]]] = []
    for name in group_names:
        m = group == name
        n = int(m.sum())
        ctrl = m & (arm == 0)
        n0 = int(ctrl.sum())
        if n0 < 2:
            raise ValueError(f"group {name!r} has {n0} control rows; cannot fit a base rate")
        p0 = float(bought[ctrl].mean())
        se0 = _binomial_se(p0, n0)
        lifts: list[float] = []
        ses: list[float] = []
        for a in range(1, n_arms + 1):
            sel = m & (arm == a)
            na = int(sel.sum())
            if na < 2:
                raise ValueError(f"group {name!r} has {na} rows in arm {a}; cannot fit its lift")
            pa = float(bought[sel].mean())
            lifts.append(pa - p0)
            ses.append(math.hypot(_binomial_se(pa, na), se0))
        raw.append((name, n, p0, se0, lifts, ses))
    total = sum(r[1] for r in raw)
    base_star, _ = shrink([r[2] for r in raw], [r[3] for r in raw])
    lift_star: list[list[float]] = []
    for a in range(n_arms):
        col, _ = shrink([r[4][a] for r in raw], [r[5][a] for r in raw])
        lift_star.append(col)
    out: list[GroupFit] = []
    for i, (name, n, p0, _se0, lifts, ses) in enumerate(raw):
        out.append(
            GroupFit(
                name=name,
                n=n,
                share=n / total,
                control_rate_raw=p0,
                control_rate=min(max(base_star[i], 0.0), 1.0),
                arm_lift_raw=tuple(lifts),
                arm_lift_se=tuple(ses),
                arm_lift=tuple(max(lift_star[a][i], 0.0) for a in range(n_arms)),
            )
        )
    return tuple(out)


def _click_stats(
    treated: BoolArray, clicked: FloatArray, bought: FloatArray
) -> tuple[float, float, float, float]:
    """Control click rate, treated click rate, control purchase rate, treated purchase rate."""
    c0, c1 = clicked[~treated], clicked[treated]
    b0, b1 = bought[~treated], bought[treated]
    return float(c0.mean()), float(c1.mean()), float(b0.mean()), float(b1.mean())


def fit_clicks(arm: IntArray, bought: FloatArray, clicked: FloatArray | None) -> ClickFit:
    """The Gym's two click knobs from TRAIN-only rows (`arm` > 0 is treated).

    `click_floor` = the control group's click-like rate; `click_per_extra_purchase` = (treated minus
    control click rate) / (treated minus control purchase rate), with a bootstrap 95% interval. With
    no click data the floor and slope are 0 and the fit says the channel logs no clicks."""
    if clicked is None:
        return ClickFit(False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treated = arm > 0
    c0, c1, b0, b1 = _click_stats(treated, clicked, bought)
    d_click, d_buy = c1 - c0, b1 - b0
    slope = max(d_click / d_buy, 0.0) if d_buy > 0 else 0.0
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    n = len(arm)
    draws: list[float] = []
    for _ in range(BOOTSTRAP_DRAWS):
        idx = rng.integers(0, n, n)
        tr = treated[idx]
        r0, r1, s0, s1 = _click_stats(tr, clicked[idx], bought[idx])
        db = s1 - s0
        draws.append(max((r1 - r0) / db, 0.0) if db > 0 else 0.0)
    low, high = (float(x) for x in np.quantile(draws, [0.025, 0.975]))
    ct, bt = clicked[treated], bought[treated]
    phi = float(np.corrcoef(ct, bt)[0, 1]) if ct.std() > 0 and bt.std() > 0 else 0.0
    return ClickFit(
        logged=True,
        control_rate=c0,
        treated_rate=c1,
        floor=c0,
        per_extra_purchase=slope,
        per_extra_purchase_low=low,
        per_extra_purchase_high=high,
        caused_share_of_treated_clicks=max(d_click, 0.0) / c1 if c1 > 0 else 0.0,
        click_purchase_phi=phi,
    )


def fit_campaign(
    *,
    arm: IntArray,
    bought: FloatArray,
    clicked: FloatArray | None,
    group: npt.NDArray[Any],
    group_names: Sequence[str],
    train: BoolArray,
    arms: Sequence[str],
    window_days: int,
    channel: Channel,
    aov_mean: float,
    margin_rate: float,
    channel_cost: float,
    fatigue_per_weekly_contact: float,
    not_fitted: Sequence[str],
    provenance: str,
    notes: Sequence[str] = (),
) -> CampaignFit:
    """Fit a scenario to a randomised campaign. Only rows where `train` is True are ever read."""
    if len(arm) != len(train):
        raise ValueError("arm and train must be the same length")
    a = np.asarray(arm)[train]
    b = np.asarray(bought, dtype=np.float64)[train]
    c = None if clicked is None else np.asarray(clicked, dtype=np.float64)[train]
    g = np.asarray(group)[train]
    return CampaignFit(
        provenance=provenance,
        window_days=window_days,
        n_train=int(train.sum()),
        channel=channel,
        arms=tuple(arms),
        groups=fit_groups(a, b, g, group_names, len(arms)),
        clicks=fit_clicks(a, b, c),
        aov_mean=aov_mean,
        margin_rate=margin_rate,
        channel_cost=channel_cost,
        fatigue_per_weekly_contact=fatigue_per_weekly_contact,
        not_fitted=tuple(not_fitted),
        notes=tuple(notes),
    )


# --- from a fit to a scenario --------------------------------------------------------------------


def _theme_multipliers(group: GroupFit) -> dict[str, float]:
    """Every Gym theme's multiplier for a group: with two or more arms, arm i's lift relative to the
    mean over arms (the arm difference); every other theme, 1."""
    out = {t.value: 1.0 for t in Theme}
    mean = sum(group.arm_lift) / len(group.arm_lift)
    if len(group.arm_lift) > 1 and mean > 0:
        for theme, lift in zip(ARM_THEMES, group.arm_lift, strict=False):
            out[theme.value] = lift / mean
    return out


def scenario_from_fit(
    fit: CampaignFit,
    *,
    name: str,
    description: str,
    history_sends_per_week: float,
    complaint_cost: float,
    persona_labels: Mapping[str, str],
    extra: Mapping[str, object] | None = None,
) -> ScenarioSpec:
    """The Gym scenario that plants a fit's numbers.

    The Gym keeps a closed set of persona names (`PERSONAS`), so each fitted group is planted under one
    of them (`persona_labels`: group name -> persona name, one each). The label carries no meaning
    except two side effects of the world, which the caller should know: a group labelled "new" signed
    up within the last month, and one labelled "at_risk" bought more earlier in the history, falling to
    its fitted rate by day 0. Every behaviour of a persona (lift, offers, themes, rate) is set
    explicitly from the fit, never taken from the persona's default.

    Everything the data cannot identify is neutral or the library value (see the module notes):
    hour sensitivity 0, offers multiply the lift by 1, no sleeping dogs, the library's fatigue and
    unsubscribe rates (the fit's `not_fitted` lists them)."""
    channels = (Channel.EMAIL, Channel.WHATSAPP, Channel.SMS, Channel.PUSH)
    labels = [persona_labels[g.name] for g in fit.groups]
    if len(set(labels)) != len(labels) or not set(labels) <= set(PERSONAS):
        raise ValueError(f"persona_labels must give each group its own name from {PERSONAS}; got {labels}")
    personas = dict(zip(labels, fit.groups, strict=True))
    other_kinds = [k.value for k in OfferKind if k is not OfferKind.NONE]
    fields: dict[str, object] = {
        "name": name,
        "description": description,
        "persona_mix": {n: g.share for n, g in personas.items()},
        "daily_purchase_rate": {n: daily_rate(g.control_rate, fit.window_days) for n, g in personas.items()},
        "channel_uplift": {
            n: {
                ch: (sum(g.arm_lift) / len(g.arm_lift) / fit.window_days if ch is fit.channel else 0.0)
                for ch in channels
            }
            for n, g in personas.items()
        },
        "offer_uplift": {n: dict.fromkeys(other_kinds, 1.0) for n in personas},
        "theme_uplift": {n: _theme_multipliers(g) for n, g in personas.items()},
        "effect_days": fit.window_days,
        "aov_mean": fit.aov_mean,
        "margin_rate": fit.margin_rate,
        "channel_consent": {
            ch: (1.0 if ch is fit.channel else 0.0) for ch in (Channel.EMAIL, Channel.WHATSAPP)
        },
        "channel_cost": {fit.channel: fit.channel_cost},
        "hour_sensitivity": 0.0,
        "sleeping_dog_share": 0.0,
        "fatigue_per_weekly_contact": fit.fatigue_per_weekly_contact,
        "click_floor": fit.clicks.floor,
        "click_per_extra_purchase": fit.clicks.per_extra_purchase,
        "history_sends_per_week": history_sends_per_week,
        "complaint_cost": complaint_cost,
    }
    if extra:
        fields.update(extra)
    return ScenarioSpec.model_validate(fields)


def fit_summary(fit: CampaignFit) -> list[dict[str, Any]]:
    """Per-group rows for a table: raw and shrunk base rate and lifts, as plain numbers."""
    rows: list[dict[str, Any]] = []
    for g in fit.groups:
        rows.append(
            {
                "group": g.name,
                "n": g.n,
                "share": g.share,
                "control_rate_raw": g.control_rate_raw,
                "control_rate": g.control_rate,
                "daily_rate": daily_rate(g.control_rate, fit.window_days),
                "lift_raw": list(g.arm_lift_raw),
                "lift_se": list(g.arm_lift_se),
                "lift": list(g.arm_lift),
                "relative_lift": [x / g.control_rate if g.control_rate > 0 else None for x in g.arm_lift],
            }
        )
    return rows
