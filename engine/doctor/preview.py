"""The data as the case's plan leaves it: the first rows of each file before and after, personal data masked.

`preview_rows(files, items)` tries the ticked steps (the person's, and the ones the agent applied by itself: both are
`ACCEPTED` items) on a seeded sample of 1,000 rows of each file under the guards - it IS the plan check's
materialisation (:func:`engine.doctor.materialize.materialize_plan`), not a copy of it, so the preview and the plan's
safety count are the same rows, steps, tables and stops - and keeps the first `PREVIEW_ROWS` rows before and after, which columns changed, and a few rows per step (`StepSample`). Nothing is
written anywhere; the frames are thrown away, so the answer holds masked text only.

Masking is one rule for every reader (the screen, the agent's `preview_working_copy` tool): a personal column shows
`[personal data]` and never its values; every other cell is masked whole (`engine.agent.formats.masked_cut`: e-mail,
phone, PAN, Aadhaar, card, IBAN, IP, URL, token, nine-digit run) before it is cut. Numbers and dates are formatted,
not run through the text masker (a float's digits would read as a phone number). A column a step makes out of a
personal one is personal too, and so is any column a query, a join or a reshape makes new or re-values while ANY
file of the case holds personal columns, or whose values are values of a personal column (a name joined in from a
second file, `REPLACE (full_name AS customer_id)`): the check follows the values, not only the names.

`GET /cases/{id}/screen` serves it as `view.preview`, counted once per plan (:func:`cached_preview`).
"""

from __future__ import annotations

import numbers
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Sequence
from datetime import date
from typing import Any, Final

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.dataset_recipe import consistent_types
from engine.agent.formats import masked_cut
from engine.agent.recipe import RecipeError, derive_names
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.decisions import decisions_from
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Prepared, PreparedFile, Sample, materialize_plan, scoped
from engine.doctor.plan_check import SAMPLE_ROWS, plan_digest
from engine.utils.logging import get_logger

__all__ = [
    "MAX_COLUMNS",
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

_LOG = get_logger(__name__)

PREVIEW_ROWS: Final[int] = 10
"""Rows of each file shown, before and after."""
PREVIEW_CELL_CHARS: Final[int] = 60
PERSONAL_CELL: Final[str] = "[personal data]"
STEP_SAMPLE_ROWS: Final[int] = 3
"""Changed rows shown for one step."""
MAX_COLUMNS: Final[int] = 60
"""Columns served for one file: the changed ones first, then the first of the rest."""
_MIN_PERSONAL_VALUE: Final[int] = 4
"""A personal value shorter than this ("no", "1") is no name, number or address a column could leak."""
_RESHAPING: Final[frozenset[RecipeStepKind]] = frozenset(
    {
        RecipeStepKind.SQL_QUERY,
        RecipeStepKind.COMBINE_ROWS,
        RecipeStepKind.KEEP_LATEST_PER_ID,
        RecipeStepKind.UNPIVOT,
    }
)
"""Steps whose output rows are not the input rows (a new index) and whose columns can come from any table."""
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
        description="The columns after the plan, then the ones it removes (the changed ones first when the file has "
        "more than 60); `before` and `after` follow this order (an empty cell where a column does not exist on "
        "that side)."
    )
    before: Table
    after: Table
    left_out_columns: int = Field(
        default=0, description=f"Columns not served ({MAX_COLUMNS} at most: the changed ones come first)."
    )
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
    # a length of time reads as an integer to `numbers`, and would show as a number of nanoseconds
    if isinstance(value, np.timedelta64):
        return "" if np.isnat(value) else masked_cut(str(pd.Timedelta(value)), PREVIEW_CELL_CHARS)
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, np.datetime64):  # a datetime column's numpy scalar is not a number of nanoseconds
        return "" if np.isnat(value) else preview_cell(pd.Timestamp(value))
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


def _texts(series: pd.Series[Any]) -> set[str]:
    """The distinct non-empty values of a column as text (`9876500000.0` and `9876500000` are one value)."""
    text = series.dropna().astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    return {t for t in text.unique().tolist() if t}


def _plain(frame: pd.DataFrame) -> pd.DataFrame:
    """The frame with text column names, the first of any repeated name only."""
    plain = frame.set_axis([str(c) for c in frame.columns], axis=1)
    return plain.loc[:, ~plain.columns.duplicated()]


