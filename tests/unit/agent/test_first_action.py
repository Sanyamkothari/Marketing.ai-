"""The model's answer is read for its first action (`engine.agent.loop.first_action`): a model asked for a lot at once
sends several actions, wraps them in a fence or adds a sentence, and a strict reader refused every such answer until
the turn gave up ("my answer was not in a form the engine could read")."""

from __future__ import annotations

import json

import pytest

from engine.agent.loop import first_action
from engine.agent.tools import AgentToolError

LOOK = {"action": "look_at_column", "args": {"column": "visit"}}
REPLY = {"action": "reply", "text": "Done."}


def test_one_object_is_read_as_before() -> None:
    assert first_action(json.dumps(LOOK)) == (LOOK, 0)


@pytest.mark.parametrize(
    ("answer", "more"),
    [
        (json.dumps([LOOK, REPLY]), 1),  # several actions as a list
        (json.dumps(LOOK) + "\n" + json.dumps(REPLY), 1),  # one object after another
        (
            "```json\n" + json.dumps(LOOK) + "\n```\nI will look at the column first.",
            0,
        ),  # a fence and a sentence
        ("Here is what I will do first: " + json.dumps(LOOK), 0),  # a sentence before it
        (json.dumps(LOOK) + " That is my first step.", 0),  # a sentence after it
    ],
)
def test_the_first_action_is_read_and_the_rest_are_counted(answer: str, more: int) -> None:
    assert first_action(answer) == (LOOK, more)


@pytest.mark.parametrize("answer", ["no json here", "{not json", "[1, 2]", '{"text": "no action"}', ""])
def test_an_answer_with_no_action_is_still_refused(answer: str) -> None:
    with pytest.raises(AgentToolError) as caught:
        first_action(answer)
    assert caught.value.code == "AGENT_REPLY_MALFORMED"
