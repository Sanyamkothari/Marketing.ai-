"""Step 4 of a use case: measure the campaign a scoring run started (Plan H M83).

A scoring run of a use case that contacts customers already holds a random control group back
(`engine.stages.actions`: the `control_group` column, action "Control (hold out)"). Once the
campaign has run and its outcomes are known, the difference between the contacted and the held-back
customers is what the campaign *changed*. This module adds nothing to that measurement - it is
`engine.uplift.incrementality.measure_incrementality`, unchanged - but it decides three things around
it, as pure functions the API route (`api/routes/measure.py`) and the tests share:

* **Whether a use case has step 4 at all** (:func:`measure_offered`): only when its actions are
  contacts with customers (`actions.contacts_customers`, true unless the use-case YAML says false,
  as operational use cases that never contact a customer do), a control group is held
  back (`actions.control_group_fraction > 0`) and the use case is not an AI-written-text one.
  Decided by configuration, never by use-case id.
* **What the result says in one plain line** (:func:`campaign_verdict`): "The campaign added about N
  conversions", "No clear effect yet" (the interval includes zero), "Outcome window not over yet",
  and so on - always from the report's own numbers, never a figure of its own. Which way round an
  outcome counts (a churn campaign exists to make its outcome *rarer*) is the pilot value view's
  rule, `engine.pilot.roi.outcome_is_good_by_default`, passed in by the caller.
* **Whether there is enough to learn from** (:func:`learn_readiness`) and **the experiment file an
  uplift model learns from** (:func:`build_experiment_frame`): the scored run's own input rows, a
  0/1 treatment column (1 = contacted, 0 = held back, read from the scores' control group - the rows
  whose action is "Control (hold out)"), and the outcome as the use case's target. Suppressed rows
  were never eligible for either arm and are left out; for an uplift run only the customers its
  policy intended to treat are kept, as the incrementality report compares them. Treatment is
  "received the campaign's actions" (intent to treat), which is what the random hold-out makes
  causal: a Phase 1 run's low band ("No action") stays in the contacted arm, exactly as the
  incrementality report counts it.

`pandas` is imported inside the function bodies, never at module level, so `import engine` stays fast.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from pydantic import Field

from engine.config import AiType, PrimaryKey, UseCaseConfig, key_columns
from engine.contracts import Artefact
from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus

if TYPE_CHECKING:
    import pandas as pd

    from engine.uplift.config import UpliftConfig

__all__ = [
    "CAMPAIGN_MEASURE_FILENAME",
    "TREATMENT_COLUMN",
    "CampaignMeasure",
    "CampaignVerdict",
    "LearnReadiness",
    "VerdictKind",
    "build_experiment_frame",
    "campaign_verdict",
    "detect_outcome_column",
    "learn_readiness",
    "measure_offered",
    "treatment_column_for",
]

CAMPAIGN_MEASURE_FILENAME: Final[str] = "campaign_measure.json"
"""`runs/<run_id>/campaign_measure.json`: which outcomes file measured the run, and the uplift run
learned from it, so step 4 reopens where the user left it."""

TREATMENT_COLUMN: Final[str] = "contacted"
"""The treatment column of the experiment file; one of the uplift checks' default hints."""

_CONTROL_COLUMN: Final[str] = "control_group"
_SUPPRESSED_COLUMN: Final[str] = "suppressed_reason"
_INTENDED_COLUMN: Final[str] = "intended_treatment"


# ---------------------------------------------------------------------------
# Does this use case have step 4?
# ---------------------------------------------------------------------------
def measure_offered(config: UseCaseConfig) -> bool:
    """True when a campaign run from this use case's list can be measured against its control group.

    The holdout share is the effective one (`engine.holdout.spec.effective_holdout_fraction`, Plan J
    M92): `actions.control_group_fraction` under `scope: run`, `actions.holdout.fraction` under a
    persistent scope.
    """
    from engine.holdout.spec import effective_holdout_fraction

    return (
        config.ai_type is not AiType.GENERATIVE
        and config.actions.contacts_customers
        and effective_holdout_fraction(config.actions) > 0.0
    )


