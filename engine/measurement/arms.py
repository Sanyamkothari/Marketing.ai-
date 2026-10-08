"""Several offers against one shared control: per-arm summaries, measurement and policy value (Plan J M100).

DEC-668 recorded how uplift grows from one treatment to several without breaking a reader: every
existing field keeps its meaning as the **first treatment against the control**, and the reports gain an
optional `arms: tuple[ArmSummary, ...]`, one entry per treatment against the same, shared control. This
module is the Plan J side of that path (DEC-1310):

* **Summaries.** `arm_from_evaluation`, `arm_from_segments`, `arm_from_policy` and
  `arm_from_incrementality` turn the report a binary run would write for one treatment into its
  `ArmSummary`. The uplift flow builds each treatment's report with the same functions it uses for the
  first one (`evaluate_uplift`, `segment_report`, `recommend_policy`, `measure_incrementality`) on that
  treatment's customers and the control's, so `arms[0]` repeats the report's own fields by construction.
* **Measurement.** `measure_arms` is `measure_campaign`'s several-offer branch: each offer against the
  shared control through `measure_incrementality`, unchanged (the Newcombe interval, the maturity rule).
* **Policy value.** `arm_policy_value` measures, out of sample, what choosing an offer per customer is
  worth: the "best offer" policy (the arm with the highest predicted uplift x value, or no offer) and
  the first treatment's own policy, each by inverse probability weighting on the randomised hold-out,
  and their difference by a paired bootstrap within each arm (arm sizes fixed, DEC-605). It is recorded
  as `arm_policy_value.json`. It is how a later DEC may let the champion rule compare models of several
  treatments (DEC-1310 (h)); until then such a model is never promoted (`MULTI_ARM_PROMOTION_REFUSED`),
  because the champion rule (`engine/registry.py`, DEC-605, DEC-613) is frozen and compares one
  treatment's AUUC only.

`numpy` and `pandas` are imported inside function bodies so `import engine` stays fast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import PrimaryKey
    from engine.contracts import ModelVersion
    from engine.storage import Storage
    from engine.uplift.contracts import (
        ArmPolicyValue,
        ArmSummary,
        IncrementalityReport,
        PolicyRecommendation,
        SegmentReport,
        UpliftEvaluation,
    )

    FloatArray = npt.NDArray[np.float64]
    IntArray = npt.NDArray[np.int_]

__all__ = [
    "MULTI_ARM_CODES",
    "MULTI_ARM_PROMOTION_REFUSED",
    "PROMOTION_REFUSED_REASON",
    "PROMOTION_UNCHECKED_REASON",
    "arm_from_evaluation",
    "arm_from_incrementality",
    "arm_from_policy",
    "arm_from_segments",
    "arm_policy_value",
    "measure_arms",
    "promotion_refusal",
]

MULTI_ARM_PROMOTION_REFUSED: Final[str] = "MULTI_ARM_PROMOTION_REFUSED"
"""A model of several offers is never made champion, by the run or by hand (DEC-668 (4), DEC-1310 (h))."""

MULTI_ARM_CODES: Final[frozenset[str]] = frozenset({MULTI_ARM_PROMOTION_REFUSED})
"""M100's codes, joined into `engine.decide.codes.PLAN_J_CODES` at integration (one definition)."""

PROMOTION_REFUSED_REASON: Final[str] = (
    "Not promoted: this model chooses between several offers, and the champion rule compares models of "
    "one offer only. It stays a candidate: score with it by naming this version. Its value against the "
    "first offer alone is shown with its interval."
)
"""The plain reason a model of several offers stays a candidate (`MULTI_ARM_PROMOTION_REFUSED`)."""

PROMOTION_UNCHECKED_REASON: Final[str] = (
    "Not promoted: neither this uplift model's card nor its run configuration can be read, so it cannot "
    "be checked that it is a model of one offer. Try again; if it keeps happening, retrain the model."
)
"""The plain reason an uplift version whose card and run configuration are both unreadable is refused."""

_NO_HOLDOUT_FOR_ARM: Final[str] = (
    "This offer's measured hold-out uplift is not stored with the model, so no expected conversions are "
    "shown for it."
)


