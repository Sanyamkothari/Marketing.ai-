"""Learn from the last cycle (Plan J M106, DEC-1316): the experiment the next model learns from.

Step 4's "Learn who to contact next time" trains a campaign-effect model on what the last campaign
did. Before M106 its experiment file (`engine.uplift.measure.build_experiment_frame`) compared every
eligible customer who was not held back with the ones who were: intent to treat for the *list as a
whole*, so a Phase 1 run's lowest band, which was never contacted, sat in the "contacted" arm. A run
that engaged the holdout service (M92: a control group kept per use case or across use cases, or an
explore share) records, per customer, whether the cycle contacted them (`treated`) and the chance it
had of doing so (`treatment_probability`) in `holdout_assignment.parquet`. This module builds the
experiment from that record, using **only variation the engine randomised**.

**Which rows enter, and why.** A scored customer enters only when their contact was decided at
random, that is when the recorded chance of contact is strictly between 0 and 1:

* *on the list* (`selected` before the hold-back, `engine.holdout.assign.selection_masks`): contacted
  unless the control group drew them, so the chance is `1 - h`;
* *outside the list*, eligible and not a predicted sleeping dog: contacted only when the explore
  share drew them, so the chance is `(1 - h) * explore_fraction`.

Everyone else is left out and counted, by reason: customers who could not be contacted at all
(suppressed: consent, contactability, a recent contact), predicted sleeping dogs (the list's rule
never contacts them, so nothing shows what a contact would do), customers with no chance of contact
(outside the list in a cycle without an explore share), and customers with no outcome in the file.
The treatment is the logged contact, `treated`: who the campaign **meant** to contact. A contact file
(M103), when the run has one, is reported beside it and never replaces it: who was actually reached is
not random (a wrong number, an opted-out device), and swapping it in would let that decide the
comparison.

**Balanced within each group.** The two groups were randomised with very different chances (nine in
ten on the list, a few in a hundred outside it), so pooling them as they are would make "who was
contacted" predictable from the customers' own data, which is exactly what the uplift checks'
randomness test refuses (`TREATMENT_NOT_RANDOM`), and would mislead every learner that assumes one
chance of treatment. So in each group the smaller side (contacted or not) enters whole, and the
larger side is cut to the same number by the smallest salted draws on the customer id
(`sha256("learn:<run seed>:<key>")`, independent of the customer's data and outcome). Inside each group
the kept customers are then a random half and half, so across the frame the chance of contact is one
half for everyone: the frame is a randomised experiment, and the learners need no weights.

**When there is nothing to learn from (`LEARN_NO_OVERLAP`).** A cycle that left customers outside its
list with no chance of contact (explore share 0) says nothing about what a contact does for them; a
model learned from it would guess there. Learning is refused, with the reason, unless every eligible
customer who is not a predicted sleeping dog was on the list (then the control group alone
randomised everyone, and the data is already randomised). The rule reads `holdout_assignment.json`
only (:func:`overlap_refusal`), so step 4's screen and the learn route give the same answer.

**Predicted against measured, for the Approver.** The model that chose the last list predicted, per
customer, what a contact would change (the scores' `uplift` column). On the frame's rows - customers
the model scored before the campaign, so its predictions are out of sample - the predicted change is
set against the measured one per tenth, with intervals from the same resampling as the training run's
own calibration (`engine.uplift.metrics.calibration_by_decile`, M96). It is stored with the learn
record and shown beside the challenger the learning produced (`ApprovalItem.live_calibration`). A
list ranked by a risk score predicts no change, so there is nothing to compare, and the block is null
with that reason.

Every computation is vectorised except the salted draws, one `sha256` per customer on the larger side
of each group (about a second per million). `pandas` and `numpy` are imported inside the function
bodies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import PrimaryKey, key_columns
from engine.contracts import Artefact
from engine.uplift.contracts import ConfidenceValue

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import ModelVersion
    from engine.holdout.spec import HoldoutAssignmentReport
    from engine.measurement.reconcile import ContactReadout
    from engine.storage import Storage

__all__ = [
    "GROUP_ON_LIST",
    "GROUP_OUTSIDE_LIST",
    "LEARN_CODES",
    "LEARN_NO_OVERLAP",
    "LEARN_RECORD_FILENAME",
    "PREDICTED_EFFECT_COLUMN",
    "Delivery",
    "LearnFrame",
    "LearnGroup",
    "LearnRecord",
    "LeftOut",
    "LiveCalibration",
    "LiveDecile",
    "build_randomised_frame",
    "delivery_from",
    "live_calibration",
    "live_calibration_for",
    "overlap_refusal",
    "read_learn_record",
]

LEARN_RECORD_FILENAME: Final[str] = "learned_from.json"
"""`runs/<uplift run>/learned_from.json`: which cycle the run learned from, which rows, and why."""

LEARN_NO_OVERLAP: Final[str] = "LEARN_NO_OVERLAP"
LEARN_CODES: Final[frozenset[str]] = frozenset({LEARN_NO_OVERLAP})
"""M106's one code: `POST /runs/{id}/measure/learn` answers 409 with it (`api.routes.measure`)."""

