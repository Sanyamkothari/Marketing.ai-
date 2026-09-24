"""The mechanical gate on the deployment documents, so their honesty survives being edited.

`docs/AWS_DEPLOYMENT.md` says plainly that no cost in it has been measured, and ships both cost
tables with every cell holding the literal marker `NOT YET MEASURED`. That claim is true on the day
it is written and it is exactly the kind of claim that rots: the next person to open the file has a
figure in their head, drops it into one cell, and the document now looks measured without anybody
having decided that it is. Plan section 13.3 forbids writing down a number nobody measured, and a
rule that depends on every future editor remembering it is not a rule (DEC-399).

So the rule is a test. Five things are checked, over the WHOLE of each document rather than only
over its cost section, because a figure quoted in the troubleshooting section is quoted just as
hard:

* **Every currency figure is sourced.** A `$` or `USD` amount is allowed only inside a fenced block
  that is plainly a command - where it is part of something the reader runs, not a claim the
  document makes - or on a line that also says where it came from: an offer version, a publication
  date, or the words "list price". `configs/aws_prices.yaml` carries exactly that provenance and
  `CostEstimate.basis` carries it into the product, so a document is held to the same bar.
* **The cost tables still carry the marker**, in every cell. A table with one real number and
  eleven markers is worse than an empty one: it reads as though somebody measured the first row.
* **The cost section still explains itself in prose**, so a reader meets the reason before the
  shouting.
* **The old claim is gone.** The file this replaced opened "Nothing in this document is built",
  which stopped being true with Phase 4a.
* **Every `make` target and every `MARKETING_AI_*` variable the documents name is real.** A
  documented target that no longer exists wastes the reader's hour - the Phase 4a acceptance test
  is an hour long. A documented variable that is neither a `Settings` field nor in
  `NON_FIELD_ENV_VARS` is worse: the application ignores a name it does not recognise, so a reader
  who follows the document gets no error and no effect, and spends that hour looking at the wrong
  thing (DEC-304).

The `make` check reads only text the documents marked as code - a fenced block, or an inline
backtick span - because that is what "the document tells the reader to run" means here: in both
files, every command a reader is meant to type is formatted as code. Scanning the prose as well
would mean no sentence could contain the words "make sure", which is a lot of grammar to buy a
check on a habit neither document has.

This test gates the documents, not the product. Nothing here says a measurement may never be
written down - it says a measurement is written down together with where it came from.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from engine.settings import ENV_VARS, NON_FIELD_ENV_VARS

DOCUMENTS: tuple[str, ...] = ("docs/AWS_DEPLOYMENT.md", "docs/RUNBOOK.md")
"""The documents this gate covers, both of them Phase 4a operator documentation."""

DEPLOYMENT_DOC = "docs/AWS_DEPLOYMENT.md"
"""The one that carries the cost tables."""

MARKER = "NOT YET MEASURED"
"""What an unmeasured cell holds: literal, uppercase, and compared verbatim."""

OLD_CLAIM = "Nothing in this document is built"
"""The sentence the Phase 4 notes opened with. Phase 4a makes it false."""

COST_SECTION_HEADING = "## 6. What it costs"
COST_TABLE_HEADINGS: tuple[str, ...] = ("#### Idle cost, per month", "#### Per-run cost")
"""The two tables that stay entirely unmeasured until somebody measures them."""

CURRENCY = re.compile(r"(?:US\$|\$|USD\s*)\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*USD\b")
"""A currency amount: `$12`, `US$12.50`, `USD 12`, `12 USD`.

