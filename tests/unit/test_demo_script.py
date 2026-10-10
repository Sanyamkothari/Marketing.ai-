"""The manager demo script tells the M110 results as they fell, and quotes only numbers the summary holds (Plan J M111).

`library/DEMO_SCRIPT.md` is prose, so the checks are mechanical where they can be: every bold number in its
story must be a whole number on `library/hillstrom-email/DEMO_SUMMARY.md` (itself generated from the artefact and
tested in `test_demo_summary.py`); the sentences that would be spin if they were missing must be there; the
rehearsal by someone outside the team is recorded as pending until a person has filled it in; and the pointers from
the guides resolve.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIBRARY = REPO / "library"
SCRIPT = (LIBRARY / "DEMO_SCRIPT.md").read_text(encoding="utf-8")
SUMMARY = (LIBRARY / "hillstrom-email" / "DEMO_SUMMARY.md").read_text(encoding="utf-8")
NUMBER = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")


def _checked_region() -> str:
    return SCRIPT.split("<!-- numbers-checked:start -->", 1)[1].split("<!-- numbers-checked:end -->", 1)[0]


def _on_the_page(span: str) -> bool:
    """`span` occurs in the summary as itself, not inside a longer number."""
    return re.search(r"(?<![\d.,])" + re.escape(span) + r"(?![\d])", SUMMARY) is not None


def test_every_bold_number_in_the_story_is_on_the_summary_page() -> None:
    spans = re.findall(r"\*\*([^*\n]+)\*\*", _checked_region())
    # "**4. Does it ...?**" numbers the question; it is not a result
    quoted = [span for span in spans if NUMBER.search(span) and not re.match(r"\d\. ", span)]
    assert len(quoted) > 30, "the story should quote the results"
    missing = [span for span in quoted if not _on_the_page(span)]
    assert missing == [], f"numbers in the script that the summary page does not hold: {missing}"


def test_no_number_in_the_story_is_left_unbolded_so_none_can_drift_unchecked() -> None:
    region = re.sub(r"\*\*[^*\n]+\*\*", "", _checked_region())
    region = re.sub(r"<!--.*?-->", "", region)
    stray = NUMBER.findall(region)
    assert stray == [], f"numbers outside bold in the checked story, so not checked against the page: {stray}"


def test_the_click_path_quotes_only_numbers_the_summary_holds() -> None:
    """The click-by-click part reads numbers off the screen; any it quotes (beyond step numbers) are the page's."""
    clicks = SCRIPT.split("## The screens to click", 1)[1].split("## What looks contradictory", 1)[0]
    quoted = [token for token in NUMBER.findall(clicks) if len(token) > 1 or "." in token]
    assert [
        token for token in quoted if not _on_the_page(token)
    ] == [], "numbers in the click path the page lacks"


def test_the_script_tells_the_results_honestly() -> None:
    lowered = " ".join(SCRIPT.lower().split())
    for needed in (
        "the list beats sending nothing",
        "does not beat the obvious alternative",
        "uplift modelling does not beat plain risk ranking",
        "stable but not calibrated",
        "measurably worse",
        "the simple rule wins",
        "public data",
        "retrospective",
        "a client's own",
        "next proof",
        "never spin",
        "do not demo",  # Criteo: the licence warning stays
    ):
        assert needed in lowered, needed
    for spin in ("proves roi", "guaranteed", "game-changing", "revolutionary", "breakthrough"):
        assert spin not in lowered, spin


def test_the_script_names_the_things_that_look_contradictory_before_a_manager_asks() -> None:
    contradictory = SCRIPT.split("## What looks contradictory", 1)[1].split("## Questions you will get", 1)[0]
    for needed in (
        "Contact 7,596",
        "To treat 12,862",
        "Customers measured 25,599",
        "before the campaign went out",
    ):
        assert needed in contradictory, needed


def test_the_script_says_how_to_know_a_screen_is_on_the_full_file_and_not_the_sample() -> None:
    assert "scripts.seed_validated" in SCRIPT and "scripts.demo_summary" in SCRIPT
    assert "sample" in SCRIPT.lower() and "never for showing numbers" in SCRIPT
    assert "diff" in SCRIPT


def test_the_rehearsal_is_recorded_as_pending_until_a_person_outside_the_team_has_done_it() -> None:
    rehearsal = (LIBRARY / "DEMO_REHEARSAL.md").read_text(encoding="utf-8")
    assert "PENDING" in rehearsal.split("\n\n", 2)[1] or "PENDING" in rehearsal[:400]
    assert "Do not ask the team anything" in rehearsal
    assert (
        "Signed off by" in rehearsal and "______" in rehearsal
    ), "the sign-off is blank until a person fills it"
    assert "pending" in SCRIPT.lower() and "DEMO_REHEARSAL.md" in SCRIPT
    # a filled-in checkbox would mean someone signed it off; nobody has
    assert "[x]" not in rehearsal.lower()


def test_the_guides_and_the_library_point_at_the_demo() -> None:
    guide = (REPO / "docs" / "START_HERE.md").read_text(encoding="utf-8")
    assert "library/DEMO_SCRIPT.md" in guide and "scripts.seed_validated" in guide
    library = (REPO / "docs" / "LIBRARY.md").read_text(encoding="utf-8")
    assert "DEMO_SCRIPT.md" in library and "DEMO_SUMMARY.md" in library
    readme = (LIBRARY / "README.md").read_text(encoding="utf-8")
    for name in ("DEMO_SCRIPT.md", "DEMO_REHEARSAL.md", "DEMO_OTHER_DATASETS.md"):
        assert name in readme, name
        assert (LIBRARY / name).is_file()


def test_the_previous_walkthroughs_moved_unchanged_and_criteo_is_still_not_to_be_shown() -> None:
    other = (LIBRARY / "DEMO_OTHER_DATASETS.md").read_text(encoding="utf-8")
    assert "Never demo Criteo Uplift" in other
    assert (
        "## Telecom → Telco Customer Churn" in other
        and "## E-commerce → UCI Online Retail (win-back)" in other
    )
    assert "DEMO_SCRIPT.md" in other


def test_every_link_in_the_demo_files_resolves() -> None:
    for name in ("DEMO_SCRIPT.md", "DEMO_REHEARSAL.md", "DEMO_OTHER_DATASETS.md"):
        text = (LIBRARY / name).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            if "://" in target:
                continue
            assert (LIBRARY / target).resolve().exists(), f"{name} links to {target}, which does not exist"
