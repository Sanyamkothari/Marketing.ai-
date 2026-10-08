"""Business-language reasons mapping (Plan J M98, DEC-1308).

Translates technical model explanations (`feature`, `direction`, `contribution`)
into plain business sentences using `configs/decide/reasons.yaml`.
Unmapped features retain their original explanation text.
All operations are vectorized for linear performance at scale.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, Field

from engine.contracts import Reason
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = [
    "CONFIG_PATH",
    "BusinessReasonDictionary",
    "extract_and_map_reasons_from_parquet",
    "load_reasons_dictionary",
    "map_reasons",
    "map_reasons_from_explanations",
]

_LOGGER = get_logger(__name__)

CONFIG_PATH: Final[Path] = Path("configs/decide/reasons.yaml")


class BusinessReasonDictionary(BaseModel):
    """Configuration mapping feature names and directions to plain business phrases."""

    schema_version: int = Field(default=1)
    features: dict[str, dict[str, str]] = Field(default_factory=dict)

    def lookup(self, feature: str, direction: str) -> str | None:
        """Return the business phrase for a feature and direction, or None if unmapped."""
        by_feat = self.features.get(feature)
        if by_feat is None:
            return None
        return by_feat.get(direction.lower())


@lru_cache(maxsize=1)
def load_reasons_dictionary(path: Path | None = None) -> BusinessReasonDictionary:
    """Load and validate the business reason dictionary from YAML."""
    target = path or CONFIG_PATH
    if not target.is_file():
        _LOGGER.warning("Reasons dictionary file not found at %s; returning empty dictionary", target)
        return BusinessReasonDictionary()
    with target.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return BusinessReasonDictionary.model_validate(data)


def map_reasons(
    features: Sequence[str] | np.ndarray | pd.Series,
    directions: Sequence[str] | np.ndarray | pd.Series,
    texts: Sequence[str] | np.ndarray | pd.Series,
    *,
    dictionary: BusinessReasonDictionary | None = None,
) -> np.ndarray:
    """Vectorized lookup of business phrases for parallel arrays of feature, direction, and text.

    Unmapped features keep the original text from `texts`.
    """
    dict_obj = dictionary or load_reasons_dictionary()
    pair_map = {
        f"{feat}::{dir_val.lower()}": phrase
        for feat, d_map in dict_obj.features.items()
        for dir_val, phrase in d_map.items()
    }

    f_ser = pd.Series(features, dtype="object")
    d_ser = pd.Series(directions, dtype="object").str.lower()
    t_ser = pd.Series(texts, dtype="object")

    keys = f_ser.astype(str) + "::" + d_ser.astype(str)
    mapped = keys.map(pair_map)
    return mapped.combine_first(t_ser).to_numpy()


def map_reasons_from_explanations(
    reasons_per_row: Sequence[Sequence[Reason | dict[str, Any]]],
    *,
    dictionary: BusinessReasonDictionary | None = None,
) -> pd.DataFrame:
    """Extract top 3 reasons per row and map them to business language.

    Returns a DataFrame with columns `["reason_1", "reason_2", "reason_3"]`.
    If a row has fewer than 3 reasons, remaining slots are set to None (null).
    """
    dict_obj = dictionary or load_reasons_dictionary()
    n = len(reasons_per_row)

    r1: list[str | None] = [None] * n
    r2: list[str | None] = [None] * n
    r3: list[str | None] = [None] * n

    for i, row_reasons in enumerate(reasons_per_row):
        for slot, slot_list in enumerate((r1, r2, r3)):
            if slot < len(row_reasons):
                item = row_reasons[slot]
                if isinstance(item, dict):
                    feat = str(item.get("feature", ""))
                    direction = str(item.get("direction", "")).lower()
                    text = str(item.get("text", ""))
                else:
                    feat = str(item.feature)
                    direction = str(item.direction).lower()
                    text = str(item.text)
                phrase = dict_obj.lookup(feat, direction)
                slot_list[i] = phrase if phrase is not None else text

    return pd.DataFrame(
        {
            "reason_1": pd.Series(r1, dtype="object"),
            "reason_2": pd.Series(r2, dtype="object"),
            "reason_3": pd.Series(r3, dtype="object"),
        }
    )


def extract_and_map_reasons_from_parquet(
    table: pa.Table,
    *,
    dictionary: BusinessReasonDictionary | None = None,
) -> pd.DataFrame:
    """Fast vectorized extraction and mapping of top 3 reasons directly from a PyArrow table.

    Operates in O(N) vectorized numpy/pyarrow time without row-by-row iteration.
    """
    if "reasons" not in table.column_names:
        n = table.num_rows
        return pd.DataFrame(
            {
                "reason_1": pd.Series([None] * n, dtype="object"),
                "reason_2": pd.Series([None] * n, dtype="object"),
                "reason_3": pd.Series([None] * n, dtype="object"),
            }
        )

    reasons_chunked = table["reasons"]
    reasons_arr = reasons_chunked.combine_chunks()
    n = len(reasons_arr)

    offsets = reasons_arr.offsets.to_numpy()
    flat_values = reasons_arr.values

    # Flat arrays of all reasons across all rows
    feat_all = flat_values.field("feature").to_pandas()
    dir_all = flat_values.field("direction").to_pandas()
    txt_all = flat_values.field("text").to_pandas()

    dict_obj = dictionary or load_reasons_dictionary()
    pair_map = {
        f"{feat}::{dir_val.lower()}": phrase
        for feat, d_map in dict_obj.features.items()
        for dir_val, phrase in d_map.items()
    }

    key_flat = feat_all.astype(str) + "::" + dir_all.astype(str).str.lower()
    mapped_flat = key_flat.map(pair_map).combine_first(txt_all).to_numpy()

    lengths = offsets[1:] - offsets[:-1]
    reason_cols: dict[str, np.ndarray] = {}

    for slot in range(3):
        has_slot = lengths > slot
        slot_indices = offsets[:-1][has_slot] + slot
        col_res = np.full(n, None, dtype=object)
        col_res[has_slot] = mapped_flat[slot_indices]
        reason_cols[f"reason_{slot + 1}"] = col_res

    return pd.DataFrame(
        {
            "reason_1": pd.Series(reason_cols["reason_1"], dtype="object"),
            "reason_2": pd.Series(reason_cols["reason_2"], dtype="object"),
            "reason_3": pd.Series(reason_cols["reason_3"], dtype="object"),
        }
    )
