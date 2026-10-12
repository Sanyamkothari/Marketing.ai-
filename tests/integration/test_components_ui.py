"""Plan M M185 (DEC-1615): the look's foundation - `ui/theme.css`, the Inter font and `ui/components.js`.

Two halves:

* without node: the style sheet and the font are what the design direction says and the app serves them - Inter
  Variable shipped with its licence, one `<link>` in the PLAN-M block after every inline style, the tokens, every
  brand-coloured rule reading `var(--brand-blue)` (so M182's brand colour reaches it), status colours only with an icon
  and a word, every component class of `components.js` styled, no page card;
* with node: `tests/integration/production/ui/components/components.test.mjs` draws every helper in jsdom and reads it
  the way a browser and a screen reader do (roles, names, escaping, the tabs' arrow keys).

Node and jsdom are a development dependency of that one directory: without them the jsdom test skips with the install
command, or fails under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
UI: Final[Path] = ROOT / "ui"
NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
FONTS: Final[tuple[str, ...]] = ("inter-latin-wght-normal.woff2", "inter-latin-ext-wght-normal.woff2")
LINK: Final[str] = '<link rel="stylesheet" href="./theme.css">'


def theme() -> str:
    return (UI / "theme.css").read_text(encoding="utf-8")


def rules(css: str) -> list[tuple[str, str]]:
    """`(selector, body)` for every innermost `selector{body}` block, comments removed."""
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [
        (" ".join(m.group(1).split()).split("}")[-1].strip(), m.group(2))
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean)
    ]


def test_inter_is_shipped_with_its_licence_and_served() -> None:
    folder = UI / "fonts" / "inter"
    for name in FONTS:
        data = (folder / name).read_bytes()
        assert data[:4] == b"wOF2", f"{name} is a woff2 file"
        assert 30_000 < len(data) < 200_000, name
    licence = (folder / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License, Version 1.1" in licence and "The Inter Project Authors" in licence
    css = theme()
    for name in FONTS:
        assert f"url(./fonts/inter/{name})" in css, name
    assert css.count("@font-face") == 2
    assert css.count('font-family: "Inter Variable";') == 2
    assert css.count("font-display: swap;") == 2
    # the rupee sign is in the latin-ext file's range, so amounts are drawn in Inter too
    assert "U+20AD-20C0" in css
    client = TestClient(create_app())
    for name in FONTS:
        response = client.get(f"/ui/fonts/inter/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "font/woff2"
    sheet = client.get("/ui/theme.css")
    assert sheet.status_code == 200
    assert sheet.headers["content-type"].startswith("text/css")


def test_the_theme_is_linked_once_in_the_plan_m_block_after_every_style() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert html.count('href="./theme.css"') == 1
    block = html[html.index("<!-- ---- PLAN-M (simple UI)") : html.index("<!-- ---- END PLAN-M ---- -->")]
    assert LINK in block, "the one link line sits inside the PLAN-M block"
    assert html.index(LINK) > html.rindex("</style>"), "after the last inline <style>, so it wins the cascade"
    # and every module injects its styles into <head>, which comes before it
    for path in UI.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        if 'createElement("style")' in text:
            assert re.search(r"\.head\b|documentElement", text), f"{path.name} injects its styles into <head>"


def test_the_tokens_of_the_design_direction() -> None:
    css = theme()
    root = re.search(r":root\s*\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    assert root
    tokens = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", root.group(1)))
    assert tokens["--btn"] == "var(--brand-blue)", "the primary button is the brand colour, not black"
    assert tokens["--bg"] == "#F5F6F8"
    for name, value in {
        "--good": "#15803D",
        "--good-t": "#E8F6EE",
        "--warning": "#B45309",
        "--warning-t": "#FEF3C7",
        "--critical": "#B42318",
        "--critical-t": "#FDECEC",
    }.items():
        assert tokens[name] == value, name
    for name, value in {
        "--s1": "4px",
        "--s2": "8px",
        "--s3": "12px",
        "--s4": "16px",
        "--s5": "24px",
        "--s6": "32px",
        "--s7": "48px",
    }.items():
        assert tokens[name] == value, name
    assert tokens["--fs-title"] == "28px" and tokens["--lh-title"] == "36px"
    assert tokens["--fs-body"] == "14px" and tokens["--lh-body"] == "22px"
    assert tokens["--r-lg"] == "12px" and tokens["--r"] == "8px"
    assert tokens["--page-max"] == "1200px"
    assert tokens["--font"].startswith('"Inter Variable"')
    # the brand tints follow the brand colour wherever color-mix() exists, with a fixed fallback for the default
    for tint in ("--brand-50", "--brand-100", "--brand-200", "--brand-700"):
        assert re.match(r"^#[0-9A-F]{6}$", tokens[tint]), tint
        assert re.search(
            rf"{tint}: color-mix\(in srgb, var\(--brand-blue\) \d+%, (white|black)\);", css
        ), tint


def test_brand_coloured_things_read_the_brand_token() -> None:
    """M182: a deployment's colour must reach every brand-coloured thing, so no rule names the default blue."""
    css = re.sub(r"/\*.*?\*/", "", theme(), flags=re.DOTALL)
    body = css[css.index("}", css.index("@supports")) :]
    assert not re.search(r"#1E57BD|#2F6FDB|30,\s*87,\s*189", body, flags=re.IGNORECASE)
    by_selector = dict(rules(css))
    for selector in (
        ".btn.primary, .run",
        '.tab.on, .ds-tab[aria-selected="true"]',
        ".sh-item.on",
        ".sh-item.on::before",
        ".stepper .st-step.done .st-dot",
        ".vrange .vr-dot",
        ".badge.tone-info",
    ):
        assert selector in by_selector, selector
        assert "var(--brand-" in by_selector[selector], selector


