"""The treat list builder: one downloadable file per scoring run (Plan J M98, DEC-1308).

A pure builder over a finished scoring run's artefacts. It writes `treat_list.csv`, `treat_list.parquet`
and `treat_list_summary.json` beside them in storage, and is never imported by the pipeline's stages.

**What it joins, and how.** Every file that carries one row per customer is joined to the scores on
**all** the key columns (`_join_keys`, the text of `engine.keys.key_series`, built column-wise), never by position: `holdout_assignment.parquet`
(holdout, explore and `treated` flags), `row_explanations.parquet` (the reasons) and, when the run's use case
configures channels, `channel_contactability.parquet` (M99: which channels each customer may be contacted on,
worked out during the run by `engine.decide.contactability`). A customer a file does not cover gets a null
for what that file would have said, not a false.

**What it reads and does not recompute.** The treat flag is M92's `treated` column from
`holdout_assignment.parquet`; where there is none (a default run, or a customer the file does not cover)
it is derived with M92's own selection function (`engine.holdout.assign.selection_masks`,
`treated_flags`). Net value on an uplift run is M97's `net_value` column of the scores. On a propensity
run M97's expected gross value is worked out per customer with `engine.decide.value`'s helpers from the
run's own `expected_gross_value.json` (its costs and value column) and the uploaded values, and goes in
its own column, `expected_gross_value`, because it is not incremental. The settings are the run's own
`run_config.json`, never today's use case file.

**Units and nulls.** `net_value` and `expected_gross_value` are rupees. A missing input stays null: an
unknown holdout flag is null, not false; a value that was not configured is null, not zero; a reason
the customer has no more of is null, not an empty string. Parquet holds the flags as booleans; the CSV
writes `1` and `0` (empty for null).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq
from pydantic import Field

from engine.config import ResolvedConfig, RunMode, UseCaseConfig
from engine.contracts import Artefact, RunRecord, RunState
from engine.decide.catalogue import CATALOGUE_STAMP_FILENAME, CatalogueStamp, catalogue_sha256
from engine.decide.contactability import (
    CHANNEL_CONTACTABILITY_FILENAME,
    CHANNEL_CONTACTABILITY_SUMMARY_FILENAME,
    ChannelContactability,
    contactable_column,
)
from engine.decide.reasons import extract_and_map_reasons_from_parquet, load_reasons_dictionary
from engine.decide.value import EXPECTED_GROSS_VALUE_FILENAME, ExpectedGrossValue, expected_gross_values
from engine.holdout.assign import (
    EXPLORE_COLUMN,
    HOLDOUT_ASSIGNMENT_FILENAME,
    HOLDOUT_MEMBER_COLUMN,
    TREATED_COLUMN,
    selection_masks,
    treated_flags,
)
from engine.keys import KEY_SEPARATOR, key_columns, key_text
from engine.pilot.roi import ValueCosts
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
from engine.stages.actions import (
    ACTION_COLUMN,
    BAND_COLUMN,
    CONTROL_ACTION,
    CONTROL_GROUP_COLUMN,
    SUPPRESSED_ACTION,
    SUPPRESSED_REASON_COLUMN,
)
from engine.stages.export import SCORES_CSV, SCORES_PARQUET
from engine.storage import Storage, StorageError, run_key
from engine.uplift.actions import NET_VALUE_COLUMN, TREAT_ACTION
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "TREAT_LIST_CSV",
    "TREAT_LIST_PARQUET",
    "TREAT_LIST_SUMMARY_FILENAME",
    "TreatListError",
    "TreatListSummary",
    "build_treat_list",
    "ensure_treat_list",
]

_LOGGER = get_logger(__name__)

pc: Any = _pc
"""`pyarrow.compute`, untyped: its stubs miss most kernels."""

TREAT_LIST_CSV: Final[str] = "treat_list.csv"
TREAT_LIST_PARQUET: Final[str] = "treat_list.parquet"
TREAT_LIST_SUMMARY_FILENAME: Final[str] = "treat_list_summary.json"

EXPECTED_GROSS_VALUE_COLUMN: Final[str] = "expected_gross_value"
"""M97's expected gross value per customer, in rupees. Not incremental: it counts customers who would have
converted without a contact. Filled on a propensity run that opted in; null otherwise."""

_RUPEE_DECIMALS: Final[int] = 2
"""Rupees are written to the paisa, so a figure never reads `71.53999999999999`."""

REASON_COLUMNS: Final[tuple[str, str, str]] = ("reason_1", "reason_2", "reason_3")

RUNNER_UP_OFFER_COLUMN: Final[str] = "runner_up_offer"
"""Plan J M100 part B: the next-best offer the customer could be given (its catalogue label, else its
level); empty on a run of one offer, and when there is none."""
RUNNER_UP_VALUE_COLUMN: Final[str] = "runner_up_net_value"
"""Its predicted net value, in rupees (whatever its sign); empty when there is none."""
OFFER_REASON_COLUMN: Final[str] = "offer_reason"
"""Why a customer who could be treated got no offer, in plain words; empty otherwise."""

_NOT_SCORED: Final[str] = "RUN_NOT_SCORED"
_SEGMENT_COLUMN: Final[str] = "segment"
_REQUIRED_SCORE_COLUMNS: Final[tuple[str, ...]] = (
    BAND_COLUMN,
    ACTION_COLUMN,
    SUPPRESSED_REASON_COLUMN,
    CONTROL_GROUP_COLUMN,
)
_NO_ASSIGNMENT_NOTE: Final[str] = (
    "This run wrote no holdout assignment file, so the holdout and explore columns are left blank. "
    "Customers the run kept back as its own control group are marked control_group in the contact list, "
    "and they are never treated."
)
_UNREADABLE_ASSIGNMENT_NOTE: Final[str] = (
    "The run's holdout assignment file could not be matched to its customers, so the holdout flag is not known."
)
_NO_NET_VALUE_NOTE: Final[str] = "Net value was not configured for this run."
_NO_GROSS_VALUE_NOTE: Final[str] = "Expected gross value was not configured for this run."
_GROSS_NOT_INCREMENTAL_NOTE: Final[str] = (
    "Expected gross value is what the customer is predicted to be worth if they respond, less the cost "
    "of contacting them, in rupees. It is not incremental: it counts customers who would have responded "
    "without a contact. Net value, which is incremental, needs an uplift run."
)


class TreatListError(Exception):
    """The treat list cannot be built for this run; `message` says why in plain words.

    `code` is an existing error code (`RUN_NOT_SCORED`, `RUN_NOT_FOUND`) the API answers with.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TreatListSummary(Artefact):
    """Aggregate summary of `treat_list.parquet`: counts, and value in rupees."""

    run_id: str = Field(description="Run the treat list was built for.")
    use_case_id: str = Field(description="Use case of the run.")
    total_rows: int = Field(description="Total rows in the treat list.")
    treat_rows: int = Field(description="Rows with treat = 1.")
    holdout_rows: int | None = Field(
        default=None,
        description="Rows with holdout = 1, or null when the run has no holdout assignment.",
    )
    explore_rows: int | None = Field(
        default=0,
        description="Rows with explore = 1, or null when the run has no holdout assignment (the column is blank).",
    )
    suppressed_rows: int = Field(default=0, description="Rows with a suppression reason.")
    net_value_total: float | None = Field(
        default=None,
        description=(
            "Total predicted net value (incremental) of the treat = 1 rows, in rupees, or null when the run "
            "has none (an uplift run ranked by value has it)."
        ),
    )
    net_value_unit: str | None = Field(default=None, description="Unit of net value ('rupees'), or null.")
    expected_gross_value_total: float | None = Field(
        default=None,
        description=(
            "Total expected gross value of the treat = 1 rows, in rupees, or null when the run has none. "
            "NOT incremental: see `expected_gross_value_note`."
        ),
    )
    holdout_note: str | None = Field(
        default=None, description="Explanation when holdout flags could not be determined for every row."
    )
    net_value_note: str | None = Field(
        default=None, description="Explanation when net value was not configured for the run."
    )
    expected_gross_value_note: str | None = Field(
        default=None,
        description="Says the figure is not incremental when it is filled, or why it is not filled.",
    )
    created_at: datetime = Field(description="UTC time the treat list was built.")
    catalogue_sha256: str | None = Field(
        default=None,
        description=(
            "SHA-256 of configs/decide/catalogue.yaml as the run read it (`catalogue_stamp.json`), or "
            "null when the run had no catalogue."
        ),
    )
    catalogue_note: str | None = Field(
        default=None,
        description=(
            "Plan J M99: set when configs/decide/catalogue.yaml is not the file the run ran under (edited, "
            "added or removed since); channels are still planned from the run's own record. Absent otherwise."
        ),
        exclude_if=lambda value: value is None,
    )
    channel_counts: dict[str, int] | None = Field(
        default=None,
        description=(
            "Plan J M99: the run's per-channel counts (`channel_contactability.json`): rows neither "
            "suppressed nor held out as control that are not contactable on the channel. Absent when the "
            "run's use case configures no channels."
        ),
        exclude_if=lambda value: value is None,
    )
    uncontactable_rows: int | None = Field(
        default=None,
        description=(
            "Plan J M99: rows that would be treated but are contactable on none of their planned channels, "
            "so treat = 0 (they are not suppressed). Absent when the run wrote no per-channel contactability."
        ),
        exclude_if=lambda value: value is None,
    )
    offer_counts: dict[str, int] | None = Field(
        default=None,
        description=(
            "Plan J M100 part B: treat = 1 rows per offer (its label), on a run that chose the offer per "
            "customer. Absent otherwise."
        ),
        exclude_if=lambda value: value is None,
    )
    channel_rows: dict[str, int] | None = Field(
        default=None,
        description=(
            "Plan J M100 part B: treat = 1 rows per channel they are sent on. Absent when no treated row "
            "has a channel (a run that configures no channels and names no catalogue action)."
        ),
        exclude_if=lambda value: value is None,
    )


