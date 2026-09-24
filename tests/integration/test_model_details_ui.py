"""The Model page's Details beeswarm: collapsed by default, loaded on demand, drawn without arithmetic.

The default view of the Model page stays the importance chart; the beeswarm sits in a closed
`<details>` and `shap_beeswarm.json` is fetched only when it is opened, so it is deliberately not in
`PAGE_ARTEFACTS`. The renderer is run under node against an artefact the engine itself built, so the
test checks the page draws exactly the dots the file carries. Node is not a Python dependency, so
that half skips cleanly where it is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Final

import numpy as np
import pandas as pd
import pytest

from api.main import UI_DIR
from engine.contracts import ARTEFACT_REGISTRY, ReasonMethod
from engine.stages.explain import (
    SHAP_BEESWARM_FILENAME,
    MeasuredContributions,
    build_beeswarm,
    unavailable_beeswarm,
)

pytestmark = pytest.mark.integration

NODE: Final[str | None] = shutil.which("node")

# `pages.js` imports `api.js`, which reads `window` at load; a stub is all node needs to import it.
SCRIPT: Final[str] = """
globalThis.window = { location: { origin: "http://localhost" } };
const pages = await import(%(pages)s);
let text = "";
for await (const chunk of process.stdin) text += chunk;
const { full, empty, uc, run } = JSON.parse(text);
process.stdout.write(JSON.stringify({
  artefact: pages.BEESWARM_ARTEFACT,
  full: pages.beeswarmHtml(full),
  empty: pages.beeswarmHtml(empty),
  page: pages.renderPage("model", uc, run, {}, "", {}),
}));
"""


def pages_code() -> str:
    return (UI_DIR / "pages.js").read_text(encoding="utf-8")


def test_the_beeswarm_is_not_part_of_the_default_view() -> None:
    block = re.search(r"export const PAGE_ARTEFACTS = \{(.*?)\n\};", pages_code(), re.DOTALL)
    assert block is not None
    assert SHAP_BEESWARM_FILENAME not in block.group(1)
    assert SHAP_BEESWARM_FILENAME in ARTEFACT_REGISTRY


def test_the_page_wires_the_details_loader_after_painting() -> None:
    app = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "bindPage(kind, app)" in app


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    if NODE is None:
        pytest.skip("node is not installed; the static half of this module still runs")
    base = np.random.default_rng(5).normal(size=60)
    rows = pd.DataFrame({"tenure": base, "plan": ["basic", "pro"] * 30})
    measured = MeasuredContributions(
        rows=rows,
        totals={"tenure": list(2 * base), "plan": list(0.5 * base)},
        method=ReasonMethod.TREE_SHAP,
    )
    full = build_beeswarm(measured, run_id="r_20260924_00000001", seed=1)
    payload = {
        "full": full.model_dump(mode="json"),
        "empty": unavailable_beeswarm("r_20260924_00000001").model_dump(mode="json"),
        "uc": {
            "id": "telco-churn",
            "name": "Telco",
            "marker": "p",
            "stars": "★",
            "pages": {"model": "Model"},
        },
        "run": {"run_id": "r_20260924_00000001", "mode": "train", "created_at": None},
    }
    done = subprocess.run(
        [NODE, "--input-type=module", "-e", SCRIPT % {"pages": json.dumps((UI_DIR / "pages.js").as_uri())}],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, str] = json.loads(done.stdout)
    return result


def test_the_page_fetches_the_registered_artefact(rendered: dict[str, str]) -> None:
    assert rendered["artefact"] == SHAP_BEESWARM_FILENAME


def test_details_is_collapsed_and_names_the_run_it_loads(rendered: dict[str, str]) -> None:
    page = rendered["page"]
    details = re.search(r"<details[^>]*>", page)
    assert details is not None
    assert 'class="more"' in details.group(0)
    assert " open" not in details.group(0)
    assert 'data-beeswarm="r_20260924_00000001"' in details.group(0)
    assert "<summary>" in page and "Details" in page
    # The default view is still there, ahead of the section.
    assert page.index("Feature importance") < page.index("<details")


def test_every_dot_in_the_file_is_drawn_once(rendered: dict[str, str]) -> None:
    html = rendered["full"]
    dots = len(re.findall(r"M[-\d.]+ [-\d.]+m", html))
    assert dots == 60 * 2
    # The numeric feature is coloured in steps of one hue; the category is grey throughout.
    assert 'class="bs-na"' in html
    assert re.search(r'<path class="bs-c[0-4]"', html)
    assert ">tenure</text>" in html and ">plan</text>" in html
    assert "SHAP value (impact on model output)" in html
    assert "TreeSHAP values, one dot per row" in html


def test_an_empty_artefact_says_why_rather_than_drawing_an_axis(rendered: dict[str, str]) -> None:
    html = rendered["empty"]
    assert "<svg" not in html
    assert "could not be measured" in html
