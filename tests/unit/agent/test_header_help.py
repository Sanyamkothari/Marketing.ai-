"""M142's setup help for new sources is always on (DEC-1409, with the switch removed).

Guided setup reads abbreviated and romanised-Hindi headers with the rules, asks the mapping and lapse questions,
drafts lapse labels and lists `header_candidates`, whether or not an AI service is connected. Its suggestions stay
"please check" and are never ticked. Only the AI second opinion needs an AI service (`POST …/assist`).
"""

from __future__ import annotations

import json
from typing import Any, Final

import pandas as pd
import pytest

from engine.agent.advisor import Advice, advise
from engine.agent.loop import _tools
from engine.agent.tools import AgentContext, call_tool
from tests.fixtures.agent_bench.cases import CASES, EXPECTED, HEADER_HELP_CASES, run_case
from tests.fixtures.agent_bench.make_hinglish import CASES as HINGLISH_CASES
from tests.fixtures.agent_bench.make_hinglish import hinglish_frame
from tests.unit.agent.helpers import context_for

MAPPING: Final[str] = "hinglish_telco_prepaid_abbr"
LAPSE: Final[str] = "hinglish_lapse_no_outcome_telco"
ALL_CASES = sorted(CASES)


def _frame(case: str) -> pd.DataFrame:
    name = case.removeprefix("hinglish_")
    return hinglish_frame(next(spec for spec in HINGLISH_CASES if spec.name == name))


def _roles(ctx: AgentContext) -> dict[str, Any]:
    return dict(call_tool(ctx, "find_roles", {}, evidence_id="e1").result)


def _mapping_or_lapse(advice: Advice) -> list[str]:
    """The questions only M142 asks: a column that may be the suppression rules' one, or a lapse draft."""
    return [
        q.text
        for q in advice.questions
        if " may say " in q.text
        or q.text.startswith("The file has no column that says whether a customer lapsed")
    ]


def test_find_roles_reads_the_headers() -> None:
    found = _roles(context_for(_frame(MAPPING)))
    assert found["header_candidates"], "the headers are read"
    assert "dnd_flag" in found["opt_out"]


def test_the_mapping_and_lapse_questions_appear() -> None:
    for case in (MAPPING, LAPSE):
        assert _mapping_or_lapse(advise(context_for(_frame(case)))), case
    advice = advise(context_for(_frame(MAPPING)))
    read = [p for p in advice.proposals if p.from_headers]
    assert read, "the headers are read"
    assert all(p.confidence.value == "check" and p.state.value == "pending" for p in read)


def test_the_lapse_tool_is_known_and_offered_to_the_chat() -> None:
    ctx = context_for(_frame(LAPSE))
    assert call_tool(ctx, "draft_outcome_labels", {}, evidence_id="e1").result["drafts"]
    assert "draft_outcome_labels" in {tool["name"] for tool in _tools()}


def test_there_are_22_original_and_27_hinglish_cases_beside_the_other_use_cases() -> None:
    assert len([name for name in CASES if name not in HEADER_HELP_CASES]) == 22


@pytest.mark.parametrize("name", ALL_CASES)
def test_every_case_matches_its_golden_digest(name: str) -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))[name]
    assert run_case(name) == expected