def _changed_columns(
    before: pd.DataFrame, after: pd.DataFrame, *, reshaped: bool = False, key: str | None = None
) -> set[str]:
    """The columns the plan adds, removes or rewrites. After a query or a reshape the rows carry a new index, so
    they are lined up by the accepted ID column when it is unique on both sides; else a column counts as changed
    only when it holds a value it did not hold before (a plain row filter marks nothing)."""
    added = set(after.columns) - set(before.columns)
    removed = set(before.columns) - set(after.columns)
    changed = {str(c) for c in added | removed}
    both = [c for c in after.columns if c in before.columns]
    if reshaped:
        if key in both and before[key].is_unique and after[key].is_unique:
            rest = [c for c in both if c != key]
            left, right = before[both].set_index(key), after[both].set_index(key)
            lined = _aligned(left, right)
            if lined is None:
                return changed | {str(c) for c in rest}
            return changed | {str(c) for c in rest if bool(_differs(lined[0][c], lined[1][c]).any())}
        return changed | {str(c) for c in both if not _texts(after[c]) <= _texts(before[c])}
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


def _personal_after(
    step: RecipeStep,
    was: pd.DataFrame,
    became: pd.DataFrame,
    personal: Collection[str],
    *,
    names: Collection[str],
    values: Collection[str],
) -> set[str]:
    """The columns of `became` that now read personal data (see the module docstring).

    The columns the step is about, or reads (:func:`_taints`), when they are new. Beyond that, a column is new or
    re-valued (it holds a value it did not hold before) and then it is personal when the step is a query or a
    reshape and ANY file of the case has personal columns (`names`: the join may bring them in under any name), or
    when it holds a value of a personal column (`values`) whatever the step."""
    found: set[str] = set()
    if _taints(step, personal):
        found |= {str(c) for c in became.columns if c not in was.columns}
    reading = step.kind in _RESHAPING and bool(names)
    if not reading and not values:
        return found
    for column in became.columns:
        name = str(column)
        if name in personal or name in found:
            continue
        new = _texts(became[column])
        if column in was.columns and new <= _texts(was[column]):
            continue
        if reading or bool(new & set(values)):
            found.add(name)
    return found


def _rows_of(frame: pd.DataFrame, column: str, personal: Collection[str]) -> tuple[dict[str, str], ...]:
    return tuple({column: row[0]} for row in masked_rows(frame, (column,), personal, STEP_SAMPLE_ROWS))


def _sample_of(
    step: RecipeStep, before: pd.DataFrame, after: pd.DataFrame, personal: Collection[str]
) -> StepSample | None:
    """Up to `STEP_SAMPLE_ROWS` rows the step changed, on its column: the rows as they were and as they became;
    for rows it left out, the rows as they were alone."""
    if step.kind in _RESHAPING:  # the output's rows are new rows: there is no row to put beside its input
        return None
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


def _base_of(loaded: LoadedFile) -> pd.DataFrame:
    base = consistent_types(scoped(loaded.frame, Sample(SAMPLE_ROWS))).copy()
    base.columns = [str(c) for c in base.columns]
    kept: pd.DataFrame = base.loc[:, ~base.columns.duplicated()]
    return kept


class _Personal:
    """What is personal in the case, over ALL its files: the column names, and the values those columns hold."""

    def __init__(self, files: Sequence[LoadedFile], bases: dict[str, pd.DataFrame]) -> None:
        self.by_file: dict[str, set[str]] = {}
        self.values: set[str] = set()
        for loaded in files:
            base = bases[loaded.upload_id]
            columns = _personal_columns(loaded, base)
            self.by_file[loaded.upload_id] = columns
            for column in columns & set(base.columns):
                self.values |= {v for v in _texts(base[column]) if len(v) >= _MIN_PERSONAL_VALUE}
        self.names: set[str] = set().union(*self.by_file.values()) if self.by_file else set()


def _served_columns(names: list[str], changed: Collection[str]) -> list[str]:
    """At most `MAX_COLUMNS` of the columns, the changed ones first, in the file's order."""
    if len(names) <= MAX_COLUMNS:
        return names
    order = sorted(range(len(names)), key=lambda i: (names[i] not in changed, i))
    return [names[i] for i in sorted(order[:MAX_COLUMNS])]


