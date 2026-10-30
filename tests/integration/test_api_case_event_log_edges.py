"""An order log with blank customer IDs, driven to approval: the questions and stops say only what is true.

About 2,000 shoppers, a quarter of the lines with no customer ID. The person is asked which column names the customer
(not shown a row number or a date), is shown how many rows each column leaves blank, and is told which one the build
uses; once the build they approved supplies the customer the question is gone, the outcome question points at the
column that build makes, and the approval has no stop for the two dates of a training table or for the shoppers the
chosen audience leaves out.
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
from tests.integration.test_api_case_buildable_goal import GOAL, order_log

BLANK_SHARE = 0.25


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def blank_log() -> pd.DataFrame:
    frame = order_log()
    rng = np.random.default_rng(11)
    frame.loc[rng.random(len(frame)) < BLANK_SHARE, "customer_id"] = None
    return frame


def open_question(case: dict[str, Any], code: str) -> dict[str, Any] | None:
    return next((q for q in case["questions"] if q["code"] == code and q["answer"] is None), None)


def answer(
    client: TestClient, case_id: str, case: dict[str, Any], code: str, option_id: str
) -> dict[str, Any]:
    asked = open_question(case, code)
    assert asked is not None, code
    sent = client.post(
        f"/cases/{case_id}/answers",
        json={"version": case["version"], "question_id": asked["question_id"], "option_id": option_id},
    )
    assert sent.status_code == 200, sent.text
    return settled(client, case_id)


def test_an_order_log_with_blank_ids_asks_which_column_names_the_customer_and_goes_to_approval(
    client: TestClient,
) -> None:
    buffer = io.StringIO()
    blank_log().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    counted = next(u for u in case["diagnosis"]["use_cases"] if u["use_case_id"] == GOAL)["buildable"]
    assert counted["entity_key"] == "customer_id"

    # 1. the customer question: the columns that repeat per customer, each with its blank share, the build's
    # column recommended with the reason, and no row number (a row number names a row, not a customer)
    key = open_question(case, "KEY_NOT_UNIQUE")
    assert key is not None and key["blocking"]
    by_label = {o["label"]: o["option_id"] for o in key["options"]}
    customer = next(label for label in by_label if label.startswith("customer_id"))
    assert "25% blank" in customer and "cannot be tied to a person" in customer, customer
    assert not any("row number" in label for label in by_label)
    assert not any(
        label.startswith("order_date") for label in by_label
    ), "a column of dates names no customer"
    assert key["recommended"] == by_label[customer]
    assert "customer_id" in key["why"]

    # 2. the goal: the audience (every question has "I don't know" with a stated safe choice), then the build
    client.post(f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"})
    case = settled(client, case_id)

    def cards(kind: str) -> list[dict[str, Any]]:
        screen = client.get(f"/cases/{case_id}/screen").json()
        return [c for c in screen["view"]["conversation"] if c["type"] == kind]

    audience = next(c for c in cards("question") if c["code"] == "GOAL_SETTING")
    assert audience["dont_know"] is not None, "the audience question offers I don't know"
    safe = next(o for o in case["questions"] if o["code"] == "GOAL_SETTING")["options"]
    # I don't know never hands the work back: with nothing recommended it takes the widest reading, and says so
    (chosen,) = [o["effect"] for o in safe if o.get("safe")]
    assert chosen.startswith("Takes the ") and "active on or before" in chosen, chosen
    case = answer(client, case_id, case, "GOAL_SETTING", "active")
    (proposal,) = [i for i in case["items"] if i["kind"] == "recipe_step" and i.get("goal_id") == GOAL]
    sentence = proposal["payload"]["sentence"]
    left_out = counted["ids"] - counted["active_before"]
    assert f"Left out, of the file's {counted['ids']:,}" in sentence, sentence
    assert f"{left_out:,} have no activity on or before" in sentence, sentence
    assert f"would have {counted['active_before']:,} rows" in sentence, sentence
    assert not cards("ready"), "no approval card while a question is open"

    # 3. the customer; the outcome is asked of nobody: the build in the plan declares it, window-neutral
    key = open_question(case, "KEY_NOT_UNIQUE")
    assert key is not None
    case = answer(client, case_id, case, "KEY_NOT_UNIQUE", key["recommended"])
    assert open_question(case, "OUTCOME_COLUMN") is None, "the build declares its outcome"
    (proposal,) = [i for i in case["items"] if i["kind"] == "recipe_step" and i.get("goal_id") == GOAL]
    assert (
        proposal["state"] == "accepted"
    ), "the goal needs the build: it is in the plan, its yes is Approve's"

    # 4. one approval card: the build is in the plan (counted, its yes given by Approve), not a box left unticked
    (ready,) = cards("ready")
    assert ready["stage"] == "approve" and "with 3 changes" in ready["sentence"], ready["sentence"]
    (yes_card,) = [c for c in ready["confirmations"] if c["item_id"] == proposal["item_id"]]
    assert yes_card["required"] is True and yes_card["can_agree"] is True
    # approving without the build's own yes is refused: it is never silently left out
    case = settled(client, case_id)
    refused = client.post(
        f"/cases/{case_id}/approval", json={"version": case["version"], "sentence": ready["sentence"]}
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "APPROVAL_CONFIRMATION_MISSING"
    case = settled(client, case_id)
    approval = client.get(f"/cases/{case_id}/screen").json()["view"]["approval"]
    stops = [y["sentence"] for y in approval["yeses"] if y["kind"] == "acknowledge"]
    assert not [s for s in stops if "reads the data as of" in s or "leaves out" in s], stops
    assert "confirm" in [y["kind"] for y in approval["yeses"]]
    shown = next(y["sentence"] for y in approval["yeses"] if y["item_id"] == proposal["item_id"])
    assert "Who is kept is the audience you chose" in shown and "sampled" not in shown
    # ... and the approval goes through to ready, the customers the audience leaves out being agreed
    case = settled(client, case_id)
    done = client.post(
        f"/cases/{case_id}/approval",
        json={
            "version": case["version"],
            "digest": approval["digest"],
            "yeses": [
                {k: y[k] for k in ("kind", "key", "item_id", "sentence") if y.get(k) is not None}
                for y in approval["yeses"]
            ],
        },
    )
    assert done.status_code == 200, done.text
    (readiness,) = done.json()["readiness"]
    assert readiness["goal_id"] == GOAL and readiness["verdict"] == "ready"
    # the person's decision and the count they agreed to are recorded by that yes, not before it
    recorded = next(
        i for i in client.get(f"/cases/{case_id}").json()["items"] if i["item_id"] == proposal["item_id"]
    )
    assert recorded["decided_by"] and recorded["agreed_rows"] == left_out, recorded


def test_a_customer_column_chosen_for_a_log_is_not_expected_to_be_unique(client: TestClient) -> None:
    buffer = io.StringIO()
    blank_log().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    key = open_question(case, "KEY_NOT_UNIQUE")
    assert key is not None and key["recommended"] is not None
    case = answer(client, case_id, case, "KEY_NOT_UNIQUE", key["recommended"])
    assert open_question(case, "KEY_NOT_UNIQUE") is None
    said = [i["title"] for i in case["items"] if i["kind"] == "data_check"]
    assert not [t for t in said if "should name each customer once" in t], said


def _log_case(client: TestClient) -> tuple[str, dict[str, Any]]:
    buffer = io.StringIO()
    blank_log().to_csv(buffer, index=False)
    uploaded = client.post(
        "/uploads",
        files={"file": ("orders.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    case = client.post("/cases", json={"upload_ids": [uploaded.json()["upload_id"]]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    client.post(f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"})
    return case_id, settled(client, case_id)


def _goal_builds(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        i
        for i in case["items"]
        if i["kind"] == "recipe_step"
        and i.get("goal_id") == GOAL
        and i["state"] == "accepted"
        and not i.get("withdrawn_because")
    ]


def test_the_goals_build_is_nobodys_decision_until_approve_and_follows_the_audience(
    client: TestClient,
) -> None:
    case_id, case = _log_case(client)
    case = answer(client, case_id, case, "GOAL_SETTING", "active")
    (build,) = _goal_builds(case)
    # in the plan by default, but no person has decided it: no decider, no agreed count, no card saying "your choice"
    assert build.get("decided_by") is None and build.get("agreed_rows") is None, build
    screen = client.get(f"/cases/{case_id}/screen").json()["view"]
    cards = [c for c in screen["conversation"] if c.get("item_id") == build["item_id"]]
    assert not [c for c in cards if c.get("by") in {"you", "someone"}], cards
    assert build["payload"]["audience"]["audience_id"] == "active"
    # the preview says how many rows the built table has in the whole file, whatever the sample the plan was checked on
    (previewed,) = screen["preview"]["files"]
    assert previewed["after_table"] and previewed["after_rows"] == build["payload"]["audience"]["customers"]
    assert previewed["after_rows"] < previewed["total_rows"]

    # the audience is answered again: the plan's build follows it, the earlier one is taken back
    case = answer_again(client, case_id, case, "GOAL_SETTING", "quiet")
    now = _goal_builds(case)
    assert not [b for b in now if b["payload"].get("audience", {}).get("audience_id") == "active"], now
    assert all(b["payload"]["audience"]["audience_id"] == "quiet" for b in now), now


def answer_again(
    client: TestClient, case_id: str, case: dict[str, Any], code: str, option_id: str
) -> dict[str, Any]:
    held = next(q for q in case["questions"] if q["code"] == code)
    sent = client.post(
        f"/cases/{case_id}/answers",
        json={"version": case["version"], "question_id": held["question_id"], "option_id": option_id},
    )
    assert sent.status_code == 200, sent.text
    return settled(client, case_id)
