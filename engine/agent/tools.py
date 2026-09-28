"""The helper's tools (Plan G §8.3): each wraps a system the platform already has.

A tool takes validated arguments and an `AgentContext` and returns JSON. `call_tool` is the only way
in: it refuses an unknown tool or bad arguments with a code, runs the tool, and wraps the result in a
`ToolResult` with an evidence id that proposals cite (DEC-1010). **No tool writes data**; the only
writer is `apply` (DEC-1002).

M71 ships the read tools. Values that leave a tool are either counts the engine measured or cells
that passed `engine.pii.redact_cells`; a column the profile marked as personal data never shows a
value at all.
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
from typing import Any, Final

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from engine.agent.checks import check_plan
from engine.agent.contracts import ToolResult
from engine.agent.formats import find_format_issues
from engine.agent.untrusted import MAX_PROMPT_LIST, resolve_column
from engine.config import ConfigError, PrimaryKey, RunMode, UseCaseConfig, resolve_config
from engine.contracts import DatasetProfile, FeatureSchema
from engine.pii import redact_cells
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

MAX_BUCKETS: Final[int] = 8
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
            raise AgentToolError("AGENT_COLUMN_UNKNOWN", f"There is no column {name[:80]!r} in this file.")
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


class CheckArgs(_Args):
    primary_key: str | None = Field(default=None, description="The ID column.")
    target: str | None = Field(default=None, description="The outcome column (training only).")
    overrides: dict[str, JsonValue] = Field(default_factory=dict, description="Run overrides, dotted paths.")


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
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return round(float(value), 6)


def _column_profiles(ctx: AgentContext) -> dict[str, Any]:
    return {column.name: column for column in ctx.profile.columns}


def _personal(ctx: AgentContext, name: str) -> bool:
    column = _column_profiles(ctx).get(name)
    return bool(column is not None and column.pii_kinds)


def _positive_mask(ctx: AgentContext, target: str) -> tuple[pd.Series[Any] | None, str | None]:
    """A boolean series for "outcome is positive", by the engine's own label rule, or None."""
    if target not in ctx.frame.columns:
        return None, None
    params = validate.CheckParams(target=target, positive_label=ctx.config.target.positive_label)
    label, positives, negatives = validate.resolve_positive_label(ctx.frame, params)
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
                    "bucket": redact_cells([str(key)[:60]])[0],
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
    if column is not None and not personal:
        # The smallest and largest value of a personal column are somebody's value.
        result.update(
            {
                "type": column.inferred_type.value,
                "minimum": _clean_float(column.minimum),
                "maximum": _clean_float(column.maximum),
                "mean": _clean_float(column.mean),
            }
        )
    if personal:
        result["examples"] = []
        result["top_values"] = []
    else:
        present = series.dropna().astype(str)
        result["examples"] = list(redact_cells(v[:80] for v in present.drop_duplicates().head(5)))
        counts = present.value_counts().head(MAX_BUCKETS)
        result["top_values"] = [
            {"value": redact_cells([str(value)[:60]])[0], "rows": int(rows)} for value, rows in counts.items()
        ]
    result["relation_to_outcome"] = _relation_to_target(ctx, name)
    return result


def _describe_outcome(ctx: AgentContext, args: ColumnArgs) -> dict[str, Any]:
    """The outcome column by the engine's own label rule: how many rows are "yes"."""
    name = ctx.resolve(args.column)
    series = ctx.frame[name]
    params = validate.CheckParams(target=name, positive_label=ctx.config.target.positive_label)
    label, positives, negatives = validate.resolve_positive_label(ctx.frame, params)
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
    columns = [c for c in asked if not _personal(ctx, c)]
    return {
        "issues": [
            issue.as_json() for issue in find_format_issues(ctx.frame, columns=[str(c) for c in columns])
        ]
    }


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
            "Text columns holding numbers, dates or yes/no values, or one category spelled several ways.",
            ToolKind.READ,
            FormatArgs,
            _find_format_issues,
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
        result=result,
        created_at=ctx.clock(),
    )
