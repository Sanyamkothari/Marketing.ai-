"""The four-segment view of an uplift model's predictions (plan B §1.3, Stage C).

A propensity model sorts customers by how likely they are to convert. An uplift model can say
something more useful to whoever pays for the contact: whether contacting a customer *changes*
anything. Two numbers per customer - the predicted uplift `P(y | treated) − P(y | not treated)` and
the untreated probability `P(y | not treated)` - place every customer in exactly one of four boxes:

| Segment        | Rule, in this order                                      | Action              |
|----------------|----------------------------------------------------------|---------------------|
| persuadable    | uplift ≥ `persuadable_min_uplift`                        | treat (in budget)   |
| sleeping dog   | uplift ≤ `sleeping_dog_max_uplift`                       | never treat         |
| sure thing     | otherwise, `p_control` ≥ `sure_thing_min_probability`    | don't waste money   |
| lost cause     | otherwise                                                | don't bother        |

The two uplift cuts come from `uplift.segments`, whose validator guarantees
`sleeping_dog_max_uplift < persuadable_min_uplift`, so the first two rules can never both match and
the order between them is a formality. Boundaries are inclusive on the side the cut names: an uplift
exactly equal to `persuadable_min_uplift` is persuadable, one exactly equal to
`sleeping_dog_max_uplift` is a sleeping dog.

**The sure-thing cut.** Between the two uplift cuts the action barely matters, and what is left to
say is whether the customer converts anyway. When `sure_thing_min_probability` is not configured it
defaults to the *training base rate*: a customer more likely than average to convert untreated is a
sure thing, one less likely a lost cause. That default is data, not a constant, so
:func:`resolve_thresholds` records which one was used (`sure_thing_from_base_rate`) and every
artefact carries the cuts as applied - a scoring run replays the training run's cuts from the model
card instead of recomputing a base rate from the rows it scores.

**No invented rows.** A missing or non-finite prediction has no honest segment: the functions here
raise `ValueError` with counts only, never a value (plan section 13.7), rather than park the row in a
default box. An empty set of rows has no shares, so :func:`segment_report` refuses it too.

`numpy` is imported inside the function bodies, never at module level, so `import engine` stays
fast.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Final, Literal

from engine.uplift.contracts import (
    SEGMENT_ACTIONS,
    SEGMENT_LABELS,
    Segment,
    SegmentReport,
    SegmentSummary,
    SegmentThresholds,
)
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    import numpy as np

    from engine.uplift.config import UpliftSegmentsConfig

__all__ = [
    "SEGMENT_ORDER",
    "assign_segments",
    "resolve_thresholds",
    "segment_report",
]

_LOGGER = get_logger(__name__)

SEGMENT_ORDER: Final[tuple[Segment, ...]] = (
    Segment.PERSUADABLE,
    Segment.SURE_THING,
    Segment.LOST_CAUSE,
    Segment.SLEEPING_DOG,
)
"""The order `segments.json` lists the segments in (the contract's order, and the chart's)."""


def resolve_thresholds(config: UpliftSegmentsConfig, *, base_rate: float) -> SegmentThresholds:
    """The cuts as they will be applied: the configured ones, the sure-thing cut defaulted.

    `base_rate` is the outcome rate over the training rows. It is used only when
    `sure_thing_min_probability` is unset, and must then lie strictly between 0 and 1 - the same
    bounds the configuration puts on the setting it stands in for. A base rate of 0 or 1 would make
    every customer a sure thing or none, which is an artefact of the data, not a cut.
    """
    configured = config.sure_thing_min_probability
    if configured is not None:
        return SegmentThresholds(
            persuadable_min_uplift=config.persuadable_min_uplift,
            sleeping_dog_max_uplift=config.sleeping_dog_max_uplift,
            sure_thing_min_probability=configured,
            sure_thing_from_base_rate=False,
        )
    if not (math.isfinite(base_rate) and 0.0 < base_rate < 1.0):
        raise ValueError(
            "The training base rate must lie strictly between 0 and 1 to stand in for "
            "uplift.segments.sure_thing_min_probability; set that value explicitly instead."
        )
    return SegmentThresholds(
        persuadable_min_uplift=config.persuadable_min_uplift,
        sleeping_dog_max_uplift=config.sleeping_dog_max_uplift,
        sure_thing_min_probability=float(base_rate),
        sure_thing_from_base_rate=True,
    )


def assign_segments(uplift: np.ndarray, p_control: np.ndarray, thresholds: SegmentThresholds) -> np.ndarray:
    """The :class:`Segment` of every row, as an object array aligned to the inputs.

    The rules and their order are in the module docstring. Both arrays must be one-dimensional, of
    the same length and finite; otherwise `ValueError` (with counts, never values).
    """
    import numpy as np

    lift = _finite_vector(uplift, "uplift")
    base = _finite_vector(p_control, "p_control")
    if len(lift) != len(base):
        raise ValueError(
            f"uplift has {len(lift)} rows but p_control has {len(base)}; they must describe the same rows."
        )
    persuadable = lift >= thresholds.persuadable_min_uplift
    sleeping = lift <= thresholds.sleeping_dog_max_uplift
    sure = base >= thresholds.sure_thing_min_probability
    segments = np.empty(len(lift), dtype=object)
    # Assigned from the weakest rule to the strongest so the strongest wins; the two uplift cuts are
    # disjoint by the config validator, so "sleeping dog" and "persuadable" never compete.
    segments[:] = Segment.LOST_CAUSE
    segments[sure] = Segment.SURE_THING
    segments[sleeping] = Segment.SLEEPING_DOG
    segments[persuadable & ~sleeping] = Segment.PERSUADABLE
    return segments


def segment_report(
    uplift: np.ndarray,
    p_treated: np.ndarray,
    p_control: np.ndarray,
    segments: np.ndarray,
    thresholds: SegmentThresholds,
    *,
    run_id: str,
    computed_on: Literal["test", "scored"],
    causal: bool,
) -> SegmentReport:
    """`segments.json`: rows, share and mean predictions per segment, in :data:`SEGMENT_ORDER`.

    Every segment is listed, empty ones too - with `rows: 0` and null means, because the mean of
    nobody is not zero. `segments` is what :func:`assign_segments` returned (plain strings with the
    segment values are accepted as well).
    """
    started = time.perf_counter()
    lift = _finite_vector(uplift, "uplift")
    treated = _finite_vector(p_treated, "p_treated")
    control = _finite_vector(p_control, "p_control")
    labels = _segment_vector(segments)
    rows = len(lift)
    if {len(treated), len(control), len(labels)} != {rows}:
        raise ValueError(
            "uplift, p_treated, p_control and segments must have the same number of rows; got "
            f"{rows}, {len(treated)}, {len(control)} and {len(labels)}."
        )
    if rows == 0:
        raise ValueError("There are no rows to segment, so no segment shares can be reported.")

    summaries: list[SegmentSummary] = []
    for segment in SEGMENT_ORDER:
        mask = labels == segment.value
        count = int(mask.sum())
        summaries.append(
            SegmentSummary(
                segment=segment,
                label=SEGMENT_LABELS[segment],
                rows=count,
                share_pct=100.0 * count / rows,
                mean_predicted_uplift=float(lift[mask].mean()) if count else None,
                mean_p_treated=float(treated[mask].mean()) if count else None,
                mean_p_control=float(control[mask].mean()) if count else None,
                action=SEGMENT_ACTIONS[segment],
            )
        )
    unknown = rows - sum(summary.rows for summary in summaries)
    if unknown:
        raise ValueError(f"{unknown} of {rows} rows carry a value that is not one of the four segments.")

    _LOGGER.info(
        "uplift_segments %s",
        " ".join(f"{summary.segment.value}={summary.rows}" for summary in summaries),
    )
    log_stage(_LOGGER, "uplift_segments", rows=rows, seconds=time.perf_counter() - started)
    return SegmentReport(
        run_id=run_id,
        computed_on=computed_on,
        rows=rows,
        thresholds=thresholds,
        segments=tuple(summaries),
        causal=causal,
    )


# ---------------------------------------------------------------------------
# Input checks shared with the policy and actions modules
# ---------------------------------------------------------------------------
def _finite_vector(values: np.ndarray, name: str) -> np.ndarray:
    """`values` as a one-dimensional float64 array; `ValueError` if it is not one or not finite."""
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional; got {array.ndim} dimensions.")
    bad = int((~np.isfinite(array)).sum())
    if bad:
        raise ValueError(f"{bad} of {len(array)} rows have no finite {name}, so they cannot be segmented.")
    return array


def _segment_vector(segments: np.ndarray) -> np.ndarray:
    """`segments` as a one-dimensional object array of plain segment value strings."""
    import numpy as np

    array = np.asarray(segments, dtype=object)
    if array.ndim != 1:
        raise ValueError(f"segments must be one-dimensional; got {array.ndim} dimensions.")
    values = np.empty(len(array), dtype=object)
    values[:] = [item.value if isinstance(item, Segment) else item for item in array.tolist()]
    return values