def promotion_refusal(storage: Storage, version: ModelVersion) -> str | None:
    """Why `version` may not be promoted by hand, or None when it may (Plan J M100, DEC-668 (4)).

    Only an uplift (AUUC) version can be a model of several offers. Its model card lists
    `treatment_levels`; when the card cannot be read, the run's `run_config.json`
    (`uplift.treatment_levels`) says the same. When neither can be read the promotion is refused too
    (:data:`PROMOTION_UNCHECKED_REASON`): the guard fails closed, because a lost or corrupt card must
    not let a model of several offers take the champion slot.
    """
    from engine.config import Metric, ResolvedConfig
    from engine.storage import StorageError
    from engine.uplift.contracts import UpliftModelCard
    from engine.uplift.flow import model_card_key

    if version.metric != Metric.AUUC:
        return None
    unreadable = (StorageError, OSError, ValueError)  # a pydantic ValidationError is a ValueError
    try:
        card = storage.read_model(model_card_key(version.predictor_key), UpliftModelCard)
    except unreadable:
        pass
    else:
        return PROMOTION_REFUSED_REASON if card.treatment_levels else None
    try:
        resolved = storage.read_model(version.run_config_key, ResolvedConfig)
    except unreadable:
        return PROMOTION_UNCHECKED_REASON
    return PROMOTION_REFUSED_REASON if resolved.config.uplift.treatment_levels else None


# ---------------------------------------------------------------------------
# One report per treatment -> its ArmSummary
# ---------------------------------------------------------------------------
def _conversions(t: npt.ArrayLike, y: npt.ArrayLike) -> tuple[int, int]:
    """`(treated conversions, control conversions)` of a 0/1 treatment and outcome."""
    import numpy as np

    treatment = np.asarray(t, dtype=np.int_)
    outcome = np.asarray(y, dtype=np.int_)
    return int(outcome[treatment == 1].sum()), int(outcome[treatment == 0].sum())


def arm_from_evaluation(
    level: str,
    position: int,
    control: str,
    evaluation: UpliftEvaluation,
    t: npt.ArrayLike,
    y: npt.ArrayLike,
) -> ArmSummary:
    """One treatment's evaluation (its customers and the control's hold-out rows) as an `ArmSummary`."""
    from engine.uplift.contracts import ArmSummary

    treated_conversions, control_conversions = _conversions(t, y)
    return ArmSummary(
        arm=level,
        position=position,
        control=control,
        rows=evaluation.rows_evaluated,
        treated_rows=evaluation.treated_rows,
        control_rows=evaluation.control_rows,
        treated_conversions=treated_conversions,
        control_conversions=control_conversions,
        treated_rate=evaluation.treated_rate,
        control_rate=evaluation.control_rate,
        effect=evaluation.average_treatment_effect,
        auuc=evaluation.auuc,
        qini_coefficient=evaluation.qini_coefficient,
        measurable_uplift=evaluation.measurable_uplift,
    )


def arm_from_segments(level: str, position: int, control: str, report: SegmentReport) -> ArmSummary:
    """The segments by one treatment's predicted uplift as an `ArmSummary`."""
    from engine.uplift.contracts import ArmSegmentCount, ArmSummary

    return ArmSummary(
        arm=level,
        position=position,
        control=control,
        rows=report.rows,
        segments=tuple(
            ArmSegmentCount(
                segment=summary.segment,
                rows=summary.rows,
                share_pct=summary.share_pct,
                mean_predicted_uplift=summary.mean_predicted_uplift,
            )
            for summary in report.segments
        ),
    )


def arm_from_policy(
    level: str,
    position: int,
    control: str,
    recommendation: PolicyRecommendation,
    *,
    note: str | None = None,
) -> ArmSummary:
    """What the run's policy would do with one treatment on its own, as an `ArmSummary`."""
    from engine.uplift.contracts import ArmSummary

    return ArmSummary(
        arm=level,
        position=position,
        control=control,
        rows=recommendation.rows,
        eligible_persuadables=recommendation.eligible_persuadables,
        contacts_recommended=recommendation.contacts_recommended,
        predicted_incremental_conversions=recommendation.predicted_incremental_conversions,
        expected_incremental_conversions=recommendation.expected_incremental_conversions,
        expected_net_value=recommendation.expected_net_value,
        note=note if note is not None else recommendation.money_note,
    )


def arm_from_incrementality(
    level: str, position: int, control: str, report: IncrementalityReport
) -> ArmSummary:
    """One offer's measured lift against the shared control as an `ArmSummary`."""
    from engine.uplift.contracts import ArmSummary, IncrementalityStatus

    immature = report.status is IncrementalityStatus.IMMATURE
    return ArmSummary(
        arm=level,
        position=position,
        control=control,
        rows=report.treated_rows + report.control_rows,
        treated_rows=report.treated_rows,
        control_rows=report.control_rows,
        treated_conversions=report.treated_conversions,
        control_conversions=report.control_conversions,
        treated_rate=report.treated_rate,
        control_rate=report.control_rate,
        effect=report.absolute_lift,
        incremental_conversions=report.incremental_conversions,
        p_value=report.p_value,
        note=report.summary if immature or report.absolute_lift is None else None,
    )


