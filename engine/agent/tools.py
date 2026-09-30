"""The helper's tools (Plan G §8.3): each wraps a system the platform already has.

A tool takes validated arguments and an `AgentContext` and returns JSON. `call_tool` is the only way
in: it refuses an unknown tool or bad arguments with a code, runs the tool, and wraps the result in a
`ToolResult` with an evidence id that proposals cite (DEC-1010). **No tool writes data**; the only
writer is `apply` (DEC-1002).

M71 ships the read tools. Values that leave a tool are either counts the engine measured or cells
that were masked whole (`egress.mask_value`) and then cut; a column the profile marked as personal data, or
that `agent.always_hide_columns` names, never shows a value at all.

The look-at-the-data tools (`sample_rows`, `value_counts`, `find_values`, `describe_numbers`,
`describe_dates`, `compare_columns`, `describe_missing`, `describe_duplicates`) let the chat helper
read the file itself instead of guessing from five examples. All of them compute on `ctx.frame`
with vectorised pandas (a million rows is a second or two; anything slower works on an evenly
spread sample and says `sampled`), keep every result well under `MAX_LOOK_RESULT_CHARS`, and keep
text in two kinds of place: names and fixed words under the keys `column`, `columns`, `type`,
`kind`, `code`, `name`, `function`, `dtype`, `label` and `message`, and everything that came from
a cell under other keys (`value`, `values`, `rows`, `cells`, `matches`, ...), each cell passed
through `_masked`. Numbers may appear anywhere.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final

import numpy as np
import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from engine.agent.checks import check_plan
from engine.agent.config import DataAccess
from engine.agent.contracts import ToolResult
from engine.agent.egress import mask_value
from engine.agent.formats import (
    _NUMERIC_DAY_MONTH,
    MIN_CONVERT_SHARE,
    _decimal_style,
    date_order,
    find_format_issues,
    parse_dates,
    parse_numbers,
)
from engine.agent.placeholders import (
    MAX_PLACEHOLDER_VALUES,
    find_placeholder_values,
    number_text,
    placeholder_impact,
)
from engine.agent.untrusted import MAX_PROMPT_LIST, display_name, quoted, resolve_column
from engine.config import ConfigError, PrimaryKey, RunMode, UseCaseConfig, resolve_config
from engine.contracts import DatasetProfile, FeatureSchema
from engine.pii import marker_for
from engine.stages import validate
from engine.utils.time import utc_now

__all__ = [
    "TOOLS",
    "AgentContext",
    "AgentToolError",
    "Tool",
    "ToolKind",
    "call_tool",
]

MAX_BUCKETS: Final[int] = 12
"""Values `inspect_column` lists as top values, and buckets of the outcome rate."""
MAX_EXAMPLES: Final[int] = 10
"""Distinct example values `inspect_column` shows (was 5)."""
LOOK_CELL_CHARS: Final[int] = 60
"""A cell shown by a look tool is masked, then cut to this many characters."""
MAX_LOOK_RESULT_CHARS: Final[int] = 5_500
"""A look tool's result stays under this many characters of JSON (the prompt cap is 60,000)."""
LOOK_SAMPLE_ROWS: Final[int] = 100_000
"""Heavier computations (co-missing sets, cross-tabs) read at most this many evenly spread rows."""
MAX_PARSED_DISTINCT: Final[int] = 20_000
"""Text dates and numbers are parsed one distinct value at a time; past this many distinct values the
column is read on an evenly spread sample of rows instead."""
PERSONAL_CELL: Final[str] = "[personal data]"
HIDDEN_CELL: Final[str] = "[hidden by your settings]"
PERSONAL_MESSAGE: Final[str] = (
    "This column holds personal data, so its values are not shown; only counts are."
)
HIDDEN_MESSAGE: Final[str] = (
    "This column is hidden by your settings (agent.always_hide_columns), so its values are not shown; only counts are."
)
K_MATCHES: Final[int] = 5
"""In `summaries_only`, `find_values` answers nothing about fewer than this many rows: a count of 1 to 4 is
too close to "is this exact value in the file", which a model that knows only shapes could ask one
character at a time."""
MAX_PROFILE_COLUMNS: Final[int] = MAX_PROMPT_LIST
"""`get_profile` lists at most this many columns per call; `offset` pages through a wider file, so a
5,000-column file cannot fill a prompt (M77 hardening)."""


