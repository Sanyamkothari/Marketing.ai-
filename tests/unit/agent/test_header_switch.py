"""M142's setup help for new sources follows Plan K's `agentic.enabled` (DEC-1409 (f), set at integration).

The builder left the rules-only header reading, the mapping questions, the lapse-label drafts and the
`header_candidates` key on for every file, because M130's switch was not on its branch (a recorded deviation from
design §11). At integration they were put behind the switch. Off (the shipped default), Guided setup's advice,
its tool results and what the chat sends to an AI service are what they were before M142; on, the help is there.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pandas as pd
import pytest

from engine.agent.advisor import Advice, advise
from engine.agent.loop import _tools
from engine.agent.tools import AgentContext, AgentToolError, call_tool, header_help_on
from tests.fixtures.agent_bench.cases import CASES, EXPECTED, HEADER_HELP_CASES, run_case
from tests.fixtures.agent_bench.make_hinglish import CASES as HINGLISH_CASES
from tests.fixtures.agent_bench.make_hinglish import hinglish_frame
from tests.unit.agent.helpers import context_for, header_help_context

MAPPING: Final[str] = "hinglish_telco_prepaid_abbr"
LAPSE: Final[str] = "hinglish_lapse_no_outcome_telco"
ORIGINAL_CASES = sorted(name for name in CASES if name not in HEADER_HELP_CASES)


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


def test_the_shipped_switch_is_off_and_the_help_follows_it() -> None:
    assert header_help_on(context_for(_frame(MAPPING))) is False
    assert header_help_on(header_help_context(_frame(MAPPING))) is True


def test_with_the_switch_off_find_roles_has_no_header_candidates_and_reads_no_header() -> None:
    off = _roles(context_for(_frame(MAPPING)))
    on = _roles(header_help_context(_frame(MAPPING)))
    assert "header_candidates" not in off
    assert on["header_candidates"], "with the switch on the headers are read"
    # Off, the contact columns are the use case's hints only, as before M142: the header-read ones are not added.
    for key in ("consent", "opt_out", "recently_contacted"):
        assert set(off[key]) <= set(on[key])
    assert "dnd_flag" in on["opt_out"] and "dnd_flag" not in off["opt_out"]


def test_with_the_switch_off_no_mapping_or_lapse_question_appears() -> None:
    for case in (MAPPING, LAPSE):
        assert _mapping_or_lapse(advise(context_for(_frame(case)))) == [], case
        assert _mapping_or_lapse(advise(header_help_context(_frame(case)))), case
    stopped = advise(context_for(_frame(LAPSE)))
    assert stopped.stop_reason is not None and "draft outcome" not in stopped.stop_reason
    assert not [p for p in advise(context_for(_frame(MAPPING))).proposals if p.from_headers]


def test_with_the_switch_off_the_lapse_tool_is_unknown_and_not_offered_to_the_chat() -> None:
    off, on = context_for(_frame(LAPSE)), header_help_context(_frame(LAPSE))
    with pytest.raises(AgentToolError) as refused:
        call_tool(off, "draft_outcome_labels", {}, evidence_id="e1")
    assert refused.value.code == "AGENT_TOOL_UNKNOWN"
    assert call_tool(on, "draft_outcome_labels", {}, evidence_id="e1").result["drafts"]
    assert "draft_outcome_labels" not in {tool["name"] for tool in _tools(off)}
    assert "draft_outcome_labels" in {tool["name"] for tool in _tools(on)}


def test_m142s_own_cases_change_with_the_switch() -> None:
    """The golden file pins M142's cases with the switch on; off, the help is gone from them."""
    assert run_case(MAPPING, header_help=False) != run_case(MAPPING, header_help=True)


@pytest.mark.parametrize("name", ORIGINAL_CASES)
def test_the_original_cases_read_the_same_whichever_way_the_switch_is(name: str) -> None:
    """The benchmark's cases from before M142 are byte-identical with the help on or off, and match the golden file."""
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))[name]
    assert run_case(name, header_help=False) == expected
    assert run_case(name, header_help=True) == expected
