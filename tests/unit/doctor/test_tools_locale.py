"""Plan O M241: the four locale tools' registry entries, their `register` contract and their egress canary.

M232's registry is on another branch; these tests pin what the integrator joins: each spec's kind, exposure, role,
models (`extra="forbid"`) and handler, and that nothing a tool returns carries a planted GSTIN, voter ID, phone or
PIN once it has been through the egress gate - or before it, since the tools return counts and masked examples only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.access.roles import Role
from engine.agent.config import DataAccess
from engine.agent.egress import RESULT_KEYS, assert_clean, prepare
from engine.doctor.locale import LocaleProfile, india_defaults
from engine.doctor.tools_locale import (
    LOCALE_TOOLS,
    LocaleToolSpec,
    ProposeLocaleStepsInput,
    register,
)
from tests.unit.agent.egress_canary import stray_keys

GSTIN = "27AAPFU0939F1ZV"
EPIC = "XYZ1234567"
PHONE = "+91 98765 43210"
PIN = "560034"
SECRETS = (GSTIN, "AAPFU0939F", EPIC, "1234567", "98765", "43210", "9876543210", PIN)


@dataclass(frozen=True)
class Ctx:
    frame: pd.DataFrame
    primary_key: str | None = "customer_id"
    target: str | None = "churned"
    locale_profile: LocaleProfile | None = None


@dataclass
class FakeRegistry:
    specs: list[LocaleToolSpec[Any, Any]] = field(default_factory=list)

    def register_spec(self, spec: LocaleToolSpec[Any, Any]) -> None:
        if any(s.name == spec.name for s in self.specs):
            raise ValueError(spec.name)
        self.specs.append(spec)


def canary_frame(rows: int = 60) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(rows)],
            "gst_no": [GSTIN] * rows,
            "voter_id": [EPIC] * rows,
            "mobile": [PHONE] * rows,
            "pincode": [PIN] * rows,
            # Personal data inside the columns the PROPOSE tools quote examples from.
            "income": [
                [f"₹2.5 L {GSTIN}", "1.2 Cr", f"12K {EPIC}", "Rs 3,50,000/-"][i % 4] for i in range(rows)
            ],
            "agreed": [["haan", "nahi", f"haan {PHONE}"][i % 3] for i in range(rows)],
            "note_flag": [["हाँ", "नहीं"][i % 2] for i in range(rows)],
            "churned": [i % 2 for i in range(rows)],
        }
    )


def test_the_catalogue_rows_60_to_63() -> None:
    rows = {s.name: (s.kind, s.mode, s.exposure, s.role, s.deterministic) for s in LOCALE_TOOLS}
    assert rows == {
        "detect_locale": ("read", "rules", "session-only", Role.VIEWER, True),
        "parse_indian_numbers": ("propose", "rules", "session-only", Role.ANALYST, True),
        "indian_booleans": ("propose", "rules", "session-only", Role.ANALYST, True),
        "detect_india_ids": ("read", "rules", "session-only", Role.VIEWER, True),
    }
    assert [s.name for s in LOCALE_TOOLS] == [
        "detect_locale",
        "parse_indian_numbers",
        "indian_booleans",
        "detect_india_ids",
    ]
    assert {s.name: s.proposes for s in LOCALE_TOOLS if s.kind == "propose"} == {
        "parse_indian_numbers": ("parse_number",),
        "indian_booleans": ("map_boolean",),
    }
    for spec in LOCALE_TOOLS:
        assert spec.input_model.model_config.get("extra") == "forbid", spec.name
        assert spec.output_model.model_config.get("extra") == "forbid", spec.name
        assert spec.name.replace("_", "").isalnum() and spec.name == spec.name.lower()
        assert spec.wraps.startswith("engine.doctor.")
        assert spec.kind in {"read", "propose", "ask"}  # APPLY is never a tool


def test_register_adds_each_tool_once_in_catalogue_order() -> None:
    registry = FakeRegistry()
    register(registry)
    assert [s.name for s in registry.specs] == [s.name for s in LOCALE_TOOLS]
    with pytest.raises(ValueError):
        register(registry)


def test_inputs_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProposeLocaleStepsInput.model_validate({"columns": ["a"], "code": "print(1)"})


def _call(spec: LocaleToolSpec[Any, Any], ctx: Ctx) -> dict[str, Any]:
    out = spec.run(spec.input_model(), ctx)
    assert isinstance(out, spec.output_model)
    dumped: dict[str, Any] = out.model_dump(mode="json")
    return dumped


def test_each_tool_answers_on_the_canary_file() -> None:
    ctx = Ctx(canary_frame())
    results = {spec.name: _call(spec, ctx) for spec in LOCALE_TOOLS}
    kinds = {f["column"]: f["kind"] for f in results["detect_india_ids"]["findings"]}
    assert kinds == {"gst_no": "gstin", "voter_id": "epic", "mobile": "phone_in", "pincode": "pin"}
    numbers = results["parse_indian_numbers"]["proposals"]
    assert [p["column"] for p in numbers] == ["income"] and numbers[0]["confidence"] == "check"
    booleans = {p["column"]: p for p in results["indian_booleans"]["proposals"]}
    assert booleans["note_flag"]["step"]["kind"] == "map_boolean"
    assert booleans["agreed"]["step"] is None  # a spelling that is not yes or no: nothing is proposed
    assert results["detect_locale"]["profile"]["number_words"]["value"] == "lakh_crore"
    assert "churned" not in {p["column"] for p in numbers} and "customer_id" not in kinds


def test_the_same_file_gives_the_same_answer() -> None:
    first = [_call(spec, Ctx(canary_frame())) for spec in LOCALE_TOOLS]
    second = [_call(spec, Ctx(canary_frame())) for spec in LOCALE_TOOLS]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.parametrize("mode", [DataAccess.MASKED_DATA, DataAccess.SUMMARIES_ONLY])
def test_canary_no_planted_id_reaches_a_prompt(mode: DataAccess) -> None:
    frame = canary_frame()
    ctx = Ctx(frame)
    for spec in LOCALE_TOOLS:
        raw = _call(spec, ctx)
        raw_text = json.dumps(raw, ensure_ascii=False)
        for secret in SECRETS:
            assert secret not in raw_text, (spec.name, secret)  # masked before it leaves the tool
        safe = prepare(
            raw, ctx_columns=frame.columns, personal_columns=("gst_no", "voter_id", "mobile"), mode=mode
        )
        text = json.dumps(safe, ensure_ascii=False)
        for secret in SECRETS:
            assert secret not in text, (spec.name, secret)
        assert assert_clean(text)[1] == 0, spec.name  # nothing for the last check to catch


def test_the_step_parameter_is_a_label_the_gate_knows() -> None:
    """Review item 5: `indian_scale_words` is a field name of ours, so the gate never reads it as a cell."""
    assert "indian_scale_words" in RESULT_KEYS


def test_result_keys_are_listed_for_the_integrator() -> None:
    """Keys the tools write that the egress gate did not know when M241 was built: the Plan O wave 1 integration
    added them to `RESULT_KEYS` when it wired the tools into M232's registry (DEC-1802 (t)), so none is stray.
    """
    ctx = Ctx(canary_frame())
    stray: set[str] = set()
    for spec in LOCALE_TOOLS:
        stray.update(stray_keys(_call(spec, ctx), ctx.frame.columns))
    confirmed = india_defaults().model_copy(
        update={"date_order": india_defaults().date_order.model_copy(update={"source": "confirmed"})}
    )
    us = Ctx(pd.DataFrame({"joined": ["12/25/2023", "01/31/2024"]}), locale_profile=confirmed)
    detected = _call(LOCALE_TOOLS[0], us)
    assert detected["conflicts"], "the conflict keys are exercised too"
    stray.update(stray_keys(detected, us.frame.columns))
    assert stray == set()
    assert PENDING_RESULT_KEYS <= RESULT_KEYS


PENDING_RESULT_KEYS = frozenset(
    {
        "boolean_words",
        "columns_read",
        "conflicts",
        "consent_regime",
        "country",
        "date_order",
        "decided_by",
        "decimal_mark",
        "digit_grouping",
        "evidence",
        "field",
        "findings",
        "found",
        "header_words",
        "invalid_checksum",
        "join_key",
        "keep_as_text",
        "matching",
        "money_units",
        "new_column",
        "number_words",
        "order",
        "personal",
        "personal_ids",
        "phone_format",
        "postcode",
        "profile",
        "profile_value",
        "proposal_id",
        "source",
        "step",
        "time_zone",
        "unknown_words",
        "unproven",
        "workspace_currency",
    }
)
