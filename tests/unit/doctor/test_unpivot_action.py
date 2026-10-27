"""`propose_unpivot`: a wide monthly sheet turned into rows by the AI, with the year asked when the names have none."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.unpivot import UnpivotError, UnpivotSpec, find_wide_columns, unpivot
from engine.doctor.actions import ACTIONS, action_lines
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_agent_review import Script, _answer_key, _guard, _opened, _review
from tests.unit.doctor.visit_support import FIRST, World

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _wide(world: World, *, year: str = "") -> World:
    frame = vs.customers(seed=1)
    rng = np.random.default_rng(5)
    for month in MONTHS:
        frame[f"spend_{year}{month}" if not year else f"spend_{year}_{month}"] = np.round(
            rng.gamma(2.0, 100.0, len(frame)), 2
        )
    return World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", frame, world.offers[0].config)},
        offers=world.offers,
    )


def _unpivot(**args: object) -> dict[str, object]:
    return {"action": "propose_unpivot", "file": "F1", "args": args}


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def test_the_action_is_offered_to_the_review_prompt() -> None:
    assert "propose_unpivot" in {a.name for a in ACTIONS}
    assert any(line.startswith('`{"action": "propose_unpivot"') for line in action_lines("review"))


def test_months_with_no_year_ask_for_it_then_become_a_pending_step(storage: LocalStorage) -> None:
    world = _wide(vs.world())
    client = Script([_unpivot(), _unpivot(year=2025)])
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    prompts = [c.prompt for c in client.calls if c.kind == "complete"]
    assert "AGENT_UNPIVOT_NEEDS_YEAR" in prompts[1], "the model is told to ask which year first"
    ai_items = [i for i in case.items if i.source is ProposalSource.AI]
    assert len(ai_items) == 1
    item = ai_items[0]
    assert item.state is ProposalState.PENDING and item.confidence is AgentConfidence.CHECK
    step = item.payload["step"]
    assert isinstance(step, dict) and step["kind"] == "unpivot" and step["column"] == "customer_id"
    assert step["params"]["year"] == 2025 and len(step["params"]["value_columns"]) == 12


def test_a_year_in_the_names_needs_no_question(storage: LocalStorage) -> None:
    world = _wide(vs.world(), year="2025")
    client = Script([_unpivot()])
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    step = next(i for i in case.items if i.source is ProposalSource.AI).payload["step"]
    assert isinstance(step, dict) and "year" not in step["params"]


def test_the_given_year_dates_the_months_in_the_rows() -> None:
    frame = pd.DataFrame({"cid": ["a", "b"], **{f"spend_{m}": [1.0, 2.0] for m in MONTHS[:3]}})
    group = find_wide_columns(list(frame.columns))
    assert group is not None and not group.dated
    params = group.params(key="cid", id_columns=["cid"], outcome=None, taken=frame.columns, year=2025)
    rows = unpivot(frame, key="cid", params=params, max_failure_pct=1.0).frame
    assert list(rows["month"].dt.strftime("%Y-%m")) == ["2025-01", "2025-02", "2025-03"] * 2
    with pytest.raises(UnpivotError):
        UnpivotSpec.from_params("cid", {**params, "year": 25})