def ensure_treat_list(storage: Storage, run_id: str, *, config_root: Path | None = None) -> TreatListSummary:
    """The summary of the run's treat list, building the three files first when they are not there."""
    try:
        return storage.read_model(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME), TreatListSummary)
    except StorageError:
        return build_treat_list(storage, run_id, config_root=config_root)


class _Flags(NamedTuple):
    """Nullable per-row flags joined from `holdout_assignment.parquet`, aligned to the scores' rows."""

    holdout: pd.Series[Any]
    explore: pd.Series[Any]
    treated: pd.Series[Any]
    note: str | None


def build_treat_list(storage: Storage, run_id: str, *, config_root: Path | None = None) -> TreatListSummary:
    """Create the treat list files from the run's artefacts and return their summary.

    Raises `TreatListError` for a run that cannot have one (not finished, not a scoring run, no scores,
    no saved settings). `config_root` is where `decide/reasons.yaml` is read from.
    """
    record = _read_record(storage, run_id)
    config = _read_config(storage, run_id)
    primary_key = record.primary_key
    key_cols = key_columns(primary_key)

    scores = _read_scores(storage, run_id, key_cols)
    n_rows = len(scores.index)
    row_keys = _join_keys(scores, primary_key)
    is_uplift = _SEGMENT_COLUMN in scores.columns

    # 1. Flags: holdout, explore and treated, joined to the scores on every key column.
    flags = _read_flags(storage, run_id, primary_key, row_keys)
    suppression = scores[SUPPRESSED_REASON_COLUMN].astype("object")
    suppression = suppression.where(suppression.notna() & (suppression != ""), None)
    suppressed = suppression.notna().to_numpy(dtype=bool)
    control = scores[CONTROL_GROUP_COLUMN].astype(bool).to_numpy(dtype=bool)
    holdout_true = flags.holdout.fillna(False).to_numpy(dtype=bool)
    explore_true = flags.explore.fillna(False).to_numpy(dtype=bool)

    # 2. The treat flag: M92's own `treated` where the assignment covers the row, else M92's selection.
    selected, sleeping = selection_masks(scores, config)
    derived = treated_flags(~suppressed, selected, control, explore_true)
    known = flags.treated.notna().to_numpy(dtype=bool)
    assigned = flags.treated.fillna(False).to_numpy(dtype=bool)
    chosen = np.where(known, assigned, derived)
    held_out = holdout_true | control
    treat = chosen & ~suppressed & ~held_out & ~sleeping
    if bool((chosen & ~treat).any()):
        _LOGGER.warning(
            "treat list %s: %d rows the assignment calls treated are suppressed, held out or sleeping dogs; "
            "they are not treated here",
            run_id,
            int((chosen & ~treat).sum()),
        )

    # 3. Offer (the band's action; M100 fills it later). The channel is step 5.
    action = scores[ACTION_COLUMN].astype("object")
    has_offer = (
        ~suppressed & ~control & ~action.isin([SUPPRESSED_ACTION, CONTROL_ACTION]).to_numpy(dtype=bool)
    )
    offer = action.where(has_offer & action.notna().to_numpy(dtype=bool), None)
    # An explored customer is treated although the policy left them out, so their band's action ("Hold",
    # "Don't treat") would contradict treat = 1. M92 leaves `scores.*` as it was and says the hand-off
    # file carries the flag; here the offer follows it: the uplift policy's own treat action, and nothing
    # on a propensity run, whose bands name no offer for a band the list does not contact.
    explored = treat & explore_true & ~selected
    if bool(explored.any()):
        offer = offer.mask(explored, TREAT_ACTION if is_uplift else None)

    # 4. Value, in rupees: net value (uplift) or expected gross value (propensity), never mixed.
    net_value, net_value_note, net_value_unit = _net_value(scores, is_uplift)
    gross_value, gross_note = _expected_gross_value(storage, record, config, scores, row_keys, is_uplift)

    # 5. Channel, from the run's own per-channel contactability and catalogue stamp (Plan J M99).
    stamp = _read_stamp(storage, run_id)
    cat_sha = None if stamp is None else stamp.catalogue_sha256
    cat_note = _catalogue_note(run_id, cat_sha, catalogue_sha256(root=config_root))
    channels = _resolve_channels(
        storage,
        run_id,
        primary_key,
        row_keys,
        scores=scores,
        config=config,
        treat=treat,
        is_uplift=is_uplift,
        planned_by_action={} if stamp is None else stamp.planned_channels,
    )
    treat = channels.treat
    channel = channels.channel
    uncontactable_rows = channels.uncontactable_rows

    # 5b. The offer chosen per customer on a run of several offers (Plan J M100 part B).
    runner_up_offer = pd.Series(np.full(n_rows, None, dtype=object), dtype="object")
    runner_up_value = pd.Series(np.full(n_rows, np.nan), dtype="float64")
    offer_reason = pd.Series(np.full(n_rows, None, dtype=object), dtype="object")
    offer_counts: dict[str, int] | None = None
    offer_choice = _read_offer_choice(storage, run_id, primary_key, row_keys)
    if offer_choice is not None:
        applied = _apply_offer_choice(
            offer_choice,
            suppressed=suppressed,
            held_out=held_out,
            explore=explore_true,
            fallback_treat=treat,
            fallback_offer=offer,
            fallback_channel=channel,
            fallback_net_value=net_value,
            contactability_written=uncontactable_rows is not None,
        )
        treat, offer, channel = applied.treat, applied.offer, applied.channel
        net_value, runner_up_offer, runner_up_value = (
            applied.net_value,
            applied.runner_up_offer,
            applied.runner_up_value,
        )
        offer_reason, uncontactable_rows = applied.offer_reason, applied.uncontactable_rows
        offer_counts = applied.offer_counts
        if net_value.notna().any():
            net_value_note, net_value_unit = None, "rupees"

    # 6. Reasons, in business words.
    reasons = _reasons(storage, run_id, scores, row_keys, config_root)

    # 7. Assemble, then write.
    out = _assemble(
        scores=scores,
        key_cols=key_cols,
        record=record,
        is_uplift=is_uplift,
        treat=treat,
        flags=flags,
        suppression=suppression,
        offer=offer,
        channel=channel,
        contactable_channels=channels.contactable_channels,
        net_value=net_value,
        gross_value=gross_value,
        reasons=reasons,
        runner_up_offer=runner_up_offer,
        runner_up_value=runner_up_value,
        offer_reason=offer_reason,
    )
    table = _table(out, is_uplift, key_cols)
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), _parquet_bytes(table))
    storage.write_bytes(run_key(run_id, TREAT_LIST_CSV), _csv_bytes(table))

    summary = TreatListSummary(
        run_id=run_id,
        use_case_id=record.use_case_id,
        total_rows=n_rows,
        treat_rows=int(treat.sum()),
        holdout_rows=int(holdout_true.sum()) if flags.holdout.notna().any() else None,
        explore_rows=int(explore_true.sum()) if flags.explore.notna().any() else None,
        suppressed_rows=int(suppressed.sum()),
        net_value_total=_total(net_value, treat),
        net_value_unit="rupees" if net_value_unit else None,
        expected_gross_value_total=_total(gross_value, treat),
        holdout_note=flags.note,
        net_value_note=net_value_note,
        expected_gross_value_note=gross_note,
        created_at=utc_now(),
        catalogue_sha256=cat_sha,
        catalogue_note=cat_note,
        channel_counts=channels.channel_counts,
        uncontactable_rows=uncontactable_rows,
        offer_counts=offer_counts,
        channel_rows=_channel_rows(channel, treat),
    )
    storage.write_model(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME), summary)
    _LOGGER.info(
        "Built treat list for run %s: %d rows (%d treat, %s holdout, %s explore)",
        run_id,
        n_rows,
        summary.treat_rows,
        summary.holdout_rows,
        summary.explore_rows,
    )
    return summary


