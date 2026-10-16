"""Small layout and wording fixes from the v1 UI audit (a phone-width clip, a blank technical tab, stale names).

The CSS ones are asserted on the source, because a stylesheet has no behaviour to run without a real browser
(the audit itself ran Chromium at 390 px and 1280 px). The breadcrumb names are run in node against the real
`ui/dom.js`, which has no imports, so no jsdom is needed. Skipped when node is not installed (failed under
`REQUIRE_JSDOM=1`, as CI sets it: `tests/fixtures/node.py`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

from tests.fixtures.node import skip_without_node

UI: Final[Path] = Path(__file__).resolve().parents[2] / "ui"


def test_the_setup_grid_cannot_grow_wider_than_a_phone() -> None:
    """`1fr` keeps a column as wide as its widest content; `minmax(0, 1fr)` lets it shrink to the screen.

    The wizard's "Customers" step drew the whole Data Doctor card 1,028 px wide on a 390 px phone, clipped on the right.
    """
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "@media (max-width:1100px){.setup-grid{grid-template-columns:minmax(0,1fr)}}" in html


def test_a_long_button_label_wraps_and_a_results_summary_does_not_wrap_its_column() -> None:
    losses = (UI / "modules" / "datadoctor" / "styles.js").read_text(encoding="utf-8")
    assert ".dds-funnel-losses .btn{height:auto;white-space:normal" in losses
    usecase = (UI / "usecase.js").read_text(encoding="utf-8")
    assert ".summary.rsum{display:flex;flex-direction:column;flex-wrap:nowrap" in usecase
    assert ".rsum .btn{height:auto;" in usecase


def test_a_technical_tab_opened_by_its_address_is_not_a_blank_page() -> None:
    """With "Show technical details" off, a technical tab opened by its address says how to show it; its technical
    words stay hidden (the visible-text check reads every page with the switch off)."""
    css = (UI / "technical.js").read_text(encoding="utf-8")
    assert 'html.${HIDE_CLASS}:has([data-ds-tabs] [data-technical][aria-selected="true"]) [data-ds-tabs]::after' in css
    assert "Turn on \\\\201CShow technical details\\\\201D in Settings" in css


_CRUMB_SCRIPT: Final[str] = """
import { crumbs, journeyBack } from "%s";
const layout = process.argv[1];
globalThis.document = { documentElement: { getAttribute: (name) => (name === "data-ui-layout" ? layout : null) } };
const places = (html) => [...html.matchAll(/<(?:a href="([^"]*)"|span class="cur"[^>]*)>([^<]*)</g)].map((m) => [m[2], m[1] || null]);
console.log(JSON.stringify({
  trail: places(crumbs([{ label: "Results", href: "#/results", place: true }, { label: "Waiting for approval" }])),
  home: places(crumbs([{ label: "A goal" }])),
  back: journeyBack({ journey: { label: "Customer Lifecycle", href: "#/" } }).label,
}));
"""


def _crumbs(layout: str) -> dict[str, object]:
    node = skip_without_node()
    script = _CRUMB_SCRIPT % (UI / "dom.js").as_uri()
    done = subprocess.run(
        [node, "--input-type=module", "-e", script, layout],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(done.stdout)  # type: ignore[no-any-return]


def test_the_breadcrumb_names_the_places_the_campaigns_sidebar_has() -> None:
    shown = _crumbs("campaigns")
    assert shown["trail"] == [["Campaigns", "#/results"], ["Waiting for approval", None]]
    assert shown["home"] == [["Overview", "#/"], ["A goal", None]]
    assert shown["back"] == "Overview"


def test_the_classic_layout_keeps_its_own_names() -> None:
    shown = _crumbs("")
    assert shown["trail"] == [["Results", "#/results"], ["Waiting for approval", None]]
    assert shown["home"] == [["Home", "#/"], ["A goal", None]]
    assert shown["back"] == "Customer Lifecycle"


def test_a_malformed_address_part_is_read_as_typed_not_thrown() -> None:
    """`#/campaigns/%` made `decodeURIComponent` throw, so the page said "could not be loaded"; now it is "not found"."""
    node = skip_without_node()
    script = 'import { safeDecode } from "%s"; console.log(JSON.stringify([safeDecode("%%"), safeDecode("a%%20b"), safeDecode(undefined)]));'
    done = subprocess.run(
        [node, "--input-type=module", "-e", script % (UI / "dom.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(done.stdout) == ["%", "a b", ""]
