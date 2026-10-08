"""The targeting recommendation: "treat the top N by uplift within budget" (plan B §1.4, Stage C).

An uplift model's ranking only becomes a decision once someone says how many customers to contact.
This module turns the ranking and the `uplift.policy` settings into that number, `N`, and into the
set of customers it names.

**Who can be chosen.** Only persuadables, and only the rows the caller marks eligible (on a scoring
run: neither suppressed nor held out as control). Sure things convert anyway, lost causes do not
convert either way, and **sleeping dogs are never chosen, whatever the configuration** - contacting
them is predicted to *lose* conversions, and no budget, cost or value setting may buy that. The rule
is enforced by selecting on the persuadable segment alone and checked again on the way out.

**How many.** Candidates are ranked by predicted uplift, highest first. Rows with the same
predicted uplift are ordered by the caller's `tiebreak` key when one is given, then by input order
(a stable sort, so the answer is deterministic either way). A scoring run passes a run-seeded
per-customer hash as `tiebreak` (see `engine.uplift.actions`): a coarse model can give thousands of
customers the same uplift, and when the budget cuts through such a block, input order would let the
file's sort order - often correlated with the outcome - decide who is contacted. `N` is the longest
prefix of that ranking that

1. stays within `budget_contacts`, when a budget is set, and
2. when *both* `value_per_conversion` and `cost_per_contact` are set, stops before the first row
   whose expected gain `uplift × value` is below the cost of contacting it. The ranking is
   descending, so every row after that one is below cost too.

`stop_reason` says why `N` is not larger: `no_persuadables` when there is no eligible persuadable,
`all_persuadables` when every one was chosen, `value_below_cost` when the next row would not pay for
itself (also when the budget happens to end at the same row: a bigger budget would not change `N`),
and `budget` otherwise.

**Expected incremental conversions.** The model's own sum of predicted uplift over the chosen rows
is reported as `predicted_incremental_conversions`, but a model is not evidence for itself. The
number the page leads with is `N × observed uplift in the top d/rows share of the hold-out` (DEC-604), with
the bootstrap interval of that observed uplift scaled by `N`. `d` is the **ranking depth** the
selection reaches: the position, in the ranking of *every* row, of the last row chosen. On a
training run's hold-out every row is eligible and `d = N`. On a scoring run control and suppressed
rows are ranked but never chosen, so the `N` chosen rows reach further down the ranking than the
top `N` - with a 10% control group and 40% suppressed, twice as far - and the hold-out must be asked
about the share the selection actually covers, not a smaller, better-ranked one (which overstated
the expectation by about a quarter on a steep uplift curve). The flow supplies the lookup as a
callable built on `engine.uplift.metrics.bootstrap_uplift_at`; with no callable, or when the
hold-out cannot measure that share, the field is null and the page shows "—". Choosing nobody is
exactly zero incremental conversions, so `N = 0` reports `0` with a zero-width interval rather than
asking the hold-out.

**Money.** `expected_cost = N × cost_per_contact`; `expected_value = expected incremental
conversions × value_per_conversion`; `expected_net_value = value − cost`. Each is null unless every
number it is made of exists: no field is ever filled with a placeholder.

**The budget curve** (`profit_curve`, DEC-1200). The same rules at every budget: for each number of
contacts from 0 to the most the rules allow (every eligible persuadable that pays for its contact),
the point is exactly what the recommendation would say with `budget_contacts` set to it - the same
ranking, the same ranking depth, the same observed hold-out uplift and the same money arithmetic -
so the point at the configured budget *is* the recommendation. The optimum is searched over every
contact count with the hold-out's point estimate; the plotted points, the configured one and the
optimum carry the recommendation's own bootstrap interval, turned into a low/high net value. With
no hold-out, or without both a cost and a value, those fields are null and the curve says why.

`numpy` is imported inside the function bodies, never at module level, so `import engine` stays
fast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, Literal, Protocol

from engine.uplift.contracts import (
    ConfidenceValue,
    PolicyRecommendation,
    PolicyStopReason,
    ProfitCurve,
    ProfitPoint,
    Segment,
)
from engine.uplift.segments import _finite_vector, _segment_vector
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np

    from engine.uplift.config import UpliftPolicyConfig

__all__ = [
    "DEFAULT_CURVE_POINTS",
    "ObservedUplift",
    "below_cost",
    "choose_contacts",
    "customer_net_values",
    "profit_curve",
    "rank_positions",
    "ranking",
    "recommend_policy",
]

_LOGGER = get_logger(__name__)

DEFAULT_CURVE_POINTS: Final[int] = 41
"""Evenly spaced contact counts the budget curve plots, 0 and the maximum included (every 2.5 %)."""

BANDS_NOTE: Final[str] = (
    "The band is the 95% bootstrap interval of the uplift observed on the hold-out among the same "
    "share of the ranking - the interval the recommendation itself quotes - turned into money. It "
    "holds for each point on its own, not for the whole curve at once."
)
NO_HOLDOUT_NOTE: Final[str] = (
    "No expected value and no band: there is no measured hold-out to take the observed uplift from."
)
NO_MONEY_NOTE: Final[str] = "Set both a cost per contact and a value per conversion to find the best budget."
NO_INTERVAL_NOTE: Final[str] = (
    "No band: a band needs a cost per contact, a value per conversion and a measured interval."
)


class ObservedUplift(Protocol):
    """The hold-out's observed uplift by top share: `engine.uplift.metrics.HoldoutUplift`, or a fake."""

    def intervals(self, fractions: Sequence[float]) -> Sequence[ConfidenceValue | None]:
        """What `recommend_policy`'s `observed_top_share(f)` returns, for each `f`."""
        ...

    def points(self, fractions: np.ndarray) -> np.ndarray:
        """The point estimate of each share, NaN where the hold-out cannot measure it."""
        ...


