"""Plan G M77 hardening: the bugs the security and correctness review found, each pinned by a test."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import agent as agent_routes
from api.routes import runs
from api.routes.agent_recipes import write_derived_upload
from api.routes.uploads import load_upload
from engine.agent.contracts import (
    RECIPE_RECEIPT_FILENAME,
    AgentSession,
    ChatMessage,
    ChatRole,
    DataRecipe,
    RecipeStep,
    RecipeStepKind,
    recipe_hash,
)
from engine.agent.loop import OUT_OF_BUDGET
from engine.config import load_use_case
from engine.generative.contracts import LlmUsageReport
from engine.jobs import CancelToken
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.storage import LocalStorage, upload_key
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.integration.agent.test_recipe_runs import KEY, USE_CASE, _messy, _recipe, _seed_model

pytestmark = pytest.mark.integration


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _upload(client: TestClient, frame: Any, mode: str = "train") -> str:
    response = client.post(
        "/uploads",
        files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _session(response: Any) -> dict[str, Any]:
    assert response.status_code in {200, 201}, response.text
    return dict(response.json()["session"])


def test_approving_a_scoring_file_checks_it_as_the_models_recipe_prepares_it(
    client: TestClient, data_dir: Path
) -> None:
    """Score mode: Approve used to check the file as sent against a schema trained on prepared data, so
    every scoring file of a recipe model was refused with SCHEMA_MISMATCH; Run itself replays first."""
    _seed_model(data_dir, _recipe(_messy(), target=None))
    upload_id = _upload(client, _messy("scoring", rows=600), mode="score")
    session = _session(client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE}))
    assert session["status"] == "needs_review"
    _session(client.post(f"/uploads/{upload_id}/agent-session/decisions", json={"accept_recommended": True}))
    applied = client.post(f"/uploads/{upload_id}/agent-session/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    run = client.post(
        "/runs",
        json={"use_case": USE_CASE, "mode": "score", "upload_id": body["upload_id"], "primary_key": KEY},
    )
    assert run.status_code == 202, run.text


def test_a_prepared_scoring_file_is_prepared_again_from_its_original_for_a_new_recipe(
    client: TestClient, data_dir: Path
) -> None:
    """A scoring upload prepared by one recipe, scored by a model with another, is prepared from the
    file the person sent - never by running the second recipe on top of the first one's output."""
    storage = LocalStorage(data_dir)
    scoring = _messy("scoring", rows=600)
    original = load_upload(storage, _upload(client, scoring, mode="score"))
    first = _recipe(scoring, target=None)
    hide_steps = (
        *first.steps,
        RecipeStep(order=3, kind=RecipeStepKind.DROP_COLUMN, column="region"),
    )
    hiding = first.model_copy(
        update={"recipe_id": "rec0", "steps": hide_steps, "recipe_hash": recipe_hash(hide_steps)}
    )
    DataRecipe.model_validate(hiding.model_dump())
    prepared = write_derived_upload(storage, load_use_case(USE_CASE), original, hiding)
    _seed_model(data_dir, first)  # the model in use prepares with `first`, which reads `region`
    run = client.post(
        "/runs",
        json={
            "use_case": USE_CASE,
            "mode": "score",
            "upload_id": prepared.record.upload_id,
            "primary_key": KEY,
        },
    )
    assert run.status_code == 202, run.text


