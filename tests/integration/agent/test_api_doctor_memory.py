"""Memory per client through the API (Plan L M159, DEC-1590 … DEC-1595).

Month one: a messy export is set up in Guided setup, with one edit made by hand, and approved. Month two:
the same export (other rows) is uploaded; `POST /uploads` and the session say it looks like last month's,
and "Use the same treatment" ticks last month's steps - the hand edit included - for the person to check
and approve. Nothing is applied before Approve. A file of another shape is offered nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.agent.contracts import DATA_RECIPE_FILENAME, DataRecipe
from engine.doctor.memory import MEMORY_PREFIX
from engine.storage import LocalStorage, upload_key
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.integration.agent.test_api_session import _decide_everything, _session

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
HAND_EDIT = {"kind": "drop_column", "column": "tenure_months"}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def upload(client: TestClient, frame: pd.DataFrame, mode: str = "train", name: str = "billing.csv") -> Any:
    response = client.post(
        "/uploads",
        files={"file": (name, frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return response.json()


def recipe_of(data_dir: Path, upload_id: str) -> DataRecipe:
    return LocalStorage(data_dir).read_model(upload_key(upload_id, DATA_RECIPE_FILENAME), DataRecipe)


def approve_month_one(client: TestClient, data_dir: Path) -> tuple[str, DataRecipe]:
    first = upload(client, messy_frame(seed=7), name="billing_sept.csv")
    assert first["treatment"] is None, "nothing is remembered yet"
    upload_id = first["upload_id"]
    started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201 and started.json()["treatment"] is None
    _session(client.post(f"/uploads/{upload_id}/agent-session/edits", json=HAND_EDIT))
    assert _decide_everything(client, upload_id)["status"] == "ready"
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    return upload_id, recipe_of(data_dir, applied.json()["upload_id"])


def identities(recipe: DataRecipe) -> set[tuple[str, str, str]]:
    return {(s.kind.value, s.column, repr(sorted(s.params.items()))) for s in recipe.steps}


def test_next_months_file_is_offered_last_months_treatment(client: TestClient, data_dir: Path) -> None:
    _, first_recipe = approve_month_one(client, data_dir)
    assert any(s.kind.value == "drop_column" and s.column == "tenure_months" for s in first_recipe.steps)
    storage = LocalStorage(data_dir)
    (entry,) = storage.list_keys(MEMORY_PREFIX)
    text = storage.read_text(entry)
    assert "₹" not in text and "Rs." not in text, "the memory holds names and ids, never a value"

    second = upload(client, messy_frame(seed=11), name="billing_oct.csv")
    offer = second["treatment"]
    assert offer is not None and offer["kind"] == "same_shape" and offer["can_apply"]
    assert offer["recipe_id"] == first_recipe.recipe_id and offer["steps"] == len(first_recipe.steps)
    assert offer["source_file_name"] == "billing_sept.csv"
    assert "This looks like billing_sept.csv" in offer["sentence"]

    upload_id = second["upload_id"]
    started = client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    assert started.status_code == 201
    assert started.json()["treatment"]["recipe_id"] == first_recipe.recipe_id
    before = started.json()["session"]
    assert all(
        p["state"] == "pending" for p in before["proposals"]
    ), "nothing is ticked before the person asks"

    stale = client.post(f"/uploads/{upload_id}/agent-session/same-treatment", json={"recipe_id": "rc-other"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "DOCTOR_MEMORY_CHANGED"

    used = client.post(
        f"/uploads/{upload_id}/agent-session/same-treatment", json={"recipe_id": first_recipe.recipe_id}
    )
    assert used.status_code == 200, used.text
    assert used.json()["treatment"] is None, "the offer is gone once taken"
    session = used.json()["session"]
    assert session["status"] != "applied", "taking the offer approves nothing"
    accepted = [p for p in session["proposals"] if p["state"] == "accepted" and p["step"] is not None]
    hand = [
        p for p in accepted if p["step"]["kind"] == "drop_column" and p["step"]["column"] == "tenure_months"
    ]
    assert len(hand) == 1 and hand[0]["source"] == "person" and hand[0]["title"].startswith("As last time")
    assert any(r["tool"] == "doctor_memory" for r in session["tool_results"])

    again = client.post(
        f"/uploads/{upload_id}/agent-session/same-treatment", json={"recipe_id": first_recipe.recipe_id}
    )
    assert again.status_code == 422 and again.json()["detail"]["code"] == "DOCTOR_MEMORY_USED"

    assert client.post(f"/uploads/{upload_id}/agent-session/apply").status_code == 409, "still to be decided"
    assert _decide_everything(client, upload_id)["status"] == "ready"
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    second_recipe = recipe_of(data_dir, applied.json()["upload_id"])
    assert identities(first_recipe) <= identities(second_recipe), "last month's steps all ran again"


def test_a_file_of_another_shape_is_offered_nothing(client: TestClient, data_dir: Path) -> None:
    approve_month_one(client, data_dir)
    other = upload(client, messy_frame(seed=11).drop(columns=["region"]))
    assert other["treatment"] is None
    started = client.post(f"/uploads/{other['upload_id']}/agent-session", json={"use_case": USE_CASE})
    assert started.json()["treatment"] is None
    refused = client.post(
        f"/uploads/{other['upload_id']}/agent-session/same-treatment", json={"recipe_id": "rc-anything"}
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "DOCTOR_MEMORY_NONE"


def test_a_file_to_score_of_the_same_shape_is_recognised_but_not_treated(
    client: TestClient, data_dir: Path
) -> None:
    approve_month_one(client, data_dir)
    scoring = upload(client, messy_frame(seed=11), mode="score")
    offer = scoring["treatment"]
    assert offer is not None and offer["kind"] == "same_shape" and not offer["can_apply"]
