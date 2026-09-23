"""The actions stage of an uplift scoring run: segments instead of bands (plan B §5, Stage C).

A Phase 1 scoring run gives every row a risk band and the band's action. An uplift run replaces the
band with the row's segment, and the action with what the segment and the targeting policy say -
but the two rules that protect customers and the measurement are **not** reimplemented here:

* **Suppression** (consent, opt-out, recent contact, in that precedence) and
* **the control group** (a run-seeded per-customer hash over the non-suppressed rows)

both come from `engine.stages.actions.apply_actions`, called unchanged on a copy of the frame whose
`actions.score_field` column holds the predicted uplift. That keeps one implementation of each rule:
the same run id holds out exactly the same customers under both problem types, and a change to a
suppression rule reaches uplift runs without a second edit. The Phase 1 band that call computes is
meaningless for an uplift (it clamps a difference of probabilities into probability bands) and is
overwritten; the input frame's own `score_field` column, if it has one, is returned untouched.

**One action per row**, first match wins:

1. `Suppressed` - kept from Phase 1. Consent and opt-out outrank any model.
2. `Control (hold out)` - kept from Phase 1. The holdout is drawn from every segment, so it is a
   random sample of the eligible customers.
3. `Treat` - the persuadables the policy selects from the remaining (eligible) rows, highest uplift
   first, within budget and cost (`engine.uplift.policy.choose_contacts`).
4. A persuadable the policy did not select: `Don't treat (below cost)` when its expected gain does
   not cover the contact cost, else `Don't treat (over budget)`.
5. Everyone else: the segment's action from `SEGMENT_ACTIONS`. **A sleeping dog is never `Treat`**,
   whatever the configuration; the stage checks that on the way out and fails rather than export it.

**`intended_treatment`.** Stage D measures a campaign by comparing treated and control customers,
and that comparison is only fair between customers the policy treats the *same way*. The column
marks the selected rows plus the control rows that *would* have been selected had they not been held
out: persuadables ranked at or above the last selected row. The policy is a threshold on the
ranking, so both sides of the comparison pass the same threshold, and the control side is a random
draw - which is what makes treated-minus-control inside `intended_treatment` an unbiased estimate of
what the campaign caused. With nobody selected, nobody is intended.

**Ties at the cut (DEC-606).** "At or above the last selected row" is a position in one ranking
(`engine.uplift.policy.ranking`), not a comparison of uplift values. When a coarse model gives a
block of customers the same uplift and the budget cuts through it, only part of the block is
treated; comparing it with *every* tied control row would compare different populations. And if
the part treated were chosen by input order, a file sorted by, say, recency would treat the
likeliest converters and credit the campaign with their conversions. So ties are broken by a
run-seeded per-customer hash (`sha256("uplift-tie:{seed}:{key}")`, salted differently from the
control-group draw so the two are independent): the treated part of a tie block is a random subset
of it, and the control rows kept are those the same hash ranks inside the cut - the same rule on
both sides, and order-independent like the control group itself.

`pandas` and `numpy` are imported inside the function bodies, never at module level, so
`import engine` stays fast.
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Final

from engine.uplift.contracts import SEGMENT_ACTIONS, SEGMENT_LABELS, Segment
from engine.utils.ids import seed_from
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    import numpy as np
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.uplift.contracts import ConfidenceValue, PolicyRecommendation, SegmentThresholds

__all__ = [
    "BELOW_COST_ACTION",
    "INTENDED_TREATMENT_COLUMN",
    "OVER_BUDGET_ACTION",
    "SEGMENT_COLUMN",
    "TIEBREAK_SALT",
    "TREAT_ACTION",
    "apply_uplift_actions",
    "tiebreak_keys",
]

_LOGGER = get_logger(__name__)

SEGMENT_COLUMN: Final[str] = "segment"
INTENDED_TREATMENT_COLUMN: Final[str] = "intended_treatment"

TREAT_ACTION: Final[str] = SEGMENT_ACTIONS[Segment.PERSUADABLE]
OVER_BUDGET_ACTION: Final[str] = "Don't treat (over budget)"
BELOW_COST_ACTION: Final[str] = "Don't treat (below cost)"

TIEBREAK_SALT: Final[str] = "uplift-tie"
"""Prefix of the tie-break hash; differs from the control-group draw's so the two are independent."""


