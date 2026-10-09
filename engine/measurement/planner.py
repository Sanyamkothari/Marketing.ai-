"""The test planner (Plan J M93): how big a test must be, what it can see, and what it costs.

Realistic campaign effects are one to three points of churn or conversion. A control group too
small to see such an effect gives a range that crosses zero even when the campaign worked, and that
reads as failure. This module answers the planning questions *before* the campaign, from counts
alone: no storage, no network, no customer data. It uses `statistics.NormalDist`, as
`engine.uplift.incrementality` does.

**The test it plans.** The comparison `engine.uplift.incrementality` runs after the campaign: the
contacted group's outcome rate against the control group's, two-sided, at significance `alpha`
(default 0.05, i.e. 95% confidence), with `power` (default 0.80) the chance of seeing a real effect.
With `n_t` contacted and `n_c` held-back customers, base rate `p0` (the control group's expected
rate), effect `d` (so the contacted rate is `p1 = p0 + d`), `z_a = Φ⁻¹(1 − alpha/2)` and
`z_b = Φ⁻¹(power)`:

    p̄   = (n_t·p1 + n_c·p0) / (n_t + n_c)            the pooled rate the test will see
    se0 = sqrt(p̄(1 − p̄)(1/n_t + 1/n_c))               spread of the difference if nothing changed
    se1 = sqrt(p1(1 − p1)/n_t + p0(1 − p0)/n_c)       spread if the effect is real
    power(d) = Φ((|d| − z_a·se0) / se1)

That is the textbook closed form for two proportions (Fleiss, without continuity correction): with
equal groups of `n`, solving it for `n` gives

    n = (z_a·sqrt(2·p̄(1 − p̄)) + z_b·sqrt(p0(1 − p0) + p1(1 − p1)))² / d²

which is :func:`n_for_mde`: 4% → 3% needs 5,301 per group and 10% → 8% needs 3,213 (both at 95% and
80%). The far tail (a significant result in the wrong direction) is ignored, as the closed form
does. That makes the smallest detectable effect slightly conservative (a little larger than the exact
one), and only matters for control groups of a few dozen customers at a low base rate: with 60 held
back at 4%, counting the far tail adds about 0.02 to the power at the smallest fall. With control
groups of hundreds or more it is negligible. `engine.uplift.power` (Plan I) keeps the far tail, so
the two agree except in that small-group corner.

**The smallest detectable effect** (:func:`mde_two_proportions`) is the smallest `|d|` with
`power(d) ≥ power`, found by bisection. A fall and a rise of the same size are not equally easy to
see (the spread depends on the rate), so `direction` says which: `"up"`, `"down"`, or `"either"`
(the default), the larger of the two - the change the test is sure to see whichever way it goes.
With a small or unequal control group the two differ a lot (20,000 eligible, 5% held back, a 4% base
rate: a rise of 2.04 points, a fall of 1.53), so a caller that knows which way the campaign aims
passes it: the readiness report passes `"down"` for an outcome the use case exists to prevent.

**Null with a reason, never 0.** A number that cannot be computed - the base rate is unknown, a
group would be empty, every customer already has the outcome, no value per conversion was entered -
is `None`, and the result carries a plain sentence saying why (Plan J §3.1, rule 1). Arguments that
are a caller's mistake (a negative count, `alpha` outside 0..1) raise `ValueError` instead.

**Costs** are ranges in rupees. Holding customers back costs the conversions the campaign would have
brought them: `n_holdout × extra conversions per contact × value per conversion`
(:func:`cost_of_holdout`). Contacting a random few outside the selection costs every contact, and
the offer for those who take it: from `n × contact cost` (nobody takes it) to
`n × (contact cost + offer cost)` (everybody does) (:func:`cost_of_explore`).

**Amounts (Plan J M102, DEC-1312).** A campaign measured on revenue is compared by the difference in
means (Welch, `engine.measurement.continuous`). With `sd` the spread of the amount per customer and an
expected `rho2` - the share of that spread an amount from before the campaign explains, which the
adjusted estimate removes (0 without one) - the difference has the spread

    se = sd · sqrt(1 − rho2) · sqrt(1/n_t + 1/n_c)

so a test of these groups sees a change of `(z_a + z_b) · se` with `power` (:func:`mde_continuous`,
the far tail ignored as above), and seeing a change `d` with equal groups needs
`n = (z_a + z_b)² · sd² · (1 − rho2) · 2 / d²` per group (:func:`n_for_mde_continuous`). A covariate
with `rho2 = 0.36` therefore needs 36% fewer customers for the same change: the reason to register one.
These are the normal approximations of the test the report runs; with the hundreds of customers per
group a campaign has, Welch's t is the normal to two decimals. A spread that is unknown is a plain
reason, never a guess.

Every sentence this module writes is plain language (`engine.pilot.plain.jargon_in` finds nothing in
it; a test runs every reason through it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_POWER",
    "MAX_HOLDOUT_SHARE",
    "ArmSizes",
    "ContinuousMde",
    "CostEstimate",
    "Direction",
    "HoldoutPlan",
    "Mde",
    "MoneyRange",
    "PowerEstimate",
    "PowerPreview",
    "PowerPreviewPoint",
    "PowerPreviewRequest",
    "achieved_power",
    "achieved_power_continuous",
    "arm_sizes",
    "continuous_power_preview",
    "cost_of_explore",
    "cost_of_holdout",
    "holdout_for_mde",
    "mde_continuous",
    "mde_two_proportions",
    "n_for_mde",
    "n_for_mde_continuous",
    "power_preview",
]

DEFAULT_ALPHA: Final[float] = 0.05
DEFAULT_POWER: Final[float] = 0.80
MAX_HOLDOUT_SHARE: Final[float] = 0.50
"""Beyond half, a bigger control group shrinks the contacted group and the test gets worse, not better."""

Direction = Literal["up", "down", "either"]

_NORMAL: Final[NormalDist] = NormalDist()
_BISECTION_STEPS: Final[int] = 100

# The reasons: one sentence each, plain language, reused wherever the same thing is unknown.
REASON_BASE_RATE_UNKNOWN: Final[str] = (
    "The base rate is not known, so the smallest change the test can see cannot be worked out. "
    "Enter the share of customers who have the outcome without any campaign."
)
REASON_BASE_RATE_EDGE: Final[str] = (
    "The base rate is 0% or 100%, so there is no room for a campaign to change it."
)
REASON_EMPTY_GROUP: Final[str] = (
    "One of the two groups would have no customers, so there is nothing to compare."
)
REASON_TOO_FEW: Final[str] = (
    "Too few customers: even the largest possible change could not be told apart from chance."
)
REASON_NO_ROOM: Final[str] = "The change asked for would take the rate below 0% or above 100%."
REASON_NO_EFFECT: Final[str] = "A change of zero can never be told apart from chance."
REASON_HALF_NOT_ENOUGH: Final[str] = (
    "Not possible: even holding back half of the customers would not show a change this small."
)
REASON_NO_VALUE: Final[str] = "No value per conversion was entered, so the cost cannot be put in rupees."
REASON_NO_EFFECT_SIZE: Final[str] = (
    "The size of the campaign's effect is not known, so the cost of holding customers back cannot be "
    "worked out."
)
REASON_NO_CONTACT_COST: Final[str] = (
    "No contact cost or offer cost was entered, so the cost cannot be put in rupees."
)


def _check_alpha_power(alpha: float, power: float | None = None) -> None:
    if not (math.isfinite(alpha) and 0.0 < alpha < 1.0):
        raise ValueError("alpha must be between 0 and 1.")
    if power is not None and not (math.isfinite(power) and 0.0 < power < 1.0):
        raise ValueError("power must be between 0 and 1.")


def _check_count(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")


def _base_rate_reason(base_rate: float | None) -> str | None:
    """Why nothing can be planned on `base_rate`, or None when it is usable."""
    if base_rate is None:
        return REASON_BASE_RATE_UNKNOWN
    if not math.isfinite(base_rate) or not 0.0 <= base_rate <= 1.0:
        raise ValueError("The base rate must be between 0 and 1.")
    if base_rate <= 0.0 or base_rate >= 1.0:
        return REASON_BASE_RATE_EDGE
    return None


def _z_alpha(alpha: float) -> float:
    return _NORMAL.inv_cdf(1.0 - alpha / 2.0)


def _power(n_treat: int, n_control: int, p0: float, effect: float, alpha: float) -> float:
    """`power(d)` of the module docstring; both groups non-empty, `p0 + effect` inside [0, 1]."""
    p1 = p0 + effect
    pooled = (n_treat * p1 + n_control * p0) / (n_treat + n_control)
    se_null = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_treat + 1.0 / n_control))
    se_alt = math.sqrt(p1 * (1.0 - p1) / n_treat + p0 * (1.0 - p0) / n_control)
    threshold = _z_alpha(alpha) * se_null
    if se_alt <= 0.0:
        return 1.0 if abs(effect) > threshold else 0.0
    return _NORMAL.cdf((abs(effect) - threshold) / se_alt)


# ---------------------------------------------------------------------------
# Results: a value, or None with the reason
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Mde:
    """The smallest change a test of these groups can reliably see."""

    absolute: float | None
    """In rate points as a fraction: 0.012 is 1.2 percentage points. None when it cannot be said."""
    relative: float | None
    """`absolute / base_rate`: 0.3 is a change of 30% of the base rate."""
    n_treat: int
    n_control: int
    reason: str | None = None
    """Why `absolute` is None; None when it is not."""

    @property
    def points(self) -> float | None:
        """`absolute` in percentage points (1.2 for 0.012)."""
        return None if self.absolute is None else self.absolute * 100.0


@dataclass(frozen=True, slots=True)
class ArmSizes:
    """Customers each group of a test needs."""

    n_treat: int | None
    n_control: int | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class HoldoutPlan:
    """The smallest control group, as a share of the eligible customers, that sees a given change."""

    share: float | None
    n_treat: int | None
    n_control: int | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PowerEstimate:
    """The chance a test of these groups sees a change of a given size."""

    power: float | None
    reason: str | None = None


class MoneyRange(BaseModel):
    """An amount in rupees that is known only to lie between `low` and `high`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low: float = Field(description="Lower end, in rupees.")
    high: float = Field(description="Upper end, in rupees; equal to `low` when the amount is exact.")
    currency: Literal["INR"] = Field(default="INR", description="Always rupees.")


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A cost range, or None with the reason it could not be worked out."""

    amount: MoneyRange | None
    reason: str | None = None


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------
def achieved_power(
    n_treat: int,
    n_control: int,
    base_rate: float | None,
    effect: float,
    alpha: float = DEFAULT_ALPHA,
) -> PowerEstimate:
    """The chance that a test with these groups sees a true change of `effect` (signed, absolute).

    `effect` is in rate points as a fraction: -0.01 is a fall of one point. None with a reason when
    the base rate is unknown or at 0% or 100%, a group is empty, `effect` is 0, or the changed rate
    would leave 0..1.
    """
    _check_alpha_power(alpha)
    _check_count("n_treat", n_treat)
    _check_count("n_control", n_control)
    reason = _base_rate_reason(base_rate)
    if reason is not None or base_rate is None:
        return PowerEstimate(power=None, reason=reason or REASON_BASE_RATE_UNKNOWN)
    if n_treat == 0 or n_control == 0:
        return PowerEstimate(power=None, reason=REASON_EMPTY_GROUP)
    if not math.isfinite(effect) or effect == 0.0:
        return PowerEstimate(power=None, reason=REASON_NO_EFFECT)
    if not 0.0 <= base_rate + effect <= 1.0:
        return PowerEstimate(power=None, reason=REASON_NO_ROOM)
    return PowerEstimate(power=_power(n_treat, n_control, base_rate, effect, alpha))


def _smallest(n_treat: int, n_control: int, p0: float, alpha: float, power: float, sign: int) -> float | None:
    """Bisect for the smallest effect, in direction `sign`, seen with `power`; None if none is."""
    room = (1.0 - p0) if sign > 0 else p0
    if room <= 0.0 or _power(n_treat, n_control, p0, sign * room, alpha) < power:
        return None
    low, high = 0.0, room
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2.0
        if _power(n_treat, n_control, p0, sign * middle, alpha) >= power:
            high = middle
        else:
            low = middle
    return high


def mde_two_proportions(
    n_treat: int,
    n_control: int,
    base_rate: float | None,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    *,
    direction: Direction = "either",
) -> Mde:
    """The smallest change in the outcome rate a test with these groups sees with `power`.

    `direction="either"` (the default) is the larger of the smallest rise and the smallest fall: the
    change the test is sure to see whichever way it goes. None with a reason when it cannot be said.
    """
    _check_alpha_power(alpha, power)
    _check_count("n_treat", n_treat)
    _check_count("n_control", n_control)
    reason = _base_rate_reason(base_rate)
    if reason is not None or base_rate is None:
        return Mde(None, None, n_treat, n_control, reason or REASON_BASE_RATE_UNKNOWN)
    if n_treat == 0 or n_control == 0:
        return Mde(None, None, n_treat, n_control, REASON_EMPTY_GROUP)
    signs = {"up": (1,), "down": (-1,), "either": (1, -1)}[direction]
    found = [_smallest(n_treat, n_control, base_rate, alpha, power, sign) for sign in signs]
    if any(value is None for value in found):
        return Mde(None, None, n_treat, n_control, REASON_TOO_FEW)
    absolute = max(value for value in found if value is not None)
    return Mde(absolute, absolute / base_rate, n_treat, n_control)


def n_for_mde(
    base_rate: float | None,
    mde: float,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    *,
    control_ratio: float = 1.0,
) -> ArmSizes:
    """Customers each group needs to see a change of `mde` (signed, absolute) with `power`.

    The closed form of the module docstring, rounded up. `control_ratio` is control customers per
    contacted customer (1.0: equal groups; 0.1: a control group a tenth the size of the contacted
    group).
    """
    _check_alpha_power(alpha, power)
    if not (math.isfinite(control_ratio) and control_ratio > 0.0):
        raise ValueError("control_ratio must be greater than 0.")
    reason = _base_rate_reason(base_rate)
    if reason is not None or base_rate is None:
        return ArmSizes(None, None, reason or REASON_BASE_RATE_UNKNOWN)
    if not math.isfinite(mde) or mde == 0.0:
        return ArmSizes(None, None, REASON_NO_EFFECT)
    p0, p1, k = base_rate, base_rate + mde, control_ratio
    if not 0.0 <= p1 <= 1.0:
        return ArmSizes(None, None, REASON_NO_ROOM)
    pooled = (p1 + k * p0) / (1.0 + k)
    z_a, z_b = _z_alpha(alpha), _NORMAL.inv_cdf(power)
    root = z_a * math.sqrt(pooled * (1.0 - pooled) * (1.0 + 1.0 / k)) + z_b * math.sqrt(
        p1 * (1.0 - p1) + p0 * (1.0 - p0) / k
    )
    n_treat = math.ceil((root / mde) ** 2 - 1e-9)
    return ArmSizes(n_treat=n_treat, n_control=math.ceil(k * n_treat - 1e-9))


def arm_sizes(eligible: int, share: float) -> tuple[int, int]:
    """`(contacted, control)` customers when `share` of `eligible` are held back.

    The control group is `round_half_up(eligible × share)`, as `engine.stages.actions` draws it
    (`engine.uplift.power.arm_rows` rounds the same way); the rest are contacted.
    """
    _check_count("eligible", eligible)
    if not (math.isfinite(share) and 0.0 <= share <= 1.0):
        raise ValueError("The control group share must be between 0 and 1.")
    control = min(eligible, math.floor(eligible * share + 0.5))
    return eligible - control, control


def holdout_for_mde(
    eligible: int,
    base_rate: float | None,
    mde: float,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> HoldoutPlan:
    """The smallest control group, as a share of `eligible`, that sees a change of `mde` with `power`.

    Searches whole customers from 1 up to half of `eligible` (power only grows as the groups even
    out, so a binary search is exact). None with a reason when even half is not enough.
    """
    _check_alpha_power(alpha, power)
    _check_count("eligible", eligible)
    reason = _base_rate_reason(base_rate)
    if reason is not None or base_rate is None:
        return HoldoutPlan(None, None, None, reason or REASON_BASE_RATE_UNKNOWN)
    if not math.isfinite(mde) or mde == 0.0:
        return HoldoutPlan(None, None, None, REASON_NO_EFFECT)
    if not 0.0 <= base_rate + mde <= 1.0:
        return HoldoutPlan(None, None, None, REASON_NO_ROOM)
    largest = math.floor(eligible * MAX_HOLDOUT_SHARE)
    if largest < 1 or eligible - largest < 1:
        return HoldoutPlan(None, None, None, REASON_EMPTY_GROUP)

    def enough(control: int) -> bool:
        return _power(eligible - control, control, base_rate, mde, alpha) >= power

    if not enough(largest):
        return HoldoutPlan(None, None, None, REASON_HALF_NOT_ENOUGH)
    low, high = 1, largest
    while low < high:
        middle = (low + high) // 2
        if enough(middle):
            high = middle
        else:
            low = middle + 1
    return HoldoutPlan(share=low / eligible, n_treat=eligible - low, n_control=low)


def _money(low: float, high: float) -> MoneyRange:
    return MoneyRange(low=round(min(low, high), 2), high=round(max(low, high), 2))


def cost_of_holdout(
    n_holdout: int,
    uplift_per_contact: float | tuple[float, float] | None,
    value_per_conversion: float | None,
) -> CostEstimate:
    """What holding `n_holdout` customers back costs: the conversions the campaign would have brought.

    `uplift_per_contact` is the extra conversions per contacted customer (0.01: one in a hundred), as
    one number or a `(low, high)` range; its size is used, so a fall in churn counts like a rise in
    sales. None with a reason when it or the value per conversion is unknown.
    """
    _check_count("n_holdout", n_holdout)
    if value_per_conversion is None:
        return CostEstimate(None, REASON_NO_VALUE)
    if not (math.isfinite(value_per_conversion) and value_per_conversion >= 0.0):
        raise ValueError("The value per conversion must be 0 or more.")
    if uplift_per_contact is None:
        return CostEstimate(None, REASON_NO_EFFECT_SIZE)
    low, high = uplift_per_contact if isinstance(uplift_per_contact, tuple) else (uplift_per_contact,) * 2
    if not (math.isfinite(low) and math.isfinite(high)):
        raise ValueError("The extra conversions per contact must be a number.")
    sizes = sorted((abs(low), abs(high)))
    return CostEstimate(
        _money(n_holdout * sizes[0] * value_per_conversion, n_holdout * sizes[1] * value_per_conversion)
    )


def cost_of_explore(n_explore: int, contact_cost: float | None, offer_cost: float | None) -> CostEstimate:
    """What contacting `n_explore` randomly chosen customers outside the selection costs.

    From every contact paid and no offer taken, to every offer taken as well. Contacting nobody costs
    nothing, whatever the costs. None with a reason when a cost is missing.
    """
    _check_count("n_explore", n_explore)
    for cost in (contact_cost, offer_cost):
        if cost is not None and not (math.isfinite(cost) and cost >= 0.0):
            raise ValueError("Costs must be 0 or more.")
    if n_explore == 0:
        return CostEstimate(_money(0.0, 0.0))
    if contact_cost is None or offer_cost is None:
        return CostEstimate(None, REASON_NO_CONTACT_COST)
    return CostEstimate(_money(n_explore * contact_cost, n_explore * (contact_cost + offer_cost)))


# ---------------------------------------------------------------------------
# The preview: one planner call per control-group share (POST /measurement/power-preview)
# ---------------------------------------------------------------------------
class PowerPreviewRequest(BaseModel):
    """Counts and rates only: no customer data is ever sent to plan a test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: int = Field(ge=1, le=1_000_000_000, description="Customers the campaign could contact.")
    base_rate: float | None = Field(
        ge=0.0,
        le=1.0,
        description="Share of customers with the outcome without any campaign, 0 to 1; null when unknown.",
    )
    holdout_shares: tuple[float, ...] = Field(
        min_length=1,
        max_length=20,
        description="Control-group shares to compare, each 0 to 0.5 (0.05 holds back 5% of the eligible customers).",
    )
    explore_share: float = Field(
        default=0.0,
        ge=0.0,
        le=0.10,
        description="Share of the eligible customers contacted at random outside the selection (0 to 0.10).",
    )
    alpha: float = Field(
        default=DEFAULT_ALPHA, gt=0.0, lt=1.0, description="Two-sided significance; 0.05 is 95% confidence."
    )
    power: float = Field(
        default=DEFAULT_POWER,
        gt=0.0,
        lt=1.0,
        description="Chance of seeing a real effect; 0.8 is the usual choice.",
    )
    value_per_conversion: float | None = Field(
        default=None, ge=0.0, le=1e9, description="Rupees one extra conversion is worth; null when not known."
    )
    contact_cost: float | None = Field(default=None, ge=0.0, le=1e9, description="Rupees each contact costs.")
    offer_cost: float | None = Field(
        default=None, ge=0.0, le=1e9, description="Rupees an offer costs when it is taken."
    )
    direction: Direction = Field(
        default="either",
        description=(
            "Which way the campaign aims to move the outcome: `down` for one to prevent (churn, lapse), "
            "`up` for one to have more of, `either` (the default) for the larger of the two."
        ),
    )

    @model_validator(mode="after")
    def _shares(self) -> PowerPreviewRequest:
        for share in self.holdout_shares:
            if not 0.0 < share <= MAX_HOLDOUT_SHARE:
                raise ValueError("Each control-group share must be more than 0 and at most 0.5.")
        return self


