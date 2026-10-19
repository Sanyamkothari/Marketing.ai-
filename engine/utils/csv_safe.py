"""Spreadsheet formula injection: a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is run
as a formula when a CSV is opened in Excel or Sheets (security review 2, M-3).

Client data is not trusted: an end customer controls what their name, e-mail or id says. The protection is
applied when a person **downloads** a CSV (`api.routes.runs.read_artefact`), never to the file as stored:
`scores.csv` and `copy_messages.csv` are also machine inputs (win-back, root cause, variant arms read them
back), and a quote written into the key would stop it joining to the Parquet files and the treat list. The
treat list is a download-only file, so it is protected where it is built.

At download time a CSV has lost its column types, so a cell is left alone when it is a plain number (a negative
net value is `-1.5`, and a number cannot run anything); every other cell that starts with one of the
characters gets a leading quote and is shown as text. The header row is cells too.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Final

__all__ = ["FORMULA_PREFIXES", "FORMULA_PREFIX_REGEX", "formula_safe", "formula_safe_csv"]

FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r")
FORMULA_PREFIX_REGEX: Final[str] = r"^[=+\-@\t\r]"
"""`FORMULA_PREFIXES` as a regular expression, for Arrow's compute kernels."""

_NUMBER: Final[re.Pattern[str]] = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_MAYBE_RISKY: Final[re.Pattern[str]] = re.compile(r'(?:^|[\n,])"?[=+\-@\t\r]')
"""A cheap look at the whole text: no cell starts with a risky character, so there is nothing to parse."""


def formula_safe(value: str) -> str:
    """`value`, with a leading `'` when a spreadsheet would run it as a formula."""
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def formula_safe_csv(payload: bytes) -> bytes:
    """`payload` (a UTF-8 CSV) with `formula_safe` applied to every cell that is not a plain number.

    Returns `payload` itself, byte for byte, when no cell needs it. Otherwise the table is written again with
    `\\n` line ends, as every CSV this product writes has them.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload
    if not _MAYBE_RISKY.search(text):
        return payload
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    changed = False
    for row in csv.reader(io.StringIO(text, newline="")):
        safe = [
            (
                cell
                if not cell.startswith(FORMULA_PREFIXES) or _NUMBER.fullmatch(cell) or cell == "-inf"
                else "'" + cell
            )
            for cell in row
        ]
        changed = changed or safe != row
        writer.writerow(safe)
    return out.getvalue().encode("utf-8") if changed else payload
