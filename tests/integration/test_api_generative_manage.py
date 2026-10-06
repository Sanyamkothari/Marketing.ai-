"""Plan I's assistant routes end to end (DEC-1270 … DEC-1279): passages, feedback, versions, compare.

Every job runs against the fake backend, as in `test_api_generative.py`, whose polling convention
this file follows: a test that starts a build polls `GET /indexes/{id}` until it is `done` or
`failed`. The bundled sample corpus is what `use_sample_documents` reads, so an update of a sample
build carries its documents over from that corpus rather than from an upload.
"""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.storage import LocalStorage

pytestmark = pytest.mark.integration

RAG_USE_CASE = "ai-onboarding-assistant"
TIMEOUT_S = 30.0
MISSING = "x_20260101_deadbeef"

POLICY_MD = b"# Refunds\n\nA refund is paid back to the original card within seven working days.\n"
POLICY_MD_EDITED = (
    b"# Refunds\n\nA refund is paid back to the original card within ten working days, never in cash.\n"
)
ROAMING_MD = b"# Roaming\n\nRoaming in Nepal costs Rs 50 a day and is switched on from the app.\n"
DEVICES_MD = b"# Devices\n\nA handset bought on instalments can be returned within fourteen days.\n"
HTML_PAGE = (
    b"<!doctype html><html><head><title>Help</title><script>var x = 1;</script></head><body>"
    b"<nav><a href='/'>Home</a> | <a href='/login'>Sign in</a></nav>"
    b"<h1>Late payment</h1><p>A late payment fee of Rs 100 is charged five days after the due date.</p>"
    b"<h2>Tariffs</h2><table><tr><th>Plan</th><th>Price</th></tr><tr><td>Starter</td><td>149</td></tr>"
    b"</table></body></html>"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def storage(data_dir: Path) -> LocalStorage:
    return LocalStorage(data_dir)


@pytest.fixture
def client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def _poll_index(client: TestClient, index_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_S
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/indexes/{index_id}").json()["status"]
        if body.get("state") in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"index {index_id} did not finish within {TIMEOUT_S}s: {body}")


def _started(response: Any) -> str:
    assert response.status_code == 202, response.text
    return str(response.json()["index_id"])


def _done(client: TestClient, index_id: str) -> dict[str, Any]:
    status = _poll_index(client, index_id)
    assert status["state"] == "done", status
    body: dict[str, Any] = client.get(f"/indexes/{index_id}").json()
    return body


def _build_sample(client: TestClient, *, with_questions: bool = True) -> str:
    return _started(
        client.post(
            f"/use-cases/{RAG_USE_CASE}/indexes",
            data={
                "use_sample_documents": "true",
                "use_sample_questions": "true" if with_questions else "false",
                "model_choice": "__automl__",
                "overrides": "{}",
            },
        )
    )


def _build_uploaded(client: TestClient, files: list[tuple[str, bytes]]) -> str:
    return _started(
        client.post(
            f"/use-cases/{RAG_USE_CASE}/indexes",
            data={"model_choice": "__automl__", "overrides": "{}"},
            files=[("documents", (name, data, "text/plain")) for name, data in files],
        )
    )


def _update(
    client: TestClient,
    index_id: str,
    *,
    files: list[tuple[str, bytes]] = (),  # type: ignore[assignment]
    remove: list[str] = (),  # type: ignore[assignment]
    data: dict[str, str] | None = None,
) -> Any:
    form: dict[str, Any] = dict(data or {})
    if remove:
        form["remove"] = list(remove)
    return client.post(
        f"/indexes/{index_id}/update",
        data=form,
        files=[("documents", (name, body, "text/plain")) for name, body in files] or None,
    )


def _embedding_input_tokens(usage: dict[str, Any]) -> int:
    return next((row["input_tokens"] for row in usage["by_purpose"] if row["purpose"] == "embedding"), 0)


def _detail(response: Any) -> dict[str, Any]:
    detail: dict[str, Any] = response.json()["detail"]
    return detail


# ---------------------------------------------------------------------------
# 1. A citation opens the passage it came from
# ---------------------------------------------------------------------------
def test_a_citation_opens_its_passage_with_the_quote_inside_and_its_neighbours(client: TestClient) -> None:
    index_id = _build_sample(client, with_questions=False)
    _done(client, index_id)
    asked = client.post(f"/indexes/{index_id}/ask", json={"question": "What is the late payment fee?"})
    assert asked.status_code == 200, asked.text
    citation = asked.json()["citations"][0]
    assert "page" in citation  # a Markdown corpus has no pages, so it is null - and present

    passage = client.get(f"/indexes/{index_id}/chunks/{citation['chunk_id']}")
    assert passage.status_code == 200, passage.text
    body = passage.json()
    assert body["chunk_id"] == citation["chunk_id"]
    assert body["document"] == citation["document"]
    assert body["section"] == citation["section"]
    assert body["page"] == citation["page"]
    if citation["quote"]:
        flat = " ".join(body["text"].lower().split())
        assert " ".join(citation["quote"].lower().split()) in flat

    # Walking "next" from the first passage of the document visits every passage of it, in order.
    first = body
    while first["previous_chunk_id"] is not None:
        first = client.get(f"/indexes/{index_id}/chunks/{first['previous_chunk_id']}").json()
    assert first["ordinal"] == 0
    seen = [first["ordinal"]]
    step = first
    while step["next_chunk_id"] is not None:
        step = client.get(f"/indexes/{index_id}/chunks/{step['next_chunk_id']}").json()
        assert step["doc_id"] == first["doc_id"]
        seen.append(step["ordinal"])
    assert seen == list(range(len(seen)))


def test_a_passage_that_is_not_there_is_a_plain_404(client: TestClient) -> None:
    index_id = _build_sample(client, with_questions=False)
    _done(client, index_id)
    missing = client.get(f"/indexes/{index_id}/chunks/nope-00000")
    assert missing.status_code == 404
    assert _detail(missing)["code"] == "CHUNK_NOT_FOUND"
    unknown = client.get(f"/indexes/{MISSING}/chunks/nope-00000")
    assert unknown.status_code == 404
    assert _detail(unknown)["code"] == "INDEX_NOT_FOUND"


# ---------------------------------------------------------------------------
# 2. Feedback on answers, stored redacted, and exported as test questions
# ---------------------------------------------------------------------------
def test_feedback_is_stored_one_file_per_entry_with_contact_details_masked(
    client: TestClient, storage: LocalStorage
) -> None:
    index_id = _build_sample(client, with_questions=False)
    _done(client, index_id)
    given = client.post(
        f"/indexes/{index_id}/feedback",
        json={
            "rating": "down",
            "question": "Can you call me on 9876543210 about roaming?",
            "answer": "I don't have that information.",
            "refused": True,
            "comment": "Wrong - write to asha.verma@example.com instead",
        },
    )
    assert given.status_code == 201, given.text
    entry = given.json()
    assert "asha.verma@example.com" not in entry["comment"]
    assert "[REDACTED:email]" in entry["comment"]
    assert "9876543210" not in entry["question"]
    assert set(entry["redacted"]) >= {"email", "phone"}
    keys = storage.list_keys(f"indexes/{index_id}/feedback/")
    assert keys == (f"indexes/{index_id}/feedback/{entry['feedback_id']}.json",)
    stored = storage.read_text(keys[0])
    assert "asha.verma@example.com" not in stored and "9876543210" not in stored

    up = client.post(f"/indexes/{index_id}/feedback", json={"rating": "up", "question": "What is the fee?"})
    assert up.status_code == 201, up.text
    listing = client.get(f"/indexes/{index_id}/feedback")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert (body["up"], body["down"]) == (1, 1)
    assert [row["feedback_id"] for row in body["entries"]] == [entry["feedback_id"], up.json()["feedback_id"]]


def test_feedback_is_refused_for_a_bad_rating_and_an_unknown_index(client: TestClient) -> None:
    index_id = _build_sample(client, with_questions=False)
    _done(client, index_id)
    bad = client.post(f"/indexes/{index_id}/feedback", json={"rating": "meh", "question": "Q?"})
    assert bad.status_code == 422
    unknown = client.post(f"/indexes/{MISSING}/feedback", json={"rating": "up", "question": "Q?"})
    assert unknown.status_code == 404
    assert _detail(unknown)["code"] == "INDEX_NOT_FOUND"


def test_thumbs_down_questions_export_as_reference_rows_with_the_answer_left_blank(
    client: TestClient,
) -> None:
    index_id = _build_sample(client, with_questions=False)
    _done(client, index_id)
    for rating, question in (
        ("down", "How do I port my number?"),
        ("up", "What is the late payment fee?"),
        ("down", "  how do I PORT my number? "),
        ("down", "=HYPERLINK(1) is this a formula?"),
    ):
        response = client.post(f"/indexes/{index_id}/feedback", json={"rating": rating, "question": question})
        assert response.status_code == 201, response.text

    exported = client.get(f"/indexes/{index_id}/feedback/test-questions.csv")
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[0] == ["question", "expect_refusal", "source_doc", "reference_answer"]
    assert rows[1:] == [
        ["How do I port my number?", "", "", ""],
        ["'=HYPERLINK(1) is this a formula?", "", "", ""],
    ]


def test_the_exported_file_is_accepted_back_as_a_reference_set(client: TestClient) -> None:
    index_id = _build_sample(client, with_questions=False)
    _done(client, index_id)
    client.post(f"/indexes/{index_id}/feedback", json={"rating": "down", "question": "Is 5G included?"})
    exported = client.get(f"/indexes/{index_id}/feedback/test-questions.csv")
    profiled = client.post(
        f"/use-cases/{RAG_USE_CASE}/reference-sets",
        files={"file": ("test_questions.csv", exported.content, "text/csv")},
    )
    assert profiled.status_code == 200, profiled.text
    assert profiled.json()["row_count"] == 1


# ---------------------------------------------------------------------------
# 3. Update documents: a new version that reuses every unchanged document
# ---------------------------------------------------------------------------
def test_an_update_of_a_sample_build_reuses_every_unchanged_document_and_embeds_only_the_new_one(
    client: TestClient,
) -> None:
    original_id = _build_sample(client, with_questions=False)
    original = _done(client, original_id)
    new_id = _started(_update(client, original_id, files=[("devices_returns.md", DEVICES_MD)]))
    assert new_id != original_id
    updated = _done(client, new_id)

    update = updated["update"]
    assert update["previous_index_id"] == original_id
    assert update["added"] == ["devices_returns.md"]
    assert update["replaced"] == [] and update["removed"] == []
    assert len(update["reused"]) == len(original["manifest"]["documents"])
    assert update["chunks_reused"] == original["manifest"]["total_chunks"]
    assert (
        update["chunks_embedded"]
        == updated["manifest"]["total_chunks"] - original["manifest"]["total_chunks"]
    )
    assert 0 < update["chunks_embedded"] < update["chunks_reused"]
    # The cost shown is the cost of the new document only.
    assert 0 < _embedding_input_tokens(updated["llm_usage"]) < _embedding_input_tokens(original["llm_usage"])

    # The previous version is untouched, and still answers.
    assert client.get(f"/indexes/{original_id}").json()["manifest"] == original["manifest"]
    assert (
        client.post(f"/indexes/{original_id}/ask", json={"question": "What is the late fee?"}).status_code
        == 200
    )
    rows = {
        row["index_id"]: row for row in client.get(f"/use-cases/{RAG_USE_CASE}/indexes").json()["indexes"]
    }
    assert rows[new_id]["previous_index_id"] == original_id
    assert rows[original_id]["previous_index_id"] is None


def test_an_update_replaces_and_removes_uploaded_documents_and_re_embeds_only_the_changed_one(
    client: TestClient,
) -> None:
    original_id = _build_uploaded(
        client, [("refunds.md", POLICY_MD), ("roaming.md", ROAMING_MD), ("devices.md", DEVICES_MD)]
    )
    original = _done(client, original_id)
    new_id = _started(
        _update(client, original_id, files=[("refunds.md", POLICY_MD_EDITED)], remove=["devices.md"])
    )
    updated = _done(client, new_id)
    names = [doc["name"] for doc in updated["manifest"]["documents"]]
    assert names == ["refunds.md", "roaming.md"]
    update = updated["update"]
    assert (update["added"], update["replaced"], update["removed"]) == ([], ["refunds.md"], ["devices.md"])
    assert update["reused"] == ["roaming.md"]
    roaming = next(doc for doc in original["manifest"]["documents"] if doc["name"] == "roaming.md")
    assert update["chunks_reused"] == roaming["chunks"]
    refunds = next(doc for doc in updated["manifest"]["documents"] if doc["name"] == "refunds.md")
    assert refunds["fingerprint"] != next(
        doc["fingerprint"] for doc in original["manifest"]["documents"] if doc["name"] == "refunds.md"
    )
    passage = client.get(f"/indexes/{new_id}/chunks/refunds-00000").json()
    assert "ten working days" in passage["text"]
    # A third version built from the second carries its copies, not the first version's.
    third_id = _started(_update(client, new_id, files=[("devices.md", DEVICES_MD)]))
    third = _done(client, third_id)
    assert sorted(third["update"]["reused"]) == ["refunds.md", "roaming.md"]


def test_an_update_that_asks_for_nothing_or_something_impossible_is_refused_before_anything_is_written(
    client: TestClient, storage: LocalStorage
) -> None:
    original_id = _build_uploaded(client, [("refunds.md", POLICY_MD), ("roaming.md", ROAMING_MD)])
    _done(client, original_id)
    before = set(storage.list_keys("indexes/"))
    cases = [
        (_update(client, original_id), 422, "INDEX_UPDATE_EMPTY"),
        (_update(client, original_id, remove=["nope.md"]), 422, "DOCUMENT_NOT_IN_INDEX"),
        (
            _update(client, original_id, remove=["refunds.md", "roaming.md"]),
            422,
            "INDEX_UPDATE_REMOVES_EVERYTHING",
        ),
        (_update(client, original_id, files=[("refunds.txt", b"Refunds.\n")]), 422, "DOCUMENT_NAME_CLASH"),
        (_update(client, MISSING, files=[("a.md", b"# A\n\nText.\n")]), 404, "INDEX_NOT_FOUND"),
    ]
    for response, status, code in cases:
        assert response.status_code == status, response.text
        assert _detail(response)["code"] == code
    assert set(storage.list_keys("indexes/")) == before


def test_an_update_of_a_graded_index_is_graded_with_the_same_questions_and_can_be_compared(
    client: TestClient,
) -> None:
    original_id = _build_sample(client, with_questions=True)
    original = _done(client, original_id)
    assert original["grade"] is not None
    new_id = _started(_update(client, original_id, remove=["policy_privacy.md"]))
    updated = _done(client, new_id)
    assert [stage["key"] for stage in updated["status"]["stages"]] == ["build", "evaluate"]
    assert updated["rag_eval"] is not None

    compared = client.get(f"/indexes/{original_id}/compare/{new_id}")
    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert body["same_reference_set"] is True
    assert body["common_questions"] == original["grade"]["questions"]
    assert body["left"]["grade"] == original["grade"]
    assert body["right"]["grade"] == updated["grade"]
    assert body["left"]["documents"] == body["right"]["documents"] + 1
    for change in body["changed"]:
        assert change["left_passed"] != change["right_passed"]


# ---------------------------------------------------------------------------
# 4. The fuller grade card and the compare view's refusals
# ---------------------------------------------------------------------------
def test_the_grade_card_counts_are_read_off_the_graded_questions(client: TestClient) -> None:
    index_id = _build_sample(client, with_questions=True)
    detail = _done(client, index_id)
    grade, rag_eval = detail["grade"], detail["rag_eval"]
    questions = rag_eval["questions"]
    assert grade["questions"] == len(questions) == rag_eval["aggregates"]["questions"]
    assert grade["refusal_correct"] == sum(q["refused"] == q["expect_refusal"] for q in questions)
    assert grade["should_refuse"] == sum(q["expect_refusal"] for q in questions)
    assert grade["refused_correctly"] == sum(q["expect_refusal"] and q["refused"] for q in questions)
    for name in ("retrieval_hit_rate", "mean_faithfulness", "mean_correctness"):
        assert grade[name] == rag_eval["aggregates"][name]


def test_comparing_with_an_ungraded_index_is_refused_in_words(client: TestClient) -> None:
    graded = _build_sample(client, with_questions=True)
    _done(client, graded)
    ungraded = _build_sample(client, with_questions=False)
    _done(client, ungraded)
    response = client.get(f"/indexes/{graded}/compare/{ungraded}")
    assert response.status_code == 409
    assert _detail(response)["code"] == "INDEX_NOT_GRADED"
    assert ungraded in _detail(response)["message"]
    assert client.get(f"/indexes/{graded}/compare/{MISSING}").status_code == 404


# ---------------------------------------------------------------------------
# 5. Deleting a version
# ---------------------------------------------------------------------------
def test_the_best_index_is_kept_and_any_other_version_can_be_deleted(
    client: TestClient, storage: LocalStorage
) -> None:
    best = _build_sample(client, with_questions=True)
    _done(client, best)
    other = _started(_update(client, best, files=[("devices_returns.md", DEVICES_MD)], data={}))
    _done(client, other)
    rows = {
        row["index_id"]: row for row in client.get(f"/use-cases/{RAG_USE_CASE}/indexes").json()["indexes"]
    }
    champion = next(index_id for index_id, row in rows.items() if row["champion"])
    loser = other if champion == best else best

    refused = client.delete(f"/indexes/{champion}")
    assert refused.status_code == 409
    assert _detail(refused)["code"] == "INDEX_IS_CHAMPION"

    client.post(f"/indexes/{loser}/feedback", json={"rating": "up", "question": "Q?"})
    deleted = client.delete(f"/indexes/{loser}")
    assert deleted.status_code == 204, deleted.text
    assert storage.list_keys(f"indexes/{loser}/") == ()
    assert client.get(f"/indexes/{loser}").status_code == 404
    listed = [row["index_id"] for row in client.get(f"/use-cases/{RAG_USE_CASE}/indexes").json()["indexes"]]
    assert listed == [champion]
    # The version that is left still answers, whichever of the two it was.
    assert (
        client.post(f"/indexes/{champion}/ask", json={"question": "What is the late fee?"}).status_code == 200
    )
    assert client.delete(f"/indexes/{MISSING}").status_code == 404


# ---------------------------------------------------------------------------
# 6. Web pages
# ---------------------------------------------------------------------------
def test_an_html_page_is_indexed_by_its_headings_without_its_navigation(client: TestClient) -> None:
    index_id = _build_uploaded(client, [("billing_help.html", HTML_PAGE)])
    detail = _done(client, index_id)
    document = detail["manifest"]["documents"][0]
    assert (document["name"], document["media_type"]) == ("billing_help.html", "html")
    assert document["sections"] == 2 and document["chunks"] >= 2
    first = client.get(f"/indexes/{index_id}/chunks/billing_help-00000").json()
    assert first["section"] == "Late payment"
    assert "Sign in" not in first["text"] and "var x" not in first["text"]
    second = client.get(f"/indexes/{index_id}/chunks/{first['next_chunk_id']}").json()
    assert second["section"] == "Tariffs"
    assert "Starter - 149" in second["text"]
