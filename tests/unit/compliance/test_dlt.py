"""The DLT matcher (Plan K M134, DEC-1405 (c)): the registered template, each slot filled by a value of its tag.

Rules only: the five documented tags, literal text kept exactly, a value of the wrong type refused, and the
work linear in the message length (a Thompson automaton, never a backtracking expression). No language model
is imported anywhere in `engine.compliance`.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from engine.compliance.dlt import (
    DLT_TAGS,
    DltTemplateError,
    Slot,
    TemplateMatcher,
    matches,
    parse_template,
)

TEMPLATE = "Dear {#var#}, use code {#alphanumeric#} for Rs {#numeric#} off. Shop {#url#} or call {#cbn#}"
GOOD = "Dear Asha, use code DIWALI50 for Rs 1,499 off. Shop https://shop.example.in/d or call +91 98765 43210"
REPO_ROOT = Path(__file__).resolve().parents[3]


def test_a_template_is_literal_text_and_tagged_slots() -> None:
    parsed = parse_template("Hi {#var#}, code {#ALPHANUMERIC#}{#numeric#}.")
    assert parsed.parts == ("Hi ", Slot("var"), ", code ", Slot("alphanumeric"), Slot("numeric"), ".")
    assert [slot.tag for slot in parsed.slots] == ["var", "alphanumeric", "numeric"]
    assert DLT_TAGS == ("var", "numeric", "alphanumeric", "url", "cbn")


@pytest.mark.parametrize("text", ["Hi {#name#}", "Offer {#date#} only", "{#var #}{#phone#}", "", "   "])
def test_an_unknown_tag_or_an_empty_template_is_refused(text: str) -> None:
    with pytest.raises(DltTemplateError):
        parse_template(text)


def test_a_known_tag_is_read_whatever_its_case_and_padding() -> None:
    assert parse_template("{# Var #}").parts == (Slot("var"),)
    assert parse_template("a {# b").parts == ("a {# b",), "an unclosed tag is literal text"


def test_the_message_that_fills_every_slot_with_its_type_matches() -> None:
    assert matches(TEMPLATE, GOOD)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (("Dear ", "Hello "), "a literal word changed"),
        (("off.", "off!"), "punctuation changed"),
        (("Shop ", "Shop  "), "a space added"),
        (("DIWALI50", "DIWALI-50"), "a hyphen in an alphanumeric slot"),
        (("1,499", "abc"), "letters in a numeric slot"),
        (("1,499", ",499"), "a numeric value must start with a digit"),
        (("https://shop", "ftp://shop"), "a url slot needs http://, https:// or www."),
        (("+91 98765 43210", "call-me"), "letters in a call-back number"),
        (("+91 98765 43210", "12345"), "a call-back number of five digits"),
        (("Asha", ""), "an empty variable"),
        ((" or call +91 98765 43210", ""), "the end of the template missing"),
    ],
)
def test_a_message_that_departs_from_the_template_does_not_match(change: tuple[str, str], why: str) -> None:
    old, new = change
    assert old in GOOD
    assert not matches(TEMPLATE, GOOD.replace(old, new, 1)), why


def test_case_matters_in_literal_text_but_not_in_a_url_s_scheme() -> None:
    assert not matches(TEMPLATE, GOOD.replace("Dear", "dear"))
    assert matches(TEMPLATE, GOOD.replace("https://", "HTTPS://"))
    assert matches(TEMPLATE, GOOD.replace("https://shop", "www.shop"))


def test_a_variable_holds_at_most_the_configured_characters() -> None:
    assert matches("Hi {#var#}!", "Hi " + "x" * 30 + "!")
    assert not matches("Hi {#var#}!", "Hi " + "x" * 31 + "!")
    assert matches("Hi {#var#}!", "Hi " + "x" * 31 + "!", var_max_chars=None)
    assert not matches("Code {#alphanumeric#}", "Code " + "A" * 6, var_max_chars=5)


def test_adjacent_slots_and_a_literal_inside_a_variable_are_matched_by_some_split() -> None:
    assert matches("{#var#}{#var#}{#var#} end", "a b c end")
    assert matches("Hi {#var#}, see {#url#}", "Hi Ravi, Mumbai, see www.x.in/a")  # the variable holds ", "
    assert matches("{#numeric#}{#alphanumeric#}", "12")
    assert not matches("{#numeric#}{#alphanumeric#}", "1")  # two slots need two characters at least


def test_matching_is_linear_in_the_message_length() -> None:
    """A long link in a {#url#} slot, at two lengths ten times apart: the time grows about ten times."""
    matcher = TemplateMatcher(parse_template("Hi {#var#}, see {#url#} now {#var#} {#var#} {#var#}"))

    def best(length: int) -> float:
        message = "Hi Ravi, see https://x.in/" + "a" * length + " now a b c"
        assert matcher.matches(message)
        times = []
        for _ in range(3):
            start = time.perf_counter()
            matcher.matches(message)
            times.append(time.perf_counter() - start)
        return min(times)

    small, large = best(2_000), best(20_000)
    print(f"\n[Perf] DLT matcher: 2k chars {small * 1000:.1f} ms, 20k chars {large * 1000:.1f} ms")
    assert large < 30 * max(small, 1e-4), "ten times the characters took far more than ten times as long"


def test_no_language_model_is_imported_by_the_gate() -> None:
    """Rules only (design §8.2): no module of engine/compliance imports an LLM client or the generative code."""
    banned = ("engine.llm", "engine.generative", "anthropic", "boto3", "openai")
    for path in sorted((REPO_ROOT / "engine" / "compliance").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(banned), f"{path.name} imports {name}"