def test_a_viewer_can_read_a_session_but_not_chat_decide_or_approve(
    config_root: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import access_policy

    policies = {
        (method, path): policy
        for (method, path), policy in access_policy.all_policies().items()
        if "agent-session" in path or path.endswith("/checks")
    }
    assert len(policies) == 8
    for (method, path), policy in policies.items():
        expected = "viewer" if method == "GET" else "analyst"
        assert policy.role.value == expected, (method, path)
    del config_root, data_dir, monkeypatch


def test_what_a_person_types_is_masked_before_it_is_stored_or_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLMClient(mode=FakeLLMMode.GROUNDED)
    monkeypatch.setattr(agent_routes, "build_client", lambda *_a, **_k: fake)
    upload_id = _upload(client, messy_frame())
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    session = _session(
        client.post(
            f"/uploads/{upload_id}/agent-session/messages",
            json={"text": "Why hide asha.rao@example.com's row? Call +91 98765 43210."},
        )
    )
    said = session["transcript"][0]["text"]
    assert "asha.rao@example.com" not in said and "98765 43210" not in said
    assert "[REDACTED:" in said
    assert fake.calls
    prompts = " ".join(f"{call.system} {call.prompt}" for call in fake.calls)
    assert "asha.rao@example.com" not in prompts and "98765 43210" not in prompts


def test_llm_usage_adds_up_over_the_whole_session(client: TestClient, data_dir: Path) -> None:
    upload_id = _upload(client, messy_frame())
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    for text in ("What about 'monthly_spend'?", "What about 'region'?"):
        _session(client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": text}))
    storage = LocalStorage(data_dir)
    usage = storage.read_model(upload_key(upload_id, "agent/llm_usage.json"), LlmUsageReport)
    session = storage.read_model(upload_key(upload_id, "agent/agent_session.json"), AgentSession)
    assert usage.totals.calls == session.llm_calls == 4


def test_the_chat_is_bounded_once_the_budget_is_spent(client: TestClient, data_dir: Path) -> None:
    """With the model budget spent, each message still added two lines "out of budget" for ever."""
    upload_id = _upload(client, messy_frame())
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    storage = LocalStorage(data_dir)
    key = upload_key(upload_id, "agent/agent_session.json")
    spent = storage.read_model(key, AgentSession)
    limit = load_use_case(USE_CASE).agent.max_llm_calls_per_session
    ceiling = 2 * (limit + agent_routes.CHAT_GRACE_TURNS)
    line = ChatMessage(role=ChatRole.USER, text="Hello?", created_at=spent.created_at)
    storage.write_model(
        key, spent.model_copy(update={"llm_calls": limit, "transcript": (line,) * (ceiling - 2)})
    )
    last = client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "Hello?"})
    assert _session(last)["transcript"][-1]["text"] == OUT_OF_BUDGET
    full = client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "Hello?"})
    assert full.status_code == 409
    assert full.json()["detail"]["code"] == "AGENT_CHAT_FULL"
    assert len(storage.read_model(key, AgentSession).transcript) == ceiling


def test_concurrent_chat_turns_cannot_spend_the_budget_twice(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two messages at once used to read the same `llm_calls`, both spend it, and the last write won."""
    import threading

    upload_id = _upload(client, messy_frame())
    client.post(f"/uploads/{upload_id}/agent-session", json={"use_case": USE_CASE})
    real_turn = agent_routes.chat_turn
    barrier = threading.Barrier(2, timeout=2)

    def slow_turn(*args: Any, **kwargs: Any) -> Any:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()  # never met while the session lock serialises the two turns
        return real_turn(*args, **kwargs)

    monkeypatch.setattr(agent_routes, "chat_turn", slow_turn)
    results: list[int] = []

    def send() -> None:
        results.append(
            client.post(f"/uploads/{upload_id}/agent-session/messages", json={"text": "Hi"}).status_code
        )

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [200, 200]
    session = LocalStorage(data_dir).read_model(
        upload_key(upload_id, "agent/agent_session.json"), AgentSession
    )
    assert len(session.transcript) == 4  # without the lock the second write dropped the first turn
    assert session.llm_calls == 2  # one call per "Hi", both counted


def test_a_receipt_names_the_file_a_prepared_upload_came_from(client: TestClient, data_dir: Path) -> None:
    storage = LocalStorage(data_dir)
    frame = _messy()
    original = load_upload(storage, _upload(client, frame))
    derived = write_derived_upload(storage, load_use_case(USE_CASE), original, _recipe(frame))
    receipt = storage.read_bytes(upload_key(derived.record.upload_id, RECIPE_RECEIPT_FILENAME))
    assert json.loads(receipt)["upload_id"] == original.upload_id


@pytest.mark.parametrize("upload_id", ["%2E%2E", "%2e", "a%5Cb"])
def test_a_session_id_the_store_refuses_is_404_not_500(client: TestClient, upload_id: str) -> None:
    response = client.get(f"/uploads/{upload_id}/agent-session")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "AGENT_SESSION_NOT_FOUND"


def test_a_combine_preview_takes_whole_entities_not_the_first_rows() -> None:
    """The preview samples 1,000 rows; combining part of an entity's rows would show wrong totals."""
    import pandas as pd

    keys = [f"S{i % 1_500:05d}" for i in range(6_000)]  # every shopper on 4 rows, spread through the file
    frame = pd.DataFrame({"customer_id": keys, "order_value": range(6_000)})
    steps = (RecipeStep(order=1, kind=RecipeStepKind.COMBINE_ROWS, column="customer_id", params={}),)
    recipe = _recipe(_messy(rows=600)).model_copy(update={"steps": steps, "recipe_hash": recipe_hash(steps)})
    sample = agent_routes._preview_sample(frame, recipe)
    assert sample["customer_id"].nunique() == agent_routes.PREVIEW_SAMPLE_ROWS
    assert (sample.groupby("customer_id").size() == 4).all()
    assert len(agent_routes._preview_sample(frame, None)) == agent_routes.PREVIEW_SAMPLE_ROWS
