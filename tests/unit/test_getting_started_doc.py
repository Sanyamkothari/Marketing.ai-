"""The marketer's guide (`docs/ux/GETTING_STARTED.md`, Plan M) keeps to the glossary.

`ui/labels.js` lists the words a marketer never sees outside a "Technical details" section (Plan M §4.1). The
guide is written for the same reader, so it may not use them either. The only place it may is the closing
"For administrators" section, which is for the person who turns the layout on and names a setting.

The word list is read from `ui/labels.js` itself, with the same matcher the visible-text check uses
(`findNeverShown`: whole words, case-insensitive, optional plural), so this test cannot drift from the
glossary. It needs node, which is not a Python dependency: without it the test skips with a reason, and
under `REQUIRE_JSDOM=1` (CI) that skip is a failure (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tests.fixtures.node import skip_without_node

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "ux" / "GETTING_STARTED.md"
LABELS = ROOT / "ui" / "labels.js"

ADMIN_HEADING = "For administrators"
"""The one section the rule does not cover. It must be the last one."""

_SCRIPT = """
import { findNeverShown } from %(labels)s;
let text = "";
for await (const chunk of process.stdin) text += chunk;
const lines = text.split("\\n");
const hits = [];
lines.forEach((line, i) => {
  const found = findNeverShown(line);
  for (const [term, count] of Object.entries(found)) hits.push({ line: i + 1, term, count, text: line.trim() });
});
process.stdout.write(JSON.stringify(hits));
"""


def _split_admin(text: str) -> tuple[str, str]:
    """The guide before the administrator section, and that section (empty when there is none)."""
    match = re.search(rf"^## {re.escape(ADMIN_HEADING)}\s*$", text, flags=re.MULTILINE)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.start() :]


def _never_shown_in(text: str) -> list[dict[str, object]]:
    node = skip_without_node()
    script = _SCRIPT % {"labels": json.dumps(LABELS.as_uri())}
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=text,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_guide_has_an_administrator_section_and_it_is_last() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    before, admin = _split_admin(text)
    assert admin, f"the guide needs a '## {ADMIN_HEADING}' section"
    assert admin.count("\n## ") == 0, "the administrator section is the last one"
    assert "MARKETING_AI_UI_LAYOUT" in admin
    assert "MARKETING_AI_UI_LAYOUT" not in before, "the setting belongs to the administrator section only"


def test_the_guide_covers_what_a_first_time_marketer_needs() -> None:
    before, _ = _split_admin(GUIDE.read_text(encoding="utf-8"))
    for place in ("Overview", "Campaigns", "Data", "Reports", "Settings"):
        assert f"**{place}**" in before, place
    for step in ("Goal", "Customers", "Audience", "Review", "Send"):
        assert re.search(rf"^\d\. \*\*{step}\.\*\*", before, flags=re.MULTILINE), step
    for label in ("Proven", "Estimated", "Not yet proven"):
        assert f"**{label}**" in before, label


def test_the_guide_uses_no_word_the_glossary_never_shows() -> None:
    before, _ = _split_admin(GUIDE.read_text(encoding="utf-8"))
    hits = _never_shown_in(before)
    assert not hits, "words that ui/labels.js never shows a marketer:\n" + "\n".join(
        f"  line {h['line']}: '{h['term']}' x{h['count']}: {h['text']}" for h in hits
    )


def test_the_check_would_catch_a_never_shown_word() -> None:
    """The check itself works: a sentence with a listed word is found, a clean one is not."""
    assert _never_shown_in("Pick a use case, then train a model.\nPick a goal.\n")[0]["line"] == 1
    assert _never_shown_in("Pick a goal and download the contact list.\n") == []
