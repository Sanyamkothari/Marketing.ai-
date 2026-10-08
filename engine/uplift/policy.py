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

**Ranking by net money (Plan J M97, DEC-1307).** :func:`customer_net_values` computes every row's
money ONCE (:class:`CustomerMoney`), and every entry point takes it: the ranking, the cut, the cost of
each contact count (one cumulative sum, so the budget curve stays linear in the rows), the reported
money and the curve. Two paths:

* **scalar** - no `uplift.policy.value_column`: ranked by uplift as above; one conversion is worth
  `value_per_conversion × margin × horizon` (with neither set, exactly `value_per_conversion`, so
  every number of a run configured before M97 is what it was); `min_roi`, when set, cuts a row whose
  `uplift × unit − cost` is below `min_roi × cost`;
* **value** - `value_column` set and its values passed: net value per row is
  `uplift × value × margin × horizon − offer_cost × p_treated − contact cost`, the contact cost being
  `cost_per_contact`, else the `value:` block of `configs/pilot/value.yaml`. Rows are ranked by it and
  cut where it is below `min_roi × the row's cost` (0 without `min_roi`). Expected conversions stay
  `N ×` the hold-out's observed uplift; the expected value is `N ×` the hold-out's VALUE-WEIGHTED
  observed uplift `× margin × horizon`, both read from the hold-out ranked the same way
  (:func:`holdout_lookups`). A missing value is never invented: it counts as zero value and is
  counted (`values_missing`); above :data:`MAX_MISSING_VALUE_SHARE`, or when the hold-out has no values
  of the column, the money is null and `money_note` says why.

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
from dataclasses import dataclass
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

    from engine.pilot.roi import ValueCosts
    from engine.uplift.config import UpliftPolicyConfig
    from engine.uplift.metrics import HoldoutUplift