class PowerPreviewPoint(BaseModel):
    """What one control-group share gives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    holdout_share: float = Field(description="The control-group share asked about.")
    n_treat: int = Field(description="Customers contacted.")
    n_control: int = Field(description="Customers held back.")
    mde_pp: float | None = Field(
        description="Smallest change in the request's direction (up, down, or the larger of the two) the test is sure to see, in percentage points; null when it cannot be said."
    )
    mde_amount: float | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Plan J M102: on a campaign planned on an amount, the smallest change in the average amount per "
            "customer the test is sure to see, in the amount's own unit (`mde_pp` is then null); absent on a "
            "yes/no outcome."
        ),
    )
    cost_of_holdout: MoneyRange | None = Field(
        description="What holding the control group back costs if the campaign changes the outcome by exactly `mde_pp`; null when a value is missing."
    )
    cost_of_explore: MoneyRange | None = Field(
        description="What contacting the random customers outside the selection costs; null when a cost is missing."
    )
    reason: str | None = Field(description="Why a number above is null; null when every number is there.")


class PowerPreview(BaseModel):
    """`POST /measurement/power-preview`: one point per control-group share, and what they rest on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    points: tuple[PowerPreviewPoint, ...] = Field(
        description="One per control-group share, in the order asked."
    )
    basis: str = Field(description="What the numbers rest on, in one plain paragraph.")


