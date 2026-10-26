"""A replacement query in the chat is tried on the lines, and the plan it leaves still runs as it was tried."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from engine.agent.contracts import ProposalState
from engine.doctor import registry as doctor_registry
from engine.doctor.plan_check import check_plan
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript, Seen
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor.chat_support import Talk, ai_items
from tests.unit.doctor.visit_support import FIRST, World

PER_CUSTOMER = "SELECT customer_id, count(*) AS orders FROM t_orders GROUP BY ALL"
ON_LINES = "SELECT customer_id, max(order_date) AS last_order FROM t_orders GROUP BY ALL"
BAD_CODES = {"PLAN_QUERY_TRIED_BEFORE_CHANGE", "PLAN_STEP_CANNOT_RUN"}


@pytest.fixture(autouse=True)
def unified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_registry, "unified_on", lambda _config: True)


@pytest.fixture
def world() -> World:
    return cs.orders_world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def sql(text: str, name: str, **more: Any) -> Callable[[Seen], dict[str, Any]]:
    def step(seen: Seen) -> dict[str, Any]:
        del seen
        return {
            "action": "propose_sql",
            "file": "F1",
            "args": {
                "description": f"Table {name}.",
                "sql": text,
                "output_name": name,
                "files": ["F1"],
                "expect": {"rows_out_min": 1},
                **more,
            },
        }

    return step


def handle_of(seen: Seen, containing: str) -> str:
    return str(next(p["id"] for p in seen.state["plan"] if containing in p["what"]))


def problems(talk: Talk, world: World) -> set[str]:
    accepted = [
        i.model_copy(update={"state": ProposalState.ACCEPTED})
        for i in talk.case.items
        if i.state is ProposalState.PENDING
    ] + [i for i in talk.case.items if i.state is ProposalState.ACCEPTED]
    files = [world.files[FIRST]]
    return {f.code for f in check_plan(files, list(talk.case.files), accepted).findings} & BAD_CODES


def test_a_replacement_query_is_tried_on_the_lines_and_the_plan_it_leaves_runs(
    storage: LocalStorage, world: World
) -> None:
    script: dict[str, list[Any]] = {
        "first table": [sql(PER_CUSTOMER, "per_customer"), cs_reply("Added a table per customer.")],
        "last order": [
            lambda seen: sql(ON_LINES, "last_order", replaces=handle_of(seen, "per_customer"))(seen),
            cs_reply("I took my table back and made one with the last order."),
        ],
    }
    talk = Talk(storage, world, ChatScript(script))
    talk.goal()
    talk.say("first table")
    talk.say("last order")
    mine = {i.payload["step"]["params"]["output_name"]: i for i in ai_items(talk.case) if "step" in i.payload}
    assert (
        mine["last_order"].state is ProposalState.PENDING
    ), "tried on the lines, not on the table it replaces"
    assert mine["per_customer"].state is not ProposalState.PENDING, "the earlier one was taken back"
    live = [i for i in talk.case.items if i.state in (ProposalState.PENDING, ProposalState.ACCEPTED)]
    assert [i.item_id for i in live if "step" in i.payload and i.source.value == "ai"] == [
        mine["last_order"].item_id
    ]
    assert not problems(talk, world)


def cs_reply(text: str) -> dict[str, Any]:
    return {"action": "reply", "text": text}


def test_a_query_after_a_rejected_earlier_query_is_tried_on_the_lines(
    storage: LocalStorage, world: World
) -> None:
    script: dict[str, list[Any]] = {
        "first table": [sql(PER_CUSTOMER, "per_customer"), cs_reply("Added a table per customer.")],
        "last order": [sql(ON_LINES, "last_order"), cs_reply("Made a table with the last order.")],
    }
    talk = Talk(storage, world, ChatScript(script))
    talk.goal()
    talk.say("first table")
    (earlier,) = [i for i in ai_items(talk.case) if "step" in i.payload]
    talk.decide(earlier.item_id, False)
    talk.say("last order")
    made = [
        i
        for i in ai_items(talk.case)
        if i.payload.get("step", {}).get("params", {}).get("output_name") == "last_order"
    ]
    assert made and made[0].state is ProposalState.PENDING
    assert not problems(talk, world)