def test_no_page_card_and_one_card_treatment() -> None:
    by_selector: dict[str, str] = {}
    for sel, b in rules(theme()):
        by_selector.setdefault(sel, b)
    screen = by_selector[".screen"]
    assert (
        "background: transparent" in screen
        and "border: 0" in screen
        and "max-width: var(--page-max)" in screen
    )
    card = by_selector[".card"]
    assert "border: 1px solid var(--line)" in card and "border-radius: var(--r-lg)" in card
    assert "box-shadow" not in card, "one treatment for every card: the hairline"
    assert "border: 0" in by_selector[".card .card"], "a card inside a card loses its own box"


def test_every_class_the_components_draw_is_styled() -> None:
    js = (UI / "components.js").read_text(encoding="utf-8")
    drawn: set[str] = set()
    for attr in re.findall(r'class="([^"$]+)"', js):
        drawn.update(attr.split())
    for template in re.findall(r'class="([^"]*\$\{[^"]*)"', js):
        drawn.update(word for word in re.sub(r"\$\{[^}]*\}", " ", template).split() if not word.endswith("-"))
    drawn.update({"primary", "secondary", "tertiary", "danger", "sm", "done", "current", "upcoming"})
    drawn.update(f"tone-{tone}" for tone in ("good", "warning", "critical", "info", "neutral"))
    css = theme()
    # classes styled by ui/index.html's own rules already (the components reuse them)
    shared = {"btn", "h1", "desc", "sr", "danger", "empty-state"}
    missing = sorted(c for c in drawn - shared if not re.search(rf"\.{re.escape(c)}(?![\w-])", css))
    assert missing == [], f"components.js draws classes theme.css never styles: {missing}"


def each_selector(css: str) -> dict[str, str]:
    """Every rule's body under each selector of its group (`a, b {x}` gives a and b the body x), in source order."""
    out: dict[str, str] = {}
    for group, body in rules(css):
        for selector in group.split(","):
            key = selector.strip()
            out[key] = out.get(key, "") + body
    return out


