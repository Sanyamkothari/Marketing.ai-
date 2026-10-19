"""The data as the case's plan leaves it: the first rows of each file before and after, personal data masked.

`preview_rows(files, items)` tries the ticked steps (the person's, and the ones the agent applied by itself: both are
`ACCEPTED` items) on each file's first 1,000 rows under the guards, as `plan_check` does, and keeps the first
`PREVIEW_ROWS` rows before and after, which columns changed, and a few rows per step (`StepSample`). Nothing is
written anywhere; the frames are thrown away, so the answer holds masked text only.

Masking is one rule for every reader (the screen, the agent's `preview_working_copy` tool): a personal column shows
`[personal data]` and never its values; every other cell is masked whole (`engine.agent.formats.masked_cut`: e-mail,
phone, PAN, Aadhaar, card, IBAN, IP, URL, token, nine-digit run) before it is cut. Numbers and dates are formatted,
not run through the text masker (a float's digits would read as a phone number). A column a step makes out of a
personal one is personal too.

`GET /cases/{id}/screen` serves it as `view.preview`, counted once per plan (:func:`cached_preview`).
"""

from __future__ import annotations

import numbers
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Sequence
from typing import Any, Final

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from engine.agent.config import AgentConfig
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.dataset_recipe import consistent_types
from engine.agent.formats import masked_cut
from engine.agent.recipe import RecipeError, derive_names
from engine.agent.sql_step import SqlStepError
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.guards import run_guarded
from engine.doctor.plan_check import (
    SAMPLE_ROWS,
    accepted_column,
    case_tables,
    guard_levels,
    plan_digest,
    recipe_steps_of,
)

__all__ = [
    "PERSONAL_CELL",
    "PREVIEW_ROWS",
    "ExactModel",
    "PreviewColumn",
    "PreviewFile",
    "PreviewView",
    "StepSample",
    "cached_preview",
    "masked_rows",
    "preview_cell",
    "preview_rows",
]

PREVIEW_ROWS: Final[int] = 10
"""Rows of each file shown, before and after."""
PREVIEW_CELL_CHARS: Final[int] = 60
PERSONAL_CELL: Final[str] = "[personal data]"
STEP_SAMPLE_ROWS: Final[int] = 3
"""Changed rows shown for one step."""
_CACHE_SIZE: Final[int] = 32

Table = tuple[tuple[str, ...], ...]


class ExactModel(BaseModel):
    """Refuses unknown fields, and - unlike the project's `StrictBase` - keeps white space as written: a cell with a
    leading or trailing space is what a preview is there to show."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PreviewColumn(ExactModel):
    name: str
    changed: bool = Field(description="The plan adds, removes or rewrites values in this column.")
    personal: bool = Field(description="Its values are never shown: every cell reads `[personal data]`.")


class StepSample(ExactModel):
    """A few rows one step changed, as the plan screen reads `steps[].samples`: each row is `{column: text}`."""

    before: tuple[dict[str, str], ...] = ()
    after: tuple[dict[str, str], ...] = ()
    personal_columns: tuple[str, ...] = ()


class PreviewFile(ExactModel):
    upload_id: str
    file_name: str
    total_rows: int = Field(
        description="Rows in the whole file (the preview is tried on the first 1,000 read)."
    )
    columns: tuple[PreviewColumn, ...] = Field(
        description="The columns after the plan, then the ones it removes; `before` and `after` follow this order "
        "(an empty cell where a column does not exist on that side)."
    )
    before: Table
    after: Table
    note: str | None = Field(
        default=None,
        description="Set when a step could not run on the sample: `after` is then the same rows as `before`.",
    )


class PreviewView(ExactModel):
    """`view.preview`: the first rows of each file before and after the plan, masked."""

    files: tuple[PreviewFile, ...]
    step_samples: dict[str, StepSample] = Field(
        default_factory=dict,
        exclude=True,
        description="Rows each step changed, by item id: the screen puts them on `view.ledger.steps[].samples`.",
    )


# ---------------------------------------------------------------------------
# Cells (moved from api/routes/agent.py: its preview and this one mask alike)
# ---------------------------------------------------------------------------
def preview_cell(value: Any) -> str:
    """One preview cell as text. Numbers and dates are formatted, never run through the text masker:
    a float's digits (0.020000000000000002) would otherwise read as a phone number.

    Every digit a person needs to tell two values apart is kept: an integer in full, a float to 15
    significant digits (so 20240001 and 20240002 never both read `2.024e+07`). A narrower float (a
    Parquet `float32`) is first written at its own precision, so 0.1 stays `0.1` rather than the
    `0.100000001490116` it is as a double. Text is masked before it is cut, so an e-mail address split
    by the cut is still one the masker knows; a marker the cut would split is left out whole.
    """
    if value is None or value is pd.NA or value is pd.NaT or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, (int, float)) or hasattr(value, "dtype"):
        try:
            dtype = getattr(value, "dtype", None)
            if getattr(dtype, "kind", "") == "f" and getattr(dtype, "itemsize", 8) < 8:
                value = float(str(value))  # numpy's shortest text at the value's own precision
            number = float(value)
            return "" if number != number else format(number, ".15g")
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        text = str(value.isoformat())
        return text[:10] if text.endswith("T00:00:00") else text
    return masked_cut(str(value), PREVIEW_CELL_CHARS)


def _values(series: Any) -> list[Any]:
    """The column's values with their own scalar types: numpy scalars for a numpy column."""
    if isinstance(series.dtype, np.dtype):
        return list(series.to_numpy())
    return list(series.array)


