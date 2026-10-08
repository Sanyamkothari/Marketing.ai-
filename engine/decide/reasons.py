"""Business-language reasons (Plan J M98, DEC-1308).

Turns the model's per-row reasons (`feature`, `direction`, `value`, `text`) into plain sentences with
`configs/decide/reasons.yaml`. Unmapped features keep today's text.

**What a reason says.** `Reason.direction` is whether the feature pushed the *score* up or down
(`engine.contracts.Reason`), not whether the customer's own value went up or down. A phrase therefore
may say only two things, both true for every row it can be printed on: the row's own value (the
`{value}` placeholder, filled from `Reason.value` exactly as the explain stage stored it) and which way
that value moved the score. It never asserts a trend ("for 3 months") or a number the row does not
carry. The dictionary refuses, when it loads, a phrase with a digit outside `{value}`.

**Directions.** `up` and `down` are the two measured directions. `none` is a general reason
(`Direction.NONE`: the tiers measured the row as all-zero, so the feature is named from the run's
importance chart and no arrow was measured); a phrase for it must not claim a direction either. A
feature with no phrase for a row's direction keeps today's text for that row.

All operations are vectorised over the Arrow list column, so the cost is linear in the reasons kept.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, Field, field_validator

from engine.config import config_root
from engine.contracts import Reason
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = [
    "REASONS_FILE",
    "REASON_DIRECTIONS",
    "VALUE_PLACEHOLDER",
    "BusinessReasonDictionary",
    "extract_and_map_reasons_from_parquet",
    "load_reasons_dictionary",
    "map_reasons",
    "map_reasons_from_explanations",
    "reasons_path",
]

_LOGGER = get_logger(__name__)

REASONS_FILE: Final[Path] = Path("decide") / "reasons.yaml"
"""`reasons.yaml` relative to the configuration root (`engine.config.config_root`)."""

VALUE_PLACEHOLDER: Final[str] = "{value}"
"""Replaced by the row's own value for the feature (`Reason.value`); the only text a phrase may fill in."""

REASON_DIRECTIONS: Final[tuple[str, ...]] = ("up", "down", "none")
"""`Direction`'s values: the feature pushed the score up, pushed it down, or was not measured on this row."""

_SLOTS: Final[int] = 3

_MISSING: Final[str] = "missing"
"""`engine.stages.explain.MISSING_VALUE`: what `Reason.value` holds for a value the row does not have.
A phrase that prints the value would read "Monthly spend of missing", so such a reason keeps its text."""


