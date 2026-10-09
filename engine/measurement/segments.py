"""The measured effect in each group of customers, and the guard against false alarms (Plan J M104, DEC-1314).

A campaign's report says what it did on average. The Value Proof Pack also says *where*: the measured effect
in each band, each predicted segment (persuadables, sure things, lost causes, sleeping dogs) and each offer,
with its 95% interval, so finance can see which part of the list paid and which part was harmed. This module
writes `campaigns/<id>/segment_effects.json` beside every stored campaign report.

**The same measurement, once per group.** Each group's numbers are `measure_incrementality`'s on that
group's rows (both arms of the group, the campaign's own population rule, maturity rule and interval:
Newcombe for a rate, Welch for an amount), never a second computation. The rows of every group are found
in one pass over the assignment and one over the outcomes (the outcome rows are matched to the assignment by
a hash index, then split by the group's code), so the work is linear in the campaign's size for a bounded
number of groups; a dimension with more than :data:`MAX_GROUPS` values is not split and says so. An amount is
measured unadjusted in each group: the adjusted estimate the plan registered is for the campaign as a whole.

**Which groups.** `band` and `segment` when the assignment carries them (a scored run; an audited file has
no bands). An uplift run's band is its segment's label, so a band that splits the customers exactly as the
segment does is read once, as the segment. A group with nobody in the measured population (a segment the
policy never meant to contact) is left out: it would say nothing and widen the family. Offers: an audited campaign of several offers names each treated customer's offer and none for
the held-back group, so each offer is compared with the whole shared control (`shared_control`, the same
comparison as the report's `arms`); a scored run that chose the offer per customer knows the offer its
policy would have given every customer, held back or not (`policy_offer_label` of `offer_choice.parquet`,
DEC-1311 (af)), so each offer is compared within its own customers (`within_group`). A programme readout has
no groups: the file records that, it does not invent one.

**The false-alarm guard (multiple comparisons).** Ten groups each read at 95% show a "significant" harm by
chance far more often than one in twenty. So a group is *judged* only when it has at least
:data:`MIN_ROWS_PER_ARM` measured customers in each arm, and every judged group gets a second interval at the
Bonferroni level `1 - FAMILY_ALPHA / m`, `m` the number of judged groups (`family_interval`): the chance that
any group of a campaign with no effect anywhere is flagged is then at most 5%. The Proof Pack flags a group as
backfiring only when that wider interval lies wholly on the harmful side of zero. For a rate it is the exact
Newcombe interval at that level; for an amount it is widened from the 95% Welch interval conservatively (the
standard error read off the 95% interval with the normal quantile, which can only overstate it, and the t
quantile at the smaller arm's degrees of freedom, which can only overstate the quantile).

The file holds counts, rates, means and intervals per group: no customer id. `pandas` is imported inside
the functions, so `import engine` stays fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.contracts import Artefact
from engine.uplift.contracts import ConfidenceValue

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd
    from numpy.typing import NDArray

    from engine.config import PrimaryKey
    from engine.storage import Storage
    from engine.uplift.contracts import IncrementalityReport

__all__ = [
    "FAMILY_ALPHA",
    "MAX_GROUPS",
    "MIN_ROWS_PER_ARM",
    "SEGMENT_EFFECTS_FILENAME",
    "DimensionNote",
    "SegmentCell",
    "SegmentEffects",
    "measure_segment_effects",
    "policy_offers",
]

SEGMENT_EFFECTS_FILENAME: Final[str] = "segment_effects.json"
"""`campaigns/<id>/segment_effects.json`: written beside the campaign's report, counts and intervals only."""

MIN_ROWS_PER_ARM: Final[int] = 50
"""A group is judged for backfire only with at least this many measured customers in each arm."""

FAMILY_ALPHA: Final[float] = 0.05
"""The chance, over all judged groups together, of flagging one when no group was harmed (Bonferroni)."""

MAX_GROUPS: Final[int] = 50
"""A dimension with more distinct values than this is not split (and the file says why)."""

Dimension = Literal["band", "segment", "offer"]

_Z_95: Final[float] = NormalDist().inv_cdf(0.975)