class AgentToolError(Exception):
    """A tool call the engine refused; `code` is machine-readable, `message` is for the model and logs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ToolKind(StrEnum):
    READ = "read"
    PROPOSE = "propose"
    ASK = "ask"


@dataclass(frozen=True)
class AgentContext:
    """What every tool may read: one upload, its profile, its rows (capped) and its use case."""

    use_case_id: str
    config: UseCaseConfig
    config_root: Path | None
    upload_id: str
    mode: RunMode
    profile: DatasetProfile
    frame: pd.DataFrame
    target: str | None = None
    schema: FeatureSchema | None = None
    clock: Callable[[], datetime] = utc_now

    def resolve(self, name: str) -> str:
        """The file's column `name` means: exact, or the one column whose shown (cleaned, cut) name it is."""
        found = resolve_column(name, self.frame.columns)
        if found is None:
            raise AgentToolError(
                "AGENT_COLUMN_UNKNOWN", f"There is no column {quoted(name[:80])} in this file."
            )
        return found

    def column(self, name: str) -> pd.Series[Any]:
        return self.frame[self.resolve(name)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class NoArgs(_Args):
    pass


class ProfileArgs(_Args):
    offset: int = Field(default=0, ge=0, description="First column to list, for a file with many columns.")


class ColumnArgs(_Args):
    column: str = Field(min_length=1, description="Column name exactly as in the file.")


class FormatArgs(_Args):
    columns: tuple[str, ...] | None = Field(
        default=None, description="Columns to check; all text columns when omitted."
    )


class PlaceholderArgs(_Args):
    column: str = Field(min_length=1, description="Column name exactly as in the file.")
    values: tuple[float, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PLACEHOLDER_VALUES,
        description="The numbers to treat as missing; the placeholder codes `find_format_issues` found when omitted.",
    )


class CheckArgs(_Args):
    primary_key: str | None = Field(default=None, description="The ID column.")
    target: str | None = Field(default=None, description="The outcome column (training only).")
    overrides: dict[str, JsonValue] = Field(default_factory=dict, description="Run overrides, dotted paths.")


class _LookArgs(_Args):
    """Arguments of a look tool: a number the model wrote as text is still a value to look for."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, coerce_numbers_to_str=True
    )


_Spaced = Annotated[str, StringConstraints(strip_whitespace=False)]
"""A value to look for keeps its leading and trailing spaces: `'pro '` next to `'pro'` is the point."""


class SampleRowsArgs(_LookArgs):
    columns: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=12,
        description="1 to 12 column names; the first 8 columns when omitted.",
    )
    n: int = Field(default=10, ge=1, le=20, description="How many rows, 1 to 20.")
    offset: int = Field(default=0, ge=0, le=999_999, description="Rows to skip from the top.")
    where_column: str | None = Field(
        default=None, min_length=1, description="Only rows whose value in this column is exactly `equals`."
    )
    equals: _Spaced | None = Field(
        default=None, min_length=1, max_length=200, description="The exact value (spaces count)."
    )

    @model_validator(mode="after")
    def _both_or_neither(self) -> SampleRowsArgs:
        if (self.where_column is None) != (self.equals is None):
            raise ValueError("give `where_column` and `equals` together, or neither")
        return self


class ValueCountsArgs(_LookArgs):
    column: str = Field(min_length=1, description="Column name.")
    top: int = Field(default=20, ge=1, le=50, description="How many of the most common values, 1 to 50.")


class FindValuesArgs(_LookArgs):
    column: str = Field(min_length=1, description="Column name.")
    contains: _Spaced = Field(
        min_length=1,
        max_length=40,
        description="Plain text, matched anywhere in a value, ignoring case (spaces count).",
    )
    n: int = Field(
        default=10, ge=1, le=20, description="How many different matching values to list, 1 to 20."
    )


class CompareArgs(_LookArgs):
    left: str = Field(min_length=1, description="First column name.")
    right: str = Field(min_length=1, description="Second column name.")


class MissingArgs(_LookArgs):
    columns: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="1 to 30 column names; every column when omitted.",
    )


class DuplicatesArgs(_LookArgs):
    columns: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=5,
        description="1 to 5 column names that make up the key; the whole row when omitted.",
    )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    kind: ToolKind
    args_model: type[_Args]
    run: Callable[[AgentContext, Any], dict[str, Any]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    """Plain JSON: numpy scalars to Python, NaN/inf to null, tuples to lists."""

    def default(item: Any) -> Any:
        if hasattr(item, "item"):
            return item.item()
        if hasattr(item, "isoformat"):
            return item.isoformat()
        return str(item)

    text = json.dumps(value, default=default, allow_nan=True)
    return json.loads(text, parse_constant=lambda _: None)


def _clean_float(value: float | None) -> float | None:
    """`value` to six decimals; a value below 0.0001 keeps six significant figures instead.

    A rate of 3e-7 rounded to six decimals reads 0.0, which is a different claim (`0.0` says none).
    Python writes such a value in exponent form (`3.14159e-07`), so no run of seven digits appears in the
    JSON, which the egress backstop would read as a phone number; `0.000666667` would be one.
    """
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    number = float(value)
    rounded = round(number, 6)
    if number != 0.0 and abs(number) < 1e-4:
        return float(f"{number:.6g}")
    return rounded


MAX_SCANNED_CELL_CHARS: Final[int] = 2_000
"""How much of one cell `_masked` scans. A cell in a client's file can be a pasted document; what is shown
is at most `LOOK_CELL_CHARS` characters of it, so scanning ten thousand is time spent for nothing."""
_CELL_BREAK: Final[re.Pattern[str]] = re.compile(r"[\s@<>()\[\],;:\"]")
"""What ends an address, a URL or a number in a cell: white space and the delimiters `engine.pii` uses."""


def _masked(value: Any, limit: int) -> str:
    """One cell for a tool result: the WHOLE cell masked with the complete scanner set, then cut to `limit`.

    The set is the gate's (`egress.mask_value`: e-mail, phone, PAN, Aadhaar, and also card, IBAN, IP, URL,
    address-shaped identifier, API-key-like token and a run of nine digits). Cutting first would leave half
    of a secret that no longer matches its pattern and so passes unmasked (`validate.sample_values`
    documents the order, DEC-095), and a card, key or address needs its whole length to be recognised at
    all, so nothing here may depend on the gate seeing a cell before it was cut (review fix 6).

    A cell longer than `MAX_SCANNED_CELL_CHARS` is masked on its head only and its remainder is replaced
    by a length marker (`[8000 characters in all]`); the remainder is never shown. The head ends at the
    last delimiter, so no address or number is left half-read at the cut; a head with no delimiter in
    its second half is one unbroken blob (a base64 string, a giant "address") and is shown as a token
    marker. When the masked head is shorter than `limit` (it was mostly masked), whatever follows its
    last marker is dropped; when it is not, the cut at `limit` falls long before the end of the head.
    """
    text = str(value)
    if len(text) <= MAX_SCANNED_CELL_CHARS:
        shown = mask_value(text, limit=limit)
        if len(shown) > limit:  # cut: mask_value's visible `…` is one more character than the limit allows
            shown = mask_value(text, limit=limit - 1)
        return shown
    head = text[:MAX_SCANNED_CELL_CHARS]
    breaks = [match.start() for match in _CELL_BREAK.finditer(head, MAX_SCANNED_CELL_CHARS // 2)]
    if not breaks:
        return marker_for("token")[:limit]
    masked = mask_value(head[: breaks[-1]], limit=None)
    if len(masked) > limit:
        return masked[:limit]
    kept = masked[: masked.rfind("]") + 1] if "[REDACTED:" in masked else ""
    return f"{kept} [{len(text)} characters in all]".strip()[:limit]


def _column_profiles(ctx: AgentContext) -> dict[str, Any]:
    return {column.name: column for column in ctx.profile.columns}


def _pii(ctx: AgentContext, name: str) -> bool:
    """Whether the profile marks the column as personal data."""
    column = _column_profiles(ctx).get(name)
    return bool(column is not None and column.pii_kinds)


def _hidden_by_settings(ctx: AgentContext, name: str) -> bool:
    """Whether `agent.always_hide_columns` names the column (case ignored, as the egress gate reads it)."""
    hidden = {entry.casefold() for entry in ctx.config.agent.always_hide_columns}
    return bool(hidden) and (name.casefold() in hidden or display_name(name).casefold() in hidden)


def _personal(ctx: AgentContext, name: str) -> bool:
    """Whether the column may show no value of any kind: personal data, or hidden by the settings.

    A column in `agent.always_hide_columns` is treated exactly like one the profile marks as personal
    (a value, a minimum, a match, a count that depends on what was searched for): the gate hides the
    strings of such a column anyway, but a tool that answers "which rows equal X" or "how many contain
    X" for it is an oracle that reads it out (review findings 7 and 12).
    """
    return _pii(ctx, name) or _hidden_by_settings(ctx, name)


def _withheld_cell(ctx: AgentContext, name: str) -> str:
    return PERSONAL_CELL if _pii(ctx, name) else HIDDEN_CELL


def _withheld_message(ctx: AgentContext, *names: str) -> str:
    return PERSONAL_MESSAGE if any(_pii(ctx, name) for name in names) else HIDDEN_MESSAGE


def _label(ctx: AgentContext, target: str, params: validate.CheckParams) -> tuple[str | None, int, int]:
    """The engine's positive-label rule on the outcome column alone.

    `resolve_positive_label` derives facts (type inference, personal-data scans) for every column of
    the frame it is given, and only the outcome's are read; on a million-row file passing the whole
    frame cost most of a minute per call (M77, `reports/plan_g_performance.md`).
    """
    return validate.resolve_positive_label(ctx.frame[[target]], params)


def _positive_mask(ctx: AgentContext, target: str) -> tuple[pd.Series[Any] | None, str | None]:
    """A boolean series for "outcome is positive", by the engine's own label rule, or None."""
    if target not in ctx.frame.columns:
        return None, None
    params = validate.CheckParams(target=target, positive_label=ctx.config.target.positive_label)
    label, positives, negatives = _label(ctx, target, params)
    if label is None or positives + negatives == 0:
        return None, None
    series = ctx.frame[target]
    present = series.notna()
    mask = present & (series.astype(str).str.strip().str.lower() == label.strip().lower())
    return mask.where(present), label


