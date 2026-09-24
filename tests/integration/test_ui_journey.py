"""The root every use-case screen goes back to is the journey of the industry that lists it.

Once the overview opens more than one industry (DEC-085, ruling D3), a hard-coded "Customer
Lifecycle" pointing at `#/` sends a Banking user to the Telecom journey under Telecom's label. The
industry file's `journey_label` is documented as the "overview subtitle and breadcrumb root", so the
screens take both the label and the route from `GET /industries` through `journeyFor`
(`ui/overview.js`) and render them with `backLink` / `journeyCrumb` (`ui/dom.js`).

Two layers are pinned here:

* statically, no use-case screen names a journey label or links the bare `#/` as its root any more;
* behaviourally, `journeyFor` and the two renderers are run under node against this app's own
  `GET /industries` response, and every expectation is read from the industry files, not typed
  here. Node is not a Python dependency, so that half skips cleanly where it is absent (and fails
  under `REQUIRE_JSDOM=1`, as CI sets it).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

from api.main import UI_DIR, create_app
from engine.config import DEFAULT_INDUSTRY, list_industries, load_industry
from tests.fixtures.node import skip_without_node

pytestmark = pytest.mark.integration

USE_CASE_SCREENS: Final[tuple[str, ...]] = (
    "usecase.js",
    "pages.js",
    "modules/generative/assistant.js",
    "modules/generative/rca.js",
    "modules/generative/copy.js",
)
"""Every screen that belongs to one use case, and so to the journey that lists it."""

HOME: Final[str] = '<a href="#/">Home</a>'
SEP: Final[str] = '<span class="sep" aria-hidden="true">›</span>'
"""v1 (docs/ui/FOUNDATION.md): every breadcrumb starts at Home, then the journey."""


def _trail(href: str, label: str) -> str:
    return f'{HOME}{SEP}<a href="{href}">{label}</a>'


def _nav(inner: str) -> str:
    return f'<nav class="crumbs" aria-label="Breadcrumb">{inner}</nav>'


SCRIPT: Final[str] = """
import { journeyFor } from %(overview)s;
import { backLink, journeyCrumb } from %(dom)s;
let text = "";
for await (const chunk of process.stdin) text += chunk;
const { payload, ids } = JSON.parse(text);
const render = (journey) => ({
  journey,
  back: backLink(journey ? { journey } : null),
  crumb: journeyCrumb(journey ? { journey } : null),
});
const out = Object.fromEntries(ids.map((id) => [id, render(journeyFor(payload, id))]));
out[""] = render(journeyFor({ industries: [], default_industry: null }, ids[0]));
process.stdout.write(JSON.stringify(out));
"""


def _cards(industry_id: str) -> tuple[str, ...]:
    """Every card an industry file lists, available and planned."""
    return tuple(ref.id for stage in load_industry(industry_id).stages for ref in stage.use_cases)


def _owner(use_case_id: str) -> str:
    """The industry a use case's screens belong to: the default when it lists it, else the first."""
    ordered = sorted(list_industries(), key=lambda i: (i != DEFAULT_INDUSTRY, i))
    return next((i for i in ordered if use_case_id in _cards(i)), DEFAULT_INDUSTRY)


def _href(industry_id: str) -> str:
    return "#/" if industry_id == DEFAULT_INDUSTRY else f"#/industry/{industry_id}"


@pytest.mark.parametrize("name", USE_CASE_SCREENS)
def test_no_use_case_screen_names_its_journey(name: str) -> None:
    text = (UI_DIR / name).read_text(encoding="utf-8")
    labels = {load_industry(i).journey_label for i in list_industries()}
    assert not [label for label in labels if label in text], name
    assert '<a href="#/">' not in text and '<a class="back" href="#/">' not in text, name
    assert "backLink(uc)" in text or "journeyCrumb(uc)" in text, name


@pytest.fixture(scope="module")
def journeys(config_root: Path) -> dict[str, dict[str, object]]:
    node = skip_without_node()  # the static half of this module runs either way
    with TestClient(create_app(config_root=config_root)) as client:
        payload = client.get("/industries").json()
    ids = sorted({card for i in list_industries() for card in _cards(i)} | {"no-such-use-case"})
    script = SCRIPT % {
        "overview": json.dumps((UI_DIR / "overview.js").as_uri()),
        "dom": json.dumps((UI_DIR / "dom.js").as_uri()),
    }
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=json.dumps({"payload": payload, "ids": ids}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, dict[str, object]] = json.loads(done.stdout)
    return result


def test_every_card_goes_back_to_the_journey_that_lists_it(journeys: dict[str, dict[str, object]]) -> None:
    assert len(list_industries()) > 1, "D3 only matters with more than one industry"
    cards = {card for i in list_industries() for card in _cards(i)}
    for card in cards:
        owner = _owner(card)
        assert journeys[card]["journey"] == {
            "href": _href(owner),
            "label": load_industry(owner).journey_label,
        }, card


def test_a_telecom_screen_reads_exactly_as_it_did(journeys: dict[str, dict[str, object]]) -> None:
    """The default journey keeps the bare `#/` and its own label, after the Home root (v1)."""
    label = load_industry(DEFAULT_INDUSTRY).journey_label
    for card in _cards(DEFAULT_INDUSTRY):
        assert journeys[card]["back"] == _nav(_trail("#/", label)), card
        assert journeys[card]["crumb"] == _trail("#/", label), card


def test_another_industrys_screen_names_and_opens_that_industry(
    journeys: dict[str, dict[str, object]],
) -> None:
    others = [i for i in list_industries() if i != DEFAULT_INDUSTRY]
    assert others
    for industry_id in others:
        label = load_industry(industry_id).journey_label
        for card in (c for c in _cards(industry_id) if _owner(c) == industry_id):
            href = f"#/industry/{industry_id}"
            assert journeys[card]["back"] == _nav(_trail(href, label)), card
            assert journeys[card]["crumb"] == _trail(href, label), card


def test_a_use_case_no_file_lists_goes_back_to_the_default_journey(
    journeys: dict[str, dict[str, object]],
) -> None:
    assert journeys["no-such-use-case"]["journey"] == {
        "href": "#/",
        "label": load_industry(DEFAULT_INDUSTRY).journey_label,
    }


def test_with_no_industry_the_root_is_the_bare_overview(journeys: dict[str, dict[str, object]]) -> None:
    """With no journey the breadcrumb is its root alone: Home, the bare overview (v1)."""
    assert journeys[""]["journey"] is None
    assert journeys[""]["back"] == _nav(HOME)
    assert journeys[""]["crumb"] == HOME
