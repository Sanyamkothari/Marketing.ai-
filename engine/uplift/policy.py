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

`numpy` is imported inside the function bodies, never at module level, so `import engine` stays
fast.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from engine.uplift.contracts import (
    ConfidenceValue,
    PolicyRecommendation,
    PolicyStopReason,
    Segment,
)
from engine.uplift.segments import _finite_vector, _segment_vector
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

    from engine.uplift.config import UpliftPolicyConfig

__all__ = [
    "below_cost",
    "choose_contacts",
    "rank_positions",
    "ranking",
    "recommend_policy",
]

_LOGGER = get_logger(__name__)


def below_cost(uplift: np.ndarray, policy: UpliftPolicyConfig) -> np.ndarray:
    """Which rows would not pay for their contact: `uplift × value < cost`.

    All `False` unless both `value_per_conversion` and `cost_per_contact` are configured - without
    both there is no money comparison to make, and no row is cut for it.
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    value, cost = policy.value_per_conversion, policy.cost_per_contact
    if value is None or cost is None:
        return np.zeros(len(lift), dtype=bool)
    return np.asarray(lift * value < cost, dtype=bool)


def ranking(uplift: np.ndarray, tiebreak: np.ndarray | None = None) -> np.ndarray:
    """Row indices, highest predicted uplift first; ties by `tiebreak` ascending, then input order.

    The one ordering every targeting decision uses: who is chosen (`choose_contacts`), how deep the
    choice reaches (`recommend_policy`) and which held-out rows would have been chosen
    (`engine.uplift.actions`). Using one function is what keeps those three consistent.
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    if tiebreak is None:
        order: np.ndarray = np.argsort(-lift, kind="mergesort")
        return order
    keys = _tiebreak_vector(tiebreak, len(lift))
    # `np.lexsort` sorts by its LAST key first and is stable, so remaining ties keep input order.
    return np.asarray(np.lexsort((keys, -lift)), dtype=np.int64)


def rank_positions(uplift: np.ndarray, tiebreak: np.ndarray | None = None) -> np.ndarray:
    """Each row's 0-based position in :func:`ranking` (0 = the highest predicted uplift)."""
    import numpy as np

    order = ranking(uplift, tiebreak)
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

    order = ranking(lift, tiebreak)
    ranked = order[is_candidate[order]]
    cut = below_cost(lift[ranked], policy)
    # The ranking is descending, so the rows that pay for themselves are a prefix of it.
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
    selected, reason = choose_contacts(lift, labels, policy, eligible=allowed, tiebreak=tiebreak)

    rows = len(lift)
    contacts = int(selected.sum())
    candidates = int(((labels == Segment.PERSUADABLE.value) & allowed).sum())
    depth = int(rank_positions(lift, tiebreak)[selected].max()) + 1 if contacts else 0
    expected = _expected_conversions(contacts, depth, rows, observed_top_share)
    cost = None if policy.cost_per_contact is None else contacts * policy.cost_per_contact
    value = (
        None
        if expected is None or policy.value_per_conversion is None
        else expected.value * policy.value_per_conversion
    )
    net = None if value is None or cost is None else value - cost

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
        return ConfidenceValue(value=0.0, ci_low=0.0, ci_high=0.0)
    observed = observed_top_share(depth / rows)
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