class SegmentCell(Artefact):
    """One group's measured effect: the campaign's measurement on that group's customers only."""

    dimension: Dimension = Field(description="`band`, `segment` (the predicted four segments) or `offer`.")
    segment: str = Field(description="The group's name, as the assignment or the offer choice spells it.")
    comparison: Literal["within_group", "shared_control"] = Field(
        description=(
            "`within_group`: contacted and held-back customers of this group. `shared_control`: the customers "
            "given this offer against every held-back customer (an audited campaign of several offers)."
        )
    )
    treated_rows: int = Field(
        description="Contacted customers of the group measured (mature, with an outcome)."
    )
    control_rows: int = Field(description="Held-back customers measured.")
    treated_conversions: int = Field(
        description="Outcomes among the contacted (an amount: those above zero)."
    )
    control_conversions: int = Field(
        description="Outcomes among the held back (an amount: those above zero)."
    )
    treated_rate: float | None = Field(
        default=None, description="A rate's contacted share; null for an amount."
    )
    control_rate: float | None = Field(
        default=None, description="A rate's held-back share; null for an amount."
    )
    treated_mean: float | None = Field(default=None, description="An amount's contacted average.")
    control_mean: float | None = Field(default=None, description="An amount's held-back average.")
    effect: ConfidenceValue | None = Field(
        default=None,
        description="Contacted minus held back (rate or average) with its 95% interval; null when an arm is empty.",
    )
    judged: bool = Field(
        description="True when both arms have at least `min_rows_per_arm` customers: the group is in the family."
    )
    family_interval: ConfidenceValue | None = Field(
        default=None,
        description="The effect at the family's Bonferroni level; null for a group that is not judged.",
    )
    rows_without_outcome: int = Field(default=0, description="Customers of the group with no usable outcome.")


class DimensionNote(Artefact):
    """A dimension the file does not split by, and why."""

    dimension: Dimension
    reason: str = Field(description="Why, in one plain sentence.")


class SegmentEffects(Artefact):
    """`campaigns/<id>/segment_effects.json`: every group's measured effect, and the false-alarm rule used."""

    campaign_id: str | None = Field(description="The campaign the groups belong to.")
    report_computed_at: AwareDatetime = Field(
        description="`computed_at` of the report measured beside it: a file from an older measurement is not read."
    )
    outcome_kind: Literal["binary", "continuous"] = Field(description="A rate (yes/no) or an amount.")
    early_look: bool = Field(default=False, description="True when the report beside it is an early look.")
    min_rows_per_arm: int = Field(description="The smallest arm a judged group may have.")
    family_alpha: float = Field(description="The family-wise false-alarm rate the judged groups share.")
    family_size: int = Field(description="How many groups are judged: the Bonferroni divisor.")
    family_confidence: float | None = Field(
        default=None, description="1 - family_alpha / family_size; null when no group is judged."
    )
    cells: tuple[SegmentCell, ...] = Field(default=(), description="Every group, dimension by dimension.")
    not_measured: tuple[DimensionNote, ...] = Field(
        default=(), description="Dimensions the campaign has no groups in, or too many, with the reason."
    )
    computed_at: AwareDatetime = Field(description="When the file was computed.")


def policy_offers(
    storage: Storage, run_id: str, assignment: pd.DataFrame, primary_key: PrimaryKey
) -> pd.Series[Any] | None:
    """Each assignment row's offer as the run's policy chose it (held back or not), or None.

    DEC-1311 (af): `offer_choice.parquet` names, for every customer, the offer the policy would give them if
    nobody were held back (`policy_offer_label`, else `policy_offer_arm`). Joined to the assignment on the
    key as the treat list spells it. None for a run that did not choose the offer per customer.
    """
    import io

    import pandas as pd

    from engine.config import key_columns
    from engine.decide.offer_run import OFFER_CHOICE_FILENAME
    from engine.decide.treat_list import _join_keys
    from engine.keys import KEY_SEPARATOR, key_text
    from engine.storage import StorageError, run_key

    try:
        table = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, OFFER_CHOICE_FILENAME))))
    except StorageError:
        return None
    column = next(
        (name for name in ("policy_offer_label", "policy_offer_arm") if name in table.columns), None
    )
    if column is None:
        return None
    offers = pd.Series(table[column].astype("string").to_numpy(), index=_join_keys(table, primary_key))
    offers = offers[~offers.index.duplicated(keep="first")]
    columns = key_columns(primary_key)
    joined = key_text(assignment[columns[0]]).astype("object")
    for name in columns[1:]:
        joined = joined + KEY_SEPARATOR + key_text(assignment[name]).astype("object")
    return pd.Series(offers.reindex(joined.to_numpy()).to_numpy(), index=assignment.index, dtype="string")


