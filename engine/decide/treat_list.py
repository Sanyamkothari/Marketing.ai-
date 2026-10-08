"""The treat list builder: one downloadable file per scoring run (Plan J M98, DEC-1308).

Pure builder reading a finished scoring run's artefacts and writing `treat_list.csv`,
`treat_list.parquet`, and `treat_list_summary.json` beside them in storage.
It is never imported by the pipeline's stages and adds no method rebinds.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field

from engine.config import ProblemType, UseCaseConfig, load_use_case
from engine.contracts import Artefact, RunRecord, RunState
from engine.decide.reasons import extract_and_map_reasons_from_parquet, load_reasons_dictionary
from engine.holdout.assign import (
    EXPLORE_COLUMN,
    HOLDOUT_ASSIGNMENT_FILENAME,
    HOLDOUT_MEMBER_COLUMN,
)
from engine.keys import key_columns, key_text
from engine.runs import RUN_FILENAME
from engine.stages.actions import (
    ACTION_COLUMN,
    BAND_COLUMN,
    CONTROL_GROUP_COLUMN,
    SUPPRESSED_ACTION,
    SUPPRESSED_REASON_COLUMN,
)
from engine.stages.export import SCORES_CSV, SCORES_PARQUET
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    pass

__all__ = [
    "TREAT_LIST_CSV",
    "TREAT_LIST_PARQUET",
    "TREAT_LIST_SUMMARY_FILENAME",
    "TreatListSummary",
    "build_treat_list",
    "ensure_treat_list",
]

_LOGGER = get_logger(__name__)

TREAT_LIST_CSV: Final[str] = "treat_list.csv"
TREAT_LIST_PARQUET: Final[str] = "treat_list.parquet"
TREAT_LIST_SUMMARY_FILENAME: Final[str] = "treat_list_summary.json"

_SLEEPING_DOG: Final[str] = "sleeping_dog"
_TREAT_ACTION: Final[str] = "Treat"


class TreatListSummary(Artefact):
    """Aggregate summary of `treat_list.parquet`: counts and net value in rupees."""

    run_id: str = Field(description="Run the treat list was built for.")
    use_case_id: str = Field(description="Use case of the run.")
    total_rows: int = Field(description="Total rows in the treat list.")
    treat_rows: int = Field(description="Rows with treat = 1.")
    holdout_rows: int | None = Field(
        default=None,
        description="Rows with holdout = 1, or null when holdout assignment is unavailable.",
    )
    explore_rows: int = Field(default=0, description="Rows with explore = 1.")
    suppressed_rows: int = Field(default=0, description="Rows with a suppression reason.")
    net_value_total: float | None = Field(
        default=None,
        description="Total expected net value across treat rows, in rupees, or null if not configured.",
    )
    net_value_unit: str | None = Field(
        default=None,
        description="Unit of net value ('rupees'), or null.",
    )
    holdout_note: str | None = Field(
        default=None,
        description="Explanation when holdout flags could not be determined.",
    )
    net_value_note: str | None = Field(
        default=None,
        description="Explanation when net value was not configured or is not incremental.",
    )
    created_at: datetime = Field(description="UTC time the treat list was built.")


def ensure_treat_list(storage: Storage, run_id: str) -> TreatListSummary:
    """Ensure `treat_list.csv`, `treat_list.parquet`, and `treat_list_summary.json` exist.

    If already present in storage, reads and returns `treat_list_summary.json`.
    Otherwise, builds them on demand.
    """
    summary_key = run_key(run_id, TREAT_LIST_SUMMARY_FILENAME)
    try:
        return storage.read_model(summary_key, TreatListSummary)
    except StorageError:
        return build_treat_list(storage, run_id)


def build_treat_list(storage: Storage, run_id: str) -> TreatListSummary:
    """Pure builder that creates the treat list from existing run artefacts."""
    record_key = run_key(run_id, RUN_FILENAME)
    record = storage.read_model(record_key, RunRecord)
    if record.state is not RunState.DONE:
        raise StorageError("RUN_NOT_DONE", f"Run {run_id!r} is not done (state is {record.state}).")

    config: UseCaseConfig = load_use_case(record.use_case_id)
    primary_key = record.primary_key
    key_cols = key_columns(primary_key)

    # 1. Read scores (parquet preferred, fallback to CSV)
    scores_df = _read_scores(storage, run_id)
    n_rows = len(scores_df)

    # 2. Read holdout_assignment.parquet (if available)
    holdout_df = _read_holdout_assignment(storage, run_id)

    # 3. Read explanations from row_explanations.parquet or scores
    reasons_df = _read_reasons(storage, run_id, scores_df)

    # 4. Determine run kind
    is_uplift = record.problem_type is ProblemType.UPLIFT or "segment" in scores_df.columns

    # 5. Build core flags: treat, holdout, explore
    holdout_series: pd.Series
    explore_series: pd.Series
    holdout_note: str | None = None

    if holdout_df is not None and HOLDOUT_MEMBER_COLUMN in holdout_df.columns:
        # Match by key or direct row alignment if lengths match
        if len(holdout_df) == n_rows:
            holdout_series = holdout_df[HOLDOUT_MEMBER_COLUMN].astype(bool)
            explore_series = (
                holdout_df[EXPLORE_COLUMN].astype(bool)
                if EXPLORE_COLUMN in holdout_df.columns
                else pd.Series(False, index=scores_df.index)
            )
        else:
            # Join by entity key
            k_col = key_cols[0]
            lookup = holdout_df.set_index(k_col)
            s_keys = scores_df[k_col].astype(str)
            holdout_series = s_keys.map(lookup[HOLDOUT_MEMBER_COLUMN]).astype(bool)
            explore_series = (
                s_keys.map(lookup[EXPLORE_COLUMN]).astype(bool)
                if EXPLORE_COLUMN in lookup.columns
                else pd.Series(False, index=scores_df.index)
            )
    else:
        # Crucial rule: missing holdout_assignment -> holdout is null (None), NOT False!
        holdout_series = pd.Series([None] * n_rows, index=scores_df.index, dtype="object")
        explore_series = pd.Series(False, index=scores_df.index, dtype=bool)
        holdout_note = "The run has no holdout assignment file, so the holdout flag is not known."

    # Determine suppression reason
    if SUPPRESSED_REASON_COLUMN in scores_df.columns:
        suppression_reason = scores_df[SUPPRESSED_REASON_COLUMN].where(
            scores_df[SUPPRESSED_REASON_COLUMN].notna(), None
        )
    else:
        suppression_reason = pd.Series([None] * n_rows, index=scores_df.index, dtype="object")

    is_suppressed = suppression_reason.notna()

    # Determine treat flag
    # Invariant: treat_flag=1 => holdout_flag != 1, suppress_reason is null, no sleeping dog
    is_holdout = (
        holdout_series.astype(bool)
        if (holdout_df is not None and holdout_df[HOLDOUT_MEMBER_COLUMN].dtype == bool)
        else (
            scores_df[CONTROL_GROUP_COLUMN].astype(bool)
            if CONTROL_GROUP_COLUMN in scores_df.columns
            else pd.Series(False, index=scores_df.index)
        )
    )

    if is_uplift:
        segment_col = scores_df["segment"].astype(str)
        is_sleeping_dog = segment_col == _SLEEPING_DOG
        if "action" in scores_df.columns:
            selected = scores_df["action"] == _TREAT_ACTION
        elif "intended_treatment" in scores_df.columns:
            selected = scores_df["intended_treatment"].astype(bool)
        else:
            selected = pd.Series(False, index=scores_df.index)
        treat_series = ((selected | explore_series) & ~is_suppressed & ~is_holdout & ~is_sleeping_dog).astype(
            bool
        )
    else:
        floor = config.actions.bands[-1].name
        if BAND_COLUMN in scores_df.columns:
            selected = scores_df[BAND_COLUMN].astype(str) != floor
        else:
            selected = pd.Series(True, index=scores_df.index)
        treat_series = ((selected | explore_series) & ~is_suppressed & ~is_holdout).astype(bool)

    # 6. Offer and channel
    # Offer is the row's action when unsuppressed; None when suppressed
    if ACTION_COLUMN in scores_df.columns:
        offer_series = scores_df[ACTION_COLUMN].where(
            ~is_suppressed & (scores_df[ACTION_COLUMN] != SUPPRESSED_ACTION), None
        )
    else:
        offer_series = pd.Series([None] * n_rows, index=scores_df.index, dtype="object")

    channel_series = pd.Series([None] * n_rows, index=scores_df.index, dtype="object")

    # 7. Net value (rupees)
    net_value_series, net_value_note, net_value_unit = _resolve_net_value(
        storage, record, config, scores_df, is_uplift
    )

    # 8. Assemble treat list table
    cols_dict: dict[str, Any] = {}
    for col in key_cols:
        cols_dict[col] = key_text(scores_df[col]).to_numpy()

    cols_dict["use_case"] = np.full(n_rows, record.use_case_id, dtype=object)
    cols_dict["model_version"] = np.full(n_rows, record.model_version_id, dtype=object)

    if is_uplift:
        cols_dict["segment"] = scores_df["segment"].to_numpy()
    else:
        cols_dict["band"] = (
            scores_df[BAND_COLUMN].to_numpy()
            if BAND_COLUMN in scores_df.columns
            else np.full(n_rows, None, dtype=object)
        )

    cols_dict["treat"] = treat_series.to_numpy(dtype=bool)
    # holdout is nullable boolean: True, False, or None
    cols_dict["holdout"] = holdout_series.to_numpy(dtype=object)
    cols_dict["explore"] = explore_series.to_numpy(dtype=bool)
    cols_dict["suppression_reason"] = suppression_reason.to_numpy(dtype=object)
    cols_dict["offer"] = offer_series.to_numpy(dtype=object)
    cols_dict["channel"] = channel_series.to_numpy(dtype=object)
    cols_dict["net_value"] = net_value_series.to_numpy(dtype=object)

    for r_col in ("reason_1", "reason_2", "reason_3"):
        cols_dict[r_col] = reasons_df[r_col].to_numpy(dtype=object)

    out_df = pd.DataFrame(cols_dict)

    # 9. Write treat_list.parquet
    parquet_buf = io.BytesIO()
    _write_treat_list_parquet(out_df, is_uplift, key_cols, parquet_buf)
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), parquet_buf.getvalue())

    # 10. Write treat_list.csv (flags as 1/0, nulls as empty)
    csv_buf = io.StringIO()
    _write_treat_list_csv(out_df, csv_buf)
    storage.write_bytes(run_key(run_id, TREAT_LIST_CSV), csv_buf.getvalue().encode("utf-8"))

    # 11. Write summary JSON
    treat_rows = int(treat_series.sum())
    holdout_rows = (
        int(holdout_series.sum())
        if (holdout_df is not None and holdout_df[HOLDOUT_MEMBER_COLUMN].dtype == bool)
        else None
    )
    explore_rows = int(explore_series.sum())
    suppressed_rows = int(is_suppressed.sum())

    total_net_val: float | None = None
    if net_value_series.notna().any():
        valid_val = pd.to_numeric(net_value_series, errors="coerce")
        # Sum of net value across treat rows
        treat_mask = treat_series.to_numpy(dtype=bool)
        val_sum = float(valid_val[treat_mask].sum(skipna=True))
        total_net_val = val_sum

    now = utc_now()
    summary = TreatListSummary(
        run_id=run_id,
        use_case_id=record.use_case_id,
        total_rows=n_rows,
        treat_rows=treat_rows,
        holdout_rows=holdout_rows,
        explore_rows=explore_rows,
        suppressed_rows=suppressed_rows,
        net_value_total=total_net_val,
        net_value_unit=net_value_unit,
        holdout_note=holdout_note,
        net_value_note=net_value_note,
        created_at=now,
    )
    storage.write_model(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME), summary)

    _LOGGER.info(
        "Built treat list for run %s: %d rows (%d treat, %s holdout, %d explore)",
        run_id,
        n_rows,
        treat_rows,
        str(holdout_rows),
        explore_rows,
    )
    return summary


def _read_scores(storage: Storage, run_id: str) -> pd.DataFrame:
    """Read scores.parquet or scores.csv."""
    parquet_key = run_key(run_id, SCORES_PARQUET)
    try:
        data = storage.read_bytes(parquet_key)
        return pd.read_parquet(io.BytesIO(data))
    except StorageError:
        pass

    csv_key = run_key(run_id, SCORES_CSV)
    try:
        data = storage.read_bytes(csv_key)
        return pd.read_csv(io.BytesIO(data))
    except StorageError as exc:
        raise StorageError(
            "KEY_NOT_FOUND", f"Neither scores.parquet nor scores.csv found for run {run_id!r}."
        ) from exc


def _read_holdout_assignment(storage: Storage, run_id: str) -> pd.DataFrame | None:
    """Read holdout_assignment.parquet if present."""
    key = run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME)
    try:
        data = storage.read_bytes(key)
        return pd.read_parquet(io.BytesIO(data))
    except StorageError:
        return None


def _read_reasons(
    storage: Storage,
    run_id: str,
    scores_df: pd.DataFrame,
) -> pd.DataFrame:
    """Extract and map business reasons from row_explanations.parquet or scores_df."""
    from engine.stages.explain import ROW_EXPLANATIONS_FILENAME

    dict_obj = load_reasons_dictionary()
    key = run_key(run_id, ROW_EXPLANATIONS_FILENAME)
    try:
        data = storage.read_bytes(key)
        table = pq.read_table(io.BytesIO(data))  # type: ignore[no-untyped-call]
        mapped_df = extract_and_map_reasons_from_parquet(table, dictionary=dict_obj)
        if len(mapped_df) == len(scores_df):
            return mapped_df
    except StorageError:
        pass

    # Fallback to scores columns if reasons exist
    n = len(scores_df)
    r1 = (
        scores_df["reason_1"].where(scores_df["reason_1"].notna(), None)
        if "reason_1" in scores_df.columns
        else pd.Series([None] * n)
    )
    r2 = (
        scores_df["reason_2"].where(scores_df["reason_2"].notna(), None)
        if "reason_2" in scores_df.columns
        else pd.Series([None] * n)
    )
    r3 = (
        scores_df["reason_3"].where(scores_df["reason_3"].notna(), None)
        if "reason_3" in scores_df.columns
        else pd.Series([None] * n)
    )
    return pd.DataFrame({"reason_1": r1, "reason_2": r2, "reason_3": r3})


def _resolve_net_value(
    storage: Storage,
    record: RunRecord,
    config: UseCaseConfig,
    scores_df: pd.DataFrame,
    is_uplift: bool,
) -> tuple[pd.Series, str | None, str | None]:
    """Retrieve per-customer net value in rupees, or null with note."""
    n = len(scores_df)
    if is_uplift:
        if "net_value" in scores_df.columns and scores_df["net_value"].notna().any():
            return scores_df["net_value"], None, "rupees"
        return pd.Series([None] * n, dtype="object"), "Net value was not configured for this run.", None

    # Propensity run: check expected_gross_values if configured
    val_col = config.uplift.policy.value_column
    if val_col is not None and record.upload_id is not None:
        try:
            from engine.decide.value import expected_gross_values
            from engine.storage import upload_key

            upload_bytes = storage.read_bytes(upload_key(record.upload_id, "source.parquet"))
            up_df = pd.read_parquet(io.BytesIO(upload_bytes))
            if val_col in up_df.columns and config.actions.score_field in scores_df.columns:
                p = pd.to_numeric(scores_df[config.actions.score_field], errors="coerce").to_numpy(
                    dtype=np.float64
                )
                # Align values
                k_col = key_columns(record.primary_key)[0]
                lookup = pd.Series(
                    pd.to_numeric(up_df[val_col], errors="coerce").to_numpy(), index=key_text(up_df[k_col])
                )
                v = lookup.reindex(key_text(scores_df[k_col])).to_numpy(dtype=np.float64)
                gross = expected_gross_values(p, v, config)
                res_series = pd.Series(
                    [None if np.isnan(val) else float(val) for val in gross], index=scores_df.index
                )
                return res_series, "Expected gross value is not incremental.", "rupees"
        except Exception:  # pragma: no cover
            pass

    return pd.Series([None] * n, dtype="object"), "Net value was not configured for this run.", None


def _write_treat_list_parquet(
    df: pd.DataFrame, is_uplift: bool, key_cols: tuple[str, ...], buffer: io.BytesIO
) -> None:
    """Write DataFrame to Parquet with typed schema."""
    fields = [pa.field(col, pa.string()) for col in key_cols]
    fields.extend(
        [
            pa.field("use_case", pa.string()),
            pa.field("model_version", pa.string()),
            pa.field("segment" if is_uplift else "band", pa.string()),
            pa.field("treat", pa.bool_()),
            pa.field("holdout", pa.bool_()),
            pa.field("explore", pa.bool_()),
            pa.field("suppression_reason", pa.string()),
            pa.field("offer", pa.string()),
            pa.field("channel", pa.string()),
            pa.field("net_value", pa.float64()),
            pa.field("reason_1", pa.string()),
            pa.field("reason_2", pa.string()),
            pa.field("reason_3", pa.string()),
        ]
    )
    schema = pa.schema(fields)

    arrays = []
    for f in schema:
        name = f.name
        val = df[name]
        if f.type == pa.bool_():
            # Handle nullable bools
            arrays.append(pa.array(val, type=pa.bool_()))
        elif f.type == pa.float64():
            arrays.append(pa.array(pd.to_numeric(val, errors="coerce"), type=pa.float64()))
        else:
            arrays.append(pa.array(val.where(val.notna(), None), type=pa.string()))

    table = pa.Table.from_arrays(arrays, schema=schema)
    pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]


def _write_treat_list_csv(df: pd.DataFrame, buffer: io.StringIO) -> None:
    """Write treat list as CSV with 1/0 for boolean flags and empty strings for nulls."""
    csv_df = df.copy()
    # Format boolean flags as 1/0
    csv_df["treat"] = csv_df["treat"].apply(lambda x: "1" if x is True else "0")
    csv_df["holdout"] = csv_df["holdout"].apply(lambda x: "1" if x is True else ("0" if x is False else ""))
    csv_df["explore"] = csv_df["explore"].apply(lambda x: "1" if x is True else "0")

    csv_df.to_csv(buffer, index=False, na_rep="", lineterminator="\n")