def _one_file(
    loaded: LoadedFile,
    items: Sequence[CaseItem],
    *,
    done: PreparedFile | None,
    prepared: Prepared | None,
    failure: str | None,
    base: pd.DataFrame,
    personal: set[str],
    rows: int,
) -> PreviewFile:
    """One file as the materialised plan left it. `done` is what the plan did to it (none: no step touched it);
    `failure` is why a step could not run on it (then the rows are shown as they were)."""
    after = base
    note: str | None = None
    reshaped = False
    key = decisions_from(items).column("key", loaded.upload_id, {str(c) for c in base.columns})
    if failure is not None:
        note = masked_cut(f"A step could not run on a sample of this file: {failure}", 300)
    elif done is not None and prepared is not None:
        after = prepared.tables[done.table].copy()
        after.columns = [str(c) for c in after.columns]
        key = done.key or key
        reshaped = any(r.kind in {k.value for k in _RESHAPING} for _, r in done.receipts)
    after = after.loc[:, ~after.columns.duplicated()]
    changed = _changed_columns(base, after, reshaped=reshaped, key=key)
    names = [str(c) for c in after.columns] + [str(c) for c in base.columns if c not in after.columns]
    served = _served_columns(names, changed)
    shown_before = base.reindex(columns=served)
    shown_after = after.reindex(columns=served)
    return PreviewFile(
        upload_id=loaded.upload_id,
        file_name=loaded.file_name,
        total_rows=int(loaded.file_rows or loaded.profile.row_count or len(loaded.frame)),
        columns=tuple(PreviewColumn(name=n, changed=n in changed, personal=n in personal) for n in served),
        before=masked_rows(shown_before, tuple(served), personal, rows),
        after=masked_rows(shown_after, tuple(served), personal, rows),
        left_out_columns=len(names) - len(served),
        note=note,
    )


def preview_rows(
    files: Sequence[LoadedFile],
    items: Sequence[CaseItem],
    *,
    hide: Collection[str] = (),
    rows: int = PREVIEW_ROWS,
    only: str | None = None,
    as_of: date | None = None,
) -> PreviewView:
    """The first `rows` rows of each file before and after the ticked steps, masked; see the module docstring.

    `items` are the case's items of the section on screen; only the ACCEPTED ones with a recipe step count, which
    is what `plan_check` counts. `hide` names columns that show no value either (the settings' hidden columns,
    for the agent's tool); `only` shows one file (by upload id), the others still being the tables a query reads
    and the files whose personal columns and values no other column may show. `as_of` is the case's date.
    """
    bases = {f.upload_id: _base_of(f) for f in files}
    everyone = _Personal(files, bases)
    # A column is personal when ANY file of the case says so by name: a join brings it in under the same name.
    personal = {f.upload_id: set(everyone.names) | {str(h) for h in hide} for f in files}
    samples: dict[str, StepSample] = {}
    by_item: dict[str, str] = {}

    def watch(
        upload_id: str, item_id: str, step: RecipeStep, was: pd.DataFrame, became: pd.DataFrame
    ) -> None:
        was, became = _plain(was), _plain(became)
        mine = personal[upload_id]
        mine.update(_personal_after(step, was, became, mine, names=everyone.names, values=everyone.values))
        sample = _sample_of(step, was, became, mine)
        if sample is not None:
            samples[item_id] = sample
            by_item[item_id] = upload_id

    prepared: Prepared | None = None
    lost = "A step could not run."
    try:
        prepared = materialize_plan(
            (), items, files, Sample(SAMPLE_ROWS), as_of=as_of, twice=False, watch=watch
        )
    except Exception as exc:  # a bug, not a step: still no failing screen
        _LOG.warning("preview_step_failed", exc_info=True)
        lost = f"A step could not run: {type(exc).__name__}"
        samples.clear()
    failures = {} if prepared is None else {f.upload_id: f.message for f in prepared.failures}
    done = {} if prepared is None else {f.upload_id: f for f in prepared.files}
    for upload_id in failures:  # a file that could not be prepared shows no sample of a step that half-ran
        for item_id in [i for i, u in by_item.items() if u == upload_id]:
            samples.pop(item_id, None)
    shown = [
        _one_file(
            loaded,
            items,
            done=done.get(loaded.upload_id),
            prepared=prepared,
            failure=failures.get(loaded.upload_id, lost if prepared is None else None),
            base=bases[loaded.upload_id],
            personal=personal[loaded.upload_id],
            rows=rows,
        )
        for loaded in files
        if only is None or loaded.upload_id == only
    ]
    return PreviewView(files=tuple(shown), step_samples=samples)


# ---------------------------------------------------------------------------
# Counted once per plan
# ---------------------------------------------------------------------------
_CACHE: OrderedDict[str, PreviewView] = OrderedDict()
_LOCK = threading.Lock()


def cached_preview(
    client_id: str,
    case_id: str,
    case_files: Sequence[CaseFile],
    items: Sequence[CaseItem],
    build: Callable[[], PreviewView],
    *,
    as_of: date | None = None,
) -> PreviewView:
    """`build()`, once per plan (`plan_check.plan_digest`: the files and every accepted item as it stands) and
    as-of date, kept by client and case: one client's rows are never another's, whatever a digest says."""
    key = f"{client_id}:{case_id}:{plan_digest(case_files, items)}:{as_of or ''}"
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