def _join_keys(frame: pd.DataFrame, primary_key: Any) -> pd.Series[Any]:
    """One text per row naming it: the key columns as text (`key_text`) joined by `KEY_SEPARATOR`.

    The same text as `engine.keys.key_series`, which builds it with a Python loop over the rows; this
    joins whole columns, so a million customers cost a few array operations.
    """
    names = key_columns(primary_key)
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise KeyError(f"The frame has no {', '.join(missing)} column, so its rows cannot be identified.")
    joined = key_text(frame[names[0]]).astype("object")
    for name in names[1:]:
        joined = joined + KEY_SEPARATOR + key_text(frame[name]).astype("object")
    return joined


# ---------------------------------------------------------------------------
# Reading the run
# ---------------------------------------------------------------------------
def _read_record(storage: Storage, run_id: str) -> RunRecord:
    try:
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except StorageError as exc:
        raise TreatListError("RUN_NOT_FOUND", f"No run with id {run_id!r}.") from exc
    if record.mode is not RunMode.SCORE:
        raise TreatListError(
            _NOT_SCORED, "A treat list is made from a scoring run; this run trained a model instead."
        )
    if record.state is not RunState.DONE:
        raise TreatListError(
            _NOT_SCORED,
            f"This run is {record.state.value}, so it has no treat list yet. Try again when it is done.",
        )
    return record


