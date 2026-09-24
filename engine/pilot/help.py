"""The plain-language catalogue: one sentence of meaning and one fix per code (Plan E M60, DEC-902).

`configs/pilot/help.yaml` is read here and nowhere else. The readiness report, the pre-flight
checker and the in-app tooltips all ask this module, so a warning reads the same on the client's
laptop, in the PDF and beside the pill on the screen.

Which codes: every *check* code a client's data can raise - the Phase 1 validation table, the
Phase 2 onboarding table, its extension and the Phase 3b uplift checks - the one code registry,
`engine.contracts.CHECK_CODES` (DEC-950) - the two drift verdicts that are worth a
sentence, the threshold fallback note and the pre-flight checker's own codes. Engine and HTTP error
codes (a missing run, a refused sign-in) are not data problems a client analyst fixes and are not
here; they keep the message and suggestion the API already sends (DEC-902).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.config import config_root, load_yaml

__all__ = [
    "DRIFT_CODES",
    "HELP_FILENAME",
    "CodeHelp",
    "HelpCatalogue",
    "MetricHelp",
    "SettingHelp",
    "code_help",
    "known_codes",
    "load_help",
    "setting_help",
    "setting_key",
]

HELP_FILENAME: Final[str] = "pilot/help.yaml"
"""Relative to the configuration root, like `roles.yaml` and `use_cases/`."""

DRIFT_CODES: Final[dict[str, str]] = {"watch": "DRIFT_WATCH", "drifted": "DRIFT_DRIFTED"}
"""`DriftStatus` values that deserve a sentence, and the catalogue code each one reads."""

THRESHOLD_FALLBACK_CODE: Final[str] = "THRESHOLD_FALLBACK"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeHelp(_Strict):
    title: str = Field(min_length=1)
    meaning: str = Field(min_length=1)
    fix: str = Field(min_length=1)


class SettingHelp(_Strict):
    meaning: str = Field(min_length=1)


class MetricHelp(_Strict):
    name: str = Field(min_length=1)
    random: float | None = None


class HelpCatalogue(_Strict):
    schema_version: Literal[1]
    codes: dict[str, CodeHelp]
    settings: dict[str, SettingHelp]
    terms: dict[str, str]
    metrics: dict[str, MetricHelp]


@lru_cache(maxsize=8)
def _load(path: Path, mtime_ns: int) -> HelpCatalogue:
    del mtime_ns  # part of the cache key only: an edited file is read again
    return HelpCatalogue.model_validate(load_yaml(path))


def load_help(root: Path | None = None) -> HelpCatalogue:
    """The catalogue under `root` (default: the configuration root the engine reads)."""
    path = config_root(root) / HELP_FILENAME
    return _load(path, path.stat().st_mtime_ns)


def known_codes() -> frozenset[str]:
    """Every code the catalogue must explain: the engine's check codes plus Plan E's own."""
    from engine.contracts import CHECK_CODES
    from engine.pilot.preflight import PREFLIGHT_CODES

    return frozenset(CHECK_CODES | set(DRIFT_CODES.values()) | {THRESHOLD_FALLBACK_CODE} | PREFLIGHT_CODES)


def code_help(code: str, root: Path | None = None) -> CodeHelp | None:
    """The plain-language entry for `code`, or None for a code the catalogue does not carry."""
    return load_help(root).codes.get(code)


_INDEX: Final[re.Pattern[str]] = re.compile(r"\[\d+\]")


def setting_key(path: str) -> str:
    """`actions.bands[1].min_score` -> `actions.bands[*].min_score`: one entry per repeated field."""
    return _INDEX.sub("[*]", path)


def setting_help(path: str, root: Path | None = None) -> SettingHelp | None:
    return load_help(root).settings.get(setting_key(path))