def customer_net_values(
    uplift: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    net_value: np.ndarray | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Net value per customer and contact cost per customer (Plan J M97, DEC-1307).

    Returns `(net_values, costs)` aligned to `uplift`, or `(None, None)` when money inputs are missing.
    Net value = uplift × value × margin − offer_cost × p_treated − contact_cost.
    """
    import numpy as np

    from engine.pilot.roi import lookup_value_costs

    lift = _finite_vector(uplift, "uplift")
    if net_value is not None:
        nv = _finite_vector(net_value, "net_value")
        if len(nv) != len(lift):
            raise ValueError(f"net_value has length {len(nv)}, expected {len(lift)}.")
        costs = (
            np.full(len(lift), policy.cost_per_contact, dtype=np.float64)
            if policy.cost_per_contact is not None
            else None
        )
        return nv, costs

    has_value_input = (
        values is not None or policy.value_column is not None or policy.value_per_conversion is not None
    )
    has_cost_input = policy.cost_per_contact is not None or (
        policy.value_column is not None and values is not None
    )
    if not (has_value_input and has_cost_input):
        return None, None

    costs_cfg = lookup_value_costs()
    contact_cost = policy.cost_per_contact if policy.cost_per_contact is not None else costs_cfg.contact_cost
    offer_cost = costs_cfg.offer_cost
    pt = (
        np.asarray(p_treated, dtype=np.float64)
        if p_treated is not None
        else np.zeros(len(lift), dtype=np.float64)
    )
    costs = offer_cost * pt + contact_cost

    if values is not None:
        val_arr = np.asarray(values, dtype=np.float64)
    elif policy.value_per_conversion is not None:
        val_arr = np.full(len(lift), policy.value_per_conversion, dtype=np.float64)
    else:
        return None, costs

    margin = policy.margin_pct / 100.0 if policy.margin_pct is not None else 1.0
    horizon = policy.horizon_months or 1
    gross = lift * val_arr * margin * horizon
    net = gross - costs
    return net, costs


def below_cost(
    uplift: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    net_value: np.ndarray | None = None,
    cost: np.ndarray | None = None,
) -> np.ndarray:
    """Which rows would not pay for their contact: `net_value < min_roi × cost` (or `uplift × value < cost`).

    All `False` unless value and cost are configured - without both there is no money comparison to
    make, and no row is cut for it.
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    if net_value is not None:
        nv = np.asarray(net_value, dtype=np.float64)
        c = (
            np.asarray(cost, dtype=np.float64)
            if cost is not None
            else (
                np.full(len(nv), policy.cost_per_contact, dtype=np.float64)
                if policy.cost_per_contact is not None
                else np.zeros(len(nv), dtype=np.float64)
            )
        )
        threshold = c * policy.min_roi if policy.min_roi is not None else 0.0
        return np.asarray(nv < threshold, dtype=bool)

    value, cost_val = policy.value_per_conversion, policy.cost_per_contact
    if value is None or cost_val is None:
        return np.zeros(len(lift), dtype=bool)
    net = lift * value - cost_val
    threshold = cost_val * policy.min_roi if policy.min_roi is not None else 0.0
    return np.asarray(net < threshold, dtype=bool)


def ranking(
    uplift: np.ndarray,
    tiebreak: np.ndarray | None = None,
    *,
    net_value: np.ndarray | None = None,
) -> np.ndarray:
    """Row indices, highest predicted net value (or uplift) first; ties by `tiebreak` ascending, then input order.

    The one ordering every targeting decision uses: who is chosen (`choose_contacts`), how deep the
    choice reaches (`recommend_policy`) and which held-out rows would have been chosen
    (`engine.uplift.actions`). Using one function is what keeps those three consistent.
    """
    import numpy as np

    score = (
        _finite_vector(net_value, "net_value") if net_value is not None else _finite_vector(uplift, "uplift")
    )
    if tiebreak is None:
        order: np.ndarray = np.argsort(-score, kind="mergesort")
        return order
    keys = _tiebreak_vector(tiebreak, len(score))
    # `np.lexsort` sorts by its LAST key first and is stable, so remaining ties keep input order.
    return np.asarray(np.lexsort((keys, -score)), dtype=np.int64)


def rank_positions(
    uplift: np.ndarray,
    tiebreak: np.ndarray | None = None,
    *,
    net_value: np.ndarray | None = None,
) -> np.ndarray:
    """Each row's 0-based position in :func:`ranking` (0 = the highest predicted net value or uplift)."""
    import numpy as np

    order = ranking(uplift, tiebreak, net_value=net_value)
    positions = np.empty(len(order), dtype=np.int64)
    positions[order] = np.arange(len(order), dtype=np.int64)
    return positions


def choose_contacts(
    uplift: np.ndarray,
    segments: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    eligible: np.ndarray | None = None,
    tiebreak: np.ndarray | None = None,
    net_value: np.ndarray | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
) -> tuple[np.ndarray, PolicyStopReason]:
    """The rows to treat (a boolean mask aligned to `uplift`) and why there are not more of them.

    Only eligible persuadables are candidates; the rest of the rules are in the module docstring.
    `eligible` defaults to every row; `tiebreak` (one number per row) orders rows of equal uplift,
    and without it they keep input order.
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    labels = _segment_vector(segments)
    allowed = _eligible_vector(eligible, len(lift))
    if len(labels) != len(lift):
        raise ValueError(
            f"uplift has {len(lift)} rows but segments has {len(labels)}; they must describe the same rows."
        )

    selected = np.zeros(len(lift), dtype=bool)
    is_candidate = (labels == Segment.PERSUADABLE.value) & allowed
    if not bool(is_candidate.any()):
        return selected, PolicyStopReason.NO_PERSUADABLES

    net_val, row_costs = customer_net_values(
        lift, policy, net_value=net_value, values=values, p_treated=p_treated
    )
    order = ranking(lift, tiebreak, net_value=net_val)
    ranked = order[is_candidate[order]]
    nv_ranked = net_val[ranked] if net_val is not None else None
    c_ranked = row_costs[ranked] if row_costs is not None else None
    cut = below_cost(lift[ranked], policy, net_value=nv_ranked, cost=c_ranked)
    paying = int(np.argmax(cut)) if cut.any() else len(ranked)
    budget = policy.budget_contacts
    take = min(len(ranked), paying, len(ranked) if budget is None else budget)

    if take == len(ranked):
        reason = PolicyStopReason.ALL_PERSUADABLES
    elif take == paying:
        reason = PolicyStopReason.VALUE_BELOW_COST
    else:
        reason = PolicyStopReason.BUDGET
    selected[ranked[:take]] = True

    sleeping = labels == Segment.SLEEPING_DOG.value
    if bool((selected & sleeping).any()):  # unreachable by construction; the rule is too important to trust
        raise RuntimeError("The targeting policy selected a sleeping dog; refusing to recommend it.")
    return selected, reason


def recommend_policy(
    uplift: np.ndarray,
    segments: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    run_id: str,
    computed_on: Literal["test", "scored"],
    causal: bool,
    observed_top_share: Callable[[float], ConfidenceValue | None] | None = None,
    eligible: np.ndarray | None = None,
    tiebreak: np.ndarray | None = None,
    net_value: np.ndarray | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
) -> tuple[PolicyRecommendation, np.ndarray]:
    """`policy_recommendation.json` and the mask of rows it recommends treating.

    `observed_top_share(fraction)` returns the uplift observed on the hold-out among the top
    `fraction` of it, with its interval, or `None` when it cannot be measured; it is asked about the
    ranking depth the selection reaches, not about `N / rows` (see the module docstring). `eligible`
    and `tiebreak` are passed to :func:`choose_contacts`.
    """
    started = time.perf_counter()
    lift = _finite_vector(uplift, "uplift")
    labels = _segment_vector(segments)
    allowed = _eligible_vector(eligible, len(lift))
    net_val, row_costs = customer_net_values(
        lift, policy, net_value=net_value, values=values, p_treated=p_treated
    )
    selected, reason = choose_contacts(
        lift,
        labels,
        policy,
        eligible=allowed,
        tiebreak=tiebreak,
        net_value=net_val,
        values=values,
        p_treated=p_treated,
    )

    rows = len(lift)
    contacts = int(selected.sum())
    candidates = int(((labels == Segment.PERSUADABLE.value) & allowed).sum())
    depth = int(rank_positions(lift, tiebreak, net_value=net_val)[selected].max()) + 1 if contacts else 0
    expected = _expected_conversions(contacts, depth, rows, observed_top_share)

    if contacts == 0:
        cost = 0.0 if (row_costs is not None or policy.cost_per_contact is not None) else None
    elif row_costs is not None:
        cost = float(row_costs[selected].sum())
    elif policy.cost_per_contact is not None:
        cost = contacts * policy.cost_per_contact
    else:
        cost = None

    is_val_weighted = policy.value_column is not None and (
        getattr(observed_top_share, "is_value_weighted", False)
        or getattr(getattr(observed_top_share, "__self__", None), "is_value_weighted", False)
    )
    margin = policy.margin_pct / 100.0 if policy.margin_pct is not None else 1.0
    horizon = policy.horizon_months or 1
    mult: float | None = None
    if is_val_weighted:
        mult = margin * horizon
    elif values is not None and policy.value_column is not None:
        mean_v = float(values[selected].mean()) if contacts else 0.0
        mult = mean_v * margin * horizon
    elif policy.value_per_conversion is not None:
        mult = policy.value_per_conversion

    if expected is None or mult is None:
        value = None
        net = None
        net_low = None
        net_high = None
    else:
        value = expected.value * mult
        net = None if cost is None else value - cost
        net_low = None if expected.ci_low is None or cost is None else expected.ci_low * mult - cost
        net_high = None if expected.ci_high is None or cost is None else expected.ci_high * mult - cost

    recommendation = PolicyRecommendation(
        run_id=run_id,
        computed_on=computed_on,
        rows=rows,
        eligible_persuadables=candidates,
        contacts_recommended=contacts,
        stop_reason=reason,
        budget_contacts=policy.budget_contacts,
        predicted_incremental_conversions=float(lift[selected].sum()),
        expected_incremental_conversions=expected,
        cost_per_contact=policy.cost_per_contact,
        value_per_conversion=policy.value_per_conversion,
        expected_cost=cost,
        expected_value=value,
        expected_net_value=net,
        net_value_low=net_low,
        net_value_high=net_high,
        causal=causal,
    )
    _LOGGER.info(
        "uplift_policy eligible_persuadables=%d contacts_recommended=%d ranking_depth=%d stop_reason=%s",
        candidates,
        contacts,
        depth,
        reason.value,
    )
    log_stage(_LOGGER, "uplift_policy", rows=rows, seconds=time.perf_counter() - started)
    return recommendation, selected


def profit_curve(
    uplift: np.ndarray,
    segments: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    run_id: str,
    computed_on: Literal["test", "scored"],
    causal: bool,
    observed: ObservedUplift | None = None,
    eligible: np.ndarray | None = None,
    tiebreak: np.ndarray | None = None,
    points: int = DEFAULT_CURVE_POINTS,
    overridden: bool = False,
    net_value: np.ndarray | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
) -> ProfitCurve:
    """Expected conversions, cost, value, net value and ROI against the number of customers contacted.

    The arguments are :func:`recommend_policy`'s, with `observed` in place of `observed_top_share`
    (its `intervals` must answer what that callable would, share for share). `policy.budget_contacts`
    only places the configured point: the curve runs to every contact the other rules allow.
    `overridden` is recorded as given - only the caller knows whether the cost and value are the run's.
    """
    import numpy as np

    if points < 2:
        raise ValueError(f"a curve needs at least 2 points, got {points}.")
    started = time.perf_counter()
    lift = _finite_vector(uplift, "uplift")
    labels = _segment_vector(segments)
    allowed = _eligible_vector(eligible, len(lift))
    net_val, row_costs = customer_net_values(
        lift, policy, net_value=net_value, values=values, p_treated=p_treated
    )
    selected, configured_reason = choose_contacts(
        lift,
        labels,
        policy,
        eligible=allowed,
        tiebreak=tiebreak,
        net_value=net_val,
        values=values,
        p_treated=p_treated,
    )

    rows = len(lift)
    order = ranking(lift, tiebreak, net_value=net_val)
    is_candidate = (labels == Segment.PERSUADABLE.value) & allowed
    candidate_positions = np.flatnonzero(is_candidate[order])  # ranking positions, best first
    ranked = order[candidate_positions]
    nv_ranked = net_val[ranked] if net_val is not None else None
    c_ranked = row_costs[ranked] if row_costs is not None else None
    cut = below_cost(lift[ranked], policy, net_value=nv_ranked, cost=c_ranked)
    reachable = int(np.argmax(cut)) if cut.any() else len(ranked)
    if not len(ranked):
        max_reason = PolicyStopReason.NO_PERSUADABLES
    elif reachable < len(ranked):
        max_reason = PolicyStopReason.VALUE_BELOW_COST
    else:
        max_reason = PolicyStopReason.ALL_PERSUADABLES
    configured = int(selected.sum())
    if configured > reachable or not bool(selected[ranked[:configured]].all()):  # unreachable by construction
        raise RuntimeError("The budget curve and the targeting policy disagree on who is chosen.")

    # `depths[c - 1]` is how far down the ranking of every row the first `c` contacts reach.
    depths = candidate_positions[:reachable] + 1
    is_val_weighted = policy.value_column is not None and getattr(observed, "is_value_weighted", False)
    best, optimum_note = _best_contacts(
        depths,
        rows,
        policy,
        observed,
        ranked=ranked,
        values=values,
        row_costs=row_costs,
        is_value_weighted=is_val_weighted,
    )
    counts = sorted(
        {round(float(c)) for c in np.linspace(0, reachable, points)}
        | {configured}
        | (set() if best is None else {best})
    )
    fractions = [int(depths[c - 1]) / rows for c in counts if c]
    looked_up = iter(list(observed.intervals(fractions)) if observed is not None and fractions else [])
    curve: list[ProfitPoint] = []
    margin = policy.margin_pct / 100.0 if policy.margin_pct is not None else 1.0
    horizon = policy.horizon_months or 1
    for contacts in counts:
        chosen = np.sort(ranked[:contacts])  # index order, as `lift[selected]` sums in recommend_policy
        expected = None if observed is None else _scaled(contacts, next(looked_up) if contacts else None)
        if contacts == 0:
            c = 0.0 if (row_costs is not None or policy.cost_per_contact is not None) else None
        elif row_costs is not None:
            c = float(row_costs[chosen].sum())
        elif policy.cost_per_contact is not None:
            c = contacts * policy.cost_per_contact
        else:
            c = None

        if is_val_weighted:
            mult = margin * horizon
        elif values is not None and policy.value_column is not None:
            mean_v = float(values[chosen].mean()) if contacts else 0.0
            mult = mean_v * margin * horizon
        elif policy.value_per_conversion is not None:
            mult = policy.value_per_conversion
        else:
            mult = None

        curve.append(
            _profit_point(
                contacts,
                int(depths[contacts - 1]) if contacts else 0,
                float(lift[chosen].sum()),
                expected,
                policy,
                cost=c,
                mult=mult,
            )
        )
    by_contacts = {point.contacts: point for point in curve}
    bands = any(point.net_value_low is not None for point in curve if point.contacts)
    bands_note = NO_HOLDOUT_NOTE if observed is None else BANDS_NOTE if bands else NO_INTERVAL_NOTE

    result = ProfitCurve(
        run_id=run_id,
        computed_on=computed_on,
        rows=rows,
        eligible_persuadables=len(ranked),
        max_contacts=reachable,
        max_contacts_reason=max_reason,
        budget_contacts=policy.budget_contacts,
        cost_per_contact=policy.cost_per_contact,
        value_per_conversion=policy.value_per_conversion,
        overridden=overridden,
        points=tuple(curve),
        configured=by_contacts[configured],
        configured_stop_reason=configured_reason,
        optimum=None if best is None else by_contacts[best],
        optimum_note=optimum_note,
        bands_available=bands,
        bands_note=bands_note,
        value_weighted=bool(policy.value_column is not None),
        value_basis=(
            policy.value_column
            or (
                f"₹{policy.value_per_conversion:g} per conversion"
                if policy.value_per_conversion is not None
                else None
            )
        ),
        causal=causal,
    )
    _LOGGER.info(
        "uplift_profit_curve points=%d max_contacts=%d configured=%d optimum=%s",
        len(curve),
        reachable,
        configured,
        "none" if best is None else str(best),
    )
    log_stage(_LOGGER, "uplift_profit_curve", rows=rows, seconds=time.perf_counter() - started)
    return result


def _best_contacts(
    depths: np.ndarray,
    rows: int,
    policy: UpliftPolicyConfig,
    observed: ObservedUplift | None,
    *,
    ranked: np.ndarray | None = None,
    values: np.ndarray | None = None,
    row_costs: np.ndarray | None = None,
    is_value_weighted: bool = False,
) -> tuple[int | None, str | None]:
    """The contact count of highest expected net value over EVERY count, or why there is none.

    Ties go to the fewest contacts: the same money for less contact. A count whose share the hold-out
    cannot measure is skipped, as its point would show "—"; zero contacts (net value exactly 0) is
    always a candidate, so contacting nobody wins when every count loses money.
    """
    import numpy as np

    has_money = (
        policy.value_per_conversion is not None
        or policy.value_column is not None
        or values is not None
        or is_value_weighted
    ) and (
        policy.cost_per_contact is not None
        or policy.value_column is not None
        or values is not None
        or row_costs is not None
    )
    if not has_money:
        return None, NO_MONEY_NOTE
    if observed is None:
        return None, NO_HOLDOUT_NOTE
    if not len(depths):
        return 0, None

    contacts = np.arange(1, len(depths) + 1, dtype=np.float64)
    lift = np.asarray(observed.points(depths / rows), dtype=np.float64)
    margin = policy.margin_pct / 100.0 if policy.margin_pct is not None else 1.0
    horizon = policy.horizon_months or 1
    if is_value_weighted:
        gross = (contacts * lift) * (margin * horizon)
    elif values is not None and ranked is not None:
        mean_vals = np.asarray([float(values[ranked[:c]].mean()) for c in range(1, len(depths) + 1)])
        gross = (contacts * lift) * (mean_vals * margin * horizon)
    elif policy.value_per_conversion is not None:
        gross = (contacts * lift) * policy.value_per_conversion
    else:
        return None, NO_MONEY_NOTE

    if row_costs is not None and ranked is not None:
        costs = np.asarray([float(row_costs[ranked[:c]].sum()) for c in range(1, len(depths) + 1)])
    elif policy.cost_per_contact is not None:
        costs = contacts * policy.cost_per_contact
    else:
        return None, NO_MONEY_NOTE

    net = np.concatenate([[0.0], gross - costs])
    return int(np.nanargmax(net)), None


def _profit_point(
    contacts: int,
    depth: int,
    predicted: float,
    expected: ConfidenceValue | None,
    policy: UpliftPolicyConfig,
    *,
    cost: float | None = None,
    mult: float | None = None,
) -> ProfitPoint:
    """One point of the budget curve; the money is computed exactly as `recommend_policy` does it."""
    if cost is None and policy.cost_per_contact is not None:
        cost = 0.0 if contacts == 0 else contacts * policy.cost_per_contact
    if mult is None and policy.value_per_conversion is not None:
        mult = policy.value_per_conversion

    if expected is None or mult is None:
        value = None
        net = None
        net_low = None
        net_high = None
    else:
        value = expected.value * mult
        net = None if cost is None else value - cost
        net_low = None if expected.ci_low is None or cost is None else expected.ci_low * mult - cost
        net_high = None if expected.ci_high is None or cost is None else expected.ci_high * mult - cost

    return ProfitPoint(
        contacts=contacts,
        ranking_depth=depth,
        predicted_incremental_conversions=predicted,
        expected_incremental_conversions=expected,
        expected_cost=cost,
        expected_value=value,
        expected_net_value=net,
        net_value_low=net_low,
        net_value_high=net_high,
        roi=None if net is None or cost is None or cost == 0 else net / cost,
    )


def _expected_conversions(
    contacts: int,
    depth: int,
    rows: int,
    observed_top_share: Callable[[float], ConfidenceValue | None] | None,
) -> ConfidenceValue | None:
    """`contacts × observed uplift in the top depth/rows share`, interval scaled the same way."""
    if observed_top_share is None:
        return None
    if contacts == 0:
        return _scaled(0, None)
    return _scaled(contacts, observed_top_share(depth / rows))


def _scaled(contacts: int, observed: ConfidenceValue | None) -> ConfidenceValue | None:
    """`contacts ×` an observed uplift and its interval; exactly zero, zero-width, for no contacts.

    Called only when a hold-out lookup exists. The one place expected conversions are formed, so a
    point of the budget curve and the recommendation cannot drift apart.
    """
    if contacts == 0:
        return ConfidenceValue(value=0.0, ci_low=0.0, ci_high=0.0)
    if observed is None:
        return None
    return ConfidenceValue(
        value=contacts * observed.value,
        ci_low=None if observed.ci_low is None else contacts * observed.ci_low,
        ci_high=None if observed.ci_high is None else contacts * observed.ci_high,
        confidence_level=observed.confidence_level,
    )


def _tiebreak_vector(tiebreak: np.ndarray, rows: int) -> np.ndarray:
    """`tiebreak` as a one-dimensional numeric vector of `rows` entries."""
    import numpy as np

    array = np.asarray(tiebreak)
    if array.ndim != 1 or len(array) != rows:
        raise ValueError(f"tiebreak must be a one-dimensional array of {rows} rows; got shape {array.shape}.")
    if not (np.issubdtype(array.dtype, np.integer) or np.issubdtype(array.dtype, np.floating)):
        raise ValueError("tiebreak must be numeric.")
    if np.issubdtype(array.dtype, np.floating) and not bool(np.isfinite(array).all()):
        raise ValueError("tiebreak must be finite.")
    return array


def _eligible_vector(eligible: np.ndarray | None, rows: int) -> np.ndarray:
    """`eligible` as a boolean vector of `rows` entries; every row when it is `None`."""
    import numpy as np

    if eligible is None:
        return np.ones(rows, dtype=bool)
    array = np.asarray(eligible)
    if array.ndim != 1 or len(array) != rows:
        raise ValueError(f"eligible must be a one-dimensional mask of {rows} rows; got shape {array.shape}.")
    if array.dtype != np.bool_:
        raise ValueError("eligible must be a boolean mask.")
    return array
