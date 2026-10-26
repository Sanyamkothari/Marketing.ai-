"""The order-log detector never fires on a parity file (v3 phase 0, B2; the proposal's section 7, point 2).

Turning `data_doctor.order_log` on must not move the parity numbers or add a question, so no file the parity
checklist runs may read as an order log: Plan G's benchmark files, the Hinglish files and the M160 benchmark's
thirty. Each is read under the use case its own run uses.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from engine.config import load_use_case
from engine.doctor.order_log import order_log_reading
from scripts.plan_l_benchmark import Messy, cases, grid
from tests.fixtures.agent_bench.cases import CASE_USE_CASES, CASES, HINGLISH_PREFIX, USE_CASE
from tests.unit.doctor.order_log_support import loaded

PLAN_G_CASES = sorted(name for name in CASES if not name.startswith(HINGLISH_PREFIX))
HINGLISH_CASES = sorted(name for name in CASES if name.startswith(HINGLISH_PREFIX))


def _read(frame: pd.DataFrame, name: str, use_case: str, config_root: Path) -> bool:
    config = load_use_case(use_case, config_root)
    return order_log_reading(loaded(frame, f"{name}.csv", config_root, use_case=use_case), config) is not None


def test_the_parity_files_are_the_ones_the_checklist_counts() -> None:
    assert len(PLAN_G_CASES) == 25 and len(HINGLISH_CASES) == 27
    assert len(cases()) == 30


@pytest.mark.parametrize("name", PLAN_G_CASES)
def test_it_never_fires_on_a_plan_g_file(name: str, config_root: Path) -> None:
    assert not _read(CASES[name](), name, CASE_USE_CASES.get(name, USE_CASE), config_root)


@pytest.mark.parametrize("name", HINGLISH_CASES)
def test_it_never_fires_on_a_hinglish_file(name: str, config_root: Path) -> None:
    assert not _read(CASES[name](), name, CASE_USE_CASES.get(name, USE_CASE), config_root)


def _frame_of(case: Messy, config_root: Path) -> pd.DataFrame:
    """The benchmark file's table as intake reads it: below its title rows, above its footer, names made unique."""
    rows = grid(case, config_root)
    header_at = next(i for i, row in enumerate(rows) if all(cell not in (None, "") for cell in row))
    body = [row for row in rows[header_at + 1 :] if not str(row[0]).startswith(("Grand Total", "Generated"))]
    seen: dict[str, int] = {}
    names: list[str] = []
    for cell in rows[header_at]:
        count = seen.get(str(cell), 0)
        seen[str(cell)] = count + 1
        names.append(str(cell) if count == 0 else f"{cell}_{count + 1}")
    return pd.DataFrame(body, columns=names)


@pytest.mark.parametrize("case", cases(), ids=lambda case: case.name)
def test_it_never_fires_on_an_m160_file(case: Messy, config_root: Path) -> None:
    frame = _frame_of(case, config_root)
    assert len(frame) > 1_000  # the file was read whole, not as its title rows
    assert not _read(frame, case.name, case.use_case, config_root)


def test_the_control_the_detector_can_fire_on_an_order_log_without_its_outcome(config_root: Path) -> None:
    """The parity result is not vacuous: the same multi-row order log is read as one once the columns that name its
    outcome are taken away (`reactivated_90d`, and `returned`, which the use case lists as a synonym of it), and
    only those keep it out as it is."""
    frame = CASES["multi_row"]()
    bare = frame.drop(columns=["reactivated_90d", "returned"])
    assert _read(bare, "multi_row", "retail-win-back", config_root)
    assert not _read(frame, "multi_row", "retail-win-back", config_root)
    assert not _read(
        bare.drop(columns=["snapshot_date", "order_date"]), "no_dates", "retail-win-back", config_root
    )
