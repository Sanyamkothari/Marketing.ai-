"""The web-page reader (`engine.generative.parsers._read_html`, DEC-1275).

The corpus sweep in `test_parsing.py` already reads two of the fourteen documents as saved web pages
(`tests.fixtures.make_docs.FORMATS`) and compares them with their Markdown sources. What is pinned
here is what a page carries that the corpus does not: the chrome around the content, entities,
lists, a page with no heading, and a file that is not text at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import DocumentType, KnowledgeBaseConfig
from engine.generative.parsers import (
    CELL_SEPARATOR,
    DOCUMENT_EMPTY,
    DOCUMENT_NO_HEADINGS,
    DOCUMENT_PARSERS,
    DOCUMENT_UNREADABLE,
    ParseError,
    parse,
)

PAGE = """<!doctype html>
<html><head><title>Help centre</title>
<style>h1 { color: red; }</style>
<script>var secret = "<h1>Not a heading</h1>";</script></head>
<body>
<nav><ul><li><a href="/">Home</a></li><li><a href="/login">Sign in</a></li></ul></nav>
<header><h1>Refund policy</h1></header>
<p>Refunds are paid within <b>7 days</b> &amp; never in cash.</p>
<ul><li>Bring the receipt.</li><li>Bring the device.</li></ul>
<form><label>Search</label><input name="q"><button>Go</button></form>
<h2>Charges</h2>
<table><thead><tr><th>Plan</th><th>Fee</th></tr></thead>
<tbody><tr><td>Starter</td><td>Rs&nbsp;149</td></tr><tr><td></td><td></td></tr></tbody></table>
<p>First line<br>second line</p>
<noscript>Turn on JavaScript.</noscript>
</body></html>
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_html_and_htm_are_accepted_types_with_a_reader_each() -> None:
    assert {DocumentType.HTML, DocumentType.HTM} <= set(DOCUMENT_PARSERS)
    assert {DocumentType.HTML, DocumentType.HTM} <= set(KnowledgeBaseConfig().accepted_types)


@pytest.mark.parametrize("name", ["help.html", "help.htm", "HELP.HTML"])
def test_headings_open_sections_and_the_chrome_is_dropped(tmp_path: Path, name: str) -> None:
    document = parse(_write(tmp_path, name, PAGE))
    assert document.media_type == Path(name).suffix.lower().lstrip(".")
    assert document.pages is None
    assert [section.heading for section in document.sections] == ["Refund policy", "Charges"]
    refund, charges = document.sections
    assert refund.text == (
        "Refunds are paid within 7 days & never in cash.\n\nBring the receipt.\n\nBring the device."
    )
    assert charges.text.split("\n\n") == [
        f"Plan{CELL_SEPARATOR}Fee",
        f"Starter{CELL_SEPARATOR}Rs 149",
        "First line",
        "second line",
    ]
    everything = " ".join(section.text for section in document.sections)
    for chrome in ("Home", "Sign in", "secret", "Not a heading", "Search", "Go", "JavaScript", "color"):
        assert chrome not in everything, chrome
    assert DOCUMENT_NO_HEADINGS not in document.warnings


def test_a_page_with_no_heading_is_one_section_named_after_the_file(tmp_path: Path) -> None:
    document = parse(
        _write(tmp_path, "notice.html", "<html><body><p>Offices close at six.</p></body></html>")
    )
    assert [(s.heading, s.text) for s in document.sections] == [("notice.html", "Offices close at six.")]
    assert DOCUMENT_NO_HEADINGS in document.warnings


def test_a_page_of_chrome_only_is_empty_and_bytes_that_are_not_text_are_unreadable(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as empty:
        parse(
            _write(tmp_path, "chrome.html", "<html><head><script>x()</script></head><nav>Home</nav></html>")
        )
    assert empty.value.code == DOCUMENT_EMPTY

    binary = tmp_path / "scan.html"
    binary.write_bytes(b"\xff\xfe\x00\x81\x82 not utf-8")
    with pytest.raises(ParseError) as unreadable:
        parse(binary)
    assert unreadable.value.code == DOCUMENT_UNREADABLE


def test_an_unclosed_tag_does_not_swallow_the_rest_of_the_page(tmp_path: Path) -> None:
    """A self-closing or void element never opens a skipped region: `<input>` has no `</input>`."""
    page = "<h1>Porting</h1><p>Text a code<input type=text> to 1900.<br/>It takes two days.</p>"
    document = parse(_write(tmp_path, "porting.html", page))
    assert document.sections[0].text == "Text a code to 1900.\n\nIt takes two days."
