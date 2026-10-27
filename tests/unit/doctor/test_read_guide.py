"""`read_guide` (Step 7b): the guide's rules plus the section asked for, a clear refusal for an unknown name or
section, and every shipped guide small enough to travel as one tool result."""

from __future__ import annotations

import pytest

from engine.agent.tools import AgentToolError
from engine.agent.untrusted import for_prompt
from engine.doctor.guides import (
    MAX_LINE_CHARS,
    MAX_LINES,
    guide_names,
    guide_sections,
    index_text,
    read_guide,
)


def test_a_section_comes_with_the_rules() -> None:
    name, section, lines = read_guide("features", "Buckets and interactions")
    text = "\n".join(lines)
    assert (name, section) == ("features", "Buckets and interactions")
    assert "## Rules" in text and "Never target-encode" in text
    assert "## Buckets and interactions" in text and "win-back" in text
    assert "One-hot, scaling" not in text  # another section is not sent


def test_no_section_gives_the_whole_guide_and_a_section_name_may_be_written_loosely() -> None:
    _, section, lines = read_guide("features")
    assert section is None
    assert {"## Rules", "## What to build", "## Receipt"} <= set(lines)
    assert read_guide("Features.md", "buckets and interactions")[1] == "Buckets and interactions"
    assert read_guide("features", "Rules")[2][0].startswith("## Rules")  # the rules are not sent twice


def test_the_rules_alone_when_the_section_is_rules() -> None:
    _, _, lines = read_guide("time_and_as_of", "Rules")
    assert lines[0] == "## Rules" and sum(1 for line in lines if line.startswith("## ")) == 1


def test_an_unknown_guide_is_refused_with_the_valid_names() -> None:
    with pytest.raises(AgentToolError) as caught:
        read_guide("../../etc/passwd")
    assert caught.value.code == "AGENT_GUIDE_UNKNOWN"
    assert all(n in caught.value.message for n in ("features", "time_and_as_of", "outcomes"))
    with pytest.raises(AgentToolError):
        read_guide("index")  # the index is in the prompt, not a guide
    with pytest.raises(AgentToolError):
        read_guide(None)


def test_an_unknown_section_is_refused_with_the_valid_sections() -> None:
    with pytest.raises(AgentToolError) as caught:
        read_guide("features", "No such section")
    assert caught.value.code == "AGENT_GUIDE_SECTION_UNKNOWN"
    assert "Rules" in caught.value.message and "Receipt" in caught.value.message


@pytest.mark.parametrize("name", guide_names())
def test_every_guide_reaches_the_model_whole(name: str) -> None:
    """The prompt keeps at most `MAX_PROMPT_LIST` lines of 500 characters of a result: no guide may be cut."""
    _, _, lines = read_guide(name)
    assert 0 < len(lines) <= MAX_LINES and all(len(line) <= MAX_LINE_CHARS for line in lines)
    assert for_prompt({"text": lines}, {})["text"] == lines
    for title in guide_sections(name):
        assert read_guide(name, title)[2]


def test_the_index_is_what_the_prompts_carry() -> None:
    assert index_text().startswith("- explore:") and len(index_text().split()) <= 150