# ---------------------------------------------------------------------------
# The stored record
# ---------------------------------------------------------------------------
class CampaignMeasure(Artefact):
    """`campaign_measure.json`: how step 4 measured this run, and what it learned from it."""

    run_id: str = Field(description="The scoring run whose campaign was measured.")
    upload_id: str = Field(description="The outcomes file.")
    file_name: str = Field(description="Its name, as uploaded.")
    outcome_column: str = Field(description="The outcome column of that file.")
    positive_label: str | None = Field(default=None, description="The value that counts as a conversion.")
    outcome_window_days: int | None = Field(default=None, description="Days the outcome is counted over.")
    outcome_named: bool = Field(
        default=False,
        description="True when the person named the column; false when it was found in the file.",
    )
    measured_at: datetime = Field(description="When it was measured.")
    uplift_run_id: str | None = Field(default=None, description="The uplift training run learned from it.")


# ---------------------------------------------------------------------------
# Which column is the outcome?
# ---------------------------------------------------------------------------
def detect_outcome_column(
    columns: list[str], *, primary_key: PrimaryKey, target_column: str, label_name: str | None = None
) -> str:
    """The outcome column of an outcomes file: the use case's own target, else the one other column.

    Raises `ValueError` with a plain sentence (column names only, never values) when the file has
    no column besides the customer id, or several and none is the use case's outcome.
    """
    keys = set(key_columns(primary_key))
    missing = [name for name in key_columns(primary_key) if name not in columns]
    if missing:
        raise ValueError(
            f"The file has no {', '.join(repr(name) for name in missing)} column: it needs the same customer "
            f"id column as the list the campaign was sent from."
        )
    others = [name for name in columns if name not in keys]
    for wanted in (target_column, label_name):
        if wanted and wanted in others:
            return wanted
    if len(others) == 1:
        return others[0]
    if not others:
        raise ValueError(
            "The file has only the customer id. Add one column saying whether each customer responded "
            "(1 or 0)."
        )
    raise ValueError(
        f"The file has {len(others)} columns besides the customer id ({', '.join(others)}). Keep only the "
        f"customer id and one column saying whether each customer responded, or name it "
        f"{target_column!r}."
    )


# ---------------------------------------------------------------------------
# The plain verdict
# ---------------------------------------------------------------------------
class VerdictKind(StrEnum):
    ADDED = "added"
    PREVENTED = "prevented"
    HARMED = "harmed"
    NO_CLEAR_EFFECT = "no_clear_effect"
    TOO_EARLY = "too_early"
    NO_CONTROL = "no_control"
    NOTHING_MATCHED = "nothing_matched"
    NOT_ENOUGH = "not_enough"


class CampaignVerdict(Artefact):
    """One big plain line and one sentence under it, read off an incrementality report."""

    kind: VerdictKind = Field(description="Which of the plain verdicts applies.")
    headline: str = Field(description="The one line shown large.")
    detail: str = Field(description="One plain sentence under it.")
    amount: int | None = Field(default=None, description="About how many outcomes the campaign changed.")
    likely_low: int | None = Field(default=None, description="The low end of the likely range.")
    likely_high: int | None = Field(default=None, description="The high end of the likely range.")
    results_on: date | None = Field(default=None, description="When every outcome will be in.")


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _day(moment: date) -> str:
    """`30 Jul 2026`: the product's date style (`DATE_LOCALE` en-IN, short month)."""
    return f"{moment.day} {moment:%b %Y}"