def _basis(request: PowerPreviewRequest) -> str:
    confidence = round((1.0 - request.alpha) * 100.0, 2)
    chance = round(request.power * 100.0, 2)
    aim = {
        "up": "a rise of the size shown",
        "down": "a fall of the size shown",
        "either": "a real change of the size shown, up or down",
    }[request.direction]
    return (
        f"A two-sided comparison of the contacted and the held-back customers at {confidence:g}% "
        f"confidence, and a chance of {chance:g}% of seeing {aim}. The base rate "
        "is the one entered. The cost of holding customers back assumes the campaign changes the "
        "outcome by exactly the size shown; contacting customers outside the selection costs every "
        "contact, plus the offer for those who take it. Every number is worked out from the counts "
        "entered; no customer data is used."
    )


def continuous_power_preview(
    request: PowerPreviewRequest, sd: float | None, *, rho2: float = 0.0
) -> PowerPreview:
    """`power_preview` for a campaign planned on an amount (Plan J M102, DEC-1312).

    Each point's `mde_amount` is `mde_continuous` at that share, with the plan's spread `sd` and the
    share `rho2` its registered covariate is expected to remove, so the slider shows the change the
    measurement (Welch's interval, adjusted when the plan names a covariate) can see. `base_rate` and
    `direction` are not used: an amount's change is the same size either way. `value_per_conversion`
    is read as the value of one unit of the amount for the cost of holding customers back.
    """
    contacted_at_random = math.floor(request.eligible * request.explore_share + 0.5)
    explore = cost_of_explore(contacted_at_random, request.contact_cost, request.offer_cost)
    points: list[PowerPreviewPoint] = []
    for share in request.holdout_shares:
        n_treat, n_control = arm_sizes(request.eligible, share)
        mde = mde_continuous(n_treat, n_control, sd, request.alpha, request.power, rho2=rho2)
        holdout = cost_of_holdout(n_control, mde.absolute, request.value_per_conversion)
        reasons = [mde.reason, holdout.reason if mde.reason is None else None, explore.reason]
        points.append(
            PowerPreviewPoint(
                holdout_share=share,
                n_treat=n_treat,
                n_control=n_control,
                mde_pp=None,
                mde_amount=None if mde.absolute is None else round(mde.absolute, 4),
                cost_of_holdout=holdout.amount,
                cost_of_explore=explore.amount,
                reason=" ".join(reason for reason in reasons if reason) or None,
            )
        )
    confidence = round((1.0 - request.alpha) * 100.0, 2)
    chance = round(request.power * 100.0, 2)
    adjusted = (
        f" The test plan's earlier amount is expected to explain {round(rho2 * 100.0, 2):g}% of the spread, "
        "and the adjusted comparison removes that much of it."
        if rho2 > 0.0
        else ""
    )
    basis = (
        f"A two-sided comparison of the average amount of the contacted and the held-back customers at "
        f"{confidence:g}% confidence, and a chance of {chance:g}% of seeing a change of the size shown, up "
        f"or down. The spread of the amount is the one in the test plan.{adjusted} The cost of holding "
        "customers back assumes the campaign changes each customer's amount by exactly the size shown, "
        "valued at what one unit is worth; contacting customers outside the selection costs every contact, "
        "plus the offer for those who take it. Every number is worked out from the counts entered; no "
        "customer data is used."
    )
    return PowerPreview(points=tuple(points), basis=basis)


