"""The grain detector asks what one row is, whether anyone was held out and who can respond: one case each."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from engine.agent.contracts import AgentConfidence
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case
from engine.doctor.checks_grain import grain_detector
from engine.doctor.diagnose import LoadedFile, Offer
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _open(tmp_path: Path, frame: pd.DataFrame, *, outcome: str | None = None) -> tuple[Case, LoadedFile]:
    config = load_use_case(vs.GOAL_A)
    file = vs.loaded(vs.FIRST, "file.csv", frame, config)
    w = vs.World(files={vs.FIRST: file}, offers=(Offer(config=config, stage="Acquire"),))
    case = vs.open_case(LocalStorage(tmp_path), w)
    if outcome is not None:
        accepted = visit._item(
            "outcome_column",
            {"upload_id": vs.FIRST, "column": outcome},
            title=f"'{outcome}' is the outcome.",
            upload_id=vs.FIRST,
            confidence=AgentConfidence.SURE,
            evidence=(),
            now=NOW,
            ticked=True,
        )
        case = case.model_copy(update={"items": [*case.items, accepted]})
    return case, file


def _codes(case: Case, file: LoadedFile) -> tuple[list[str], list[Any]]:
    found = grain_detector()(case, (file,), None)
    return [a.code for a in found.asks], list(found.items)


def test_an_order_total_repeated_on_each_line_is_asked_about(tmp_path: Path) -> None:
    orders = 40
    frame = pd.DataFrame(
        {
            "order_id": [f"O{n // 3}" for n in range(orders * 3)],
            "sku": [f"S{n}" for n in range(orders * 3)],
            "order_total": [100 + (n // 3) * 7 for n in range(orders * 3)],
        }
    )
    case, file = _open(tmp_path, frame)
    codes, _ = _codes(case, file)
    assert codes == ["Q21_ONE_ROW"]


def test_a_contacted_column_with_no_holdout_is_asked_and_noted(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{n}" for n in range(100)],
            "contacted": ["yes" if n % 3 == 0 else "no" for n in range(100)],
        }
    )
    case, file = _open(tmp_path, frame)
    codes, items = _codes(case, file)
    assert codes == ["Q13_HOLDOUT"]
    assert [i.payload["code"] for i in items] == ["NO_HOLDOUT"] and items[0].payload["level"] == "note"


def test_a_holdout_column_means_no_question(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{n}" for n in range(100)],
            "contacted": ["yes" if n % 3 == 0 else "no" for n in range(100)],
            "control_group": ["control" if n % 10 == 0 else "test" for n in range(100)],
        }
    )
    case, file = _open(tmp_path, frame)
    assert _codes(case, file) == ([], [])


def test_customers_never_active_who_cannot_say_yes_are_asked_about(tmp_path: Path) -> None:
    rows = 200
    never = [n % 5 == 0 for n in range(rows)]
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{n}" for n in range(rows)],
            "orders_count": [0 if idle else 3 for idle in never],
            "converted": ["no" if idle or n % 4 else "yes" for n, idle in enumerate(never)],
        }
    )
    case, file = _open(tmp_path, frame, outcome="converted")
    codes, _ = _codes(case, file)
    assert codes == ["Q23_WHO_CAN_RESPOND"]
    ask = grain_detector()(case, (file,), None).asks[0]
    assert ask.options[0].effect.kind == "recipe_step"  # the step that leaves them out is ready on the answer
