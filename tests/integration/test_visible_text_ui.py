"""Plan M, M170 (DEC-1600): the glossary (`ui/labels.js`) and the visible-text check, in the fast suite.

Two node suites in `tests/integration/production/ui/glossary/` (sharing that directory's jsdom install, as the other
screens' tests do):

* `labels.test.mjs` - the glossary as data: `term(key)`, the matching rules, and that `docs/ux/GLOSSARY.md` is the
  glossary rendered from it, word for word;
* `visible_text.test.mjs` - draws every main page in jsdom against the API answers recorded from the validated Hillstrom
  demo (`tests/fixtures/capture_ui_demo.py` -> `tests/fixtures/ui_demo/api.json`), collects what a person can read
  (technical sections and hidden elements left out) and fails on a glossary "never shown" word that is not on the page's
  allow-list (`baseline.json`) or is shown more often than the list allows. The same suite plants a violation and expects
  the check to fail.

M172 (DEC-1602) emptied the baseline: every page now passes with an empty allow-list, and the file stays as an empty
object so the check forbids any regression. A third suite, `words_numbers.test.mjs`, draws the same demo pages and pins
M172's other changes: the goal names of Plan M section 4.2, Home's "More goals" and stage labels, the one money format
(`ui/format.js`, exact figures in tooltips), the short campaign names and the mark on names the person supplied.

Node and jsdom are a development dependency of that one directory: without them the tests skip with the install command,
or fail under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
GLOSSARY_DIR: Final[Path] = NODE_DIR / "glossary"
RECORDED: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "ui_demo" / "api.json"


def test_the_recorded_demo_api_is_what_the_pages_are_drawn_from() -> None:
    """Runs without node: the replay file holds the demo's runs, campaigns and the answers a page needs to draw."""
    recorded: dict[str, Any] = json.loads(RECORDED.read_text(encoding="utf-8"))
    for key in (
        "GET /industries",
        "GET /runs?limit=100",
        "GET /use-cases/hillstrom-email",
        "GET /campaigns/summary",
        "GET /pilot/help",
    ):
        assert key in recorded, f"{key} was not recorded; run tests/fixtures/capture_ui_demo.py"
        assert recorded[key]["status"] == 200
    runs = recorded["GET /runs?limit=100"]["body"]["runs"]
    assert {r["mode"] for r in runs} == {"train", "score"}, "the demo has training and scoring runs"
    assert {r["run_id"] for r in runs} >= {
        "r_20261010_11100001",
        "r_20261010_11100002",
        "r_20261010_11100003",
        "r_20261010_11100004",
    }
    assert all(set(v) == {"status", "content_type", "body"} for v in recorded.values())


def test_the_baseline_is_a_plain_table_of_page_term_count() -> None:
    """Runs without node: `{page: {term: count}}`, whole positive counts, nothing else (an empty file is M172 done)."""
    baseline: dict[str, dict[str, int]] = json.loads(
        (GLOSSARY_DIR / "baseline.json").read_text(encoding="utf-8")
    )
    for page, terms in baseline.items():
        assert isinstance(page, str) and terms, f"{page} has no terms; drop the page"
        for term, count in terms.items():
            assert term == term.lower() and isinstance(count, int) and count > 0, (page, term, count)


def test_the_baseline_is_empty_so_no_page_may_show_an_engine_word() -> None:
    """Plan M M172 (DEC-1602): the sweep is done. The file stays, as an empty object, so the check forbids any regression."""
    baseline: dict[str, dict[str, int]] = json.loads(
        (GLOSSARY_DIR / "baseline.json").read_text(encoding="utf-8")
    )
    assert (
        baseline == {}
    ), f"M172 emptied the baseline; these pages show engine words again: {sorted(baseline)}"


def test_the_glossary_and_the_visible_text_check_in_jsdom() -> None:
    node = skip_without_jsdom(NODE_DIR)
    tests = [
        str(GLOSSARY_DIR / "labels.test.mjs"),
        str(GLOSSARY_DIR / "visible_text.test.mjs"),
        str(
            GLOSSARY_DIR / "m172_labels.test.mjs"
        ),  # Plan M M172: goal names, plainText, userText, short names
        str(
            GLOSSARY_DIR / "words_numbers.test.mjs"
        ),  # Plan M M172: goal names, Home, numbers, short campaign names
    ]
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={key: value for key, value in os.environ.items() if key != "GLOSSARY_UPDATE_BASELINE"},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
    table = [line[2:] for line in result.stdout.splitlines() if line.startswith("# ")]
    print("\n".join(table))  # -s shows M172's to-do list
