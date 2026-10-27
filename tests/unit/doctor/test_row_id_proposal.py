"""A file with no ID column (one row per call): a row number is proposed, with the plain warning, never added alone."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import CaseStore
from engine.doctor.diagnose import Offer
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, World, case_file, deps_for


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


def _case(tmp_path: Path, frame: pd.DataFrame) -> Any:
    config = load_use_case(GOAL_A)
    w = World(
        files={FIRST: vs.loaded(FIRST, "bank_calls.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    storage = LocalStorage(tmp_path / "data")
    case = CaseStore(storage, clock=w.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=[case_file(FIRST, w)]
    )
    return visit.resume(CLIENT, case.case_id, deps_for(storage, w), actor=ANALYST)


def test_a_file_with_no_id_gets_a_proposed_row_number_with_the_warning(tmp_path: Path) -> None:
    case = _case(tmp_path, _calls())
    keys = [i for i in case.items if i.kind == "key"]
    assert keys == [], "no ID is decided on the person's behalf"
    proposed = [i for i in case.items if "row number" in i.title]
    assert len(proposed) == 1
    item = proposed[0]
    assert item.state.value == "pending", "the person's yes adds it"
    assert item.payload["needs_yes"] is True
    assert "SELECT row_number() OVER () AS row_id" in item.payload["detail"]
    warning = [f for f in item.payload["findings"] if f["code"] == "ROW_ID_INVENTED"]
    assert warning and "cannot be joined back to your CRM" in warning[0]["message"]
    assert item.payload["rows_out"] == 1_200 and "row_id" in item.payload["columns"]


def test_a_file_with_an_id_gets_no_row_number(tmp_path: Path) -> None:
    frame = _calls().assign(customer_id=[f"C{i}" for i in range(1_200)])
    case = _case(tmp_path, frame)
    assert not [i for i in case.items if "row number" in i.title]
    assert [i.kind for i in case.items if i.kind == "key"] == ["key"]
