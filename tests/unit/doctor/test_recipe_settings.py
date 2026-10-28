"""A stored recipe carries the locale profile and money basis the person approved, and every replay checks them."""

from __future__ import annotations

import ast
from datetime import UTC, datetime

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.dataset_recipe import run_replay
from engine.agent.recipe import RecipeError
from engine.doctor.currency import MoneyBasis, MoneyColumn
from engine.doctor.locale import india_defaults
from engine.doctor.materialize import UNCHECKED_SETTINGS_NOTE
from tests.unit.doctor._ast_calls import called_names

STEP = RecipeStep(order=1, kind=RecipeStepKind.DROP_COLUMN, column="junk")


def _recipe(*, basis: bool, guarded: bool = True, profile: bool = False) -> DataRecipe:
    money = MoneyBasis(
        workspace_currency="INR",
        columns=(MoneyColumn(column="amount", written_in=("INR",), reported_in="INR"),),
    )
    return DataRecipe(
        recipe_id="r1",
        use_case_id="u",
        source_fingerprint="f",
        steps=(STEP,),
        recipe_hash=recipe_hash((STEP,)),
        acknowledged_stops=() if guarded else None,
        money_basis=money.model_dump(mode="json") if basis else None,
        locale_profile=india_defaults().model_dump(mode="json") if profile else None,
        created_at=datetime.now(UTC),
    )


def _replay(recipe: DataRecipe, frame: pd.DataFrame) -> object:
    return run_replay(frame, recipe, levels=(AgentLevel.CLEAN,), max_failure_pct=5.0, upload_id="up")


def test_a_new_currency_stops_a_scoring_replay_with_a_plain_error() -> None:
    frame = pd.DataFrame({"amount": ["$10", "$20", "$30"], "junk": [1, 2, 3]})
    with pytest.raises(RecipeError) as stopped:
        _replay(_recipe(basis=True), frame)
    assert stopped.value.code == "CURRENCY_FOREIGN" and stopped.value.column == "amount"
    assert "USD" in stopped.value.message


def test_a_file_in_the_approved_currency_replays() -> None:
    frame = pd.DataFrame({"amount": ["₹10", "₹20", "₹30"], "junk": [1, 2, 3]})
    run = _replay(_recipe(basis=True), frame)
    assert "junk" not in run.frame.columns and run.receipt.notes == ()  # type: ignore[attr-defined]


def test_a_recipe_without_the_settings_still_replays_with_a_note() -> None:
    frame = pd.DataFrame({"amount": ["$10", "$20", "$30"], "junk": [1, 2, 3]})
    run = _replay(_recipe(basis=False), frame)
    assert run.receipt.notes == (UNCHECKED_SETTINGS_NOTE,)  # type: ignore[attr-defined]


def test_the_locale_profile_is_checked_too() -> None:
    frame = pd.DataFrame({"when": ["13/01/2025", "25/02/2025", "30/03/2025"], "junk": [1, 2, 3]})
    profile = india_defaults().model_dump(mode="json")
    profile["date_order"] = {**profile["date_order"], "value": "month_first", "source": "confirmed"}
    recipe = _recipe(basis=False).model_copy(update={"locale_profile": profile})
    with pytest.raises(RecipeError) as stopped:
        _replay(recipe, frame)
    assert stopped.value.code == "LOCALE_CHANGED"


def test_the_prescription_check_is_the_same_function() -> None:
    from pathlib import Path

    source = Path(__file__).resolve().parents[3] / "engine/doctor/prescription_replay.py"
    assert "check_approved_settings" in called_names(ast.parse(source.read_text(encoding="utf-8")))
    assert "detect_locale" not in called_names(ast.parse(source.read_text(encoding="utf-8")))


def test_aliased_imports_are_resolved_by_the_architecture_scan() -> None:
    tree = ast.parse("from engine.agent.recipe import run_recipe as go\nagain = go\nagain(1)\n")
    assert "run_recipe" in called_names(tree)


def _yes_no(words: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame({"flag": [words[i % 2] for i in range(40)]})


def test_yes_no_families_are_compared_as_a_set_not_an_order() -> None:
    """A detected (not confirmed) yes/no setting stays approved for a file that shows only some of its families."""
    from engine.doctor.locale import detect_locale
    from engine.doctor.materialize import check_approved_settings

    english, hinglish = _yes_no(("yes", "no")), _yes_no(("haan", "nahi"))
    both = {"a": english, "b": hinglish}
    profile = detect_locale(both, base=india_defaults(), client_id="c").profile
    assert check_approved_settings(both, profile=profile) == ("locale",)
    assert check_approved_settings({"file": hinglish}, profile=profile) == ("locale",)  # a scoring file
    assert check_approved_settings({"file": english}, profile=profile) == ("locale",)
    assert check_approved_settings({"a": english}, profile=profile) == ("locale",)


def test_readiness_keeps_every_code_the_scoring_route_keeps() -> None:
    from api.routes.agent_recipes import _SUGGESTIONS
    from engine.doctor.readiness import _RECIPE_REFUSAL_CODES

    assert frozenset(_SUGGESTIONS) == _RECIPE_REFUSAL_CODES