Deliberately not matched: `$(cat .image-digest)`, `$SERVICE_URL`, `${ENV}` and `monthly_budget_usd`.
None of those is a claim about money; a `$` followed by a digit is.
"""

CITATION = re.compile(r"list price|offer[_ ]version|publication date|\b\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)
"""What makes a figure sourced: the words "list price", an offer version, or a publication date."""

COMMAND_LANGUAGES = frozenset({"bash", "sh", "shell", "console"})
"""Fence languages that make a block plainly a command rather than a claim the document makes."""

FENCE = re.compile(r"^\s*```(.*)$")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
MAKE_TARGET = re.compile(r"\bmake\s+([a-z][a-z0-9-]*)")
MAKEFILE_RULE = re.compile(r"^([a-z][a-z0-9-]*)\s*:(?!=)", re.MULTILINE)
ENV_VAR = re.compile(r"\bMARKETING_AI_[A-Z0-9_]+")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")


@pytest.fixture(params=DOCUMENTS)
def document(request: pytest.FixtureRequest, repo_root: Path) -> tuple[str, str]:
    """`(relative path, text)` for each covered document, so a failure names which one."""
    relative = str(request.param)
    return relative, (repo_root / relative).read_text(encoding="utf-8")


@pytest.fixture
def deployment_doc(repo_root: Path) -> str:
    """`docs/AWS_DEPLOYMENT.md`: the document the cost tables live in."""
    return (repo_root / DEPLOYMENT_DOC).read_text(encoding="utf-8")


def _lines_with_fence(text: str) -> Iterator[tuple[int, str, str | None]]:
    """`(line number, line, fence language)` for every line; the language is None outside a fence.

    A fence marker line is yielded with the language it opens or closes. That never matters: a
    fence line carries no currency figure and no command.
    """
    language: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        match = FENCE.match(line)
        if match is not None:
            language = None if language is not None else (match.group(1).strip().lower() or "")
            yield number, line, language
            continue
        yield number, line, language


def _code_fragments(text: str) -> Iterator[str]:
    """Every fragment the document marked as code: fenced-block lines and inline backtick spans.

    This is what "the document tells the reader to run" means in these two files. Prose is not
    scanned, so a sentence may contain the words "make sure" without the test having an opinion.
    """
    for _, line, language in _lines_with_fence(text):
        if language is not None:
            yield line
        else:
            yield from INLINE_CODE.findall(line)


def _cells(row: str) -> list[str]:
    """The cells of one markdown table row, trimmed; an empty list when the line is not a row."""
    match = TABLE_ROW.match(row)
    if match is None:
        return []
    return [cell.strip() for cell in match.group(1).split("|")]


def _is_separator(cells: list[str]) -> bool:
    """Whether these cells are a `|---|:--:|` rule rather than data."""
    return all(cell and set(cell) <= {"-", ":"} for cell in cells)


def _table_after(text: str, heading: str) -> list[list[str]]:
    """The body rows of the first markdown table following `heading`.

    The header row and the `|---|` rule are dropped, so what comes back is the data an editor would
    be tempted to fill in.
    """
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    assert start is not None, f"{heading!r} is not in the document; a cost table moved or was renamed."
    rows: list[list[str]] = []
    seen_table = False
    for line in lines[start + 1 :]:
        cells = _cells(line)
        if not cells:
            if seen_table:
                break
            continue
        seen_table = True
        if _is_separator(cells):
            rows.clear()  # everything before the rule was the header row
            continue
        rows.append(cells)
    return rows


def _makefile_targets(repo_root: Path) -> set[str]:
    """Every target the Makefile defines, from its rules and from its `.PHONY` lists."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    targets = set(MAKEFILE_RULE.findall(makefile))
    phony = False
    for line in makefile.splitlines():
        if line.startswith(".PHONY:"):
            phony = True
            line = line.removeprefix(".PHONY:")
        elif not phony:
            continue
        targets.update(line.replace("\\", " ").split())
        phony = line.rstrip().endswith("\\")
    return targets


def test_every_currency_figure_is_a_command_or_is_sourced(document: tuple[str, str]) -> None:
    """A `$`/USD amount is inside a command block, or on a line that says where it came from."""
    relative, text = document
    unsourced = [
        (number, line.strip())
        for number, line, language in _lines_with_fence(text)
        if CURRENCY.search(line) and language not in COMMAND_LANGUAGES and not CITATION.search(line)
    ]
    assert not unsourced, (
        f"{relative} states a currency figure with nothing to source it. Put it in a fenced "
        f"{sorted(COMMAND_LANGUAGES)} block if it is part of a command, or name the offer version, "
        f"the publication date or the words 'list price' on the same line: {unsourced}"
    )


