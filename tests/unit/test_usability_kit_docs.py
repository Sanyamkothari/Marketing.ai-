"""Plan M, M181 part A (DEC-1611): the usability test kit keeps its promises.

`docs/ux/USABILITY_TEST.md` is a procedure a facilitator follows with real people, and
`docs/ux/usability_results_TEMPLATE.md` is the form results go in. Both are text, so both can rot. This file checks the
things that matter and that a reader would not notice failing:

* the template holds no results: every cell a person fills in is the blank `____`, and it says in so many words that
  results are recorded by a person and never invented (Plan M §5 M181);
* the two task cards do not use a word the kit forbids the facilitator to say (a card that named a menu would hand the
  participant the answer);
* the kit carries the targets of Plan M §0 and the questions the plan asks for (two tasks, at most 5 clicks, at least 4
  of 5, the ease rating from 1 to 7, three open questions);
* every command and file the kit names exists in the repository.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO: Final[Path] = Path(__file__).resolve().parents[2]
KIT: Final[Path] = REPO / "docs" / "ux" / "USABILITY_TEST.md"
TEMPLATE: Final[Path] = REPO / "docs" / "ux" / "usability_results_TEMPLATE.md"

BLANK: Final[str] = "____"


def _kit() -> str:
    return KIT.read_text(encoding="utf-8")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_the_template_says_results_are_recorded_by_a_person_and_never_invented() -> None:
    text = _template()
    assert "recorded by a person" in text
    assert "never invented" in text
    assert "A cell nobody observed stays empty" in text


def test_the_template_holds_no_results_only_blanks() -> None:
    """Every table row that is filled in per participant (P1 to P5) has nothing in it but blanks."""
    rows = [line for line in _template().splitlines() if re.match(r"\|\s*P[1-5]\s*\|", line)]
    assert len(rows) >= 20, "the template has a row per participant in each table"
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert all(c == BLANK for c in cells[1:]), f"the template has something filled in: {row}"
    assert _template().count(BLANK) > 100


def test_the_template_has_the_sections_the_kit_points_at() -> None:
    text = _template()
    for heading in (
        "## 1. The test",
        "## 2. Answer key",
        "## 3. Participants",
        "## 4. Task 1",
        "## 5. Task 2",
        "## 6. Words that confused",
        "## 7. What people said, verbatim",
        "## 8. The success rules",
        "## 9. Actions",
        "## 10. Decision (product owner only)",
    ):
        assert heading in text, heading
    for rule in ("R1", "R2", "R3", "R4", "R5", "R6"):
        assert f"| {rule} |" in text and f"| {rule} |" in _kit(), rule


def test_the_kit_carries_plan_ms_targets_and_questions() -> None:
    text = _kit()
    assert "Plan a win-back campaign and download who to contact" in text
    assert "Show what last month's campaign earned" in text
    assert "at most 5" in text and "at least 4" in text
    assert "Single Ease Question" in text and "1 to 7" in text
    open_questions = re.search(r"### 9\.2 At the end: three open questions\n\n(.*?)\n\nAsk them", text, re.S)
    assert open_questions, "section 9.2 lists the open questions"
    numbered = re.findall(r"^\d\. .+\?$", open_questions.group(1), re.M)
    assert len(numbered) == 3, numbered
    assert "never estimated" in text, "the kit says results are not estimated"
    assert "MARKETING_AI_UI_LAYOUT=campaigns" in text, "the layout to test is named with its setting"
    assert "recruit" in text.lower() and "have not seen Marketing AI" in text


def test_the_task_cards_use_none_of_the_words_the_facilitator_may_not_say() -> None:
    text = _kit()
    forbidden_line = re.search(
        r"\*\*Words the facilitator does not say\*\*[^:]*: (.*?)\. If the person", text, re.S
    )
    assert forbidden_line, "the kit lists the words the facilitator does not say"
    forbidden = [
        w.strip().lower() for w in forbidden_line.group(1).replace("\n", " ").split(",") if w.strip()
    ]
    assert len(forbidden) >= 20 and "contact list" in forbidden and "proof pack" in forbidden
    cards = re.findall(r"^> \*\*Task [12]\*\*\n((?:>.*\n?)+)", text, re.M)
    assert len(cards) == 2, "two task cards"
    for card in cards:
        spoken = card.replace(">", " ").lower()
        for word in forbidden:
            assert not re.search(rf"\b{re.escape(word)}\b", spoken), f"a task card says {word!r}: {card}"


def test_every_file_and_command_the_kit_names_exists() -> None:
    text = _kit()
    # (how the kit writes it, the file it means)
    named = (
        ("python -m scripts.seed_validated --force", "scripts/seed_validated.py"),
        ("python -m scripts.ui_screenshots", "scripts/ui_screenshots.py"),
        ("library/hillstrom-email/fetch.py", "library/hillstrom-email/fetch.py"),
        ("library/DEMO_SCRIPT.md", "library/DEMO_SCRIPT.md"),
        ("tests/integration/test_visible_text_ui.py", "tests/integration/test_visible_text_ui.py"),
        ("tests/integration/test_accessibility_ui.py", "tests/integration/test_accessibility_ui.py"),
        ("tests/integration/test_keyboard_path_ui.py", "tests/integration/test_keyboard_path_ui.py"),
        (
            "tests/integration/production/ui/a11y/known.json",
            "tests/integration/production/ui/a11y/known.json",
        ),
        ("contrast_known.json", "tests/integration/production/ui/a11y/contrast_known.json"),
        ("usability_results_TEMPLATE.md", "docs/ux/usability_results_TEMPLATE.md"),
    )
    for written, path in named:
        assert written in text, f"the kit no longer mentions {written}"
        assert (REPO / path).is_file(), f"the kit names {path}, which is not in the repository"


def test_the_journey_test_is_named_and_the_kit_says_what_to_do_if_it_is_missing() -> None:
    """M177 adds `tests/integration/test_campaign_wizard_journey.py`; until it is merged the kit says so instead of linking a ghost."""
    text = _kit()
    assert "tests/integration/test_campaign_wizard_journey.py" in text
    journey = REPO / "tests" / "integration" / "test_campaign_wizard_journey.py"
    if not journey.is_file():
        assert "If that file is not in the repository yet" in text


def test_the_serve_command_does_not_turn_demo_mode_on() -> None:
    """Demo mode is for the synthetic Demo Company. On the validated store it would show a false "Sample data: not
    loaded" notice to every participant, so the command the kit gives must not set it, and the pre-session checklist
    looks for the notice."""
    text = _kit()
    assert "DEMO_MODE=true" not in text, "the serve command turns demo mode on"
    assert "MARKETING_AI_UI_LAYOUT=campaigns MARKETING_AI_DATA_DIR=store-p1" in text
    assert 'no "Sample data" notice' in text.replace("There is no", "no")
