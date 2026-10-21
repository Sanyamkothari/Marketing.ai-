"""Answer the exact prompts with real models, in two steps, without an API key in the loop.

`RecordingLLM` is an `LLMClient` that never calls a model. Each prompt it is asked to complete is
saved as `{id, role, system, prompt, schema}` and the call raises `AnswerPending`, so the code that
asked (the agent roles) stops at that point instead of using a made-up answer. A person, or a later
workflow step with real Sonnet sub-agents, reads the saved prompts, answers each one, and writes a
JSON file of `{id: response}`.

`ReplayLLM` is an `LLMClient` that answers from such a file. When a prompt's id is not in the file it
hands the prompt to `fallback` (a `RecordingLLM`, usually), so a multi-stage run can replay the stages
already answered and record the next one.

**The id** is `<role>-<first 12 hex of SHA-256 over role, system and prompt>`. The same prompt always
has the same id, on any machine, so answers stay valid between the recording run and the replay run.
A prompt that includes an earlier answer (the repair round `ask_json` sends when an answer does not
parse) has a different id and is therefore recorded afresh.

**The role** is read from the system prompt's first markdown heading (`# Data Doctor` -> `data_doctor`);
a system prompt without one is `unknown`. **The schema** is the JSON schema `ask_json` appends to the
prompt, saved on its own so an answering step can see what shape is expected.

An answer in the file may be text (any answer a model would give, with or without a code fence) or an
already-parsed JSON object, which is written back out as JSON text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.llm import LLMCompletion, LLMError

SCHEMA_MARKER = "matching this JSON schema:\n"
"""The text `autonomy.agents.base.ask_json` puts before the schema it appends to a prompt."""

_HEADING = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


class AnswerPending(Exception):  # noqa: N818 - a control-flow signal, not an error to report
    """A prompt was recorded and has no answer yet. Deliberately not an `LLMError`: the roles fall back
    to their safe defaults on `LLMError`, and a pending answer must stop the run instead."""

    def __init__(self, prompt_id: str) -> None:
        super().__init__(f"no answer yet for prompt {prompt_id}")
        self.prompt_id = prompt_id


def role_of(system: str) -> str:
    """`data_doctor` for a system prompt that starts `# Data Doctor`; `unknown` without a heading."""
    m = _HEADING.search(system)
    if not m:
        return "unknown"
    slug = re.sub(r"[^a-z0-9]+", "_", m.group(1).lower()).strip("_")
    return slug or "unknown"


def prompt_id(role: str, system: str, prompt: str) -> str:
    digest = hashlib.sha256(json.dumps([role, system, prompt], ensure_ascii=False).encode("utf-8"))
    return f"{role}-{digest.hexdigest()[:12]}"


def schema_of(prompt: str) -> dict[str, Any] | None:
    """The JSON schema `ask_json` appended to `prompt`, or None when there is none."""
    at = prompt.rfind(SCHEMA_MARKER)
    if at == -1:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(prompt[at + len(SCHEMA_MARKER) :])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _completion(text: str, prompt: str, model_id: str) -> LLMCompletion:
    return LLMCompletion(
        text=text,
        model_id=model_id,
        input_tokens=max(1, len(prompt) // 4),
        output_tokens=max(1, len(text) // 4),
        stop_reason="end_turn",
    )


class _NoEmbeddings:
    def embed(
        self, texts: Sequence[str], *, model_id: str | None = None  # noqa: ARG002
    ) -> tuple[tuple[float, ...], ...]:
        raise LLMError(
            "LLM_INVALID_REQUEST", "this client only records or replays completions; it has no embeddings"
        )

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:  # noqa: ARG002
        return max(1, len(text) // 4)


class RecordingLLM(_NoEmbeddings):
    """Saves every prompt it is asked to complete, then raises `AnswerPending`. Calls no model."""

    def __init__(self, model_id: str = "recording") -> None:
        self._model_id = model_id
        self._records: dict[str, dict[str, Any]] = {}

    @property
    def records(self) -> list[dict[str, Any]]:
        """The recorded prompts in the order first asked, each `{id, role, system, prompt, schema}`."""
        return list(self._records.values())

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,  # noqa: ARG002
        max_tokens: int = 512,  # noqa: ARG002
        temperature: float = 0.0,  # noqa: ARG002
    ) -> LLMCompletion:
        role = role_of(system)
        pid = prompt_id(role, system, prompt)
        self._records.setdefault(
            pid,
            {"id": pid, "role": role, "system": system, "prompt": prompt, "schema": schema_of(prompt)},
        )
        raise AnswerPending(pid)

    def save(self, path: Path) -> None:
        """Write the recorded prompts to `path` as a JSON list (the parent folder is created)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of prompt records")
    return [dict(r) for r in data]


class ReplayLLM(_NoEmbeddings):
    """Answers from `{id: response}`; a prompt with no answer goes to `fallback`, else `LLMError`."""

    def __init__(
        self,
        answers: Mapping[str, str | Mapping[str, Any] | Sequence[Any]],
        *,
        fallback: RecordingLLM | None = None,
        model_id: str = "replay",
    ) -> None:
        self._answers = dict(answers)
        self._fallback = fallback
        self._model_id = model_id
        self.used: list[str] = []

    @classmethod
    def from_file(cls, path: Path, *, fallback: RecordingLLM | None = None) -> ReplayLLM:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object of {{prompt id: response}}")
        return cls(data, fallback=fallback)

    @property
    def unused(self) -> tuple[str, ...]:
        """Answer ids no prompt asked for (stale or misspelt); a caller can warn about them."""
        return tuple(sorted(set(self._answers) - set(self.used)))

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        pid = prompt_id(role_of(system), system, prompt)
        if pid in self._answers:
            self.used.append(pid)
            answer = self._answers[pid]
            text = answer if isinstance(answer, str) else json.dumps(answer)
            return _completion(text, prompt, model_id or self._model_id)
        if self._fallback is not None:
            return self._fallback.complete(
                prompt, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
            )
        raise LLMError("LLM_UNAVAILABLE", f"no replay answer for prompt {pid}")


__all__ = [
    "SCHEMA_MARKER",
    "AnswerPending",
    "RecordingLLM",
    "ReplayLLM",
    "load_records",
    "prompt_id",
    "role_of",
    "schema_of",
]
