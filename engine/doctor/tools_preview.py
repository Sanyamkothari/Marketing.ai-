"""`preview_working_copy` (catalogue #85): the agent looks at its own work.

The agent fixes a file on a working copy (the ticked steps and the ones it applied itself); this READ tool shows it the
first rows of the file before and after, and which columns changed, so it can see what its fixes did before it says so.
Nothing is changed: the rows are the plan's trial run on at most 1,000 rows (`engine.doctor.preview`).

Egress, as the other row-level tools: a personal column, a column the settings hide and a long free-text column show
`[personal data]` and no value; every other cell is masked whole and cut; in `summaries_only` no cell is shown at
all, only the columns and which of them the plan changes. A column name is shown as `display_name` makes it. The
result stays under the row-level budget: fewer rows, then fewer columns, and it says how many it left out.
Session-only (never listed on MCP), for the AI and the screen's own calls; the person sees the same rows on the
screen (`view.preview`).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.config import DataAccess
from engine.agent.untrusted import display_name
from engine.doctor.preview import PREVIEW_ROWS, ExactModel, PreviewFile, preview_rows
from engine.doctor.registry import (
    ROW_LEVEL_BUDGET,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
    output_size,
)
from engine.utils.logging import get_logger

__all__ = [
    "M_PREVIEW_TOOLS",
    "PreviewWorkingCopyArgs",
    "WorkingCopyFile",
    "WorkingCopyPreview",
    "WorkingCopyTable",
    "register_preview_tools",
]

_LOG = get_logger(__name__)

M_PREVIEW_TOOLS: Final[tuple[str, ...]] = ("preview_working_copy",)
DEFAULT_ROWS: Final[int] = 5
DEFAULT_COLUMNS: Final[int] = 12
"""Columns shown when the agent names none: the ones the plan changed first, then the first of the rest."""
_MARGIN: Final[int] = 300
"""Characters kept free under the budget: the envelope the registry adds around the rows."""
_Table = tuple[tuple[str, ...], ...]


class PreviewWorkingCopyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    rows: int = Field(default=DEFAULT_ROWS, ge=1, le=PREVIEW_ROWS, description="How many rows to show.")
    columns: tuple[str, ...] = Field(
        default=(), max_length=40, description="Columns to show; none given: the changed ones first."
    )


class WorkingCopyTable(ExactModel):
    """Rows of cells, one per column in `columns`: the shape the egress gate reads (`columns` + `rows`), so a hidden
    column hides its own cells."""

    columns: tuple[str, ...]
    rows: _Table


class WorkingCopyFile(ExactModel):
    file: str = Field(description="F1, F2, ...: the file, in the order the case lists them.")
    total_rows: int
    changed_columns: tuple[str, ...] = Field(
        description="The columns the plan adds, removes or rewrites values in."
    )
    before: WorkingCopyTable = Field(description="The first rows as they are in the file.")
    after: WorkingCopyTable = Field(description="The first rows after the plan, same columns, same order.")
    left_out_columns: int = Field(
        default=0, description="Columns not shown (name them in `columns` to see them)."
    )
    note: str | None = None


class WorkingCopyPreview(ExactModel):
    files: tuple[WorkingCopyFile, ...]
    shows_values: bool = Field(
        description="False when the settings let the AI see summaries only: no cell is shown."
    )


def _pick(file: PreviewFile, wanted: tuple[str, ...]) -> list[int]:
    """The positions of the columns to show: the ones asked for, else the changed ones and then the first of the rest
    (at most 12), in the file's order."""
    names = [display_name(c.name) for c in file.columns]
    if wanted:
        unknown = [w for w in wanted if w not in names]
        if unknown:
            raise DataToolError(
                "TOOL_INPUT_INVALID", f"The file has no column {', '.join(unknown)[:120]!r}.", status=422
            )
        return [names.index(w) for w in dict.fromkeys(wanted)]
    order = sorted(range(len(names)), key=lambda i: (not file.columns[i].changed, i))
    return sorted(order[:DEFAULT_COLUMNS])


def _shaped(file: PreviewFile, label: str, picked: list[int], rows: int, values: bool) -> WorkingCopyFile:
    names = tuple(display_name(file.columns[i].name) for i in picked)

    def table(shown: _Table) -> WorkingCopyTable:
        cells = tuple(tuple(row[i] for i in picked) for row in shown[:rows]) if values else ()
        return WorkingCopyTable(columns=names, rows=cells)

    return WorkingCopyFile(
        file=label,
        total_rows=file.total_rows,
        changed_columns=tuple(display_name(c.name) for c in file.columns if c.changed)[:40],
        before=table(file.before),
        after=table(file.after),
        left_out_columns=file.left_out_columns + len(file.columns) - len(picked),
        note=file.note,
    )


