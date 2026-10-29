"""The Data Doctor visit through the API (Plan O M235, DEC-1805).

A real upload, a real case, and the default ports: the visit goes generic -> goal -> approval -> M234's readiness
report on the stored file -> hand-off, with no AI service. Also the routes' policies, their refusals, the goal
brief route, and with the switch off no path at all.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_policy import all_policies
from api.main import create_app
from api.routes import case_visit
from engine import ai_service
from engine.access.roles import Role
from engine.agent.loop import UNAVAILABLE
from engine.doctor import case as doctor_case
from engine.doctor.goal_brief import load_goal_brief
from engine.doctor.visit import AI_NEEDED_REPLY
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.llm_http import PROBLEMS, LLMHttpError
from tests.fixtures.doctor_eval.chat_model import ChatScript
from tests.fixtures.make_data import GenerationSpec, generate
from tests.integration.doctor_wait import settled
from tests.integration.production.access_support import bearer, local_app, make_user

GOAL = "targeted-advertisement"
ROUTES = set(case_visit.POLICIES)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _upload(client: TestClient) -> str:
    frame = generate(GenerationSpec(use_case_id=GOAL, rows=1_500, seed=3))
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    response = client.post(
        "/uploads",
        files={"file": ("customers.csv", buffer.getvalue().encode(), "text/csv")},
        data={"use_case": GOAL, "mode": "train"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _code(response: Any) -> str:
    return str(response.json()["detail"]["code"])


def _answer_open(client: TestClient, case: dict[str, Any]) -> dict[str, Any]:
    """Answer each open blocking question with its first option, as a person picking would."""
    while case["state"] == "ask":
        open_ = next(q for q in case["questions"] if q["blocking"] and q["answer"] is None)
        response = client.post(
            f"/cases/{case['case_id']}/answers",
            json={"version": case["version"], "question_id": open_["question_id"], "option_id": "c0"},
        )
        assert response.status_code == 200, response.text
        case = response.json()
    return case


def test_a_visit_goes_from_generic_to_a_goal_to_a_readiness_verdict(
    client: TestClient, data_dir: Path
) -> None:
    upload_id = _upload(client)
    case = client.post("/cases", json={"upload_ids": [upload_id]}).json()
    case_id = case["case_id"]
    case = client.post(f"/cases/{case_id}/visit", json={}).json()
    assert case["state"] in {"ask", "approve"} and case["goals"] == []
    assert case["diagnosis"]["use_cases"], "generic mode says what the file can power"
    case = _answer_open(client, case)
    assert case["state"] == "approve"

    chatted = client.post(f"/cases/{case_id}/chat", json={"version": case["version"], "message": "Hello"})
    assert chatted.status_code == 200, chatted.text
    case = chatted.json()
    assert [m["role"] for m in case["chat"]] == ["user", "agent"] and case["state"] == "approve"

    chosen = client.post(
        f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"}
    )
    assert chosen.status_code == 200, chosen.text
    case = _answer_open(client, chosen.json())
    case = settled(client, case_id)  # the whole-file check is in the case, so a retry has nothing to take in
    assert [g["brief_hash"] for g in case["goals"]] == [load_goal_brief(GOAL).brief_hash]
    outcome = [i for i in case["items"] if i["kind"] == "outcome_column"]
    assert outcome and outcome[0]["goal_id"] == GOAL and outcome[0]["state"] == "accepted"
    again = client.post(f"/cases/{case_id}/goal", json={"version": 1, "goal_id": GOAL, "op_id": "g1"})
    assert again.status_code == 200 and again.json() == case, "a retry changes nothing"

    case = settled(client, case_id)
    approved = client.post(f"/cases/{case_id}/approval", json={"version": case["version"]})
    assert approved.status_code == 200, approved.text
    case = approved.json()
    (readiness,) = case["readiness"]
    assert readiness["goal_id"] == GOAL and readiness["verdict"]
    stored = list((data_dir / "cases" / "default" / case_id / "readiness").glob(f"{GOAL}_*.json"))
    assert stored, "M234's report is kept in the case"
    events = client.get(f"/cases/{case_id}/timeline").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "readiness.checked" in kinds and "approval" in kinds and "prescription.saved" in kinds
    if readiness["verdict"] in {"ready", "ready_with_warnings"}:
        assert case["state"] == "follow_up" and case["visit"]["hand_off"]["target"] == "runs"
    else:
        assert case["state"] == "approve", "not ready: back to the person with the reason"


def test_a_file_added_after_approval_is_bound_to_the_prescription_and_reopens_the_visit(
    client: TestClient, data_dir: Path
) -> None:
    from engine.doctor.prescription_runs import bound
    from engine.storage import LocalStorage

    case_id = client.post("/cases", json={"upload_ids": [_upload(client)]}).json()["case_id"]
    case = _answer_open(client, client.post(f"/cases/{case_id}/visit", json={}).json())
    case = _answer_open(
        client,
        client.post(f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL}).json(),
    )
    case = settled(client, case_id)
    case = client.post(f"/cases/{case_id}/approval", json={"version": case["version"]}).json()
    assert case["prescriptions"], "approval stored a prescription"
    storage = LocalStorage(data_dir)
    first = case["files"][0]["upload_id"]
    assert bound(storage, upload_id=first) is not None

    second = _upload(client)
    assert bound(storage, upload_id=second) is None
    added = client.post(f"/cases/{case_id}/files", json={"version": case["version"], "upload_id": second})
    assert added.status_code == 200, added.text
    ref = bound(storage, upload_id=second)
    assert ref is not None and ref.prescription_hash == case["prescriptions"][-1]["prescription_hash"]
    events = client.get(f"/cases/{case_id}/timeline").json()["events"]
    after = events[[e["upload_id"] for e in events].index(second) :]
    reopened = [e for e in after if e["kind"] == "state.changed" and e["details"].get("to") == "intake"]
    assert reopened, "the added file sends the visit back to intake"
    assert (
        added.json()["version"] == case["version"] + 1 and added.json()["state"] == "intake"
    ), "adding a file only writes it: the visit does not run until it is asked"


class _Killed(BaseException):
    """The process dies during the AI call: nothing after it runs."""


class _DiesOnce(FakeLLMClient):
    """A connected AI service whose first call never returns (the process is killed while it waits)."""

    died = False

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        if not type(self).died:
            type(self).died = True
            raise _Killed
        return super().complete(*args, **kwargs)


def test_a_chat_whose_process_died_during_the_ai_call_is_answered_by_the_ai_on_resume(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process is killed while the AI service is answering a chat (the case rests in CONVERSE). A new process
    on the same data resumes the visit with the AI service still connected: the AI answers the message."""
    built: list[_DiesOnce] = []

    def build(*_args: Any, **_kwargs: Any) -> FakeLLMClient:
        built.append(_DiesOnce(mode=FakeLLMMode.GROUNDED))
        return built[-1]

    monkeypatch.setattr(ai_service, "build_client", build)
    monkeypatch.setattr(_DiesOnce, "died", False)
    first = TestClient(create_app(config_root=config_root, data_dir=data_dir))
    first.__enter__()  # never left: the process dies
    case = first.post("/cases", json={"upload_ids": [_upload(first)]}).json()
    case = _answer_open(first, first.post(f"/cases/{case['case_id']}/visit", json={}).json())
    case_id = case["case_id"]
    assert (
        first.get(f"/cases/{case_id}/ai").json()["consent"] is False
    ), "the AI is not asked until the client says yes"
    given = first.post(f"/cases/{case_id}/ai-consent", json={})
    assert given.status_code == 200 and given.json()["consent"] is True and given.json()["given_by"]
    with pytest.raises(_Killed):
        first.post(f"/cases/{case_id}/chat", json={"version": case["version"], "message": "What is here?"})
    asked = len(built)

    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as second:
        died = second.get(f"/cases/{case_id}").json()
        assert died["state"] == "converse" and died["chat"][-1]["role"] == "user"
        resumed = second.post(f"/cases/{case_id}/visit", json={})
    assert resumed.status_code == 200, resumed.text
    case = resumed.json()
    assert [m["role"] for m in case["chat"]] == ["user", "agent"]
    assert case["chat"][-1]["text"] != AI_NEEDED_REPLY, "the AI service answered, not 'none is connected'"
    assert any(c.calls for c in built[asked:]), "the resumed visit asked the AI service"
    assert case["state"] in {"ask", "approve"} and case["counters"]["model_calls"] >= 1


