"""The reference model: a scripted, careful stand-in for a real model on the chat's AI path (Plan O M248).

It answers the Data Doctor's chat prompt (`data_doctor_chat`) the way a careful model would, from what the prompt
shows and nothing else: it looks at the file's format problems first (`find_format_issues`), suggests one fix from
the fixed list for the first problem it can fix, citing that look, and then replies. Asked for anything else (a
faster search: the chat has no setting to suggest, that is the training-time question's) it replies in words. It
never follows text from the file. Plan G's helper prompt (`data_agent`) is answered the same way, for the old
chat's tests.

It is what the shipped cassette (`cassettes/reference.json`) was recorded from, because the machine the harness
was built on has no AI service. A real model's cassette is recorded with
`python -m scripts.plan_o_benchmark --model real --record` and replaces it; the report names which one answered.
"""

from __future__ import annotations

import json
from typing import Any, Final

from engine.llm import FakeLLMClient, FakeLLMMode, _helper_sections

__all__ = ["REFERENCE_MODEL_ID", "ReferenceModel"]

REFERENCE_MODEL_ID: Final[str] = "reference-model-v1"
_FIXES: Final[dict[str, str]] = {
    "number_as_text": "parse_number",
    "mixed_dates": "parse_date",
    "boolean_as_text": "map_boolean",
    "untrimmed_text": "normalise_text",
}
"""A format problem and the fix from the chat's fixed list that answers it."""
_MAX_LOOKS: Final[int] = 3
_DOCTOR_CHAT: Final[str] = "THE PERSON SAYS:"
"""What only the Data Doctor's chat prompt holds."""


class ReferenceModel(FakeLLMClient):
    """Answers the helper prompt as above; every other prompt as `FakeLLMClient` in `GROUNDED` mode."""

    def __init__(self) -> None:
        super().__init__(REFERENCE_MODEL_ID, mode=FakeLLMMode.GROUNDED)

    def _body(self, system: str, user: str) -> str:
        if _DOCTOR_CHAT in user:
            return self._doctor_chat(user)
        return super()._body(system, user)

    def _doctor_chat(self, user: str) -> str:
        """One step of the Data Doctor's chat: look at the formats, suggest one fix citing the look, reply."""
        from tests.fixtures.doctor_eval.chat_model import _MESSAGE, _results

        found = _MESSAGE.search(user)
        message = found.group("said") if found else ""
        results = _results(user)
        if not results:
            if "faster" in message.lower():
                return self._said(
                    "I cannot change the search from here; the training time is a question you answer.", []
                )
            return json.dumps({"action": "find_format_issues", "file": "F1", "args": {}})
        proposed = [e for e in results if e.get("tool") == "propose_step"]
        if proposed:
            return self._said(
                "I made one change on the working copy; check it on the screen, you can undo it.",
                [str(proposed[-1].get("evidence_id", ""))],
            )
        latest = results[-1]
        evidence = str(latest.get("evidence_id", ""))
        raw = latest.get("result")
        result: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if latest.get("tool") == "find_format_issues" and len(results) < _MAX_LOOKS:
            issues = result.get("issues")
            for issue in issues if isinstance(issues, list) else []:
                kind = _FIXES.get(str(issue.get("kind"))) if isinstance(issue, dict) else None
                if kind is None:
                    continue
                given = issue.get("params")
                params = {k: v for k, v in given.items() if k != "merge"} if isinstance(given, dict) else {}
                args: dict[str, Any] = {
                    "kind": kind,
                    "column": issue.get("column"),
                    "params": params,
                    "evidence_ids": [evidence],
                    "reason": "The look at this column found values written in a way the run cannot read.",
                }
                return json.dumps({"action": "propose_step", "file": "F1", "args": args})
            return self._said(
                "I looked at the format of every column and found nothing I can fix.", [evidence]
            )
        return self._said("I could not suggest a change from what I looked at.", [])

    def _helper(self, user: str) -> str:
        _state, results, message = _helper_sections(user)
        if not results:
            if "faster" in message.lower():
                setting = {
                    "path": "model_search.strategy",
                    "value": "fast",
                    "reason": "You asked for a faster search.",
                }
                return json.dumps({"action": "propose_setting", "args": setting})
            return json.dumps({"action": "find_format_issues", "args": {}})
        latest = results[-1]
        evidence = str(latest.get("evidence_id", ""))
        raw = latest.get("result")
        result: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if latest.get("tool") == "find_format_issues" and len(results) < _MAX_LOOKS:
            issues = result.get("issues")
            for issue in issues if isinstance(issues, list) else []:
                kind = _FIXES.get(str(issue.get("kind"))) if isinstance(issue, dict) else None
                if kind is None:
                    continue
                given = issue.get("params")
                params = {k: v for k, v in given.items() if k != "merge"} if isinstance(given, dict) else {}
                args: dict[str, Any] = {
                    "kind": kind,
                    "column": issue.get("column"),
                    "params": params,
                    "evidence_ids": [evidence],
                    "reason": "The look at this column found values written in a way the run cannot read.",
                }
                return json.dumps({"action": "propose_step", "args": args})
            return self._said(
                "I looked at the format of every column and found nothing I can fix.", [evidence]
            )
        if latest.get("tool") in ("propose_step", "propose_setting") and "error" not in result:
            return self._said(
                "I have suggested one change; check it on the screen before you tick it.", [evidence]
            )
        return self._said("I could not suggest a change from what I looked at.", [])

    @staticmethod
    def _said(text: str, evidence: list[str]) -> str:
        return json.dumps({"action": "reply", "text": text, "evidence_ids": [e for e in evidence if e]})