def _read_config(storage: Storage, run_id: str) -> UseCaseConfig:
    """The settings the run was scored with (`run_config.json`), not today's use case file."""
    try:
        return storage.read_model(run_key(run_id, RUN_CONFIG_FILENAME), ResolvedConfig).config
    except StorageError as exc:
        raise TreatListError(
            _NOT_SCORED,
            "This run did not save the settings it was scored with (run_config.json), so its treat list "
            "cannot be built from them.",
        ) from exc


def _read_scores(storage: Storage, run_id: str, key_cols: tuple[str, ...]) -> pd.DataFrame:
    """`scores.parquet`, else `scores.csv`; with the columns the builder needs checked."""
    try:
        scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, SCORES_PARQUET))))
    except StorageError:
        try:
            scores = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(run_id, SCORES_CSV))))
        except StorageError as exc:
            raise TreatListError(
                _NOT_SCORED,
                "This run has no scores file (scores.parquet or scores.csv) to build a treat list from.",
            ) from exc
    missing = [name for name in (*key_cols, *_REQUIRED_SCORE_COLUMNS) if name not in scores.columns]
    if missing:
        raise TreatListError(
            _NOT_SCORED,
            f"This run's scores file has no {', '.join(repr(name) for name in missing)} column, "
            "so a treat list cannot be built from it.",
        )
    return scores.reset_index(drop=True)


def _read_flags(storage: Storage, run_id: str, primary_key: Any, row_keys: pd.Series[Any]) -> _Flags:
    """Join `holdout_assignment.parquet` to the scores on every key column; null where it has no row.

    Never by position: the file may list the customers in another order, and a customer it does not
    cover gets null, which is neither in nor out of the holdout.
    """
    n = len(row_keys.index)
    empty = pd.Series(pd.array([pd.NA] * n, dtype="boolean"))
    try:
        data = storage.read_bytes(run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME))
    except StorageError:
        return _Flags(empty, empty, empty, _NO_ASSIGNMENT_NOTE)
    assignment = pd.read_parquet(io.BytesIO(data))
    key_cols = key_columns(primary_key)
    if HOLDOUT_MEMBER_COLUMN not in assignment.columns or any(c not in assignment.columns for c in key_cols):
        _LOGGER.warning("treat list %s: holdout_assignment.parquet lacks the key or holdout columns", run_id)
        return _Flags(empty, empty, empty, _UNREADABLE_ASSIGNMENT_NOTE)
    index = pd.Index(_join_keys(assignment, primary_key))
    if not index.is_unique:
        _LOGGER.warning("treat list %s: holdout_assignment.parquet repeats a customer key", run_id)
        return _Flags(empty, empty, empty, _UNREADABLE_ASSIGNMENT_NOTE)
    positions = index.get_indexer(pd.Index(row_keys))
    covered = positions >= 0

    def joined(column: str) -> pd.Series[Any]:
        if column not in assignment.columns:
            return empty
        values = assignment[column].to_numpy(dtype=bool)[np.where(covered, positions, 0)]
        return pd.Series(pd.arrays.BooleanArray(values, ~covered))

    missing_rows = int((~covered).sum())
    note = (
        f"{missing_rows:,} of {n:,} customers are not in the run's holdout assignment, "
        "so their holdout flag is not known."
        if missing_rows
        else None
    )
    return _Flags(joined(HOLDOUT_MEMBER_COLUMN), joined(EXPLORE_COLUMN), joined(TREATED_COLUMN), note)


def _reasons(
    storage: Storage,
    run_id: str,
    scores: pd.DataFrame,
    row_keys: pd.Series[Any],
    config_root: Path | None,
) -> pd.DataFrame:
    """`reason_1..3` in business words, joined to the scores by key.

    A customer without a row in `row_explanations.parquet` keeps the text the scores file already
    holds for them, so a reason is never another customer's.
    """
    from engine.stages.explain import ROW_EXPLANATIONS_FILENAME

    n = len(scores.index)
    own = pd.DataFrame(
        {
            name: (
                scores[name].astype("object").where(scores[name].notna() & (scores[name] != ""), None)
                if name in scores.columns
                else pd.Series([None] * n, dtype="object")
            )
            for name in REASON_COLUMNS
        }
    )
    try:
        data = storage.read_bytes(run_key(run_id, ROW_EXPLANATIONS_FILENAME))
    except StorageError:
        return own
    table = pq.read_table(io.BytesIO(data))  # type: ignore[no-untyped-call]
    if "primary_key" not in table.column_names:
        return own
    mapped = extract_and_map_reasons_from_parquet(table, dictionary=load_reasons_dictionary(root=config_root))
    index = pd.Index(table["primary_key"].to_pandas().astype(str))
    if not index.is_unique:
        _LOGGER.warning("treat list %s: row_explanations.parquet repeats a customer key", run_id)
        return own
    positions = index.get_indexer(pd.Index(row_keys.astype(str)))
    found = positions >= 0
    out: dict[str, np.ndarray] = {}
    for name in REASON_COLUMNS:
        column = own[name].to_numpy(dtype=object).copy()
        column[found] = mapped[name].to_numpy(dtype=object)[positions[found]]
        out[name] = column
    return pd.DataFrame(out)


