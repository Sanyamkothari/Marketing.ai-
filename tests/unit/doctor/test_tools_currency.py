"""Plan O M242 (DEC-1812): the currency tools' registry entries, standalone until M232's registry is integrated."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.access.roles import Role
from engine.doctor.locale import LocaleProfile
from engine.doctor.tools_currency import (
    CURRENCY_TOOLS,
    CheckCurrencyInput,
    ConvertCurrencyInput,
    CurrencyToolSpec,
    ReportInCurrencyInput,
    register,
)


@dataclass
class Ctx:
    frame: pd.DataFrame
    primary_key: str | None = "id"
    target: str | None = None
    locale_profile: LocaleProfile | None = None
    workspace_currency: str = "INR"


def _tool(name: str) -> CurrencyToolSpec[Any, Any]:
    return next(spec for spec in CURRENCY_TOOLS if spec.name == name)


FRAME = pd.DataFrame({"id": [1, 2, 3], "amount": ["$12", "$1,200", "$5"], "mix": ["₹1", "$2", "₹3"]})


def test_three_tools_in_catalogue_order_rules_only_deterministic() -> None:
    assert [s.name for s in CURRENCY_TOOLS] == ["check_currency", "report_in_currency", "convert_currency"]
    assert all(s.mode == "rules" and s.deterministic and s.exposure == "session-only" for s in CURRENCY_TOOLS)
    assert [s.kind for s in CURRENCY_TOOLS] == ["read", "propose", "propose"]
    assert _tool("check_currency").role is Role.VIEWER
    assert _tool("convert_currency").role is Role.ANALYST


def test_the_rate_tool_is_approve_class_and_never_listed_to_a_model() -> None:
    convert = _tool("convert_currency")
    assert convert.approve_class and convert.person_only
    assert convert.proposes == ("convert_currency", "parse_number")
    assert not any(s.person_only for s in CURRENCY_TOOLS if s.name != "convert_currency")


def test_check_currency_returns_counts_and_the_questions() -> None:
    out = _tool("check_currency").run(CheckCurrencyInput(), Ctx(FRAME))
    assert out.stopped
    assert [(f["code"], f["column"]) for f in out.findings] == [
        ("CURRENCY_FOREIGN", "amount"),
        ("CURRENCY_MIXED", "mix"),
    ]
    assert "$1,200" not in out.model_dump_json()


def test_report_in_proposes_the_unit_and_refuses_a_mixed_column() -> None:
    out = _tool("report_in_currency").run(ReportInCurrencyInput(column="amount", currency="USD"), Ctx(FRAME))
    assert [(s["kind"], s["params"]) for s in out.steps] == [("parse_number", {"unit": "USD"})]
    refused = _tool("report_in_currency").run(ReportInCurrencyInput(column="mix", currency="INR"), Ctx(FRAME))
    assert refused.steps == [] and refused.refused is not None and refused.refused["code"] == "CURRENCY_MIXED"


def test_convert_proposes_pending_steps_and_the_sentence_and_refuses_a_bad_rate() -> None:
    args = ConvertCurrencyInput(
        column="amount",
        from_currency="USD",
        to_currency="INR",
        rate=83.1,
        rate_date=date(2026, 10, 10),
        source="RBI reference rate",
    )
    out = _tool("convert_currency").run(args, Ctx(FRAME))
    assert [s["kind"] for s in out.steps] == ["convert_currency", "parse_number"]
    assert out.sentence is not None and "83.1" in out.sentence and out.refused is None
    future = _tool("convert_currency").run(
        args.model_copy(update={"rate_date": date(2999, 1, 1)}), Ctx(FRAME)
    )
    assert future.refused is not None and future.refused["code"] == "CURRENCY_RATE_INVALID"
    with pytest.raises(ValidationError):
        ConvertCurrencyInput.model_validate({**args.model_dump(), "rate": 0})
    with pytest.raises(ValidationError):
        ConvertCurrencyInput.model_validate({**args.model_dump(), "rate_url": "https://example.com"})


def test_register_adds_each_spec_once() -> None:
    seen: list[str] = []

    class Registry:
        def register_spec(self, spec: CurrencyToolSpec[Any, Any]) -> None:
            seen.append(spec.name)

    register(Registry())
    assert seen == ["check_currency", "report_in_currency", "convert_currency"]
