"""Plan O M241 (O4, DEC-1811 (k)): Hinglish header reading is on by default once Data Doctor is on.

`data_doctor.unified` (M230) turns M142's rules-only setup help on - the header reading, the mapping questions, the
lapse drafts and `draft_outcome_labels` - whatever Plan K's `agentic.enabled` says. Its suggestions stay "please check"
and are never ticked. The AI second opinion still follows `agentic.enabled`. With the switch off (shipped), nothing
moves: `test_header_switch.py` and `test_advisor.py` pin that, and so does the first test here.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Final

import pandas as pd
import pytest

from engine.agent.advisor import advise
from engine.agent.tools import AgentContext, call_tool, header_help_on, tool_available
from engine.doctor.config import DataDoctorConfig
from tests.fixtures.agent_bench.cases import (
    CASE_USE_CASES,
    CASES,
    EXPECTED,
    HEADER_HELP_CASES,
    HINGLISH_PREFIX,
    USE_CASE,
    digest,
    header_reading,
)
from tests.unit.agent.helpers import context_for

GOLDEN: Final[dict[str, Any]] = json.loads(EXPECTED.read_text(encoding="utf-8"))
CHANGED_WITH_THE_SWITCH: Final[frozenset[str]] = frozenset(
    {
        "hinglish_ambiguous_ids",
        "hinglish_auto_loans",
        "hinglish_bank_cards",
        "hinglish_dth_hinglish",
        "hinglish_dth_recharge",
        "hinglish_ecom_titlecase",
        "hinglish_edtech_whatsapp",
        "hinglish_fintech_lending",
        "hinglish_food_delivery_camel",
        "hinglish_gold_loan",
        "hinglish_housing_finance",
        "hinglish_insurance_renewal",
        "hinglish_kirana_orders",
        "hinglish_lapse_no_outcome_retail",
        "hinglish_lapse_no_outcome_telco",
        "hinglish_mobile_postpaid_short",
        "hinglish_no_flags_hinglish",
        "hinglish_pharmacy_chain",
        "hinglish_postpaid_bills",
        "hinglish_retail_runon",
        "hinglish_savings_hinglish",
        "hinglish_telco_hinglish",
        "hinglish_telco_prepaid_abbr",
        "hinglish_travel_app",
        "hinglish_upi_wallet",
        "hinglish_wide_telecom",
        "hinglish_yes_no_values",
    }
)
"""The golden cases whose digest with the switch on differs from the shipped one (switch off, Plan K off): the 27
Hinglish exports, which DEC-1811 (k) lists. Each equals its recorded `expected.json` digest, recorded with the help on;
the 22 original cases and the 3 other-use-case cases do not move."""


def unified(ctx: AgentContext) -> AgentContext:
    """`ctx` with `data_doctor.unified` on in its use case; Plan K's switch stays as shipped (off)."""
    config = ctx.config.model_copy(update={"data_doctor": DataDoctorConfig(unified=True)})
    return dataclasses.replace(ctx, config=config)


def _digest(name: str, *, switch: bool) -> dict[str, Any]:
    ctx = context_for(CASES[name](), CASE_USE_CASES.get(name, USE_CASE))
    advice = advise(unified(ctx) if switch else ctx)
    result = digest(advice)
    if name.startswith(HINGLISH_PREFIX):
        result["headers"] = header_reading(advice)
    return result


def test_the_shipped_switch_is_off_and_the_help_still_follows_plan_k() -> None:
    ctx = context_for(pd.DataFrame({"grahak_id": ["a", "b"], "kul_kharch": [1, 2]}))
    assert ctx.config.data_doctor.unified is False
    assert header_help_on(ctx) is False
    assert header_help_on(unified(ctx)) is True


def test_with_the_switch_on_headers_are_read_without_plan_k() -> None:
    frame = pd.DataFrame({"grahak_id": [f"g{i}" for i in range(40)], "kul_kharch": list(range(40))})
    off = context_for(frame)
    on = unified(off)
    assert "header_candidates" not in call_tool(off, "find_roles", {}, evidence_id="e1").result
    assert call_tool(on, "find_roles", {}, evidence_id="e1").result["header_candidates"]
    assert tool_available(on, "draft_outcome_labels") and not tool_available(off, "draft_outcome_labels")


def test_header_suggestions_with_the_switch_on_are_never_ticked() -> None:
    advice = advise(unified(context_for(CASES["hinglish_telco_prepaid_abbr"](), USE_CASE)))
    read = [p for p in advice.proposals if p.from_headers]
    assert read, "the switch reads the headers"
    assert all(p.confidence.value == "check" and p.state.value == "pending" for p in read)


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_digests_with_the_switch_on_are_the_recorded_ones(name: str) -> None:
    """With the switch on every case gives its recorded digest: the 22 originals are unchanged either way, and each
    Hinglish or other-use-case case gives the digest M142 recorded with the help on."""
    on = _digest(name, switch=True)
    assert on == GOLDEN[name]
    if name in CHANGED_WITH_THE_SWITCH:
        assert name in HEADER_HELP_CASES
        assert _digest(name, switch=False) != on
    else:
        assert _digest(name, switch=False) == on
