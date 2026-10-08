"""Cross-use-case arbitration (Plan J M101, DEC-1311).

Arbitrates between multiple finished scoring runs over the same customer key:
- Each customer receives at most one action per cycle across all use cases (or within contact cap).
- Chosen deterministically by priority weight x net value (M97).
- Holdout members stay untouched across every use case; explore rows preserve randomisation (M92).
- Records winning use case, losing actions, priority score, and conflicts summary.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from engine.contracts import Artefact
from engine.decide.treat_list import (
    _RUPEE_DECIMALS,
    _SEGMENT_COLUMN,
    EXPECTED_GROSS_VALUE_COLUMN,
    REASON_COLUMNS,
)
from engine.keys import KEY_SEPARATOR, key_text
from engine.stages.actions import BAND_COLUMN
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "ARBITRATED_TREAT_LIST_CSV",
    "ARBITRATED_TREAT_LIST_PARQUET",
    "ARBITRATION_CONFIG_FILE",
    "ARBITRATION_SUMMARY_FILENAME",
    "LOSING_ACTIONS_COLUMN",
    "PRIORITY_SCORE_COLUMN",
    "WINNING_USE_CASE_COLUMN",
    "ArbitrationConfig",
    "ArbitrationError",
    "ArbitrationSummary",
    "UseCasePriorityConfig",
    "arbitrate_treat_lists",
    "arbitrated_table",
    "csv_bytes",
    "load_arbitration_config",
    "parquet_bytes",
]

_LOGGER = get_logger(__name__)

ARBITRATED_TREAT_LIST_CSV: Final[str] = "arbitrated_treat_list.csv"
ARBITRATED_TREAT_LIST_PARQUET: Final[str] = "arbitrated_treat_list.parquet"
ARBITRATION_SUMMARY_FILENAME: Final[str] = "arbitration_summary.json"
ARBITRATION_CONFIG_FILE: Final[str] = "decide/arbitration.yaml"

WINNING_USE_CASE_COLUMN: Final[str] = "winning_use_case"
LOSING_ACTIONS_COLUMN: Final[str] = "losing_actions"
PRIORITY_SCORE_COLUMN: Final[str] = "priority_score"

_DEFAULT_CONFIG_PATH = Path("configs/decide/arbitration.yaml")


class ArbitrationError(Exception):
    """The arbitration run failed or inputs were invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UseCasePriorityConfig(BaseModel):
    """Priority weight for a use case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: float = Field(default=1.0, ge=0.0, description="Priority multiplier (default: 1.0).")


class ArbitrationConfig(BaseModel):
    """Configuration for cross-use-case arbitration (`configs/decide/arbitration.yaml`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    use_cases: dict[str, UseCasePriorityConfig] = Field(
        default_factory=dict, description="Use case specific priorities."
    )
    contact_cap_per_customer: int = Field(
        default=1, ge=1, description="Max treatments per customer across use cases."
    )
    channel_caps: dict[str, int] = Field(
        default_factory=dict, description="Max treatments allowed per channel."
    )

    def priority_for(self, use_case_id: str) -> float:
        if use_case_id in self.use_cases:
            return float(self.use_cases[use_case_id].priority)
        return 1.0


class ArbitrationSummary(Artefact):
    """Summary of cross-use-case arbitration results and conflicts."""

    total_customers: int = Field(description="Total distinct customers evaluated.")
    customers_with_actions: int = Field(description="Customers eligible for at least one treatment.")
    customers_with_conflicts: int = Field(
        description="Customers eligible for more than one treatment across use cases."
    )
    treated_customers: int = Field(description="Customers awarded a winning treatment.")
    dropped_actions_count: int = Field(description="Total candidate actions dropped in arbitration.")
    channel_capped_count: int = Field(
        default=0, description="Candidate actions dropped due to channel capacity caps."
    )
    winning_by_use_case: dict[str, int] = Field(
        default_factory=dict, description="Winning treatment counts by use case."
    )
    dropped_by_use_case: dict[str, int] = Field(
        default_factory=dict, description="Dropped treatment counts by use case."
    )
    created_at: AwareDatetime = Field(description="UTC timestamp of arbitration completion.")


def load_arbitration_config(config_root: Path | None = None) -> ArbitrationConfig:
    """Load and validate `configs/decide/arbitration.yaml` or return defaults if missing."""
    path = config_root / ARBITRATION_CONFIG_FILE if config_root is not None else _DEFAULT_CONFIG_PATH

    if not path.is_file():
        return ArbitrationConfig()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ArbitrationConfig.model_validate(raw)
    except Exception as exc:
        _LOGGER.warning("Could not load arbitration config from %s: %s", path, exc)
        return ArbitrationConfig()


def _join_keys_vectorised(frame: pd.DataFrame, key_cols: tuple[str, ...]) -> pd.Series:
    joined = key_text(frame[key_cols[0]]).astype("object")
    for name in key_cols[1:]:
        joined = joined + KEY_SEPARATOR + key_text(frame[name]).astype("object")
    return joined


def arbitrate_treat_lists(
    treat_lists: Sequence[pd.DataFrame],
    config: ArbitrationConfig | None = None,
    key_cols: tuple[str, ...] = ("customer_id",),
) -> tuple[pd.DataFrame, ArbitrationSummary]:
    """Arbitrate across treat lists to assign at most one action per customer.

    Pure function executing in O(N) time with vectorized operations.
    """
    if not treat_lists:
        raise ValueError("At least one treat list must be provided.")

    cfg = config or ArbitrationConfig()
    now = utc_now()

    # Verify key columns present in all treat lists
    for idx, tl in enumerate(treat_lists):
        missing = [c for c in key_cols if c not in tl.columns]
        if missing:
            raise KeyError(f"Treat list {idx} missing key column(s): {', '.join(repr(c) for c in missing)}")

    # Fast path for single use case without channel caps
    if len(treat_lists) == 1 and not cfg.channel_caps:
        tl = treat_lists[0].copy()
        n = len(tl)
        uc = str(tl["use_case"].iloc[0]) if n > 0 else "unknown"
        priority_weight = cfg.priority_for(uc)

        # Compute priority score
        net_val = pd.to_numeric(tl.get("net_value", pd.Series(np.nan, index=tl.index)), errors="coerce")
        gross_val = pd.to_numeric(
            tl.get(EXPECTED_GROSS_VALUE_COLUMN, pd.Series(np.nan, index=tl.index)), errors="coerce"
        )
        val = net_val.where(net_val.notna(), gross_val).fillna(1.0)
        p_scores = priority_weight * val

        is_treated = tl["treat"].fillna(False).astype(bool).to_numpy()

        tl[WINNING_USE_CASE_COLUMN] = pd.Series(uc, index=tl.index, dtype="object").where(
            is_treated, other=None
        )
        tl[LOSING_ACTIONS_COLUMN] = pd.Series([None] * n, index=tl.index, dtype="object")
        tl[PRIORITY_SCORE_COLUMN] = pd.Series(p_scores, index=tl.index, dtype="float64").where(
            is_treated, other=np.nan
        )

        treated_count = int(is_treated.sum())
        summary = ArbitrationSummary(
            total_customers=n,
            customers_with_actions=treated_count,
            customers_with_conflicts=0,
            treated_customers=treated_count,
            dropped_actions_count=0,
            channel_capped_count=0,
            winning_by_use_case={uc: treated_count} if treated_count > 0 else {},
            dropped_by_use_case={},
            created_at=now,
        )
        return tl, summary

    # Multi use case arbitration
    prepared: list[pd.DataFrame] = []
    holdout_key_set: set[str] = set()

    for idx, tl in enumerate(treat_lists):
        df = tl.copy()
        df["_source_idx"] = idx
        df["_row_key"] = _join_keys_vectorised(df, key_cols)

        # Record holdouts across all inputs
        if "holdout" in df.columns:
            h_mask = df["holdout"].fillna(False).astype(bool).to_numpy()
            if h_mask.any():
                holdout_key_set.update(df.loc[h_mask, "_row_key"].astype(str))

        # Check candidate treatment eligibility
        is_treat = df["treat"].fillna(False).astype(bool).to_numpy()
        df["_is_candidate"] = is_treat

        # Calculate priority score
        uc_series = df["use_case"].astype(str)
        p_weights = uc_series.map(lambda u: cfg.priority_for(u)).astype(float).to_numpy()

        net_val = pd.to_numeric(df.get("net_value", pd.Series(np.nan, index=df.index)), errors="coerce")
        gross_val = pd.to_numeric(
            df.get(EXPECTED_GROSS_VALUE_COLUMN, pd.Series(np.nan, index=df.index)), errors="coerce"
        )
        val_s = net_val.where(net_val.notna(), gross_val).fillna(1.0)
        val_arr = val_s.to_numpy(dtype=float)

        df["_priority_score"] = pd.Series(p_weights * val_arr, index=df.index, dtype="float64")
        df["_val"] = pd.Series(val_arr, index=df.index, dtype="float64")
        prepared.append(df)

    combined = pd.concat(prepared, ignore_index=True)

    # Enforce holdout protection: customers in holdout in ANY use case are disqualified from treatment
    if holdout_key_set:
        is_holdout_cust = combined["_row_key"].isin(holdout_key_set)
        combined.loc[is_holdout_cust, "_is_candidate"] = False

    # Extract all candidate actions
    candidates = combined[combined["_is_candidate"]].copy()

    total_unique_customers = int(combined["_row_key"].nunique())
    customers_with_actions = int(candidates["_row_key"].nunique()) if not candidates.empty else 0

    # Count conflicts (customers with >1 candidate action)
    if not candidates.empty:
        cand_counts = candidates.groupby("_row_key").size()
        customers_with_conflicts = int((cand_counts > 1).sum())
    else:
        customers_with_conflicts = 0

    # Sort candidates by:
    # 1. _priority_score descending
    # 2. _val descending
    # 3. _source_idx ascending (deterministic tie-breaker)
    if not candidates.empty:
        candidates.sort_values(
            by=["_row_key", "_priority_score", "_val", "_source_idx"],
            ascending=[True, False, False, True],
            inplace=True,
        )

        # Rank candidates per customer
        candidates["_rank"] = candidates.groupby("_row_key").cumcount() + 1
        cap = cfg.contact_cap_per_customer

        initial_winners = candidates[candidates["_rank"] <= cap].copy()
        initial_losers = candidates[candidates["_rank"] > cap].copy()
    else:
        initial_winners = pd.DataFrame(columns=combined.columns)
        initial_losers = pd.DataFrame(columns=combined.columns)

    # Enforce channel capacity caps on initial winners
    channel_capped_losers: list[pd.DataFrame] = []
    final_winners: pd.DataFrame
    channel_capped_count = 0

    if not initial_winners.empty and cfg.channel_caps:
        # Sort initial winners globally by priority score descending
        initial_winners.sort_values(by=["_priority_score", "_val"], ascending=[False, False], inplace=True)
        channel_col = initial_winners.get("channel", pd.Series(None, index=initial_winners.index)).astype(
            "object"
        )

        keep_mask = np.ones(len(initial_winners), dtype=bool)
        for ch, ch_cap in cfg.channel_caps.items():
            is_ch = channel_col == ch
            if is_ch.any():
                ch_cum = is_ch.astype(int).cumsum()
                exceeded = is_ch & (ch_cum > ch_cap)
                keep_mask &= ~exceeded

        final_winners = initial_winners[keep_mask].copy()
        dropped_by_channel = initial_winners[~keep_mask].copy()
        if not dropped_by_channel.empty:
            channel_capped_count = len(dropped_by_channel)
            channel_capped_losers.append(dropped_by_channel)
    else:
        final_winners = initial_winners

    # Combine all losing actions
    all_losers_list = [initial_losers, *channel_capped_losers]
    all_losers = (
        pd.concat(all_losers_list, ignore_index=True)
        if any(not df.empty for df in all_losers_list)
        else pd.DataFrame()
    )

    # Compute dropped actions breakdown by use case
    dropped_by_uc: dict[str, int] = {}
    if not all_losers.empty:
        counts = all_losers["use_case"].value_counts()
        dropped_by_uc = {str(k): int(v) for k, v in counts.items()}

    # Compute winning actions breakdown by use case
    winning_by_uc: dict[str, int] = {}
    if not final_winners.empty:
        counts = final_winners["use_case"].value_counts()
        winning_by_uc = {str(k): int(v) for k, v in counts.items()}

    # Map losing actions string per customer
    losing_actions_map: dict[str, str] = {}
    if not all_losers.empty:
        offers = all_losers["offer"].fillna("")
        all_losers["_action_desc"] = np.where(
            offers != "",
            all_losers["use_case"].astype(str) + ":" + offers.astype(str),
            all_losers["use_case"].astype(str),
        )
        losing_grouped = all_losers.groupby("_row_key")["_action_desc"].agg(lambda items: ", ".join(items))
        losing_actions_map = {str(k): str(v) for k, v in losing_grouped.to_dict().items()}

    # Build final result per customer key
    # 1. Winning rows keep their full data with treat = True
    if not final_winners.empty:
        final_winners["treat"] = True
        final_winners[WINNING_USE_CASE_COLUMN] = final_winners["use_case"].astype("object")
        final_winners[PRIORITY_SCORE_COLUMN] = final_winners["_priority_score"].astype("float64")
        final_winners[LOSING_ACTIONS_COLUMN] = (
            final_winners["_row_key"].map(losing_actions_map).astype("object")
        )

    # 2. Non-winning customers (no candidate won)
    winning_keys_set = set(final_winners["_row_key"]) if not final_winners.empty else set()
    all_unique_keys = combined["_row_key"].unique()
    non_winning_keys = [k for k in all_unique_keys if k not in winning_keys_set]

    non_winning_rows: list[pd.DataFrame] = []
    if non_winning_keys:
        # Take representative row for each non-winning customer (e.g. from lowest _source_idx)
        nw_df = combined[combined["_row_key"].isin(non_winning_keys)].copy()
        nw_df.sort_values(by=["_source_idx"], ascending=True, inplace=True)
        nw_rep = nw_df.drop_duplicates(subset=["_row_key"]).copy()

        nw_rep["treat"] = False
        nw_rep["offer"] = None
        nw_rep["channel"] = None
        nw_rep[WINNING_USE_CASE_COLUMN] = None
        nw_rep[PRIORITY_SCORE_COLUMN] = np.nan
        nw_rep[LOSING_ACTIONS_COLUMN] = nw_rep["_row_key"].map(losing_actions_map).astype("object")

        # Mark holdout if customer was in holdout set
        if holdout_key_set:
            is_h = nw_rep["_row_key"].isin(holdout_key_set)
            nw_rep.loc[is_h, "holdout"] = True

        non_winning_rows.append(nw_rep)

    # Combine winners and non-winners
    parts_to_combine = []
    if not final_winners.empty:
        parts_to_combine.append(final_winners)
    if non_winning_rows:
        parts_to_combine.extend(non_winning_rows)

    result_combined = pd.concat(parts_to_combine, ignore_index=True)

    # Sort result deterministically by key columns
    result_combined.sort_values(by=list(key_cols), inplace=True)
    result_combined.reset_index(drop=True, inplace=True)

    # Build clean output DataFrame with correct columns
    group_col = _SEGMENT_COLUMN if _SEGMENT_COLUMN in result_combined.columns else BAND_COLUMN
    output_cols = [
        *key_cols,
        "use_case",
        "model_version",
        group_col,
        "treat",
        "holdout",
        "explore",
        "suppression_reason",
        "offer",
        "channel",
        "net_value",
        EXPECTED_GROSS_VALUE_COLUMN,
        *REASON_COLUMNS,
        WINNING_USE_CASE_COLUMN,
        LOSING_ACTIONS_COLUMN,
        PRIORITY_SCORE_COLUMN,
    ]

    # Ensure all expected columns exist
    for col in output_cols:
        if col not in result_combined.columns:
            result_combined[col] = None

    result_df = result_combined[output_cols].copy()

    # Build summary
    treated_customers = int(result_df["treat"].sum())
    dropped_actions_count = len(all_losers)

    summary = ArbitrationSummary(
        total_customers=total_unique_customers,
        customers_with_actions=customers_with_actions,
        customers_with_conflicts=customers_with_conflicts,
        treated_customers=treated_customers,
        dropped_actions_count=dropped_actions_count,
        channel_capped_count=channel_capped_count,
        winning_by_use_case=winning_by_uc,
        dropped_by_use_case=dropped_by_uc,
        created_at=now,
    )

    return result_df, summary


def arbitrated_table(df: pd.DataFrame, key_cols: tuple[str, ...]) -> pa.Table:
    """Build PyArrow Table from arbitrated treat list DataFrame."""
    group_col = _SEGMENT_COLUMN if _SEGMENT_COLUMN in df.columns else BAND_COLUMN

    fields = [pa.field(name, pa.string()) for name in key_cols]
    fields += [
        pa.field("use_case", pa.string()),
        pa.field("model_version", pa.string()),
        pa.field(group_col, pa.string()),
        pa.field("treat", pa.bool_()),
        pa.field("holdout", pa.bool_()),
        pa.field("explore", pa.bool_()),
        pa.field("suppression_reason", pa.string()),
        pa.field("offer", pa.string()),
        pa.field("channel", pa.string()),
        pa.field("net_value", pa.float64()),
        pa.field(EXPECTED_GROSS_VALUE_COLUMN, pa.float64()),
        *[pa.field(name, pa.string()) for name in REASON_COLUMNS],
        pa.field(WINNING_USE_CASE_COLUMN, pa.string()),
        pa.field(LOSING_ACTIONS_COLUMN, pa.string()),
        pa.field(PRIORITY_SCORE_COLUMN, pa.float64()),
    ]
    schema = pa.schema(fields)

    arrays: list[pa.Array] = []
    for f in schema:
        name = f.name
        col = df[name] if name in df.columns else pd.Series([None] * len(df), dtype="object")
        if f.type.equals(pa.bool_()):
            arrays.append(pa.array(col.astype("boolean"), type=pa.bool_()))
        elif f.type.equals(pa.float64()):
            arrays.append(pa.array(pd.to_numeric(col, errors="coerce"), type=pa.float64()))
        else:
            arrays.append(pa.array(col.astype("string"), type=pa.string()))

    return pa.Table.from_arrays(arrays, schema=schema)


def parquet_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return sink.getvalue()


def csv_bytes(table: pa.Table) -> bytes:
    df = table.to_pandas()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(table.schema.names)
    for row in df.itertuples(index=False):
        formatted: list[str] = []
        for val in row:
            if val is None or pd.isna(val):
                formatted.append("")
            elif isinstance(val, bool | np.bool_):
                formatted.append("1" if val else "0")
            elif isinstance(val, float | np.floating):
                formatted.append(f"{val:.{_RUPEE_DECIMALS}f}".rstrip("0").rstrip("."))
            else:
                formatted.append(str(val))
        writer.writerow(formatted)
    return buf.getvalue().encode("utf-8")
