"""A `Meter` stand-in that returns scripted replies, for the tests of the chat loop."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from engine.generative.guardrails import Guardrails, load_policy
from engine.llm import LLMCompletion


class ScriptedMeter:
    """A `Meter` stand-in that returns scripted replies in order, or raises."""

    def __init__(self, replies: Sequence[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.rendered: list[Any] = []  # every prompt sent, so a test can assert on what reached the model

    def complete(self, rendered: Any, purpose: Any) -> LLMCompletion:
        del purpose
        self.rendered.append(rendered)
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMCompletion(
            text=reply, model_id="scripted", input_tokens=1, output_tokens=1, stop_reason="end_turn"
        )


def guardrails() -> Guardrails:
    return Guardrails(load_policy())
