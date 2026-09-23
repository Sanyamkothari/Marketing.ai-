"""`api.routes.generative` end to end through `TestClient` (docs/generative-ui-endpoints.md).

Every job runs against the fake backend (`FakeLLMClient` in `GROUNDED` mode, the shipped use cases'
default), so a test that waits for a job polls `GET /indexes/{id}` or a run's own `*_status.json` artefact
until it reaches `done` or `failed`, exactly as the UI does. `tests.fixtures.make_run.write_run`
fabricates a finished scoring run in milliseconds - no AutoGluon model is ever fitted here - and
`tests.fixtures.make_docs`'s committed sample corpus and reference set are what `use_sample_documents`
and `use_sample_questions` read, so the RAG happy path needs no upload at all.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import RunMode
from engine.storage import LocalStorage
from tests.fixtures.make_run import RunSpec, write_run

pytestmark = pytest.mark.integration

RAG_USE_CASE = "ai-onboarding-assistant"
RCA_USE_CASE = "rca"
COPY_USE_CASE = "win-back-campaign"
PREDICTIVE_USE_CASE = "targeted-advertisement"

RCA_ROWS = 240
RCA_POSITIVE_RATE = 0.25
COPY_ROWS = 240
COPY_POSITIVE_RATE = 0.3
COPY_ALLOWED_FIELDS = ("snapshot_date", "band")
"""`win-back-campaign.yaml`'s own `allowed_fields` names columns `tests.fixtures.make_run`'s
synthetic frame does not carry (`tests/unit/generative/test_win_back.py` overrides it the same way
for the same reason); these two are real columns of that use case's template."""

TIMEOUT_S = 20.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
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


@pytest.fixture
def rca_run_id(storage: LocalStorage, config_root: Path) -> str:
    return write_run(
        storage,
        RunSpec(
            use_case_id=RCA_USE_CASE,
            mode=RunMode.SCORE,
            rows=RCA_ROWS,
            positive_rate=RCA_POSITIVE_RATE,
            with_text=True,
            config_root=config_root,
        ),
    )


@pytest.fixture
def copy_run_id(storage: LocalStorage, config_root: Path) -> str:
    return write_run(
        storage,
        RunSpec(
            use_case_id=COPY_USE_CASE,
            mode=RunMode.SCORE,
            rows=COPY_ROWS,
            positive_rate=COPY_POSITIVE_RATE,
            config_root=config_root,
        ),
    )


@pytest.fixture
def unfinished_run_id(storage: LocalStorage, config_root: Path) -> str:
    """A `rca` run with no scores at all: `write_run` in train mode writes none (DEC-... see its own docstring)."""
    return write_run(
        storage, RunSpec(use_case_id=RCA_USE_CASE, mode=RunMode.TRAIN, rows=40, config_root=config_root)
    )


