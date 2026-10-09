"""Cross-use-case arbitration (Plan J M101, DEC-1311).

Arbitrates between the treat lists (M98) of several finished scoring runs over the same customer key, so
that a customer gets at most `contact_cap_per_customer` actions in one cycle across all the use cases.

**Who wins (DEC-1311 (a), (h)).** A candidate is a treat-list row with `treat = 1`. For one customer:

1. A customer a hold-out kept back in *any* use case is never treated by any other (DEC-1311 (c)). So is a
   customer in the control group of *any* selected run (`control_group` in its treat list, Phase 1's per-run
   control, DEC-1311 (al)): the row names the use case that held them back (`control_use_cases`), and
   `holdout` and `holdout_use_cases` keep meaning M92's hold-out only.
2. A row M92 treated at random (`explore = 1`, `treat = 1`) keeps its action: it wins before any
   comparison, like hold-out protection, so the random sample stays random (DEC-1311 (i)). Two
   explore-treated rows of one customer are both kept only if the contact cap allows; otherwise the first in
   the request's use case order is kept and the other is recorded as dropped.
3. The rest are ranked by `priority x value` **only when every candidate of that customer carries the same
   kind of value**: all an incremental `net_value` (uplift runs), or all an `expected_gross_value`
   (propensity runs, not incremental). Rupees of the two kinds are never compared, and a candidate with no
   value is never given one. When the kinds differ, or one has no value, the customer is ranked by priority
   alone (DEC-1311 (h)).
4. A tie is broken by the order of the treat lists in the request (the use case order), which is stable.

Why a customer's choice was made is written on every row (`arbitration_reason`) and counted in the
summary (`customers_decided_by_*`).

**Caps (DEC-1311 (b)).** `contact_cap_per_customer` limits the actions per customer; `channel_caps` limit
the actions per channel over the whole cycle. Capacity goes to explore rows first, then to rows with an
incremental value, then to rows with an expected gross value, then to rows without a value; inside each group
by `priority x value` (priority alone for rows without a value), then by request order. The explore rows are
the exception: they keep their places in a fixed pseudo-random order that ignores their value (`_draw`), so a
binding cap leaves a random sample of them.

**Measuring an arbitrated cycle (DEC-1311 (n)).** `comparable_keys` says, for each use case, which customers
its campaign compares: the arbitration run again as if no hold-out and no control group held anyone back, a
held-back customer (hold-out member or control group) the use case's policy intended to contact competing
like a treated one. Both arms of the campaign are cut with that
one set, so they stay comparable.

**No config, no surprises.** With no `configs/decide/arbitration.yaml` every use case has priority 1, the
cap is one action per customer, and there are no channel caps. The repository ships only
`configs/decide/arbitration.example.yaml`, which is never read.

Everything is whole-array work (sorts, bincounts, Arrow string kernels): no Python loop over the rows, so
the time is linear in the rows up to the sorts.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq
import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from engine.contracts import Artefact
from engine.decide.treat_list import (
    EXPECTED_GROSS_VALUE_COLUMN,
    REASON_COLUMNS,
    SEGMENT_COLUMN,
    table_csv_bytes,
)
from engine.keys import KEY_SEPARATOR, key_text
from engine.stages.actions import BAND_COLUMN, CONTROL_GROUP_COLUMN
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "ARBITRATED_TREAT_LIST_CSV",
    "ARBITRATED_TREAT_LIST_PARQUET",
    "ARBITRATION_CONFIG_FILE",
    "ARBITRATION_CONFIG_INVALID",
    "ARBITRATION_REASON_COLUMN",
    "ARBITRATION_SUMMARY_FILENAME",
    "CONTROL_USE_CASES_COLUMN",
    "HOLDOUT_USE_CASES_COLUMN",
    "LOSING_ACTIONS_COLUMN",
    "PRIORITY_SCORE_COLUMN",
    "PRIORITY_WEIGHT_COLUMN",
    "WINNING_USE_CASE_COLUMN",
    "ArbitrationConfig",
    "ArbitrationError",
    "ArbitrationSummary",
    "UseCasePriorityConfig",
    "arbitrate_treat_lists",
    "arbitrated_table",
    "comparable_keys",
    "csv_bytes",
    "load_arbitration_config",
    "parquet_bytes",
    "winning_keys",
]

_LOGGER = get_logger(__name__)

pc: Any = _pc
"""`pyarrow.compute`, untyped: its stubs miss most kernels."""

ARBITRATED_TREAT_LIST_CSV: Final[str] = "arbitrated_treat_list.csv"
ARBITRATED_TREAT_LIST_PARQUET: Final[str] = "arbitrated_treat_list.parquet"
ARBITRATION_SUMMARY_FILENAME: Final[str] = "arbitration_summary.json"
ARBITRATION_CONFIG_FILE: Final[str] = "decide/arbitration.yaml"
ARBITRATION_CONFIG_INVALID: Final[str] = "ARBITRATION_CONFIG_INVALID"
"""The client's `decide/arbitration.yaml` cannot be read; the request is refused (422), never run on defaults."""

WINNING_USE_CASE_COLUMN: Final[str] = "winning_use_case"
LOSING_ACTIONS_COLUMN: Final[str] = "losing_actions"
PRIORITY_SCORE_COLUMN: Final[str] = "priority_score"
PRIORITY_WEIGHT_COLUMN: Final[str] = "priority_weight"
ARBITRATION_REASON_COLUMN: Final[str] = "arbitration_reason"
HOLDOUT_USE_CASES_COLUMN: Final[str] = "holdout_use_cases"
CONTROL_USE_CASES_COLUMN: Final[str] = "control_use_cases"

