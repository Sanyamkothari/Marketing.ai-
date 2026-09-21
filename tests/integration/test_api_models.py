"""The model registry endpoints of plan §8, end to end through `TestClient`.

Every test drives a real `LocalModelRegistry` on a temporary directory: the app is built with
`data_dir=tmp_path`, and the test seeds rows through a second registry pointed at the same SQLite
file, which `LocalModelRegistry` documents as safe. Nothing here stubs the registry, so the champion
swap these routes expose is the one the registry actually performs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import ModelListResponse, ModelVersionResponse
from engine.config import Metric
from engine.contracts import ModelStatus, ModelVersion
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.utils.time import utc_now

pytestmark = pytest.mark.integration

USE_CASE: str = "targeted-advertisement"
OTHER_USE_CASE: str = "payment-propensity"


def make_version(
    model_id: str,
    version: int,
    *,
    use_case_id: str = USE_CASE,
    status: ModelStatus = ModelStatus.CANDIDATE,
    test_score: float = 0.80,
    minutes: int = 0,
    measured_against_champion_id: str | None = None,
) -> ModelVersion:
    """A complete `ModelVersion`; only the fields a test asserts on carry meaning."""
    return ModelVersion(
        model_id=model_id,
        use_case_id=use_case_id,
        version=version,
        run_id=f"r_{model_id}",
        created_at=utc_now() + timedelta(minutes=minutes),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=test_score,
        validation_score=None,
        model_display_name="WeightedEnsemble_L2",
        schema_key=f"models/{model_id}/schema.json",
        run_config_key=f"models/{model_id}/run_config.json",
        predictor_key=f"models/{model_id}/model",
        artefact_keys={"run.json": f"runs/r_{model_id}/run.json"},
        measured_against_champion_id=measured_against_champion_id,
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    """The registry the test seeds; the app builds its own instance on the same file."""
    return LocalModelRegistry(tmp_path / REGISTRY_FILENAME)


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as test_client:
        yield test_client


def entries(response) -> tuple[ModelVersionResponse, ...]:
    """The validated body of `GET /models`, so a shape change fails here and not in an assertion."""
    assert response.status_code == 200, response.json()
    return ModelListResponse.model_validate(response.json()).versions


def detail(response) -> dict[str, object]:
    """The error envelope: exactly `code`, `message` and `path`, never a bare string."""
    body = response.json()
    assert set(body) == {"detail"}, body
    assert set(body["detail"]) == {"code", "message", "path"}, body
    assert isinstance(body["detail"]["message"], str) and body["detail"]["message"]
    return dict(body["detail"])


# ---------------------------------------------------------------------------
# GET /models
# ---------------------------------------------------------------------------
def test_an_empty_registry_lists_nothing(client: TestClient) -> None:
    assert entries(client.get("/models")) == ()


def test_a_use_case_without_a_champion_flags_nothing(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1))
    registry.register(make_version("m_2", 2, status=ModelStatus.PENDING_APPROVAL, minutes=1))
    listed = entries(client.get("/models", params={"use_case": USE_CASE}))
    assert [item.version.model_id for item in listed] == ["m_2", "m_1"]
    assert [item.is_champion for item in listed] == [False, False]


def test_the_champion_is_the_one_flagged_row(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1))
    registry.register(make_version("m_2", 2, status=ModelStatus.CHAMPION, minutes=1))
    listed = entries(client.get("/models", params={"use_case": USE_CASE}))
    flagged = [item.version.model_id for item in listed if item.is_champion]
    assert flagged == ["m_2"]
    assert next(item for item in listed if item.is_champion).version.status is ModelStatus.CHAMPION


def test_the_use_case_filter_keeps_only_that_use_case(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1))
    registry.register(make_version("m_other", 1, use_case_id=OTHER_USE_CASE, minutes=1))
    assert [item.version.model_id for item in entries(client.get("/models"))] == ["m_other", "m_1"]
    listed = entries(client.get("/models", params={"use_case": OTHER_USE_CASE}))
    assert [item.version.model_id for item in listed] == ["m_other"]
    assert listed[0].version.use_case_id == OTHER_USE_CASE


def test_an_unknown_use_case_filter_is_an_empty_list_not_an_error(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1))
    assert entries(client.get("/models", params={"use_case": "no-such-use-case"})) == ()


# ---------------------------------------------------------------------------
# POST /models/{id}/approve
# ---------------------------------------------------------------------------
def test_approving_a_pending_version_crowns_it_and_records_the_approver(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    response = client.post("/models/m_1/approve", json={"approved_by": "priya@minfy.example"})
    assert response.status_code == 200, response.json()
    body = ModelVersionResponse.model_validate(response.json())
    assert body.is_champion is True
    assert body.version.status is ModelStatus.CHAMPION
    assert body.version.approved_by == "priya@minfy.example"
    assert body.version.approved_at is not None
    assert body.version.promotion_note is None, "approve records no override reason; only promote does"
    assert registry.get("m_1").status is ModelStatus.CHAMPION


def test_approving_a_candidate_is_a_409_because_nothing_is_awaiting_a_human(
    client: TestClient, registry
) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CANDIDATE))
    response = client.post("/models/m_1/approve", json={"approved_by": "priya"})
    assert response.status_code == 409
    assert detail(response)["code"] == "INVALID_TRANSITION"
    assert registry.get("m_1").status is ModelStatus.CANDIDATE


def test_approving_an_already_crowned_champion_is_a_409(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CHAMPION))
    response = client.post("/models/m_1/approve", json={"approved_by": "priya"})
    assert response.status_code == 409
    assert detail(response)["code"] == "INVALID_TRANSITION"


def test_approving_against_a_champion_that_has_changed_is_a_409_not_a_500(
    client: TestClient, registry
) -> None:
    """The registry refuses a stale approval (DEC-047), and this router answers it as a refusal.

    An unmapped `RegistryError.code` is deliberately a 500 here - this router does not guess a 4xx
    for a condition nobody taught it - so a code the registry gains and the table does not learn
    turns a business refusal into a server fault. `CHAMPION_CHANGED` is that code: the request is
    well formed and the version exists, but the head-to-head behind it was measured against a
    model that is no longer the champion, which is a conflict and nothing else.
    """
    registry.register(make_version("m_1", 1, status=ModelStatus.CHAMPION))
    registry.register(
        make_version(
            "m_2",
            2,
            status=ModelStatus.PENDING_APPROVAL,
            minutes=1,
            measured_against_champion_id="m_0",
        )
    )

    response = client.post("/models/m_2/approve", json={"approved_by": "priya@minfy.example"})

    assert response.status_code == 409, response.json()
    body = detail(response)
    assert body["code"] == "CHAMPION_CHANGED"
    assert "m_0" in body["message"] and "m_1" in body["message"], "both champions are named"
    assert registry.get("m_2").status is ModelStatus.PENDING_APPROVAL, "nothing was crowned"
    assert registry.get("m_1").status is ModelStatus.CHAMPION


def test_approving_an_unknown_id_is_a_404(client: TestClient) -> None:
    response = client.post("/models/m_nope/approve", json={"approved_by": "priya"})
    assert response.status_code == 404
    assert detail(response)["code"] == "MODEL_NOT_FOUND"


@pytest.mark.parametrize("body", [{}, {"approved_by": ""}, {"approved_by": "   "}])
def test_approving_without_a_named_approver_is_a_422(client: TestClient, registry, body) -> None:
    """There is no authentication to fall back on, so the API refuses rather than invent a name."""
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    assert client.post("/models/m_1/approve", json=body).status_code == 422
    assert registry.get("m_1").status is ModelStatus.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# POST /models/{id}/promote
# ---------------------------------------------------------------------------
def test_promoting_with_a_reason_records_who_and_why(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CANDIDATE))
    response = client.post(
        "/models/m_1/promote",
        json={"promoted_by": "arun", "reason": "Campaign starts Monday; the incumbent misses the SLA."},
    )
    assert response.status_code == 200, response.json()
    body = ModelVersionResponse.model_validate(response.json())
    assert body.is_champion is True
    assert body.version.promoted_by == "arun"
    assert body.version.promotion_note == "Campaign starts Monday; the incumbent misses the SLA."
    assert body.version.promoted_at is not None
    assert body.version.approved_by is None, "promote is an override, not an approval"


def test_promoting_a_pending_version_is_allowed_and_is_still_an_override(
    client: TestClient, registry
) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL))
    response = client.post(
        "/models/m_1/promote", json={"promoted_by": "arun", "reason": "Signed off offline."}
    )
    assert response.status_code == 200, response.json()
    body = ModelVersionResponse.model_validate(response.json())
    assert body.version.status is ModelStatus.CHAMPION
    assert body.version.promotion_note == "Signed off offline."
    assert body.version.approved_by is None


@pytest.mark.parametrize(
    "body",
    [
        {"promoted_by": "arun"},
        {"promoted_by": "arun", "reason": ""},
        {"promoted_by": "arun", "reason": "   "},
        {"reason": "Because."},
    ],
)
def test_promoting_without_both_who_and_why_is_a_422(client: TestClient, registry, body) -> None:
    """An override with no recorded justification is worse than no override, so it is refused."""
    registry.register(make_version("m_1", 1, status=ModelStatus.CANDIDATE))
    assert client.post("/models/m_1/promote", json=body).status_code == 422
    assert registry.get("m_1").status is ModelStatus.CANDIDATE


def test_promoting_an_unknown_id_is_a_404(client: TestClient) -> None:
    response = client.post("/models/m_nope/promote", json={"promoted_by": "arun", "reason": "Why not."})
    assert response.status_code == 404
    assert detail(response)["code"] == "MODEL_NOT_FOUND"


def test_promoting_an_archived_version_is_a_409(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.ARCHIVED))
    response = client.post("/models/m_1/promote", json={"promoted_by": "arun", "reason": "Rollback."})
    assert response.status_code == 409
    assert detail(response)["code"] == "INVALID_TRANSITION"


def test_an_unknown_body_key_is_a_422(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CANDIDATE))
    body = {"promoted_by": "arun", "reason": "Fine.", "becasue": "typo"}
    assert client.post("/models/m_1/promote", json=body).status_code == 422


# ---------------------------------------------------------------------------
# The champion swap, as seen through the API
# ---------------------------------------------------------------------------
def test_approving_a_successor_demotes_the_incumbent_and_records_it(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CHAMPION))
    registry.register(make_version("m_2", 2, status=ModelStatus.PENDING_APPROVAL, minutes=1))

    body = ModelVersionResponse.model_validate(
        client.post("/models/m_2/approve", json={"approved_by": "priya"}).json()
    )
    assert body.version.previous_champion_id == "m_1"

    listed = {
        item.version.model_id: item for item in entries(client.get("/models", params={"use_case": USE_CASE}))
    }
    assert listed["m_2"].is_champion is True
    assert listed["m_1"].is_champion is False
    assert listed["m_1"].version.status is ModelStatus.ARCHIVED
    assert [model_id for model_id, item in listed.items() if item.is_champion] == ["m_2"]


def test_promoting_a_successor_demotes_the_incumbent_and_records_it(client: TestClient, registry) -> None:
    registry.register(make_version("m_1", 1, status=ModelStatus.CHAMPION))
    registry.register(make_version("m_2", 2, status=ModelStatus.CANDIDATE, minutes=1))

    body = ModelVersionResponse.model_validate(
        client.post("/models/m_2/promote", json={"promoted_by": "arun", "reason": "Vendor outage."}).json()
    )
    assert body.version.previous_champion_id == "m_1"
    assert body.version.promotion_note == "Vendor outage."

    listed = {item.version.model_id: item for item in entries(client.get("/models"))}
    assert listed["m_2"].is_champion is True
    assert listed["m_1"].version.status is ModelStatus.ARCHIVED


def test_a_champion_swap_leaves_another_use_case_untouched(client: TestClient, registry) -> None:
    registry.register(make_version("m_other", 1, use_case_id=OTHER_USE_CASE, status=ModelStatus.CHAMPION))
    registry.register(make_version("m_1", 1, status=ModelStatus.PENDING_APPROVAL, minutes=1))

    assert client.post("/models/m_1/approve", json={"approved_by": "priya"}).status_code == 200
    other = entries(client.get("/models", params={"use_case": OTHER_USE_CASE}))
    assert [item.version.model_id for item in other] == ["m_other"]
    assert other[0].is_champion is True


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_openapi_builds_and_documents_the_three_model_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert {"/models", "/models/{model_id}/approve", "/models/{model_id}/promote"} <= set(paths)
    assert set(paths["/models"]) == {"get"}
    assert set(paths["/models/{model_id}/approve"]) == {"post"}
    assert set(paths["/models/{model_id}/promote"]) == {"post"}
    for path in ("/models/{model_id}/approve", "/models/{model_id}/promote"):
        assert set(paths[path]["post"]["responses"]) >= {"200", "404", "409", "422"}
