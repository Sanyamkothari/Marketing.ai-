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


def test_a_real_newline_inside_a_text_is_read() -> None:
    answer = '{"action": "reply", "text": "First line.\nSecond line."}'
    assert first_action(answer) == ({"action": "reply", "text": "First line.\nSecond line."}, 0)


def test_the_models_thinking_is_dropped_before_reading() -> None:
    answer = '<think>I could write {"action": "reply"} but first look. [1, 2]</think>\n' + json.dumps(LOOK)
    assert first_action(answer) == (LOOK, 0)


def test_the_first_object_that_names_an_action_is_taken_wherever_it_is() -> None:
    answer = (
        'Columns {a, b} matter. {"note": 1} Then ```json\n'
        + json.dumps(LOOK)
        + "\n``` and "
        + json.dumps(REPLY)
    )
    assert first_action(answer) == (LOOK, 1)


def test_a_later_action_after_a_sentence_is_counted() -> None:
    assert first_action(json.dumps(LOOK) + " then " + json.dumps(REPLY) + " and " + json.dumps(LOOK))[1] == 2


@pytest.mark.parametrize(
    "answer",
    [
        '{"name": "look_at_column", "arguments": {"column": "visit"}}',
        '{"tool": "look_at_column", "parameters": {"column": "visit"}}',
        '{"function": "look_at_column", "arguments": "{\\"column\\": \\"visit\\"}"}',
    ],
)
def test_the_tool_call_shapes_of_other_models_are_read(answer: str) -> None:
    assert first_action(answer) == ({"action": "look_at_column", "args": {"column": "visit"}}, 0)


def test_the_malformed_refusal_says_how_to_write_a_new_line() -> None:
    with pytest.raises(AgentToolError) as caught:
        first_action("no json")
    assert "\\n" in str(caught.value.refusal.what_failed)