def power_preview(request: PowerPreviewRequest) -> PowerPreview:
    """The planner at each control-group share of `request` (pure; the route only wraps it)."""
    contacted_at_random = math.floor(request.eligible * request.explore_share + 0.5)
    explore = cost_of_explore(contacted_at_random, request.contact_cost, request.offer_cost)
    points: list[PowerPreviewPoint] = []
    for share in request.holdout_shares:
        n_treat, n_control = arm_sizes(request.eligible, share)
        mde = mde_two_proportions(
            n_treat, n_control, request.base_rate, request.alpha, request.power, direction=request.direction
        )
        holdout = cost_of_holdout(n_control, mde.absolute, request.value_per_conversion)
        reasons = [mde.reason, holdout.reason if mde.reason is None else None, explore.reason]
        points.append(
            PowerPreviewPoint(
                holdout_share=share,
                n_treat=n_treat,
                n_control=n_control,
                mde_pp=None if mde.points is None else round(mde.points, 4),
                cost_of_holdout=holdout.amount,
                cost_of_explore=explore.amount,
                reason=" ".join(reason for reason in reasons if reason) or None,
            )
        )
    return PowerPreview(points=tuple(points), basis=_basis(request))


# ---------------------------------------------------------------------------
# Plan J M102: amounts (revenue), with the expected share an earlier amount explains
# ---------------------------------------------------------------------------
REASON_SPREAD_UNKNOWN: Final[str] = (
    "How much the amount varies from customer to customer is not known, so the smallest change the test "
    "can see cannot be worked out. Enter its spread, for example from last quarter's revenue."
)


