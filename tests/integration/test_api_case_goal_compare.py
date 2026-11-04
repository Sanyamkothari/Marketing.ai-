"""A file that fits another goal better than the one the person chose: Data Doctor says so, the person decides.

The file is generated here (no real data): an e-mail experiment of 6,000 customers split at random into a men's
e-mail, a women's e-mail and no e-mail, with the visit, the purchase and the spend of the next two weeks. The check is
started under Targeted Advertisement. The file fits E-mail Offer Choice better (a no-email control group exists and
its outcome column is named for it), so one question lists the goals that fit better, ranked as the goal list ranks
them, and f"Keep {CHOSEN_NAME}". Choosing another goal switches the case's goal through the ordinary goal
switch; keeping it is remembered and not asked again.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.integration.doctor_wait import settled

pytestmark = pytest.mark.integration

CHOSEN = "targeted-advertisement"
BETTER = "hillstrom-email"
CODE = "GOAL_FIT_COMPARE"
BETTER_NAME = "Choose the best e-mail for each customer"
CHOSEN_NAME = "Find customers likely to buy"
CUSTOMERS = 6_000


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def email_experiment(seed: int = 11) -> pd.DataFrame:
    """Customers who last bought within a year, each sent a men's e-mail, a women's e-mail or none, at random."""
    rng = np.random.default_rng(seed)
    segment = rng.choice(["No E-Mail", "Mens E-Mail", "Womens E-Mail"], size=CUSTOMERS)
    recency = rng.integers(1, 13, size=CUSTOMERS)
    history = np.round(rng.gamma(2.0, 120.0, size=CUSTOMERS), 2)
    lift = np.where(segment == "No E-Mail", 0.0, 0.02)
    visit = rng.random(CUSTOMERS) < 0.1 + lift * 4 + (13 - recency) * 0.004
    conversion = visit & (rng.random(CUSTOMERS) < 0.18 + lift * 3)
    return pd.DataFrame(
        {
            "customer_id": np.arange(1, CUSTOMERS + 1),
            "recency": recency,
            "history": history,
            "channel": rng.choice(["Phone", "Web", "Multichannel"], size=CUSTOMERS),
            "segment": segment,
            "visit": visit.astype(int),
            "conversion": conversion.astype(int),
            "spend": np.where(conversion, np.round(rng.gamma(2.0, 40.0, size=CUSTOMERS), 2), 0.0),
        }
    )


def start(client: TestClient, goal: str = CHOSEN) -> dict[str, Any]:
    buffer = io.StringIO()
    email_experiment().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("email_experiment.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": goal, "mode": "train"},
    )
    assert uploaded.status_code == 201, uploaded.text
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    chosen = client.post(
        f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": goal, "op_id": "g1"}
    )
    assert chosen.status_code == 200, chosen.text
    return settled(client, case_id)


def asked(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in case["questions"] if q["code"] == CODE]


def active_goal(case: dict[str, Any]) -> str:
    (goal,) = [g["goal_id"] for g in case["goals"] if g["active"]]
    return str(goal)


def answer(
    client: TestClient, case: dict[str, Any], question: dict[str, Any], option_id: str
) -> dict[str, Any]:
    response = client.post(
        f"/cases/{case['case_id']}/answers",
        json={"version": case["version"], "question_id": question["question_id"], "option_id": option_id},
    )
    assert response.status_code == 200, response.text
    return settled(client, case["case_id"])


def test_the_question_lists_the_goals_that_fit_better_then_keep(client: TestClient) -> None:
    case = start(client)
    verdicts = {u["use_case_id"]: u for u in case["diagnosis"]["use_cases"]}
    assert verdicts[BETTER]["status"] in {"ready", "check"}, verdicts[BETTER]
    (question,) = asked(case)
    assert question["goal_id"] == CHOSEN and question["answer"] is None
    assert question["text"].startswith("This file also fits other goals")
    assert not question["blocking"], "the chosen goal still fits: nothing waits for the answer"
    labels = [o["label"] for o in question["options"]]
    assert labels[0] == BETTER_NAME, labels
    assert labels[-1] == f"Keep {CHOSEN_NAME}" and len(labels) <= 4
    keep = question["options"][-1]
    assert keep["safe"] and question["recommended"] == keep["option_id"]
    assert all(o["effect"] for o in question["options"]), "each answer says what it does, with its reason"
    assert active_goal(case) == CHOSEN


def test_choosing_the_other_goal_switches_the_goal_through_the_goal_switch(client: TestClient) -> None:
    case = start(client)
    (question,) = asked(case)
    option = next(o for o in question["options"] if o["label"] == BETTER_NAME)
    switched = answer(client, case, question, option["option_id"])
    assert active_goal(switched) == BETTER
    assert {g["goal_id"] for g in switched["goals"]} == {
        CHOSEN,
        BETTER,
    }, "the first goal is set aside, not lost"
    (answered,) = asked(switched)
    assert answered["answer"] == option["option_id"] and answered["goal_id"] == CHOSEN
    assert all(q["answer"] is not None for q in asked(switched)), "nothing is left to ask about the goals"


def test_keeping_the_goal_is_remembered_and_not_asked_again(client: TestClient) -> None:
    case = start(client)
    (question,) = asked(case)
    kept = answer(client, case, question, question["options"][-1]["option_id"])
    assert active_goal(kept) == CHOSEN
    (again,) = asked(kept)
    assert (
        again["question_id"] == question["question_id"]
        and again["answer"] == question["options"][-1]["option_id"]
    )
    resumed = client.post(f"/cases/{case['case_id']}/visit", json={}).json()
    assert [q["answer"] for q in asked(resumed)] == [again["answer"]], "asked once, answered once"


def test_the_best_fitting_goal_is_not_asked_about(client: TestClient) -> None:
    case = start(client)
    (question,) = asked(case)
    option = next(o for o in question["options"] if o["label"] == BETTER_NAME)
    switched = answer(client, case, question, option["option_id"])
    assert active_goal(switched) == BETTER
    assert [q for q in switched["questions"] if q["code"] == CODE and q["goal_id"] == BETTER] == []


def test_a_goal_the_file_cannot_serve_is_asked_about_and_approve_waits_on_the_fit(client: TestClient) -> None:
    case = start(client, "telco-churn")
    (question,) = asked(case)
    assert question["text"].startswith("This file does not fit") and question["recommended"] is None
    labels = [o["label"] for o in question["options"]]
    assert BETTER_NAME in labels and labels[-1].startswith("Keep ") and len(labels) <= 4
    assert "needs" in question["options"][-1]["effect"], "Keep says what is missing"
    view = client.get(f"/cases/{case['case_id']}/screen").json()["view"]
    assert view["goal_id"] == "telco-churn" and view["verdict"]["key"] != "ready"
