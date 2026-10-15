"""The Data Doctor agent's instructions load through the prompt loader, stay short, and keep what they promise."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.agent import egress
from engine.doctor.questions import CATALOGUE, catalogue_lines
from engine.generative.contracts import GenerativePurpose
from engine.generative.prompts import load_prompt, prompts_dir, render

NAME = "data_doctor_agent"
MAX_WORDS = 2500
"""The rendered instructions with the question list, before the tool list: a cap on what the model must hold in
mind every turn."""

OUT_OF_SCOPE_CODES = (
    "PDF",
    "IDS_ROUNDED",
    "SUMMARY_NOT_ROWS",
    "AMOUNT_PREDICTION",
    "NON_RANDOM_LIFT",
    "CURRENCY_CONVERSION",
    "TOO_FEW_POSITIVES",
)
CONFIG = Path(__file__).resolve().parents[3] / "configs" / "doctor" / "out_of_scope.yaml"


def _rendered(tool_count: int = 24) -> str:
    prompt = load_prompt(NAME)
    tools = [
        {
            "name": f"tool_{n}",
            "description": "Shows what a column holds, with counts.",
            "args": '{"column": "str"}',
        }
        for n in range(tool_count)
    ]
    done = render(
        prompt,
        {
            "goal": "Find the customers most likely to renew their policy.",
            "tools": tools,
            "questions": catalogue_lines(),
            "state": '{"files": ["F1"], "steps": []}',
            "turn_results": "none",
        },
    )
    return f"{done.system}\n{done.user}"


def test_it_loads_as_a_versioned_prompt_with_its_own_purpose() -> None:
    prompt = load_prompt(NAME)
    assert prompt.version == 1 and prompt.name == NAME
    assert prompt.purpose == GenerativePurpose.DOCTOR_AGENT.value
    assert (prompts_dir() / f"{NAME}.v1.md").is_file()


def test_it_renders_with_every_tool_and_question_and_stays_under_the_size_limit() -> None:
    text = _rendered()
    assert "{%" not in text and "{{" not in text
    for line in catalogue_lines():
        assert line in text
    assert "tool_23" in text
    # The model also gets each tool's own description, so the size limit is on the instructions and the
    # question list; the tools are counted by what they add on their own.
    words = len(_rendered(tool_count=0).split())
    assert words <= MAX_WORDS, f"the instructions are {words} words; keep them under {MAX_WORDS}"


def test_every_rule_r1_to_r29_is_in_it_once() -> None:
    text = load_prompt(NAME).system
    found = re.findall(r"^- (R\d+)\. ", text, flags=re.MULTILINE)
    assert sorted(found, key=lambda rule: int(rule[1:])) == [f"R{n}" for n in range(1, 30)]


def test_the_questions_it_names_exist_in_the_catalogue() -> None:
    text = load_prompt(NAME).system
    named = set(re.findall(r"\bQ\d+\b", text))
    assert named and named <= set(CATALOGUE)


def test_it_keeps_the_promises_the_checks_depend_on() -> None:
    system = load_prompt(NAME).system
    for phrase in (
        "one JSON object and nothing else",
        "Never claim a number that is not in a tool result",
        "Retry at most 2 times",
        "at most 3 blocking questions",
        "Never write code other than one SELECT",
        "Never use now()",
        "Never join on name similarity",
        "Never treat a blank consent as yes",
        "Never claim lift or extra sales without a random holdout",
        "the only proof",
        "Do not rewrite an expectation after it fails",
    ):
        assert phrase in system, phrase


@pytest.mark.parametrize("code", OUT_OF_SCOPE_CODES)
def test_the_out_of_scope_sentences_are_there(code: str) -> None:
    assert f"- {code}:" in load_prompt(NAME).system


def test_it_names_every_code_the_out_of_scope_config_has_once_that_config_exists() -> None:
    codes = (
        re.findall(r"^\s*-?\s*(?:code:\s*)?([A-Z][A-Z_]{2,})\s*:", CONFIG.read_text(encoding="utf-8"), re.M)
        if (CONFIG.is_file())
        else list(OUT_OF_SCOPE_CODES)
    )
    system = load_prompt(NAME).system
    missing = [code for code in codes if code not in system]
    assert (
        not missing
    ), f"configs/doctor/out_of_scope.yaml has codes the instructions do not mention: {missing}"


def test_the_instructions_are_untouched_by_the_egress_backstop() -> None:
    path = prompts_dir() / f"{NAME}.v1.md"
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        cleaned, matches = egress.assert_clean(line)
        assert matches == 0, f"{path.name} line {number} would be masked: {line!r} -> {cleaned!r}"