def measure_segment_effects(
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame,
    report: IncrementalityReport,
    *,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None = None,
    intended_column: str | None = None,
    treatment_time: datetime,
    treatment_date_column: str | None = None,
    outcome_window_days: int | None = None,
    as_of: datetime,
    offers: pd.Series[Any] | None = None,
    control_level: str | None = None,
) -> SegmentEffects:
    """Every group's effect, measured as the campaign was (`report`), with the false-alarm rule applied.

    `assignment` and `outcomes` are what `measure_campaign` read to make `report`; the other arguments are the
    ones it was given. `offers` (aligned with `assignment`) names each customer's offer; a held-back customer
    with none, or with `control_level`, makes the offers `shared_control`. Raises `ValueError` only for what
    `measure_campaign` itself would have refused.
    """
    import numpy as np
    import pandas as pd

    from engine.config import key_columns
    from engine.measurement.campaign import as_scores_frame
    from engine.uplift.incrementality import _flag, _joined_keys

    frame = as_scores_frame(assignment).reset_index(drop=True)
    outcome_frame = outcomes.reset_index(drop=True)
    columns = key_columns(primary_key)
    keys = pd.Index(_joined_keys(frame, columns).to_numpy())
    if not keys.is_unique:
        raise ValueError("The assignment repeats a customer, so it cannot be split into groups.")
    position = keys.get_indexer(_joined_keys(outcome_frame, columns).to_numpy())
    held_out = _flag(frame["control_group"]).to_numpy(dtype=bool)
    kind: Literal["binary", "continuous"] = "continuous" if report.outcome_kind == "continuous" else "binary"

    dimensions: list[tuple[Dimension, pd.Series[Any]]] = []
    notes: list[DimensionNote] = []
    if "band" in frame.columns and not _same_groups(frame, "band", "segment"):
        dimensions.append(("band", frame["band"]))
    elif "band" in frame.columns:
        notes.append(
            DimensionNote(dimension="band", reason="The bands are the predicted segments, read once.")
        )
    if "segment" in frame.columns:
        dimensions.append(("segment", frame["segment"]))
    if offers is not None:
        dimensions.append(("offer", pd.Series(offers.to_numpy(), index=frame.index)))

    measure_args: dict[str, Any] = {
        "run_id": report.run_id,
        "primary_key": primary_key,
        "outcome_column": outcome_column,
        "positive_label": positive_label,
        "intended_column": intended_column,
        "treatment_time": treatment_time,
        "treatment_date_column": treatment_date_column,
        "outcome_window_days": outcome_window_days,
        "as_of": as_of,
        "campaign_id": report.campaign_id,
        "outcome_kind": kind,
    }
    measured: list[SegmentCell] = []
    for dimension, values in dimensions:
        text = values.astype("string").str.strip()
        if dimension == "offer" and control_level is not None:
            text = text.mask(text == str(control_level))
        text = text.mask(text == "")
        codes, uniques = pd.factorize(text, use_na_sentinel=True)
        codes = np.asarray(codes, dtype=np.int64)
        if len(uniques) == 0:
            notes.append(DimensionNote(dimension=dimension, reason=_NO_GROUPS[dimension]))
            continue
        if len(uniques) > MAX_GROUPS:
            notes.append(
                DimensionNote(
                    dimension=dimension,
                    reason=(
                        f"The campaign has more than {MAX_GROUPS} different {_WORDS[dimension]}, too many to read "
                        f"one by one."
                    ),
                )
            )
            continue
        shared = dimension == "offer" and not bool((codes[held_out] >= 0).any())
        row_groups = _groups(codes)
        matched = position >= 0
        outcome_codes = np.where(matched, codes[np.where(matched, position, 0)], -1)
        outcome_groups = _groups(outcome_codes)
        control_rows = np.flatnonzero(held_out)
        control_outcome_rows = np.flatnonzero(matched & held_out[np.where(matched, position, 0)])
        empty = np.zeros(0, dtype=np.int64)
        for code, label in enumerate(uniques):
            rows = row_groups.get(code, empty)
            outcome_rows = outcome_groups.get(code, empty)
            if shared:
                rows = np.union1d(rows, control_rows)
                outcome_rows = np.union1d(outcome_rows, control_outcome_rows)
            cell = _cell(
                dimension,
                str(label),
                "shared_control" if shared else "within_group",
                frame.take(rows),
                outcome_frame.take(outcome_rows),
                measure_args,
            )
            if (
                cell.treated_rows or cell.control_rows
            ):  # a group with nobody in the measured population says nothing
                measured.append(cell)
    judged = [cell for cell in measured if cell.judged]
    family = len(judged)
    confidence = 1.0 - FAMILY_ALPHA / family if family else None
    cells = tuple(
        (
            cell.model_copy(update={"family_interval": _family_interval(cell, family, kind)})
            if cell.judged
            else cell
        )
        for cell in measured
    )
    return SegmentEffects(
        campaign_id=report.campaign_id,
        report_computed_at=report.computed_at,
        outcome_kind=kind,
        early_look=report.early_look,
        min_rows_per_arm=MIN_ROWS_PER_ARM,
        family_alpha=FAMILY_ALPHA,
        family_size=family,
        family_confidence=confidence,
        cells=cells,
        not_measured=tuple(notes),
        computed_at=datetime.now(UTC),
    )


