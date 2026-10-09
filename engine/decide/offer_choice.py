"""Choose the offer: which treatment, if any, each customer gets (Plan J M100, DEC-1310).

A model of several treatments predicts, for every customer, what each offer would change
(`engine.uplift.learners.MultiArmUpliftModel.predict_arms`; a scoring run writes it to
`scores.parquet` as `uplift_arm_<k>` and `p_treated_arm_<k>`). This module turns those predictions and
each offer's money into one choice per customer. It is a pure function of arrays: no storage, no
configuration file, no clock. Part B of M100 wires it into the scoring run and the treat list, where
the eligibility mask is the customer's contactability per offer (M99's channel consent).

**The rule, per customer** (plan J §4 M100):

1. An offer is a *candidate* when the customer is eligible for it, is not a sleeping dog for it (its
   predicted effect is at or below the sleeping-dog cut: contacting them with it makes things worse),
   and its net value is known and at least `min_roi` x its cost (0 by default: it pays for itself).
2. The chosen offer is the candidate with the highest net value (ties to the earlier offer). With no
   candidate the customer gets **no offer**, and `reason` says why: nothing eligible, a sleeping dog
   for every eligible offer, or every eligible offer below cost.
3. The **runner-up** is the next-best offer the customer could be given (eligible, not a sleeping dog,
   net value known), whatever its sign: for a customer with an offer it is the second-best one, for a
   customer with none it is the best one there was, so a treat list can say what the next choice was
   worth.
4. **Budget.** With a total budget, every customer keeps their chosen offer (an offer is never switched
   to a cheaper one to fit) and customers are taken greedily by net value per rupee of their offer's
   cost, highest first (net value, then the row order, break ties), while the running cost stays within
   the budget; a customer whose offer does not fit is skipped and the walk goes on, so a cheaper offer
   further down can still fit. A customer left out gets no offer, reason `over_budget`. A zero-cost
   offer is taken first, at no cost.

Net value per offer is M97's (DEC-1307): `uplift x value x margin x horizon - offer cost x p_treated -
contact cost`, from :func:`arm_net_values`, which takes each offer's costs from the caller
(`engine.pilot.roi.ValueCosts` per offer; M99's catalogue supplies them in Part B). Everything is
linear in the customers except the budget walk's one sort.

`numpy` is imported inside function bodies so `import engine` stays fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    import numpy.typing as npt

    from engine.pilot.roi import ValueCosts
    from engine.uplift.config import UpliftPolicyConfig

    FloatArray = npt.NDArray[np.float64]
    IntArray = npt.NDArray[np.int_]
    BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "NO_OFFER",
    "OFFER_REASONS",
    "ArmMoney",
    "OfferChoice",
    "OfferReason",
    "arm_net_values",
    "choose_offers",
]

NO_OFFER: Final[int] = 0
"""The arm code of "no offer" (the control level); offers are 1..K in `uplift.treatment_levels` order."""

OfferReason = Literal["offer", "no_eligible_offer", "sleeping_dog", "below_cost", "over_budget"]

OFFER_REASONS: Final[tuple[OfferReason, ...]] = (
    "offer",
    "no_eligible_offer",
    "sleeping_dog",
    "below_cost",
    "over_budget",
)
"""Why each customer got what they got; `reason_code` indexes this tuple."""


@dataclass(frozen=True)
class ArmMoney:
    """Every offer's net value and cost per customer: `rows x K`, offer `k` in column `k-1`."""

    net_value: FloatArray
    cost: FloatArray


@dataclass(frozen=True)
class OfferChoice:
    """One choice per customer (see the module docstring); every array is aligned with the input rows."""

    arm: IntArray
    """The offer given: 1..K, or :data:`NO_OFFER`."""
    preferred_arm: IntArray
    """The best candidate before the budget (equal to `arm` without a budget); :data:`NO_OFFER` when none."""
    net_value: FloatArray
    """Net value of the offer given; NaN for no offer."""
    cost: FloatArray
    """Cost of the offer given; 0 for no offer."""
    runner_up_arm: IntArray
    """The next-best offer the customer could be given; :data:`NO_OFFER` when there is none."""
    runner_up_net_value: FloatArray
    """Its net value; NaN when there is none."""
    reason_code: IntArray
    """Index into :data:`OFFER_REASONS`."""
    budget: float | None
    spent: float
    """Total cost of the offers given."""

    @property
    def reason(self) -> list[OfferReason]:
        """`reason_code` as words, one per customer."""
        return [OFFER_REASONS[code] for code in self.reason_code.tolist()]

    def offered(self) -> BoolArray:
        """Customers given an offer."""
        import numpy as np

        mask: BoolArray = np.asarray(self.arm != NO_OFFER, dtype=np.bool_)
        return mask


