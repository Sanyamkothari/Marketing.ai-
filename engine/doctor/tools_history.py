"""`check_history` (catalogue #87): is the dated history long enough for the goal?

A READ tool for the rules and the AI. It reads one date column of one file and says, in plain words, whether the history
holds a look-back before the training snapshot and a full outcome window after it, and whether a history under a year
is mostly one season (`engine.doctor.history`). Nothing is changed. The same warnings are shown on the plan before
approval (`VERIFY`), so the person reads them whether or not the AI called the tool.

The training snapshot is the case's (`training_as_of`, else the as-of date) when the case has one. Counts and months
only, never a cell value.
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.untrusted import display_name
from engine.config import StrictBase
from engine.doctor.history import HISTORY_CODES, read_history
from engine.doctor.registry import (
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)
from engine.doctor.tools import FileArgs, _file_or_files, _loaded

__all__ = ["M_HISTORY_TOOLS", "HistoryArgs", "HistoryCheck", "register_history_tools"]

M_HISTORY_TOOLS: Final[tuple[str, ...]] = ("check_history",)


class HistoryArgs(FileArgs):
    column: str = Field(
        min_length=1, max_length=256, description="The column that dates each row (the event date)."
    )
    outcome_window_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="Days the outcome looks ahead; without it only the one-season check runs.",
    )
    lookback_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="Days of history features read; the outcome window when omitted.",
    )


class HistoryWarning(StrictBase):
    code: str = Field(description="One of " + ", ".join(HISTORY_CODES) + ".")
    text: str = Field(description="The warning in plain words, with counts.")


class HistoryCheck(StrictBase):
    upload_id: str
    column: str
    history_days: int = Field(description="Days from the first date to the last.")
    months: int = Field(description="Calendar months the dates touch.")
    needed_days: int | None = Field(description="Look-back plus outcome window; null without a window.")
    enough: bool = Field(description="True when there is nothing to warn about.")
    warnings: tuple[HistoryWarning, ...] = ()
    sentence: str = Field(description="What to tell the person, in one or two plain sentences.")


def _check_history(given: HistoryArgs, ctx: DataToolContext) -> HistoryCheck:
    loaded = _loaded(ctx, given.upload_id)
    if given.column not in loaded.frame.columns:
        raise DataToolError("TOOL_INPUT_INVALID", "The file has no such column.", status=422)
    reading = read_history(
        loaded.frame[given.column],
        outcome_window_days=given.outcome_window_days,
        training_as_of=ctx.training_as_of or ctx.as_of,
        lookback_days=given.lookback_days,
    )
    if reading is None:
        raise DataToolError("TOOL_INPUT_INVALID", "That column does not read as dates.", status=422)
    warnings = tuple(HistoryWarning(code=f.code, text=f.text) for f in reading.findings)
    return HistoryCheck(
        upload_id=loaded.upload_id,
        column=display_name(given.column),
        history_days=reading.history_days,
        months=reading.months,
        needed_days=reading.needed_days,
        enough=not warnings,
        warnings=warnings,
        sentence=" ".join(w.text for w in warnings) or "The history is long enough for this goal.",
    )


def register_history_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `check_history` (catalogue #87), for the rules and the AI; its names."""
    registry.register(
        DataTool(
            number=87,
            name="check_history",
            kind=DataToolKind.READ,
            description=(
                "Check that the dated history is long enough for the goal: room for a look-back before the "
                "training date and a full outcome window after it, and a history under a year that is mostly one "
                "season. Name the date column and the outcome window in days. Changes nothing."
            ),
            input_model=HistoryArgs,
            output_model=HistoryCheck,
            route_policy=RoutePolicy(
                role=Role.ANALYST, action="doctor.check_history", purpose="check the length of the history"
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_check_history,
            callers=frozenset({"rules", "ai", "person"}),
            available=_file_or_files,
            wraps="engine/doctor/history.py read_history (extends OUTCOME_WINDOW_PAST_DATA and X_LABEL_MATURITY)",
        )
    )
    return M_HISTORY_TOOLS
