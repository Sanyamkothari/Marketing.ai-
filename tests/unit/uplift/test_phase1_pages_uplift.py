"""Phase 1's Data, Model and Output pages on an uplift run (M53), checked without a browser.

`phase1_pages_uplift.test.mjs` renders `ui/pages.js` from uplift artefact fixtures under node - the
Qini curve and AUUC instead of ROC and lift, treatment and control with the randomness check, the
drift card - and is skipped, with the reason printed, where there is no node. The static checks
below need nothing: every artefact an uplift page reads is one a route will serve, and none is an
artefact an uplift run never writes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

from engine.contracts import ARTEFACT_REGISTRY, TABULAR_SCHEMAS
from engine.uplift.contracts import UPLIFT_ARTEFACTS
from tests.fixtures.node import skip_without_node

REPO: Final[Path] = Path(__file__).resolve().parents[3]
HERE: Final[Path] = Path(__file__).resolve().parent
PAGES: Final[Path] = REPO / "ui" / "pages.js"
NODE: Final[str | None] = shutil.which("node")

BLOCK: Final[re.Pattern[str]] = re.compile(r"export const UPLIFT_PAGE_ARTEFACTS = \{(.*?)\n\};", re.DOTALL)
ENTRY: Final[re.Pattern[str]] = re.compile(r"(\w+):\s*\[(.*?)\]", re.DOTALL)
NEVER_WRITTEN: Final[frozenset[str]] = frozenset({"prepare.json", "drift.json", "decile_lift.json"})
"""Artefacts an uplift run never writes (DEC-647): asking for them only logs 404s in the browser."""


def uplift_page_artefacts() -> dict[str, list[str]]:
    block = BLOCK.search(PAGES.read_text(encoding="utf-8"))
    assert block is not None, "ui/pages.js has no UPLIFT_PAGE_ARTEFACTS"
    return {key: re.findall(r'"([^"]+)"', body) for key, body in ENTRY.findall(block.group(1))}


def test_the_uplift_pages_read_only_registered_artefacts() -> None:
    known = set(ARTEFACT_REGISTRY) | set(TABULAR_SCHEMAS) | set(UPLIFT_ARTEFACTS)
    pages = uplift_page_artefacts()
    assert set(pages) == {"data", "model", "output"}
    for page, names in pages.items():
        assert names, page
        assert not [name for name in names if name not in known], page
        assert not NEVER_WRITTEN & set(names), page


def test_the_uplift_pages_read_the_uplift_artefacts_m53_names() -> None:
    pages = uplift_page_artefacts()
    assert {"uplift_validation.json", "uplift_drift.json"} <= set(pages["data"])
    assert {"uplift_evaluation.json", "qini_curve.json"} <= set(pages["model"])
    assert {"scoring_summary.json", "segments.json", "uplift_drift.json"} <= set(pages["output"])


def test_node_suite_passes() -> None:
    node = skip_without_node()  # REQUIRE_JSDOM=1 (CI) turns a missing node into a failure
    result = subprocess.run(
        [node, "--test", str(HERE / "phase1_pages_uplift.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