def _net_value(scores: pd.DataFrame, is_uplift: bool) -> tuple[pd.Series[Any], str | None, str | None]:
    """M97's `net_value` column of an uplift run's scores; null (with a note) when there is none.

    A propensity run has no incremental money, so its `net_value` is always null: its expected gross
    value goes in its own column.
    """
    n = len(scores.index)
    if is_uplift and NET_VALUE_COLUMN in scores.columns and scores[NET_VALUE_COLUMN].notna().any():
        net = pd.to_numeric(scores[NET_VALUE_COLUMN], errors="coerce").astype("float64")
        return net.round(_RUPEE_DECIMALS), None, "rupees"
    note = (
        _NO_NET_VALUE_NOTE
        if is_uplift
        else "Net value is incremental and needs an uplift run; see expected gross value for a propensity run."
    )
    return pd.Series(np.full(n, np.nan), dtype="float64"), note, None


def _expected_gross_value(
    storage: Storage,
    record: RunRecord,
    config: UseCaseConfig,
    scores: pd.DataFrame,
    row_keys: pd.Series[Any],
    is_uplift: bool,
) -> tuple[pd.Series[Any], str | None]:
    """M97's expected gross value per customer (rupees, not incremental) for a propensity run.

    Worked out with `engine.decide.value.expected_gross_values` from the costs, value column and score
    field the run recorded in `expected_gross_value.json` (written only when the run opted in), and the
    values the customer uploaded, joined on every key column. A value that is missing stays null.
    """
    n = len(scores.index)
    nothing = pd.Series(np.full(n, np.nan), dtype="float64")
    if is_uplift:
        return nothing, None
    try:
        report = storage.read_model(run_key(record.run_id, EXPECTED_GROSS_VALUE_FILENAME), ExpectedGrossValue)
    except StorageError:
        return nothing, _NO_GROSS_VALUE_NOTE
    try:
        values = _uploaded_values(storage, record, row_keys, report.value_column)
        probability = pd.to_numeric(scores[report.score_field], errors="coerce").to_numpy(dtype=np.float64)
    except (StorageError, KeyError, ValueError) as exc:
        _LOGGER.warning("treat list %s: expected gross value not worked out: %s", record.run_id, exc)
        return nothing, f"Expected gross value could not be worked out for this run: {exc}"
    costs = ValueCosts(offer_cost=report.offer_cost, contact_cost=report.contact_cost)
    gross = expected_gross_values(probability, values, config, value_costs=costs)
    return pd.Series(gross, dtype="float64").round(_RUPEE_DECIMALS), _GROSS_NOT_INCREMENTAL_NOTE


def _uploaded_values(
    storage: Storage, record: RunRecord, row_keys: pd.Series[Any], column: str
) -> np.ndarray:
    """The uploaded `column` per scored customer, joined on every key column (NaN where absent)."""
    from engine.onboarding.datasets import run_source_key
    from engine.stages.ingest import read_upload

    formats = ("parquet",) if record.dataset_id is not None else ("parquet", "csv")
    key = next((k for k in (run_source_key(record, fmt) for fmt in formats) if storage.exists(k)), None)
    if key is None:
        raise StorageError("KEY_NOT_FOUND", "the uploaded file is no longer stored")
    if key.endswith(".parquet"):
        source = pd.read_parquet(io.BytesIO(storage.read_bytes(key)))
    else:
        rows = read_upload(storage, key, file_format="csv", keep_all_rows=True).all_rows
        if rows is None:
            raise ValueError("the uploaded file could not be read in full")
        source = rows
    if column not in source.columns:
        raise KeyError(f"the uploaded file has no {column!r} column")
    lookup_keys = pd.Index(_join_keys(source, record.primary_key))
    if not lookup_keys.is_unique:
        raise ValueError("the uploaded file repeats a customer key")
    numbers = pd.to_numeric(source[column], errors="coerce").to_numpy(dtype=np.float64)
    positions = lookup_keys.get_indexer(pd.Index(row_keys))
    found = np.asarray(numbers[np.where(positions >= 0, positions, 0)], dtype=np.float64)
    return np.where(positions >= 0, found, np.nan)


def _total(values: pd.Series[Any], treat: np.ndarray) -> float | None:
    """Sum over the treat = 1 rows of the known values, or null when none is known."""
    numbers = values.to_numpy(dtype=np.float64)
    if not bool(np.isfinite(numbers).any()):
        return None
    return float(np.nansum(np.where(treat, numbers, 0.0)))


class _Channels(NamedTuple):
    """The channel each treated customer is sent on, and what the run said they are contactable on."""

    treat: np.ndarray
    channel: pd.Series[Any]
    contactable_channels: pd.Series[Any]
    channel_counts: dict[str, int] | None
    uncontactable_rows: int | None