def masked_rows(frame: Any, columns: tuple[str, ...], personal: Collection[str], limit: int) -> Table:
    """The first `limit` rows of `frame`'s `columns` as masked text; a personal column's cells are withheld."""
    head = frame.head(limit)
    # Column by column, not `iterrows`: a row Series would turn a float32 into a double (0.1 into
    # 0.100000001490116) before `preview_cell` could write it at its own precision.
    cells = {
        name: (
            (PERSONAL_CELL,) * len(head)
            if name in personal
            else tuple(preview_cell(value) for value in _values(head[name]))
        )
        for name in columns
    }
    return tuple(tuple(cells[name][index] for name in columns) for index in range(len(head)))


# ---------------------------------------------------------------------------
# What changed
# ---------------------------------------------------------------------------
def _aligned(before: pd.DataFrame, after: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """The rows both tables still hold, side by side (by the row's own label, else by position when the tables
    are the same length); None when they cannot be lined up."""
    if before.index.is_unique and after.index.is_unique:
        common = before.index.intersection(after.index, sort=False)
        return before.loc[common], after.loc[common]
    if len(before) == len(after):
        return before.reset_index(drop=True), after.reset_index(drop=True)
    return None


def _differs(a: pd.Series[Any], b: pd.Series[Any]) -> np.ndarray[Any, Any]:
    """Where two aligned columns hold different values (an empty cell equals an empty cell)."""
    x = a.astype(object).to_numpy()
    y = b.astype(object).to_numpy()
    try:
        same = np.asarray(x == y, dtype=bool) | (pd.isna(x) & pd.isna(y))
    except (TypeError, ValueError):
        return np.ones(len(x), dtype=bool)
    return np.asarray(~same)


def _changed_columns(before: pd.DataFrame, after: pd.DataFrame) -> set[str]:
    added = set(after.columns) - set(before.columns)
    removed = set(before.columns) - set(after.columns)
    changed = {str(c) for c in added | removed}
    both = [c for c in after.columns if c in before.columns]
    lined = _aligned(before[both], after[both]) if both else None
    if lined is None:  # rows that cannot be lined up: every shared column is a different table
        return changed | {str(c) for c in both}
    left, right = lined
    return changed | {str(c) for c in both if bool(_differs(left[c], right[c]).any())}


def _taints(step: RecipeStep, personal: Collection[str]) -> bool:
    """Whether the columns this step makes read personal data: it is about a personal column, its formula reads
    one, or it is a query (which can name any column anything)."""
    if step.column in personal:
        return True
    if step.kind is RecipeStepKind.SQL_QUERY:
        return bool(personal)
    if step.kind is RecipeStepKind.DERIVE:
        try:
            return bool(derive_names(str(step.params.get("expression", ""))) & set(personal))
        except RecipeError:
            return True
    return False


def _rows_of(frame: pd.DataFrame, column: str, personal: Collection[str]) -> tuple[dict[str, str], ...]:
    return tuple({column: row[0]} for row in masked_rows(frame, (column,), personal, STEP_SAMPLE_ROWS))


def _sample_of(
    step: RecipeStep, before: pd.DataFrame, after: pd.DataFrame, personal: Collection[str]
) -> StepSample | None:
    """Up to `STEP_SAMPLE_ROWS` rows the step changed, on its column: the rows as they were and as they became;
    for rows it left out, the rows as they were alone."""
    column = step.column
    made = (
        column
        if column in after.columns
        else next((c for c in after.columns if c not in before.columns), None)
    )
    lined = _aligned(before, after) if column in before.columns and made is not None else None
    if lined is None or made is None:
        return None
    left, right = lined
    hit = np.flatnonzero(_differs(left[column], right[made]))[:STEP_SAMPLE_ROWS]
    hidden = tuple(c for c in dict.fromkeys((column, made)) if c in personal)
    if len(hit):
        return StepSample(
            before=_rows_of(left.iloc[hit], column, personal),
            after=_rows_of(right.iloc[hit], made, personal),
            personal_columns=hidden,
        )
    if len(after) < len(before) and before.index.is_unique:
        gone = before.loc[before.index.difference(after.index, sort=False)]
        if len(gone):
            return StepSample(before=_rows_of(gone, column, personal), personal_columns=hidden)
    return None


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------
def _personal_columns(loaded: LoadedFile, frame: pd.DataFrame) -> set[str]:
    """The columns that never show a value: what the profile marks as personal data, and the sensitive ones
    (religion, caste, health, ...) `engine.privacy.sensitive` finds, as the agent's own context does."""
    from engine.privacy.sensitive import find_sensitive

    marked = {c.name for c in loaded.profile.columns if c.pii_kinds}
    return marked | {found.column for found in find_sensitive(frame)}


def _one_file(
    loaded: LoadedFile,
    items: Sequence[CaseItem],
    *,
    only_file: bool,
    tables: dict[str, Any],
    hide: Collection[str],
    rows: int,
) -> tuple[PreviewFile, dict[str, StepSample]]:
    base = consistent_types(loaded.frame.head(SAMPLE_ROWS)).copy()
    base.columns = [str(c) for c in base.columns]
    base = base.loc[:, ~base.columns.duplicated()]
    personal = _personal_columns(loaded, base) | {str(h) for h in hide}
    mine = recipe_steps_of(items, loaded.upload_id, only_file=only_file)
    after = base
    note: str | None = None
    samples: dict[str, StepSample] = {}
    if mine:
        ordered = [step for step, _ in mine]
        by_order = {step.order: item_id for step, item_id in mine}
        columns = {str(c) for c in base.columns}

        def watch(step: RecipeStep, was: pd.DataFrame, became: pd.DataFrame) -> None:
            if _taints(step, personal):
                personal.update(str(c) for c in became.columns if c not in was.columns)
            sample = _sample_of(step, was, became, personal)
            if sample is not None:
                samples[by_order[step.order]] = sample

        try:
            after = run_guarded(
                base,
                ordered,
                primary_key=accepted_column(items, "key", loaded.upload_id, columns),
                target=accepted_column(items, "outcome_column", loaded.upload_id, columns),
                tables=tables,
                upload_id=loaded.upload_id,
                source_name=loaded.file_name or None,
                levels=guard_levels(ordered),
                max_failure_pct=AgentConfig().max_conversion_failure_pct,
                twice=False,  # repeatability is the plan check's; the preview only needs the rows
                watch=watch,
            ).run.frame.copy()
            after.columns = [str(c) for c in after.columns]
        except (RecipeError, SqlStepError, ValueError, KeyError, TypeError) as exc:
            after, samples = base, {}
            note = masked_cut(
                f"A step could not run on a sample of this file: {getattr(exc, 'message', None) or type(exc).__name__}",
                300,
            )
    after = after.loc[:, ~after.columns.duplicated()]
    changed = _changed_columns(base, after)
    names = tuple(str(c) for c in after.columns) + tuple(
        str(c) for c in base.columns if c not in after.columns
    )
    shown_before = base.reindex(columns=list(names))
    shown_after = after.reindex(columns=list(names))
    file = PreviewFile(
        upload_id=loaded.upload_id,
        file_name=loaded.file_name,
        total_rows=int(loaded.file_rows or loaded.profile.row_count or len(loaded.frame)),
        columns=tuple(PreviewColumn(name=n, changed=n in changed, personal=n in personal) for n in names),
        before=masked_rows(shown_before, names, personal, rows),
        after=masked_rows(shown_after, names, personal, rows),
        note=note,
    )
    return file, samples


def preview_rows(
    files: Sequence[LoadedFile],
    items: Sequence[CaseItem],
    *,
    hide: Collection[str] = (),
    rows: int = PREVIEW_ROWS,
    only: str | None = None,
) -> PreviewView:
    """The first `rows` rows of each file before and after the ticked steps, masked; see the module docstring.

    `items` are the case's items of the section on screen; only the ACCEPTED ones with a recipe step count, which
    is what `plan_check` counts. `hide` names columns that show no value either (the settings' hidden columns,
    for the agent's tool); `only` shows one file (by upload id), the others still being the tables a query reads.
    """
    tables = case_tables(files)
    shown: list[PreviewFile] = []
    samples: dict[str, StepSample] = {}
    for loaded in files:
        if only is not None and loaded.upload_id != only:
            continue
        file, found = _one_file(loaded, items, only_file=len(files) == 1, tables=tables, hide=hide, rows=rows)
        shown.append(file)
        samples.update(found)
    return PreviewView(files=tuple(shown), step_samples=samples)


# ---------------------------------------------------------------------------
# Counted once per plan
# ---------------------------------------------------------------------------
_CACHE: OrderedDict[str, PreviewView] = OrderedDict()
_LOCK = threading.Lock()


def cached_preview(
    case_id: str,
    case_files: Sequence[CaseFile],
    items: Sequence[CaseItem],
    build: Callable[[], PreviewView],
) -> PreviewView:
    """`build()`, once per plan (`plan_check.plan_digest`: the files and every accepted item as it stands)."""
    key = f"{case_id}:{plan_digest(case_files, items)}"
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    made = build()
    with _LOCK:
        _CACHE[key] = made
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return made
