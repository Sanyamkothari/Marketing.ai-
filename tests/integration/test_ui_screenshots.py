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
