"""`configs/intake.yaml`: the settings of Data Doctor's intake rules (Plan L M150, DEC-1500).

The model's defaults are the shipped file's values, so a configuration directory without an
`intake.yaml` (a test's temporary root, a deployment that never copied it) reads files exactly as the
shipped one does; `tests/unit/intake/test_intake_config.py` holds the two equal. A malformed file is a
`ValueError` naming the file, never a silent fallback.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = ["INTAKE_FILENAME", "IntakeConfig", "load_intake_config"]

INTAKE_FILENAME: Final[str] = "intake.yaml"

_TOTAL_WORDS: Final[tuple[str, ...]] = (
    "total",
    "totals",
    "grand total",
    "sub total",
    "sub-total",
    "subtotal",
    "net total",
    "overall total",
)
_MISSING_WORDS: Final[tuple[str, ...]] = (
    "-",
    "--",
    "---",
    "–",
    "—",
    ".",
    "?",
    "na",
    "n.a.",
    "n/a",
    "none",
    "null",
    "nil",
    "nan",
    "unknown",
    "not known",
    "not available",
    "not applicable",
    "missing",
    "blank",
    "(blank)",
    "#n/a",
    "#div/0!",
    "#value!",
    "#ref!",
    "#name?",
    "#num!",
    "#null!",
)


class IntakeConfig(BaseModel):
    """What the intake rules may do, and how far they look."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: int = Field(default=1, description="Version of this file's layout.")
    max_header_search_rows: Annotated[int, Field(ge=1, le=200)] = Field(
        default=30, description="Rows the header finder looks through for the column names."
    )
    max_footer_rows: Annotated[int, Field(ge=0, le=100)] = Field(
        default=10, description="The most trailing total or note rows ever left out."
    )
    total_words: tuple[str, ...] = Field(
        default=_TOTAL_WORDS,
        description="Whole first cells that mark a total row, compared case-insensitively.",
    )
    missing_words: tuple[str, ...] = Field(
        default=_MISSING_WORDS,
        description="Cells read as empty in a column of numbers, dates or yes/no, compared trimmed and case-insensitively.",
    )
    sample_rows: Annotated[int, Field(ge=100, le=1_000_000)] = Field(
        default=20_000, description="Data rows the rules look at to decide."
    )
    max_workbook_uncompressed_mb: Annotated[int, Field(ge=1, le=65_536)] = Field(
        default=1024, description="The most an .xlsx archive may unpack to before it is refused unopened."
    )
    max_shared_strings_mb: Annotated[int, Field(ge=1, le=65_536)] = Field(
        default=64,
        description=(
            "The most a workbook's table of texts (`xl/sharedStrings.xml`) may unpack to: it is held in "
            "memory whole, about ten times its size."
        ),
    )

    @field_validator("total_words", "missing_words")
    @classmethod
    def _casefolded(cls, words: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(word.strip().casefold() for word in words if word.strip()))
        if not cleaned:
            raise ValueError("the list needs at least one word")
        return cleaned


_CACHE: dict[Path, tuple[float, IntakeConfig]] = {}
_LOCK: Final[threading.Lock] = threading.Lock()


def load_intake_config(root: Path) -> IntakeConfig:
    """`<root>/intake.yaml`, or the shipped defaults when the directory has none.

    Cached per file and modification time, so an upload costs one `stat` and an edited file is
    picked up without a restart.
    """
    path = Path(root) / INTAKE_FILENAME
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return IntakeConfig()
    with _LOCK:
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = IntakeConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"{INTAKE_FILENAME} is not valid: {exc}") from exc
    with _LOCK:
        _CACHE[path] = (mtime, config)
    return config
