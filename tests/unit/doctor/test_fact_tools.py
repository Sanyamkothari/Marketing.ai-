"""The fact functions as registry tools (#93 to #98, the design §1.3): the AI reaches them through the registry.

Each tool reads the stored case and the files of its context, and answers what the function it wraps answers (the
same suggestions, with the same ids), paged. None writes the case.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from engine.access.roles import Principal, Role
from engine.doctor import visit
from engine.doctor.case import Case, CaseFile, CaseStore
from engine.doctor.facts_goal import goal_facts, goal_fit_facts, suggest_build
from engine.doctor.goal_brief import load_goal_brief
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from engine.doctor.tools_facts import (
    M_CASE_FACT_TOOLS,
    Facts,
    Suggestion,
    detector_facts,
    key_facts,
    role_step_facts,
)
from engine.doctor.visit import VisitDeps
from engine.storage import LocalStorage
from tests.unit.doctor.test_facts_parity import _SCENARIOS, _world
from tests.unit.doctor.visit_support import ANALYST, CLIENT, T0, World

PERSON = Principal(user_id=ANALYST, username="a", roles=frozenset({Role.ANALYST}), kind="user")


def _case(tmp_path: Path, scenario: str, *, goal: bool) -> tuple[World, VisitDeps, Case]:
    goal_id, named = _SCENARIOS[scenario]
    world = _world(goal_id, named, scenario)
    deps = VisitDeps(
        storage=LocalStorage(tmp_path), load_files=world.load, offers=world.offers, clock=world.clock
    )
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    case = CaseStore(deps.storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=files
    )
    case = visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)
    if goal:
        case = visit.set_goal(
            CLIENT, case.case_id, goal_id, deps, expected_version=case.version, actor=ANALYST
        )
    return world, deps, case


def _context(world: World, deps: VisitDeps, case: Case, caller: str = "ai") -> DataToolContext:
    return DataToolContext(
        case=case,
        principal=PERSON,
        caller=caller,  # type: ignore[arg-type]
        storage=deps.storage,
        files=tuple(world.load(case)),
        offers=world.offers,
        clock=world.clock,
    )


def _ids(suggested: tuple[Suggestion, ...] | list[Suggestion]) -> list[str]:
    return [s.id for s in suggested]


def _page(name: str, ctx: DataToolContext, offset: int = 0) -> dict[str, Any]:
    return data_registry().call(name, {"offset": offset}, ctx).output


def _same(parts: tuple[Facts, ...], page: dict[str, Any]) -> None:
    shown = [s["id"] for s in page["suggested"]]
    assert shown == _ids([s for part in parts for s in part.suggested])[: len(shown)]
    assert page["left_out"] == (
        sum(len(part.suggested) for part in parts) - len(shown) + sum(part.left_out for part in parts)
    )


def test_the_tools_are_in_the_registry_and_need_a_case(tmp_path: Path) -> None:
    world, deps, case = _case(tmp_path, "customers", goal=True)
    ctx = _context(world, deps, case)
    for name in M_CASE_FACT_TOOLS:
        tool = data_registry().get(name)
        assert tool.callers == {"rules", "ai"}
        assert data_registry().find(name, ctx) is tool, name
        assert data_registry().find(name, replace(ctx, case=None)) is None, name
        assert data_registry().find(name, replace(ctx, storage=None)) is None, name
        assert data_registry().find(name, replace(ctx, caller="person")) is None, name


@pytest.mark.parametrize("scenario", ["customers", "order_log", "customers_and_orders"])
def test_the_goal_tools_answer_what_the_functions_answer(tmp_path: Path, scenario: str) -> None:
    world, deps, case = _case(tmp_path, scenario, goal=True)
    ctx = _context(world, deps, case)
    brief = load_goal_brief(_SCENARIOS[scenario][0])
    run = visit._Run(deps, ANALYST)
    _same((key_facts(run, case),), _page("key_facts", ctx))
    parts = goal_facts(run, case, brief)
    page = _page("goal_facts", ctx)
    _same(parts, page)
    assert set(page["facts"]) <= {"outcome", "time", "consent"}
    _same((suggest_build(run, case, brief),), _page("suggest_build", ctx))
    _same((role_step_facts(case),), _page("role_step_facts", ctx))
    _same((detector_facts(run, case, brief),), _page("detector_facts", ctx))


def test_with_no_goal_the_fit_is_read_and_the_goal_tools_refuse(tmp_path: Path) -> None:
    world, deps, case = _case(tmp_path, "customers", goal=False)
    ctx = _context(world, deps, case)
    run = visit._Run(deps, ANALYST)
    page = _page("goal_fit_facts", ctx)
    fit = goal_fit_facts(run, case)
    _same((fit,), page)
    assert page["facts"]["missing_for"] == list(fit.facts["missing_for"])
    for name in ("goal_facts", "suggest_build"):
        with pytest.raises(DataToolError) as refused:
            _page(name, ctx)
        assert refused.value.code == "TOOL_INPUT_INVALID"


def test_a_long_list_is_cut_to_the_budget_and_counted() -> None:
    from engine.doctor.registry import ROW_LEVEL_BUDGET, Exposure, output_size
    from engine.doctor.tools_facts import Withdraw, _paged, suggestion

    long = Facts(
        suggested=tuple(
            suggestion(Withdraw(item_id=f"i{n}", reason="r" * 300, consequence="c" * 100), "because")
            for n in range(40)
        ),
        left_out=3,
    )
    first = _paged([(None, long)], 0)
    assert output_size(Exposure.SESSION_ONLY, first.model_dump(mode="json")) <= ROW_LEVEL_BUDGET
    assert 0 < len(first.suggested) < 40
    assert first.left_out == 40 - len(first.suggested) + 3
    rest = _paged([(None, long)], len(first.suggested))
    assert _ids(rest.suggested)[0] == _ids(long.suggested)[len(first.suggested)]
    assert _paged([(None, long)], 99).suggested == ()


def test_no_tool_writes_the_case(tmp_path: Path) -> None:
    world, deps, case = _case(tmp_path, "customers", goal=True)
    ctx = _context(world, deps, case)
    before = CaseStore(deps.storage, clock=world.clock).get(CLIENT, case.case_id)
    for name in M_CASE_FACT_TOOLS:
        _page(name, ctx)
    assert CaseStore(deps.storage, clock=world.clock).get(CLIENT, case.case_id) == before
