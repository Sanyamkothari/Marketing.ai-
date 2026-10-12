"""M238's tool specs (Plan O, DEC-1808 (h)): the `register(registry)` contract, refusals on the ID and the outcome,
deterministic evidence, and the egress canary for every tool in both data-access modes."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import BaseModel, ValidationError

from engine.access.roles import Role
from engine.agent.config import DataAccess
from engine.agent.egress import prepare
from engine.agentic.registry import evidence_id_for
from engine.doctor.split import SplitRule
from engine.doctor.tools_rows import (
    ROW_TOOL_CODES,
    ROW_TOOLS,
    DataToolSpec,
    DetectUnitsInput,
    ImpossibleValuesInput,
    KeepLatestInput,
    RowToolError,
    RowToolFrame,
    SplitFileInput,
    SynonymClustersInput,
    register,
)
from tests.unit.agent.egress_canary import HARD_SECRETS, canary_frame, leaks, stray_keys

AS_OF = date(2026, 10, 12)
NAMES = ("synonym_clusters", "impossible_values", "detect_units", "keep_latest_per_id", "split_file")


class Collecting:
    def __init__(self) -> None:
        self.specs: list[DataToolSpec[Any, Any]] = []

    def register(self, spec: DataToolSpec[Any, Any]) -> None:
        self.specs.append(spec)


def test_register_hands_every_tool_to_the_registry() -> None:
    registry = Collecting()
    assert register(registry) == NAMES
    assert [spec.name for spec in registry.specs] == list(NAMES)


@pytest.mark.parametrize("spec", ROW_TOOLS, ids=lambda spec: spec.name)
def test_each_spec_is_shaped_as_the_design_says(spec: DataToolSpec[Any, Any]) -> None:
    assert spec.exposure == "session-only"  # decision O7: never listed on MCP
    assert spec.input_model.model_config.get("extra") == "forbid"
    assert spec.output_model.model_config.get("extra") == "forbid"
    assert spec.deterministic and spec.milestone == "M238"
    assert spec.role is (Role.VIEWER if spec.kind == "read" else Role.ANALYST)
    assert spec.kind in {"read", "propose"}  # APPLY is never a tool
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({"column": "x", "rule": None, "time_column": "t", "surprise": 1})


def test_the_new_codes_are_named() -> None:
    assert {
        "AI_STEP_CHANGES_MANY", "AI_STEP_CHANGES_TOO_MANY", "IMPOSSIBLE_VALUE", "UNIT_MISMATCH", "RESHAPE_GUARD_MISSING",
        "SPLIT_RULE_INVALID", "SPLIT_PART_EMPTY", "SPLIT_VALUES_UNREADABLE", "SPLIT_LATER_DATES_IN_TRAINING",
        "SYNONYM_MERGE_INVALID", "DOCTOR_COLUMN_PROTECTED", "DOCTOR_COLUMN_UNKNOWN",
    } == ROW_TOOL_CODES  # fmt: skip


def messy() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": ["C1", "C1", "C2", "C3", "C3", "C4"],
            "seen": ["2024-01-01", "2024-02-01", "2024-01-15", "2024-01-20", "2024-03-01", "2024-02-02"],
            "plan": ["Prepaid", "Pre-paid", "PP", "Postpaid", "Prepaid", "Prepaid"],
            "age": [25, 25, 130, 40, -1, 33],
            "data_used": ["2 GB", "2 GB", "500 MB", "1 GB", "3 GB", "1 GB"],
            "churned": [0, 0, 1, 0, 1, 0],
        }
    )


def ctx(frame: pd.DataFrame | None = None, **changes: Any) -> RowToolFrame:
    base: dict[str, Any] = {"primary_key": "customer_id", "target": "churned", "as_of": AS_OF}
    return RowToolFrame(frame=messy() if frame is None else frame, **{**base, **changes})


CALLS: dict[str, BaseModel] = {
    "synonym_clusters": SynonymClustersInput(column="plan"),
    "impossible_values": ImpossibleValuesInput(),
    "detect_units": DetectUnitsInput(column="data_used"),
    "keep_latest_per_id": KeepLatestInput(time_column="seen"),
    "split_file": SplitFileInput(rule=SplitRule(column="seen", by="date", cut="2024-01-31")),
}
TOOLS = {spec.name: spec for spec in ROW_TOOLS}


def evidence(name: str, frame: pd.DataFrame | None = None) -> str:
    given = CALLS[name]
    out = TOOLS[name].run(given, ctx(frame))
    return evidence_id_for(name, given.model_dump(mode="json"), out.model_dump(mode="json"))


@pytest.mark.parametrize("name", NAMES)
def test_the_same_input_gives_the_same_evidence_id(name: str) -> None:
    assert evidence(name) == evidence(name)


def test_the_evidence_ids_are_the_same_in_another_process() -> None:
    here = {name: evidence(name) for name in NAMES}
    script = (
        "import json\n"
        "from tests.unit.doctor.test_tools_rows import NAMES, evidence\n"
        "print(json.dumps({name: evidence(name) for name in NAMES}))\n"
    )
    root = Path(__file__).resolve().parents[3]
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True, timeout=300
    )
    assert json.loads(done.stdout.strip().splitlines()[-1]) == here


@pytest.mark.parametrize(
    "call",
    [
        SynonymClustersInput(column="customer_id"),
        SynonymClustersInput(column="churned"),
        ImpossibleValuesInput(columns=("churned",)),
        DetectUnitsInput(column="customer_id"),
        KeepLatestInput(time_column="churned"),
        KeepLatestInput(time_column="seen", snapshot_column="churned"),
        SplitFileInput(rule=SplitRule(column="customer_id", by="values", scoring_values=("C1",))),
    ],
    ids=lambda call: type(call).__name__,
)
def test_every_tool_refuses_the_id_and_the_outcome(call: BaseModel) -> None:
    tool = next(spec for spec in ROW_TOOLS if spec.input_model is type(call))
    with pytest.raises(RowToolError) as refused:
        tool.run(call, ctx())
    assert refused.value.code == "DOCTOR_COLUMN_PROTECTED"


def test_an_unknown_column_is_refused() -> None:
    with pytest.raises(RowToolError) as refused:
        TOOLS["detect_units"].run(DetectUnitsInput(column="nope"), ctx())
    assert refused.value.code == "DOCTOR_COLUMN_UNKNOWN"


def test_keep_latest_asks_one_more_question_when_duplicates_conflict() -> None:
    data = messy()
    data.loc[1, "seen"] = "2024-01-01"  # C1's two rows now share the latest date and differ in plan
    out = TOOLS["keep_latest_per_id"].run(KeepLatestInput(time_column="seen"), ctx(data))
    assert out.counts.ids == 1 and out.conflict_question is not None and out.conflict_question.blocking
    assert [o.option_id for o in out.conflict_question.options] == [
        "last-in-file",
        "first-in-file",
        "dont-know",
    ]
    assert out.step.params["ties"] == "refuse" and out.step.ticked is False
    assert out.question.blocking  # decision O3: offered through a blocking question


def test_the_ai_merge_runs_through_the_rules_and_its_refusal_is_reported() -> None:
    tool = TOOLS["synonym_clusters"]
    ok = tool.run(
        SynonymClustersInput(column="plan", ai_groups=(("Prepaid", "PP"),), ai_keep=("Prepaid",)), ctx()
    )
    assert ok.ai_step is not None and ok.ai_step.source.value == "ai" and ok.ai_step.ticked is False
    bad = tool.run(
        SynonymClustersInput(column="plan", ai_groups=(("Prepaid", "Gold"),), ai_keep=("Prepaid",)), ctx()
    )
    assert (
        bad.ai_step is None
        and bad.ai_refused is not None
        and bad.ai_refused.startswith("SYNONYM_MERGE_INVALID")
    )


# ---------------------------------------------------------------------------
# The egress canary: every tool on a file of planted personal data, both modes
# ---------------------------------------------------------------------------
def canary() -> pd.DataFrame:
    frame = canary_frame(rows=300)
    frame["data_used"] = ["2 GB", "500 MB", "1 GB"] * 100
    frame["age"] = [25, 130, -2] * 100
    return frame


CANARY_CALLS: list[tuple[str, BaseModel]] = [
    *[
        ("synonym_clusters", SynonymClustersInput(column=c))
        for c in ("notes", "feedback", "region_contact", "label")
    ],
    *[
        ("detect_units", DetectUnitsInput(column=c))
        for c in ("notes", "card_hint", "data_used", "monthly_spend")
    ],
    ("impossible_values", ImpossibleValuesInput()),
    ("keep_latest_per_id", KeepLatestInput(time_column="signup_date")),
    ("split_file", SplitFileInput(rule=SplitRule(column="signup_date", by="date", cut="2024-06-30"))),
    ("split_file", SplitFileInput(rule=SplitRule(column="label", by="values", scoring_values=("standard",)))),
]


@pytest.mark.parametrize("mode", [DataAccess.MASKED_DATA, DataAccess.SUMMARIES_ONLY])
def test_the_canary_finds_no_planted_secret_in_any_tool_result(mode: DataAccess) -> None:
    frame = canary()
    context = ctx(frame, target="converted_30d")
    personal = ["contact_email", "phone_number", "pan_number", "name"]
    for name, given in CANARY_CALLS:
        result = TOOLS[name].run(given, context).model_dump(mode="json")
        assert stray_keys(result, frame.columns) == [], name  # no dict is keyed by a cell
        sent = json.dumps(prepare(result, ctx_columns=frame.columns, personal_columns=personal, mode=mode))
        assert leaks(sent, HARD_SECRETS) == [], (name, mode)


def test_examples_are_masked_in_the_result_itself() -> None:
    frame = canary()
    out = TOOLS["detect_units"].run(DetectUnitsInput(column="card_hint"), ctx(frame, target="converted_30d"))
    assert out.examples and leaks(json.dumps(out.model_dump(mode="json")), HARD_SECRETS) == []