__all__ = [
    "DEFAULT_CURVE_POINTS",
    "MAX_MISSING_VALUE_SHARE",
    "CustomerMoney",
    "HoldoutLookups",
    "ObservedUplift",
    "below_cost",
    "choose_contacts",
    "customer_net_values",
    "holdout_lookups",
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

MAX_MISSING_VALUE_SHARE: Final[float] = 0.10
"""Plan J M97 (DEC-1307): above this share of customers without a value, no money is shown.

A customer whose value is missing is never given one: it counts at zero value (so it ranks below
every customer who pays for the contact) and is counted in `values_missing`. Beyond this share the
value-weighted money would describe too few of the customers it is quoted for, so it is null, with
the reason."""

NO_VALUE_COLUMN_NOTE: Final[str] = (
    "The rows have no '{column}' column, so customers are ranked by predicted uplift and any money "
    "uses the value of one conversion."
)
NO_HOLDOUT_VALUES_NOTE: Final[str] = (
    "The model's training hold-out has no '{column}' values, so the expected conversions and money of a "
    "list ranked by '{column}' cannot be measured. Train the model with uplift.policy.value_column set "
    "to '{column}' to get them."
)
VALUES_MISSING_NOTE: Final[str] = (
    "{missing} of {rows} customers have no '{column}'; they count at zero value and are not given one."
)
TOO_MANY_MISSING_NOTE: Final[str] = (
    "{missing} of {rows} customers ({share}) have no '{column}', more than the {limit} the money can "
    "leave out, so no expected value is shown. Fill in '{column}' to see it."
)
HOLDOUT_TOO_MANY_MISSING_NOTE: Final[str] = (
    "{missing} of {rows} hold-out customers ({share}) have no '{column}', more than the {limit} the money "
    "can leave out, so no expected value is shown."
)


class ObservedUplift(Protocol):
    """The hold-out's observed uplift by top share: `engine.uplift.metrics.HoldoutUplift`, or a fake."""

    def intervals(self, fractions: Sequence[float]) -> Sequence[ConfidenceValue | None]:
        """What `recommend_policy`'s `observed_top_share(f)` returns, for each `f`."""
        ...

    def points(self, fractions: np.ndarray) -> np.ndarray:
        """The point estimate of each share, NaN where the hold-out cannot measure it."""
        ...


# ---------------------------------------------------------------------------
# The money of every row, computed once (Plan J M97, DEC-1307)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CustomerMoney:
    """The money of one policy for every row: computed once, then passed to every step that needs it.

    **Scalar path** (no `value_column`, or its values not given): the ranking is by predicted
    uplift, one conversion is worth `value_per_conversion × margin × horizon` (exactly
    `value_per_conversion` with neither set), a contact costs `cost_per_contact`, and a row is below
    cost when `uplift × unit < cost` (with `min_roi`: when `uplift × unit − cost < min_roi × cost`).
    Every number is what a run configured before M97 computed.

    **Value path** (`value_column` set and its values given): net value per row is
    `uplift × value × margin × horizon − offer_cost × p_treated − contact cost`, the contact cost being
    `cost_per_contact`, else the `value:` block of `configs/pilot/value.yaml`
    (`engine.pilot.roi.lookup_value_costs`). The ranking is by net value, a row is below cost when
    `net value < min_roi × its cost` (0 without `min_roi`), and the money is the hold-out's
    value-weighted observed uplift × margin × horizon. A missing value counts as zero value, never as
    an invented one.
    """

    net_value: np.ndarray | None
    """Net value per row on the value path (the ranking key); `None` on the scalar path."""
    cost: np.ndarray | None
    """Cost of contacting each row; `None` when no cost is configured."""
    below_cost: np.ndarray
    """Rows that would not pay for their contact (or would not reach `min_roi`)."""
    unit: float | None
    """What one unit of the hold-out's observed uplift is worth; `None` when no money can be shown."""
    contact_cost: float | None
    """The cost of every contact when it is the same for all rows (totals are `contacts × it`)."""
    value_weighted: bool = False
    """True on the value path: the money comes from the value-weighted hold-out."""
    value_column: str | None = None
    values_missing: int | None = None
    """Rows without a value on the value path (counted at zero value); `None` on the scalar path."""
    note: str | None = None
    """Why money is missing, or what it leaves out, in plain words."""

    def cost_totals(self, ranked: np.ndarray) -> np.ndarray | None:
        """`totals[c]` is the cost of contacting the first `c` rows of `ranked`; `None` without a cost.

        One cumulative sum, so every contact count costs O(1) afterwards. With one cost for every
        contact the total is `c × cost`, the arithmetic a run configured before M97 used.
        """
        import numpy as np

        if self.cost is None:
            return None
        if self.contact_cost is not None:
            totals: np.ndarray = np.arange(len(ranked) + 1, dtype=np.float64) * self.contact_cost
            return totals
        return np.concatenate([[0.0], np.cumsum(self.cost[ranked], dtype=np.float64)])


def _value_factor(policy: UpliftPolicyConfig) -> float:
    """`margin × horizon`: 1.0 when neither is set, so the scalar path's numbers are unchanged."""
    margin = 1.0 if policy.margin_pct is None else policy.margin_pct / 100.0
    return margin * (1 if policy.horizon_months is None else policy.horizon_months)


def customer_net_values(
    uplift: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
    value_costs: ValueCosts | None = None,
) -> CustomerMoney:
    """Every row's money under `policy` (see :class:`CustomerMoney` for the two paths).

    `values` are the rows' `value_column` values (NaN where missing); they are used only when
    `policy.value_column` is set. `p_treated` prices the offer (`offer_cost × p_treated`).
    `value_costs` defaults to the `value:` block of `configs/pilot/value.yaml`, read once here.
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    rows = len(lift)
    factor = _value_factor(policy)
    column = policy.value_column
    if column is None or values is None:
        unit = None if policy.value_per_conversion is None else policy.value_per_conversion * factor
        each = policy.cost_per_contact
        if unit is None or each is None:
            below = np.zeros(rows, dtype=bool)
        elif policy.min_roi is None:
            below = np.asarray(lift * unit < each, dtype=bool)
        else:
            below = np.asarray(lift * unit - each < policy.min_roi * each, dtype=bool)
        return CustomerMoney(
            net_value=None,
            cost=None if each is None else np.full(rows, each, dtype=np.float64),
            below_cost=below,
            unit=unit,
            contact_cost=each,
            note=None if column is None else NO_VALUE_COLUMN_NOTE.format(column=column),
        )

    if value_costs is None:
        from engine.pilot.roi import lookup_value_costs

        value_costs = lookup_value_costs()
    raw = np.asarray(values, dtype=np.float64)
    if raw.ndim != 1 or len(raw) != rows:
        raise ValueError(f"values must be a one-dimensional array of {rows} rows; got shape {raw.shape}.")
    missing = int((~np.isfinite(raw)).sum())
    known = np.where(np.isfinite(raw), raw, 0.0)
    contact = policy.cost_per_contact if policy.cost_per_contact is not None else value_costs.contact_cost
    same_cost: float | None = contact
    cost = np.full(rows, contact, dtype=np.float64)
    if value_costs.offer_cost and p_treated is not None:
        taken = _finite_vector(p_treated, "p_treated")
        if len(taken) != rows:
            raise ValueError(f"p_treated has {len(taken)} rows, expected {rows}.")
        cost = cost + value_costs.offer_cost * taken
        same_cost = None
    net = lift * known * factor - cost
    threshold = 0.0 if policy.min_roi is None else policy.min_roi * cost
    too_many = rows > 0 and missing / rows > MAX_MISSING_VALUE_SHARE
    return CustomerMoney(
        net_value=net,
        cost=cost,
        below_cost=np.asarray(net < threshold, dtype=bool),
        unit=None if too_many else factor,
        contact_cost=same_cost,
        value_weighted=True,
        value_column=column,
        values_missing=missing,
        note=_missing_note(missing, rows, column, TOO_MANY_MISSING_NOTE if too_many else None),
    )


def _missing_note(missing: int, rows: int, column: str, too_many: str | None) -> str | None:
    if not missing:
        return None
    template = too_many or VALUES_MISSING_NOTE
    return template.format(
        missing=missing,
        rows=rows,
        column=column,
        share=f"{missing / rows:.0%}",
        limit=f"{MAX_MISSING_VALUE_SHARE:.0%}",
    )


@dataclass(frozen=True)
class HoldoutLookups:
    """The hold-out's observed uplift for a ranking: conversions, and money when ranked by value."""

    conversions: HoldoutUplift | None
    """Unweighted: incremental conversions per customer contacted, by top share."""
    value: HoldoutUplift | None
    """Value-weighted (value path only): incremental value per customer contacted, by top share."""
    note: str | None = None


def holdout_lookups(
    uplift: np.ndarray,
    t: np.ndarray,
    y: np.ndarray,
    *,
    policy: UpliftPolicyConfig,
    samples: int,
    seed: int,
    ranked_by_value: bool,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
    value_costs: ValueCosts | None = None,
) -> HoldoutLookups:
    """The lookups `recommend_policy` and `profit_curve` quote the hold-out with.

    A list ranked by uplift (`ranked_by_value` false) asks the hold-out ranked by uplift, exactly as
    before M97. A list ranked by value asks the hold-out ranked the same way - by each hold-out row's
    net value under `policy` - so "the top share" is the same kind of customer the list chose; the
    value lookup weights each outcome by the row's value (missing values count as zero). `values` are
    the hold-out's own values of the same column, or `None` when it has none: then nothing is quoted
    and the note says why.
    """
    import numpy as np

    from engine.uplift.metrics import HoldoutUplift

    treated = np.asarray(t, dtype=np.int64)
    outcome = np.asarray(y, dtype=np.int64)
    if not ranked_by_value:
        pred = np.asarray(uplift, dtype=np.float64)
        return HoldoutLookups(
            HoldoutUplift(pred=pred, t=treated, y=outcome, samples=samples, seed=seed), None
        )
    if values is None:
        return HoldoutLookups(None, None, NO_HOLDOUT_VALUES_NOTE.format(column=policy.value_column))
    money = customer_net_values(uplift, policy, values=values, p_treated=p_treated, value_costs=value_costs)
    key = money.net_value
    if (
        key is None or money.value_column is None or money.values_missing is None
    ):  # value path by construction
        raise RuntimeError("holdout_lookups: values were given but the value path was not taken.")
    conversions = HoldoutUplift(pred=key, t=treated, y=outcome, samples=samples, seed=seed)
    rows = len(key)
    if money.unit is None:
        note = _missing_note(money.values_missing, rows, money.value_column, HOLDOUT_TOO_MANY_MISSING_NOTE)
        return HoldoutLookups(conversions, None, note)
    raw = np.asarray(values, dtype=np.float64)
    weights = np.where(np.isfinite(raw), raw, 0.0)
    return HoldoutLookups(
        conversions,
        HoldoutUplift(pred=key, t=treated, y=outcome, samples=samples, seed=seed, value=weights),
    )


def _resolve_money(
    lift: np.ndarray,
    policy: UpliftPolicyConfig,
    money: CustomerMoney | None,
    values: np.ndarray | None,
    p_treated: np.ndarray | None,
) -> CustomerMoney:
    """`money` when the caller computed it, else :func:`customer_net_values` of the inputs."""
    if money is None:
        return customer_net_values(lift, policy, values=values, p_treated=p_treated)
    if values is not None or p_treated is not None:
        raise ValueError("Pass either money or values/p_treated, not both.")
    if len(money.below_cost) != len(lift):
        raise ValueError(f"money describes {len(money.below_cost)} rows, expected {len(lift)}.")
    return money


def below_cost(
    uplift: np.ndarray, policy: UpliftPolicyConfig, *, money: CustomerMoney | None = None
) -> np.ndarray:
    """Which rows would not pay for their contact (see :class:`CustomerMoney` for the rule).

    All `False` on the scalar path unless both `value_per_conversion` and `cost_per_contact` are
    configured - without both there is no money comparison to make, and no row is cut for it.
    """
    lift = _finite_vector(uplift, "uplift")
    return _resolve_money(lift, policy, money, None, None).below_cost


def ranking(
    uplift: np.ndarray,
    tiebreak: np.ndarray | None = None,
    *,
    net_value: np.ndarray | None = None,
) -> np.ndarray:
    """Row indices, highest predicted uplift (or `net_value`, when given) first; ties by `tiebreak`
    ascending, then input order.

    The one ordering every targeting decision uses: who is chosen (`choose_contacts`), how deep the
    choice reaches (`recommend_policy`) and which held-out rows would have been chosen
    (`engine.uplift.actions`). Using one function is what keeps those three consistent. A list ranked
    by value passes its :attr:`CustomerMoney.net_value` (Plan J M97).
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    score = lift if net_value is None else _finite_vector(net_value, "net value")
    if len(score) != len(lift):
        raise ValueError(f"net_value has {len(score)} rows, expected {len(lift)}.")
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
    """Each row's 0-based position in :func:`ranking` (0 = the highest predicted uplift or net value)."""
    import numpy as np

    order = ranking(uplift, tiebreak, net_value=net_value)
    positions = np.empty(len(order), dtype=np.int64)
    positions[order] = np.arange(len(order), dtype=np.int64)
    return positions


@dataclass(frozen=True)
class _Plan:
    """Who can be chosen, in ranking order, and how many are: what every entry point shares."""

    candidate_positions: np.ndarray
    """Ranking positions (of every row) of the eligible persuadables, best first."""
    ranked: np.ndarray
    """Their row indices, in the same order."""
    reachable: int
    """How many of them pay for their contact: a prefix of `ranked`."""
    take: int
    reason: PolicyStopReason


def _plan(
    lift: np.ndarray,
    labels: np.ndarray,
    allowed: np.ndarray,
    tiebreak: np.ndarray | None,
    money: CustomerMoney,
    budget: int | None,
) -> _Plan:
    import numpy as np

    if len(labels) != len(lift):
        raise ValueError(
            f"uplift has {len(lift)} rows but segments has {len(labels)}; they must describe the same rows."
        )
    is_candidate = (labels == Segment.PERSUADABLE.value) & allowed
    if not bool(is_candidate.any()):
        empty = np.zeros(0, dtype=np.int64)
        return _Plan(empty, empty, 0, 0, PolicyStopReason.NO_PERSUADABLES)
    order = ranking(lift, tiebreak, net_value=money.net_value)
    candidate_positions = np.flatnonzero(is_candidate[order])
    ranked = order[candidate_positions]
    cut = money.below_cost[ranked]
    # The ranking is descending, so the rows that pay for themselves are a prefix of it.
    paying = int(np.argmax(cut)) if cut.any() else len(ranked)
    take = min(len(ranked), paying, len(ranked) if budget is None else budget)
    if take == len(ranked):
        reason = PolicyStopReason.ALL_PERSUADABLES
    elif take == paying:
        reason = PolicyStopReason.VALUE_BELOW_COST
    else:
        reason = PolicyStopReason.BUDGET
    return _Plan(candidate_positions, ranked, paying, take, reason)


def _selected(plan: _Plan, labels: np.ndarray) -> np.ndarray:
    import numpy as np

    selected = np.zeros(len(labels), dtype=bool)
    selected[plan.ranked[: plan.take]] = True
    sleeping = labels == Segment.SLEEPING_DOG.value
    if bool((selected & sleeping).any()):  # unreachable by construction; the rule is too important to trust
        raise RuntimeError("The targeting policy selected a sleeping dog; refusing to recommend it.")
    return selected


def choose_contacts(
    uplift: np.ndarray,
    segments: np.ndarray,
    policy: UpliftPolicyConfig,
    *,
    eligible: np.ndarray | None = None,
    tiebreak: np.ndarray | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
    money: CustomerMoney | None = None,
) -> tuple[np.ndarray, PolicyStopReason]:
    """The rows to treat (a boolean mask aligned to `uplift`) and why there are not more of them.

    Only eligible persuadables are candidates; the rest of the rules are in the module docstring.
    `eligible` defaults to every row; `tiebreak` (one number per row) orders rows of equal uplift,
    and without it they keep input order. `money` is the rows' :class:`CustomerMoney` when the caller
    has computed it, else it is computed from `values` and `p_treated`.
    """
    lift = _finite_vector(uplift, "uplift")
    labels = _segment_vector(segments)
    allowed = _eligible_vector(eligible, len(lift))
    resolved = _resolve_money(lift, policy, money, values, p_treated)
    plan = _plan(lift, labels, allowed, tiebreak, resolved, policy.budget_contacts)
    return _selected(plan, labels), plan.reason


def _join(*notes: str | None) -> str | None:
    kept = [note for note in notes if note]
    return " ".join(kept) if kept else None


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
    observed_top_value: Callable[[float], ConfidenceValue | None] | None = None,
    holdout_note: str | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
    money: CustomerMoney | None = None,
) -> tuple[PolicyRecommendation, np.ndarray]:
    """`policy_recommendation.json` and the mask of rows it recommends treating.

    `observed_top_share(fraction)` returns the uplift observed on the hold-out among the top
    `fraction` of it, with its interval, or `None` when it cannot be measured; it is asked about the
    ranking depth the selection reaches, not about `N / rows` (see the module docstring). `eligible`
    and `tiebreak` are passed to :func:`choose_contacts`.

    A list ranked by value (Plan J M97) also takes `observed_top_value`, the same lookup weighted by
    each hold-out customer's value: the expected value is `N ×` it `× margin × horizon`, while the
    expected conversions stay `N ×` the unweighted one. `holdout_note` is the caller's reason when
    the hold-out could not be asked (:class:`HoldoutLookups`).
    """
    started = time.perf_counter()
    lift = _finite_vector(uplift, "uplift")
    labels = _segment_vector(segments)
    allowed = _eligible_vector(eligible, len(lift))
    resolved = _resolve_money(lift, policy, money, values, p_treated)
    plan = _plan(lift, labels, allowed, tiebreak, resolved, policy.budget_contacts)
    selected = _selected(plan, labels)

    rows = len(lift)
    contacts = plan.take
    candidates = len(plan.ranked)
    depth = int(plan.candidate_positions[contacts - 1]) + 1 if contacts else 0
    expected = _expected_conversions(contacts, depth, rows, observed_top_share)
    basis = (
        _expected_conversions(contacts, depth, rows, observed_top_value)
        if resolved.value_weighted
        else expected
    )
    totals = resolved.cost_totals(plan.ranked)
    cost = None if totals is None else float(totals[contacts])
    value, net, net_low, net_high = _money_fields(basis, resolved.unit, cost)

    recommendation = PolicyRecommendation(
        run_id=run_id,
        computed_on=computed_on,
        rows=rows,
        eligible_persuadables=candidates,
        contacts_recommended=contacts,
        stop_reason=plan.reason,
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
        money_note=_join(resolved.note, holdout_note),
        values_missing=resolved.values_missing,
        causal=causal,
    )
    _LOGGER.info(
        "uplift_policy eligible_persuadables=%d contacts_recommended=%d ranking_depth=%d stop_reason=%s",
        candidates,
        contacts,
        depth,
        plan.reason.value,
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
    observed_value: ObservedUplift | None = None,
    holdout_note: str | None = None,
    values: np.ndarray | None = None,
    p_treated: np.ndarray | None = None,
    money: CustomerMoney | None = None,
) -> ProfitCurve:
    """Expected conversions, cost, value, net value and ROI against the number of customers contacted.

    The arguments are :func:`recommend_policy`'s, with `observed` in place of `observed_top_share`
    and `observed_value` in place of `observed_top_value` (their `intervals` must answer what those
    callables would, share for share). `policy.budget_contacts` only places the configured point: the
    curve runs to every contact the other rules allow. `overridden` is recorded as given - only the
    caller knows whether the cost and value are the run's.

    Linear in the rows: the money is computed once, the cost of every contact count comes from one
    cumulative sum, and only the plotted counts sum the model's predictions.
    """
    import numpy as np

    if points < 2:
        raise ValueError(f"a curve needs at least 2 points, got {points}.")
    started = time.perf_counter()
    lift = _finite_vector(uplift, "uplift")
    labels = _segment_vector(segments)
    allowed = _eligible_vector(eligible, len(lift))
    resolved = _resolve_money(lift, policy, money, values, p_treated)
    plan = _plan(lift, labels, allowed, tiebreak, resolved, policy.budget_contacts)
    selected = _selected(plan, labels)

    rows = len(lift)
    ranked, reachable = plan.ranked, plan.reachable
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
    depths = plan.candidate_positions[:reachable] + 1
    totals = resolved.cost_totals(ranked)
    best, optimum_note = _best_contacts(
        depths, rows, resolved, observed, observed_value, totals, holdout_note
    )
    counts = sorted(
        {round(float(c)) for c in np.linspace(0, reachable, points)}
        | {configured}
        | (set() if best is None else {best})
    )
    fractions = [int(depths[c - 1]) / rows for c in counts if c]
    looked_up = iter(list(observed.intervals(fractions)) if observed is not None and fractions else [])
    valued_lookup = observed_value if resolved.value_weighted else None
    valued = iter(list(valued_lookup.intervals(fractions)) if valued_lookup is not None and fractions else [])
    curve: list[ProfitPoint] = []
    for contacts in counts:
        chosen = np.sort(ranked[:contacts])  # index order, as `lift[selected]` sums in recommend_policy
        expected = None if observed is None else _scaled(contacts, next(looked_up) if contacts else None)
        if not resolved.value_weighted:
            basis = expected
        else:
            basis = None if valued_lookup is None else _scaled(contacts, next(valued) if contacts else None)
        curve.append(
            _profit_point(
                contacts,
                int(depths[contacts - 1]) if contacts else 0,
                float(lift[chosen].sum()),
                expected,
                basis,
                resolved.unit,
                None if totals is None else float(totals[contacts]),
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
        configured_stop_reason=plan.reason,
        optimum=None if best is None else by_contacts[best],
        optimum_note=optimum_note,
        bands_available=bands,
        bands_note=bands_note,
        value_weighted=resolved.value_weighted,
        value_basis=_value_basis(policy, resolved),
        money_note=_join(resolved.note, holdout_note),
        values_missing=resolved.values_missing,
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


def _value_basis(policy: UpliftPolicyConfig, money: CustomerMoney) -> str | None:
    """What the money is based on, in words; the rupees in the repository's INR format."""
    from engine.pilot.roi import format_inr

    if money.value_weighted:
        base = f"each customer's {money.value_column}"
    elif policy.value_per_conversion is not None:
        base = f"{format_inr(policy.value_per_conversion)} per conversion"
    else:
        return None
    if policy.margin_pct is not None:
        base += f" × {policy.margin_pct:g}% margin"
    if policy.horizon_months is not None:
        base += f" × {policy.horizon_months} month{'' if policy.horizon_months == 1 else 's'}"
    return base


def _best_contacts(
    depths: np.ndarray,
    rows: int,
    money: CustomerMoney,
    observed: ObservedUplift | None,
    observed_value: ObservedUplift | None,
    totals: np.ndarray | None,
    holdout_note: str | None,
) -> tuple[int | None, str | None]:
    """The contact count of highest expected net value over EVERY count, or why there is none.

    Ties go to the fewest contacts: the same money for less contact. A count whose share the hold-out
    cannot measure is skipped, as its point would show "—"; zero contacts (net value exactly 0) is
    always a candidate, so contacting nobody wins when every count loses money. Linear: the costs
    come from `totals` (one cumulative sum), never from a sum per count.
    """
    import numpy as np

    lookup: ObservedUplift | None
    if money.value_weighted:
        lookup = observed_value
        if lookup is None or money.unit is None:
            return None, _join(money.note, holdout_note) or NO_HOLDOUT_NOTE
    else:
        lookup = observed
        if lookup is None:
            return None, NO_HOLDOUT_NOTE
        if money.unit is None or totals is None:
            return None, NO_MONEY_NOTE
    if totals is None:  # a value-ranked list always has a cost; kept for the type checker
        return None, NO_MONEY_NOTE
    if not len(depths):
        return 0, None
    contacts = np.arange(1, len(depths) + 1, dtype=np.float64)
    lift = np.asarray(lookup.points(depths / rows), dtype=np.float64)
    # The arithmetic of `_scaled` and `_money_fields`, in their order: (contacts × uplift) × unit − cost.
    net = np.concatenate([[0.0], (contacts * lift) * money.unit - totals[1 : len(depths) + 1]])
    return int(np.nanargmax(net)), None


def _money_fields(
    basis: ConfidenceValue | None, unit: float | None, cost: float | None
) -> tuple[float | None, float | None, float | None, float | None]:
    """`(value, net, net at the low end, net at the high end)`: the one place money is formed.

    `basis` is the expected incremental conversions (scalar path) or the expected value before margin
    and horizon (value path); `unit` turns it into rupees. Each field is null unless every number it
    is made of exists.
    """
    value = None if basis is None or unit is None else basis.value * unit
    net = None if value is None or cost is None else value - cost

    def at(bound: float | None) -> float | None:
        """The net value were the basis at `bound` instead of the point estimate."""
        if bound is None or unit is None or cost is None:
            return None
        return bound * unit - cost

    if basis is None:
        return value, net, None, None
    return value, net, at(basis.ci_low), at(basis.ci_high)


def _profit_point(
    contacts: int,
    depth: int,
    predicted: float,
    expected: ConfidenceValue | None,
    basis: ConfidenceValue | None,
    unit: float | None,
    cost: float | None,
) -> ProfitPoint:
    """One point of the budget curve; the money is computed exactly as `recommend_policy` does it."""
    value, net, net_low, net_high = _money_fields(basis, unit, cost)
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