@pytest.fixture
def predictive_run_id(storage: LocalStorage, config_root: Path) -> str:
    """A finished run of a use case with no generative flow at all (`generative.kind` is `none`)."""
    return write_run(
        storage,
        RunSpec(use_case_id=PREDICTIVE_USE_CASE, mode=RunMode.SCORE, rows=40, config_root=config_root),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _poll(get_status: Any, *, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """Call `get_status()` until it returns a body whose `state` is terminal, or raise."""
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = get_status()
        if body.get("state") in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job did not reach a terminal state within {timeout}s: {body}")


def _poll_index(client: TestClient, index_id: str, **kwargs: Any) -> dict[str, Any]:
    return _poll(lambda: client.get(f"/indexes/{index_id}").json()["status"], **kwargs)


def _poll_run_status(client: TestClient, run_id: str, filename: str, **kwargs: Any) -> dict[str, Any]:
    return _poll(lambda: client.get(f"/runs/{run_id}/artefacts/{filename}").json(), **kwargs)


def _calls_for(usage: dict[str, Any], purpose: str) -> int:
    """Calls `usage` records against one purpose, or zero when it has never been billed for one."""
    return next((row["calls"] for row in usage["by_purpose"] if row["purpose"] == purpose), 0)


def _build_sample_index(
    client: TestClient, *, use_case_id: str = RAG_USE_CASE, with_questions: bool = True
) -> str:
    response = client.post(
        f"/use-cases/{use_case_id}/indexes",
        data={
            "use_sample_documents": "true",
            "use_sample_questions": "true" if with_questions else "false",
            "model_choice": "__automl__",
            "overrides": "{}",
        },
    )
    assert response.status_code == 202, response.text
    index_id = str(response.json()["index_id"])
    status = _poll_index(client, index_id)
    assert status["state"] == "done", status
    return index_id


# ---------------------------------------------------------------------------
# Every endpoint is reachable and answers its contracted shape
# ---------------------------------------------------------------------------
def test_reference_set_upload_is_profiled(client: TestClient) -> None:
    csv = "question,reference_answer,expect_refusal,source_doc\nWhat is X?,X is Y.,false,faq.md\n"
    response = client.post(
        f"/use-cases/{RAG_USE_CASE}/reference-sets",
        files={"file": ("questions.csv", csv.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reference_set_id"].startswith("refset_")
    assert set(body["columns"]) == {"question", "reference_answer", "expect_refusal", "source_doc"}
    assert body["row_count"] == 1


def test_reference_set_upload_for_an_unknown_use_case_is_404(client: TestClient) -> None:
    response = client.post(
        "/use-cases/not-a-use-case/reference-sets",
        files={"file": ("questions.csv", b"question\n", "text/csv")},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_index_build_is_started_and_reaches_done(client: TestClient) -> None:
    index_id = _build_sample_index(client)
    assert index_id.startswith("x_")


def test_index_build_requires_documents(client: TestClient) -> None:
    response = client.post(
        f"/use-cases/{RAG_USE_CASE}/indexes",
        data={"use_sample_documents": "false", "model_choice": "__automl__", "overrides": "{}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INDEX_DOCUMENTS_REQUIRED"


def test_index_build_from_uploaded_documents(client: TestClient) -> None:
    response = client.post(
        f"/use-cases/{RAG_USE_CASE}/indexes",
        data={"use_sample_documents": "false", "model_choice": "__automl__", "overrides": "{}"},
        files=[("documents", ("plan.md", b"# A Plan\n\nThe plan costs Rs 299 a month.\n", "text/markdown"))],
    )
    assert response.status_code == 202, response.text
    index_id = response.json()["index_id"]
    status = _poll_index(client, index_id)
    assert status["state"] == "done", status
    manifest = client.get(f"/indexes/{index_id}").json()["manifest"]
    assert manifest is not None
    assert manifest["documents"][0]["name"] == "plan.md"


def test_index_list_shows_the_built_index_as_champion(client: TestClient) -> None:
    index_id = _build_sample_index(client)
    listing = client.get(f"/use-cases/{RAG_USE_CASE}/indexes")
    assert listing.status_code == 200, listing.text
    rows = listing.json()["indexes"]
    assert [row["index_id"] for row in rows] == [index_id]
    row = rows[0]
    assert row["kind"] == "build"
    assert row["champion"] is True
    assert row["state"] == "done"
    assert row["llm"]["backend"] == "fake"
    assert row["faithfulness"] is not None


def test_index_list_for_an_unknown_use_case_is_404(client: TestClient) -> None:
    response = client.get("/use-cases/not-a-use-case/indexes")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


def test_index_detail_holds_every_artefact_once_graded(client: TestClient) -> None:
    index_id = _build_sample_index(client)
    detail = client.get(f"/indexes/{index_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["index_id"] == index_id
    assert body["status"]["state"] == "done"
    assert body["manifest"] is not None
    assert body["rag_eval"] is not None
    assert body["llm_usage"] is not None
    assert body["llm"]["backend"] == "fake"
    assert body["llm"]["generation_model_id"] == "fake"


def test_unknown_index_is_404_everywhere_an_index_id_is_read(client: TestClient) -> None:
    for response in (
        client.get("/indexes/x_20260101_deadbeef"),
        client.post("/indexes/x_20260101_deadbeef/ask", json={"question": "hello"}),
        client.post("/indexes/x_20260101_deadbeef/evaluate", data={"use_sample_questions": "true"}),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "INDEX_NOT_FOUND"


def test_evaluate_regrades_an_index_in_place(client: TestClient) -> None:
    index_id = _build_sample_index(client, with_questions=False)
    before = client.get(f"/indexes/{index_id}").json()
    assert before["rag_eval"] is None

    response = client.post(f"/indexes/{index_id}/evaluate", data={"use_sample_questions": "true"})
    assert response.status_code == 202, response.text
    assert response.json()["index_id"] == index_id
    status = _poll_index(client, index_id)
    assert status["state"] == "done", status
    assert status["kind"] == "reference_eval"

    after = client.get(f"/indexes/{index_id}").json()
    assert after["rag_eval"] is not None
    rows = client.get(f"/use-cases/{RAG_USE_CASE}/indexes").json()["indexes"]
    assert rows[0]["kind"] == "evaluate"
    assert rows[0]["source_label"] == "reference_qa.csv"


def test_evaluate_without_a_reference_set_is_422(client: TestClient) -> None:
    index_id = _build_sample_index(client, with_questions=False)
    response = client.post(f"/indexes/{index_id}/evaluate", data={})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "REFERENCE_SET_REQUIRED"


# ---------------------------------------------------------------------------
# Ask: the happy path, and the refusal that costs no generation call
# ---------------------------------------------------------------------------
def test_ask_answers_a_grounded_question_with_citations_and_the_backend_visible(client: TestClient) -> None:
    index_id = _build_sample_index(client)
    response = client.post(f"/indexes/{index_id}/ask", json={"question": "What is the late payment fee?"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refused"] is False
    assert body["called_model"] is True
    assert body["citations"]
    assert body["citations"][0]["document"]


def test_ask_refuses_an_unanswerable_question_without_calling_a_model(client: TestClient) -> None:
    index_id = _build_sample_index(client)
    response = client.post(
        f"/indexes/{index_id}/ask", json={"question": "What is the airspeed velocity of an unladen swallow?"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refused"] is True
    assert body["called_model"] is False
    assert body["citations"] == []


def test_asking_a_question_adds_what_it_cost_to_the_index_usage_report(client: TestClient) -> None:
    """An ask is metered like any other call, so the index's `llm_usage.json` grows by it."""
    index_id = _build_sample_index(client)
    before = client.get(f"/indexes/{index_id}").json()["llm_usage"]
    assert before is not None

    asked = client.post(f"/indexes/{index_id}/ask", json={"question": "What is the late payment fee?"})
    assert asked.status_code == 200, asked.text

    after = client.get(f"/indexes/{index_id}").json()["llm_usage"]
    assert after["totals"]["calls"] > before["totals"]["calls"]
    assert _calls_for(after, "assistant_answer") > _calls_for(before, "assistant_answer")
    assert _calls_for(after, "embedding") > _calls_for(before, "embedding")


# ---------------------------------------------------------------------------
# An uploaded reference set: refused up front, and graded on the right column
# ---------------------------------------------------------------------------
GRADEABLE_REFERENCE_CSV = (
    "question_id,question,reference_answer,expect_refusal,source_doc\n"
    "q001,How long does a new SIM take to activate?,Usually within four hours.,false,faq_activation.md\n"
    "q002,What is the late payment fee?,A flat fee after the due date.,false,faq_billing.md\n"
)
"""`docs/generative-ui-endpoints.md`'s own example columns plus the `source_doc` column
`engine.generative.evaluation` fixes rather than configures, which that example leaves out."""


def _upload_reference_set(client: TestClient, csv: str) -> str:
    response = client.post(
        f"/use-cases/{RAG_USE_CASE}/reference-sets",
        files={"file": ("questions.csv", csv.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["reference_set_id"])


def _build_against(client: TestClient, **fields: str) -> Any:
    return client.post(
        f"/use-cases/{RAG_USE_CASE}/indexes",
        data={"use_sample_documents": "true", "model_choice": "__automl__", "overrides": "{}", **fields},
    )


def test_a_reference_set_missing_a_column_the_grader_needs_is_refused_before_any_job_starts(
    client: TestClient,
) -> None:
    """A file without `source_doc` is a 422 naming it, not a 202 and a failed job to go and poll."""
    reference_set_id = _upload_reference_set(
        client, "question_id,question,reference_answer,expect_refusal\nq001,How long?,Four hours.,false\n"
    )
    response = _build_against(
        client,
        reference_set_id=reference_set_id,
        primary_key="question_id",
        reference_column="reference_answer",
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "REFERENCE_SET_INVALID"
    assert "source_doc" in detail["message"]


def test_naming_a_column_the_reference_file_does_not_hold_is_refused(client: TestClient) -> None:
    """A primary key picked from a stale profile is caught by name, not accepted and ignored."""
    reference_set_id = _upload_reference_set(client, GRADEABLE_REFERENCE_CSV)
    response = _build_against(
        client,
        reference_set_id=reference_set_id,
        primary_key="not_a_column",
        reference_column="reference_answer",
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "REFERENCE_SET_INVALID"
    assert "not_a_column" in response.json()["detail"]["message"]


def test_the_primary_key_column_is_never_graded_as_though_it_were_the_question(
    client: TestClient,
) -> None:
    """`primary_key` identifies a row; the questions graded are the ones a customer would ask."""
    reference_set_id = _upload_reference_set(client, GRADEABLE_REFERENCE_CSV)
    response = _build_against(
        client,
        reference_set_id=reference_set_id,
        primary_key="question_id",
        reference_column="reference_answer",
    )
    assert response.status_code == 202, response.text
    index_id = response.json()["index_id"]
    assert _poll_index(client, index_id)["state"] == "done"

    graded = client.get(f"/indexes/{index_id}").json()["rag_eval"]["questions"]
    assert [row["question"] for row in graded] == [
        "How long does a new SIM take to activate?",
        "What is the late payment fee?",
    ]


def test_a_failed_build_marks_the_step_that_failed_rather_than_leaving_it_running(
    client: TestClient,
) -> None:
    """A document the knowledge base cannot index fails the build, and the step says so."""
    response = client.post(
        f"/use-cases/{RAG_USE_CASE}/indexes",
        data={"use_sample_documents": "false", "model_choice": "__automl__", "overrides": "{}"},
        files=[("documents", ("notes.csv", b"a,b\n1,2\n", "text/csv"))],
    )
    assert response.status_code == 202, response.text
    status = _poll_index(client, response.json()["index_id"])
    assert status["state"] == "failed", status
    assert [stage["state"] for stage in status["stages"]] == ["failed"]
    assert status["stages"][0]["detail"] == status["error_message"]


# ---------------------------------------------------------------------------
# Root cause: the happy path and its preconditions
# ---------------------------------------------------------------------------
def test_root_cause_happy_path(client: TestClient, rca_run_id: str) -> None:
    response = client.post(f"/runs/{rca_run_id}/root-cause", json={"overrides": {}})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"] == rca_run_id
    assert body["job_id"]

    status = _poll_run_status(client, rca_run_id, "root_cause_status.json")
    assert status["state"] == "done", status
    assert status["kind"] == "root_cause"

    summary = client.get(f"/runs/{rca_run_id}/artefacts/root_cause_summary.json")
    assert summary.status_code == 200, summary.text
    segments = summary.json()["segments"]
    assert segments
    assert all(segment["evidence_pack"]["reasons"] for segment in segments)

    usage = client.get(f"/runs/{rca_run_id}/artefacts/llm_usage.json")
    assert usage.status_code == 200
    assert usage.json()["totals"]["calls"] > 0
    assert usage.json()["totals"]["model_ids"] == ["fake"]

    guardrails = client.get(f"/runs/{rca_run_id}/artefacts/guardrail_report.json")
    assert guardrails.status_code == 200
    assert guardrails.json()["kind"] == "root_cause"
    assert guardrails.json()["summary"]["checked"] > 0


def test_root_cause_honours_an_override(client: TestClient, rca_run_id: str) -> None:
    response = client.post(f"/runs/{rca_run_id}/root-cause", json={"overrides": {"max_segments": 1}})
    assert response.status_code == 202, response.text
    status = _poll_run_status(client, rca_run_id, "root_cause_status.json")
    assert status["state"] == "done", status
    summary = client.get(f"/runs/{rca_run_id}/artefacts/root_cause_summary.json").json()
    assert len(summary["segments"]) == 1


def test_root_cause_unknown_override_path_is_422(client: TestClient, rca_run_id: str) -> None:
    response = client.post(f"/runs/{rca_run_id}/root-cause", json={"overrides": {"nonsense": 1}})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"].startswith("OVERRIDE_")


def test_root_cause_on_the_wrong_kind_of_use_case_is_409(client: TestClient, predictive_run_id: str) -> None:
    response = client.post(f"/runs/{predictive_run_id}/root-cause", json={"overrides": {}})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "NOT_A_GENERATIVE_USE_CASE"


def test_root_cause_on_a_run_without_scores_is_409(client: TestClient, unfinished_run_id: str) -> None:
    response = client.post(f"/runs/{unfinished_run_id}/root-cause", json={"overrides": {}})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_WITHOUT_SCORES"


def test_root_cause_on_an_unknown_run_is_404(client: TestClient) -> None:
    response = client.post("/runs/r_20260101_deadbeef/root-cause", json={"overrides": {}})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# Campaign copy: the happy path, approve, regenerate and the run's own artefact route
# ---------------------------------------------------------------------------
def _generate_copy(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.post(
        f"/runs/{run_id}/campaign-copy", json={"overrides": {"allowed_fields": list(COPY_ALLOWED_FIELDS)}}
    )
    assert response.status_code == 202, response.text
    status = _poll_run_status(client, run_id, "copy_status.json")
    assert status["state"] == "done", status
    batch = client.get(f"/runs/{run_id}/artefacts/copy_batch.json")
    assert batch.status_code == 200, batch.text
    return dict(batch.json())


def test_campaign_copy_happy_path_and_the_run_artefact_route(client: TestClient, copy_run_id: str) -> None:
    batch = _generate_copy(client, copy_run_id)
    assert batch["templates"]
    assert batch["run_id"] == copy_run_id

    messages = client.get(f"/runs/{copy_run_id}/copy_messages.csv")
    assert messages.status_code == 200, messages.text
    assert messages.headers["content-type"].startswith("text/csv")
    rows = messages.text.splitlines()
    assert "entity_key" in rows[0].split(",")
    assert len(rows) > 1


def test_campaign_copy_on_the_wrong_kind_of_use_case_is_409(client: TestClient, rca_run_id: str) -> None:
    response = client.post(f"/runs/{rca_run_id}/campaign-copy", json={"overrides": {}})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "NOT_A_GENERATIVE_USE_CASE"


def test_copy_messages_before_any_copy_is_generated_is_404(client: TestClient, copy_run_id: str) -> None:
    response = client.get(f"/runs/{copy_run_id}/copy_messages.csv")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ARTEFACT_NOT_FOUND"


def test_approve_a_pending_template(client: TestClient, copy_run_id: str) -> None:
    batch = _generate_copy(client, copy_run_id)
    pending = next(t for t in batch["templates"] if t["status"] == "pending_review")

    response = client.post(
        f"/runs/{copy_run_id}/campaign-copy/templates/{pending['template_id']}/approve",
        json={"approved_by": "Asha Rao"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == "Asha Rao"
    assert body["approved_at"] is not None

    stored = client.get(f"/runs/{copy_run_id}/artefacts/copy_batch.json").json()
    stored_template = next(t for t in stored["templates"] if t["template_id"] == pending["template_id"])
    assert stored_template["status"] == "approved"


def test_approving_a_blocked_template_is_409(client: TestClient, copy_run_id: str) -> None:
    batch = _generate_copy(client, copy_run_id)
    blocked = next((t for t in batch["templates"] if t["status"] == "blocked"), None)
    if blocked is None:
        pytest.skip("no template was blocked for this seed")
    response = client.post(
        f"/runs/{copy_run_id}/campaign-copy/templates/{blocked['template_id']}/approve",
        json={"approved_by": "Asha Rao"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "COPY_TEMPLATE_NOT_PENDING"


def test_approving_an_already_approved_template_is_409(client: TestClient, copy_run_id: str) -> None:
    batch = _generate_copy(client, copy_run_id)
    pending = next(t for t in batch["templates"] if t["status"] == "pending_review")
    first = client.post(
        f"/runs/{copy_run_id}/campaign-copy/templates/{pending['template_id']}/approve",
        json={"approved_by": "Asha Rao"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/runs/{copy_run_id}/campaign-copy/templates/{pending['template_id']}/approve",
        json={"approved_by": "Asha Rao"},
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "COPY_TEMPLATE_NOT_PENDING"


def test_approving_an_unknown_template_is_404(client: TestClient, copy_run_id: str) -> None:
    _generate_copy(client, copy_run_id)
    response = client.post(
        f"/runs/{copy_run_id}/campaign-copy/templates/no-such-template/approve",
        json={"approved_by": "Asha Rao"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "COPY_TEMPLATE_NOT_FOUND"


def test_regenerate_replaces_one_template_and_costs_one_more_usage_entry(
    client: TestClient, copy_run_id: str
) -> None:
    batch = _generate_copy(client, copy_run_id)
    target = batch["templates"][0]
    before_usage = client.get(f"/runs/{copy_run_id}/artefacts/llm_usage.json").json()

    response = client.post(f"/runs/{copy_run_id}/campaign-copy/templates/{target['template_id']}/regenerate")
    assert response.status_code == 200, response.text
    replaced = response.json()
    assert replaced["band"] == target["band"]
    assert replaced["channel"] == target["channel"]
    assert replaced["attempts"] > target["attempts"]

    stored = client.get(f"/runs/{copy_run_id}/artefacts/copy_batch.json").json()
    ids = [t["template_id"] for t in stored["templates"]]
    assert ids.count(replaced["template_id"]) == 1

    after_usage = client.get(f"/runs/{copy_run_id}/artefacts/llm_usage.json").json()
    assert after_usage["totals"]["calls"] > before_usage["totals"]["calls"]


def test_regenerate_an_unknown_template_is_404(client: TestClient, copy_run_id: str) -> None:
    _generate_copy(client, copy_run_id)
    response = client.post(f"/runs/{copy_run_id}/campaign-copy/templates/no-such-template/regenerate")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "COPY_TEMPLATE_NOT_FOUND"


# ---------------------------------------------------------------------------
# The run artefact route now serves the generative union (DEC-210)
# ---------------------------------------------------------------------------
def test_a_generative_run_artefact_is_now_fetchable_through_the_run_route(
    client: TestClient, rca_run_id: str
) -> None:
    client.post(f"/runs/{rca_run_id}/root-cause", json={"overrides": {}})
    _poll_run_status(client, rca_run_id, "root_cause_status.json")
    direct = client.get(f"/runs/{rca_run_id}/artefacts/root_cause_summary.json")
    assert direct.status_code == 200
    assert direct.headers["content-type"].startswith("application/json")


def test_an_index_only_artefact_is_whitelisted_but_still_404s_on_a_run(
    client: TestClient, rca_run_id: str
) -> None:
    """`chunks.parquet` is a generative name, but this run never wrote it - not an unknown name."""
    response = client.get(f"/runs/{rca_run_id}/artefacts/chunks.parquet")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ARTEFACT_NOT_FOUND"


def test_a_nonsense_name_is_still_unknown(client: TestClient, rca_run_id: str) -> None:
    response = client.get(f"/runs/{rca_run_id}/artefacts/nonsense.json")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ARTEFACT_UNKNOWN"
