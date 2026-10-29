"""The protocol's routes (Plan O M246, DEC-1816): Guided setup's Approve with the sentence read, a case's own yes to
an APPROVE-class item, standing answers per client, candidate rules - and with the switch off, no path at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_policy import all_policies
from api.main import create_app
from api.routes import case_visit, data_protocol
from engine.doctor.case import CaseQuestion
from engine.doctor.standing import StandingAnswerStore, meaning_of
from engine.storage import LocalStorage
from engine.utils.time import utc_now

pytestmark = pytest.mark.integration


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _code(response: Any) -> str:
    return str(response.json()["detail"]["code"])


def test_every_route_has_its_policy() -> None:
    policies = all_policies()
    for key in (*data_protocol.POLICIES, *case_visit.POLICIES):
        assert key in policies


def test_a_case_item_gets_its_own_yes_with_its_exact_sentence(client: TestClient) -> None:
    from tests.integration.test_api_case_visit import GOAL, _answer_open
    from tests.integration.test_api_case_visit import _upload as upload_customers

    case = client.post("/cases", json={"upload_ids": [upload_customers(client)]}).json()
    case = _answer_open(client, client.post(f"/cases/{case['case_id']}/visit", json={}).json())
    case = _answer_open(
        client,
        client.post(
            f"/cases/{case['case_id']}/goal", json={"version": case["version"], "goal_id": GOAL}
        ).json(),
    )
    consent = [i for i in case["items"] if i["kind"] == "consent_column"]
    if not consent:
        pytest.skip("this goal's file names no consent column")
    item = consent[0]
    refused = client.post(
        f"/cases/{case['case_id']}/confirmations",
        json={"version": case["version"], "item_id": item["item_id"], "sentence": "Use it."},
    )
    assert refused.status_code == 409 and _code(refused) == "APPROVAL_SENTENCE_CHANGED"
    confirmed = client.post(
        f"/cases/{case['case_id']}/confirmations",
        json={"version": case["version"], "item_id": item["item_id"], "sentence": item["title"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    after = confirmed.json()
    yes = after["approvals"][-1]
    assert yes["approve_class"] and yes["role"] == "approver" and yes["text_shown"] == item["title"]
    assert next(i for i in after["items"] if i["item_id"] == item["item_id"])["state"] == "accepted"
    wrong = client.post(
        f"/cases/{case['case_id']}/approval", json={"version": after["version"], "sentence": "Prepare it."}
    )
    assert wrong.status_code == 409


def test_standing_answers_are_listed_and_revoked_per_client(client: TestClient, data_dir: Path) -> None:
    assert client.get("/clients/default/standing-answers").json() == []
    question = CaseQuestion(
        question_id="q.unknown_boolean.base.abc",
        code="UNKNOWN_BOOLEAN",
        column="opt_in",
        text="Do haa and nahi mean yes and no?",
        value_signature=("haa", "nahi"),
    )
    answer = StandingAnswerStore(LocalStorage(data_dir)).remember(
        "default",
        question,
        option_id="c0",
        effect_digest=None,
        meaning=meaning_of(None, {}),
        title="haa is yes",
        actor="u-asha",
        at=utc_now(),
    )
    (listed,) = client.get("/clients/default/standing-answers").json()
    assert listed["answer_id"] == answer.answer_id and listed["revoked_at"] is None
    assert client.get("/clients/other/standing-answers").json() == []
    revoked = client.post(f"/clients/default/standing-answers/{answer.answer_id}/revoke")
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
    missing = client.post("/clients/default/standing-answers/sa_000000000000000000000000/revoke")
    assert missing.status_code == 404 and _code(missing) == "STANDING_ANSWER_NOT_FOUND"
    assert client.get("/data-doctor/candidate-rules").json() == []