def _read_contactability(
    storage: Storage, run_id: str, primary_key: Any, row_keys: pd.Series[Any], channels: tuple[str, ...]
) -> tuple[dict[str, np.ndarray], np.ndarray | None, dict[str, int] | None]:
    """Join `channel_contactability.parquet` to the scores on every key column (Plan J M99).

    Returns, per configured channel, whether each row may be contacted on it (True for a row the file
    does not cover: nothing restricts it), which rows the file covers (None when the run wrote no file:
    its use case configures no channels), and the run's `channel_counts`.
    """
    n = len(row_keys.index)
    try:
        data = storage.read_bytes(run_key(run_id, CHANNEL_CONTACTABILITY_FILENAME))
    except StorageError:
        return {}, None, None
    table = pd.read_parquet(io.BytesIO(data))
    key_cols = key_columns(primary_key)
    wanted = [contactable_column(channel) for channel in channels]
    if any(name not in table.columns for name in (*key_cols, *wanted)):
        _LOGGER.warning(
            "treat list %s: %s lacks the key or channel columns", run_id, CHANNEL_CONTACTABILITY_FILENAME
        )
        return {}, np.zeros(n, dtype=bool), None
    index = pd.Index(_join_keys(table, primary_key))
    if not index.is_unique:
        _LOGGER.warning("treat list %s: %s repeats a customer key", run_id, CHANNEL_CONTACTABILITY_FILENAME)
        return {}, np.zeros(n, dtype=bool), None
    positions = index.get_indexer(pd.Index(row_keys))
    covered = positions >= 0
    flags = {
        channel: np.where(covered, table[name].to_numpy(dtype=bool)[np.where(covered, positions, 0)], True)
        for channel, name in zip(channels, wanted, strict=True)
    }
    counts: dict[str, int] | None = None
    try:
        counts = dict(
            storage.read_model(
                run_key(run_id, CHANNEL_CONTACTABILITY_SUMMARY_FILENAME), ChannelContactability
            ).channel_counts
        )
    except (StorageError, ValueError):
        _LOGGER.warning(
            "treat list %s: %s could not be read", run_id, CHANNEL_CONTACTABILITY_SUMMARY_FILENAME
        )
    return flags, covered, counts


def _channel_labels(
    configured: tuple[str, ...], flags: dict[str, np.ndarray], covered: np.ndarray, n: int
) -> np.ndarray:
    """Per row, the channels it is contactable on (`"sms,email"`), None where the file does not cover it.

    Only the combinations present are labelled (`np.unique` over the rows' flag patterns): at most
    min(rows, 2**channels) labels, so the work is linear in rows and never exponential in channels.
    """
    text = np.full(n, "", dtype=object)
    if configured and n:
        matrix = np.column_stack(
            [np.asarray(flags.get(channel, np.ones(n, dtype=bool)), dtype=bool) for channel in configured]
        )
        present, inverse = np.unique(matrix, axis=0, return_inverse=True)
        labels = np.array(
            [",".join(name for name, on in zip(configured, row, strict=True) if on) for row in present],
            dtype=object,
        )
        text = labels[np.asarray(inverse).reshape(-1)].copy()
    text[~covered] = None
    return text


def _read_stamp(storage: Storage, run_id: str) -> CatalogueStamp | None:
    """The catalogue the run ran under (`catalogue_stamp.json`), or None when it had none."""
    key = run_key(run_id, CATALOGUE_STAMP_FILENAME)
    if not storage.exists(key):
        return None
    try:
        return storage.read_model(key, CatalogueStamp)
    except (StorageError, ValueError):
        _LOGGER.warning("treat list %s: %s could not be read", run_id, CATALOGUE_STAMP_FILENAME)
        return None


def _catalogue_note(run_id: str, at_run: str | None, now: str | None) -> str | None:
    """Why the catalogue file today is not the run's, or None when it is (or neither exists)."""
    if at_run == now:
        return None
    if at_run is None:
        note = (
            "configs/decide/catalogue.yaml was added after the run, which had no catalogue; channels are "
            "planned from the run's configured channels, not from the new file."
        )
    elif now is None:
        note = (
            "configs/decide/catalogue.yaml was removed after the run; channels are planned from the "
            "catalogue the run ran under (catalogue_stamp.json)."
        )
    else:
        note = (
            "configs/decide/catalogue.yaml has changed since the run; channels are planned from the "
            "catalogue the run ran under (catalogue_stamp.json), whose sha256 is catalogue_sha256."
        )
    _LOGGER.warning("treat list %s: %s", run_id, note)
    return note


def _resolve_channels(
    storage: Storage,
    run_id: str,
    primary_key: Any,
    row_keys: pd.Series[Any],
    *,
    scores: pd.DataFrame,
    config: UseCaseConfig,
    treat: np.ndarray,
    is_uplift: bool,
    planned_by_action: dict[str, tuple[str, ...]],
) -> _Channels:
    """Each treated customer's channel, and the treat flag of a customer with no planned channel open.

    The planned channels are the catalogue action's (`Band.action_id`, or `uplift.policy.treat_action_id`
    on an uplift run), in its order, as the run recorded them (`planned_by_action`, from
    `catalogue_stamp.json` - never the catalogue file as it is now); with no recorded catalogue action
    they are the configured channels (`actions.suppression.channels`), in theirs. A customer is sent the first planned channel the run's
    `channel_contactability.parquet` says they are contactable on; one contactable on none of them is
    not treated - and not suppressed: `suppression_reason` and `scores.csv` are untouched. A customer the
    file does not cover (and every customer of a run that wrote none) is treated as before, on the first
    planned channel, with a null `contactable_channels`.
    """
    n = len(scores.index)
    configured = tuple(config.actions.suppression.channels)
    flags, covered, counts = _read_contactability(storage, run_id, primary_key, row_keys, configured)

    def planned(action_id: str | None) -> tuple[str, ...]:
        if action_id is not None and action_id in planned_by_action:
            return tuple(planned_by_action[action_id])
        return configured

    everyone = np.ones(n, dtype=bool)
    if is_uplift:
        policy = getattr(getattr(config, "uplift", None), "policy", None)
        groups = [(everyone, planned(getattr(policy, "treat_action_id", None)))]
    else:
        bands = scores[BAND_COLUMN].astype(str).to_numpy(dtype=object)
        groups = [(bands == band.name, planned(band.action_id)) for band in config.actions.bands]

    treat_out = treat.copy()
    chosen = np.full(n, None, dtype=object)
    for rows, channels in groups:
        if not channels or not rows.any():
            continue
        first_open = np.full(n, None, dtype=object)
        any_open = np.zeros(n, dtype=bool)
        for channel in reversed(channels):
            open_ = flags.get(channel, everyone)
            first_open = np.where(open_, channel, first_open)
            any_open |= open_
        treat_out[rows & ~any_open] = False
        sent = rows & treat_out
        chosen[sent] = first_open[sent]

    if covered is None:
        contactable = pd.Series([None] * n, dtype="object")
    else:
        contactable = pd.Series(_channel_labels(configured, flags, covered, n), dtype="object")
    return _Channels(
        treat=treat_out,
        channel=pd.Series(chosen, dtype="object"),
        contactable_channels=contactable,
        channel_counts=counts,
        uncontactable_rows=None if covered is None else int((treat & ~treat_out).sum()),
    )


