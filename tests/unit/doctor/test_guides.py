"""The agent's guides (configs/doctor/guides/*.md) stay small, honest and in step with the code.

They are loaded into the model's prompt on demand, so every word costs tokens on every call that reads them, and a
name that drifted from the code sends the model to a tool that does not exist. A guide holds rules, never facts about
one client's data: a copied example number would read as a measurement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.doctor.catalogue import CATALOGUE
from engine.doctor.questions import CATALOGUE as QUESTIONS
from engine.doctor.tools_features import NAMED

GUIDES = Path(__file__).resolve().parents[3] / "configs" / "doctor" / "guides"
MAX_WORDS = 400
MAX_INDEX_WORDS = 150
MAX_SECTION_WORDS = 150
NAMES = sorted(p.stem for p in GUIDES.glob("*.md"))
TOPICS = [n for n in NAMES if n != "index"]

# Words in backticks that are not tool names: arguments, columns, SQL, values, presets.
_NOT_TOOLS = re.compile(
    r"[^a-z_]|^(as_of|na|this|rfm|rfm_plus|rfm_ratios|t_\w*|reads_future|outcome_window_days|expect)$"
)


def _text(name: str) -> str:
    return (GUIDES / f"{name}.md").read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_there_is_an_index_and_it_names_every_guide() -> None:
    assert "index" in NAMES and len(TOPICS) >= 10
    index = _text("index")
    assert len(index.split()) <= MAX_INDEX_WORDS
    for name in TOPICS:
        assert re.search(rf"\b{name}\b", index), f"index.md does not list {name}"


@pytest.mark.parametrize("name", TOPICS)
def test_a_guide_opens_with_its_rules_and_stays_short(name: str) -> None:
    text = _text(name)
    assert text.startswith("# Guide: ")
    sections = _sections(text)
    assert next(iter(sections), None) == "Rules", "the rules come first: they are what the model reads"
    assert len(text.split()) <= MAX_WORDS, f"{name}.md has {len(text.split())} words"
    for title, body in sections.items():
        assert len(body.split()) <= MAX_SECTION_WORDS, f"{name}.md section {title!r} is too long"


@pytest.mark.parametrize("name", NAMES)
def test_a_guide_holds_no_numbers_about_one_clients_data(name: str) -> None:
    text = _text(name)
    assert not re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b|\b20[0-3]\d\b", text), "a calendar date or year"
    # Product limits (1,000 rows, a 100,000-row trial) and Indian grouping examples (3,50,000) are rules, not counts.
    rules = re.sub(r"\b(1,000|100,000)\b|\b\d{1,2}(,\d{2})+,\d{3}\b", "", text)
    assert not re.search(r"\b\d{1,3}(,\d{3})+\b", rules), "a row or customer count"


@pytest.mark.parametrize("name", NAMES)
def test_every_question_id_a_guide_names_exists(name: str) -> None:
    for qid in set(re.findall(r"\bQ\d{1,2}\b", _text(name))):
        assert qid in QUESTIONS, f"{name}.md names {qid}, which the question catalogue does not have"


@pytest.mark.parametrize("name", NAMES)
def test_every_tool_a_guide_names_in_backticks_exists(name: str) -> None:
    known = {row.name for row in CATALOGUE} | {
        "propose_step",
        "propose_sql",
        "ask_question",
        "express_doubt",
        "request_data",
        "suggest_answer",
        "reply",
        "finish",
        "withdraw",
        "join_files",
        "stack_files",
        "read_guide",
        "propose_features",
    }
    known |= set(NAMED)  # the named aggregations the features guide lists
    # Action arguments are named in the prompts the model already reads (`evidence_ids`, `rows_out_equals`, ...).
    prompts = GUIDES.parents[1] / "prompts"
    known |= {
        word
        for prompt in ("data_doctor_agent.v1.md", "data_doctor_chat.v1.md")
        for word in re.findall(r"[a-z_]{3,}", (prompts / prompt).read_text(encoding="utf-8"))
    }
    # Step parameters the policy allows (`indian_scale_words`, `dayfirst`, ...).
    policy = GUIDES.parents[1] / "agentic" / "policy.yaml"
    known |= set(re.findall(r"[a-z_]{3,}", policy.read_text(encoding="utf-8")))
    for word in re.findall(r"`([^`]+)`", _text(name)):
        if _NOT_TOOLS.search(word) or "_" not in word:
            continue  # not a snake_case name: a value, an expression or a column
        if word.endswith(("_column", "_id", "_days", "_rate", "_date")) or word.startswith(("is_", "t_")):
            continue
        assert word in known or word in {k.value for k in _step_kinds()}, f"{name}.md names `{word}`, unknown"


def _step_kinds() -> list:  # type: ignore[type-arg]
    from engine.agent.contracts import RecipeStepKind

    return list(RecipeStepKind)
