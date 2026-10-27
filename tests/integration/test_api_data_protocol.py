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
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.integration.agent.test_api_session import _decide_everything, _session, _start, _upload

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


def test_guided_setup_approves_only_the_sentence_shown_and_a_remove_rows_edit_needs_it(
    client: TestClient, data_dir: Path
) -> None:
    upload_id = _upload(client, messy_frame())
    _start(client, upload_id)
    _decide_everything(client, upload_id)
    edited = client.post(
        f"/uploads/{upload_id}/agent-session/edits",
        json={
            "kind": "remove_rows",
            "column": "region",
            "params": {"operator": "equals", "values": ["TEST"]},
        },
    )
    session = _session(edited)
    assert session["status"] == "ready"
    client.post(f"/uploads/{upload_id}/agent-session/preview")
    shown = client.get(f"/uploads/{upload_id}/agent-session/approval")
    assert shown.status_code == 200, shown.text
    view = shown.json()
    assert view["preview_shown"] and view["reshape_missing"] == []
    (special,) = view["approve_class"]
    assert special["kind"] == "remove_rows" and special["role"] == "analyst"
    plain = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert plain.status_code == 409 and _code(plain) == "APPROVAL_SENTENCE_CHANGED"
    assert view["sentence"] in plain.json()["detail"]["message"], "the refusal carries the sentence to read"
    wrong = client.post(f"/uploads/{upload_id}/agent-session/approve", json={"sentence": "Prepare it."})
    assert wrong.status_code == 409 and _code(wrong) == "APPROVAL_SENTENCE_CHANGED"
    state = client.get(f"/uploads/{upload_id}/agent-session").json()["session"]
    assert state["status"] == "ready" and state["applied_upload_id"] is None, "a refusal records nothing"
    approved = client.post(f"/uploads/{upload_id}/agent-session/approve", json={"sentence": view["sentence"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["upload_id"] != upload_id
    assert (data_dir / "uploads" / upload_id / "agent" / "preview_shown.json").exists()


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


def test_guided_setup_keeps_nothing_of_an_approval_whose_ledger_entry_cannot_be_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: the approval's record comes first - before the client's memory of the recipe and the session."""
    from api.routes import agent as agent_routes
    from engine.agentic import config as agentic_config
    from engine.agentic.errors import AgenticError
    from engine.doctor import protocol

    remembered: list[str] = []
    monkeypatch.setattr(agent_routes, "remember", lambda *_a, **_k: remembered.append("recipe"))
    monkeypatch.setattr(agentic_config, "agentic_enabled", lambda _root=None: True)

    def broken(*_args: Any) -> Any:
        def record(kind: str, actor: str, body: Any) -> str:
            raise AgenticError("LEDGER_CHAIN_BROKEN", "The ledger could not be appended to.", status=503)

        return record

    monkeypatch.setattr(protocol, "ledger_port", broken)
    upload_id = _upload(client, messy_frame())
    _start(client, upload_id)
    _decide_everything(client, upload_id)
    client.post(f"/uploads/{upload_id}/agent-session/preview")
    view = client.get(f"/uploads/{upload_id}/agent-session/approval").json()
    refused = client.post(f"/uploads/{upload_id}/agent-session/approve", json={"sentence": view["sentence"]})
    assert refused.status_code == 503 and _code(refused) == "LEDGER_CHAIN_BROKEN", refused.text
    assert remembered == [], "no recipe is remembered for an approval that was not recorded"
    state = client.get(f"/uploads/{upload_id}/agent-session").json()["session"]
    assert state["status"] == "ready" and state["applied_upload_id"] is None
    recorded: list[str] = []
    monkeypatch.setattr(
        protocol, "ledger_port", lambda *_a: lambda kind, actor, body: recorded.append(kind) or "e" * 64
    )
    approved = client.post(f"/uploads/{upload_id}/agent-session/approve", json={"sentence": view["sentence"]})
    assert approved.status_code == 200, approved.text
    assert recorded == ["data.prescription.approved"] and remembered == [
        "recipe"
    ], "recorded, then remembered"