def _relation_to_target(ctx: AgentContext, name: str) -> dict[str, Any] | None:
    if ctx.target is None or name == ctx.target or _personal(ctx, name):
        return None
    positive, label = _positive_mask(ctx, ctx.target)
    if positive is None:
        return None
    series = ctx.frame[name]
    frame = pd.DataFrame({"x": series, "y": positive}).dropna(subset=["y"])
    buckets: list[dict[str, Any]] = []
    if (
        pd.api.types.is_numeric_dtype(series)
        and not pd.api.types.is_bool_dtype(series)
        and series.nunique() > MAX_BUCKETS
    ):
        binned = pd.qcut(frame["x"], q=5, duplicates="drop")
        summary = frame.groupby(binned, observed=True)["y"].agg(["size", "mean"])
        intervals: list[Any] = list(summary.index)
        for interval, rows, rate in zip(
            intervals, summary["size"].tolist(), summary["mean"].tolist(), strict=True
        ):
            buckets.append(
                {
                    "bucket": f"{_clean_float(interval.left)} to {_clean_float(interval.right)}",
                    "rows": int(rows),
                    "positive_rate": _clean_float(float(rate)),
                }
            )
    else:
        keys = frame["x"].astype(str).where(frame["x"].notna(), "(empty)")
        top = keys.value_counts().head(MAX_BUCKETS).index
        keys = keys.where(keys.isin(top), "(other)")
        summary = frame.groupby(keys)["y"].agg(["size", "mean"]).sort_values("size", ascending=False)
        labels: list[Any] = list(summary.index)
        for key, rows, rate in zip(labels, summary["size"].tolist(), summary["mean"].tolist(), strict=True):
            buckets.append(
                {
                    "bucket": _masked(key, 60),
                    "rows": int(rows),
                    "positive_rate": _clean_float(float(rate)),
                }
            )
    overall = float(frame["y"].mean()) if len(frame) else None
    return {"positive_label": label, "overall_positive_rate": _clean_float(overall), "buckets": buckets}


def _name_matches(name: str, hints: tuple[str, ...]) -> bool:
    folded = name.casefold()
    parts = set(re.split(r"[_\s.\-]+", folded))
    return any(hint.casefold() == folded or hint.casefold() in parts for hint in hints)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
def _get_profile(ctx: AgentContext, args: ProfileArgs) -> dict[str, Any]:
    profile = ctx.profile
    shown = profile.columns[args.offset : args.offset + MAX_PROFILE_COLUMNS]
    return {
        "file_name": profile.file_name,
        "rows": profile.row_count,
        "columns_total": len(profile.columns),
        "columns_offset": args.offset,
        "columns": [
            {
                "name": column.name,
                "type": column.inferred_type.value,
                "null_rate": round(column.null_rate, 4),
                "distinct": column.distinct_count,
                "unique": column.is_unique,
                "constant": column.is_constant,
                "looks_like_id": column.looks_like_id,
                "looks_like_time": column.looks_like_time,
                "personal_data": list(column.pii_kinds),
                "personal_data_in_text": list(column.free_text_pii_kinds),
            }
            for column in shown
        ],
        "primary_key_candidates": list(profile.primary_key_candidates),
        "time_column_candidates": list(profile.time_column_candidates),
        "target_candidate": profile.target_candidate,
        "missing_value_rate_pct": profile.missing_value_rate_pct,
    }


def _inspect_column(ctx: AgentContext, args: ColumnArgs) -> dict[str, Any]:
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    column = _column_profiles(ctx).get(name)
    personal = _personal(ctx, name)
    result: dict[str, Any] = {
        "column": name,
        "rows": len(series),
        "empty": int(series.isna().sum()),
        "distinct": int(series.nunique(dropna=True)),
        "personal_data": list(column.pii_kinds) if column is not None else [],
    }
    if column is not None:
        result["type"] = column.inferred_type.value
    if column is not None and not personal:
        # The smallest and largest value of a personal column are somebody's value.
        result.update(
            {
                "minimum": _clean_float(column.minimum),
                "maximum": _clean_float(column.maximum),
                "mean": _clean_float(column.mean),
            }
        )
    if personal:
        result["examples"] = []
        result["top_values"] = []
        result["message"] = _withheld_message(ctx, name)
    else:
        present = series.dropna().astype(str)
        result["examples"] = [_masked(v, 80) for v in present.drop_duplicates().head(MAX_EXAMPLES)]
        counts = present.value_counts().head(MAX_BUCKETS)
        result["top_values"] = [
            {"value": _masked(value, 60), "rows": int(rows)} for value, rows in counts.items()
        ]
    result["relation_to_outcome"] = _relation_to_target(ctx, name)
    return result


def _describe_outcome(ctx: AgentContext, args: ColumnArgs) -> dict[str, Any]:
    """The outcome column by the engine's own label rule: how many rows are "yes"."""
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    params = validate.CheckParams(target=name, positive_label=ctx.config.target.positive_label)
    label, positives, negatives = _label(ctx, name, params)
    total = positives + negatives
    return {
        "column": name,
        "rows": len(series),
        "empty": int(series.isna().sum()),
        "distinct": int(series.nunique(dropna=True)),
        "two_valued": label is not None,
        "positive_label": label,
        "positives": positives,
        "negatives": negatives,
        "positive_rate": _clean_float(positives / total) if total else None,
    }


def _find_format_issues(ctx: AgentContext, args: FormatArgs) -> dict[str, Any]:
    asked = [ctx.resolve(c) for c in args.columns] if args.columns else [str(c) for c in ctx.frame.columns]
    columns = [str(c) for c in asked if not _pii(ctx, c)]  # a hidden column is still read by the rules
    # Text columns get the format checks, number columns the placeholder check (DEC-1221). The outcome is
    # never offered a placeholder fix: a value outside its labels is `TARGET_NOT_BINARY`'s to report.
    numbers = [c for c in columns if c != ctx.target]
    return {
        "issues": [
            *(issue.as_json() for issue in find_format_issues(ctx.frame, columns=columns)),
            *(issue.as_json() for issue in find_placeholder_values(ctx.frame, columns=numbers)),
        ]
    }


def _describe_placeholder_values(ctx: AgentContext, args: PlaceholderArgs) -> dict[str, Any]:
    """What treating some numbers of a column as missing would do, measured (DEC-1222).

    The outcome figures need a known, two-valued outcome (`ctx.target`) and are null otherwise. No model
    is trained, so nothing here is a model score (DEC-1223).
    """
    name = ctx.resolve(args.column)
    if _personal(ctx, name):
        return _personal_refusal(ctx, name)
    series = ctx.frame[name]
    if args.values is not None:
        values: tuple[float, ...] = tuple(args.values)
    else:
        found = find_placeholder_values(ctx.frame, columns=[name])
        values = found[0].values if found else ()
    result: dict[str, Any] = {"column": _shown(name), "values": [number_text(v) for v in values]}
    if not values:
        result.update(
            {
                "rows": len(series),
                "message": "No placeholder value was found in this column; name the values to look at.",
            }
        )
        return result
    positive: pd.Series[Any] | None = None
    if ctx.target is not None and ctx.target != name:
        mask, _ = _positive_mask(ctx, ctx.target)
        positive = None if mask is None else pd.to_numeric(mask.astype("float64"), errors="coerce")
    result.update(placeholder_impact(series, values, positive=positive))
    return result


def _find_roles(ctx: AgentContext, _: NoArgs) -> dict[str, Any]:
    config = ctx.config
    hints = config.agent.column_hints
    names = [str(c) for c in ctx.frame.columns]
    configured = config.target.column
    exact = [n for n in names if configured and n == configured]
    folded = [n for n in names if configured and n != configured and n.casefold() == configured.casefold()]
    synonyms = [n for n in names if n not in exact + folded and _name_matches(n, hints.target_synonyms)]

    def outcome(name: str) -> dict[str, Any]:
        series = ctx.frame[name]
        return {
            "column": name,
            "distinct": int(series.nunique(dropna=True)),
            "empty": int(series.isna().sum()),
        }

    return {
        "configured_target": configured,
        "target_exact": [outcome(n) for n in exact],
        "target_case_insensitive": [outcome(n) for n in folded],
        "target_by_synonym": [outcome(n) for n in synonyms],
        "primary_key_candidates": list(ctx.profile.primary_key_candidates),
        "time_column_candidates": list(ctx.profile.time_column_candidates),
        "consent": [n for n in names if _name_matches(n, hints.consent)],
        "opt_out": [n for n in names if _name_matches(n, hints.opt_out)],
        "recently_contacted": [n for n in names if _name_matches(n, hints.recently_contacted)],
    }


