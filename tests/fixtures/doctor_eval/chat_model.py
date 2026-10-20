"""A turn-keyed scripted model for the Data Doctor's chat (`data_doctor_chat`).

`ToolingClient` plays one list of actions for a whole session. A conversation needs each message to get its own
answer, so this fake is **keyed by what the person said**: `ChatScript({"drop test accounts": [step, step, ...]})`
plays those steps, one per model call, whenever the message of the prompt contains that text, and a plain `reply`
when a turn's steps run out. A step is a JSON-able action, or a function of what the model was shown (`Seen`) that
returns one, so a script can use the ids the case gave an earlier action (`question_id`, an item's `id`).

It reads the prompt as a model would (the person's message, the lookups so far, the case as it stands) and nothing
else; every prompt it was sent is in `calls`, so a test can read exactly what left. It is what the tests and a
walkthrough of the AI path drive the chat with when no AI service is connected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from engine.llm import FakeLLMClient, FakeLLMMode

__all__ = ["ChatScript", "Seen", "Step"]

_MESSAGE: Final = re.compile(
    r"THE PERSON SAYS:\n(?P<said>.*?)\n\nWhat you have looked up this turn:", re.DOTALL
)
_STATE: Final = re.compile(r"THE CASE NOW \([^)]*\):\n(?P<state>.*?)\n\nTHE PERSON SAYS:", re.DOTALL)
_HISTORY: Final = re.compile(
    r"THE CONVERSATION SO FAR \([^)]*\):\n(?P<history>.*?)\n\nTHE CASE NOW", re.DOTALL
)
_RESULTS: Final = re.compile(r"What you have looked up this turn:\n(?P<results>.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Seen:
    """What the model was shown for one call."""

    message: str
    history: list[dict[str, Any]]
    state: dict[str, Any]
    results: list[dict[str, Any]]
    """This turn's lookups and feedback, oldest first (`evidence_id`, `tool`, `result` ... `asked`, `error`)."""

    def last(self, key: str) -> Any:
        """The newest value under `key` in this turn's feedback (an id the case just gave), or None."""
        for entry in reversed(self.results):
            if key in entry:
                return entry[key]
        return None


Step = Mapping[str, Any] | str | Callable[[Seen], Mapping[str, Any] | str]
"""An action, plain sentences a friendly model might write instead of JSON, or a function of what it was shown."""


def reply(text: str) -> dict[str, Any]:
    return {"action": "reply", "text": text}


class ChatScript(FakeLLMClient):
    """Plays `turns[key]` for a message containing `key`; a turn whose steps are used up replies and stops."""

    def __init__(self, turns: Mapping[str, Sequence[Step]]) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self.turns = {key: list(steps) for key, steps in turns.items()}
        self.seen: list[Seen] = []

    def _body(self, system: str, user: str) -> str:
        del system
        found = _MESSAGE.search(user)
        message = found.group("said") if found else ""
        seen = Seen(
            message, _load(_HISTORY, user, "history", []), _load(_STATE, user, "state", {}), _results(user)
        )
        self.seen.append(seen)
        key = next((k for k in self.turns if k in message), None)
        steps = self.turns.get(key, []) if key is not None else []
        if not steps:
            return json.dumps(reply("I have nothing more to add."))
        step = steps.pop(0)
        made = step(seen) if callable(step) else step
        return made if isinstance(made, str) else json.dumps(made)

    @property
    def asked(self) -> int:
        """How many times the model was asked."""
        return len([c for c in self.calls if c.kind == "complete"])

    @property
    def sent_text(self) -> list[str]:
        """Every prompt, system and user, exactly as the client received it."""
        return [f"{call.system}\n{call.prompt}" for call in self.calls if call.kind == "complete"]


def _load(pattern: re.Pattern[str], text: str, group: str, default: Any) -> Any:
    found = pattern.search(text)
    if not found:
        return default
    try:
        return json.loads(found.group(group))
    except ValueError:
        return default


def _results(user: str) -> list[dict[str, Any]]:
    found = _RESULTS.search(user)
    if not found or found.group("results").strip() == "none":
        return []
    try:
        value = json.loads(found.group("results"))
    except ValueError:
        return []
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []
