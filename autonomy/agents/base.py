"""Shared plumbing for every agent: ask an LLM for JSON that must parse into a pydantic model.

Agents talk to models only through `engine.llm.LLMClient` (v1's protocol: Bedrock, Anthropic,
OpenAI-compatible, or a fake). `ask_json` asks once, and if the answer does not parse into the
model it asks once more with the error, then raises `AgentOutputError`. Callers decide the fallback.

`ScriptedLLM` is the test double: it answers from a function of (system, prompt), so tests can
script exact JSON replies. (v1's `FakeLLMClient` answers with digests, which no agent can parse.)
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from engine.llm import LLMClient, LLMCompletion

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

PROMPTS_DIR = Path(__file__).parent / "prompts"
"""Where every agent prompt lives, one markdown file per prompt."""


class AgentOutputError(RuntimeError):
    """The model did not produce valid output after a repair attempt."""


@cache
def load_prompt(name: str) -> str:
    """A prompt from `autonomy/agents/prompts/<name>.md`. Prompts are files so they can be reviewed."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def extract_json(text: str) -> str:
    """The JSON part of a model answer: a fenced block if there is one, else the outermost {...}."""
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def ask_json(
    llm: LLMClient,
    model: type[M],
    *,
    system: str,
    prompt: str,
    model_id: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
) -> M:
    schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
    full = f"{prompt}\n\nAnswer with one JSON object only, matching this JSON schema:\n{schema}"
    answer = llm.complete(
        full, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
    )
    try:
        return model.model_validate_json(extract_json(answer.text))
    except (ValidationError, ValueError) as first:
        retry = (
            f"{full}\n\nYour previous answer was not valid:\n{answer.text[:2000]}\n\nThe error was:\n{first}\n"
            "Answer again with one corrected JSON object only."
        )
        again = llm.complete(
            retry, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
        )
        try:
            return model.model_validate_json(extract_json(again.text))
        except (ValidationError, ValueError) as second:
            raise AgentOutputError(f"{model.__name__}: {second}") from second


class ScriptedLLM:
    """An `LLMClient` whose answers come from `script(system, prompt) -> text`. Records every call."""

    def __init__(self, script: Callable[[str, str], str], model_id: str = "scripted") -> None:
        self._script = script
        self._model_id = model_id
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,  # noqa: ARG002
        temperature: float = 0.0,  # noqa: ARG002
    ) -> LLMCompletion:
        self.calls.append((system, prompt))
        text = self._script(system, prompt)
        return LLMCompletion(
            text=text,
            model_id=model_id or self._model_id,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            stop_reason="end_turn",
        )

    def embed(
        self, texts: Sequence[str], *, model_id: str | None = None  # noqa: ARG002
    ) -> tuple[tuple[float, ...], ...]:
        """Bag-of-letters vectors: crude, deterministic, and enough for nearest-centroid tests."""
        out = []
        for t in texts:
            v = [0.0] * 26
            for ch in t.lower():
                if "a" <= ch <= "z":
                    v[ord(ch) - 97] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append(tuple(x / norm for x in v))
        return tuple(out)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:  # noqa: ARG002
        return max(1, len(text) // 4)