PREDICTED_EFFECT_COLUMN: Final[str] = "uplift"
"""The scores' predicted change from a contact (an uplift scoring run's own column)."""

GROUP_ON_LIST: Final[str] = "on_the_list"
GROUP_OUTSIDE_LIST: Final[str] = "outside_the_list"
_DRAW_LABEL: Final[str] = "learn"

_SUPPRESSED_COLUMN: Final[str] = "suppressed_reason"
_TREATED: Final[str] = "treated"
_PROBABILITY: Final[str] = "treatment_probability"

Group = Literal["on_the_list", "outside_the_list"]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
class LearnGroup(Artefact):
    """One group whose contact was randomised: who was in it, and who entered the frame."""

    group: Group = Field(description="`on_the_list`, or `outside_the_list` (the explore share's group).")
    chance_of_contact: float = Field(description="The recorded chance of contact of every customer in it.")
    contacted: int = Field(
        description="Customers in the group, with an outcome, whom the campaign contacted."
    )
    not_contacted: int = Field(description="Customers in the group, with an outcome, whom it did not.")
    entered_contacted: int = Field(description="Of the contacted, how many entered the frame.")
    entered_not_contacted: int = Field(description="Of the not contacted, how many entered the frame.")


class LeftOut(Artefact):
    """Scored customers who did not enter the frame, by reason (each customer counted once)."""

    not_eligible: int = Field(description="Could not be contacted at all (suppressed).")
    never_contacted_by_the_rule: int = Field(
        description="Predicted to be put off by a contact, which the list's rule never makes."
    )
    no_chance_of_contact: int = Field(
        description="Eligible, but with no chance of being contacted (or none of being left alone)."
    )
    no_outcome: int = Field(description="Randomised, but the outcomes file has no outcome for them.")
    cut_to_balance: int = Field(description="Randomised, with an outcome, drawn out to balance their group.")


class Delivery(Artefact):
    """What the run's contact file (M103) says about who was really reached; reported, never learned from."""

    campaign_id: str = Field(description="The campaign whose contact file it is.")
    contact_rate: float | None = Field(description="Share of the customers meant to be contacted who were.")
    contamination: float | None = Field(description="Share of the held-back customers contacted anyway.")
    sentence: str = Field(description="One plain sentence.")


class LiveDecile(Artefact):
    """One tenth of the frame, ranked by the change the model in use predicted, highest first."""

    decile: int = Field(description="1 is the tenth with the highest predicted change.")
    rows: int = Field(description="Customers in the tenth.")
    predicted: float = Field(description="Mean predicted change from a contact (a share, 0.05 = 5 points).")
    measured: ConfidenceValue | None = Field(
        description="Contacted rate minus not-contacted rate in the tenth, with its 95% range; null when a side is empty."
    )
    inside_range: bool | None = Field(
        description="Whether the measured range holds the prediction; null without one."
    )


class LiveCalibration(Artefact):
    """Predicted against measured change by tenth, on the last campaign's randomised customers."""

    source_run_id: str = Field(description="The scoring run whose list went out.")
    model_id: str | None = Field(description="The model that chose that list (whose predictions these are).")
    rows: int = Field(description="Customers compared: the learn frame's rows.")
    deciles: tuple[LiveDecile, ...] = Field(description="Up to ten rows, highest predicted change first.")
    deciles_with_range: int = Field(description="Tenths whose measured change has a 95% range.")
    deciles_inside: int = Field(description="Of those, how many hold the prediction.")
    matches: bool | None = Field(
        description="True when at least 8 in 10 of those tenths hold the prediction; null with fewer than five."
    )
    average_gap: float | None = Field(
        description="Row-weighted mean of |measured - predicted|; null when none measured."
    )
    resamples: int = Field(description="Resamples behind each range.")
    summary: str = Field(description="One plain sentence for the Approver.")


class LearnRecord(Artefact):
    """`learned_from.json`: the last cycle a campaign-effect model learned from, and exactly which rows."""

    source_run_id: str = Field(description="The scoring run whose campaign was learned from.")
    uplift_run_id: str | None = Field(description="The training run started on the frame.")
    model_id: str | None = Field(description="The model that chose the source run's list.")
    treatment_column: str = Field(
        description="The 0/1 column of the frame: 1 = the campaign meant to contact them."
    )
    rows: int = Field(description="Customers in the frame.")
    contacted: int = Field(description="Of them, contacted (half of the frame, by construction).")
    not_contacted: int = Field(description="Of them, not contacted.")
    explore_fraction: float = Field(description="The cycle's explore share.")
    holdout_fraction: float = Field(description="The cycle's control-group share.")
    groups: tuple[LearnGroup, ...] = Field(description="The randomised groups and who entered from each.")
    left_out: LeftOut = Field(description="Scored customers left out, by reason.")
    delivery: Delivery | None = Field(description="The contact file's readout, when the run has one.")
    calibration: LiveCalibration | None = Field(description="Predicted against measured change, by tenth.")
    calibration_reason: str | None = Field(description="Why `calibration` is null; null when it is given.")
    summary: str = Field(description="Which customers the model learned from and why, in one sentence.")
    notes: tuple[str, ...] = Field(default=(), description="Plain sentences on who was left out and why.")
    created_at: AwareDatetime = Field(description="When the frame was built.")


@dataclass(frozen=True)
class LearnFrame:
    """The experiment frame (the run's input rows plus treatment and outcome) and its record."""

    frame: pd.DataFrame
    record: LearnRecord


# ---------------------------------------------------------------------------
# Is there anything to learn from?
# ---------------------------------------------------------------------------
def overlap_refusal(report: HoldoutAssignmentReport) -> str | None:
    """Why this cycle cannot teach a model what a contact does, or None when it can.

    Refused when the cycle had no explore share and left eligible customers outside its list who
    were not predicted sleeping dogs (`explore_candidates`, counted by M92 whatever the share): for
    them, nobody was contacted at random. With no such customer the control group randomised
    everyone the list could contact, so the cycle is already randomised.
    """
    if report.spec.explore_fraction > 0.0 or report.explore_candidates == 0:
        return None
    count = report.explore_candidates
    return (
        f"{count:,} {'customer' if count == 1 else 'customers'} who could have been contacted "
        f"{'was' if count == 1 else 'were'} outside the list, and the last cycle contacted none of them at "
        "random, so nothing shows what a contact would change for them. Set an explore share "
        "(actions.explore_fraction, 5% or more) for the next cycle and learn from that one, or train on a "
        "file from a campaign that chose its customers at random."
    )


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------
def build_randomised_frame(
    inputs: pd.DataFrame,
    scores: pd.DataFrame,
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None,
    target_column: str,
    treatment_column: str,
    run_id: str,
    model_id: str | None,
    holdout_fraction: float,
    explore_fraction: float,
    samples: int,
    now: datetime,
    delivery: Delivery | None = None,
) -> LearnFrame:
    """The run's input rows whose contact was randomised, balanced per group (module docstring).

    `scores` and `assignment` are the scoring run's `scores.parquet` and `holdout_assignment.parquet`,
    `config` its resolved use case (for the one definition of "on the list"), `outcomes` the measured
    outcomes file. Raises `ValueError` with a plain sentence for a missing column, a repeated key, a
    non-binary outcome, or a scored customer the input does not hold.
    """
    import numpy as np
    import pandas as pd

    from engine.holdout.assign import selection_masks
    from engine.uplift.incrementality import _coerce_outcome, _joined_keys, _require_unique, _suppressed

    columns = key_columns(primary_key)
    for frame, what in (
        (inputs, "The run's input"),
        (scores, "The run's scores"),
        (assignment, "The run's control-group record"),
        (outcomes, "The outcomes file"),
    ):
        missing = [name for name in columns if name not in frame.columns]
        if missing:
            raise ValueError(f"{what} has no column {', '.join(repr(name) for name in missing)}.")
    for name in (_TREATED, _PROBABILITY):
        if name not in assignment.columns:
            raise ValueError(f"The run's control-group record has no {name!r} column.")
    if _SUPPRESSED_COLUMN not in scores.columns:
        raise ValueError(f"The run's scores have no {_SUPPRESSED_COLUMN!r} column.")
    if outcome_column not in outcomes.columns:
        raise ValueError(f"The outcomes file has no column {outcome_column!r}.")

    scores = scores.reset_index(drop=True)
    score_keys = _joined_keys(scores, columns)
    input_keys = _joined_keys(inputs, columns)
    assignment_keys = _joined_keys(assignment, columns)
    outcome_keys = _joined_keys(outcomes, columns)
    _require_unique(score_keys, what="The run's scores")
    _require_unique(input_keys, what="The run's input")
    _require_unique(assignment_keys, what="The run's control-group record")
    _require_unique(outcome_keys, what="The outcomes file")

    # The assignment joined to the scores by key, never by position; a scored customer it does not
    # list had no recorded chance of contact.
    position = pd.Index(assignment_keys.to_numpy()).get_indexer(pd.Index(score_keys.to_numpy()))
    listed = position >= 0
    taken = np.where(listed, position, 0)
    probability = np.where(listed, assignment[_PROBABILITY].to_numpy(dtype=np.float64)[taken], 0.0)
    treated = listed & assignment[_TREATED].to_numpy(dtype=bool)[taken]

    eligible = ~_suppressed(scores[_SUPPRESSED_COLUMN]).to_numpy(dtype=bool)
    selected, sleeping = selection_masks(scores, config)
    chance = (probability > 0.0) & (probability < 1.0)
    randomised = eligible & ~sleeping & chance

    converted = _coerce_outcome(outcomes[outcome_column], positive_label)
    outcome_by_key = pd.Series(converted.to_numpy(), index=pd.Index(outcome_keys.to_numpy()))
    outcome = score_keys.map(outcome_by_key)
    known = outcome.notna().to_numpy(dtype=bool)
    pool = randomised & known

    seed = _seed(run_id)
    keep = np.zeros(len(scores.index), dtype=bool)
    groups: list[LearnGroup] = []
    for name, members in ((GROUP_ON_LIST, pool & selected), (GROUP_OUTSIDE_LIST, pool & ~selected)):
        rows = np.flatnonzero(members)
        if rows.size == 0:
            continue
        contacted = rows[treated[rows]]
        alone = rows[~treated[rows]]
        smaller, larger = (contacted, alone) if contacted.size <= alone.size else (alone, contacted)
        drawn = _smallest_draws(score_keys.to_numpy()[larger], smaller.size, seed=seed)
        keep[smaller] = True
        keep[larger[drawn]] = True
        groups.append(
            LearnGroup(
                group=name,  # type: ignore[arg-type]
                chance_of_contact=float(probability[rows[0]]),
                contacted=int(contacted.size),
                not_contacted=int(alone.size),
                entered_contacted=int(keep[contacted].sum()),
                entered_not_contacted=int(keep[alone].sum()),
            )
        )

    kept_keys = score_keys.to_numpy()[keep]
    in_input = pd.Index(input_keys.to_numpy()).isin(kept_keys)
    if int(in_input.sum()) != int(keep.sum()):
        absent = int(keep.sum()) - int(in_input.sum())
        raise ValueError(
            f"The run's input does not hold {absent:,} of the customers it scored, so the experiment "
            "cannot be built from it."
        )
    treatment_by_key = pd.Series(treated[keep].astype(np.int_), index=pd.Index(kept_keys))
    outcome_kept = pd.Series(outcome.to_numpy()[keep].astype(bool).astype(np.int_), index=pd.Index(kept_keys))
    result = inputs.reset_index(drop=True).loc[in_input].copy()
    frame_keys = input_keys.to_numpy()[in_input]
    result[treatment_column] = treatment_by_key.loc[frame_keys].to_numpy()
    result[target_column] = outcome_kept.loc[frame_keys].to_numpy()
    result = result.reset_index(drop=True)

    left_out = LeftOut(
        not_eligible=int((~eligible).sum()),
        never_contacted_by_the_rule=int((eligible & sleeping).sum()),
        no_chance_of_contact=int((eligible & ~sleeping & ~chance).sum()),
        no_outcome=int((randomised & ~known).sum()),
        cut_to_balance=int((pool & ~keep).sum()),
    )
    contacted_rows = int(result[treatment_column].sum())
    calibration, reason = _calibration_for(
        scores,
        score_keys,
        frame_keys,
        result[treatment_column].to_numpy(),
        result[target_column].to_numpy(),
        source_run_id=run_id,
        model_id=model_id,
        samples=samples,
        seed=seed,
    )
    record = LearnRecord(
        source_run_id=run_id,
        uplift_run_id=None,
        model_id=model_id,
        treatment_column=treatment_column,
        rows=len(result.index),
        contacted=contacted_rows,
        not_contacted=len(result.index) - contacted_rows,
        explore_fraction=float(explore_fraction),
        holdout_fraction=float(holdout_fraction),
        groups=tuple(groups),
        left_out=left_out,
        delivery=delivery,
        calibration=calibration,
        calibration_reason=reason,
        summary=_summary(groups, len(result.index)),
        notes=_notes(left_out, delivery),
        created_at=now,
    )
    return LearnFrame(frame=result, record=record)