def test_status_colours_come_with_an_icon_and_a_word() -> None:
    """The status bridge for markup drawn before M185 adds an icon to every tone it colours: the health chips of
    Connections (M185 review). The campaign status chip (M186), Reports' evidence label and Data's health chip (M188)
    are components.js `statusChip` now, an inline icon and the word, so their bridges were removed at integration
    (`test_no_page_draws_the_removed_status_bridges`)."""
    by_selector = each_selector(theme())
    statuses = set()
    for path in sorted((UI / "modules").rglob("*.js")):
        statuses.update(re.findall(r'data-cn-status="([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert {"connected", "failed", "not_tested"} <= statuses
    for status in sorted(statuses):
        selector = f'.cn-badge[data-cn-status="{status}"]'
        body = by_selector.get(selector, "")
        assert "--ds-icon: url(" in body and "color: var(--" in body, f"{selector} has no icon or tone"
    assert "mask: var(--ds-icon)" in by_selector[".cn-badge[data-cn-status]::before"]


def test_no_page_draws_the_removed_status_bridges() -> None:
    """Integration of the look (M186-M188 hand-backs): no page draws the campaign status chip `.chip.cp-status`, a
    `.pill` or `.chip` inside Reports' `[data-claim]`, or a `.cn-badge` with `data-dt-status`, so the theme no longer
    styles them. A page that draws one again must use `statusChip` instead."""
    clean = re.sub(r"/\*.*?\*/", "", theme(), flags=re.DOTALL)
    for gone in (".cp-status", "[data-claim", "data-dt-status"):
        assert gone not in clean, f"theme.css still styles {gone}"
    for path in sorted(UI.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert "cp-status" not in text, path
        assert not re.search(r"cn-badge[^>]*data-dt-status|data-dt-status[^>]*cn-badge", text), path
        assert not re.search(r"""data-claim=[^>]*>\s*<span class=["'](?:pill|chip)""", text), path
    proof = (UI / "modules" / "reports" / "value_proof.js").read_text(encoding="utf-8")
    assert 'data-claim="${esc(claim.key)}">${statusChip("evidence"' in proof, "Reports' label is a statusChip"


def test_a_small_button_is_a_40px_target_on_a_touch_screen() -> None:
    """M186-M188 reviews, at integration: on a coarse pointer every small button (secondary, tertiary and quiet) is
    at least 40 px tall, whatever page draws it; the pages keep their own rules."""
    css = theme()
    block = css[css.index("@media (pointer: coarse) {") :]
    block = block[: block.index("}\n}") + 3]
    inner = each_selector(block)
    for selector in (".btn.sm", ".btn.tertiary.sm", ".btn.quiet.sm"):
        assert "min-height: 40px" in inner.get(selector, ""), selector
    # it comes after the 24 px tertiary and quiet rule, so it wins at the same specificity
    assert css.index("@media (pointer: coarse) {") > css.index(".btn.tertiary.sm {")


def test_a_value_range_without_an_estimate_draws_its_bar_in_the_brand_colour() -> None:
    """M187 review, at integration: with no estimate dot the bar is the range's only mark, so it is drawn in
    `--brand-blue` (the pale `--brand-200` reached about 1.4:1 on white); with a dot the bar stays the tint."""
    by_selector = each_selector(theme())
    assert "background: var(--brand-blue)" in by_selector[".vrange:not(:has(.vr-dot)) .vr-bar"]
    assert "background: var(--brand-200)" in by_selector[".vrange .vr-bar"]


def test_the_helpers_hold_in_narrow_places() -> None:
    """M185 review: the rules that keep the helpers usable on a phone (test_shell_browser draws them in Chromium).

    A stepper's link back has a box and a focus ring (not `display: contents`); a tile's unit wraps whole and its
    figure never breaks mid-number; a value range's track shrinks; a chip keeps its own width in a flex column; the
    bar's bell and Help are never made smaller than 40 px.
    """
    css = theme()
    by_selector = each_selector(css)
    link = by_selector[".stepper .st-step a"]
    assert "display: contents" not in link and "display: flex" in link
    assert "outline: 2px solid var(--brand-blue)" in by_selector[".stepper .st-step a:focus-visible"]
    value = by_selector[".stat .stat-value"]
    assert "overflow-wrap: anywhere" not in value and "overflow-wrap: break-word" in value
    unit = by_selector[".stat .stat-unit"]
    assert "white-space: nowrap" in unit and "display: inline-block" in unit
    track = by_selector[".vrange .vr-track"]
    assert "flex: 1 1 120px" in track and "max-width: 160px" in track and "min-width: 64px" in track
    for chip in (".chip", ".pill", ".badge", ".tag"):
        assert "width: fit-content" in by_selector[chip], chip
    for selector, body in rules(css):
        if any(part.strip().endswith(".sh-icon") for part in selector.split(",")):
            for size in re.findall(r"(?:^|;|\s)(?:width|height):\s*(\d+)px", body):
                assert int(size) >= 40, f"{selector} makes a bar target {size} px"
    assert (
        "td .td-sub" in by_selector and ".ds-table" not in css
    ), "the second line works inside dataTable's .tbl"


def test_reduced_motion_turns_the_motion_off() -> None:
    css = theme()
    block = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert "transition-duration: 0ms !important" in block and "animation-duration: 0ms !important" in block


def test_the_components_in_jsdom() -> None:
    node = skip_without_jsdom(NODE_DIR)
    result = subprocess.run(
        [node, "--test", str(NODE_DIR / "components" / "components.test.mjs")],
        cwd=NODE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
