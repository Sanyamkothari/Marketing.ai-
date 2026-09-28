"""Plan G adversarial review, API findings: the parts that need no app."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from api.routes.agent import _cell, _rows
from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.formats import masked_cut
from engine.utils.time import utc_now


def test_the_preview_shows_every_digit_of_a_number() -> None:
    assert [_cell(v) for v in (np.int64(20240001), np.int64(20240002), 20240003)] == [
        "20240001",
        "20240002",
        "20240003",
    ]
    assert _cell(1234567.89) == "1234567.89"
    assert _cell(np.float64(1234567.89)) == "1234567.89"
    assert _cell(0.020000000000000002) == "0.02"
    assert _cell(3.0) == "3"
    assert _cell(True) == "True"
    assert _cell(float("nan")) == ""


def test_a_preview_cell_is_masked_before_it_is_cut() -> None:
    """Cut first, and an e-mail address split at the cut is no longer one the masker knows."""
    cell = _cell("x" * 45 + " jane.doe@example.com")
    assert "jane" not in cell
    assert len(cell) <= 60


def _recipe(**update: object) -> DataRecipe:
    steps = (RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="ctr", params={"decimal": "."}),)
    base = DataRecipe(
        recipe_id="rec1",
        use_case_id="targeted-advertisement",
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key="customer_id",
        target=None,
        created_at=utc_now(),
    )
    return DataRecipe.model_validate({**base.model_dump(), **update})


def test_a_recipe_written_before_levels_were_recorded_still_loads() -> None:
    old = _recipe().model_dump(mode="json")
    del old["levels"], old["max_failure_pct"]
    loaded = DataRecipe.model_validate(old)
    assert (loaded.levels, loaded.max_failure_pct) == (None, None)
    assert loaded.recipe_hash == recipe_hash(loaded.steps)  # the hash still covers the steps only


def test_recipes_with_the_same_steps_prepare_alike_only_under_the_same_roles_and_limits() -> None:
    base = _recipe()
    assert base.prepares_like(_recipe(recipe_id="rec2", approved_by="someone"))
    for update in (
        {"primary_key": None},
        {"target": "converted_30d"},
        {"snapshot_column": "as_of"},
        {"levels": (AgentLevel.CLEAN,)},
        {"max_failure_pct": 10.0},
    ):
        other = _recipe(**update)
        assert other.recipe_hash == base.recipe_hash
        assert not base.prepares_like(other), update


def test_the_preview_formats_a_pandas_integer_as_an_integer() -> None:
    frame = pd.DataFrame({"n": pd.array([20240001, None], dtype="Int64")})
    assert _cell(frame["n"].iloc[0]) == "20240001"


def test_a_narrow_float_is_shown_at_its_own_precision() -> None:
    assert _cell(np.float32(0.1)) == "0.1"
    assert _cell(np.float16(0.5)) == "0.5"
    assert _cell(np.float32(3.0)) == "3"
    assert _cell(np.float32("nan")) == ""
    assert _cell(np.float64(0.1)) == "0.1"


def test_the_cut_never_splits_a_marker() -> None:
    cell = _cell("a" * 50 + " jane.doe@example.com")
    assert "jane" not in cell and "[RED" not in cell
    assert cell == "a" * 50 + " "
    whole = _cell("a" * 40 + " jane.doe@example.com")
    assert whole.endswith("[REDACTED:email]") and len(whole) <= 60


def test_the_preview_rows_keep_a_float32_column_at_its_own_precision(tmp_path: Path) -> None:
    """The real preview path, not `_cell` alone: a Parquet float32 column read back as float32."""
    path = tmp_path / "f.parquet"
    pd.DataFrame(
        {
            "a": np.array([0.1, 0.2, np.nan], dtype="float32"),
            "n": pd.array([1, None, 3], dtype="Int64"),
            "s": ["x", "y", "z"],
        }
    ).to_parquet(path)
    frame = pd.read_parquet(path)
    assert frame["a"].dtype == np.dtype("float32")
    assert _rows(frame, ("a", "n", "s"), set()) == (("0.1", "1", "x"), ("0.2", "", "y"), ("", "3", "z"))
    assert _rows(frame, ("a", "s"), {"s"})[0] == ("0.1", "[personal data]")


def test_format_examples_are_masked_before_they_are_cut() -> None:
    value = "a" * 70 + " jane.doe@example.com"
    cut = masked_cut(value, 80)
    assert "jane" not in cut and "[RED" not in cut and len(cut) <= 80
