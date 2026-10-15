"""The contact list is ranked best first: highest score on top, no score last, ties by customer id."""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.decide.treat_list import SCORE_COLUMN, _best_first


def _frame(ids: list[str], scores: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"customer_id": pd.Series(ids, dtype="object"), SCORE_COLUMN: scores})


def test_highest_score_first_with_missing_scores_last() -> None:
    frame = _frame(["a", "b", "c", "d"], [0.2, np.nan, 0.9, 0.5])
    assert list(_best_first(frame, ("customer_id",))["customer_id"]) == ["c", "d", "a", "b"]


def test_a_tie_goes_by_customer_id_whatever_the_file_order() -> None:
    frame = _frame(["c", "a", "b"], [0.5, 0.5, 0.5])
    assert list(_best_first(frame, ("customer_id",))["customer_id"]) == ["a", "b", "c"]
    assert list(_best_first(frame.iloc[::-1], ("customer_id",))["customer_id"]) == ["a", "b", "c"]


def test_numeric_ids_tie_break_as_numbers_not_text() -> None:
    frame = _frame(["10", "9", "100"], [0.5, 0.5, 0.5])
    assert list(_best_first(frame, ("customer_id",))["customer_id"]) == ["9", "10", "100"]


def test_a_composite_key_breaks_a_tie_on_every_part() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": ["b", "a", "a"],
            "snapshot_date": ["2026-08-01", "2026-09-01", "2026-08-01"],
            SCORE_COLUMN: [0.7, 0.7, 0.7],
        }
    )
    out = _best_first(frame, ("customer_id", "snapshot_date"))
    assert list(zip(out["customer_id"], out["snapshot_date"], strict=True)) == [
        ("a", "2026-08-01"),
        ("a", "2026-09-01"),
        ("b", "2026-08-01"),
    ]