# ---------------------------------------------------------------------------
# measure_campaign's several-offer branch
# ---------------------------------------------------------------------------
def _offer_text(values: pd.Series) -> pd.Series:
    """Each offer cell as stripped text; a blank or null cell is no offer (NA)."""
    import pandas as pd

    text = values.astype("string").str.strip()
    return text.where(text.notna() & (text != ""), other=pd.NA)


def measure_arms(
    frame: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    arm_column: str,
    arms: Sequence[str] | None,
    control_level: str | None,
    run_id: str,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None,
    intended_column: str | None,
    bands: Sequence[str] | None,
    treatment_time: datetime,
    treatment_date_column: str | None,
    outcome_window_days: int | None,
    as_of: datetime,
    campaign_id: str | None,
) -> IncrementalityReport:
    """Every offer against the shared control; the first offer's report with `arms` (see `measure_campaign`).

    `arms` (the configured treatment levels after the control, `uplift.treatment_levels[1:]`) and
    `control_level` (`uplift.treatment_levels[0]`) are required: the report's own fields are the first
    configured offer's (DEC-668 (3)), never whichever offer happens to come first in the file, and every
    `ArmSummary.control` names the configured control level. Raises `ValueError` when either is missing,
    when `arm_column` is missing, when no treated customer names one of `arms`, or for anything
    `measure_incrementality` refuses on an offer's customers.
    """
    from engine.uplift.incrementality import _flag, measure_incrementality

    if arms is None or not arms:
        raise ValueError(
            "Measuring several offers needs `arms`, the configured offers in order "
            "(uplift.treatment_levels after the control): the report's own fields are the first one's."
        )
    if control_level is None or not str(control_level).strip():
        raise ValueError(
            "Measuring several offers needs `control_level`, the configured control value "
            "(the first of uplift.treatment_levels)."
        )
    if arm_column not in frame.columns:
        raise ValueError(f"The assignment has no column {arm_column!r} naming each customer's offer.")
    control = _flag(frame["control_group"]).to_numpy(dtype=bool)
    offer = _offer_text(frame[arm_column])
    levels = [str(level).strip() for level in arms]
    if not offer[~control].isin(levels).fillna(value=False).any():
        raise ValueError(f"No treated customer names one of the offers {levels} in {arm_column!r}.")
    reports: list[IncrementalityReport] = []
    for level in levels:
        mine = (offer == level).fillna(value=False).to_numpy(dtype=bool) & ~control
        reports.append(
            measure_incrementality(
                frame.loc[control | mine],
                outcomes,
                run_id=run_id,
                primary_key=primary_key,
                outcome_column=outcome_column,
                positive_label=positive_label,
                intended_column=intended_column,
                bands=bands,
                treatment_time=treatment_time,
                treatment_date_column=treatment_date_column,
                outcome_window_days=outcome_window_days,
                as_of=as_of,
                campaign_id=campaign_id,
            )
        )
    held = str(control_level).strip()
    summaries = tuple(
        arm_from_incrementality(level, position, held, report)
        for position, (level, report) in enumerate(zip(levels, reports, strict=True), start=1)
    )
    return reports[0].model_copy(update={"arms": summaries})


# ---------------------------------------------------------------------------
# The best-offer policy's value, out of sample
# ---------------------------------------------------------------------------
def _policy_contributions(
    policy: IntArray, arm: IntArray, outcome: FloatArray, share: FloatArray
) -> FloatArray:
    """Each row's term of the IPS estimate of `V(policy) - V(nobody)`: `w y (1[a = pi]/e_a - 1[a = 0]/e_0)`."""
    import numpy as np

    weight = 1.0 / share[arm]
    gets = (arm == policy).astype(np.float64)
    control = (arm == 0).astype(np.float64)
    contributions: FloatArray = outcome * (gets * weight - control / share[0])
    return contributions