def _preview(given: PreviewWorkingCopyArgs, ctx: DataToolContext) -> WorkingCopyPreview:
    from engine.privacy.sensitive import find_long_text

    agent, files, items = ctx.agent, ctx.files, ctx.case_items or ()
    only = (
        agent.upload_id if agent is not None and any(f.upload_id == agent.upload_id for f in files) else None
    )
    hidden = {str(c) for c in agent.config.agent.always_hide_columns} if agent is not None else set()
    for loaded in files:
        if only is None or loaded.upload_id == only:
            hidden |= {found.column for found in find_long_text(loaded.frame.head(1_000))}
    values = agent is None or agent.config.agent.ai_data_access is not DataAccess.SUMMARIES_ONLY
    try:
        view = preview_rows(
            files,
            items,
            as_of=ctx.as_of,
            training_as_of=ctx.training_as_of,
            hide=hidden,
            rows=given.rows,
            only=only,
        )
    except Exception as exc:  # a refusal the AI can recover from, never the end of its whole review
        _LOG.warning("preview_working_copy_failed", exc_info=True)
        raise DataToolError(
            "PREVIEW_UNAVAILABLE", "The working copy could not be tried on a sample just now.", status=409
        ) from exc
    labels = {f.upload_id: f"F{n}" for n, f in enumerate(files, start=1)}
    shaped = [
        _shaped(file, labels[file.upload_id], _pick(file, given.columns), given.rows, values)
        for file in view.files
    ]
    result = WorkingCopyPreview(files=tuple(shaped), shows_values=values)
    # The row-level budget (the registry refuses beyond it): fewer rows first, then fewer columns.
    rows = given.rows
    while output_size(Exposure.SESSION_ONLY, result.model_dump(mode="json")) > ROW_LEVEL_BUDGET - _MARGIN:
        if rows > 1:
            rows -= 1
        elif any(len(f.before.columns) > 1 for f in shaped) and not given.columns:
            shaped = [_fewer(f) for f in shaped]
        else:
            break
        shaped = [_rows_cut(f, rows) for f in shaped]
        result = WorkingCopyPreview(files=tuple(shaped), shows_values=values)
    return result


def _rows_cut(file: WorkingCopyFile, rows: int) -> WorkingCopyFile:
    return file.model_copy(
        update={
            "before": file.before.model_copy(update={"rows": file.before.rows[:rows]}),
            "after": file.after.model_copy(update={"rows": file.after.rows[:rows]}),
        }
    )


def _fewer(file: WorkingCopyFile) -> WorkingCopyFile:
    """One column fewer (the last shown), the rows cut to match."""
    keep = max(1, len(file.before.columns) - 1)

    def narrow(table: WorkingCopyTable) -> WorkingCopyTable:
        return WorkingCopyTable(columns=table.columns[:keep], rows=tuple(row[:keep] for row in table.rows))

    return file.model_copy(
        update={
            "before": narrow(file.before),
            "after": narrow(file.after),
            "left_out_columns": file.left_out_columns + len(file.before.columns) - keep,
        }
    )


def register_preview_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register `preview_working_copy` (catalogue #85), for the AI; its names."""
    registry.register(
        DataTool(
            number=85,
            name="preview_working_copy",
            kind=DataToolKind.READ,
            description=(
                "Look at your own work: the file's first rows as they are and as the plan so far leaves them "
                "(the fixes ticked and the ones you applied), and which columns changed. Personal data is "
                "withheld. Changes nothing."
            ),
            input_model=PreviewWorkingCopyArgs,
            output_model=WorkingCopyPreview,
            route_policy=RoutePolicy(
                role=Role.ANALYST,
                action="doctor.preview_working_copy",
                purpose="see the first rows of the working copy",
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_preview,
            # The catalogue lists the AI (#85, mode "ai"); "person" stays so the eval world and the canary, which call
            # every tool as a person, can run it. No HTTP route dispatches it, and `case_items` is the plan only.
            callers=frozenset({"ai", "person"}),  # never the rules: they have no use for it
            needs=("files", "case_items"),
            wraps="engine/doctor/preview.py preview_rows (the plan_check trial run on 1,000 rows)",
        )
    )
    return M_PREVIEW_TOOLS