def _seed(run_id: str) -> int:
    from engine.utils.ids import seed_from

    return seed_from(run_id)


def _smallest_draws(keys: Any, count: int, *, seed: int) -> Any:
    """Positions of the `count` keys with the smallest `sha256("learn:<seed>:<key>")` draws."""
    import numpy as np

    from engine.holdout.assign import draw

    if count <= 0:
        return np.zeros(0, dtype=np.int64)
    draws = np.fromiter(
        (draw(f"{_DRAW_LABEL}:{seed}:{key}") for key in keys.tolist()), dtype=np.uint64, count=len(keys)
    )
    if count >= draws.size:
        return np.arange(draws.size, dtype=np.int64)
    return np.argpartition(draws, count - 1)[:count]


def _summary(groups: list[LearnGroup], rows: int) -> str:
    entered: dict[str, int] = {
        group.group: group.entered_contacted + group.entered_not_contacted for group in groups
    }
    on_list = entered.get(GROUP_ON_LIST, 0)
    outside = entered.get(GROUP_OUTSIDE_LIST, 0)
    return (
        f"Learned from {rows:,} customers whose contact was decided at random: {on_list:,} on the list "
        f"(contacted unless the control group drew them) and {outside:,} outside it (contacted only when "
        "the explore share drew them). In each group every customer on the smaller side entered, with as "
        "many drawn at random from the larger side, so the contacted and the not contacted are alike in "
        "everything but the contact."
    )


def _notes(left: LeftOut, delivery: Delivery | None) -> tuple[str, ...]:
    def people(count: int) -> str:
        return f"{count:,} {'customer was' if count == 1 else 'customers were'}"

    notes: list[str] = []
    if left.not_eligible:
        notes.append(f"{people(left.not_eligible)} left out because they could not be contacted at all.")
    if left.never_contacted_by_the_rule:
        notes.append(
            f"{people(left.never_contacted_by_the_rule)} left out because the list's rule never contacts a "
            "customer it expects a contact to put off."
        )
    if left.no_chance_of_contact:
        notes.append(
            f"{people(left.no_chance_of_contact)} left out because nothing contacted them at random."
        )
    if left.no_outcome:
        notes.append(f"{people(left.no_outcome)} left out because the outcomes file has no outcome for them.")
    if left.cut_to_balance:
        notes.append(f"{people(left.cut_to_balance)} drawn out at random to balance their group.")
    notes.append(
        "Contacted means the campaign meant to contact the customer, which is what was decided at random; "
        "a contact file, when there is one, is reported beside it and never replaces it."
    )
    if delivery is not None:
        notes.append(delivery.sentence)
    return tuple(notes)


