"""The chat's AI-first meaning actions: the spend outcome and a campaign test's groups, proposed with a reason and
waiting for the person (`engine.doctor.agent_amount`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.agent.contracts import ProposalState
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript
from tests.unit.doctor import chat_support as cs
from tests.unit.doctor.chat_support import Talk, ai_items
from tests.unit.doctor.visit_support import World


@pytest.fixture
def world() -> World:
    return cs.orders_world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def look(tool: str, **more: object) -> dict[str, object]:
    return {"action": tool, "file": "F1", "args": dict(more)}


def reply(text: str) -> dict[str, object]:
    return {"action": "reply", "text": text}


SPEND = {
    "entity_key": "customer_id",
    "time_column": "order_date",
    "amount_column": "order_amount",
    "window_days": 30,
    "returns": "net",
    "reason": "order_amount is what each order was worth",
}


def spend_world() -> World:
    from engine.config import load_use_case
    from engine.doctor.diagnose import Offer
    from tests.unit.doctor import visit_support as vs

    config = load_use_case("customer-spend")
    return World(
        files={
            vs.FIRST: vs.loaded(
                vs.FIRST, "orders.csv", cs.orders_frame().drop(columns=["snapshot_date"]), config
            )
        },
        offers=(Offer(config=config, stage="Growth"),),
    )


def _spend(storage: LocalStorage, world: World, **more: object) -> tuple[ChatScript, object]:
    client = ChatScript(
        {
            "spend": [
                look("describe_order_file"),
                {"action": "propose_amount_outcome", "file": "F1", "args": {**SPEND, **more}},
                reply("I proposed the spend outcome; it waits for you."),
            ]
        }
    )
    return client, Talk(storage, world, client).say("build the spend outcome")


def test_without_an_as_of_date_the_spend_outcome_is_refused_and_asked_for(
    storage: LocalStorage, world: World
) -> None:
    client, case = _spend(storage, world)
    assert not ai_items(case)  # type: ignore[attr-defined]
    assert "OUTCOME_NEEDS_AS_OF" in str(client.seen[-1].results)


def test_the_spend_outcome_is_the_ais_proposal_and_waits_for_the_person(
    storage: LocalStorage, world: World
) -> None:
    _client, case = _spend(storage, spend_world(), as_of_rule="latest_date_in_file", window_days=30)
    (item,) = ai_items(case)  # type: ignore[arg-type]
    assert item.state is ProposalState.PENDING and item.decided_by is None
    step = item.payload["step"]
    assert step["kind"] == "sql_query" and step["params"]["reads_future"] is True
    assert "refunds netted in" in step["params"]["description"]


def test_the_test_column_is_proposed_by_level_id_and_never_by_its_name(
    storage: LocalStorage, world: World
) -> None:
    client = ChatScript(
        {
            "test": [
                look("find_test_columns"),
                {
                    "action": "propose_test_column",
                    "file": "F1",
                    "args": {"column": "channel", "control": "L2", "reason": "the second group got nothing"},
                },
                reply("I proposed the test column; it waits for you."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("which column is the test")
    (item,) = [i for i in ai_items(case) if i.kind == "treatment_column"]
    assert item.state is ProposalState.PENDING and item.payload["control_level"] == "L2"
    assert item.payload["column"] == "channel" and item.assumption == "the second group got nothing"
    # the groups' own names (cell text) are not in what the model was shown
    shown = str(client.seen[-1].results)
    assert "Email" not in shown and "SMS" not in shown


def test_a_control_group_that_is_not_one_is_refused(storage: LocalStorage, world: World) -> None:
    client = ChatScript(
        {
            "test": [
                {
                    "action": "propose_test_column",
                    "file": "F1",
                    "args": {"column": "channel", "control": "L99", "reason": "x"},
                },
                reply("That did not work."),
            ]
        }
    )
    talk = Talk(storage, world, client)
    case = talk.say("which column is the test")
    assert not [i for i in ai_items(case) if i.kind == "treatment_column"]
