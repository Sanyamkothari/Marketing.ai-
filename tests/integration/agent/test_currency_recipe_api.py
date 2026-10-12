"""Plan O M242 (DEC-1812 (c)), review fix: a saved recipe that reads a money column in rupees stops a run whose file has
a `$12` in it with `CURRENCY_MIXED` - the stop that asks which rows are which - and never with `RECIPE_STEP_INVALID`
and its "retrain the model" advice."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import runs
from engine.agent.contracts import RecipeStep, RecipeStepKind, recipe_hash
from engine.jobs import CancelToken
from tests.integration.agent.test_recipe_runs import _messy, _recipe, _score, _seed_model, _upload

pytestmark = pytest.mark.integration


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Real ingest, recipes and validation; the job itself does nothing, so nothing trains."""

    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def test_a_dollar_in_a_rupee_column_is_a_409_that_asks_which_rows_are_which(
    client: TestClient, data_dir: Path
) -> None:
    recipe = _recipe(_messy(), target=None)
    steps = (
        *recipe.steps,
        RecipeStep(order=3, kind=RecipeStepKind.PARSE_NUMBER, column="monthly_bill", params={"unit": "INR"}),
    )
    _seed_model(data_dir, recipe.model_copy(update={"steps": steps, "recipe_hash": recipe_hash(steps)}))
    scoring = _messy("scoring", rows=800)
    scoring["monthly_bill"] = ["$12" if i % 100 == 0 else "₹1,200" for i in range(len(scoring))]
    response = _score(client, _upload(client, scoring, mode="score"))
    assert response.status_code == 409, response.text
    (check,) = response.json()["validation"]["checks"]
    assert (check["code"], check["column"]) == ("CURRENCY_MIXED", "monthly_bill")
    assert "$12" in check["message"]
    assert "Say which rows are in which currency" in check["suggestion"]
    assert "Retrain" not in check["suggestion"]