_DEFAULT_CONFIG_PATH = Path("configs/decide/arbitration.yaml")

# Why a row is (or is not) the customer's action. The values written to `arbitration_reason`.
REASON_ONLY_ACTION: Final[str] = "only_action"
REASON_WITHIN_CAP: Final[str] = "within_contact_cap"
REASON_NET_VALUE: Final[str] = "net_value"
REASON_GROSS_VALUE: Final[str] = "expected_gross_value"
REASON_PRIORITY: Final[str] = "priority"
REASON_REQUEST_ORDER: Final[str] = "request_order"
REASON_EXPLORE: Final[str] = "explore_treated"
REASON_EXPLORE_ORDER: Final[str] = "explore_request_order"
REASON_HELD_OUT: Final[str] = "held_out"
REASON_CHANNEL_CAP: Final[str] = "channel_cap"
REASON_NOT_SELECTED: Final[str] = "not_selected"

_KIND_NET: Final[int] = 0
_KIND_GROSS: Final[int] = 1
_KIND_NONE: Final[int] = 2

_DECIDED_NONE: Final[int] = 0
_DECIDED_VALUE: Final[int] = 1
_DECIDED_PRIORITY: Final[int] = 2
_DECIDED_ORDER: Final[int] = 3
_DECIDED_EXPLORE: Final[int] = 4
_DECIDED_EXPLORE_ORDER: Final[int] = 5

_BOOL_COLUMNS: Final[tuple[str, ...]] = ("treat", "holdout", "explore", CONTROL_GROUP_COLUMN)
_STRING_COLUMNS: Final[tuple[str, ...]] = (
    "use_case",
    "model_version",
    SEGMENT_COLUMN,
    BAND_COLUMN,
    "suppression_reason",
    "offer",
    "channel",
    "contactable_channels",
    "runner_up_offer",
    "offer_reason",
    *REASON_COLUMNS,
)
_FLOAT_COLUMNS: Final[tuple[str, ...]] = ("net_value", EXPECTED_GROSS_VALUE_COLUMN, "runner_up_net_value")
_OFFER_DETAIL_COLUMNS: Final[tuple[str, ...]] = ("runner_up_offer", "runner_up_net_value", "offer_reason")
"""M100's columns that describe the offer chosen: they go with the offer when the action is not taken."""
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("use_case", "treat")


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
    """Configuration for cross-use-case arbitration (`configs/decide/arbitration.yaml`, absent by default)."""

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

    @field_validator("channel_caps")
    @classmethod
    def _caps_not_negative(cls, value: dict[str, int]) -> dict[str, int]:
        bad = sorted(name for name, cap in value.items() if cap < 0)
        if bad:
            raise ValueError(f"A channel cap cannot be negative: {', '.join(bad)}.")
        return value

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
    treated_customers: int = Field(description="Customers awarded at least one winning treatment.")
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
    holdout_blocked_actions: int = Field(
        default=0,
        description="Actions a use case wanted that were not allowed because a hold-out kept the customer back.",
    )
    control_blocked_actions: int = Field(
        default=0,
        description=(
            "Actions a use case wanted that were not allowed because the customer is in another use case's "
            "control group for the run (its own per-run control, not M92's hold-out), and no hold-out "
            "kept them back either."
        ),
    )
    explore_kept_count: int = Field(
        default=0, description="Actions M92 treated at random that kept their action."
    )
    explore_dropped_count: int = Field(
        default=0,
        description="Explore-treated actions dropped because the customer's contact cap or a channel cap was reached.",
    )
    customers_decided_by_value: int = Field(
        default=0,
        description=(
            "Customers who end with an action chosen by priority times value among several candidates, "
            "every candidate carrying the same kind of value."
        ),
    )
    customers_decided_by_priority: int = Field(
        default=0,
        description=(
            "Customers who end with an action chosen by priority alone, because a candidate had no value "
            "or the values were of different kinds."
        ),
    )
    customers_decided_by_request_order: int = Field(
        default=0,
        description=(
            "Customers who end with an action whose candidates tied, decided by the order of the use "
            "cases requested."
        ),
    )
    customers_decided_by_explore: int = Field(
        default=0,
        description="Customers who end with an action kept because it was treated at random (explore).",
    )
    contested_customers_channel_capped: int = Field(
        default=0,
        description=(
            "Customers more than one use case wanted whose chosen action a channel cap then removed, so "
            "they end with no action; they are in none of the `customers_decided_by_*` counts."
        ),
    )
    run_ids: list[str] = Field(
        default_factory=list,
        description="The scoring runs arbitrated, in the order of the request (the use case order).",
    )
    created_at: AwareDatetime = Field(description="UTC timestamp of arbitration completion.")


