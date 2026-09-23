"""The uplift UI module (`ui/modules/uplift/`), checked without a browser.

Two layers. The node tests (`uplift_ui.test.mjs`, `uplift_ui_wiring.test.mjs`) render every uplift
screen from artefact fixtures and drive the module's routes through `ui/modules/router.js` with a
stubbed `fetch`; they need nothing but `node`, and are skipped - with the reason printed - where
there is none. The static checks below need nothing at all: they pin what a regression could break
silently in a browser too - a file that no longer parses, an import that no longer resolves, an
endpoint outside the Stage F contract, a prototype sample value, a placeholder other than the em
dash, and the one line in each shared file's PHASE-3B block.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Final

import pytest

REPO: Final[Path] = Path(__file__).resolve().parents[3]
UI_DIR: Final[Path] = REPO / "ui"
MODULE_DIR: Final[Path] = UI_DIR / "modules" / "uplift"
HERE: Final[Path] = Path(__file__).resolve().parent
NODE_TESTS: Final[tuple[str, ...]] = ("uplift_ui.test.mjs", "uplift_ui_wiring.test.mjs")

EXPECTED_FILES: Final[frozenset[str]] = frozenset(
    {"api.js", "charts.js", "controller.js", "format.js", "index.js", "styles.js", "views.js"}
)

CONTRACT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/uploads/{}/treatment-candidates",
        "/uplift/runs",
        "/runs/{}/uplift/{}",
        "/runs/{}/campaign-results",
        "/runs/{}/uplift/ope",
    }
)
"""Every uplift endpoint of UPLIFT_INTERFACES.md (Stage F, "API it talks to"), parameters as `{}`."""

SAMPLE_VALUES: Final[tuple[str, ...]] = (
    "Illustrative",
    "184K",
    "3.2x",
    "0.84",
    "2.4M",
    "₹",
    "converted_30d",
    "snapshot_date",
    "reactivated_90d",
    "Uplift forest",
)
"""Figures and column names the prototype filled its screens with (tests/integration/test_ui.py)."""

PLACEHOLDERS: Final[tuple[str, ...]] = ('"N/A"', "'N/A'", '"TBD"', '">--<"', '"n/a"')

IMPORT_PATH: Final[re.Pattern[str]] = re.compile(r"""from\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""")
API_PATH: Final[re.Pattern[str]] = re.compile(r"[`\"'](/[^`\"'\s]*)[`\"']")
LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"(?<![:\"'`])//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)

NODE: Final[str | None] = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed; the JS checks need it")


def module_files() -> list[Path]:
    return sorted(MODULE_DIR.glob("*.js"))


def code_of(path: Path) -> str:
    """A module with its comments removed, so prose is not mistaken for code."""
    text = path.read_text(encoding="utf-8")
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def block(text: str, start: str, end: str) -> str:
    """The lines between a shared file's PHASE-3B markers."""
    begin = text.index(start) + len(start)
    return text[begin : text.index(end, begin)]


# ---------------------------------------------------------------------------
# node: parse, render, route
# ---------------------------------------------------------------------------
@needs_node
@pytest.mark.parametrize("name", sorted(EXPECTED_FILES))
def test_every_module_file_parses(name: str) -> None:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--check", str(MODULE_DIR / name)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr


@needs_node
@pytest.mark.parametrize("name", NODE_TESTS)
def test_node_suite_passes(name: str) -> None:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--test", str(HERE / name)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout


# ---------------------------------------------------------------------------
# static
# ---------------------------------------------------------------------------
def test_the_module_holds_exactly_its_files() -> None:
    assert {p.name for p in module_files()} == EXPECTED_FILES


def test_every_import_resolves_to_a_file_in_ui() -> None:
    for path in module_files():
        base = PurePosixPath(path.relative_to(UI_DIR).as_posix()).parent
        for target in IMPORT_PATH.findall(code_of(path)):
            resolved = posixpath.normpath(str(base / target))
            assert (UI_DIR / resolved).is_file(), f"{path.name} imports {resolved}"


def test_every_uplift_endpoint_is_in_the_stage_f_contract() -> None:
    # `run(runId)` is api.js's helper for the `/runs/{run_id}` prefix: spelled out, so each path is whole.
    code = code_of(MODULE_DIR / "api.js").replace("`${run(runId)}", "`/runs/${runId}")
    called = {re.sub(r"\$\{[^}]+\}", "{}", raw).split("?")[0] for raw in API_PATH.findall(code)}
    called = {path for path in called if path != "/"} - {"/runs/{}"}
    assert called, "no API path was found in uplift/api.js"
    unknown = sorted(path for path in called if path not in CONTRACT_PATHS)
    assert not unknown, f"uplift/api.js calls endpoints outside the contract: {unknown}"


def test_no_illustrative_value_from_the_prototype() -> None:
    leaked = [
        (path.name, value)
        for path in module_files()
        for value in SAMPLE_VALUES
        if value in path.read_text(encoding="utf-8")
    ]
    assert not leaked, f"prototype sample values reached the uplift module: {leaked}"


def test_the_em_dash_is_the_only_placeholder() -> None:
    for path in module_files():
        text = path.read_text(encoding="utf-8")
        for placeholder in PLACEHOLDERS:
            assert placeholder not in text, f"{path.name} uses {placeholder} instead of the em dash"


def test_the_not_causal_note_matches_the_engine() -> None:
    from engine.uplift.contracts import NOT_CAUSAL_NOTE

    views = (MODULE_DIR / "views.js").read_text(encoding="utf-8")
    match = re.search(r"export const NOT_CAUSAL_NOTE =\s*((?:\"[^\"]*\"\s*\+?\s*)+);", views)
    assert match is not None
    assert "".join(re.findall(r"\"([^\"]*)\"", match.group(1))) == NOT_CAUSAL_NOTE


def test_the_shared_files_load_the_module_inside_the_phase_3b_blocks_only() -> None:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    inside = block(
        html,
        "<!-- ---- PHASE-3B (uplift) — append only below this line ---- -->",
        "<!-- ---- END PHASE-3B ---- -->",
    )
    assert inside.strip() == '<script type="module" src="./modules/uplift/index.js"></script>'
    assert html.count("./modules/uplift/index.js") == 1
    router = (UI_DIR / "modules" / "router.js").read_text(encoding="utf-8")
    inside_router = block(
        router, "// ---- PHASE-3B (uplift) — append only below this line ----", "// ---- END PHASE-3B ----"
    )
    assert "import" not in LINE_COMMENT.sub("", inside_router), "router.js must not import the module back"


def test_the_entry_point_documents_how_a_user_reaches_it() -> None:
    head = (MODULE_DIR / "index.js").read_text(encoding="utf-8").split("\nimport ", 1)[0]
    assert "HOW A USER REACHES THE UPLIFT SCREENS" in head
    for route in ("#/uplift", "#/uplift/<uc>", "#/campaign/<uc>/<run>"):
        assert route in head, route