class _OfferChoice(NamedTuple):
    """`offer_choice.parquet` joined to the scores on every key column (Plan J M100 part B)."""

    covered: np.ndarray
    arm: np.ndarray
    label: np.ndarray
    channel: np.ndarray
    net_value: np.ndarray
    runner_up_arm: np.ndarray
    runner_up_label: np.ndarray
    runner_up_channel: np.ndarray
    runner_up_value: np.ndarray
    reason: np.ndarray


def _read_offer_choice(
    storage: Storage, run_id: str, primary_key: Any, row_keys: pd.Series[Any]
) -> _OfferChoice | None:
    """The run's choice of offer per customer, or None when it made none (a run of one offer, or a run
    of several with no value to choose by: its treat list is then the first offer's, as before)."""
    from engine.decide.offer_run import OFFER_CHOICE_FILENAME

    try:
        data = storage.read_bytes(run_key(run_id, OFFER_CHOICE_FILENAME))
    except StorageError:
        return None
    table = pd.read_parquet(io.BytesIO(data))
    index = pd.Index(_join_keys(table, primary_key))
    if not index.is_unique:
        _LOGGER.warning("treat list %s: %s repeats a customer key", run_id, OFFER_CHOICE_FILENAME)
        return None
    positions = index.get_indexer(pd.Index(row_keys))
    covered = positions >= 0
    at = np.where(covered, positions, 0)

    def take(name: str, missing: Any) -> np.ndarray:
        values = table[name].to_numpy()[at]
        return np.where(covered, values, missing)

    return _OfferChoice(
        covered=covered,
        arm=take("offer_arm", 0).astype(np.int64),
        label=take("offer_label", None).astype(object),
        channel=take("offer_channel", None).astype(object),
        net_value=take("offer_net_value", np.nan).astype(np.float64),
        runner_up_arm=take("runner_up_arm", 0).astype(np.int64),
        runner_up_label=take("runner_up_label", None).astype(object),
        runner_up_channel=take("runner_up_channel", None).astype(object),
        runner_up_value=take("runner_up_net_value", np.nan).astype(np.float64),
        reason=take("offer_reason", None).astype(object),
    )


class _Applied(NamedTuple):
    treat: np.ndarray
    offer: pd.Series[Any]
    channel: pd.Series[Any]
    net_value: pd.Series[Any]
    runner_up_offer: pd.Series[Any]
    runner_up_value: pd.Series[Any]
    offer_reason: pd.Series[Any]
    uncontactable_rows: int | None
    offer_counts: dict[str, int]


def _apply_offer_choice(
    chosen: _OfferChoice,
    *,
    suppressed: np.ndarray,
    held_out: np.ndarray,
    explore: np.ndarray,
    fallback_treat: np.ndarray,
    fallback_offer: pd.Series[Any],
    fallback_channel: pd.Series[Any],
    fallback_net_value: pd.Series[Any],
    contactability_written: bool,
) -> _Applied:
    """The treat flag, offer, channel and money of a run that chose the offer per customer.

    A customer is treated with the offer the run chose (never suppressed, held out, or given an offer
    they are a sleeping dog for or cannot be reached on: the run's choice already says so). An explored
    customer (M92) the choice left without an offer is given the best offer they could be given (the
    choice's runner-up), outside the budget, as M92 treats them outside the policy; one with no such
    offer is not treated. A customer the file does not cover keeps what the list said before.
    """
    from engine.decide.offer_run import OFFER_REASON_TEXT

    covered = chosen.covered
    open_ = ~suppressed & ~held_out
    offered = covered & (chosen.arm > 0) & open_
    explored = covered & ~offered & open_ & explore & (chosen.runner_up_arm > 0)
    treat = np.where(covered, offered | explored, fallback_treat)

    nothing = np.full(len(covered), None, dtype=object)
    label = np.where(offered, chosen.label, np.where(explored, chosen.runner_up_label, nothing))
    channel = np.where(offered, chosen.channel, np.where(explored, chosen.runner_up_channel, nothing))
    value = np.where(offered, chosen.net_value, np.where(explored, chosen.runner_up_value, np.nan))
    offer = fallback_offer.mask(pd.Series(covered), pd.Series(label, dtype="object"))
    channel_out = fallback_channel.mask(pd.Series(covered), pd.Series(channel, dtype="object"))
    net = fallback_net_value.mask(pd.Series(covered), pd.Series(np.round(value, _RUPEE_DECIMALS)))

    has_runner = covered & (chosen.runner_up_arm > 0) & ~explored
    runner_offer = pd.Series(np.where(has_runner, chosen.runner_up_label, nothing), dtype="object")
    runner_value = pd.Series(
        np.round(np.where(has_runner, chosen.runner_up_value, np.nan), _RUPEE_DECIMALS), dtype="float64"
    )
    told = covered & open_ & ~treat & (chosen.reason != "offer")
    texts = np.array([OFFER_REASON_TEXT.get(str(code)) for code in chosen.reason], dtype=object)
    reason = pd.Series(np.where(told, texts, nothing), dtype="object")
    unreachable = int((told & (chosen.reason == "no_eligible_offer")).sum())
    labels, counts = np.unique(label[treat & covered].astype(str), return_counts=True)
    return _Applied(
        treat=np.asarray(treat, dtype=bool),
        offer=offer,
        channel=channel_out,
        net_value=net.astype("float64"),
        runner_up_offer=runner_offer,
        runner_up_value=runner_value,
        offer_reason=reason,
        uncontactable_rows=unreachable if contactability_written else None,
        offer_counts={str(name): int(count) for name, count in zip(labels, counts, strict=True)},
    )


