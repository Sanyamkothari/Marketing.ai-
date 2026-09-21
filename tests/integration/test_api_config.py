"""The config-only endpoints of M1, end to end through `TestClient` (design §7)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import IndustriesResponse, UseCaseResponse
from engine import __version__
from engine.config import DEFAULT_CONFIG_ROOT, UseCaseConfig, get_catalog, list_use_case_ids
from engine.templates import template_filenames

pytestmark = pytest.mark.integration

USE_CASE_IDS: tuple[str, ...] = list_use_case_ids()
DEMO_ID: str = "targeted-advertisement"
PLANNED_ID: str = "ai-onboarding-assistant"
EXPECTED_MARKERS: tuple[str, ...] = ("P", "G", "P", "H", "H")
EXPECTED_PAGES: dict[str, str] = {
    "data": "Customer + campaign data",
    "model": "Audience Propensity Model",
    "output": "Target Audience / Ad Targeting",
}


@pytest.fixture(scope="module")
def client(config_root: Path) -> Iterator[TestClient]:
    """One app pointed at the checkout's `configs/`."""
    with TestClient(create_app(config_root=config_root)) as test_client:
        yield test_client


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_industries_validates_and_lists_five_stages_in_order(client: TestClient) -> None:
    response = client.get("/industries")
    assert response.status_code == 200
    body = IndustriesResponse.model_validate(response.json())
    (industry,) = body.industries
    assert industry.id == "telecom"
    assert industry.journey_label == "Customer Lifecycle"
    assert tuple(stage.marker for stage in industry.stages) == EXPECTED_MARKERS
    assert tuple(stage.order for stage in industry.stages) == (1, 2, 3, 4, 5)


def test_industries_legend_has_three_entries_with_stars(client: TestClient) -> None:
    body = IndustriesResponse.model_validate(client.get("/industries").json())
    legend = body.industries[0].legend
    assert len(legend) == 3
    assert all(entry.stars for entry in legend)
    assert {entry.label for entry in legend} == {"Predictive AI", "Generative AI", "Hybrid"}


def test_a_planned_use_case_is_a_card_without_a_configuration(client: TestClient) -> None:
    body = IndustriesResponse.model_validate(client.get("/industries").json())
    cards = {card.id: card for stage in body.industries[0].stages for card in stage.use_cases}
    planned = cards[PLANNED_ID]
    assert planned.status == "planned"
    assert planned.problem_type is None
    assert planned.entity is None
    assert planned.target_column is None
    assert planned.trainable_in_phase_1 is False
    assert planned.name and planned.description
    available = cards[DEMO_ID]
    assert available.status == "available"
    assert available.problem_type == "binary_classification"
    assert available.target_column == "converted_30d"


def test_use_case_body_validates_and_carries_the_setup_screen(client: TestClient) -> None:
    response = client.get(f"/use-cases/{DEMO_ID}")
    assert response.status_code == 200
    body = UseCaseResponse.model_validate(response.json())
    assert body.id == DEMO_ID
    assert len(body.advanced_settings.stages) == 8
    assert tuple(stage.number for stage in body.advanced_settings.stages) == (1, 2, 3, 4, 5, 6, 7, 8)
    assert body.pages.model_dump() == EXPECTED_PAGES
    assert len(body.running_rows.train) == 5
    assert len(body.running_rows.score) == 4
    assert body.problem_type_label == "Classification (yes / no)"
    assert body.target.label == "Target column"
    assert body.setup.template_url == f"/use-cases/{DEMO_ID}/template.csv"
    assert body.setup.template_readme_url == f"/use-cases/{DEMO_ID}/template_README.md"


def test_model_choices_start_with_automl_and_use_catalog_labels(client: TestClient) -> None:
    body = UseCaseResponse.model_validate(client.get(f"/use-cases/{DEMO_ID}").json())
    first = body.setup.model_choices[0]
    assert (first.value, first.label) == ("__automl__", "AutoML (recommended)")
    assert "Random Forest" in [choice.label for choice in body.setup.model_choices]
    assert all(choice.enabled for choice in body.setup.model_choices)