def arm_policy_value(
    uplift: npt.ArrayLike,
    arm: npt.ArrayLike,
    y: npt.ArrayLike,
    *,
    levels: Sequence[str],
    values: npt.ArrayLike | None,
    value_column: str | None,
    samples: int,
    seed: int,
    run_id: str,
    causal: bool,
    now: datetime,
) -> ArmPolicyValue:
    """`arm_policy_value.json` from a hold-out of every arm (see the module docstring).

    `uplift` is the hold-out's predicted uplift per treatment (`rows x K`, a model that never saw these
    rows), `arm` each row's level code (0 the control) and `y` the 0/1 outcome. With `values` each
    conversion is weighted by the customer's value (a missing value counts as zero, never invented).
    Raises `ValueError` when an arm has no hold-out row: its share cannot weight anything.
    """
    import numpy as np

    from engine.uplift.contracts import ArmPolicyValue
    from engine.uplift.metrics import percentile_interval

    lift = np.asarray(uplift, dtype=np.float64)
    codes = np.asarray(arm, dtype=np.int_)
    outcome = np.asarray(y, dtype=np.float64)
    rows = len(codes)
    arms = len(levels) - 1
    if lift.ndim != 2 or lift.shape != (rows, arms) or len(outcome) != rows:
        raise ValueError("uplift must be rows x treatments, with one arm code and one outcome per row.")
    counts = np.bincount(codes, minlength=arms + 1)
    if len(counts) != arms + 1 or (counts == 0).any():
        raise ValueError("Every level needs hold-out customers for the policy's value to be measured.")
    share = counts / float(rows)
    missing = 0
    if values is None:
        weight = np.ones(rows, dtype=np.float64)
    else:
        raw = np.asarray(values, dtype=np.float64)
        missing = int((~np.isfinite(raw)).sum())
        weight = np.where(np.isfinite(raw), raw, 0.0)
    worth = lift * weight[:, None]
    best = np.argmax(worth, axis=1)
    best_offer = np.where(worth[np.arange(rows), best] > 0.0, best + 1, 0).astype(np.int_)
    first_only = np.where(worth[:, 0] > 0.0, 1, 0).astype(np.int_)
    weighted = outcome * weight
    best_terms = _policy_contributions(best_offer, codes, weighted, share)
    first_terms = _policy_contributions(first_only, codes, weighted, share)
    difference_terms = best_terms - first_terms

    rng = np.random.default_rng(seed)
    positions = [np.flatnonzero(codes == k) for k in range(arms + 1)]
    resampled = np.empty((samples, 3), dtype=np.float64)
    stacked = np.column_stack([best_terms, first_terms, difference_terms])
    for b in range(samples):
        total = np.zeros(3, dtype=np.float64)
        for members in positions:  # within each arm, arm sizes fixed (DEC-605)
            drawn = members[rng.integers(0, len(members), size=len(members))]
            total += stacked[drawn].sum(axis=0)
        resampled[b] = total / rows
    best_value = percentile_interval(float(best_terms.mean()), resampled[:, 0])
    first_value = percentile_interval(float(first_terms.mean()), resampled[:, 1])
    difference = percentile_interval(float(difference_terms.mean()), resampled[:, 2])
    better = difference.ci_low is not None and difference.ci_low > 0.0
    unit = "value" if values is not None else "conversions"
    if better:
        verdict = (
            f"Choosing the offer per customer adds {difference.value:.4f} {unit} per customer over the first "
            f"offer alone (95% interval {difference.ci_low:.4f} to {difference.ci_high:.4f})."
        )
    else:
        verdict = (
            "Choosing the offer per customer cannot be shown to beat the first offer alone on this hold-out "
            f"({difference.value:.4f} {unit} per customer, 95% interval "
            f"{_bound(difference.ci_low)} to {_bound(difference.ci_high)})."
        )
    return ArmPolicyValue(
        run_id=run_id,
        rows=rows,
        arms=tuple(levels),
        arm_rows=tuple(int(count) for count in counts),
        value_weighted=values is not None,
        value_column=value_column if values is not None else None,
        values_missing=missing,
        best_offer=best_value,
        first_treatment=first_value,
        difference=difference,
        best_offer_better=better,
        policy_shares={level: float(np.mean(best_offer == k)) for k, level in enumerate(levels)},
        bootstrap_samples=samples,
        costs_included=False,
        promotion=PROMOTION_REFUSED_REASON,
        promotion_code=MULTI_ARM_PROMOTION_REFUSED,
        causal=causal,
        summary=verdict,
        computed_at=now,
    )


def _bound(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def no_holdout_note() -> str:
    """Why a scoring run quotes no expected conversions for the second and later offers."""
    return _NO_HOLDOUT_FOR_ARM