def load_arbitration_config(config_root: Path | None = None) -> ArbitrationConfig:
    """Load and validate `decide/arbitration.yaml` under the config root, or return the defaults.

    The defaults (no file) are priority 1 for every use case, one action per customer and no channel caps.
    A file that is there but cannot be read (not YAML, an unknown key, a cap that is not allowed) raises
    `ArbitrationError(ARBITRATION_CONFIG_INVALID)`: running on the defaults would drop the caps and
    priorities a client wrote without telling them.
    """
    path = config_root / ARBITRATION_CONFIG_FILE if config_root is not None else _DEFAULT_CONFIG_PATH

    if not path.is_file():
        return ArbitrationConfig()

    refusal = "The arbitration settings file could not be read; fix it or remove it to use the defaults."
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        _LOGGER.warning("Could not read arbitration config %s: %s", path, exc)
        raise ArbitrationError(ARBITRATION_CONFIG_INVALID, f"{refusal} It is not a valid YAML file.") from exc
    if not isinstance(raw, dict):
        raise ArbitrationError(
            ARBITRATION_CONFIG_INVALID, f"{refusal} It must list settings by name, such as channel_caps."
        )
    try:
        return ArbitrationConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'the file'}: "
            f"{str(error['msg']).removeprefix('Value error, ')}"
            for error in exc.errors()
        )
        _LOGGER.warning("Invalid arbitration config %s: %s", path, problems)
        raise ArbitrationError(ARBITRATION_CONFIG_INVALID, f"{refusal} {problems}") from exc


def _join_keys_vectorised(frame: pd.DataFrame, key_cols: tuple[str, ...]) -> pd.Series[Any]:
    joined = key_text(frame[key_cols[0]]).astype("object")
    for name in key_cols[1:]:
        joined = joined + KEY_SEPARATOR + key_text(frame[name]).astype("object")
    return joined


def _stack(frames: Sequence[pd.DataFrame], name: str, kind: str) -> pd.Series[Any] | None:
    """One column of all the lists stacked, in one dtype, or None when no list has it.

    A list without the column contributes nulls, so the stacked column never mixes dtypes (and never
    triggers pandas' all-null concat deprecation).
    """
    if not any(name in f.columns for f in frames):
        return None
    parts: list[pd.Series[Any]] = []
    for f in frames:
        n = len(f.index)
        if name not in f.columns:
            if kind == "bool":
                parts.append(pd.Series(pd.array([None] * n, dtype="boolean")))
            elif kind == "float":
                parts.append(pd.Series(np.full(n, np.nan), dtype="float64"))
            else:
                parts.append(pd.Series(np.full(n, None, dtype=object), dtype="object"))
            continue
        col = f[name].reset_index(drop=True)
        if kind == "bool":
            parts.append(col.astype("boolean"))
        elif kind == "float":
            parts.append(pd.to_numeric(col, errors="coerce").astype("float64"))
        else:
            parts.append(col.astype("object"))
    return pd.concat(parts, ignore_index=True)


def _restore_dtype(series: pd.Series[Any], dtypes: set[str]) -> pd.Series[Any]:
    """Give a column back the dtype every input list had it in, when it can hold the values."""
    if len(dtypes) != 1:
        return series
    wanted = next(iter(dtypes))
    if wanted == "object":
        return series.astype("object").where(series.notna(), None)
    try:
        return series.astype(wanted)  # type: ignore[no-any-return,call-overload]
    except (TypeError, ValueError):
        return series


def _join_by_group(group: np.ndarray, labels: pa.Array, sep: str = ", ") -> tuple[np.ndarray, np.ndarray]:
    """Join `labels` of equal `group` values (the groups consecutive, `group` sorted) with `sep`.

    Returns (the distinct groups, one joined text per group), built by Arrow's list join: no Python loop.
    """
    if group.size == 0:
        return group, np.empty(0, dtype=object)
    starts = np.flatnonzero(np.r_[True, group[1:] != group[:-1]])
    offsets = pa.array(np.r_[starts, group.size].astype(np.int32))
    lists = pa.ListArray.from_arrays(offsets, labels)
    joined = pc.binary_join(lists, pa.scalar(sep, pa.large_string()))
    return group[starts], np.asarray(joined.to_numpy(zero_copy_only=False), dtype=object)


def _action_labels(use_case: np.ndarray, offer: np.ndarray, tag: np.ndarray) -> pa.Array:
    """`use_case:offer` (just `use_case` for a row with no offer), then `tag` (empty for none)."""
    uc = pa.array(use_case, pa.large_string())
    off = pc.fill_null(pa.array(offer, pa.large_string()), "")
    has_offer = pc.not_equal(off, "")
    colon = pa.scalar(":", pa.large_string())
    empty = pa.scalar("", pa.large_string())
    label = pc.if_else(has_offer, pc.binary_join_element_wise(uc, off, colon), uc)
    return pc.binary_join_element_wise(label, pa.array(tag, pa.large_string()), empty)


def _empty_object(n: int) -> np.ndarray:
    return np.full(n, None, dtype=object)


@dataclass(frozen=True)
class _Stack:
    """All the treat lists stacked into one frame, with what the ranking needs worked out once."""

    n: int
    src: np.ndarray
    cid: np.ndarray
    keys: np.ndarray
    uniques: np.ndarray
    n_cust: int
    combined: pd.DataFrame
    source_dtypes: dict[str, set[str]]
    treat_flag: np.ndarray
    holdout_own: np.ndarray
    control_own: np.ndarray
    explore_own: np.ndarray
    uc_codes: np.ndarray
    uc_names: pd.Index[Any]
    weight: np.ndarray
    kind: np.ndarray
    value: np.ndarray


