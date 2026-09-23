"""How the onboarding panel reaches the Setup form, the header and the Data page (Plan A M35).

`tests/unit/test_onboarding_ui.py` holds the panel's three original modules to the API. This module
holds the wiring M35 added around them - the entry point, the client picker, the setup source, the
two seams in `ui/modules/router.js`, and the three Phase 1 files that now ask those seams for help -
to the same standard: every field a screen reads, every body it sends and every hand-off between two
modules is pinned to something outside the file doing it (a pydantic model's own fields, the other
module's own code), never to a transcript of itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from pydantic import BaseModel

from api.routes.datasets import ReplayRequest, ReplayResponse
from api.schemas import RunRequest
from engine.contracts import ModelVersion, RunRecord
from engine.onboarding.datasets import Lineage, LineageNode
from engine.onboarding.replay import ReplayedSource, UnmatchedSource
from engine.onboarding.specs import ClientRecord, DatasetManifest
from tests.unit.test_onboarding_ui import object_literal_keys

UI: Final[Path] = Path(__file__).resolve().parents[3] / "ui"
ONBOARDING: Final[Path] = UI / "modules" / "onboarding"
NEW_MODULES: Final[tuple[str, ...]] = ("index.js", "clients.js", "setup.js")

LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
PLACEHOLDERS: Final[tuple[str, ...]] = ('"N/A"', "'N/A'", '"TBD"', '">--<"', '"n/a"')
COMMA_GROUPED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
IMPORT: Final[re.Pattern[str]] = re.compile(r"""from\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""")

PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"datasetId", "primaryKey", "target", "problemType", "timeColumn", "manifest", "sample"}
)
"""What "Use this dataset" hands the Setup form (`panel.js`), before `setup.js` adds `clientId`."""


def code(path: Path) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", path.read_text(encoding="utf-8")))


def fields(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(model.model_fields)


# ---------------------------------------------------------------------------
# The module reaches the page, and only through the registry
# ---------------------------------------------------------------------------
def test_the_phase_2_block_of_index_html_loads_the_onboarding_entry_point() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    block = html[html.index("PHASE-2 (onboarding) — append only below") : html.index("END PHASE-2")]
    assert '<script type="module" src="./modules/onboarding/index.js"></script>' in block


def test_the_entry_point_registers_a_setup_source_and_a_header_tool() -> None:
    text = code(ONBOARDING / "index.js")
    assert 'from "../router.js"' in text
    assert "registerSetupSource(" in text and "registerHeaderTool(" in text
    router = code(UI / "modules" / "router.js")
    for name in (
        "registerSetupSource",
        "setupSource",
        "registerHeaderTool",
        "headerToolHtml",
        "MODULES_CHANGED",
    ):
        assert re.search(rf"export (const|function) {name}\b", router), name


def test_the_phase_1_files_ask_the_registry_and_never_import_onboarding() -> None:
    """`usecase.js`, `dom.js` and `app.js` know the seams, never the module behind them."""
    for name, seam in (
        ("usecase.js", "setupSource"),
        ("dom.js", "headerToolHtml"),
        ("app.js", "MODULES_CHANGED"),
    ):
        text = code(UI / name)
        assert "modules/onboarding" not in text, name
        assert re.search(rf'import \{{[^}}]*\b{seam}\b[^}}]*\}} from "\./modules/router\.js"', text), name


def test_the_setup_source_implements_what_the_registry_requires() -> None:
    index = code(ONBOARDING / "index.js")
    for key in ("name", "card", "mount", "context"):
        assert re.search(rf"registerSetupSource\(\{{[^}}]*\b{key}\b", index), key
    setup = code(ONBOARDING / "setup.js")
    for name in ("setupCard", "mountSetup", "setupContext"):
        assert f"export function {name}(" in setup, name


@pytest.mark.parametrize("name", NEW_MODULES)
def test_every_relative_import_of_a_new_module_resolves(name: str) -> None:
    for target in IMPORT.findall(code(ONBOARDING / name)):
        assert (ONBOARDING / target).resolve().is_file(), f"{name} imports {target}"


@pytest.mark.parametrize("name", NEW_MODULES)
def test_nothing_in_a_new_module_is_a_stand_in_value(name: str) -> None:
    text = code(ONBOARDING / name)
    assert not COMMA_GROUPED_NUMBER.findall(text), name
    for placeholder in PLACEHOLDERS:
        assert placeholder not in (ONBOARDING / name).read_text(encoding="utf-8"), (name, placeholder)


# ---------------------------------------------------------------------------
# The hand-off: panel -> setup source -> Setup form -> POST /runs
# ---------------------------------------------------------------------------
def test_use_this_dataset_hands_over_exactly_the_documented_payload() -> None:
    panel = code(ONBOARDING / "panel.js")
    sent = object_literal_keys(panel[panel.index("async function useDataset(") :], "onDatasetReady(")
    assert sent == PAYLOAD_KEYS
    assert "clientId" in code(ONBOARDING / "setup.js")


def test_the_setup_form_reads_every_payload_key_it_is_handed() -> None:
    usecase = code(UI / "usecase.js")
    for key in (*PAYLOAD_KEYS, "clientId"):
        assert re.search(rf"\.{key}\b", usecase), key


def test_run_posts_the_dataset_and_its_client_with_the_run_requests_own_field_names() -> None:
    usecase = code(UI / "usecase.js")
    assert "body.dataset_id = s.dataset.datasetId" in usecase
    assert "body.client_id = s.dataset.clientId" in usecase
    assert {"dataset_id", "client_id", "upload_id", "primary_key"} <= fields(RunRequest)


# ---------------------------------------------------------------------------
# Every body sent and every field read is the model's own
# ---------------------------------------------------------------------------
def test_the_replay_body_is_exactly_the_request_models_fields() -> None:
    sent = object_literal_keys(code(ONBOARDING / "api.js"), "export const replayOnboardingSpec =")
    assert sent == fields(ReplayRequest)


READS: Final[dict[type[BaseModel], tuple[str, ...]]] = {
    ReplayResponse: ("spec_id", "sources", "unmatched", "unused_source_ids"),
    ReplayedSource: ("source_id", "file_name", "role", "mapping_id", "missing_columns", "message"),
    UnmatchedSource: ("file_name", "message"),
    ClientRecord: ("client_id", "name", "industry"),
    ModelVersion: ("model_id", "run_id"),
    RunRecord: ("dataset_id",),
    DatasetManifest: ("spec_id", "client_id", "n_rows", "columns", "primary_key", "target"),
    Lineage: ("sources", "mappings", "spec", "dataset"),
    LineageNode: ("id", "label", "detail"),
}
"""Every wire field the new code reaches for, by the model that sends it."""


def test_every_field_the_new_code_reads_is_a_field_of_its_model() -> None:
    for model, names in READS.items():
        unknown = sorted(set(names) - fields(model))
        assert not unknown, f"{model.__name__} has no {unknown}"


def test_every_field_listed_here_is_read_somewhere() -> None:
    sources = "".join(
        code(path)
        for path in (
            *(ONBOARDING / name for name in (*NEW_MODULES, "panel.js", "steps.js")),
            UI / "pages.js",
            UI / "usecase.js",
        )
    )
    for model, names in READS.items():
        for name in names:
            assert re.search(rf"\b{name}\b", sources), f"{model.__name__}.{name} is read nowhere"


def test_the_lineage_block_draws_the_five_layers_in_order() -> None:
    pages = code(UI / "pages.js")
    titles = re.findall(r'lineageColumn\("([A-Za-z]+)"', pages)
    assert titles == ["Sources", "Mapping", "Recipe", "Dataset", "Run"]


# ---------------------------------------------------------------------------
# "Use this dataset" makes a periodic dataset's split time-based
# ---------------------------------------------------------------------------
def test_the_time_column_field_is_shown_under_the_time_based_split() -> None:
    """`adoptTimeSplit` reads the split type it writes from the time column's `visible_when`.

    Filling the time column alone left `split.type` at `random_stratified`, which never reads it:
    a periodic dataset was split at random by row. The path and the value the Setup form writes
    are therefore the schema's own, and this pins them to the engine's split enum.
    """
    from engine.config import ColumnSource, SplitType, advanced_settings_schema, load_use_case

    schema = advanced_settings_schema(load_use_case("telco-churn"))
    fields_ = [field for stage in schema.stages for field in stage.fields]
    time_fields = [field for field in fields_ if field.column_source is ColumnSource.TIME_LIKE]
    assert len(time_fields) == 1
    when = time_fields[0].visible_when
    assert when is not None
    assert when.path == "split.type"
    assert when.equals == SplitType.TIME_BASED.value
    assert when.path in {field.path for field in fields_}


def test_use_this_dataset_writes_the_split_type_the_time_column_is_shown_under() -> None:
    usecase = code(UI / "usecase.js")
    adopt = usecase[usecase.index("function adoptDataset(") :]
    adopt = adopt[: adopt.index("\n  }\n")]
    assert "adoptTimeSplit(dataset.timeColumn)" in adopt
    assert "fillTimeColumn" not in adopt, "a periodic dataset's time column must overwrite a stale one"
    split = usecase[usecase.index("function adoptTimeSplit(") :]
    split = split[: split.index("\n  }\n")]
    assert "[field.visible_when.path, field.visible_when.equals]" in split
    assert "written.push([field.path, column || null])" in split
    assert 's.mode === "train"' in split
    upload = usecase[usecase.index("function adoptUpload(") :]
    assert "releaseTimeSplit()" in upload[: upload.index("\n  }\n")]