def test_problem_type_choices_disable_the_later_phases(client: TestClient) -> None:
    body = UseCaseResponse.model_validate(client.get(f"/use-cases/{DEMO_ID}").json())
    enabled = {choice.value: choice.enabled for choice in body.setup.problem_type_choices}
    assert enabled == {
        "binary_classification": True,
        "regression": True,
        "forecasting": False,
        "clustering": False,
    }
    disabled = [choice for choice in body.setup.problem_type_choices if not choice.enabled]
    assert all(choice.help for choice in disabled)


def test_mode_copy_substitutes_the_entity(client: TestClient) -> None:
    body = UseCaseResponse.model_validate(client.get(f"/use-cases/{DEMO_ID}").json())
    train, score = body.setup.modes
    assert train.value == "train"
    assert score.value == "score"
    assert train.dataset_hint == "One row per customer, including the outcome column. CSV or Parquet."
    assert train.run_button == "Run training"
    assert score.run_button == "Score data"
    assert "{entity}" not in train.dataset_hint + score.dataset_hint


def test_the_embedded_config_revalidates(client: TestClient) -> None:
    payload = client.get(f"/use-cases/{DEMO_ID}").json()
    config = UseCaseConfig.model_validate(payload["config"])
    assert config.id == DEMO_ID


def test_uploaded_columns_populate_the_column_widgets(client: TestClient) -> None:
    response = client.get(
        f"/use-cases/{DEMO_ID}",
        params={
            "columns": "customer_id,snapshot_date,visits_last_7d,converted_30d",
            "primary_key": "customer_id",
            "target": "converted_30d",
        },
    )
    assert response.status_code == 200
    body = UseCaseResponse.model_validate(response.json())
    fields = {field.path: field for stage in body.advanced_settings.stages for field in stage.fields}
    time_choices = fields["split.time_column"].choices
    assert time_choices is not None
    assert [choice.value for choice in time_choices] == ["snapshot_date"]
    feature_choices = fields["prepare.exclude_columns"].choices
    assert feature_choices is not None
    assert [choice.value for choice in feature_choices] == ["snapshot_date", "visits_last_7d"]


def test_without_columns_the_column_widgets_have_no_choices(client: TestClient) -> None:
    body = UseCaseResponse.model_validate(client.get(f"/use-cases/{DEMO_ID}").json())
    fields = {field.path: field for stage in body.advanced_settings.stages for field in stage.fields}
    assert fields["split.time_column"].choices is None
    assert fields["prepare.exclude_columns"].choices is None