class _KeyExpired(FakeLLMClient):
    """A connected AI service whose key has expired: every call fails the way the HTTP client reports it."""

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise LLMHttpError("key_expired", status=401)


def test_a_chat_the_ai_service_could_not_answer_says_why_in_the_response_and_the_timeline(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: _KeyExpired(mode=FakeLLMMode.GROUNDED))
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        case = client.post("/cases", json={"upload_ids": [_upload(client)]}).json()
        case = _answer_open(client, client.post(f"/cases/{case['case_id']}/visit", json={}).json())
        case_id = case["case_id"]
        assert client.post(f"/cases/{case_id}/ai-consent", json={}).status_code == 200
        chatted = client.post(f"/cases/{case_id}/chat", json={"version": case["version"], "message": "Hello"})
        assert chatted.status_code == 200, chatted.text
        reply = chatted.json()["chat"][-1]
        sentence, fix = PROBLEMS["key_expired"]
        assert reply["text"] == f"{UNAVAILABLE} {sentence} {fix}"
        assert reply["blocked_by"] == "unavailable"
        assert reply["error_codes"] == ["LLM_UNAVAILABLE", "LLM_KEY_EXPIRED"]
        events = client.get(f"/cases/{case_id}/timeline").json()["events"]
        agent = [e for e in events if e["kind"] == "chat.message" and e["details"].get("role") == "agent"]
        (event,) = [e for e in agent if not e["details"].get("review")]
        assert event["details"]["blocked_by"] == "unavailable"
        assert event["details"]["error_codes"] == "LLM_UNAVAILABLE,LLM_KEY_EXPIRED"
        assert event["details"]["ai"] is True
        # The review that followed the person's yes failed the same way: the timeline records it, and the chat,
        # which had told the person already, does not say it twice.
        (review,) = [e for e in agent if e["details"].get("review")]
        assert review["details"]["error_codes"] == "LLM_UNAVAILABLE,LLM_KEY_EXPIRED"
        assert [m["role"] for m in chatted.json()["chat"]] == ["user", "agent"]