def _describe_repeats(ctx: AgentContext, args: ColumnArgs) -> dict[str, Any]:
    """How often each value of an ID column repeats: the evidence behind "combine the rows?" (M76)."""
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    counts = series.dropna().value_counts()
    ids = len(counts)
    present = int(counts.sum())
    return {
        "column": name,
        "rows": len(series),
        "empty": int(series.isna().sum()),
        "ids": ids,
        "rows_per_id": round(present / ids, 1) if ids else None,
        "most_rows": int(counts.max()) if ids else 0,
        "ids_on_several_rows": int((counts > 1).sum()),
    }


def _check_data(ctx: AgentContext, args: CheckArgs) -> dict[str, Any]:
    try:
        resolved = resolve_config(ctx.use_case_id, dict(args.overrides), root=ctx.config_root)
    except ConfigError as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    primary_key: PrimaryKey | None = ctx.resolve(args.primary_key) if args.primary_key else None
    report = check_plan(
        ctx.frame,
        resolved.config,
        mode=ctx.mode,
        primary_key=primary_key,
        target=ctx.resolve(args.target) if args.target else None,
        upload_id=ctx.upload_id,
        row_count=ctx.profile.row_count,
        schema=ctx.schema,
    )
    return {
        "passed": report.passed,
        "errors": report.error_count,
        "warnings": report.warning_count,
        "checks": [
            {
                "code": check.code,
                "severity": check.severity.value,
                "column": check.column,
                "message": check.message,
                "suggestion": check.suggestion,
                "acknowledgeable": check.acknowledgeable,
                "acknowledged": check.acknowledged,
                "override_path": check.details.get("override_path"),
                "acknowledge": check.details.get("acknowledge"),
            }
            for check in report.checks
        ],
    }


# ---------------------------------------------------------------------------
# Look-at-the-data tools
# ---------------------------------------------------------------------------
def _json_size(value: Any) -> int:
    """The characters of JSON a result takes in a prompt (quotes and control characters count in full)."""
    return len(json.dumps(value, ensure_ascii=False))


def _fit(result: dict[str, Any], *lists: list[Any], keep: int = 0) -> bool:
    """Drop items from the end of each of `lists` (in the order given) until `result` fits its budget.

    The lists are parts of `result`. What counts is the real JSON size, not an estimate: a cell full of
    quotes or control characters is several times longer once escaped. Returns whether anything was dropped.
    """
    cut = False
    for items in lists:
        while len(items) > keep and _json_size(result) > MAX_LOOK_RESULT_CHARS:
            items.pop()
            cut = True
    return cut


def _shown(name: object) -> str:
    """A column name as a result lists it: cleaned and cut to 64 characters (`resolve` reads it back)."""
    return display_name(name)


def _zeroed(frame: pd.DataFrame) -> pd.DataFrame:
    """`frame` with `-0.0` written as `0.0` (equal numbers, different bits, so different hashes)."""
    out = frame.copy()
    for position, dtype in enumerate(out.dtypes):
        if pd.api.types.is_float_dtype(dtype):
            out.isetitem(position, (out.iloc[:, position] + 0.0).array)
    return out


def _round(value: float | np.floating[Any] | None) -> float | None:
    """`_clean_float` that never returns `-0.0`."""
    cleaned = _clean_float(None if value is None else float(value))
    return None if cleaned is None else cleaned + 0.0


def _share(part: int | float, whole: int | float) -> float:
    return float(_clean_float(float(part) / float(whole)) or 0.0) if whole else 0.0


def _kinds(ctx: AgentContext, name: str) -> list[str]:
    column = _column_profiles(ctx).get(name)
    return list(column.pii_kinds) if column is not None else []


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set, np.ndarray)):
        return False
    return bool(pd.isna(value))


def _cell(value: Any, limit: int = LOOK_CELL_CHARS) -> str | None:
    """One cell as a look tool shows it: an empty cell is null, any other is masked then cut."""
    return None if _is_missing(value) else _masked(value, limit)


def _numeric_dtype(series: pd.Series[Any]) -> bool:
    return bool(pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series))


def _floats(series: pd.Series[Any]) -> np.ndarray[Any, Any]:
    """A numeric series as float64 with NaN for an empty cell (nullable integers included)."""
    return np.asarray(pd.to_numeric(series, errors="coerce").to_numpy(dtype="float64", na_value=np.nan))


def _empty_mask(series: pd.Series[Any]) -> np.ndarray[Any, Any]:
    """Rows with no value: null, or (in a text column) an empty string - a CSV can hold either."""
    gap = series.isna().to_numpy(dtype=bool)
    if isinstance(series.dtype, pd.CategoricalDtype):
        gap = gap | series.astype(object).eq("").fillna(False).to_numpy(dtype=bool)
    elif pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
        gap = gap | series.eq("").fillna(False).to_numpy(dtype=bool)
    return np.asarray(gap)


def _counts(series: pd.Series[Any]) -> pd.Series[Any]:
    """How many rows hold each distinct non-empty value, most common first."""
    try:
        counts = series.value_counts(dropna=True)
    except TypeError:  # a cell that cannot be hashed (a list from a Parquet file)
        counts = series.dropna().astype(str).value_counts()
    counts = counts[counts > 0]  # a category no row uses
    if "" in counts.index:  # an empty string is an empty cell in every text dtype
        counts = counts.drop("")
    return counts


