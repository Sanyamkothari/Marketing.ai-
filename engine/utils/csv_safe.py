"""Spreadsheet formula injection: a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is run
as a formula when a CSV is opened in Excel or Sheets (security review 2, M-3).

Client data is not trusted: an end customer controls what their name, e-mail or id says. Every CSV a person
downloads (contact lists, scores, copy messages) writes its text cells through here, so such a cell gets a
leading quote and is shown as text. Numbers, flags, dates and empty cells are never touched. Parquet files stay
exactly as they were; they are not opened in a spreadsheet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import pandas as pd

__all__ = ["FORMULA_PREFIXES", "FORMULA_PREFIX_REGEX", "formula_safe", "formula_safe_frame"]

FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r")
FORMULA_PREFIX_REGEX: Final[str] = r"^[=+\-@\t\r]"
"""`FORMULA_PREFIXES` as a regular expression, for Arrow's compute kernels."""


def formula_safe(value: str) -> str:
    """`value`, with a leading `'` when a spreadsheet would run it as a formula."""
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def formula_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """`frame` with `formula_safe` applied to every text cell; the same object when nothing needs it."""
    out = frame
    for position in range(frame.shape[1]):
        column = frame.iloc[:, position]
        if column.dtype.kind not in "OT" and str(column.dtype) not in ("string", "str"):
            continue
        risky = column.map(lambda v: isinstance(v, str) and v.startswith(FORMULA_PREFIXES)).astype(bool)
        if not risky.any():
            continue
        if out is frame:
            out = frame.copy()
        safe = column.where(~risky, column.map(lambda v: formula_safe(v) if isinstance(v, str) else v))
        out.isetitem(position, safe.array)
    return out