# ---------------------------------------------------------------------------
# The contact file, reported
# ---------------------------------------------------------------------------
def delivery_from(readouts: Iterable[ContactReadout]) -> Delivery | None:
    """The first readout (the newest campaign's) as a `Delivery`, or None when there is none."""
    for readout in readouts:
        rate, leak = readout.contact_rate, readout.contamination
        parts = []
        if rate is not None:
            parts.append(f"{rate:.0%} of the customers meant to be contacted were reached")
        if leak is not None:
            parts.append(f"{leak:.0%} of the customers held back were contacted anyway")
        said = " and ".join(parts) if parts else "the contact rates could not be worked out"
        return Delivery(
            campaign_id=readout.campaign_id,
            contact_rate=rate,
            contamination=leak,
            sentence=(
                f"The contact file of campaign {readout.campaign_id} says {said}. The model still learns from "
                "who the campaign meant to contact."
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Predicted against measured, for the Approver
# ---------------------------------------------------------------------------
def _calibration_for(
    scores: pd.DataFrame,
    score_keys: pd.Series,
    frame_keys: Any,
    t: Any,
    y: Any,
    *,
    source_run_id: str,
    model_id: str | None,
    samples: int,
    seed: int,
) -> tuple[LiveCalibration | None, str | None]:
    import numpy as np
    import pandas as pd

    if PREDICTED_EFFECT_COLUMN not in scores.columns:
        return None, (
            "The last list was ranked by a risk score, which does not predict what a contact changes, so "
            "there is no prediction to set against what was measured."
        )
    predicted = pd.to_numeric(scores[PREDICTED_EFFECT_COLUMN], errors="coerce")
    by_key = pd.Series(predicted.to_numpy(dtype=np.float64), index=pd.Index(score_keys.to_numpy()))
    values = by_key.loc[frame_keys].to_numpy(dtype=np.float64)
    if values.size == 0 or not np.isfinite(values).all():
        return (
            None,
            "The model's predicted change is missing for some of these customers, so it is not compared.",
        )
    try:
        return (
            live_calibration(
                values, t, y, samples=samples, seed=seed, source_run_id=source_run_id, model_id=model_id
            ),
            None,
        )
    except ValueError:
        return (
            None,
            "Both contacted and not-contacted customers are needed to compare, and one side is empty.",
        )


def live_calibration(
    predicted: Any,
    t: Any,
    y: Any,
    *,
    samples: int,
    seed: int,
    source_run_id: str,
    model_id: str | None,
) -> LiveCalibration:
    """`calibration_by_decile` (M96) on a campaign's randomised rows, in the Approver's words."""
    from engine.uplift.metrics import calibration_by_decile

    table = calibration_by_decile(predicted, t, y, samples=samples, seed=seed)
    deciles = tuple(
        LiveDecile(
            decile=row.decile,
            rows=row.rows,
            predicted=row.predicted_uplift,
            measured=row.observed_uplift,
            inside_range=row.within_interval,
        )
        for row in table.deciles
    )
    return LiveCalibration(
        source_run_id=source_run_id,
        model_id=model_id,
        rows=sum(row.rows for row in deciles),
        deciles=deciles,
        deciles_with_range=table.deciles_with_interval,
        deciles_inside=table.deciles_covered,
        matches=table.well_calibrated,
        average_gap=table.weighted_abs_gap,
        resamples=samples,
        summary=_calibration_sentence(
            deciles, table.well_calibrated, table.deciles_covered, table.deciles_with_interval
        ),
    )


def _points(value: float) -> str:
    return f"{value * 100:+.1f} points"


def _calibration_sentence(
    deciles: tuple[LiveDecile, ...], matches: bool | None, inside: int, judged: int
) -> str:
    if matches is None:
        return (
            f"Not judged: only {judged} of the ten groups had both contacted and not-contacted customers "
            "to compare."
        )
    top = deciles[0]
    measured = top.measured
    if measured is not None and measured.ci_low is not None and measured.ci_high is not None:
        first = (
            f"For the tenth it ranked highest the model in use predicted {_points(top.predicted)}; the last "
            f"campaign measured {_points(measured.value)} (95% range {_points(measured.ci_low)} to "
            f"{_points(measured.ci_high)})."
        )
    else:
        first = f"For the tenth it ranked highest the model in use predicted {_points(top.predicted)}."
    verdict = "match" if matches else "do not match"
    return (
        f"{first} In {inside} of {judged} tenths the measured range holds the prediction, so its predictions "
        f"{verdict} what the campaign measured."
    )


def read_learn_record(storage: Storage, run_id: str) -> LearnRecord | None:
    """`runs/<run_id>/learned_from.json`, or None when the run was not learned from a cycle (or it is unreadable)."""
    from engine.storage import StorageError, run_key

    key = run_key(run_id, LEARN_RECORD_FILENAME)
    try:
        return storage.read_model(key, LearnRecord) if storage.exists(key) else None
    except (StorageError, ValueError, OSError):
        return None


def live_calibration_for(storage: Storage, version: ModelVersion) -> LiveCalibration | None:
    """The block the Approver sees beside a challenger learned from a cycle; None for any other model."""
    record = read_learn_record(storage, version.run_id)
    return None if record is None else record.calibration
