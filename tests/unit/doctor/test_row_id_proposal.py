"""A file with no ID column (one row per call): a row number is proposed as the ID, with the plain warning, never added alone."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import CaseStore
from engine.doctor.decisions import decisions
from engine.doctor.diagnose import Offer
from engine.doctor.materialize import Full, materialize
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, SECOND, World, case_file, deps_for


def _calls(rows: int = 1_200) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "age": rng.integers(18, 80, rows),
            "job": rng.choice(["admin", "services", "retired"], rows),
            "duration": rng.integers(5, 900, rows),
            "campaign": rng.integers(1, 8, rows),
            "subscribed": rng.integers(0, 2, rows),
        }
    )


def _world(files: dict[str, tuple[str, pd.DataFrame]]) -> World:
    config = load_use_case(GOAL_A)
    return World(
        files={u: vs.loaded(u, name, frame, config) for u, (name, frame) in files.items()},
        offers=(Offer(config=config, stage="Acquire"),),
    )


def _open(tmp_path: Path, w: World) -> tuple[Any, Any]:
    storage = LocalStorage(tmp_path / "data")
    case = CaseStore(storage, clock=w.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=[case_file(u, w) for u in w.files]
    )
    deps = deps_for(storage, w)
    return visit.resume(CLIENT, case.case_id, deps, actor=ANALYST), deps


def _case(tmp_path: Path, frame: pd.DataFrame, name: str = "bank_calls.csv") -> Any:
    return _open(tmp_path, _world({FIRST: (name, frame)}))[0]


def _proposed(case: Any) -> list[Any]:
    return [i for i in case.items if "row number" in i.title]


def test_a_file_with_no_id_gets_a_proposed_row_number_as_its_id_with_the_warning(tmp_path: Path) -> None:
    case = _case(tmp_path, _calls())
    (item,) = _proposed(case)
    assert item.kind == "key" and item.upload_id == FIRST, "the ID decision, on the file it reads"
    assert item.state.value == "pending", "the person's yes adds it"
    assert item.payload["needs_yes"] is True and item.payload["column"] == "row_id"
    assert "SELECT row_number() OVER () AS row_id" in item.payload["detail"]
    warning = [f for f in item.payload["findings"] if f["code"] == "ROW_ID_INVENTED"]
    assert warning and "cannot be joined back to your CRM" in warning[0]["message"]
    assert item.payload["rows_out"] == 1_200 and "row_id" in item.payload["columns"]


def test_a_file_with_an_id_gets_no_row_number(tmp_path: Path) -> None:
    frame = _calls().assign(customer_id=[f"C{i}" for i in range(1_200)])
    case = _case(tmp_path, frame)
    assert not _proposed(case)
    assert [i.kind for i in case.items if i.kind == "key"] == ["key"]


def test_an_events_file_with_a_repeating_customer_id_gets_no_row_number(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in rng.integers(0, 300, 1_200)],
            "order_date": pd.date_range("2024-01-01", periods=1_200, freq="h").strftime("%Y-%m-%d"),
            "amount": rng.integers(5, 500, 1_200),
        }
    )
    case = _case(tmp_path, frame, "orders.csv")
    assert not _proposed(case)
    assert not [
        f for i in case.items for f in i.payload.get("findings", []) if f["code"] == "ROW_ID_INVENTED"
    ]


@pytest.mark.parametrize("name", ["2024_calls.csv", "calls_" + "x" * 80 + ".csv"])
def test_a_file_name_that_is_not_a_table_name_still_gets_the_proposal_and_the_visit_goes_on(
    tmp_path: Path, name: str
) -> None:
    case = _case(tmp_path, _calls(), name)
    assert len(_proposed(case)) == 1
    assert case.visit is not None and case.visit.stopped is None


def test_saying_yes_records_the_row_number_as_the_id_and_builds_it_beside_another_file(
    tmp_path: Path,
) -> None:
    customers = pd.DataFrame({"customer_id": [f"C{i}" for i in range(1_200)], "age": range(1_200)})
    w = _world({FIRST: ("customers.csv", customers), SECOND: ("bank_calls.csv", _calls())})
    case, deps = _open(tmp_path, w)
    (item,) = _proposed(case)
    assert item.upload_id == SECOND
    case = visit.decide(
        CLIENT, case.case_id, item.item_id, True, deps, expected_version=case.version, actor=ANALYST
    )
    assert decisions(case).key(SECOND) == "row_id"
    items = [i for i in case.items if i.state.value == "accepted"]
    prepared = materialize(case, items, tuple(w.files.values()), Full(), twice=False)
    assert prepared.failed == ()
    built = prepared.tables[prepared.by_upload[SECOND]]
    assert "row_id" in built.columns and len(built) == 1_200
    assert next(f.key for f in prepared.files if f.upload_id == SECOND) == "row_id"


def test_an_id_with_another_name_is_asked_about_not_replaced_by_a_row_number(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    frame = pd.DataFrame(
        {
            "Kundennummer": [f"K{i}" for i in rng.integers(0, 300, 1_200)],
            "order_date": pd.date_range("2024-01-01", periods=1_200, freq="h").strftime("%Y-%m-%d"),
            "amount": rng.integers(5, 500, 1_200),
        }
    )
    w = _world({FIRST: ("bestellungen.csv", frame)})
    case, deps = _open(tmp_path, w)
    assert not _proposed(case), "no row number is added for it"
    q = vs.question(case, "KEY_NOT_UNIQUE")
    labels = [o.label for o in q.options]
    assert labels[0].startswith("Kundennummer") and "None of these: add a row number" in labels
    assert q.recommended is None, "which column is the ID is the person's to say"
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, vs.option(q, "Kundennummer"), deps,
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert decisions(case).key(FIRST) == "Kundennummer"


def test_the_row_number_is_one_answer_the_person_may_give(tmp_path: Path) -> None:
    rng = np.random.default_rng(6)
    frame = _calls().assign(
        caller=[f"P{i}" for i in rng.integers(0, 40, 1_200)]
    )  # a text column that repeats
    w = _world({FIRST: ("bank_calls.csv", frame)})
    case, deps = _open(tmp_path, w)
    q = vs.question(case, "KEY_NOT_UNIQUE")
    case = visit.answer(
        CLIENT, case.case_id, q.question_id, vs.option(q, "None of these: add a row number"), deps,
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert decisions(case).key(FIRST) == "row_id"
