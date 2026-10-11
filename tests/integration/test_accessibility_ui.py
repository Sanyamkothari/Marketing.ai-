"""Plan M, M181 part A (DEC-1611): the accessibility checks, in the fast suite.

Two node suites in `tests/integration/production/ui/a11y/` (sharing that directory's jsdom install, as the other
screens' tests do):

* `pages.test.mjs` - every page of the visible-text check (its `PAGES` list is read from that test's source, so a page
  added there is checked here) is drawn in jsdom against the answers recorded from the validated Hillstrom demo, and
  its markup is checked: a name on every form control, button and link, alt text on images, one H1, headings that do
  not jump, no repeated id, no positive tabindex. It runs once per layout: `classic` always, and `campaigns` when
  `ui/layout.js` (M174) is in the tree. The defects the pages still have are listed in `a11y/known.json`, each with a
  reason, the file and line, and the fix; the suite fails on any new defect and the list can only shrink.
* `contrast.test.mjs` - the colour tokens and every text/background pair in the style sheets against WCAG AA (4.5:1,
  3:1 for large text); the failures that exist today are listed in `a11y/contrast_known.json` the same way.

The pages are not claimed to be clean: run this file with `-s` to see what is left. The keyboard path (Tab through the
Overview, the Help menu and the wizard) is `tests/integration/test_keyboard_path_ui.py`.

Node and jsdom are a development dependency of that one directory: without them the node tests skip with the install
command, or fail under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
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
A11Y_DIR: Final[Path] = NODE_DIR / "a11y"
REPO: Final[Path] = Path(__file__).resolve().parents[2]
LAYOUT_JS: Final[Path] = REPO / "ui" / "layout.js"
LAYOUTS: Final[tuple[str, ...]] = ("classic", "campaigns") if LAYOUT_JS.is_file() else ("classic",)

RULES: Final[frozenset[str]] = frozenset(
    {
        "control-name",
        "button-name",
        "link-name",
        "image-alt",
        "one-h1",
        "heading-jump",
        "tabindex-positive",
        "duplicate-id",
    }
)


def _node(*tests: str, layout: str = "classic") -> subprocess.CompletedProcess[str]:
    node = skip_without_jsdom(NODE_DIR)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"A11Y_UPDATE_KNOWN", "A11Y_DUMP", "A11Y_DUMP_CONTRAST"}
    }
    env["A11Y_LAYOUT"] = layout
    return subprocess.run(
        [node, "--test", *[str(A11Y_DIR / name) for name in tests]],
        cwd=NODE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


SUMMARY: Final[tuple[str, ...]] = (
    "# tests",
    "# suites",
    "# pass",
    "# fail",
    "# cancelled",
    "# skipped",
    "# todo",
    "# duration",
)


def _table(stdout: str) -> str:
    """The lines the node suites print for people (the to-do tables), without node's own summary."""
    return "\n".join(
        line[2:] for line in stdout.splitlines() if line.startswith("# ") and not line.startswith(SUMMARY)
    )


def _explained(key: str, why: dict[str, str]) -> None:
    """A known defect says why it is there, where in the code, and how to fix it; the file exists."""
    assert set(why) == {"reason", "where", "fix"}, key
    assert len(why["reason"]) > 40 and len(why["fix"]) > 20, key
    path = why["where"].split(" ", 1)[0].split(":", 1)[0]
    assert (REPO / path).is_file(), f"{key}: {path} is not a file in the repository"


def test_the_known_defect_lists_are_plain_and_every_entry_is_explained() -> None:
    """Runs without node: each entry has a reason, a place in the code and a fix, and a count above zero."""
    known: dict[str, Any] = json.loads((A11Y_DIR / "known.json").read_text(encoding="utf-8"))
    assert set(known) == {"defects", "reasons"}
    used: set[str] = set()
    for layout, pages in known["defects"].items():
        assert layout in {"classic", "campaigns"}
        for page, keys in pages.items():
            assert keys, f"{layout}/{page} has no defects; drop the page"
            for key, count in keys.items():
                assert key.split("|", 1)[0] in RULES, key
                assert isinstance(count, int) and count > 0, (layout, page, key, count)
                used.add(key)
    assert used == set(
        known["reasons"]
    ), "every listed defect has a reason, and every reason is for a listed defect"
    for key, why in known["reasons"].items():
        assert set(why) == {"reason", "where", "fix"}, key
        _explained(key, why)

    contrast: dict[str, Any] = json.loads((A11Y_DIR / "contrast_known.json").read_text(encoding="utf-8"))
    assert set(contrast) == {"palette", "rules"}
    for section in contrast.values():
        for key, why in section.items():
            _explained(key, why)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_page_has_names_alt_text_one_h1_and_no_new_defect(layout: str) -> None:
    result = _node("pages.test.mjs", layout=layout)
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
    assert f"Accessibility ({layout}):" in result.stdout, "the run did not draw the pages"
    print("\n" + _table(result.stdout))  # -s shows what is left


def test_the_palette_and_every_text_colour_reach_wcag_aa_apart_from_the_listed() -> None:
    result = _node("contrast.test.mjs")
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
    print("\n" + _table(result.stdout))