def apply_uplift_actions(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: str,
    causal: bool,
    prediction_columns: tuple[str, str, str] = ("uplift", "p_treated", "p_control"),
    thresholds: SegmentThresholds,
    now: datetime | None = None,
    observed_top_share: Callable[[float], ConfidenceValue | None] | None = None,
) -> tuple[pd.DataFrame, PolicyRecommendation]:
    """Segment, suppress, hold out and act on every scored row; see the module docstring.

    `prediction_columns` names the uplift, P(y | treated) and P(y | not treated) columns of
    `frame`. Returns a copy of `frame` with `segment`, `intended_treatment` and Phase 1's
    `band`/`action`/`suppressed_reason`/`control_group` added (same-named input columns replaced,
    every other column kept, Phase 1's `attrs` about the suppression rules carried over), and the
    run's `policy_recommendation.json` with `computed_on="scored"`. `causal` is the trained model's
    flag and is copied onto the recommendation; `observed_top_share` is passed to
    :func:`engine.uplift.policy.recommend_policy` (without it, expected incremental conversions are
    null). `now` is the reference time of the recency rule, as in Phase 1.
    """
    import numpy as np
    import pandas as pd

    from engine.stages.actions import (
        ACTION_COLUMN,
        BAND_COLUMN,
        CONTROL_GROUP_COLUMN,
        OUTPUT_COLUMNS,
        SUPPRESSED_REASON_COLUMN,
        apply_actions,
    )
    from engine.uplift.policy import below_cost, rank_positions, recommend_policy
    from engine.uplift.segments import assign_segments

    started = time.perf_counter()
    uplift_column, p_treated_column, p_control_column = prediction_columns
    missing = [
        name
        for name in (primary_key, uplift_column, p_treated_column, p_control_column)
        if name not in frame.columns
    ]
    if missing:
        raise ValueError(
            f"The scored frame is missing {', '.join(repr(name) for name in missing)}; uplift actions "
            f"need the primary key and the three prediction columns."
        )
    uplift = _numeric(frame[uplift_column], uplift_column)
    _numeric(frame[p_treated_column], p_treated_column)
    p_control = _numeric(frame[p_control_column], p_control_column)

    # Phase 1's suppression and control group, unchanged, on a copy whose score column is the uplift.
    work = frame.copy()
    work[config.actions.score_field] = uplift
    acted = apply_actions(work, config, run_id=run_id, primary_key=primary_key, now=now)

    segments = assign_segments(uplift, p_control, thresholds)
    suppressed = acted[SUPPRESSED_REASON_COLUMN].notna().to_numpy(dtype=bool)
    control = acted[CONTROL_GROUP_COLUMN].to_numpy(dtype=bool)
    eligible = ~suppressed & ~control

    policy = config.uplift.policy
    tiebreak = tiebreak_keys(frame[primary_key], run_id=run_id)
    recommendation, selected = recommend_policy(
        uplift,
        segments,
        policy,
        run_id=run_id,
        computed_on="scored",
        causal=causal,
        observed_top_share=observed_top_share,
        eligible=eligible,
        tiebreak=tiebreak,
    )

    values = np.array([segment.value for segment in segments.tolist()], dtype=object)
    persuadable = values == Segment.PERSUADABLE.value
    sleeping = values == Segment.SLEEPING_DOG.value

    actions = np.array([SEGMENT_ACTIONS[segment] for segment in segments.tolist()], dtype=object)
    passed_over = persuadable & eligible & ~selected
    actions[passed_over] = OVER_BUDGET_ACTION
    actions[passed_over & below_cost(uplift, policy)] = BELOW_COST_ACTION
    actions[selected] = TREAT_ACTION
    actions[control] = acted[ACTION_COLUMN].to_numpy(dtype=object)[control]
    actions[suppressed] = acted[ACTION_COLUMN].to_numpy(dtype=object)[suppressed]

    intended = selected.copy()
    if selected.any():
        positions = rank_positions(uplift, tiebreak)
        intended |= control & persuadable & (positions <= int(positions[selected].max()))

    if bool((sleeping & ((actions == TREAT_ACTION) | intended)).any()):
        raise RuntimeError("A sleeping dog was marked for treatment; refusing to export the actions.")

    result = frame.copy()
    for column in OUTPUT_COLUMNS:
        result[column] = acted[column].to_numpy()
    result[BAND_COLUMN] = [SEGMENT_LABELS[segment] for segment in segments.tolist()]
    result[ACTION_COLUMN] = actions
    result[SEGMENT_COLUMN] = values
    result[INTENDED_TREATMENT_COLUMN] = pd.Series(intended, index=frame.index, dtype=bool)
    result.attrs = dict(acted.attrs)

    _LOGGER.info(
        "uplift_actions treat_rows=%d intended_rows=%d suppressed_rows=%d control_rows=%d",
        int(selected.sum()),
        int(intended.sum()),
        int(suppressed.sum()),
        int(control.sum()),
    )
    log_stage(_LOGGER, "uplift_actions", rows=len(result), seconds=time.perf_counter() - started)
    return result, recommendation


def tiebreak_keys(keys: pd.Series, *, run_id: str) -> np.ndarray:
    """One run-seeded 64-bit draw per row, from its primary key; orders rows of equal uplift.

    Order-independent by construction, like Phase 1's control-group draw: reordering the file cannot
    change which of several tied customers is contacted.
    """
    import numpy as np
    import pandas as pd

    salt = f"{TIEBREAK_SALT}:{seed_from(run_id)}:"
    values = keys.to_numpy()
    draws = [
        int.from_bytes(
            hashlib.sha256((salt + ("" if pd.isna(value) else str(value))).encode("utf-8")).digest()[:8],
            "big",
        )
        for value in values.tolist()
    ]
    return np.asarray(draws, dtype=np.uint64)


def _numeric(values: pd.Series, name: str) -> np.ndarray:
    """A prediction column as float64; `ValueError` (counts only) if any row is missing or not finite."""
    import numpy as np
    import pandas as pd

    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan)
    bad = int((~np.isfinite(numeric)).sum())
    if bad:
        raise ValueError(
            f"{bad} of {len(numeric)} rows have no usable value in {name!r}; every scored row needs one."
        )
    return numeric