def test_a_planned_use_case_is_a_404_with_its_own_code(client: TestClient) -> None:
    response = client.get(f"/use-cases/{PLANNED_ID}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_PLANNED"


def test_an_unknown_use_case_is_a_404(client: TestClient) -> None:
    response = client.get("/use-cases/not-a-use-case")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USE_CASE_NOT_FOUND"


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_template_csv_is_the_committed_file(client: TestClient, repo_root: Path, use_case_id: str) -> None:
    response = client.get(f"/use-cases/{use_case_id}/template.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    csv_name, _readme_name = template_filenames(
        UseCaseConfig.model_validate(client.get(f"/use-cases/{use_case_id}").json()["config"])
    )
    assert response.headers["content-disposition"] == f'attachment; filename="{csv_name}"'
    assert response.headers["cache-control"] == "no-store"
    assert response.content == (repo_root / "templates" / csv_name).read_bytes()


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_template_readme_is_the_committed_file(client: TestClient, repo_root: Path, use_case_id: str) -> None:
    response = client.get(f"/use-cases/{use_case_id}/template_README.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    _csv_name, readme_name = template_filenames(
        UseCaseConfig.model_validate(client.get(f"/use-cases/{use_case_id}").json()["config"])
    )
    assert response.headers["content-disposition"] == f'attachment; filename="{readme_name}"'
    assert response.content == (repo_root / "templates" / readme_name).read_bytes()


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_template_csv_answers_head_with_the_download_headers(
    client: TestClient, repo_root: Path, use_case_id: str
) -> None:
    """`curl -I` (design §14) and download managers probe the headers before fetching the body.

    FastAPI does not add HEAD to a GET route by itself, so the route registers it explicitly; the headers
    must be the GET headers and the announced length must be the committed file's.
    """
    response = client.head(f"/use-cases/{use_case_id}/template.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    csv_name, _readme_name = template_filenames(
        UseCaseConfig.model_validate(client.get(f"/use-cases/{use_case_id}").json()["config"])
    )
    assert response.headers["content-disposition"] == f'attachment; filename="{csv_name}"'
    assert response.headers["cache-control"] == "no-store"
    assert int(response.headers["content-length"]) == (repo_root / "templates" / csv_name).stat().st_size


def test_template_readme_answers_head_with_the_download_headers(client: TestClient, repo_root: Path) -> None:
    response = client.head(f"/use-cases/{DEMO_ID}/template_README.md")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    _csv_name, readme_name = template_filenames(
        UseCaseConfig.model_validate(client.get(f"/use-cases/{DEMO_ID}").json()["config"])
    )
    assert response.headers["content-disposition"] == f'attachment; filename="{readme_name}"'
    assert int(response.headers["content-length"]) == (repo_root / "templates" / readme_name).stat().st_size


def test_openapi_builds_and_documents_every_route(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert set(paths) == {
        # M1: configuration only
        "/healthz",
        "/industries",
        "/use-cases/{use_case_id}",
        "/use-cases/{use_case_id}/template.csv",
        "/use-cases/{use_case_id}/template_README.md",
        # M2: uploads and runs
        "/uploads",
        "/uploads/{upload_id}/profile",
        "/runs",
        "/runs/{run_id}",
        "/runs/{run_id}/artefacts/{name}",
        "/runs/{run_id}/scores.csv",
        "/runs/{run_id}/cancel",
        # M3: the model registry
        "/models",
        "/models/{model_id}/approve",
        "/models/{model_id}/promote",
    }


def _root_where_rmse_scores_classification(tmp_path: Path) -> Path:
    """A copy of `configs/` whose catalog also lets `rmse` (relabelled) score a classification problem,
    with a targeted-advertisement file that is only valid under that catalog."""
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    engine_yaml = root / "engine.yaml"
    text = engine_yaml.read_text(encoding="utf-8")
    rmse_line = next(line for line in text.splitlines() if line.strip().startswith("rmse:"))
    patched = rmse_line.replace(
        "problem_types: [regression]", "problem_types: [regression, binary_classification]"
    ).replace('label: "RMSE"', 'label: "Root MSE"')
    engine_yaml.write_text(text.replace(rmse_line, patched, 1), encoding="utf-8")
    use_case = root / "use_cases" / "targeted_advertisement.yaml"
    use_case.write_text(
        use_case.read_text(encoding="utf-8")
        + "\nmodel_search:\n  metric: rmse\n  metric_choices: [rmse, roc_auc]\n",
        encoding="utf-8",
    )
    return root


def test_the_app_validates_and_labels_against_the_catalog_of_its_own_config_root(tmp_path: Path) -> None:
    """`create_app(config_root=…)` points the whole app at that tree, its catalog included (DEC-038)."""
    root = _root_where_rmse_scores_classification(tmp_path)
    with TestClient(create_app(config_root=root)) as client:
        industries = client.get("/industries")
        assert industries.status_code == 200, industries.json()
        response = client.get(f"/use-cases/{DEMO_ID}")
        assert response.status_code == 200, response.json()
    # The body is only valid under that root's catalog, so it is re-validated with it as context.
    body = UseCaseResponse.model_validate(response.json(), context={"catalog": get_catalog(root)})
    assert body.config.model_search.metric == "rmse"
    assert body.config.catalog is get_catalog(root)
    metric_field = next(
        field
        for stage in body.advanced_settings.stages
        for field in stage.fields
        if field.path == "model_search.metric"
    )
    assert metric_field.choices is not None
    assert [choice.label for choice in metric_field.choices] == ["Root MSE", "ROC-AUC"]
    assert body.advanced_settings.stages[4].summary.startswith("Root MSE · ")
