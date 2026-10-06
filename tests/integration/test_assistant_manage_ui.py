"""The AI Onboarding Assistant's Plan I additions in jsdom, against REAL API responses (DEC-1270…1279).

The node test (`production/ui/generative/assistant.test.mjs`, sharing that directory's jsdom install
as `measure/` does) renders `ui/modules/generative/assistant.js`'s Results view of a graded index and
drives what Plan I added to it: a citation that opens its passage, feedback on an answer, "Add to
test questions", "Update documents", deleting a version and the compare view. Every body its fake API
answers with was answered by the app below, over the bundled sample corpus and the fake AI backend.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "generative"
USE_CASE: Final[str] = "ai-onboarding-assistant"
QUESTION: Final[str] = "What is the late payment fee?"


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _wait(client: TestClient, index_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        detail: dict[str, Any] = _ok(client.get(f"/indexes/{index_id}"))
        if detail["status"]["state"] in ("done", "failed"):
            assert detail["status"]["state"] == "done", detail["status"]
            return detail
        assert time.monotonic() < deadline, "the index did not finish"
        time.sleep(0.05)


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        built = _ok(
            client.post(
                f"/use-cases/{USE_CASE}/indexes",
                data={
                    "use_sample_documents": "true",
                    "use_sample_questions": "true",
                    "model_choice": "__automl__",
                    "overrides": "{}",
                },
            ),
            202,
        )
        first = built["index_id"]
        bodies["detail"] = _wait(client, first)
        bodies["answer"] = _ok(client.post(f"/indexes/{first}/ask", json={"question": QUESTION}))
        cited = bodies["answer"]["citations"][0]["chunk_id"]
        bodies["chunk"] = _ok(client.get(f"/indexes/{first}/chunks/{cited}"))
        neighbour = bodies["chunk"]["next_chunk_id"] or bodies["chunk"]["previous_chunk_id"]
        bodies["chunk_neighbour"] = _ok(client.get(f"/indexes/{first}/chunks/{neighbour}"))
        bodies["feedback_saved"] = _ok(
            client.post(
                f"/indexes/{first}/feedback",
                json={
                    "rating": "down",
                    "question": QUESTION,
                    "answer": bodies["answer"]["answer"],
                    "comment": "Ask asha.verma@example.com, she knows the fee.",
                },
            ),
            201,
        )
        bodies["feedback_list"] = _ok(client.get(f"/indexes/{first}/feedback"))
        bodies["feedback_empty"] = {"index_id": first, "up": 0, "down": 0, "entries": []}
        csv = client.get(f"/indexes/{first}/feedback/test-questions.csv")
        assert csv.status_code == 200, csv.text
        bodies["test_questions_csv"] = {"text": csv.text}
        removed = bodies["detail"]["manifest"]["documents"][0]["name"]
        bodies["update_started"] = _ok(
            client.post(f"/indexes/{first}/update", data={"remove": [removed]}), 202
        )
        second = bodies["update_started"]["index_id"]
        bodies["detail_updated"] = _wait(client, second)
        bodies["indexes"] = _ok(client.get(f"/use-cases/{USE_CASE}/indexes"))
        bodies["comparison"] = _ok(client.get(f"/indexes/{first}/compare/{second}"))
        champion = next(row["index_id"] for row in bodies["indexes"]["indexes"] if row["champion"])
        refused = client.delete(f"/indexes/{champion}")
        assert refused.status_code == 409, refused.text
        bodies["delete_refused"] = refused.json()
        bodies["ids"] = {"first": first, "second": second, "champion": champion, "removed": removed}
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def fixtures(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_fixtures(tmp_path_factory.mktemp("assistant-ui"), config_root)


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_the_fixtures_are_the_world_the_screen_is_tested_in(fixtures: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    detail = _read(fixtures, "detail")
    assert detail["grade"] is not None and detail["rag_eval"] is not None
    answer = _read(fixtures, "answer")
    assert answer["refused"] is False and answer["citations"]
    chunk = _read(fixtures, "chunk")
    assert chunk["chunk_id"] == answer["citations"][0]["chunk_id"]
    assert "asha.verma@example.com" not in json.dumps(_read(fixtures, "feedback_list"))
    assert _read(fixtures, "feedback_saved")["redacted"] == ["email"]
    assert _read(fixtures, "test_questions_csv")["text"].startswith(
        "question,expect_refusal,source_doc,reference_answer"
    )
    updated = _read(fixtures, "detail_updated")
    ids = _read(fixtures, "ids")
    assert updated["update"]["removed"] == [ids["removed"]]
    assert updated["update"]["previous_index_id"] == ids["first"]
    assert _read(fixtures, "delete_refused")["detail"]["code"] == "INDEX_IS_CHAMPION"
    assert _read(fixtures, "comparison")["same_reference_set"] is True


def test_the_assistant_screen_in_jsdom(fixtures: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(fixtures)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
