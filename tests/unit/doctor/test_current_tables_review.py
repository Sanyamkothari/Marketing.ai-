"""The current tables' guarantees: a proposed fix reads the table as the run will see it, a built table keeps the personal
marks of what it reads, the cache keys on everything it reads, and the plan's tables show only what was approved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import load_use_case
from engine.doctor import current_tables as ct
from engine.doctor import visit
from engine.doctor.case import CaseStore
from engine.doctor.cleaning import cleaning_detector
from engine.doctor.current_tables import current_tables
from engine.doctor.diagnose import Offer
from engine.doctor.materialize import Full, materialize
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, World, open_case

GOAL = "retail-win-back"


def _ai_query(
    world: World, sql: str, name: str, *, reason: str = "x", extra: dict[str, Any] | None = None
) -> Any:
    step = {
        "order": 1,
        "kind": "sql_query",
        "column": name,
        "reason": reason,
        "params": {"sql": sql, "inputs": ["customers_oct"], "output_name": name, "description": reason},
    }
    return visit._item(
        "recipe_step",
        {"step": step, "stage": "post_build", **(extra or {})},
        title="t",
        upload_id=None,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=world.clock(),
        ticked=False,
        source=ProposalSource.AI,
    )


def _orders(extra: dict[str, Any] | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    ids = [f"C{1000 + i % 150}" for i in range(600)]
    data: dict[str, Any] = {
        "customer_id": ids,
        "order_date": ["2026-09-01"] * 600,
        "amount": rng.integers(10, 120, 600),
    }
    data.update(extra or {})
    return pd.DataFrame(data)


def _world(frame: pd.DataFrame) -> World:
    config = load_use_case(GOAL)
    return World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", frame, config)},
        offers=(Offer(config=config, stage="Retain"),),
    )


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def test_a_fix_proposed_after_an_accepted_rename_still_runs(tmp_path: Path) -> None:
    config = load_use_case(vs.GOAL_A)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(300)],
            "spend": [f"₹{1200 + 3 * i:,}" for i in range(300)],
            "age": np.arange(300) % 60 + 18,
        }
    )
    world = World(
        files={FIRST: vs.loaded(FIRST, "spend.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = CaseStore(LocalStorage(tmp_path / "d"), clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=[vs.case_file(FIRST, world)]
    )
    files = tuple(world.files.values())
    rename = visit._item(
        "recipe_step",
        {"step": {"order": 1, "kind": "rename_column", "column": "spend", "new_column": "money", "params": {}, "reason": "x"}},
        title="rename", upload_id=FIRST, confidence=AgentConfidence.SURE, evidence=(), now=world.clock(),
        ticked=True, source=ProposalSource.RULES,
    ).model_copy(update={"state": ProposalState.ACCEPTED})  # fmt: skip
    case = case.model_copy(update={"items": (rename,)})
    current = current_tables(case, [rename], files, config=config)
    assert "spend" in current.files[0].frame.columns, "a rename comes after the value fixes: not applied to F"
    found = cleaning_detector(world.offers)(case, current.files, None)
    fixes = [
        i.model_copy(update={"state": ProposalState.ACCEPTED}) for i in found.items if i.payload.get("step")
    ]
    assert fixes
    plan = [rename, *fixes]
    prepared = materialize(case.model_copy(update={"items": tuple(plan)}), plan, files, Full(), twice=False)
    assert not prepared.failed


def test_a_built_column_that_reads_a_personal_column_is_personal(storage: LocalStorage) -> None:
    names = ["Asha Rao", "Ravi Kumar", "Meera Iyer", "John Smith", "Priya Nair"]
    ids = [f"C{1000 + i % 150}" for i in range(600)]
    world = _world(_orders({"full_name": [names[int(x[1:]) % 5] for x in ids]}))
    case = open_case(storage, world)
    files = tuple(world.files.values())
    assert any(c.pii_kinds for c in files[0].profile.columns if c.name == "full_name")
    sql = (
        "SELECT customer_id, MAX(full_name) AS who, MAX(CAST(order_date AS DATE)) AS last_order "
        "FROM t_customers_oct GROUP BY customer_id"
    )
    q = _ai_query(world, sql, "t")
    (table,) = current_tables(case, [q], files, config=world.offers[0].config).built
    marks = {c.name: c.pii_kinds for c in table.loaded.profile.columns}
    assert marks["who"] and not marks["last_order"]
    nested = _ai_query(world, f"SELECT customer_id, who AS w2 FROM ({sql}) s", "t2")
    (inner,) = current_tables(case, [nested], files, config=world.offers[0].config).built
    assert {c.name: c.pii_kinds for c in inner.loaded.profile.columns}["w2"]


def test_an_accepted_key_item_invalidates_the_cache(storage: LocalStorage) -> None:
    ids = [f"C{1000 + i % 150}" for i in range(600)]
    world = _world(_orders({"client_ref": [f"R{x}" for x in ids]}))
    case = open_case(storage, world)
    files = tuple(world.files.values())
    config = world.offers[0].config
    sql = (
        "SELECT customer_id, client_ref, MAX(CAST(order_date AS DATE)) AS last_order FROM t_customers_oct "
        "GROUP BY customer_id, client_ref"
    )
    q = _ai_query(world, sql, "t")
    before = current_tables(case, [q], files, config=config)
    key = visit._item(
        "key", {"upload_id": FIRST, "column": "client_ref"}, title="k", upload_id=FIRST,
        confidence=AgentConfidence.SURE, evidence=(), now=world.clock(), ticked=True,
    ).model_copy(update={"state": ProposalState.ACCEPTED})  # fmt: skip
    after = current_tables(case, [q, key], files, config=config)
    assert after is not before
    ct._CACHE.clear()
    assert current_tables(case, [q, key], files, config=config).built[0].key == after.built[0].key


def test_a_pending_step_does_not_change_a_built_table(storage: LocalStorage) -> None:
    world = _world(_orders())
    case = open_case(storage, world)
    files = tuple(world.files.values())
    config = world.offers[0].config
    q = _ai_query(
        world, "SELECT customer_id, SUM(amount) AS total FROM t_customers_oct GROUP BY customer_id", "t"
    )
    remove = visit._item(
        "recipe_step",
        {"step": {"order": 2, "kind": "remove_rows", "column": "amount", "params": {"operator": "before", "values": ["60"]}, "reason": "x"}},
        title="r", upload_id=FIRST, confidence=AgentConfidence.CHECK, evidence=(), now=world.clock(),
        ticked=False, source=ProposalSource.RULES,
    )  # fmt: skip
    assert remove.state is ProposalState.PENDING
    plain = current_tables(case, [q], files, config=config).built[0].loaded.frame
    withpending = current_tables(case, [q, remove], files, config=config).built[0].loaded.frame
    assert plain["total"].sum() == withpending["total"].sum()


def test_a_built_outcome_found_by_the_steps_own_words(storage: LocalStorage) -> None:
    from engine.doctor.goal_brief import load_goal_brief
    from engine.doctor.visit import _built_outcomes

    world = _world(_orders())
    case = open_case(storage, world)
    files = tuple(world.files.values())
    sql = (
        "SELECT customer_id, CASE WHEN SUM(amount) > 260 THEN 1 ELSE 0 END AS repeat_order, "
        "CASE WHEN SUM(amount) > 0 THEN 1 ELSE 1 END AS always_one FROM t_customers_oct GROUP BY customer_id"
    )
    q = _ai_query(world, sql, "t", reason="placed another order in the 90 days after the snapshot")
    case = case.model_copy(update={"items": (q,)})
    current = current_tables(case, [q], files, config=world.offers[0].config)
    found = _built_outcomes(current, load_goal_brief(GOAL), case)
    assert [c for _f, c in found] == ["repeat_order"], "a constant column is no yes/no"


def test_the_ai_can_look_at_the_upload_as_it_came_in(storage: LocalStorage) -> None:
    from tests.unit.doctor.test_agent_review import Script, _guard, _review

    config = load_use_case(vs.GOAL_A)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(300)],
            "spend": [f"₹{1200 + 3 * i:,}" for i in range(300)],
        }
    )
    world = World(
        files={FIRST: vs.loaded(FIRST, "spend.csv", frame, config)},
        offers=(Offer(config=config, stage="Acquire"),),
    )
    case = open_case(storage, world)
    found = cleaning_detector(world.offers)(case, tuple(world.files.values()), None)
    fix = next(i for i in found.items if i.payload.get("step"))
    case = case.model_copy(update={"items": (fix.model_copy(update={"state": ProposalState.ACCEPTED}),)})
    client = Script(
        [
            {"action": "get_profile", "file": "F1", "args": {}},
            {"action": "get_profile", "file": "F1", "args": {"raw": True}},
            {"action": "finish", "text": "done"},
        ]
    )
    result = _review(_guard(client))(case, tuple(world.files.values()), None)
    profiles = [r for r in result.evidence if r.tool == "get_profile"]
    assert len(profiles) == 2
    assert profiles[0].output != profiles[1].output, "the raw upload still has the text money"