def _stack_lists(
    treat_lists: Sequence[pd.DataFrame], cfg: ArbitrationConfig, key_cols: tuple[str, ...]
) -> _Stack:
    """Stack the lists in one frame, one dtype per column, and work out each row's weight and kind of value."""
    if not treat_lists:
        raise ValueError("At least one treat list must be provided.")
    frames = [tl.reset_index(drop=True) for tl in treat_lists]
    for idx, tl in enumerate(frames):
        missing = [c for c in (*key_cols, *_REQUIRED_COLUMNS) if c not in tl.columns]
        if missing:
            raise KeyError(f"Treat list {idx} missing column(s): {', '.join(repr(c) for c in missing)}")

    sizes = [len(f.index) for f in frames]
    n = int(sum(sizes))
    src = np.repeat(np.arange(len(frames), dtype=np.int64), sizes)
    keys = pd.concat([_join_keys_vectorised(f, key_cols) for f in frames], ignore_index=True)
    cid, uniques = pd.factorize(keys)  # customer ids in order of first appearance
    stacked: dict[str, pd.Series[Any]] = {}
    for name in key_cols:
        stacked[name] = pd.concat([f[name].astype("object") for f in frames], ignore_index=True)
    for names, column_kind in ((_BOOL_COLUMNS, "bool"), (_STRING_COLUMNS, "str"), (_FLOAT_COLUMNS, "float")):
        for name in names:
            column = _stack(frames, name, column_kind)
            if column is not None:
                stacked[name] = column
    combined = pd.DataFrame(stacked)
    source_dtypes: dict[str, set[str]] = {}
    for f in frames:
        for name in f.columns:
            source_dtypes.setdefault(str(name), set()).add(str(f[name].dtype))

    treat_flag = combined["treat"].fillna(False).to_numpy(dtype=bool)
    holdout_own = (
        combined["holdout"].fillna(False).to_numpy(dtype=bool) if "holdout" in combined else np.zeros(n, bool)
    )
    # The run's own control group (Phase 1's actions stage), when M92's hold-out flag does not already say it:
    # a list written before the column existed has none, and a null is not a control (DEC-1311 (al)).
    control_own = (
        combined[CONTROL_GROUP_COLUMN].fillna(False).to_numpy(dtype=bool) & ~holdout_own
        if CONTROL_GROUP_COLUMN in combined
        else np.zeros(n, bool)
    )
    explore_own = (
        combined["explore"].fillna(False).to_numpy(dtype=bool) if "explore" in combined else np.zeros(n, bool)
    )

    # Priority weights, one config lookup per distinct use case.
    uc_codes, uc_names = pd.factorize(combined["use_case"].astype(str))
    uc_weight = np.array([cfg.priority_for(str(u)) for u in uc_names], dtype=float)
    weight = uc_weight[uc_codes] if n else np.empty(0)

    # The kind of value each row carries: incremental, gross, or none.
    net = combined["net_value"].to_numpy(dtype=float) if "net_value" in combined else np.full(n, np.nan)
    gross = (
        combined[EXPECTED_GROSS_VALUE_COLUMN].to_numpy(dtype=float)
        if EXPECTED_GROSS_VALUE_COLUMN in combined
        else np.full(n, np.nan)
    )
    net_ok = np.isfinite(net)
    gross_ok = np.isfinite(gross) & ~net_ok
    kind = np.where(net_ok, _KIND_NET, np.where(gross_ok, _KIND_GROSS, _KIND_NONE))
    value = np.where(net_ok, net, np.where(gross_ok, gross, 0.0))
    return _Stack(
        n=n,
        src=src,
        cid=cid,
        keys=keys.to_numpy(dtype=object),
        uniques=np.asarray(uniques, dtype=object),
        n_cust=len(uniques),
        combined=combined,
        source_dtypes=source_dtypes,
        treat_flag=treat_flag,
        holdout_own=holdout_own,
        control_own=control_own,
        explore_own=explore_own,
        uc_codes=uc_codes,
        uc_names=uc_names,
        weight=weight,
        kind=kind,
        value=value,
    )


@dataclass(frozen=True)
class _Ranking:
    """The candidates of every customer ranked: the order, who is kept under the contact cap, and how."""

    ci: np.ndarray
    c_cid: np.ndarray
    c_kind: np.ndarray
    c_weight: np.ndarray
    c_value: np.ndarray
    c_src: np.ndarray
    cand_count: np.ndarray
    by_value_cust: np.ndarray
    cust_kind: np.ndarray
    order: np.ndarray
    s_pos: np.ndarray
    s_cid: np.ndarray
    s_explore: np.ndarray
    s_score: np.ndarray
    s_value_row: np.ndarray
    s_weight: np.ndarray
    m: int
    group_start: np.ndarray
    group_id: np.ndarray
    rank: np.ndarray
    group_size: np.ndarray
    keep: np.ndarray