def test_the_chat_through_the_api_applies_a_filter_for_the_person_who_can_undo_it(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scripted model drives the real route: a look, a filter the guards pass (applied by the agent with its counts,
    decided by `agent:data-doctor`), a reply; the person unticks it and it is theirs from then on. Evidence of the look
    is stored, so the next message can cite it."""

    def step(seen: Any) -> dict[str, Any]:
        return {
            "action": "propose_step",
            "file": "F1",
            "args": {
                "kind": "remove_rows",
                "column": "customer_id",
                "params": {"operator": "starts_with", "values": ["C-1048"]},
                "reason": "You asked for this.",
                "evidence_ids": [seen.last("evidence_id")],
            },
        }

    script = ChatScript(
        {
            "drop the 1048": [
                {
                    "action": "find_values",
                    "file": "F1",
                    "args": {"column": "customer_id", "contains": "1048"},
                },
                step,
                {"action": "reply", "text": "Done: I left out the customers whose id starts with C-1048."},
            ],
            "what did you": [{"action": "reply", "text": "I looked at the customer ids."}],
        }
    )
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: script)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        case = client.post("/cases", json={"upload_ids": [_upload(client)]}).json()
        case = _answer_open(client, client.post(f"/cases/{case['case_id']}/visit", json={}).json())
        case_id = case["case_id"]
        assert client.post(f"/cases/{case_id}/ai-consent", json={}).status_code == 200
        chatted = client.post(
            f"/cases/{case_id}/chat", json={"version": case["version"], "message": "drop the 1048s"}
        )
        assert chatted.status_code == 200, chatted.text
        case = chatted.json()
        reply = case["chat"][-1]
        (item,) = [i for i in case["items"] if i["source"] == "ai"]
        assert reply["item_ids"] == [item["item_id"]] and reply["tools"] == ["find_values", "propose_step"]
        assert "withdrawn" not in reply and "suggestion" not in reply, "left out of the JSON while empty"
        assert item["state"] == "accepted" and item["decided_by"] == "agent:data-doctor"
        assert item["payload"]["tried"]["changed"] > 0 and item["payload"]["tried"]["rows"] == 1_000
        assert case["counters"]["model_calls"] >= 3, "the chat's three calls are counted on the case"

        undone = client.post(
            f"/cases/{case_id}/decisions",
            json={"version": case["version"], "item_id": item["item_id"], "accept": False},
        )
        assert undone.status_code == 200, undone.text
        mine = next(i for i in undone.json()["items"] if i["item_id"] == item["item_id"])
        assert mine["state"] == "rejected" and mine["decided_by"] != "agent:data-doctor"

        later = client.post(
            f"/cases/{case_id}/chat",
            json={"version": undone.json()["version"], "message": "what did you look at?"},
        ).json()
        seen = script.seen[-1]
        assert seen.state["earlier_looks"] == [
            {"id": "e1", "tool": "find_values", "file": "F1", "column": "customer_id"}
        ], "the look of the earlier turn is back from the case's evidence"
        assert [h["who"] for h in seen.history] == ["person", "you"]
        assert later["chat"][-1]["text"] == "I looked at the customer ids."


def test_refusals_answer_their_codes(client: TestClient) -> None:
    case = client.post("/cases", json={"upload_ids": [_upload(client)]}).json()
    case = client.post(f"/cases/{case['case_id']}/visit", json={}).json()
    case_id, version = case["case_id"], case["version"]
    stale = client.post(f"/cases/{case_id}/goal", json={"version": version - 1, "goal_id": GOAL})
    assert stale.status_code == 409 and _code(stale) == "CASE_VERSION_CONFLICT"
    unknown = client.post(
        f"/cases/{case_id}/answers", json={"version": version, "question_id": "q.x", "option_id": "c0"}
    )
    assert unknown.status_code == 422 and _code(unknown) == "VISIT_UNKNOWN_CHOICE"
    if case["state"] != "approve":
        waiting = client.post(f"/cases/{case_id}/approval", json={"version": version})
        assert waiting.status_code == 409 and _code(waiting) == "VISIT_NOT_WAITING"
    no_goal = client.post(f"/cases/{case_id}/goal", json={"version": version, "goal_id": "no-such-goal"})
    assert no_goal.status_code == 404 and _code(no_goal) == "USE_CASE_NOT_FOUND"
    missing = client.post("/cases/case_0000000000000000/visit", json={})
    assert missing.status_code == 404 and _code(missing) == "CASE_NOT_FOUND"


def test_a_step_can_be_parked_for_someone_who_would_know_and_resumed(client: TestClient) -> None:
    case = client.post("/cases", json={"upload_ids": [_upload(client)]}).json()
    case_id = case["case_id"]
    case = _answer_open(client, client.post(f"/cases/{case_id}/visit", json={}).json())
    item = next(i for i in case["items"] if i["state"] == "accepted")

    def decide(body: dict[str, Any]) -> Any:
        return client.post(f"/cases/{case_id}/decisions", json=body)

    parked = decide(
        {"version": case["version"], "item_id": item["item_id"], "accept": False, "park_for": "finance"}
    )
    assert parked.status_code == 200, parked.text
    held = next(i for i in parked.json()["items"] if i["item_id"] == item["item_id"])
    assert (
        held["state"] == "rejected" and held["parked_for"] == "finance"
    ), "left out, and waiting for finance"
    version = parked.json()["version"]
    ticked = decide({"version": version, "item_id": item["item_id"], "accept": True, "park_for": "finance"})
    assert (
        ticked.status_code == 422 and _code(ticked) == "VISIT_UNKNOWN_CHOICE"
    ), "a ticked step is not parked"
    stranger = decide(
        {"version": version, "item_id": item["item_id"], "accept": False, "park_for": "the intern"}
    )
    assert stranger.status_code == 422 and _code(stranger) == "VISIT_UNKNOWN_CHOICE"
    resumed = decide({"version": version, "item_id": item["item_id"], "accept": False})
    assert resumed.status_code == 200, resumed.text
    again = next(i for i in resumed.json()["items"] if i["item_id"] == item["item_id"])
    assert again["state"] == "rejected" and "parked_for" not in again, "any later decision resumes it"


def test_the_goal_brief_route(client: TestClient) -> None:
    found = client.get(f"/goal-briefs/{GOAL}")
    assert found.status_code == 200 and found.json()["brief_hash"] == load_goal_brief(GOAL).brief_hash
    scored = client.get(f"/goal-briefs/{GOAL}", params={"mode": "score"})
    assert scored.json()["mode"] == "score"
    assert client.get("/goal-briefs/no-such-goal").status_code == 404


def test_the_routes_have_their_policies(client: TestClient) -> None:
    policies = all_policies()
    assert set(policies) >= ROUTES
    for key in ROUTES:
        expected = Role.VIEWER if key[0] == "GET" else Role.ANALYST
        assert policies[key].role is expected, key
    paths = set(client.app.openapi()["paths"])  # type: ignore[attr-defined]
    assert {path for _method, path in ROUTES} <= paths


def test_a_viewer_reads_a_brief_but_cannot_move_a_visit(tmp_path: Path) -> None:
    app = local_app(tmp_path)
    viewer = make_user(app, "vera", [Role.VIEWER])
    analyst = make_user(app, "asha", [Role.ANALYST])
    with TestClient(app, raise_server_exceptions=False) as client:
        case = client.post("/cases", json={}, headers=bearer(app, analyst)).json()
        refused = client.post(
            f"/cases/{case['case_id']}/goal",
            json={"version": 1, "goal_id": GOAL},
            headers=bearer(app, viewer),
        )
        assert refused.status_code == 403 and _code(refused) == "ROLE_REQUIRED"
        assert client.get(f"/goal-briefs/{GOAL}", headers=bearer(app, viewer)).status_code == 200
        moved = client.post(f"/cases/{case['case_id']}/visit", json={}, headers=bearer(app, analyst))
        assert moved.status_code == 200 and moved.json()["visit"]["waiting_for"] == "files"
        events = client.get(f"/cases/{case['case_id']}/timeline", headers=bearer(app, viewer)).json()[
            "events"
        ]
        assert {e["actor"] for e in events} == {analyst}, "the timeline records the user id"


def test_an_answer_value_is_refused_where_the_option_takes_none(client: TestClient, data_dir: Path) -> None:
    upload_id = _upload(client)
    case = client.post("/cases", json={"upload_ids": [upload_id]}).json()
    case = client.post(f"/cases/{case['case_id']}/visit", json={}).json()
    open_ = next(q for q in case["questions"] if q["answer"] is None and q["options"])
    response = client.post(
        f"/cases/{case['case_id']}/answers",
        json={
            "version": case["version"],
            "question_id": open_["question_id"],
            "option_id": open_["options"][0]["option_id"],
            "value": "2011-11-30",
        },
    )
    assert response.status_code == 422 and _code(response) == "VISIT_ANSWER_VALUE_INVALID"


def test_a_typed_date_is_accepted_over_http(client: TestClient, data_dir: Path) -> None:
    from engine.doctor.protocol import _case_question, _with_dont_know
    from engine.doctor.questions import build_ask
    from engine.storage import LocalStorage

    upload_id = _upload(client)
    case = client.post("/cases", json={"upload_ids": [upload_id]}).json()
    ask = build_ask("Q8", {"latest_date": "2011-12-09"}, rows=100)
    q8 = _case_question(ask, "q8", _with_dont_know(ask)).model_copy(update={"code": "Q08_AS_OF"})
    store = doctor_case.CaseStore(LocalStorage(data_dir))
    store.update(
        "default",
        case["case_id"],
        expected_version=case["version"],
        actor="test",
        change=lambda c: (c.model_copy(update={"questions": (*c.questions, q8)}), ()),
    )
    fresh = client.get(f"/cases/{case['case_id']}").json()
    typed = next(o for o in q8.options if o.value_kind == "date")
    response = client.post(
        f"/cases/{case['case_id']}/answers",
        json={
            "version": fresh["version"],
            "question_id": "q8",
            "option_id": typed.option_id,
            "value": "2011-11-30",
        },
    )
    assert response.status_code == 200, response.text
    answered = next(q for q in response.json()["questions"] if q["question_id"] == "q8")
    assert answered["answer_value"] == "2011-11-30"


def test_an_invoice_log_with_an_ai_query_reaches_a_computed_readiness_verdict_on_the_prepared_table(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay blocker end to end: an invoice log (key = invoice line), the AI's query makes one row per customer
    with `ordered_again_90d`; the person says yes to the table and to the outcome; the readiness check is computed
    on that table, with the customer id (not the invoice id) as its key."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(5)
    customers = 900
    lines = pd.DataFrame(
        {
            "invoice_id": [f"I{n}" for n in range(customers * 2)],
            "customer_id": [f"C{n % customers}" for n in range(customers * 2)],
            "invoice_date": ["2026-06-01"] * customers + ["2026-08-15"] * customers,
            "amount": np.round(rng.gamma(2.0, 40.0, customers * 2), 2),
        }
    )
    again = rng.random(customers) < 0.4
    lines.loc[customers:, "invoice_date"] = np.where(
        np.concatenate([again, again]), "2026-08-15", "2026-06-02"
    )[customers:]
    sql = (
        "SELECT customer_id, "
        "ROUND(SUM(CASE WHEN invoice_date <= CAST($as_of AS VARCHAR) THEN amount ELSE 0 END), 2) AS total_spend, "
        "CASE WHEN MAX(invoice_date) > CAST($as_of AS VARCHAR) THEN 1 ELSE 0 END AS ordered_again_90d "
        "FROM t_invoices GROUP BY customer_id"
    )

    buffer = io.StringIO()
    lines.to_csv(buffer, index=False)
    holder: dict[str, str] = {}

    script_steps: list[Any] = [
        lambda _seen: {
            "action": "propose_sql",
            "args": {
                "description": "One row per customer with whether they ordered again in the 90 days after",
                "sql": sql,
                "output_name": "customer_training",
                "files": ["F1"],
                "as_of": "2026-08-01",
                # the outcome is read from the rows after the as-of date: said so, or the future-data check (now run
                # on the whole plan before the person approves) stops the plan
                "reads_future": True,
                "outcome_window_days": 14,
                "expect": {"keys_unique": True, "max_keys_lost_pct": 0},
            },
        },
        {"action": "reply", "text": "I built one row per customer."},
    ]
    script = ChatScript({"build": script_steps})
    monkeypatch.setattr(ai_service, "build_client", lambda *_a, **_k: script)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        response = client.post(
            "/uploads",
            files={"file": ("invoices.csv", buffer.getvalue().encode(), "text/csv")},
            data={"use_case": "retail-win-back", "mode": "train"},
        )
        assert response.status_code == 201, response.text
        holder["upload_id"] = str(response.json()["upload_id"])
        case = client.post("/cases", json={"upload_ids": [holder["upload_id"]]}).json()
        case = _answer_open(client, client.post(f"/cases/{case['case_id']}/visit", json={}).json())
        case_id = case["case_id"]
        assert client.post(f"/cases/{case_id}/ai-consent", json={}).status_code == 200
        chatted = client.post(
            f"/cases/{case_id}/chat", json={"version": case["version"], "message": "build it"}
        )
        assert chatted.status_code == 200, chatted.text
        case = chatted.json()
        ai_items = [i for i in case["items"] if i["source"] == "ai"]
        assert len(ai_items) == 1
        made = ai_items[0]
        assert made["payload"]["columns"] == ["customer_id", "total_spend", "ordered_again_90d"]
        assert (
            "ordered_again_90d" in made["payload"]["two_valued"]
            and "total_spend" not in made["payload"]["two_valued"]
        )
        case = client.post(
            f"/cases/{case_id}/goal",
            json={"version": case["version"], "goal_id": "retail-win-back", "op_id": "g"},
        ).json()
        asked = next(q for q in case["questions"] if q["code"] == "OUTCOME_COLUMN" and q["answer"] is None)
        labels = [o["label"] for o in asked["options"]]
        assert labels == ["ordered_again_90d", "None of these"], labels
        if made["state"] != "accepted":
            case = client.post(
                f"/cases/{case_id}/decisions",
                json={"version": case["version"], "item_id": made["item_id"], "accept": True},
            ).json()
        case = client.post(
            f"/cases/{case_id}/answers",
            json={"version": case["version"], "question_id": asked["question_id"], "option_id": "c0"},
        ).json()
        case = _answer_open(client, case)
        screen = client.get(f"/cases/{case_id}/screen").json()
        ready = next(c for c in screen["view"]["conversation"] if c["type"] == "ready")
        for yes in ready["confirmations"]:
            confirmed = client.post(
                f"/cases/{case_id}/confirmations",
                json={"version": case["version"], "item_id": yes["item_id"], "sentence": yes["sentence"]},
            )
            assert confirmed.status_code == 200, confirmed.text
            case = confirmed.json()
        case = settled(client, case_id)
        sentence = client.get(f"/cases/{case_id}/screen").json()["view"]["approval_sentence"]
        approved = client.post(
            f"/cases/{case_id}/approval", json={"version": case["version"], "sentence": sentence}
        )
        assert approved.status_code == 200, approved.text
        readiness = approved.json()["readiness"]
        assert readiness and readiness[-1]["goal_id"] == "retail-win-back" and readiness[-1]["verdict"]


def test_one_approval_call_takes_the_screens_yeses_and_digest_and_refuses_a_stale_one(
    client: TestClient,
) -> None:
    upload_id = _upload(client)
    case = client.post("/cases", json={"upload_ids": [upload_id]}).json()
    case_id = case["case_id"]
    case = _answer_open(client, client.post(f"/cases/{case_id}/visit", json={}).json())
    chosen = client.post(
        f"/cases/{case_id}/goal", json={"version": case["version"], "goal_id": GOAL, "op_id": "g1"}
    )
    case = _answer_open(client, chosen.json())
    case = settled(client, case_id)
    shown = client.get(f"/cases/{case_id}/screen").json()["view"]["approval"]
    assert shown is not None and shown["digest"]
    stale = client.post(
        f"/cases/{case_id}/approval", json={"version": case["version"], "digest": "0" * 32, "yeses": []}
    )
    assert stale.status_code == 409 and _code(stale) == "APPROVAL_SENTENCE_CHANGED"
    unseen = client.post(
        f"/cases/{case_id}/approval",
        json={
            "version": case["version"],
            "yeses": [{"kind": "confirm", "item_id": "nope", "sentence": "x"}],
            "digest": shown["digest"],
        },
    )
    assert unseen.status_code == 200, "a yes to something not shown is not given"
    assert unseen.json()["approvals"]
