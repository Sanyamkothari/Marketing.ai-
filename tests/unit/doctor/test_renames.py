"""Next month's file changed: the rename matcher (Plan L follow-up #16, DEC-1597).

Profiles are made the way every upload's are (`ingest.profile_dataset`), from small frames shaped like a
monthly billing export, so the matcher is tested on what it reads in production.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.config import load_use_case
from engine.contracts import ColumnProfile, DatasetProfile
from engine.doctor.renames import (
    MARGIN,
    NAME_MIN,
    STATS_MIN,
    match_renames,
    name_similarity,
    recipe_columns,
    unseen_values,
    value_similarity,
)
from engine.stages import ingest

ROWS = 600


def profile(frame: pd.DataFrame) -> DatasetProfile:
    return ingest.profile_dataset(
        frame,
        load_use_case("targeted-advertisement"),
        upload_id="u",
        file_name="bills.csv",
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )


def column(prof: DatasetProfile, name: str) -> ColumnProfile:
    return next(c for c in prof.columns if c.name == name)


def bills(seed: int, **renamed: str) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{seed}{i:05d}" for i in range(ROWS)],
            "amount": np.round(rng.gamma(2.0, 600.0, size=ROWS), 2),
            "visits": rng.integers(0, 6, size=ROWS),
            "region": rng.choice(["Mumbai", "Pune", "Delhi", "Chennai"], size=ROWS),
        }
    )
    return frame.rename(columns=renamed)


def match(old: pd.DataFrame, new: pd.DataFrame, missing: list[str]) -> tuple[tuple, tuple]:  # type: ignore[type-arg]
    before, after = profile(old), profile(new)
    extras = [c.name for c in after.columns if c.name not in set(old.columns)]
    return match_renames(
        [column(before, m) for m in missing],
        [column(after, e) for e in extras],
        old_rows=before.row_count,
        new_rows=after.row_count,
    )


def test_names_are_compared_by_their_words() -> None:
    assert name_similarity("amount", "bill_amount") >= 0.8 > NAME_MIN
    assert name_similarity("billAmount", "bill amount") == 1.0
    assert name_similarity("amount", "region") < NAME_MIN


def test_a_renamed_column_with_the_same_values_is_proposed() -> None:
    old = bills(1)
    new = bills(2, amount="bill_amount")
    (rename,), unresolved = match(old, new, ["amount"])
    assert unresolved == ()
    assert (rename.missing, rename.candidate) == ("amount", "bill_amount")
    assert rename.values >= STATS_MIN and rename.name >= NAME_MIN
    assert "no 'amount' but has 'bill_amount'" in rename.reason and "numbers" in rename.reason


def test_a_clear_winner_beats_another_new_column_of_the_same_kind() -> None:
    old = bills(1)
    new = bills(2, amount="bill_amount")
    new["late_fee"] = np.round(np.random.default_rng(3).uniform(0, 5, size=ROWS), 2)  # numbers, other spread
    (rename,), unresolved = match(old, new, ["amount"])
    assert rename.candidate == "bill_amount" and unresolved == ()


def test_two_close_candidates_are_never_guessed_between() -> None:
    old = bills(1)
    new = bills(2, amount="bill_amount")
    new["paid_amount"] = np.round(np.random.default_rng(4).gamma(2.0, 600.0, size=ROWS), 2)
    suggested, (unresolved,) = match(old, new, ["amount"])
    assert suggested == ()
    assert unresolved.missing == "amount"
    assert set(unresolved.candidates) == {"bill_amount", "paid_amount"}
    assert "choose" in unresolved.reason


def test_a_type_change_is_never_a_rename() -> None:
    old = bills(1)
    new = bills(2).drop(columns=["amount"])
    new["amount_note"] = [f"paid {i}" for i in range(ROWS)]  # text, with the name close
    suggested, (unresolved,) = match(old, new, ["amount"])
    assert suggested == () and unresolved.candidates == ()


def test_a_close_name_alone_is_not_enough() -> None:
    old = bills(1)
    new = bills(2).drop(columns=["amount"])
    new["amount_due"] = np.round(np.random.default_rng(5).uniform(1e6, 2e6, size=ROWS), 0)  # other values
    suggested, (unresolved,) = match(old, new, ["amount"])
    assert suggested == ()
    assert unresolved.candidates == ("amount_due",)
    assert "has a name like 'amount', but its values are not like" in unresolved.reason


def test_text_with_no_name_in_common_is_not_proposed_on_its_counts_alone() -> None:
    """Two columns of all-different text agree on every count; that says nothing about what they hold."""
    old = bills(1)
    old["note"] = [f"n{i}" for i in range(ROWS)]
    new = bills(2).drop(columns=[])
    new["remark_text"] = [f"r{i}" for i in range(ROWS)]
    suggested, _ = match(old, new, ["note"])
    assert suggested == ()


def test_numbers_that_agree_closely_need_no_name() -> None:
    old = bills(1)
    new = bills(2, amount="x7")
    (rename,), _ = match(old, new, ["amount"])
    assert rename.candidate == "x7" and rename.name < NAME_MIN


def test_one_new_column_is_never_proposed_for_two_missing_ones() -> None:
    old = bills(1)
    old["amount_2"] = old["amount"]
    new = bills(2, amount="bill_amount")
    suggested, unresolved = match(old, new, ["amount", "amount_2"])
    assert suggested == ()
    assert {u.missing for u in unresolved} == {"amount", "amount_2"}


def test_categories_are_compared_by_their_common_values() -> None:
    old = bills(1)
    new = bills(2, region="city")
    (rename,), _ = match(old, new, ["region"])
    assert rename.candidate == "city" and "most common values in common" in rename.reason


def test_value_similarity_is_zero_across_kinds() -> None:
    prof = profile(bills(1))
    assert value_similarity(column(prof, "amount"), ROWS, column(prof, "region"), ROWS) == 0.0
    assert MARGIN > 0


def test_recipe_columns_name_what_the_steps_read_and_the_months() -> None:
    steps = (
        RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="amount", params={"decimal": "."}),
        RecipeStep(
            order=2,
            kind=RecipeStepKind.DERIVE,
            column="amount",
            new_column="per_visit",
            params={"expression": "amount / visits"},
        ),
    )
    reads, months = recipe_columns(steps)
    assert {"amount", "visits"} <= reads and months == frozenset()


def test_new_spellings_are_listed_for_a_map_values_step() -> None:
    step = RecipeStep(
        order=1, kind=RecipeStepKind.MAP_VALUES, column="city", params={"mapping": {"Bombay": "Mumbai"}}
    )
    old = pd.DataFrame({"city": ["Mumbai", "Bombay", "Pune", None]})
    new = pd.DataFrame({"town": ["Mumbai", "Bombay", "Pune ", "Bengaluru", "Bangalore", "Bangalore"]})
    (found,) = unseen_values([step], new, old, renames={"city": "town"})
    assert found.column == "town" and found.step == 1
    assert found.values == ("Bangalore", "Bengaluru") and (found.distinct, found.rows) == (2, 3)
    (hidden,) = unseen_values([step], new, old, renames={"city": "town"}, personal=frozenset({"town"}))
    assert hidden.values == () and hidden.distinct == 2


@pytest.mark.parametrize("missing", ["amount", "visits"])
def test_a_column_with_no_new_column_like_it_is_unresolved(missing: str) -> None:
    old = bills(1)
    new = bills(2).drop(columns=[missing])
    suggested, (unresolved,) = match(old, new, [missing])
    assert suggested == () and unresolved.missing == missing and unresolved.candidates == ()