def _matrix(values: npt.ArrayLike, name: str, rows: int | None = None, arms: int | None = None) -> FloatArray:
    """`values` as a two-dimensional float matrix of the expected shape; `ValueError` otherwise."""
    import numpy as np

    matrix = np.asarray(values, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        raise ValueError(f"{name} must be a rows x offers matrix with at least one offer.")
    if rows is not None and matrix.shape[0] != rows:
        raise ValueError(f"{name} has {matrix.shape[0]} rows, expected {rows}.")
    if arms is not None and matrix.shape[1] != arms:
        raise ValueError(f"{name} has {matrix.shape[1]} offers, expected {arms}.")
    return matrix


def _mask(values: npt.ArrayLike | None, name: str, shape: tuple[int, int], default: bool) -> BoolArray:
    import numpy as np

    if values is None:
        return np.full(shape, default, dtype=np.bool_)
    mask = np.asarray(values)
    if mask.shape != shape:
        raise ValueError(f"{name} must have the shape {shape}, not {mask.shape}.")
    if mask.dtype != np.bool_:
        raise ValueError(f"{name} must be a boolean mask.")
    return mask


def arm_net_values(
    uplift: npt.ArrayLike,
    policy: UpliftPolicyConfig,
    *,
    arm_costs: Sequence[ValueCosts],
    p_treated: npt.ArrayLike | None = None,
    values: npt.ArrayLike | None = None,
) -> ArmMoney:
    """Each offer's M97 net value and cost per customer (DEC-1307 (a)), with that offer's own costs.

    `uplift` and `p_treated` are `rows x K`; `arm_costs[k-1]` is offer `k`'s contact and offer cost
    (`cost_per_contact` in `policy`, when set, is every offer's contact cost, as in M97). With
    `policy.value_column` and `values` the value path: `uplift x value x margin x horizon - offer cost x
    p_treated - contact cost` (a missing value counts as zero). Without them each conversion is worth
    `value_per_conversion x margin x horizon`, and the costs are the same on both paths. `ValueError`
    when no value is configured at all, because an offer cannot be chosen by money nobody stated, and
    when an offer has an offer cost but `p_treated` is not given: the offer is paid by the customers
    who take it, and without `p_treated` that cost would be charged in full on one path and not at all
    on the other (M97's `customer_net_values`), so the same customer could get opposite choices.
    """
    import numpy as np

    from engine.uplift.policy import customer_net_values

    lift = _matrix(uplift, "uplift")
    rows, arms = lift.shape
    if len(arm_costs) != arms:
        raise ValueError(f"arm_costs names {len(arm_costs)} offers, expected {arms}.")
    taken = None if p_treated is None else _matrix(p_treated, "p_treated", rows, arms)
    if taken is None and any(costs.offer_cost for costs in arm_costs):
        raise ValueError(
            "An offer with an offer cost needs p_treated, each customer's chance of taking it: "
            "the offer cost is offer_cost x p_treated."
        )
    value_path = policy.value_column is not None and values is not None
    if not value_path and policy.value_per_conversion is None:
        raise ValueError(
            "Set uplift.policy.value_per_conversion or a value column: an offer is chosen by its net value."
        )
    net = np.empty((rows, arms), dtype=np.float64)
    cost = np.empty((rows, arms), dtype=np.float64)
    for k in range(arms):
        costs = arm_costs[k]
        contact = policy.cost_per_contact if policy.cost_per_contact is not None else costs.contact_cost
        cost[:, k] = contact + (0.0 if taken is None else costs.offer_cost * taken[:, k])
        if value_path:
            money = customer_net_values(
                lift[:, k],
                policy,
                values=None if values is None else np.asarray(values, dtype=np.float64),
                p_treated=None if taken is None else taken[:, k],
                value_costs=costs,
            )
            if money.net_value is None:  # pragma: no cover - the value path always has net values
                raise ValueError("The value path gave no net value.")
            net[:, k] = money.net_value
            if money.cost is not None:
                cost[:, k] = money.cost
        else:
            margin = 1.0 if policy.margin_pct is None else policy.margin_pct / 100.0
            horizon = 1 if policy.horizon_months is None else policy.horizon_months
            unit = float(policy.value_per_conversion or 0.0) * margin * horizon
            net[:, k] = lift[:, k] * unit - cost[:, k]
    return ArmMoney(net_value=net, cost=cost)


def choose_offers(
    net_value: npt.ArrayLike,
    cost: npt.ArrayLike,
    *,
    sleeping_dog: npt.ArrayLike,
    eligible: npt.ArrayLike | None = None,
    budget: float | None = None,
    min_roi: float = 0.0,
    max_offers: int | None = None,
) -> OfferChoice:
    """The offer per customer, or no offer, under an optional total budget (see the module docstring).

    `net_value`, `cost`, `sleeping_dog` and `eligible` are `rows x K`, offer `k` in column `k-1`;
    `eligible` defaults to every offer for everyone (Part B passes contactability). A NaN net value is
    never chosen. `budget` is in the same unit as `cost`; `min_roi` is M97's (DEC-1307 (c)).
    `max_offers` (Plan J M100 part B: the policy's `budget_contacts`) caps how many customers get an
    offer, taken in the same greedy order as the budget; a customer left out is `over_budget` too.
    """
    import numpy as np

    net = _matrix(net_value, "net_value")
    rows, arms = net.shape
    costs = _matrix(cost, "cost", rows, arms)
    if (costs < 0).any() or not np.isfinite(costs).all():
        raise ValueError("cost must be finite and not negative.")
    dogs = _mask(sleeping_dog, "sleeping_dog", (rows, arms), default=False)
    allowed = _mask(eligible, "eligible", (rows, arms), default=True)
    if budget is not None and budget < 0:
        raise ValueError("budget cannot be negative.")
    if max_offers is not None and max_offers < 0:
        raise ValueError("max_offers cannot be negative.")
    if min_roi < 0:
        raise ValueError("min_roi cannot be negative.")

    known = np.isfinite(net)
    available = allowed & ~dogs & known
    candidate = available & (net >= min_roi * costs)
    ranked = np.where(candidate, net, -np.inf)
    best = np.argmax(ranked, axis=1)
    rows_index = np.arange(rows)
    has_offer = candidate[rows_index, best]
    preferred = np.where(has_offer, best + 1, NO_OFFER).astype(np.int_)

    # Runner-up: among the offers the customer could be given (any sign), excluding the one chosen.
    pool = np.where(available, net, -np.inf)
    pool[rows_index[has_offer], best[has_offer]] = -np.inf
    second = np.argmax(pool, axis=1)
    has_second = np.isfinite(pool[rows_index, second])
    runner_up = np.where(has_second, second + 1, NO_OFFER).astype(np.int_)
    runner_up_value = np.where(has_second, pool[rows_index, second], np.nan)

    # Why no offer: nothing eligible; else a sleeping dog for every eligible offer; else below cost
    # (which includes an eligible offer whose net value is not known: it cannot be shown to pay).
    reason = np.full(rows, OFFER_REASONS.index("offer"), dtype=np.int_)
    any_allowed = allowed.any(axis=1)
    any_not_dog = (allowed & ~dogs).any(axis=1)
    reason[~has_offer] = OFFER_REASONS.index("below_cost")
    reason[~has_offer & any_allowed & ~any_not_dog] = OFFER_REASONS.index("sleeping_dog")
    reason[~has_offer & ~any_allowed] = OFFER_REASONS.index("no_eligible_offer")

    chosen_cost = np.where(has_offer, costs[rows_index, best], 0.0)
    chosen_value = np.where(has_offer, net[rows_index, best], np.nan)
    arm = preferred.copy()
    if budget is not None or max_offers is not None:
        taken = _within_budget(chosen_value, chosen_cost, has_offer, budget, max_offers)
        dropped = has_offer & ~taken
        arm[dropped] = NO_OFFER
        reason[dropped] = OFFER_REASONS.index("over_budget")
        chosen_cost = np.where(taken, chosen_cost, 0.0)
        chosen_value = np.where(taken, chosen_value, np.nan)
    return OfferChoice(
        arm=arm,
        preferred_arm=preferred,
        net_value=chosen_value,
        cost=chosen_cost,
        runner_up_arm=runner_up,
        runner_up_net_value=runner_up_value,
        reason_code=reason,
        budget=budget,
        spent=float(chosen_cost.sum()),
    )


def _within_budget(
    value: FloatArray,
    cost: FloatArray,
    offered: BoolArray,
    budget: float | None,
    max_offers: int | None = None,
) -> BoolArray:
    """The greedy walk: by net value per rupee (zero cost first), then net value, then row order.

    Without a `budget` only `max_offers` limits it: the first `max_offers` customers in that order."""
    import numpy as np

    candidates = np.flatnonzero(offered)
    per_rupee = np.where(
        cost[candidates] > 0.0,
        value[candidates] / np.where(cost[candidates] > 0.0, cost[candidates], 1.0),
        np.inf,
    )
    # np.lexsort sorts by the last key first: ratio (desc), then value (desc), then row order (asc).
    order = candidates[np.lexsort((candidates, -value[candidates], -per_rupee))]
    taken = np.zeros(len(value), dtype=np.bool_)
    if budget is None:
        taken[order[: max_offers if max_offers is not None else len(order)]] = True
        return taken
    if max_offers is not None:
        # The walk still skips an offer that does not fit; the count is of the offers given.
        return _budget_walk(order, cost, budget, len(value), max_offers)
    ordered_cost = cost[order]
    spent = 0.0
    # One pass; a customer whose offer does not fit is skipped and the walk goes on.
    for position, each in zip(order.tolist(), ordered_cost.tolist(), strict=True):
        if spent + each <= budget + 1e-9:
            spent += each
            taken[position] = True
    return taken


def _budget_walk(order: IntArray, cost: FloatArray, budget: float, rows: int, max_offers: int) -> BoolArray:
    """The walk with both limits: by `order`, skipping what does not fit, until `max_offers` are given."""
    import numpy as np

    taken = np.zeros(rows, dtype=np.bool_)
    spent = 0.0
    given = 0
    for position, each in zip(order.tolist(), cost[order].tolist(), strict=True):
        if given >= max_offers:
            break
        if spent + each <= budget + 1e-9:
            spent += each
            given += 1
            taken[position] = True
    return taken
