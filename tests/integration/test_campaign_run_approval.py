"""A campaign run starts only from a Data Doctor case its person approved, as it stands."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.doctor import visit
from engine.doctor.case import Case, CaseStore
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import CLIENT

pytestmark = pytest.mark.integration

BODY = {"use_case": "telco-churn", "upload_id": "u_missing", "primary_key": "customer_id"}


@pytest.fixture
def served(config_root: Path, tmp_path: Path) -> Iterator[tuple[TestClient, LocalStorage]]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as client:
        yield client, LocalStorage(tmp_path)


def _code(answer: Any) -> str:
    return str(answer.json()["detail"]["code"])


def _case_through(storage: LocalStorage, steps: int) -> Case:
    w = vs.world()
    case = vs.open_case(storage, w)
    for _name, _op, act in vs.SCRIPT[:steps]:
        case = act(vs.deps_for(storage, w), CaseStore(storage).get(CLIENT, case.case_id))
    return case


def test_a_case_that_is_not_approved_cannot_start_a_campaign_run(
    served: tuple[TestClient, LocalStorage],
) -> None:
    client, storage = served
    waiting = _case_through(storage, 4)  # everything answered, waiting at Approve
    assert not visit.is_approved(waiting)
    answer = client.post("/campaign-runs", json={**BODY, "case_id": waiting.case_id})
    assert answer.status_code == 409 and _code(answer) == "CASE_NOT_APPROVED"


def test_a_case_that_does_not_exist_cannot_start_one_either(served: tuple[TestClient, LocalStorage]) -> None:
    client, _storage = served
    answer = client.post("/campaign-runs", json={**BODY, "case_id": "case_0123456789abcdef"})
    assert answer.status_code == 404 and _code(answer) == "CASE_NOT_FOUND"


def test_an_approved_case_passes_the_check(served: tuple[TestClient, LocalStorage]) -> None:
    client, storage = served
    approved = _case_through(storage, 6)  # ... and approved
    assert visit.is_approved(approved)
    answer = client.post("/campaign-runs", json={**BODY, "case_id": approved.case_id})
    assert _code(answer) != "CASE_NOT_APPROVED"  # it goes on to the next check (the missing upload)


def test_a_change_after_approval_takes_the_approval_away(served: tuple[TestClient, LocalStorage]) -> None:
    client, storage = served
    approved = _case_through(storage, 6)
    item = next(i for i in approved.items if i.state.value == "accepted")
    changed = visit.decide(
        CLIENT,
        approved.case_id,
        item.item_id,
        False,
        vs.deps_for(storage, vs.world()),
        expected_version=approved.version,
        actor=vs.ANALYST,
    )
    assert not visit.is_approved(changed)
    answer = client.post("/campaign-runs", json={**BODY, "case_id": approved.case_id})
    assert answer.status_code == 409 and _code(answer) == "CASE_NOT_APPROVED"