class BusinessReasonDictionary(BaseModel):
    """`reasons.yaml`: feature -> direction (`up`, `down`, `none`) -> a phrase true for any row.

    A phrase is validated when the file loads: at most one `{value}`, no other braces, and no digit
    outside the placeholder (a digit there would be a number the row does not carry).
    """

    schema_version: int = Field(default=2)
    features: dict[str, dict[str, str]] = Field(default_factory=dict)

    @field_validator("features")
    @classmethod
    def _phrases_are_true_for_any_row(cls, features: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for feature, by_direction in features.items():
            for direction, phrase in by_direction.items():
                where = f"reasons.yaml, {feature!r} / {direction!r}"
                if direction not in REASON_DIRECTIONS:
                    raise ValueError(f"{where}: the direction must be one of {', '.join(REASON_DIRECTIONS)}.")
                if not phrase.strip():
                    raise ValueError(f"{where}: the phrase is empty.")
                if phrase.count(VALUE_PLACEHOLDER) > 1:
                    raise ValueError(f"{where}: {VALUE_PLACEHOLDER} may appear once.")
                bare = phrase.replace(VALUE_PLACEHOLDER, "")
                if "{" in bare or "}" in bare:
                    raise ValueError(f"{where}: the only placeholder is {VALUE_PLACEHOLDER}.")
                if any(char.isdigit() for char in bare):
                    raise ValueError(
                        f"{where}: a phrase cannot hold a number of its own (it would be printed for a "
                        f"row that does not carry it); put the row's {VALUE_PLACEHOLDER} in instead."
                    )
        return features

    def phrase(self, feature: str, direction: str) -> str | None:
        """The template for a feature and direction, or None when unmapped."""
        by_direction = self.features.get(feature)
        if by_direction is None:
            return None
        return by_direction.get(direction.lower())

    def render(self, feature: str, direction: str, value: str, text: str) -> str:
        """The phrase with the row's `value`, or `text` when unmapped or the value is blank."""
        template = self.phrase(feature, direction)
        if template is None:
            return text
        if VALUE_PLACEHOLDER in template:
            if not value.strip() or value.strip().lower() == _MISSING:
                return text
            return template.replace(VALUE_PLACEHOLDER, value)
        return template


def reasons_path(root: Path | None = None) -> Path:
    """Where `reasons.yaml` is read from: under the configuration root, never the working directory."""
    return config_root(root) / REASONS_FILE


def load_reasons_dictionary(
    path: Path | None = None, *, root: Path | None = None
) -> BusinessReasonDictionary:
    """Load and validate the dictionary: `path`, else `reasons.yaml` under `config_root(root)`.

    A missing file gives an empty dictionary (every reason keeps today's text) and a warning; an
    invalid file raises, with the phrase to fix named.
    """
    target = Path(path) if path is not None else reasons_path(root)
    if not target.is_file():
        _LOGGER.warning("Reasons dictionary file not found at %s; reasons keep the model's own text", target)
        return BusinessReasonDictionary()
    return _load(str(target.resolve()), target.stat().st_mtime_ns)


@lru_cache(maxsize=8)
def _load(resolved: str, mtime_ns: int) -> BusinessReasonDictionary:
    """Cached on the file's path and modification time, so an edit is picked up without a restart."""
    del mtime_ns
    with Path(resolved).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return BusinessReasonDictionary.model_validate(data)


def _render_flat(
    features: pd.Series[Any],
    directions: pd.Series[Any],
    values: pd.Series[Any] | None,
    texts: pd.Series[Any],
    dictionary: BusinessReasonDictionary,
) -> np.ndarray:
    """One phrase (or today's text) per flat reason, with no loop over the rows.

    The feature and the direction are each reduced to integer codes once (`pd.factorize`), the dictionary
    is looked up for the few distinct pairs only, and the result is taken back per reason by indexing.
    """
    feature_codes, feature_names = pd.factorize(features.astype(str))
    direction_codes, direction_names = pd.factorize(directions.astype(str).str.lower())
    shape = (len(feature_names), len(direction_names))
    head = np.full(shape, None, dtype=object)
    tail = np.full(shape, "", dtype=object)
    needs = np.zeros(shape, dtype=bool)
    mapped = np.zeros(shape, dtype=bool)
    for row, feature in enumerate(feature_names):
        by_direction = dictionary.features.get(str(feature), {})
        for col, direction in enumerate(direction_names):
            phrase = by_direction.get(str(direction))
            if phrase is not None:
                before, found, after = phrase.partition(VALUE_PLACEHOLDER)
                head[row, col], tail[row, col], needs[row, col] = before, after, bool(found)
                mapped[row, col] = True
    heads = head[feature_codes, direction_codes]
    tails = tail[feature_codes, direction_codes]
    needed = needs[feature_codes, direction_codes]
    if values is None:
        shown = np.full(len(features), "", dtype=object)
    else:
        shown = values.astype("object").where(values.notna(), "").astype(str).to_numpy(dtype=object)
    shown_codes, shown_names = pd.factorize(
        shown
    )  # a value repeats across reasons: test each distinct one once
    blank = pd.Series(shown_names).str.strip().str.lower().isin(["", _MISSING]).to_numpy()[shown_codes]
    usable = mapped[feature_codes, direction_codes] & ~(needed & blank)
    rendered = pd.Series(np.where(usable, heads, ""), dtype="object")
    rendered = (
        rendered + pd.Series(np.where(needed, shown, ""), dtype="object") + pd.Series(tails, dtype="object")
    )
    return np.where(usable, rendered.to_numpy(dtype=object), texts.to_numpy(dtype=object))


def map_reasons(
    features: Sequence[str] | np.ndarray | pd.Series[Any],
    directions: Sequence[str] | np.ndarray | pd.Series[Any],
    texts: Sequence[str] | np.ndarray | pd.Series[Any],
    *,
    values: Sequence[str] | np.ndarray | pd.Series[Any] | None = None,
    dictionary: BusinessReasonDictionary | None = None,
) -> np.ndarray:
    """Vectorised phrases for parallel arrays of feature, direction and text (and the row's value).

    Unmapped features keep the original text from `texts`; so does a mapped one whose phrase needs the
    row's `{value}` when none is given.
    """
    dict_obj = dictionary or load_reasons_dictionary()
    return _render_flat(
        pd.Series(features, dtype="object"),
        pd.Series(directions, dtype="object"),
        None if values is None else pd.Series(values, dtype="object"),
        pd.Series(texts, dtype="object"),
        dict_obj,
    )


def _empty_slots(n: int) -> dict[str, pd.Series[Any]]:
    return {f"reason_{slot + 1}": pd.Series([None] * n, dtype="object") for slot in range(_SLOTS)}


def _slot_columns(mapped_flat: np.ndarray, offsets: np.ndarray, n: int) -> pd.DataFrame:
    """Cut the flat phrases into `reason_1..3` per row from the list offsets; missing slots stay null."""
    lengths = offsets[1:] - offsets[:-1]
    columns: dict[str, pd.Series[Any]] = {}
    for slot in range(_SLOTS):
        has_slot = lengths > slot
        column = np.full(n, None, dtype=object)
        column[has_slot] = mapped_flat[offsets[:-1][has_slot] + slot]
        columns[f"reason_{slot + 1}"] = pd.Series(column, dtype="object")
    return pd.DataFrame(columns)


def map_reasons_from_explanations(
    reasons_per_row: Sequence[Sequence[Reason | Mapping[str, Any]]],
    *,
    dictionary: BusinessReasonDictionary | None = None,
) -> pd.DataFrame:
    """The top three reasons per row in business words, as `reason_1`, `reason_2`, `reason_3`.

    A row with fewer than three reasons has null in the remaining slots. The rows are flattened once
    and rendered by the same vectorised path as the Arrow reader.
    """
    dict_obj = dictionary or load_reasons_dictionary()
    n = len(reasons_per_row)
    lengths = np.fromiter((len(row) for row in reasons_per_row), dtype=np.int64, count=n)
    offsets = np.concatenate(([0], np.cumsum(lengths)))
    flat = [item for row in reasons_per_row for item in row]

    def field(item: Reason | Mapping[str, Any], name: str) -> str:
        return str(item.get(name, "")) if isinstance(item, Mapping) else str(getattr(item, name))

    mapped = _render_flat(
        pd.Series([field(item, "feature") for item in flat], dtype="object"),
        pd.Series([field(item, "direction") for item in flat], dtype="object"),
        pd.Series([field(item, "value") for item in flat], dtype="object"),
        pd.Series([field(item, "text") for item in flat], dtype="object"),
        dict_obj,
    )
    return _slot_columns(mapped, offsets, n)


def extract_and_map_reasons_from_parquet(
    table: pa.Table,
    *,
    dictionary: BusinessReasonDictionary | None = None,
) -> pd.DataFrame:
    """Top three reasons per row straight from the Arrow `reasons` list column, in business words.

    O(N): the list's offsets and child arrays are read once, with no row-by-row iteration.
    """
    n = table.num_rows
    if "reasons" not in table.column_names:
        return pd.DataFrame(_empty_slots(n))

    reasons_arr = table["reasons"].combine_chunks()
    offsets = reasons_arr.offsets.to_numpy()
    flat_values = reasons_arr.values
    names = set(flat_values.type.names) if hasattr(flat_values.type, "names") else set()
    dict_obj = dictionary or load_reasons_dictionary()

    mapped = _render_flat(
        flat_values.field("feature").to_pandas(),
        flat_values.field("direction").to_pandas(),
        flat_values.field("value").to_pandas() if "value" in names else None,
        flat_values.field("text").to_pandas(),
        dict_obj,
    )
    return _slot_columns(mapped, offsets, n)