def campaign_verdict(
    report: IncrementalityReport, *, outcome_is_good: bool, outcome_label: str | None = None
) -> CampaignVerdict:
    """The plain verdict of `report`. `outcome_is_good` is false when the campaign exists to prevent it."""
    if not report.causal:
        return CampaignVerdict(
            kind=VerdictKind.NO_CONTROL,
            headline="This run held nobody back",
            detail=(
                "Without customers held back there is nothing to compare with, so what the campaign "
                "changed cannot be measured."
            ),
        )
    if report.status is IncrementalityStatus.IMMATURE:
        when = report.results_available_on
        return CampaignVerdict(
            kind=VerdictKind.TOO_EARLY,
            headline="Outcome window not over yet",
            detail=(
                f"Customers still have time to respond, so anything counted now would be an early look, "
                f"not a result. Upload the outcomes again on or after {_day(when)}."
                if when is not None
                else "Customers still have time to respond, so anything counted now would be an early look, "
                "not a result. Upload the outcomes again once it is over."
            ),
            results_on=when,
        )
    if report.treated_rows == 0 and report.control_rows == 0:
        return CampaignVerdict(
            kind=VerdictKind.NOTHING_MATCHED,
            headline="No customer in this file is on the list",
            detail=(
                "None of the customer ids in the file match this run's list. Check it is the outcomes of "
                "this campaign, with the same customer id column."
            ),
        )
    incremental = report.incremental_conversions
    lift = report.absolute_lift
    if incremental is None or lift is None or report.treated_rate is None or report.control_rate is None:
        return CampaignVerdict(
            kind=VerdictKind.NOT_ENOUGH,
            headline="Not enough results yet",
            detail=(
                f"Outcomes are needed for both contacted and held-back customers; this file has "
                f"{report.treated_rows:,} contacted and {report.control_rows:,} held back."
            ),
        )
    if not lift.excludes_zero or incremental.ci_low is None or incremental.ci_high is None:
        return CampaignVerdict(
            kind=VerdictKind.NO_CLEAR_EFFECT,
            headline="No clear effect yet",
            detail=(
                "Contacted and held-back customers did about the same, so the difference could be chance. "
                "The test can only show a change at or above its detectable effect, the smallest change a "
                "group of this size can see. A bigger group or a longer wait lowers it."
            ),
        )
    amount = round(abs(incremental.value))
    ends = sorted((round(abs(incremental.ci_low)), round(abs(incremental.ci_high))))
    low, high = ends[0], ends[1]
    likely = f"Likely between {low:,} and {high:,}."
    counted = f" Counted: {outcome_label}." if outcome_label else ""
    went_up = incremental.value > 0
    if outcome_is_good and went_up:
        kind = VerdictKind.ADDED
        headline = f"The campaign added about {amount:,} {_plural(amount, 'conversion', 'conversions')}"
    elif not outcome_is_good and not went_up:
        kind = VerdictKind.PREVENTED
        headline = f"The campaign prevented about {amount:,} {_plural(amount, 'case', 'cases')}"
    elif outcome_is_good:
        kind = VerdictKind.HARMED
        headline = f"The campaign cost about {amount:,} {_plural(amount, 'conversion', 'conversions')}"
        likely = "Contacted customers did worse than the ones held back. " + likely
    else:
        kind = VerdictKind.HARMED
        headline = f"The campaign caused about {amount:,} more {_plural(amount, 'case', 'cases')}"
        likely = "Contacted customers did worse than the ones held back. " + likely
    return CampaignVerdict(
        kind=kind,
        headline=headline,
        detail=likely + counted,
        amount=amount,
        likely_low=low,
        likely_high=high,
    )


# ---------------------------------------------------------------------------
# Enough to learn who to contact next time?
# ---------------------------------------------------------------------------
class LearnReadiness(Artefact):
    """Whether an uplift model can be learned from this measured campaign, and why not."""

    ready: bool = Field(description="True when both groups are large enough to learn from.")
    reason: str = Field(description="One plain sentence.")
    min_rows: int = Field(description="Customers each group needs (uplift.min_arm_rows).")
    min_positives: int = Field(description="Responders each group needs (uplift.min_arm_positives).")


def learn_readiness(report: IncrementalityReport | None, uplift: UpliftConfig) -> LearnReadiness:
    """Ready when the campaign was measured, every outcome is in, and both groups clear the uplift floors."""
    rows, positives = uplift.min_arm_rows, uplift.min_arm_positives

    def verdict(ready: bool, reason: str) -> LearnReadiness:
        return LearnReadiness(ready=ready, reason=reason, min_rows=rows, min_positives=positives)

    if report is None:
        return verdict(False, "Measure the campaign first.")
    if not report.causal:
        return verdict(False, "This run held nobody back, so there is nothing to learn from.")
    if report.status is IncrementalityStatus.IMMATURE or report.rows_immature:
        return verdict(False, "Wait until every customer's outcome is in.")
    enough = (
        report.treated_rows >= rows
        and report.control_rows >= rows
        and report.treated_conversions >= positives
        and report.control_conversions >= positives
    )
    if not enough:
        return verdict(
            False,
            f"To learn who to contact next time, each group needs at least {rows:,} customers and "
            f"{positives:,} who responded. This campaign has {report.treated_rows:,} contacted "
            f"({report.treated_conversions:,} responded) and {report.control_rows:,} held back "
            f"({report.control_conversions:,} responded).",
        )
    return verdict(True, "There is enough data to learn which customers the campaign really changes.")


