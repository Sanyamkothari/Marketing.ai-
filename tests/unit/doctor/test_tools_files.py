"""The M237 tool specs and `register(registry)` (Plan O M237, DEC-1807 (g)), and the builder adapter's refusals.

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
from engine.doctor.builder_api import ApiBuilderPort
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


@dataclass
class Answer:
    status_code: int
    body: Any

    def json(self) -> Any:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@dataclass
class Http:
    answers: list[Answer]
    sent: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def post(self, url: str, **kwargs: Any) -> Answer:
        self.sent.append(("POST", url, kwargs))
        return self.answers.pop(0)

    def put(self, url: str, **kwargs: Any) -> Answer:
        self.sent.append(("PUT", url, kwargs))
        return self.answers.pop(0)


def test_the_adapter_calls_the_builders_routes() -> None:
    http = Http(
        [
            Answer(201, {"source_id": "s1"}),
            Answer(200, {"mapping_id": "m1", "columns": []}),
            Answer(200, {"mapping_id": "m1", "checks": []}),
            Answer(201, {"spec_id": "sp1", "checks": []}),
            Answer(202, {"dataset_id": "d1"}),
        ]
    )
    port = ApiBuilderPort(http, "c1")
    assert port.add_source(file_name="a.csv", data=b"x\n1\n", role="entity") == "s1"
    assert port.suggest_mapping(source_id="s1", use_case="rca")["mapping_id"] == "m1"
    assert (
        port.save_mapping(mapping_id="m1", source_id="s1", use_case="rca", role="entity", columns=[]) == "m1"
    )
    assert port.save_spec({"use_case": "rca"}) == "sp1"
    assert port.build(spec_id="sp1", mode="train") == "d1"
    assert [(method, url) for method, url, _ in http.sent] == [
        ("POST", "/clients/c1/sources"),
        ("POST", "/clients/c1/mappings/suggest"),
        ("PUT", "/clients/c1/mappings/m1"),
        ("POST", "/clients/c1/onboarding-specs"),
        ("POST", "/datasets"),
    ]
    assert http.sent[0][2]["data"] == {"role": "entity"}


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (Answer(409, {"detail": {"code": "TOO_MANY_SOURCES", "message": "too many"}}), "TOO_MANY_SOURCES"),
        (
            Answer(409, {"checks": [{"code": "JOIN_KEY_COVERAGE_LOW", "severity": "error", "message": "m"}]}),
            "JOIN_KEY_COVERAGE_LOW",
        ),
        (Answer(500, ValueError("not json")), "BUILD_STEP_FAILED"),
        (Answer(201, ["not", "an", "object"]), "BUILD_STEP_FAILED"),
    ],
)
def test_a_refused_call_raises_the_routes_own_code(answer: Answer, code: str) -> None:
    port = ApiBuilderPort(Http([answer]), "c1")
    with pytest.raises(BuildDriveError) as raised:
        port.add_source(file_name="a.csv", data=b"", role="entity")
    assert raised.value.code == code


def test_a_missing_column_is_the_coded_join_error_not_a_key_error() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "normalise_key")
    given = spec.input_model.model_validate({**INPUTS["normalise_key"], "column": "nope"})
    with pytest.raises(JoinError) as raised:
        spec.run(given, _context())
    assert (raised.value.code, raised.value.column) == ("JOIN_COLUMN_MISSING", "nope")


def test_the_stack_tool_does_not_claim_to_replace_the_builders_concat() -> None:
    spec = next(tool for tool in TOOLS if tool.name == "stack_files")
    assert "replaces" not in spec.wraps and "build.py's concat is unchanged" in spec.wraps
