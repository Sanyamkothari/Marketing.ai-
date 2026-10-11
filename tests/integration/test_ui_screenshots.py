"""Plan M, M181 part A: the screenshot script (`scripts/ui_screenshots.py`) takes what it says it takes.

It runs the real script into a temporary folder for one cheap page and checks the pictures and the index it wrote. It
skips, with the reason, when playwright or Chromium is missing (the same rule as the keyboard path check).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the screenshot set needs a browser"
)

from scripts import ui_screenshots  # noqa: E402 - after the importorskip on purpose


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_the_script_writes_a_desktop_and_a_phone_picture_and_an_index(tmp_path: Path) -> None:
    chromium = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
    if not chromium.exists():
        pytest.skip(f"no Chromium at {chromium}; set PLAYWRIGHT_BROWSERS_PATH")
    out = tmp_path / "shots"
    code = ui_screenshots.main([str(out), "--layouts", "classic", "--pages", "home"])
    assert code == 0
    desktop = out / "classic-home-desktop.png"
    phone = out / "classic-home-phone.png"
    assert desktop.is_file() and phone.is_file()
    assert _png_size(desktop)[0] == 1440, "the desktop picture is 1440 px wide"
    assert _png_size(phone)[0] == 390, "the phone picture is 390 px wide"
    index = (out / "INDEX.md").read_text(encoding="utf-8")
    assert "classic-home-desktop.png" in index and "classic-home-phone.png" in index
    assert "drew nothing" not in index and "scrolls sideways" not in index, index
    assert sorted(p.name for p in out.iterdir()) == [
        "INDEX.md",
        "classic-home-desktop.png",
        "classic-home-phone.png",
    ]


def test_an_unknown_layout_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ui_screenshots.main([str(tmp_path / "x"), "--layouts", "sideways"]) == 2
    assert "unknown layout" in capsys.readouterr().err


def test_base_with_two_layouts_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """One running application shows one layout; photographing it twice would label half the files wrongly."""
    code = ui_screenshots.main(
        [str(tmp_path / "x"), "--base", "http://127.0.0.1:9", "--layouts", "classic,campaigns"]
    )
    assert code == 2
    assert "exactly that one" in capsys.readouterr().err


def test_pages_that_need_a_run_or_campaign_id_are_skipped_without_one() -> None:
    """The recorded ids do not exist in a store seeded fresh: without --run/--campaign those pages are left out."""
    for _name, _layout, template in ui_screenshots.PAGES:
        needs = "{run}" in template or "{campaign}" in template
        assert (ui_screenshots.resolve(template, "", "") is None) == needs
    assert ui_screenshots.resolve("#/campaigns/{campaign}", "", "c_1") == "#/campaigns/c_1"
    assert ui_screenshots.resolve("#/uc/x/run/{run}", "r_1", "") == "#/uc/x/run/r_1"
    assert ui_screenshots.resolve("#/uc/x/run/{run}", "", "c_1") is None


def test_base_without_ids_lists_the_id_pages_as_skipped(tmp_path: Path) -> None:
    """With no --run/--campaign, the campaign page is not photographed and INDEX.md says so."""
    chromium = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
    if not chromium.exists():
        pytest.skip(f"no Chromium at {chromium}; set PLAYWRIGHT_BROWSERS_PATH")
    from tests.fixtures.ui_replay import ReplayServer

    out = tmp_path / "shots"
    with ReplayServer(ui_layout="classic") as server:
        code = ui_screenshots.main(
            [str(out), "--base", server.base, "--layouts", "classic", "--pages", "campaign"]
        )
    assert code == 0
    index = (out / "INDEX.md").read_text(encoding="utf-8")
    assert "## Skipped" in index and "classic campaign: needs the id" in index, index
    assert not list(out.glob("*.png"))