def _spread(frame_or_series: Any, cap: int) -> tuple[Any, bool]:
    """At most `cap` evenly spread rows (deterministic), and whether that was fewer than all."""
    rows = len(frame_or_series)
    if rows <= cap:
        return frame_or_series, False
    step = -(-rows // cap)
    return (
        frame_or_series.iloc[::step] if hasattr(frame_or_series, "iloc") else frame_or_series[::step]
    ), True


def _parse_source(series: pd.Series[Any]) -> tuple[pd.Series[Any], bool]:
    """The whole column if it has few distinct values (parsing is per distinct value), else a spread of rows."""
    if len(series) <= MAX_PARSED_DISTINCT or series.nunique(dropna=True) <= MAX_PARSED_DISTINCT:
        return series, False
    return _spread(series, MAX_PARSED_DISTINCT)


def _personal_refusal(ctx: AgentContext, name: str, **extra: Any) -> dict[str, Any]:
    """The answer for a personal-data column: counts, the kinds found, and no value of any kind."""
    series = ctx.frame[name]
    return {
        "column": _shown(name),
        "rows": len(series),
        "empty": int(_empty_mask(series).sum()),
        "distinct": int(series.nunique(dropna=True)),
        "personal_data": _kinds(ctx, name),
        "message": _withheld_message(ctx, name),
        **extra,
    }


def _equals_mask(series: pd.Series[Any], text: str) -> np.ndarray[Any, Any]:
    """Rows whose value is exactly `text` (a number column is compared as a number)."""
    present = series.notna().to_numpy()
    if pd.api.types.is_bool_dtype(series):
        return np.asarray(present & (series.astype(str).str.lower().to_numpy() == text.lower()))
    if pd.api.types.is_numeric_dtype(series):
        try:
            wanted = float(text)
        except ValueError:
            return np.zeros(len(series), dtype=bool)
        return np.asarray(_floats(series) == wanted)
    values = series.astype(str)
    hit = values.to_numpy() == text
    if text.lower() in {"true", "false"} and pd.api.types.is_object_dtype(series):
        booleans = series.map(lambda cell: isinstance(cell, (bool, np.bool_))).to_numpy(dtype=bool)
        hit = np.where(booleans, values.str.lower().to_numpy() == text.lower(), hit)
    return np.asarray(present & hit)


def _summaries_only(ctx: AgentContext) -> bool:
    return ctx.config.agent.ai_data_access is DataAccess.SUMMARIES_ONLY


def _sample_rows(ctx: AgentContext, args: SampleRowsArgs) -> dict[str, Any]:
    frame = ctx.frame
    names = list(dict.fromkeys(ctx.resolve(c) for c in args.columns)) if args.columns else None
    shown = names if names is not None else [str(c) for c in frame.columns[:8]]
    if args.where_column is not None and args.equals is not None:
        where = ctx.resolve(args.where_column)
        if _pii(ctx, where):
            raise AgentToolError(
                "AGENT_TOOL_ARGS_INVALID",
                f"{where[:60]}: a personal-data column cannot be used to pick rows.",
            )
        positions = np.flatnonzero(_equals_mask(frame[where], args.equals))
        if _hidden_by_settings(ctx, where) or (_summaries_only(ctx) and len(positions) < K_MATCHES):
            # A column hidden by the settings is not searched, and in `summaries_only` neither is a value
            # that fewer than K rows hold. The answer does not depend on `equals`: it is no oracle.
            return _no_search(ctx, shown, args, where)
        total = len(positions)
        chosen = positions[args.offset : args.offset + args.n]
    else:
        total = len(frame)
        chosen = np.arange(min(args.offset, total), min(total, args.offset + args.n))
    picked = frame.iloc[chosen]
    personal = {name for name in shown if _personal(ctx, name)}
    data = {
        name: picked[name].tolist() for name in shown if name not in personal
    }  # never read a personal column
    rows: list[list[str | None]] = [
        [_withheld_cell(ctx, name) if name in personal else _cell(data[name][i]) for name in shown]
        for i in range(len(chosen))
    ]
    result: dict[str, Any] = {
        "columns": [_shown(name) for name in shown],
        "rows": rows,
        "offset": args.offset,
        "returned": len(rows),
        "total_matching": total,
        "truncated": False,
        "withheld": _withheld(ctx, [name for name in shown if name in personal]),
    }
    _fit(result, rows, keep=1)  # the real JSON size: fewer rows, and `next_offset` says where to go on
    result["returned"] = len(rows)
    result["truncated"] = args.offset + len(rows) < total
    if result["truncated"]:
        result["next_offset"] = args.offset + len(rows)
    return result


def _withheld(ctx: AgentContext, names: list[str]) -> list[dict[str, str]]:
    return [
        {"column": _shown(name), "kind": "personal_data" if _pii(ctx, name) else "hidden_by_settings"}
        for name in names
    ]


def _no_search(ctx: AgentContext, shown: list[str], args: SampleRowsArgs, where: str) -> dict[str, Any]:
    """`sample_rows` that could not pick rows by `where`: no rows and no count, whatever `equals` was."""
    return {
        "columns": shown,
        "rows": [],
        "offset": args.offset,
        "returned": 0,
        "total_matching": None,
        "truncated": False,
        "withheld": _withheld(ctx, [name for name in shown if _personal(ctx, name)]),
        "message": (
            f"{where[:60]}: rows cannot be picked by this value - the column is hidden by your settings, "
            f"or in summaries_only fewer than {K_MATCHES} rows match."
        ),
    }


def _value_counts(ctx: AgentContext, args: ValueCountsArgs) -> dict[str, Any]:
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    counts = _counts(series)
    rows = len(series)
    result: dict[str, Any] = {
        "column": _shown(name),
        "rows": rows,
        "distinct": len(counts),
        "empty": int(_empty_mask(series).sum()),
        "personal_data": _kinds(ctx, name),
    }
    if _personal(ctx, name):
        result.update(
            {
                "values": [],
                "values_seen_once": int((counts == 1).sum()),
                "most_rows_for_one_value": int(counts.iloc[0]) if len(counts) else 0,
                "message": _withheld_message(ctx, name),
            }
        )
        return result
    top = counts.head(args.top)
    result["values"] = [
        {"value": _masked(value, LOOK_CELL_CHARS), "rows": int(n), "share": _share(int(n), rows)}
        for value, n in top.items()
    ]
    listed = result["values"]
    if _fit(result, listed, keep=1):
        result["truncated"] = True
        result["shown"] = len(listed)
    result["other_rows"] = int(counts.sum()) - sum(int(item["rows"]) for item in listed)
    return result


def _find_values(ctx: AgentContext, args: FindValuesArgs) -> dict[str, Any]:
    name = ctx.resolve(args.column)
    counts = _counts(ctx.frame[name])
    result: dict[str, Any] = {
        "column": _shown(name),
        "rows": len(ctx.frame),
        "personal_data": _kinds(ctx, name),
    }
    if _personal(ctx, name):
        # No search at all on a personal or hidden column: a count (or a yes / no) that depends on the text
        # is an oracle that reads the value out one character at a time (review findings 7 and 12). The
        # answer is the same whatever `contains` is; what the column holds in total is `distinct`.
        result.update(
            {
                "distinct": len(counts),
                "matches": [],
                "message": f"{_withheld_message(ctx, name)} It cannot be searched.",
            }
        )
        return result
    texts = pd.Series(counts.index.astype(str), dtype=object)
    hit = texts.str.contains(args.contains, case=False, regex=False).to_numpy(dtype=bool)
    rows = counts.to_numpy()[hit]
    if _summaries_only(ctx):
        # A model that knows only shapes must not read a value out with counts: what fewer than K rows
        # hold is not reported, whether it is 0 or 1 to K-1 rows (a value held by many rows is an aggregate
        # anyway, `value_counts` lists it).
        keep = rows >= K_MATCHES
        found = texts.to_numpy()[hit][keep]
        rows = rows[keep]
        result.update(
            {
                "total_matching_rows": int(rows.sum()) if rows.size else None,
                "distinct_matching": int(rows.size) if rows.size else None,
            }
        )
    else:
        found = texts.to_numpy()[hit]
        result.update({"total_matching_rows": int(rows.sum()), "distinct_matching": int(hit.sum())})
    order = np.argsort(-rows, kind="stable")[: args.n]
    matches = [{"value": _masked(found[i], LOOK_CELL_CHARS), "rows": int(rows[i])} for i in order]
    result["matches"] = matches
    if _fit(result, matches, keep=1):
        result["truncated"] = True
        result["shown"] = len(matches)
    return result


def _numbers_of(series: pd.Series[Any]) -> tuple[np.ndarray[Any, Any], bool, int, bool]:
    """(float values with NaN for empty or unparseable, read from text, unparseable count, sampled)."""
    if _numeric_dtype(series):
        return _floats(series), False, 0, False
    source, sampled = _parse_source(series)
    present = source.dropna().astype(str)
    present = present[present.str.strip() != ""]
    style = _decimal_style(present) if len(present) else "."
    parsed = parse_numbers(source, decimal=style or ".")
    return np.asarray(parsed.values.to_numpy(dtype="float64")), True, parsed.failed, sampled


def _exact_mean(values: np.ndarray[Any, Any]) -> float:
    """The mean without the rounding a float sum makes when huge and small values meet (`math.fsum`)."""
    try:
        return math.fsum(values.tolist()) / values.size
    except OverflowError:  # a sum beyond the float range: the plain mean is the best there is
        return float(values.mean())


def _describe_numbers(ctx: AgentContext, args: ColumnArgs) -> dict[str, Any]:
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    if _personal(ctx, name):
        return _personal_refusal(ctx, name)
    rows, empty = len(series), int(_empty_mask(series).sum())
    values, from_text, unparseable, sampled = (
        (np.array([]), False, 0, False) if pd.api.types.is_bool_dtype(series) else _numbers_of(series)
    )
    checked = int(values.size)
    finite = values[np.isfinite(values)]
    result: dict[str, Any] = {
        "column": _shown(name),
        "rows": rows,
        "checked": checked,
        "sampled": sampled,
        "empty": empty,
        "from_text": from_text,
        "numbers": int(finite.size),
        "unparseable": unparseable,
        "infinite": int(np.isinf(values).sum()),  # numbers + empty + unparseable + infinite = rows
    }
    if finite.size == 0 or (from_text and finite.size / (finite.size + unparseable) < MIN_CONVERT_SHARE):
        result.update(
            {
                "numeric": False,
                "message": "Fewer than half of the values in this column are numbers, so no statistics are given.",
            }
        )
        return result
    q = np.quantile(finite, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    q1, q3 = float(q[2]), float(q[4])
    spread = q3 - q1
    low, high = int((finite < q1 - 1.5 * spread).sum()), int((finite > q3 + 1.5 * spread).sum())
    lo, hi = float(finite.min()), float(finite.max())
    whole = bool(np.all(finite == np.floor(finite)))
    if lo == hi:
        histogram = [{"from": _round(lo), "to": _round(hi), "rows": int(finite.size)}]
    else:
        bins = int(hi - lo) + 1 if whole and hi - lo < 10 else 10
        counts, edges = np.histogram(finite, bins=bins, range=(lo, hi))
        histogram = [
            {"from": _round(edges[i]), "to": _round(edges[i + 1]), "rows": int(counts[i])}
            for i in range(len(counts))
        ]
    result.update(
        {
            "numeric": True,
            "minimum": _round(lo),
            "maximum": _round(hi),
            "mean": _round(_exact_mean(finite)),
            "median": _round(float(q[3])),
            "quantiles": {
                f"p{p}": _round(float(v)) for p, v in zip((1, 5, 25, 50, 75, 95, 99), q, strict=True)
            },
            "share_negative": _share(int((finite < 0).sum()), checked),
            "share_zero": _share(int((finite == 0).sum()), checked),
            "share_empty": _share(int(np.isnan(values).sum()) - unparseable, checked),
            "outliers": {"low": low, "high": high, "count": low + high},
            "histogram": histogram,
            "whole_numbers": whole,
        }
    )
    return result


_SHAPE_LETTERS: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]")


def _shape(text: str) -> str:
    """How a value is written: digits become 9, capitals A, lower-case letters a (`99/99/9999`, `9 Aaa 9999`)."""
    out = re.sub(r"\d", "9", text.strip())
    out = _SHAPE_LETTERS.sub(lambda m: "A" if m.group(0).isupper() else "a", out)
    return out[:30]


def _describe_dates(ctx: AgentContext, args: ColumnArgs) -> dict[str, Any]:
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    if _personal(ctx, name):
        return _personal_refusal(ctx, name)
    result: dict[str, Any] = {
        "column": _shown(name),
        "rows": len(series),
        "empty": int(_empty_mask(series).sum()),
    }
    day_first: bool | None = None
    ambiguous = False
    shapes: list[dict[str, Any]] = []
    sampled = False
    unparseable = 0
    if pd.api.types.is_datetime64_any_dtype(series):
        stamps = series.dropna()
        if getattr(stamps.dt, "tz", None) is not None:
            stamps = stamps.dt.tz_localize(None)
        checked = len(series)
    else:
        source, sampled = _parse_source(series)
        cells = source.dropna().astype(str)
        cells = cells[cells.str.strip() != ""]
        counts = cells.value_counts()
        by_shape: dict[str, int] = {}
        for text, n in counts.items():
            key = _shape(str(text))
            by_shape[key] = by_shape.get(key, 0) + int(n)
        ranked = sorted(by_shape.items(), key=lambda item: (-item[1], item[0]))[:5]
        shapes = [{"shape": _masked(shape, 30), "rows": n} for shape, n in ranked]
        spellings = pd.Series(
            counts.index.astype(str), dtype=object
        )  # the answers below are ORs over spellings
        day_first = date_order(spellings)
        ambiguous = day_first is None and bool(spellings.str.match(_NUMERIC_DAY_MONTH.pattern).any())
        parsed = parse_dates(source, dayfirst=bool(day_first))
        stamps = parsed.values.dropna()
        unparseable = parsed.failed
        checked = len(source)
    non_empty = len(stamps) + unparseable
    result.update(
        {
            "checked": checked,
            "sampled": sampled,
            "dates": len(stamps),
            "unparseable": unparseable,
            "shapes": shapes,
        }
    )
    if len(stamps) == 0 or len(stamps) / max(1, non_empty) < MIN_CONVERT_SHARE:
        result.update(
            {
                "dates_found": False,
                "message": "Fewer than half of the values in this column are dates, so no date statistics are given.",
            }
        )
        return result
    days = stamps.dt.normalize()
    distinct = pd.DatetimeIndex(np.sort(days.unique()))
    gap = int(np.diff(distinct.to_numpy()).max() / np.timedelta64(1, "D")) if len(distinct) > 1 else 0
    result.update(
        {
            "dates_found": True,
            "earliest": stamps.min().date().isoformat(),
            "latest": stamps.max().date().isoformat(),
            "distinct_days": len(distinct),
            "distinct_months": len(set(zip(distinct.year, distinct.month, strict=True))),
            "longest_gap_days": gap,
            "share_with_time": _share(int((stamps != days).sum()), len(stamps)),
            "day_first": day_first,
            "order_ambiguous": ambiguous,
        }
    )
    return result


def _numeric_view(series: pd.Series[Any], other_is_number: bool) -> np.ndarray[Any, Any] | None:
    """`series` as floats when it is a number column, or a text column that holds numbers next to one."""
    if _numeric_dtype(series):
        return _floats(series)
    if other_is_number and pd.api.types.is_object_dtype(series):
        values = _floats(series)
        present = int(series.notna().sum())
        if present and int(np.isfinite(values).sum()) / present >= 0.95:
            return values
    return None


def _compare_columns(ctx: AgentContext, args: CompareArgs) -> dict[str, Any]:
    left, right = ctx.resolve(args.left), ctx.resolve(args.right)
    if left == right:
        raise AgentToolError("AGENT_TOOL_ARGS_INVALID", "compare_columns: pick two different columns.")
    kinds = sorted({*_kinds(ctx, left), *_kinds(ctx, right)})
    if _personal(ctx, left) or _personal(ctx, right):
        return {
            "columns": [_shown(left), _shown(right)],
            "rows": len(ctx.frame),
            "personal_data": kinds,
            "message": _withheld_message(ctx, left, right),
        }
    ls, rs = ctx.frame[left], ctx.frame[right]
    rows = len(ls)
    lp, rp = ~_empty_mask(ls), ~_empty_mask(rs)
    both = lp & rp
    both_n = int(both.sum())
    lx = _numeric_view(ls, _numeric_dtype(rs))
    rx = _numeric_view(rs, _numeric_dtype(ls))
    numeric = lx is not None and rx is not None
    if lx is not None and rx is not None:
        equal = int(np.sum(both & (lx == rx)))
    else:
        equal = int(np.sum(ls.astype(object).to_numpy()[both] == rs.astype(object).to_numpy()[both]))
    only_left, only_right = int((lp & ~rp).sum()), int((rp & ~lp).sum())
    result: dict[str, Any] = {
        "columns": [_shown(left), _shown(right)],
        "rows": rows,
        "equal_share": _share(equal, rows),
        "equal_of_both_present_share": _share(equal, both_n),
        "both_empty_share": _share(int((~lp & ~rp).sum()), rows),
        "only_left_empty_share": _share(only_left, rows),
        "only_right_empty_share": _share(only_right, rows),
    }
    same_empties = only_left == 0 and only_right == 0
    identical = both_n > 0 and equal == both_n and same_empties
    kind = "identical" if identical else "unrelated"
    if numeric and lx is not None and rx is not None:
        ok = both & np.isfinite(lx) & np.isfinite(rx)
        x, y = lx[ok], rx[ok]
        result["pairs"] = int(x.size)
        pearson = spearman = None
        if x.size >= 2 and x.std() > 0 and y.std() > 0:
            pearson = float(np.corrcoef(x, y)[0, 1])
            spearman = float(pd.Series(x).rank().corr(pd.Series(y).rank()))
        result["pearson"], result["spearman"] = _round(pearson), _round(spearman)
        if (
            not identical and x.size >= 2 and x.std() > 0 and y.std() > 0
        ):  # a constant is derived from nothing
            slope = float(((x - x.mean()) * (y - y.mean())).mean() / x.var())
            intercept = float(y.mean() - slope * x.mean())
            tolerance = 1e-9 * max(1.0, float(np.abs(y).max()))
            if float(np.abs(y - (slope * x + intercept)).max()) <= tolerance:
                exact_multiple = abs(intercept) <= tolerance
                kind = (
                    "constant_multiple"
                    if exact_multiple
                    else "offset" if abs(slope - 1.0) <= 1e-9 else "linear"
                )
                result["slope"], result["intercept"] = _round(slope), _round(intercept)
        if kind == "unrelated" and any(v is not None and abs(v) >= 0.9 for v in (pearson, spearman)):
            kind = "correlated"
    few_values = (
        lx is not None
        and rx is not None
        and len(np.unique(lx[both])) <= MAX_BUCKETS
        and len(np.unique(rx[both])) <= MAX_BUCKETS
    )
    if not numeric or few_values:
        table, sampled = _crosstab(ls, rs, both)
        result.update(table)
        result["sampled"] = sampled
        if _fit(result, table["crosstab"], keep=1):
            result["truncated"] = True
            result["shown"] = len(table["crosstab"])
        if not numeric and kind == "unrelated":
            forward, backward = table["right_given_left_share"], table["left_given_right_share"]
            kind = (
                "one_to_one"
                if forward >= 0.999 and backward >= 0.999
                else (
                    "left_determines_right"
                    if forward >= 0.999
                    else "right_determines_left" if backward >= 0.999 else "not_derived"
                )
            )
    result["kind"] = kind
    return result


def _crosstab(
    ls: pd.Series[Any], rs: pd.Series[Any], both: np.ndarray[Any, Any]
) -> tuple[dict[str, Any], bool]:
    """The ten most common (left, right) pairs and how well each column predicts the other."""
    positions, sampled = _spread(np.flatnonzero(both), LOOK_SAMPLE_ROWS)
    lcodes, luniq = pd.factorize(ls.astype(object).to_numpy()[positions])
    rcodes, runiq = pd.factorize(rs.astype(object).to_numpy()[positions])
    width = max(1, len(runiq))
    pairs, counts = np.unique(lcodes.astype(np.int64) * width + rcodes, return_counts=True)
    left_code, right_code = pairs // width, pairs % width
    total = int(counts.sum())

    def predicted(group: np.ndarray[Any, Any]) -> float:
        best = pd.Series(counts).groupby(group).max().sum()
        return _share(int(best), total)

    order = np.argsort(-counts, kind="stable")[:10]
    return (
        {
            "crosstab": [
                {
                    "left_value": _masked(luniq[left_code[i]], LOOK_CELL_CHARS),
                    "right_value": _masked(runiq[right_code[i]], LOOK_CELL_CHARS),
                    "rows": int(counts[i]),
                }
                for i in order
            ],
            "distinct_pairs": len(pairs),
            "left_distinct": len(luniq),
            "right_distinct": len(runiq),
            "right_given_left_share": predicted(left_code),
            "left_given_right_share": predicted(right_code),
        },
        sampled,
    )


MAX_COMISSING_COLUMNS: Final[int] = 60
MAX_MISSING_LISTED: Final[int] = 30


def _describe_missing(ctx: AgentContext, args: MissingArgs) -> dict[str, Any]:
    frame = ctx.frame
    names = (
        list(dict.fromkeys(ctx.resolve(c) for c in args.columns))
        if args.columns
        else [str(c) for c in frame.columns]
    )
    rows = len(frame)
    counts = {name: int(_empty_mask(frame[name]).sum()) for name in names}
    gappy = sorted(
        (n for n in names if counts[n] > 0), key=lambda n: -counts[n]
    )  # stable: file order on ties
    result: dict[str, Any] = {
        "rows": rows,
        "columns_total": len(names),
        "columns_with_missing": len(gappy),
        "always_empty": {
            "columns": [_shown(n) for n in names if rows and counts[n] == rows][:MAX_MISSING_LISTED]
        },
        "missing": [
            {"column": _shown(n), "empty": counts[n], "share": _share(counts[n], rows)}
            for n in gappy[:MAX_MISSING_LISTED]
        ],
    }
    candidates = [n for n in gappy if counts[n] < rows][:MAX_COMISSING_COLUMNS]
    sample, sampled = _spread(frame[candidates], LOOK_SAMPLE_ROWS)
    patterns: dict[bytes, list[str]] = {}
    if candidates:
        gaps = {c: _empty_mask(sample[c]) for c in candidates}
        for name in candidates:
            patterns.setdefault(np.packbits(gaps[name]).tobytes(), []).append(name)
    together = sorted(
        (cols for cols in patterns.values() if len(cols) > 1),
        key=lambda cols: (-counts[cols[0]], names.index(cols[0])),
    )
    result["groups"] = [
        {"columns": [_shown(c) for c in cols[:12]], "rows": counts[cols[0]]} for cols in together[:8]
    ]
    result["sampled"] = sampled
    if ctx.target is not None and ctx.target in frame.columns and gappy:
        result["outcome"] = _missing_vs_outcome(
            ctx, [n for n in gappy if n != ctx.target and counts[n] < rows][:5]
        )
    # Least useful first: the co-missing groups, then the always-empty names, then the emptiest columns.
    if _fit(result, result["groups"], result["always_empty"]["columns"], result["missing"]):
        result["truncated"] = True
    return result


def _missing_vs_outcome(ctx: AgentContext, names: list[str]) -> dict[str, Any] | None:
    """Positive rate among rows where each column is empty and where it is filled (the outcome by the checks' own rule)."""
    positive, label = _positive_mask(ctx, str(ctx.target))
    if positive is None:
        return None
    known = positive.notna().to_numpy()
    yes = positive[known].astype(bool).to_numpy()
    by_column: list[dict[str, Any]] = []
    for name in names:
        gap = _empty_mask(ctx.frame[name])[known]
        by_column.append(
            {
                "column": _shown(name),
                "empty_rows": int(gap.sum()),
                "positive_rate_when_empty": _round(float(yes[gap].mean())) if gap.any() else None,
                "positive_rate_when_filled": _round(float(yes[~gap].mean())) if (~gap).any() else None,
            }
        )
    return {
        "column": _shown(ctx.target),
        "positive_label": label,
        "overall_positive_rate": _round(float(yes.mean())) if yes.size else None,
        "by_column": by_column,
    }


def _describe_duplicates(ctx: AgentContext, args: DuplicatesArgs) -> dict[str, Any]:
    frame = ctx.frame
    keys = list(dict.fromkeys(ctx.resolve(c) for c in args.columns)) if args.columns else None
    subset = keys if keys is not None else [str(c) for c in frame.columns]
    rows = len(frame)
    present = np.any([~_empty_mask(frame[c]) for c in subset], axis=0)
    positions = np.flatnonzero(present)
    keyed = _zeroed(frame[subset].iloc[positions])
    ids, _ = pd.factorize(pd.util.hash_pandas_object(keyed, index=False).to_numpy())
    sizes = np.bincount(ids) if len(ids) else np.zeros(0, dtype=np.int64)
    repeated = sizes > 1
    shown = subset[:8]
    result: dict[str, Any] = {
        "columns": [_shown(c) for c in shown],
        "whole_row": keys is None,
        "rows": rows,
        "empty_key_rows": int(rows - len(positions)),
        "duplicate_rows": int((sizes - 1).sum()) if sizes.size else 0,
        "share": _share(int((sizes - 1).sum()) if sizes.size else 0, rows),
        "repeated_keys": int(repeated.sum()),
        "rows_in_repeated_keys": int(sizes[repeated].sum()),
        "largest_group": int(sizes.max()) if sizes.size else 0,
    }
    order = [int(g) for g in np.argsort(-sizes, kind="stable")[:5] if sizes[g] > 1]
    first = np.unique(ids, return_index=True)[1] if len(ids) else np.zeros(0, dtype=np.int64)
    personal = {name for name in shown if _personal(ctx, name)}
    top: list[dict[str, Any]] = []
    for group in order:
        at = int(first[group])
        cells = [
            _withheld_cell(ctx, name) if name in personal else _cell(keyed[name].iloc[at], 40)
            for name in shown
        ]
        top.append({"cells": cells, "rows": int(sizes[group])})
    result["top_keys"] = top
    others = [str(c) for c in frame.columns if str(c) not in subset]
    differing = 0
    checked_groups = int(repeated.sum())
    if others and repeated.any():
        # Compare the other columns on at most LOOK_SAMPLE_ROWS rows of whole groups, first groups first.
        groups = np.flatnonzero(repeated)
        groups = groups[np.cumsum(sizes[groups]) <= LOOK_SAMPLE_ROWS] if len(groups) > 1 else groups
        checked_groups = len(groups)
        chosen = np.zeros(len(sizes), dtype=bool)
        chosen[groups] = True
        in_group = chosen[ids]
        rest = _zeroed(frame[others].iloc[positions[in_group]])
        fingerprint = pd.util.hash_pandas_object(rest, index=False).to_numpy()
        differing = int((pd.Series(fingerprint).groupby(ids[in_group]).nunique() > 1).sum())
    result["groups_checked"] = checked_groups
    result["other_columns"] = len(others)
    result["groups_differing_in_other_columns"] = differing
    if _fit(result, top):
        result["truncated"] = True
    return result


TOOLS: Final[Mapping[str, Tool]] = {
    tool.name: tool
    for tool in (
        Tool(
            "get_profile",
            "Rows, columns, types, empty shares and detected roles of the file (50 columns per call; `offset` pages on).",
            ToolKind.READ,
            ProfileArgs,
            _get_profile,
        ),
        Tool(
            "inspect_column",
            "One column in detail: examples, top values and how the outcome rate changes across its values.",
            ToolKind.READ,
            ColumnArgs,
            _inspect_column,
        ),
        Tool(
            "find_format_issues",
            "Text columns holding numbers, dates or yes/no values, or one category spelled several ways; "
            "number columns holding a placeholder code (99, 999, 9999, -1, -99, -999) far outside every other value.",
            ToolKind.READ,
            FormatArgs,
            _find_format_issues,
        ),
        Tool(
            "describe_placeholder_values",
            "What treating some numbers of a number column as missing would change (the placeholder codes "
            "find_format_issues found, or the values given): rows affected and their share, the column's mean "
            "and median before and after and, when the outcome is known and has two values, the outcome rate on "
            "those rows against the rest and the column's single-column AUC before and after. No model is "
            "trained. Refused for personal-data columns.",
            ToolKind.READ,
            PlaceholderArgs,
            _describe_placeholder_values,
        ),
        Tool(
            "describe_outcome",
            "The outcome column: how many rows are 'yes', by the same rule the checks use.",
            ToolKind.READ,
            ColumnArgs,
            _describe_outcome,
        ),
        Tool(
            "find_roles",
            "Which columns could be the ID, the outcome, the date, consent, opt-out and last contact.",
            ToolKind.READ,
            NoArgs,
            _find_roles,
        ),
        Tool(
            "describe_repeats",
            "How many rows each value of an ID column has: whether the file holds several rows per customer.",
            ToolKind.READ,
            ColumnArgs,
            _describe_repeats,
        ),
        Tool(
            "sample_rows",
            "Look at real rows: up to 20 rows of up to 12 columns, from a row offset, optionally only the rows "
            "whose value in one column is exactly a given value. Empty cells show as null; personal-data columns "
            "show '[personal data]'. Gives total_matching and truncated so you know how much more there is.",
            ToolKind.READ,
            SampleRowsArgs,
            _sample_rows,
        ),
        Tool(
            "value_counts",
            "The most common values of one column (up to 50) with their row counts and share of all rows, plus the "
            "distinct and empty counts. Use it to see how a category is really spelled. Personal-data columns give counts only.",
            ToolKind.READ,
            ValueCountsArgs,
            _value_counts,
        ),
        Tool(
            "find_values",
            "Search one column for values containing some plain text (case ignored, never a pattern): the matching "
            "values with their row counts, and how many rows match in all. Personal-data columns give the count only.",
            ToolKind.READ,
            FindValuesArgs,
            _find_values,
        ),
        Tool(
            "describe_numbers",
            "A number column (or text that mostly holds numbers): minimum, maximum, mean, median, the 1, 5, 25, 50, 75, "
            "95 and 99 percent points, shares negative, zero and empty, outliers by the IQR rule, a 10-bucket "
            "histogram, and whether every value is a whole number. Refused for personal-data columns.",
            ToolKind.READ,
            ColumnArgs,
            _describe_numbers,
        ),
        Tool(
            "describe_dates",
            "A date column (or text that mostly holds dates): earliest and latest date, distinct days and months, the "
            "longest gap in days, share with a time part, whether day-first or month-first is ambiguous, how many "
            "values are not dates, and the most common written shapes (digits shown as 9). Refused for personal-data columns.",
            ToolKind.READ,
            ColumnArgs,
            _describe_dates,
        ),
        Tool(
            "compare_columns",
            "Two columns side by side, to spot a duplicate or a leak: share of rows equal, share both empty, whether one "
            "is derived from the other (identical, one_to_one, left_determines_right, constant_multiple, offset), a "
            "top-10 cross-tab for categories, and Pearson and Spearman correlation for numbers. Refused for personal-data columns.",
            ToolKind.READ,
            CompareArgs,
            _compare_columns,
        ),
        Tool(
            "describe_missing",
            "Empty share for each column (the 30 most empty), sets of columns that are empty on the same rows, and, "
            "when the outcome is known, how the outcome rate differs between rows where a column is empty and where it is filled.",
            ToolKind.READ,
            MissingArgs,
            _describe_missing,
        ),
        Tool(
            "describe_duplicates",
            "Duplicate rows for a key of 1 to 5 columns (or the whole row when no column is given): how many rows repeat, "
            "the share, the 5 most repeated keys, and how many repeated keys differ in their other columns.",
            ToolKind.READ,
            DuplicatesArgs,
            _describe_duplicates,
        ),
        Tool(
            "check_data",
            "Run every data check the Run button would, for a chosen ID, outcome and settings.",
            ToolKind.READ,
            CheckArgs,
            _check_data,
        ),
    )
}


def call_tool(
    ctx: AgentContext, name: str, args: Mapping[str, Any] | None, *, evidence_id: str
) -> ToolResult:
    """Validate, run and record one tool call. Raises `AgentToolError` on refusal."""
    tool = TOOLS.get(name)
    if tool is None:
        raise AgentToolError("AGENT_TOOL_UNKNOWN", f"There is no tool called {name!r}.")
    try:
        parsed = tool.args_model.model_validate(dict(args or {}))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "arguments"
        raise AgentToolError("AGENT_TOOL_ARGS_INVALID", f"{name}: {where}: {first['msg']}") from exc
    result = _jsonable(tool.run(ctx, parsed))
    return ToolResult(
        evidence_id=evidence_id,
        tool=name,
        args=_jsonable(parsed.model_dump(exclude_none=True)),
        supplied=_jsonable(parsed.model_dump(exclude_none=True, exclude_unset=True)),
        result=result,
        created_at=ctx.clock(),
    )
