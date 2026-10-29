"""The M237 tool specs and `register(registry)` (Plan O M237, DEC-1807 (g)).

Every tool is PROPOSE, rules, `session-only`, for an Analyst, with an input model that refuses unknown fields;
its output is a pending, `check`, never-ticked proposal. The privacy canary plants personal data as IDs and in
cells and checks no tool output carries any of it (join tools return counts, never keys).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.access.roles import Role
from engine.config import LabelDefinition, LabelType
from engine.doctor.build_driver import BuildDriveError
from engine.doctor.contracts import Diagnosis
from engine.doctor.join_files import JoinError
from engine.doctor.stack_files import StackProposal
from engine.doctor.tools_files import TOOLS, NormaliseKeyProposal, register
from tests.unit.doctor.m237_support import diagnosis, frames


@dataclass
class Context:
    loaded: dict[str, pd.DataFrame]
    found: Diagnosis | None

    def frame(self, upload_id: str) -> pd.DataFrame:
        return self.loaded[upload_id]

    def file_name(self, upload_id: str) -> str:
        return f"{upload_id}.csv"

    def diagnosis(self) -> Diagnosis | None:
        return self.found


@dataclass
class Registry:
    tools: list[Any] = field(default_factory=list)

    def register(self, tool: Any) -> None:
        self.tools.append(tool)


INPUTS: dict[str, dict[str, Any]] = {
    "join_files": {
        "base_upload_id": "u_list",
        "base_key_column": "customer_id",
        "upload_ids": ["u_jan"],
    },
    "stack_files": {
        "upload_ids": ["u_jan", "u_feb", "u_mar"],
        "key_column": "customer_id",
        "date_column": "invoice_date",
    },
    "normalise_key": {
        "upload_id": "u_jan",
        "column": "customer_id",
        "other_upload_id": "u_list",
        "other_column": "customer_id",
    },
    "build_dataset": {
        "use_case": "rca",
        "upload_ids": ["u_list", "u_jan", "u_feb", "u_mar", "u_phones"],
        "label_spec": LabelDefinition(
            name="churn_next_60d", type=LabelType.EVENT_ABSENCE, role="bills", horizon_days=60
        ).model_dump(mode="json", by_alias=True),
    },
}


def _context() -> Context:
    loaded = frames()
    return Context(loaded=loaded, found=diagnosis(loaded))


def test_register_hands_over_the_four_tools_in_order() -> None:
    registry = Registry()
    assert register(registry) == ("join_files", "stack_files", "normalise_key", "build_dataset")
    assert [tool.name for tool in registry.tools] == [
        "join_files",
        "stack_files",
        "normalise_key",
        "build_dataset",
    ]


@pytest.mark.parametrize("spec", TOOLS, ids=lambda spec: spec.name)
def test_every_tool_is_a_rules_proposal_for_an_analyst_and_never_on_mcp(spec: Any) -> None:
    assert (spec.kind, spec.mode, spec.exposure, spec.role) == (
        "propose",
        "rules",
        "session-only",
        Role.ANALYST,
    )
    assert spec.policy_kind == f"data.{spec.name}"
    assert spec.input_model.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({**INPUTS[spec.name], "apply": True})


@pytest.mark.parametrize("spec", TOOLS, ids=lambda spec: spec.name)
def test_every_tool_answers_a_pending_never_ticked_proposal(spec: Any) -> None:
    given = spec.input_model.model_validate(INPUTS[spec.name])
    out = spec.run(given, _context())
    assert isinstance(out, spec.output_model)
    assert (out.state, out.confidence, out.never_ticked) == ("pending", "check", True)


def test_normalise_key_proposes_the_steps_for_both_sides() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "normalise_key")
    out = spec.run(spec.input_model.model_validate(INPUTS["normalise_key"]), _context())
    assert isinstance(out, NormaliseKeyProposal)
    assert out.meets and (out.coverage, out.fixed_coverage) == (0.0, 1.0)
    assert {(step["upload_id"], tuple(sorted(step["params"]))) for step in out.steps} == {
        ("u_jan", ("zeros",)),
        ("u_list", ("prefix", "zeros")),
    }


def test_stack_files_without_a_diagnosis_reads_the_columns_by_name() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "stack_files")
    context = _context()
    context.found = None
    out = spec.run(spec.input_model.model_validate(INPUTS["stack_files"]), context)
    assert isinstance(out, StackProposal)
    assert "bill_amount" in out.spec.columns, "without the diagnosis no rename is assumed"


def test_build_dataset_needs_the_diagnosis() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "build_dataset")
    context = _context()
    context.found = None
    with pytest.raises(BuildDriveError) as raised:
        spec.run(spec.input_model.model_validate(INPUTS["build_dataset"]), context)
    assert raised.value.code == "BUILD_PLAN_INCOMPLETE"


def test_the_privacy_canary_finds_no_id_or_cell_in_any_tool_output() -> None:
    """Personal data planted as IDs (e-mail addresses, phone numbers) and in cells never reaches an output."""
    loaded = frames()
    emails = [f"jane.roe{n}@example.com" for n in range(len(loaded["u_list"]))]
    loaded["u_list"] = loaded["u_list"].assign(customer_id=emails, note="Jane Roe, 12 Baker Street")
    loaded["u_jan"] = loaded["u_jan"].assign(
        customer_id=[emails[i % len(emails)] for i in range(len(loaded["u_jan"]))]
    )
    context = Context(loaded=loaded, found=diagnosis(loaded))
    planted = [*emails, "Jane Roe", "Baker Street", *loaded["u_phones"]["cust_no"]]
    for spec in TOOLS:
        out = spec.run(spec.input_model.model_validate(INPUTS[spec.name]), context)
        text = json.dumps(out.model_dump(mode="json"))
        leaked = [value for value in planted if value in text]
        assert leaked == [], f"{spec.name} returned {leaked[:3]}"


def test_a_missing_column_is_the_coded_join_error_not_a_key_error() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "normalise_key")
    given = spec.input_model.model_validate({**INPUTS["normalise_key"], "column": "nope"})
    with pytest.raises(JoinError) as raised:
        spec.run(given, _context())
    assert (raised.value.code, raised.value.column) == ("JOIN_COLUMN_MISSING", "nope")


def test_the_stack_tool_does_not_claim_to_replace_the_builders_concat() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "stack_files")
    assert "replaces" not in spec.wraps and "build.py's concat is unchanged" in spec.wraps