# ---------------------------------------------------------------------------
# The experiment file
# ---------------------------------------------------------------------------
def treatment_column_for(columns: list[str]) -> str:
    """`contacted`, or a name the file does not use yet."""
    name, number = TREATMENT_COLUMN, 1
    while name in columns:
        number += 1
        name = f"{TREATMENT_COLUMN}_{number}"
    return name


def build_experiment_frame(
    inputs: pd.DataFrame,
    scores: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None,
    target_column: str,
    treatment_column: str,
) -> pd.DataFrame:
    """The scored run's input rows with a 0/1 `treatment_column` and the 0/1 outcome as `target_column`.

    Kept: rows eligible for the campaign (not suppressed; for an uplift run, intended for treatment)
    whose customer has a known outcome. Raises `ValueError` for a missing column, a repeated key or a
    non-binary outcome, with the incrementality report's own messages.
    """
    import numpy as np

    from engine.uplift.incrementality import (
        _coerce_outcome,
        _flag,
        _joined_keys,
        _require_unique,
        _suppressed,
    )

    columns = key_columns(primary_key)
    for frame, what in (
        (inputs, "The run's input"),
        (scores, "The run's scores"),
        (outcomes, "The outcomes file"),
    ):
        missing = [name for name in columns if name not in frame.columns]
        if missing:
            raise ValueError(f"{what} has no column {', '.join(repr(name) for name in missing)}.")
    if _CONTROL_COLUMN not in scores.columns:
        raise ValueError(f"The run's scores have no {_CONTROL_COLUMN!r} column.")
    if outcome_column not in outcomes.columns:
        raise ValueError(f"The outcomes file has no column {outcome_column!r}.")

    score_keys = _joined_keys(scores, columns)
    input_keys = _joined_keys(inputs, columns)
    outcome_keys = _joined_keys(outcomes, columns)
    _require_unique(score_keys, what="The run's scores")
    _require_unique(input_keys, what="The run's input")
    _require_unique(outcome_keys, what="The outcomes file")

    scores = scores.reset_index(drop=True)
    eligible = np.ones(len(scores.index), dtype=bool)
    if _SUPPRESSED_COLUMN in scores.columns:
        eligible &= ~_suppressed(scores[_SUPPRESSED_COLUMN]).to_numpy(dtype=bool)
    if _INTENDED_COLUMN in scores.columns:
        eligible &= _flag(scores[_INTENDED_COLUMN]).to_numpy(dtype=bool)
    held_back = _flag(scores[_CONTROL_COLUMN]).to_numpy(dtype=bool)
    arm = _series(np.where(held_back, 0, 1)[eligible], score_keys.to_numpy()[eligible])

    converted = _coerce_outcome(outcomes[outcome_column], positive_label)
    known = converted.notna().to_numpy(dtype=bool)
    outcome = _series(
        converted.to_numpy()[known].astype(bool).astype(np.int_), outcome_keys.to_numpy()[known]
    )

    treatment = input_keys.map(arm)
    outcome_values = input_keys.map(outcome)
    keep = (treatment.notna() & outcome_values.notna()).to_numpy(dtype=bool)
    result = inputs.reset_index(drop=True).loc[keep].copy()
    result[treatment_column] = treatment[keep].astype(np.int_).to_numpy()
    result[target_column] = outcome_values[keep].astype(np.int_).to_numpy()
    return result.reset_index(drop=True)


def _series(values: Any, index: Any) -> pd.Series:
    """A Series keyed by the text key, for `Series.map` (the keys are unique: checked above)."""
    import pandas as pd

    return pd.Series(values, index=pd.Index(index))