def _rank(stack: _Stack, is_cand: np.ndarray, cap: int) -> _Ranking:
    """Rank the candidates (`is_cand`, per stacked row) of each customer and keep the first `cap`.

    Explore rows first, then by priority x value when every candidate of the customer carries the same
    kind of value (priority alone otherwise), then by the order of the lists.
    """
    n_cust = stack.n_cust
    ci = np.flatnonzero(is_cand)
    c_cid = stack.cid[ci]
    c_kind = stack.kind[ci]
    c_weight = stack.weight[ci]
    c_value = stack.value[ci]
    c_explore = stack.explore_own[ci]
    c_src = stack.src[ci]
    cand_count = np.bincount(c_cid, minlength=n_cust)
    kind_counts = np.bincount(c_cid * 3 + c_kind, minlength=n_cust * 3).reshape(n_cust, 3)
    uniform_kind = (kind_counts > 0).sum(axis=1) == 1
    cust_kind = kind_counts.argmax(axis=1)
    by_value_cust = uniform_kind & (cust_kind != _KIND_NONE)
    row_by_value = by_value_cust[c_cid]
    score = np.where(row_by_value, c_weight * c_value, c_weight)
    rank_key = np.where(c_explore, 0.0, -score)
    order = np.lexsort((ci, c_src, rank_key, ~c_explore, c_cid))
    s_cid = c_cid[order]
    m = len(order)
    if m:
        first_of_group = np.r_[True, s_cid[1:] != s_cid[:-1]]
        group_start = np.flatnonzero(first_of_group)
        group_id = np.cumsum(first_of_group) - 1
        rank = np.arange(m) - group_start[group_id]
        group_size = cand_count[s_cid[group_start]]
    else:
        group_start = np.empty(0, dtype=np.int64)
        group_id = np.empty(0, dtype=np.int64)
        rank = np.empty(0, dtype=np.int64)
        group_size = np.empty(0, dtype=np.int64)
    return _Ranking(
        ci=ci,
        c_cid=c_cid,
        c_kind=c_kind,
        c_weight=c_weight,
        c_value=c_value,
        c_src=c_src,
        cand_count=cand_count,
        by_value_cust=by_value_cust,
        cust_kind=cust_kind,
        order=order,
        s_pos=ci[order],
        s_cid=s_cid,
        s_explore=c_explore[order],
        s_score=score[order],
        s_value_row=row_by_value[order],
        s_weight=c_weight[order],
        m=m,
        group_start=group_start,
        group_id=group_id,
        rank=rank,
        group_size=group_size,
        keep=rank < cap,
    )