def _channel_rows(channel: pd.Series[Any], treat: np.ndarray) -> dict[str, int] | None:
    """treat = 1 rows per channel, or None when no treated row has a channel."""
    sent = channel[pd.Series(treat) & channel.notna()]
    if sent.empty:
        return None
    return {str(name): int(count) for name, count in sent.value_counts(sort=False).sort_index().items()}


# ---------------------------------------------------------------------------
# Assembling and writing
# ---------------------------------------------------------------------------
def _assemble(
    *,
    scores: pd.DataFrame,
    key_cols: tuple[str, ...],
    record: RunRecord,
    is_uplift: bool,
    treat: np.ndarray,
    flags: _Flags,
    suppression: pd.Series[Any],
    offer: pd.Series[Any],
    channel: pd.Series[Any],
    contactable_channels: pd.Series[Any],
    net_value: pd.Series[Any],
    gross_value: pd.Series[Any],
    reasons: pd.DataFrame,
    runner_up_offer: pd.Series[Any],
    runner_up_value: pd.Series[Any],
    offer_reason: pd.Series[Any],
) -> pd.DataFrame:
    n = len(scores.index)
    data: dict[str, Any] = {name: key_text(scores[name]).astype("object") for name in key_cols}
    data["use_case"] = pd.Series([record.use_case_id] * n, dtype="object")
    data["model_version"] = pd.Series([record.model_version_id] * n, dtype="object")
    group = _SEGMENT_COLUMN if is_uplift else BAND_COLUMN
    data[group] = scores[group].astype("object")
    data["treat"] = pd.Series(treat, dtype="bool")
    data["holdout"] = flags.holdout
    data["explore"] = flags.explore
    data["suppression_reason"] = suppression.astype("object")
    data["offer"] = offer.astype("object")
    data["channel"] = channel.astype("object")
    data["contactable_channels"] = contactable_channels.astype("object")
    # Plan J M100 part B: the next-best offer and its net value, and why a customer got no offer.
    data[RUNNER_UP_OFFER_COLUMN] = runner_up_offer.astype("object")
    data[RUNNER_UP_VALUE_COLUMN] = runner_up_value
    data[OFFER_REASON_COLUMN] = offer_reason.astype("object")
    data["net_value"] = net_value
    data[EXPECTED_GROSS_VALUE_COLUMN] = gross_value
    for name in REASON_COLUMNS:
        data[name] = reasons[name].astype("object")
    return pd.DataFrame(data)


def _schema(is_uplift: bool, key_cols: tuple[str, ...]) -> pa.Schema:
    fields = [pa.field(name, pa.string()) for name in key_cols]
    fields += [
        pa.field("use_case", pa.string()),
        pa.field("model_version", pa.string()),
        pa.field(_SEGMENT_COLUMN if is_uplift else BAND_COLUMN, pa.string()),
        pa.field("treat", pa.bool_()),
        pa.field("holdout", pa.bool_()),
        pa.field("explore", pa.bool_()),
        pa.field("suppression_reason", pa.string()),
        pa.field("offer", pa.string()),
        pa.field("channel", pa.string()),
        pa.field("contactable_channels", pa.string()),
        pa.field(RUNNER_UP_OFFER_COLUMN, pa.string()),
        pa.field(RUNNER_UP_VALUE_COLUMN, pa.float64()),
        pa.field(OFFER_REASON_COLUMN, pa.string()),
        pa.field("net_value", pa.float64()),
        pa.field(EXPECTED_GROSS_VALUE_COLUMN, pa.float64()),
        *[pa.field(name, pa.string()) for name in REASON_COLUMNS],
    ]
    return pa.schema(fields)


def _table(frame: pd.DataFrame, is_uplift: bool, key_cols: tuple[str, ...]) -> pa.Table:
    """The treat list as one typed Arrow table: booleans (null where unknown), floats, strings."""
    return pa.Table.from_pandas(frame, schema=_schema(is_uplift, key_cols), preserve_index=False)


def _parquet_bytes(table: pa.Table) -> bytes:
    """Typed parquet: booleans stay booleans (null where unknown), money stays a float."""
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
    return buffer.getvalue()


_NEEDS_QUOTES: Final[str] = '[",\r\n]'


def _csv_text(column: pa.ChunkedArray) -> pa.Array:
    """One column as CSV text: flags `1` / `0`, numbers as Arrow prints them, null as empty, strings quoted
    only when they hold a comma, a quote or a line break."""
    values = column.combine_chunks()
    if pa.types.is_boolean(values.type):
        return pc.cast(pc.fill_null(pc.if_else(values, "1", "0"), ""), pa.large_string())
    text = pc.fill_null(pc.cast(values, pa.large_string()), "")
    if pa.types.is_floating(values.type):
        return text
    quote = pa.scalar('"', pa.large_string())
    quoted = pc.binary_join_element_wise(
        quote, pc.replace_substring(text, '"', '""'), quote, pa.scalar("", pa.large_string())
    )
    return pc.if_else(pc.match_substring_regex(text, _NEEDS_QUOTES), quoted, text)


def _csv_bytes(table: pa.Table) -> bytes:
    """The CSV, written column-wise by Arrow's compute kernels (no loop over the rows), `\\n` line ends."""
    header = io.StringIO()
    csv.writer(header, lineterminator="\n").writerow(table.column_names)
    if table.num_rows == 0:
        return header.getvalue().encode("utf-8")
    comma, newline = pa.scalar(",", pa.large_string()), pa.scalar("\n", pa.large_string())
    lines = pc.binary_join_element_wise(*[_csv_text(table[name]) for name in table.column_names], comma)
    one_list = pa.ListArray.from_arrays(pa.array([0, len(lines)], pa.int32()), lines)
    body = pc.binary_join(one_list, newline)[0]
    # The joined text is copied out as bytes, never decoded to a Python string and encoded again.
    return header.getvalue().encode("utf-8") + bytes(body.as_buffer()) + b"\n"
