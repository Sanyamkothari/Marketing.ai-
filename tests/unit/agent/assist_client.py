"""A scripted stand-in for the AI service, for the second-opinion tests (Plan K M142)."""

from __future__ import annotations

import json
from typing import Any

from engine.llm import FakeLLMClient, FakeLLMMode

__all__ = ["AssistClient"]


class AssistClient(FakeLLMClient):
    """Answers the rerank task and the leak task with whatever the test scripted, and records the prompts."""

    def __init__(self, *, orders: Any = None, columns: Any = None, raw: str | None = None) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self.orders = orders
        self.columns = columns
        self.raw = raw

    def _body(self, system: str, user: str) -> str:
        if self.raw is not None:
            return self.raw
        if "put candidate columns in a better order" in system:
            return json.dumps({"orders": self.orders if self.orders is not None else []})
        return json.dumps({"columns": self.columns if self.columns is not None else []})

    @property
    def prompts(self) -> list[str]:
        return [f"{call.system}\n{call.prompt}" for call in self.calls if call.kind == "complete"]

    @property
    def tasks(self) -> list[str]:
        return ["rerank" if "better order" in text else "leaks" for text in self.prompts]
