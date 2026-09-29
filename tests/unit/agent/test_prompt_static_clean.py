"""The instructions the model is given never trip the egress backstop.

`egress.assert_clean` re-scans the whole rendered prompt as the last check before it leaves, and a hit
is recorded as `EGRESS_LATE_MASK`: a signal that something upstream leaked. Text we wrote ourselves
(the tool catalogue with each argument's bounds, the prompt template) must never raise that signal, or
a real alarm would drown among false ones. A bound such as `"maximum": 100000000` once did: the phone
scanner reads any run of seven or more digits, so the tool list the model saw said
`"maximum": [REDACTED:phone]` and every turn logged a late mask.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.agent import egress
from engine.agent.loop import _tools

PROMPT = Path(__file__).resolve().parents[3] / "configs" / "prompts" / "data_agent.v1.md"


@pytest.mark.parametrize("tool", _tools(), ids=lambda tool: tool["name"])
def test_a_tool_as_shown_to_the_model_is_untouched_by_the_backstop(tool: dict[str, str]) -> None:
    for part in ("description", "args"):
        cleaned, matches = egress.assert_clean(tool[part])
        assert matches == 0 and cleaned == tool[part], (
            f"{tool['name']} {part}: the backstop would change {tool[part]!r} to {cleaned!r}; keep numbers in "
            "a tool's argument bounds below seven digits"
        )


def test_the_prompt_template_itself_is_untouched_by_the_backstop() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        cleaned, matches = egress.assert_clean(line)
        assert matches == 0, f"data_agent.v1.md line {number} would be masked: {line!r} -> {cleaned!r}"