def test_the_cost_tables_are_entirely_unmeasured(deployment_doc: str) -> None:
    """Every cell of both cost tables still holds the marker, so nobody half-fills one."""
    for heading in COST_TABLE_HEADINGS:
        rows = _table_after(deployment_doc, heading)
        assert rows, f"{heading!r} has no table under it."
        filled = [
            (row[0], column, cell)
            for row in rows
            for column, cell in enumerate(row[1:], start=1)
            if cell != MARKER
        ]
        assert not filled, (
            f"{DEPLOYMENT_DOC}: a cell under {heading!r} no longer reads {MARKER!r}. A measurement "
            "belongs here only with the command and the date it was measured beside it, and a "
            f"partly filled table reads as though the whole table had been measured: {filled}"
        )


def test_the_cost_section_says_in_prose_that_nothing_was_measured(deployment_doc: str) -> None:
    """The markers are explained where they start, not left to speak for themselves."""
    section = deployment_doc.split(COST_SECTION_HEADING, 1)
    assert len(section) == 2, f"{DEPLOYMENT_DOC} has no {COST_SECTION_HEADING!r} section."
    assert "has been measured" in section[1], (
        f"{DEPLOYMENT_DOC}: the cost section has to say plainly, in prose, that nothing in it has "
        "been measured and why. A table full of markers with no sentence beside it is shouting."
    )


def test_the_old_not_built_claim_is_gone(deployment_doc: str) -> None:
    """The sentence the Phase 4 notes opened with stopped being true with Phase 4a."""
    assert OLD_CLAIM.lower() not in deployment_doc.lower(), (
        f"{DEPLOYMENT_DOC} still claims {OLD_CLAIM!r}. Phase 4a builds it. Say what is built and "
        "what is not, section by section, instead of one sentence covering the whole file."
    )


def test_every_make_target_named_exists(document: tuple[str, str], repo_root: Path) -> None:
    """A documented `make` target the Makefile does not define wastes the reader's hour."""
    relative, text = document
    targets = _makefile_targets(repo_root)
    named = {target for fragment in _code_fragments(text) for target in MAKE_TARGET.findall(fragment)}
    missing = sorted(named - targets)
    assert not missing, (
        f"{relative} tells the reader to run `make` targets the Makefile does not define: "
        f"{missing}. Either the target was renamed and the document was not, or the document "
        "formatted something as code that is not a command."
    )


def test_every_environment_variable_named_is_real(document: tuple[str, str]) -> None:
    """A documented `MARKETING_AI_*` name that is neither a field nor allowed has no effect at all."""
    relative, text = document
    known = set(ENV_VARS.values()) | set(NON_FIELD_ENV_VARS)
    unknown = sorted(set(ENV_VAR.findall(text)) - known)
    assert not unknown, (
        f"{relative} names {unknown}, which is neither a Settings field nor in NON_FIELD_ENV_VARS. "
        "The application ignores a MARKETING_AI_* name it does not recognise, so a reader who "
        "follows this document gets no error and no effect - which is the failure DEC-304 exists "
        "to make impossible to ship."
    )


def test_every_setting_has_a_row_in_the_deployment_field_table(deployment_doc: str) -> None:
    """A `Settings` field nobody documented is one a deployment cannot know to set (DEC-867).

    Checked as a row of the §3.2 table - field, then its variable - rather than as a mention
    anywhere, so a variable quoted in passing does not stand in for the row that says its default,
    who needs it and whether `cdk deploy` writes it.
    """
    missing = [
        variable
        for field, variable in ENV_VARS.items()
        if not re.search(rf"^\| `{re.escape(field)}` \| `{re.escape(variable)}` \|", deployment_doc, re.M)
    ]
    assert not missing, (
        f"{DEPLOYMENT_DOC}'s field table (§3.2) has no row for {missing}. Add one in the existing "
        "format: field, environment variable, SSM parameter, default, needed by, written by cdk deploy."
    )


def test_the_operator_guide_describes_the_sign_in_throttle(repo_root: Path) -> None:
    """`docs/PRODUCTION.md` once said there was no rate limiting; now it has to say how it works."""
    guide = (repo_root / "docs/PRODUCTION.md").read_text(encoding="utf-8")
    assert "no login rate limiting" not in guide.lower()
    for field in (
        "login_max_failures_per_account",
        "login_max_failures_per_address",
        "login_failure_window_seconds",
        "login_lockout_seconds",
        "trusted_proxy_hops",
    ):
        assert ENV_VARS[field] in guide, field
    assert "trusted_proxy_hops=1" in guide, "why a deployment behind the ALB trusts one hop"