@dataclass(frozen=True, slots=True)
class ContinuousMde:
    """The smallest change in the average amount a test of these groups can reliably see."""

    absolute: float | None
    """In the amount's own unit (rupees for revenue). None when it cannot be said."""
    n_treat: int
    n_control: int
    rho2: float
    """The share of the spread the adjustment was expected to remove (0 without one)."""
    reason: str | None = None


def _check_rho2(rho2: float) -> None:
    if not (math.isfinite(rho2) and 0.0 <= rho2 < 1.0):
        raise ValueError(
            "The expected share explained by the earlier amount (rho2) must be from 0 to below 1."
        )


def _spread_reason(sd: float | None) -> str | None:
    if sd is None:
        return REASON_SPREAD_UNKNOWN
    if not math.isfinite(sd) or sd <= 0.0:
        raise ValueError("The spread of the amount must be a number above 0.")
    return None


def _continuous_se(n_treat: int, n_control: int, sd: float, rho2: float) -> float:
    return sd * math.sqrt(1.0 - rho2) * math.sqrt(1.0 / n_treat + 1.0 / n_control)


def mde_continuous(
    n_treat: int,
    n_control: int,
    sd: float | None,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    *,
    rho2: float = 0.0,
) -> ContinuousMde:
    """The smallest change in the mean amount these groups see with `power`: `(z_a + z_b) · se`.

    Symmetric: a rise and a fall of the same size are equally easy to see on an amount. None with a
    reason when the spread is unknown or a group is empty.
    """
    _check_alpha_power(alpha, power)
    _check_count("n_treat", n_treat)
    _check_count("n_control", n_control)
    _check_rho2(rho2)
    reason = _spread_reason(sd)
    if reason is not None or sd is None:
        return ContinuousMde(None, n_treat, n_control, rho2, reason or REASON_SPREAD_UNKNOWN)
    if n_treat == 0 or n_control == 0:
        return ContinuousMde(None, n_treat, n_control, rho2, REASON_EMPTY_GROUP)
    z = _z_alpha(alpha) + _NORMAL.inv_cdf(power)
    return ContinuousMde(z * _continuous_se(n_treat, n_control, sd, rho2), n_treat, n_control, rho2)


