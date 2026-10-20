"""Checks on the committed screenshots of the marketer app (docs/v2/screenshots) and the CSS fixes made from them.

The screenshots are taken by hand with `capture.mjs` against the demo API, so CI cannot retake them. These
tests make sure the folder stays honest instead: every PNG is listed in the README and every listed PNG
exists, each one is the size its name says, and the folder stays small enough to commit.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SHOTS = REPO / "docs" / "v2" / "screenshots"
STYLES = REPO / "ui" / "v2" / "styles.css"
FEED = REPO / "ui" / "v2" / "screens" / "feed.css"

SCREENS = ("connect", "goal", "plan", "live", "proof", "operator")
DARK_SCREENS = ("live", "proof")
VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}
MAX_TOTAL_BYTES = 3_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height from the PNG header (the IHDR chunk is always first), without an image library."""
    head = path.read_bytes()[:24]
    assert head[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    assert head[12:16] == b"IHDR", f"{path.name} has no IHDR chunk first"
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


def _expected_names() -> set[str]:
    names = {f"{s}-{d}-light.png" for s in SCREENS for d in VIEWPORTS}
    names |= {f"{s}-{d}-dark.png" for s in DARK_SCREENS for d in VIEWPORTS}
    return names


def test_every_screen_is_captured_at_both_sizes_and_dark() -> None:
    present = {p.name for p in SHOTS.glob("*.png")}
    assert _expected_names() <= present, sorted(_expected_names() - present)


def test_readme_lists_exactly_the_pngs_in_the_folder() -> None:
    readme = (SHOTS / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\(([\w.-]+\.png)\)", readme))
    present = {p.name for p in SHOTS.glob("*.png")}
    assert linked == present, {"not in README": sorted(present - linked), "missing": sorted(linked - present)}


@pytest.mark.parametrize("name", sorted(_expected_names()))
def test_png_is_the_width_its_name_says_and_at_most_two_viewports_tall(name: str) -> None:
    device = name.split("-")[1]
    view_w, view_h = VIEWPORTS[device]
    width, height = _png_size(SHOTS / name)
    assert width == view_w
    assert 0 < height <= 2 * view_h


def test_screenshots_stay_small_enough_to_commit() -> None:
    total = sum(p.stat().st_size for p in SHOTS.glob("*.png"))
    assert total < MAX_TOTAL_BYTES, f"{total} bytes of PNG; quantize them as the README says"


@pytest.mark.parametrize("css", [STYLES, FEED], ids=["styles.css", "feed.css"])
def test_css_braces_balance(css: Path) -> None:
    text = re.sub(r"/\*.*?\*/", "", css.read_text(encoding="utf-8"), flags=re.S)
    assert text.count("{") == text.count("}")


def test_phone_top_bar_is_one_row_and_hides_only_the_wordmark_text() -> None:
    """The fix from the phone screenshots: a sticky header of three rows took a fifth of the screen."""
    css = STYLES.read_text(encoding="utf-8")
    phone = css[css.index("@media (max-width: 719px)") :]
    phone = phone[: phone.index("/* ---- wider screens")]
    assert re.search(r"\.topbar-row\s*{[^}]*flex-wrap:\s*nowrap", phone)
    assert re.search(r"\.wordmark-text\s*{[^}]*clip:", phone)
    assert "display: none" not in phone, "hide visually, so the Pause button and links stay reachable"


def test_feed_screens_do_not_double_the_shell_padding() -> None:
    css = FEED.read_text(encoding="utf-8")
    assert re.search(r"\.main \.ap-page\s*{[^}]*padding:\s*0;", css)


def test_operator_table_headers_may_wrap_while_short_cells_do_not() -> None:
    """With every cell on one line, whole headers made the decisions table wider than a desktop card and cut off
    its JSON buttons (seen in operator-desktop-light.png), so the headers are the part allowed to wrap."""
    css = re.sub(r"/\*.*?\*/", "", FEED.read_text(encoding="utf-8"), flags=re.S)
    th_rules = re.findall(r"\.ap-table th\s*{([^}]*)}", css)
    assert th_rules and not any("nowrap" in rule for rule in th_rules)
    assert re.search(r"\.ap-table td\s*{[^}]*white-space:\s*nowrap", css)
    assert re.search(r"\.ap-table td\.ap-reason\s*{[^}]*white-space:\s*normal", css)