_WORDS: Final[dict[str, str]] = {"band": "bands", "segment": "segments", "offer": "offers"}
_NO_GROUPS: Final[dict[str, str]] = {
    "band": "No customer of the campaign has a band.",
    "segment": "No customer of the campaign has a predicted segment.",
    "offer": "No customer of the campaign has an offer named.",
}


def _same_groups(frame: pd.DataFrame, first: str, second: str) -> bool:
    """True when two columns split the customers the same way (an uplift run's band is its segment's label)."""
    if second not in frame.columns:
        return False
    pairs = frame[[first, second]].astype("string").drop_duplicates()
    return bool(pairs[first].is_unique and pairs[second].is_unique)


def _groups(codes: NDArray[np.int64]) -> dict[int, NDArray[np.int64]]:
    """Row positions per code (code -1, no group, is left out), in one stable sort: O(n log n), no loop over rows."""
    import numpy as np

    order = np.argsort(codes, kind="stable")
    ordered = codes[order]
    starts = (
        np.flatnonzero(np.r_[True, ordered[1:] != ordered[:-1]]) if len(ordered) else np.zeros(0, dtype=int)
    )
    ends = np.r_[starts[1:], len(ordered)] if len(ordered) else np.zeros(0, dtype=int)
    return {int(ordered[s]): order[s:e] for s, e in zip(starts, ends, strict=True) if ordered[s] >= 0}


def _cell(
    dimension: Dimension,
    label: str,
    comparison: Literal["within_group", "shared_control"],
    rows: pd.DataFrame,
    outcome_rows: pd.DataFrame,
    measure_args: dict[str, Any],
) -> SegmentCell:
    """One group's `measure_incrementality`, read into a cell."""
    from engine.uplift.incrementality import measure_incrementality

    report = measure_incrementality(rows, outcome_rows, **measure_args)
    continuous = report.outcome_kind == "continuous"
    effect = report.mean_difference_ci if continuous else report.absolute_lift
    judged = (
        effect is not None
        and effect.ci_low is not None
        and report.treated_rows >= MIN_ROWS_PER_ARM
        and report.control_rows >= MIN_ROWS_PER_ARM
    )
    return SegmentCell(
        dimension=dimension,
        segment=label,
        comparison=comparison,
        treated_rows=report.treated_rows,
        control_rows=report.control_rows,
        treated_conversions=report.treated_conversions,
        control_conversions=report.control_conversions,
        treated_rate=report.treated_rate,
        control_rate=report.control_rate,
        treated_mean=report.treated_mean,
        control_mean=report.control_mean,
        effect=effect,
        judged=judged,
        rows_without_outcome=report.rows_without_outcome,
    )


def _family_interval(
    cell: SegmentCell, family: int, kind: Literal["binary", "continuous"]
) -> ConfidenceValue | None:
    """The cell's effect at the Bonferroni level of a family of `family` judged groups (see the module docstring)."""
    from engine.measurement.continuous import t_quantile
    from engine.uplift.incrementality import newcombe_interval

    effect = cell.effect
    if effect is None or effect.ci_low is None or effect.ci_high is None or family < 1:
        return None
    level = 1.0 - FAMILY_ALPHA / family
    if kind == "binary":
        z = NormalDist().inv_cdf(1.0 - FAMILY_ALPHA / (2.0 * family))
        difference, low, high = newcombe_interval(
            cell.treated_conversions, cell.treated_rows, cell.control_conversions, cell.control_rows, z=z
        )
        return ConfidenceValue(value=difference, ci_low=low, ci_high=high, confidence_level=level)
    half_95 = max(effect.ci_high - effect.value, effect.value - effect.ci_low)
    smaller_arm = min(cell.treated_rows, cell.control_rows)
    quantile = t_quantile(1.0 - FAMILY_ALPHA / (2.0 * family), float(max(smaller_arm - 1, 1)))
    half = quantile * half_95 / _Z_95
    return ConfidenceValue(
        value=effect.value, ci_low=effect.value - half, ci_high=effect.value + half, confidence_level=level
    )