def n_for_mde_continuous(
    sd: float | None,
    mde: float,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    *,
    rho2: float = 0.0,
    control_ratio: float = 1.0,
) -> ArmSizes:
    """Customers each group needs to see a change of `mde` in the mean amount with `power`.

    `n_t = (z_a + z_b)² · sd² · (1 − rho2) · (1 + 1/k) / mde²`, rounded up, and `n_c = k · n_t`, where
    `k = control_ratio` is control customers per contacted customer.
    """
    _check_alpha_power(alpha, power)
    _check_rho2(rho2)
    if not (math.isfinite(control_ratio) and control_ratio > 0.0):
        raise ValueError("control_ratio must be greater than 0.")
    reason = _spread_reason(sd)
    if reason is not None or sd is None:
        return ArmSizes(None, None, reason or REASON_SPREAD_UNKNOWN)
    if not math.isfinite(mde) or mde == 0.0:
        return ArmSizes(None, None, REASON_NO_EFFECT)
    z = _z_alpha(alpha) + _NORMAL.inv_cdf(power)
    n_treat = math.ceil((z * sd / mde) ** 2 * (1.0 - rho2) * (1.0 + 1.0 / control_ratio) - 1e-9)
    return ArmSizes(n_treat=n_treat, n_control=math.ceil(control_ratio * n_treat - 1e-9))


def achieved_power_continuous(
    n_treat: int,
    n_control: int,
    sd: float | None,
    effect: float,
    alpha: float = DEFAULT_ALPHA,
    *,
    rho2: float = 0.0,
) -> PowerEstimate:
    """The chance a test with these groups sees a true change of `effect` in the mean amount: `Φ(|d|/se − z_a)`."""
    _check_alpha_power(alpha)
    _check_count("n_treat", n_treat)
    _check_count("n_control", n_control)
    _check_rho2(rho2)
    reason = _spread_reason(sd)
    if reason is not None or sd is None:
        return PowerEstimate(power=None, reason=reason or REASON_SPREAD_UNKNOWN)
    if n_treat == 0 or n_control == 0:
        return PowerEstimate(power=None, reason=REASON_EMPTY_GROUP)
    if not math.isfinite(effect) or effect == 0.0:
        return PowerEstimate(power=None, reason=REASON_NO_EFFECT)
    se = _continuous_se(n_treat, n_control, sd, rho2)
    return PowerEstimate(power=_NORMAL.cdf(abs(effect) / se - _z_alpha(alpha)))