def _draw(use_case: np.ndarray, key: np.ndarray) -> np.ndarray:
    """A fixed pseudo-random number in [0, 1) per (use case, customer): the same on every machine and run.

    It orders rows for which nothing else may matter, such as who keeps a channel's last places among
    the rows M92 treated at random: their value must not decide that.
    """
    joined = pc.binary_join_element_wise(
        pa.array(use_case, pa.large_string()),
        pa.array(key, pa.large_string()),
        pa.scalar("\x1f", pa.large_string()),
    )
    hashed = pd.util.hash_array(np.asarray(joined.to_numpy(zero_copy_only=False), dtype=object))
    return (hashed >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def arbitrate_treat_lists(
    treat_lists: Sequence[pd.DataFrame],
    config: ArbitrationConfig | None = None,
    key_cols: tuple[str, ...] = ("customer_id",),
) -> tuple[pd.DataFrame, ArbitrationSummary]:
    """Arbitrate across treat lists to assign at most `contact_cap_per_customer` actions per customer.

    The order of `treat_lists` is the request's use case order: it breaks ties. Pure function. Rows come
    back in the order each customer is first met (the first list's order for one list, so one list gives
    back its own rows); a customer with a winning action has one row per winning action, any other has one
    row, with `treat = False`.
    """
    cfg = config or ArbitrationConfig()
    now = utc_now()
    stack = _stack_lists(treat_lists, cfg, key_cols)
    n = stack.n
    n_cust = stack.n_cust
    cid = stack.cid
    src = stack.src
    combined = stack.combined
    treat_flag = stack.treat_flag
    holdout_own = stack.holdout_own
    control_own = stack.control_own
    uc_codes = stack.uc_codes
    uc_names = stack.uc_names

    # ---- hold-out protection: M92's hold-out, and the control group of any run (DEC-1311 (c), (al)) ------
    held_by_holdout = np.zeros(n_cust, dtype=bool)
    held_by_holdout[cid[holdout_own]] = True
    held_by_control = np.zeros(n_cust, dtype=bool)
    held_by_control[cid[control_own]] = True
    held_cust = held_by_holdout | held_by_control
    is_cand = treat_flag & ~held_cust[cid]
    holdout_blocked = int((treat_flag & held_by_holdout[cid]).sum())
    control_blocked = int((treat_flag & held_by_control[cid] & ~held_by_holdout[cid]).sum())

    # ---- rank the candidates of each customer ---------------------------------------------------------
    cap = cfg.contact_cap_per_customer
    ranking = _rank(stack, is_cand, cap)
    c_cid = ranking.c_cid
    c_kind = ranking.c_kind
    c_weight = ranking.c_weight
    c_value = ranking.c_value
    c_src = ranking.c_src
    cand_count = ranking.cand_count
    by_value_cust = ranking.by_value_cust
    cust_kind = ranking.cust_kind
    order = ranking.order
    s_pos = ranking.s_pos
    s_cid = ranking.s_cid
    s_explore = ranking.s_explore
    s_score = ranking.s_score
    s_value_row = ranking.s_value_row
    s_weight = ranking.s_weight
    m = ranking.m
    group_start = ranking.group_start
    group_id = ranking.group_id
    rank = ranking.rank
    group_size = ranking.group_size
    keep = ranking.keep

    # How each customer who lost a candidate was decided: at the boundary between the last kept and the
    # first dropped candidate.
    decided = np.full(len(group_start), _DECIDED_NONE, dtype=np.int64)
    contested = np.flatnonzero(group_size > cap)
    if contested.size:
        last_kept = group_start[contested] + cap - 1
        first_dropped = last_kept + 1
        both_explore = s_explore[last_kept] & s_explore[first_dropped]
        kept_explore = s_explore[last_kept]
        tie = ~kept_explore & (s_score[last_kept] == s_score[first_dropped])
        by_value = by_value_cust[s_cid[last_kept]]
        decided[contested] = np.where(
            both_explore,
            _DECIDED_EXPLORE_ORDER,
            np.where(
                kept_explore,
                _DECIDED_EXPLORE,
                np.where(tie, _DECIDED_ORDER, np.where(by_value, _DECIDED_VALUE, _DECIDED_PRIORITY)),
            ),
        )
    s_decided = decided[group_id] if m else decided

    # ---- channel capacity: explore rows first (in a fixed order that ignores their value, so the random
    # sample stays random), then by kind of value, then priority x value ---------------------------------
    capped = np.zeros(m, dtype=bool)
    if cfg.channel_caps and m:
        own_score = np.where(c_kind != _KIND_NONE, c_weight * c_value, c_weight)[order]
        kept_at = np.flatnonzero(keep)
        k_explore = s_explore[kept_at]
        k_kind = np.where(k_explore, 0, c_kind[order][kept_at])
        k_rank = -own_score[kept_at]
        if k_explore.any():
            explored_pos = s_pos[kept_at[k_explore]]
            use_case_text = combined["use_case"].astype(str).to_numpy(dtype=object)
            k_rank[k_explore] = _draw(use_case_text[explored_pos], stack.keys[explored_pos])
        alloc = np.lexsort((s_pos[kept_at], c_src[order][kept_at], k_rank, k_kind, ~k_explore))
        ordered = kept_at[alloc]
        channel_of = (
            combined["channel"].to_numpy(dtype=object)[s_pos[ordered]] if "channel" in combined else None
        )
        if channel_of is not None:
            for channel_name, channel_cap in cfg.channel_caps.items():
                in_channel = np.flatnonzero(channel_of == channel_name)
                capped[ordered[in_channel[channel_cap:]]] = True
    final = keep & ~capped
    lost = ~final

    # ---- reasons, per winning row ----------------------------------------------------------------------
    cust_value_reason = np.where(cust_kind == _KIND_NET, REASON_NET_VALUE, REASON_GROSS_VALUE).astype(object)
    s_reason = np.full(m, REASON_WITHIN_CAP, dtype=object)
    by_value_rows = s_decided == _DECIDED_VALUE
    s_reason[by_value_rows] = cust_value_reason[s_cid[by_value_rows]]
    s_reason[s_decided == _DECIDED_PRIORITY] = REASON_PRIORITY
    s_reason[s_decided == _DECIDED_ORDER] = REASON_REQUEST_ORDER
    s_reason[s_explore] = REASON_EXPLORE
    s_reason[s_explore & (s_decided == _DECIDED_EXPLORE_ORDER)] = REASON_EXPLORE_ORDER
    if m:
        s_reason[group_size[group_id] == 1] = REASON_ONLY_ACTION

    # ---- losing actions per customer -------------------------------------------------------------------
    offers_all = combined["offer"].to_numpy(dtype=object) if "offer" in combined else _empty_object(n)
    uc_all = combined["use_case"].astype(str).to_numpy(dtype=object)
    lost_at = np.flatnonzero(lost)
    tag = np.full(lost_at.size, "", dtype=object)
    tag[capped[lost_at]] = " (channel cap)"
    tag[(s_explore & ~capped)[lost_at]] = " (explore, over the cap)"
    tag[(s_explore & capped)[lost_at]] = " (explore, channel cap)"
    losing_text = np.full(n_cust, None, dtype=object)
    if lost_at.size:
        labels = _action_labels(uc_all[s_pos[lost_at]], offers_all[s_pos[lost_at]], tag)
        distinct, joined = _join_by_group(s_cid[lost_at], labels)
        losing_text[distinct] = joined

    # ---- winners and the rows of customers nobody treats ----------------------------------------------
    win_at = np.flatnonzero(final)
    win_pos = s_pos[win_at]
    win_cid = s_cid[win_at]
    has_winner = np.zeros(n_cust, dtype=bool)
    has_winner[win_cid] = True

    rows_nw = np.flatnonzero(~has_winner[cid])
    held_row = held_cust[cid[rows_nw]]
    # A held-back customer's row is the use case that held them back; M92's hold-out is named before a run's own
    # control group, so a customer in both keeps the hold-out row as before the control group was protected.
    held_tier = np.where(holdout_own[rows_nw], 0, np.where(control_own[rows_nw], 1, 2))
    tier = np.where(held_row, held_tier, np.where(is_cand[rows_nw], 0, 1)).astype(np.int64)
    pick = np.lexsort((rows_nw, src[rows_nw], tier, cid[rows_nw]))
    ordered_nw = rows_nw[pick]
    nw_first = (
        np.r_[True, cid[ordered_nw][1:] != cid[ordered_nw][:-1]] if ordered_nw.size else np.empty(0, bool)
    )
    rep_pos = ordered_nw[nw_first]
    rep_cid = cid[rep_pos]

    sel = np.concatenate([win_pos, rep_pos])
    sel_cid = np.concatenate([win_cid, rep_cid])
    sel_rank = np.concatenate([rank[win_at], np.zeros(rep_pos.size, dtype=np.int64)])
    final_order = np.lexsort((sel_rank, sel_cid))
    sel = sel[final_order]
    sel_cid = sel_cid[final_order]
    is_winner = np.concatenate([np.ones(win_pos.size, bool), np.zeros(rep_pos.size, bool)])[final_order]

    out = combined.iloc[sel].reset_index(drop=True)
    for name in out.columns:
        out[name] = _restore_dtype(out[name], stack.source_dtypes.get(str(name), set()))

    # Per-row columns of the winners, aligned to `sel`.
    by_pos_reason = np.full(n, None, dtype=object)
    by_pos_reason[win_pos] = s_reason[win_at]
    by_pos_score = np.full(n, np.nan)
    by_pos_score[win_pos] = np.where(s_value_row[win_at], s_score[win_at], np.nan)
    by_pos_weight = np.full(n, np.nan)
    by_pos_weight[win_pos] = s_weight[win_at]
    # A winner's position can be repeated only across customers, never within one: positions are unique.
    row_reason = by_pos_reason[sel]
    cust_has_candidate = np.zeros(n_cust, dtype=bool)
    cust_has_candidate[c_cid] = True
    # A control group is `held_out` when it kept an action from the customer; a customer in it whom no use case
    # asked to treat reads as before (`not_selected`), for the one use case's own control included (DEC-1311 (am)).
    asked_for = np.zeros(n_cust, dtype=bool)
    asked_for[cid[treat_flag]] = True
    nw_reason = np.where(
        held_by_holdout[sel_cid] | (held_by_control[sel_cid] & asked_for[sel_cid]),
        REASON_HELD_OUT,
        np.where(cust_has_candidate[sel_cid], REASON_CHANNEL_CAP, REASON_NOT_SELECTED),
    ).astype(object)
    out["treat"] = is_winner
    winning_use_case = uc_all[sel].copy()
    winning_use_case[~is_winner] = None
    out[WINNING_USE_CASE_COLUMN] = winning_use_case
    out[LOSING_ACTIONS_COLUMN] = losing_text[sel_cid]
    out[PRIORITY_SCORE_COLUMN] = by_pos_score[sel]
    out[PRIORITY_WEIGHT_COLUMN] = by_pos_weight[sel]
    out[ARBITRATION_REASON_COLUMN] = np.where(is_winner, row_reason, nw_reason)

    # An action not taken keeps no offer, channel or offer detail: they would read as a decision.
    not_taken = ~is_winner & is_cand[sel]
    for name in ("offer", "channel", *_OFFER_DETAIL_COLUMNS):
        if name not in out.columns:
            continue
        if name in _FLOAT_COLUMNS:
            out[name] = out[name].mask(not_taken)
        else:
            blanked = out[name].to_numpy(dtype=object, copy=True)
            blanked[not_taken] = None
            out[name] = blanked

    # The use cases whose hold-out kept the customer back (each row's own `holdout` is its own list's).
    holdout_text = np.full(n_cust, None, dtype=object)
    held_at = np.flatnonzero(holdout_own)
    if held_at.size:
        by_cust = held_at[np.argsort(cid[held_at], kind="stable")]
        distinct, joined = _join_by_group(cid[by_cust], pa.array(uc_all[by_cust], pa.large_string()))
        holdout_text[distinct] = joined
    out[HOLDOUT_USE_CASES_COLUMN] = holdout_text[sel_cid]
    # The use cases whose own run kept the customer back as its control group (and no hold-out did, per list).
    control_text = np.full(n_cust, None, dtype=object)
    control_at = np.flatnonzero(control_own)
    if control_at.size:
        by_cust = control_at[np.argsort(cid[control_at], kind="stable")]
        distinct, joined = _join_by_group(cid[by_cust], pa.array(uc_all[by_cust], pa.large_string()))
        control_text[distinct] = joined
    out[CONTROL_USE_CASES_COLUMN] = control_text[sel_cid]

    # ---- the summary -----------------------------------------------------------------------------------
    lost_counts = (
        np.bincount(uc_codes[s_pos[lost_at]], minlength=len(uc_names))
        if lost_at.size
        else np.zeros(len(uc_names), dtype=np.int64)
    )
    win_counts = (
        np.bincount(uc_codes[win_pos], minlength=len(uc_names))
        if win_pos.size
        else np.zeros(len(uc_names), dtype=np.int64)
    )
    # How a customer was decided counts only customers who end with an action: one whose chosen action a
    # channel cap then removed is counted apart, so the counts match the reasons on the treated rows.
    group_won = has_winner[s_cid[group_start]] if m else np.empty(0, dtype=bool)
    decided_won = np.where(group_won, decided, _DECIDED_NONE)
    summary = ArbitrationSummary(
        total_customers=n_cust,
        customers_with_actions=int((cand_count > 0).sum()),
        customers_with_conflicts=int((cand_count > 1).sum()),
        treated_customers=int(has_winner.sum()),
        dropped_actions_count=int(lost.sum()),
        channel_capped_count=int(capped.sum()),
        winning_by_use_case={str(uc_names[i]): int(c) for i, c in enumerate(win_counts) if c > 0},
        dropped_by_use_case={str(uc_names[i]): int(c) for i, c in enumerate(lost_counts) if c > 0},
        holdout_blocked_actions=holdout_blocked,
        control_blocked_actions=control_blocked,
        explore_kept_count=int((s_explore & final).sum()),
        explore_dropped_count=int((s_explore & lost).sum()),
        customers_decided_by_value=int((decided_won == _DECIDED_VALUE).sum()),
        customers_decided_by_priority=int((decided_won == _DECIDED_PRIORITY).sum()),
        customers_decided_by_request_order=int(
            ((decided_won == _DECIDED_ORDER) | (decided_won == _DECIDED_EXPLORE_ORDER)).sum()
        ),
        customers_decided_by_explore=int((decided_won == _DECIDED_EXPLORE).sum()),
        contested_customers_channel_capped=int(((decided != _DECIDED_NONE) & ~group_won).sum()),
        created_at=now,
    )

    columns = _output_columns(out, key_cols)
    return out[columns], summary


def comparable_keys(
    treat_lists: Sequence[pd.DataFrame],
    intended: Sequence[pd.Series[Any] | None],
    config: ArbitrationConfig | None = None,
    key_cols: tuple[str, ...] = ("customer_id",),
) -> list[pd.Index[Any]]:
    """The customers each use case's campaign compares, one `Index` of customer key text per list.

    A campaign compares the customers it contacted with those it held back (DEC-1304). Arbitration takes
    some customers from a use case (another use case wins them), but only among those who were not held
    back, so measuring only the use case's winners would leave the held-back arm with every customer.
    Both arms are therefore cut with one rule that does not look at the hold-out: the arbitration is run
    again as if no one were held back, a held-back customer the use case's policy intended to contact
    (`intended`, one `Series` per list, indexed by customer key text, or None for a list that has none)
    competing like a treated one. A customer is compared in a use case when it would win him then.

    A held-back row competes with the value its list carries. A one-offer uplift list carries its own net value on
    a held-back row; a list that chose the offer per customer carries the net value of the offer its policy would
    have chosen (DEC-1311 (ah)), so a customer held back and intended by that use case and by a one-offer use case
    goes to the larger value (between values of one kind, (h)), not to whichever list came first.

    What is left out of both arms is therefore the same kind of customer: those another use case beats,
    and the customers a rival's random (explore) action took. A customer the hold-out of another use case
    or a channel cap kept from being contacted stays in both arms (intent to treat): nothing in them
    depends on the customer's own random draw.
    """
    cfg = config or ArbitrationConfig()
    if len(intended) != len(treat_lists):
        raise ValueError("`intended` needs one entry (a Series or None) per treat list.")
    stack = _stack_lists(treat_lists, cfg, key_cols)
    would = np.zeros(stack.n, dtype=bool)
    offset = 0
    for frame, flags in zip(treat_lists, intended, strict=True):
        size = len(frame.index)
        if flags is not None and size:
            aligned = flags.reindex(stack.keys[offset : offset + size]).fillna(False)
            would[offset : offset + size] = aligned.to_numpy(dtype=bool)
        offset += size
    held = stack.holdout_own | stack.control_own
    ranking = _rank(stack, stack.treat_flag | (held & would), cfg.contact_cap_per_customer)
    won = np.zeros(stack.n, dtype=bool)
    won[ranking.s_pos[ranking.keep]] = True
    return [pd.Index(stack.keys[won & (stack.src == i)]) for i in range(len(treat_lists))]


def _output_columns(frame: pd.DataFrame, key_cols: tuple[str, ...]) -> list[str]:
    """The columns of the arbitrated list: the treat list's own, in its order, then the arbitration's."""
    treat_list_columns = [
        *key_cols,
        "use_case",
        "model_version",
        SEGMENT_COLUMN,
        BAND_COLUMN,
        "treat",
        "holdout",
        "explore",
        "suppression_reason",
        "offer",
        "channel",
        "contactable_channels",
        "runner_up_offer",
        "runner_up_net_value",
        "offer_reason",
        CONTROL_GROUP_COLUMN,
        "net_value",
        EXPECTED_GROSS_VALUE_COLUMN,
        *REASON_COLUMNS,
    ]
    own = [c for c in treat_list_columns if c in frame.columns]
    return [
        *own,
        WINNING_USE_CASE_COLUMN,
        LOSING_ACTIONS_COLUMN,
        PRIORITY_SCORE_COLUMN,
        PRIORITY_WEIGHT_COLUMN,
        ARBITRATION_REASON_COLUMN,
        HOLDOUT_USE_CASES_COLUMN,
        CONTROL_USE_CASES_COLUMN,
    ]


def winning_keys(arbitrated: pd.DataFrame, key_cols: tuple[str, ...], use_case: str) -> set[str]:
    """The customer keys `use_case` won, spelled as the treat list spells them (`key_text`, joined by `KEY_SEPARATOR`).

    One campaign per use case measures exactly these rows (`engine.measurement.campaign.create_arbitrated_campaign`).
    """
    won = arbitrated[arbitrated["treat"].astype(bool) & (arbitrated[WINNING_USE_CASE_COLUMN] == use_case)]
    if won.empty:
        return set()
    return set(_join_keys_vectorised(won, key_cols))


def arbitrated_table(df: pd.DataFrame, key_cols: tuple[str, ...]) -> pa.Table:
    """Build the PyArrow table of an arbitrated treat list: booleans (null where unknown), floats, strings."""
    float_columns = {
        "net_value",
        EXPECTED_GROSS_VALUE_COLUMN,
        "runner_up_net_value",
        PRIORITY_SCORE_COLUMN,
        PRIORITY_WEIGHT_COLUMN,
    }
    fields: list[pa.Field[Any]] = []
    arrays: list[pa.Array[Any]] = []
    for name in _output_columns(df, key_cols):
        col = df[name] if name in df.columns else pd.Series([None] * len(df), dtype="object")
        if name in _BOOL_COLUMNS:
            fields.append(pa.field(name, pa.bool_()))
            arrays.append(pa.array(col.astype("boolean"), type=pa.bool_()))
        elif name in float_columns:
            fields.append(pa.field(name, pa.float64()))
            arrays.append(pa.array(pd.to_numeric(col, errors="coerce"), type=pa.float64()))
        else:
            fields.append(pa.field(name, pa.string()))
            arrays.append(pa.array(col.astype("string"), type=pa.string()))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def parquet_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink)  # type: ignore[no-untyped-call]
    return sink.getvalue()


def csv_bytes(table: pa.Table) -> bytes:
    """The CSV, written column-wise like the treat list's: flags `1` and `0`, null as empty."""
    return table_csv_bytes(table)
