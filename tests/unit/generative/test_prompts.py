"""`engine.generative.prompts`: the loader, and the nine prompts the repository ships.

Two different things are proved here and it is worth saying which is which.

**The loader's contract.** A prompt is found by name at its highest version, parsed into front
matter plus a system and a user section, hashed by its exact bytes, and rendered through a
sandboxed environment that refuses a missing variable. Each of those is a rule a caller depends on:
the hash is what an artefact records, and the refusal is what stops a prompt with a hole in it from
producing a confidently wrong answer.

**The shipped prompts themselves.** Every file in `configs/prompts/` is swept: it must parse, its
declared `variables` must be exactly what its two sections use, its `purpose` must be a
`GenerativePurpose` member, and it must ask for JSON in a shape the flows can parse. A prompt whose
front matter and body have drifted apart is a prompt that fails at run time on the one input nobody
tested, and the sweep is read off the directory so a tenth prompt is covered by existing without
anyone editing this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from engine.generative.contracts import GenerativePurpose
from engine.generative.errors import GenerativeError
from engine.generative.prompts import (
    PROMPT_FILENAME,
    Prompt,
    list_prompts,
    load_prompt,
    prompt_hashes,
    prompt_versions,
    prompts_dir,
    render,
)

SHIPPED: tuple[str, ...] = list_prompts()

RAW_BLOCK = re.compile(r"{%-?\s*raw\s*-?%}.*?{%-?\s*endraw\s*-?%}", re.DOTALL)
VARIABLE = re.compile(r"{{-?\s*([a-z_][a-z0-9_]*)")
CONTROL = re.compile(r"{%-?\s*(?:for|if)\s+(?:[a-z_, ]+\s+in\s+)?([a-z_][a-z0-9_]*)")
LOOP_LOCAL = re.compile(r"{%-?\s*for\s+([a-z_][a-z0-9_]*)")

VALUES: dict[str, object] = {
    "question": "How long does activation take?",
    "chunks": [{"document": "d.md", "section": "s", "text": "Four hours."}],
    "refusal_message": "I don't have that.",
    "answer_language": "auto",
    "history": [{"role": "user", "text": "hello"}],
    "segment": "High",
    "evidence": '{"reasons": [{"id": "r1"}]}',
    "tone": "neutral_business",
    "entity": "customer",
    "band": "High",
    "band_action": "Send best offer",
    "reasons": [{"feature": "months_since_churn", "direction": "up"}],
    "brand_name": "A Brand",
    "allowed_fields": ["first_name", "plan_type"],
    "banned_claims": ["guaranteed"],
    "limits": {"sms_chars": 160, "whatsapp_chars": 1024, "email_subject_chars": 60, "email_body_words": 150},
    "required_line": "Reply STOP to opt out",
    "variant_labels": ["A", "B"],
    "channel": "sms",
    "source": "Refunds take 21 working days.",
    "generated": "Refunds take about three weeks.",
    "reference_answer": "Within four hours.",
    "answer": "Four hours.",
}
"""One plausible value per variable any shipped prompt declares, so the sweep can render them all."""


def test_the_repository_ships_a_prompt_for_every_purpose_that_needs_one() -> None:
    """A purpose with no prompt is a flow that cannot run; a prompt with no purpose is unmetered."""
    assert SHIPPED
    purposes = {member.value for member in GenerativePurpose} - {GenerativePurpose.EMBEDDING.value}
    assert set(SHIPPED) == purposes


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_prompt_parses_and_names_itself(name: str) -> None:
    prompt = load_prompt(name)
    assert isinstance(prompt, Prompt)
    assert prompt.name == name
    assert prompt.version >= 1
    assert prompt.purpose in {member.value for member in GenerativePurpose}
    assert prompt.description.endswith(".")
    assert prompt.system and prompt.user


@pytest.mark.parametrize("name", SHIPPED)
def test_a_prompts_declared_variables_are_exactly_what_it_uses(name: str) -> None:
    """Front matter and body must not drift: the declaration is what the loader checks against.

    Loop variables are excluded, because `{% for chunk in chunks %}` introduces `chunk` rather than
    requiring it - the caller supplies `chunks`, not `chunk`.
    """
    prompt = load_prompt(name)
    # A `{% raw %}` block is the literal placeholder syntax shown to the model, not a variable the
    # caller supplies, so it is taken out before the body is scanned.
    body = RAW_BLOCK.sub("", f"{prompt.system}\n{prompt.user}")
    locals_ = set(LOOP_LOCAL.findall(body)) | {"loop"}
    used = (set(VARIABLE.findall(body)) | set(CONTROL.findall(body))) - locals_
    assert used == set(prompt.variables), f"{name}: body uses {sorted(used)}"


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_prompt_renders(name: str) -> None:
    prompt = load_prompt(name)
    rendered = render(prompt, {key: VALUES[key] for key in prompt.variables})
    assert rendered.system.strip()
    assert rendered.user.strip()
    # No jinja tag may survive a render, and no declared variable may still be standing where it
    # was. A literal `{{field}}` may survive: the copy prompts show the placeholder syntax to the
    # model through a `{% raw %}` block, which is the whole point of that block.
    whole = f"{rendered.system}\n{rendered.user}"
    assert "{%" not in whole
    for variable in prompt.variables:
        assert not re.search(r"{{-?\s*" + variable + r"\b", whole), variable


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_prompt_asks_for_one_json_object_and_nothing_else(name: str) -> None:
    """Every flow parses the reply as JSON, so every prompt has to ask for JSON in the same words."""
    prompt = load_prompt(name)
    assert "one JSON object and nothing else" in prompt.system


@pytest.mark.parametrize("name", [n for n in SHIPPED if n.startswith("copy_")])
def test_a_copy_prompt_shows_the_placeholder_syntax_literally(name: str) -> None:
    """A `{% raw %}` block that was not raw would render the example away and leave no instruction."""
    rendered = render(load_prompt(name), {key: VALUES[key] for key in load_prompt(name).variables})
    assert "{{field}}" in rendered.system


@pytest.mark.parametrize("name", [n for n in SHIPPED if not n.startswith("judge_")])
def test_a_generating_prompt_forbids_inventing_a_number(name: str) -> None:
    """The rule the guardrails then enforce; a prompt that did not say it would make them a formality."""
    system = load_prompt(name).system.lower()
    assert "never" in system or "no " in system
    assert "invent" in system or "copied" in system or "no price" in system


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------
def test_a_missing_prompt_is_a_coded_error_naming_the_prompt_not_the_path() -> None:
    """A path is a deployment detail; the name is what a reader can act on."""
    with pytest.raises(GenerativeError) as error:
        load_prompt("no_such_prompt")
    assert error.value.code == "PROMPT_NOT_FOUND"
    assert "no_such_prompt" in error.value.message
    assert "/" not in error.value.message
    assert error.value.suggestion


def test_a_missing_variable_is_refused_before_anything_is_rendered() -> None:
    """Named, and by name: a template error would point at a line rather than at what is missing."""
    prompt = load_prompt("assistant_answer")
    with pytest.raises(GenerativeError) as error:
        render(prompt, {"question": "only this one"})
    assert error.value.code == "PROMPT_VARIABLE_MISSING"
    assert "chunks" in error.value.message


def test_the_highest_version_wins_and_an_older_one_can_still_be_asked_for(tmp_path: Path) -> None:
    directory = tmp_path / "configs" / "prompts"
    directory.mkdir(parents=True)
    for version, word in ((1, "older"), (2, "newer")):
        (directory / f"demo.v{version}.md").write_text(
            f"---\nname: demo\nversion: {version}\npurpose: demo\nvariables: []\n---\n\n"
            f"# System\n\nThe {word} wording.\n\n# User\n\nGo.\n",
            encoding="utf-8",
        )
    assert "newer" in load_prompt("demo", tmp_path / "configs").system
    assert "older" in load_prompt("demo", tmp_path / "configs", version=1).system
    with pytest.raises(GenerativeError) as error:
        load_prompt("demo", tmp_path / "configs", version=9)
    assert error.value.code == "PROMPT_NOT_FOUND"


def test_the_hash_is_of_the_file_so_a_reworded_prompt_is_a_different_prompt(tmp_path: Path) -> None:
    """The hash is what an artefact records, so it must move when and only when the wording does."""
    directory = tmp_path / "configs" / "prompts"
    directory.mkdir(parents=True)
    path = directory / "demo.v1.md"
    header = "---\nname: demo\nversion: 1\npurpose: demo\nvariables: []\n---\n\n# System\n\n"
    path.write_text(f"{header}One wording.\n\n# User\n\nGo.\n", encoding="utf-8")
    first = load_prompt("demo", tmp_path / "configs").content_hash
    path.write_text(f"{header}Another wording.\n\n# User\n\nGo.\n", encoding="utf-8")
    from engine.generative.prompts import _load_prompt_cached

    _load_prompt_cached.cache_clear()
    assert load_prompt("demo", tmp_path / "configs").content_hash != first


def test_the_rendered_input_hash_changes_with_the_values() -> None:
    """Half the cache key: two renderings of one prompt with different inputs are different calls."""
    prompt = load_prompt("judge_toxicity")
    one = render(prompt, {"generated": "a text"})
    two = render(prompt, {"generated": "another text"})
    assert one.input_hash != two.input_hash
    assert one.input_hash == render(prompt, {"generated": "a text"}).input_hash


def test_a_prompt_with_no_front_matter_is_refused_as_a_deployment_mistake(tmp_path: Path) -> None:
    """Nothing a customer does can produce one, so it is a `ValueError` naming the file, not a code."""
    directory = tmp_path / "configs" / "prompts"
    directory.mkdir(parents=True)
    (directory / "broken.v1.md").write_text(
        "# System\n\nNo front matter.\n\n# User\n\nGo.\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"broken\.v1\.md"):
        load_prompt("broken", tmp_path / "configs")


def test_a_prompt_missing_a_section_names_the_sections_it_did_find(tmp_path: Path) -> None:
    directory = tmp_path / "configs" / "prompts"
    directory.mkdir(parents=True)
    (directory / "halved.v1.md").write_text(
        "---\nname: halved\nversion: 1\npurpose: halved\nvariables: []\n---\n\n# System\n\nOnly one.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="System"):
        load_prompt("halved", tmp_path / "configs")


def test_the_environment_is_sandboxed_so_a_template_cannot_reach_into_what_it_is_given(
    tmp_path: Path,
) -> None:
    """A prompt is a file an operator edits; the sandbox is what makes that safe."""
    from jinja2.exceptions import SecurityError

    directory = tmp_path / "configs" / "prompts"
    directory.mkdir(parents=True)
    (directory / "nosy.v1.md").write_text(
        "---\nname: nosy\nversion: 1\npurpose: nosy\nvariables: [thing]\n---\n\n"
        "# System\n\n{{ thing.__class__.__mro__ }}\n\n# User\n\nGo.\n",
        encoding="utf-8",
    )
    prompt = load_prompt("nosy", tmp_path / "configs")
    with pytest.raises((GenerativeError, SecurityError)):
        render(prompt, {"thing": object()})


def test_the_filename_pattern_is_what_decides_a_prompt_from_any_other_markdown() -> None:
    assert PROMPT_FILENAME.match("assistant_answer.v1.md")
    assert PROMPT_FILENAME.match("copy_sms.v12.md")
    assert not PROMPT_FILENAME.match("README.md")
    assert not PROMPT_FILENAME.match("assistant_answer.md")
    assert not PROMPT_FILENAME.match("Assistant.v1.md")


def test_a_directory_with_no_prompts_lists_nothing_rather_than_failing(tmp_path: Path) -> None:
    assert list_prompts(tmp_path) == ()


def test_versions_and_hashes_are_what_an_artefact_records() -> None:
    names = ["assistant_answer", "judge_faithfulness"]
    versions = prompt_versions(names)
    hashes = prompt_hashes(names)
    assert set(versions) == set(names) == set(hashes)
    assert all(isinstance(value, int) for value in versions.values())
    assert all(len(value) == 32 for value in hashes.values())


def test_every_prompt_file_in_the_directory_is_one_the_loader_can_find() -> None:
    """A file that is nearly a prompt - a stray copy, a wrong suffix - would be silently ignored."""
    files = sorted(path.name for path in prompts_dir().glob("*.md"))
    assert files
    for filename in files:
        assert PROMPT_FILENAME.match(filename), filename
        meta = yaml.safe_load((prompts_dir() / filename).read_text(encoding="utf-8").split("---")[1])
        assert meta["name"] == PROMPT_FILENAME.match(filename).group("name")  # type: ignore[union-attr]
