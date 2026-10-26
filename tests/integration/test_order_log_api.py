"""With `data_doctor.order_log` off, the API answers as it did before the order-log path (v3 phase 0, DEC-1822).

Two routes could have moved: `POST /doctor/outcome` (retail-win-back now has a template, marked `order_log_only`)
and the source routes (the two internal roles `order_lines` and `returns` are in the role catalogue). Off, the first
still has no sentence for retail-win-back and the second still does not know either role. On, the path's builder
port registers its tables through the same source route.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.fixtures.order_log import order_log_config_root

pytestmark = pytest.mark.integration

EVENTS_CSV = b"customer_id,event_time,quantity\nC1,2011-01-04,3\nC2,2011-02-09,1\n"


def _client(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


@pytest.fixture
def client(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(config_root, tmp_path / "data")


@pytest.fixture
def switched_on(config_root: Path, tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(order_log_config_root(config_root, tmp_path), tmp_path / "data")


def _add_source(client: TestClient, role: str) -> tuple[int, dict[str, object]]:
    made = client.post("/clients", json={"name": "Gifts Co", "industry": "ecommerce", "notes": ""})
    assert made.status_code == 201, made.text
    response = client.post(
        f"/clients/{made.json()['client_id']}/sources",
        files={"file": ("returns.csv", EVENTS_CSV, "text/csv")},
        data={"role": role},
    )
    return response.status_code, response.json()


def test_retail_win_back_has_no_outcome_sentence_while_the_switch_is_off(client: TestClient) -> None:
    body = {
        "use_case": "retail-win-back",
        "entity_upload_id": "u_customers",
        "event_upload_id": "u_log",
        "date_column": "InvoiceDate",
        "days": 90,
        "sentence": "Customers who placed an order within 90 days.",
        "agreed": True,
    }
    response = client.post("/doctor/outcome", json=body)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "OUTCOME_NO_SENTENCE"


@pytest.mark.parametrize("role", ["returns", "order_lines"])
def test_an_internal_role_is_unknown_to_a_client_while_the_switch_is_off(
    client: TestClient, role: str
) -> None:
    status, body = _add_source(client, role)
    assert status == 409, body
    detail = body["detail"]
    assert isinstance(detail, dict) and detail["code"] == "ROLE_UNKNOWN"
    listed = str(detail["message"]).split("Choose one of:")[1]
    assert "returns" not in listed and "order_lines" not in listed and "orders" in listed


def test_the_path_registers_an_internal_role_while_the_switch_is_on(switched_on: TestClient) -> None:
    status, body = _add_source(switched_on, "returns")
    assert status == 201, body
    profile = body["profile"]
    assert isinstance(profile, dict) and profile["role"] == "returns"


def _spec_with_audience(client: TestClient) -> tuple[int, dict[str, object]]:
    made = client.post("/clients", json={"name": "Gifts Co", "industry": "ecommerce", "notes": ""})
    assert made.status_code == 201, made.text
    response = client.post(
        f"/clients/{made.json()['client_id']}/onboarding-specs",
        json={
            "use_case": "retail-win-back",
            "entity_source_id": "src_missing",
            "mapping_ids": ["map_missing"],
            "feature_spec": {"features": []},
            "snapshot_spec": {"mode": "periodic"},
            "audience": {"role": "orders", "no_event_days": 90},
        },
    )
    return response.status_code, response.json()


def test_a_recipe_may_not_carry_an_audience_while_the_switch_is_off(client: TestClient) -> None:
    """The audience is the order-log path's (B5): off, the recipe route refuses it before reading anything else."""
    status, body = _spec_with_audience(client)
    assert status == 422, body
    detail = body["detail"]
    assert (
        isinstance(detail, dict) and detail["code"] == "AUDIENCE_NOT_ENABLED" and detail["path"] == "audience"
    )


def test_with_the_switch_on_the_audience_passes_to_the_recipes_own_checks(switched_on: TestClient) -> None:
    status, body = _spec_with_audience(switched_on)
    detail = body["detail"]
    assert status == 404 and isinstance(detail, dict) and detail["code"] != "AUDIENCE_NOT_ENABLED", body
