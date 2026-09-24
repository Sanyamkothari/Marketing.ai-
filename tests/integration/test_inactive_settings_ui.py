"""Plan A ruling D2 on the wire: the served schema, the generated form, and `POST /runs`.

The Setup screen's advanced settings are generated from `GET /use-cases/{id}.advanced_settings`
by `ui/settings.js`, which names no setting (plan §9.2). So "the six inactive `features` settings
are rendered disabled under Coming later" is only true if the schema the route serves marks them
*and* the module turns that mark into a disabled control. The first half is a JSON assertion; the
second is run here in node against the real module and the schema the route really served, so
neither half can drift without this failing. Skipped when node is not installed (failed instead
under `REQUIRE_JSDOM=1`, as CI sets it: `tests/fixtures/node.py`).

The API half pins the behaviour D2 keeps: an override of an inactive setting is still accepted
and recorded on the run, even though the screen itself never sends one.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import UI_DIR, create_app
from engine.config import ADVISORY_NOTE
from engine.contracts import RunRecord
from engine.storage import LocalStorage, run_key
from tests.fixtures.node import skip_without_node
from tests.integration.test_api_runs import (
    install_m2_job_stub,
    install_validate_stub,
    run_body,
    upload,
)
from tests.integration.test_api_uploads import DEMO_ID, install_ingest_stub
from tests.unit.test_inactive_settings import INACTIVE, MOVED

pytestmark = pytest.mark.integration

RENDER_SCRIPT: Final[str] = """
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [settingsPath] = process.argv.slice(2);
const settings = await import(pathToFileURL(settingsPath).href);
const { schema, base, moved } = JSON.parse(readFileSync(0, "utf8"));

const values = settings.initialValues(schema, base);
const fields = {};
for (const stage of schema.stages) {
  for (const field of stage.fields) fields[field.path] = settings.fieldHtml(field, values, {});
}
// Move every field named in `moved` the way a script or a stale value could, bypassing the control.
for (const [path, value] of Object.entries(moved)) settings.writePath(values, path, value);
process.stdout.write(
  JSON.stringify({
    fields,
    stages: settings.stagesHtml(schema, settings.initialValues(schema, base), {}),
    overrides: settings.overridesFrom(schema, values),
  }),
);
"""


@pytest.fixture
def client(config_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_ingest_stub(monkeypatch)
    install_validate_stub(monkeypatch)
    install_m2_job_stub(monkeypatch)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as test_client:
        yield test_client


def served_use_case(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/use-cases/{DEMO_ID}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def render(schema: dict[str, Any], base: dict[str, Any], moved: dict[str, Any], tmp_path: Path) -> Any:
    node = skip_without_node()
    script = tmp_path / "render.mjs"
    script.write_text(RENDER_SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [node, str(script), str(UI_DIR / "settings.js")],
        input=json.dumps({"schema": schema, "base": base, "moved": moved}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# The schema the route serves
# ---------------------------------------------------------------------------
def test_the_route_serves_the_six_as_advisory_under_coming_later(client: TestClient) -> None:
    schema = served_use_case(client)["advanced_settings"]
    fields = {field["path"]: field for stage in schema["stages"] for field in stage["fields"]}
    for path in INACTIVE:
        assert fields[path]["advisory"] is True, path
        assert fields[path]["help"] == ADVISORY_NOTE, path
    assert fields["model_search.tuning_trials"]["advisory"] is False


# ---------------------------------------------------------------------------
# The form `ui/settings.js` generates from it
# ---------------------------------------------------------------------------
def test_the_generated_form_disables_exactly_the_advisory_controls(
    client: TestClient, tmp_path: Path
) -> None:
    body = served_use_case(client)
    schema = body["advanced_settings"]
    rendered = render(schema, body["config"], {}, tmp_path)
    advisory = {field["path"] for stage in schema["stages"] for field in stage["fields"] if field["advisory"]}
    assert set(INACTIVE) <= advisory
    for path, html in rendered["fields"].items():
        if not html:
            continue
        if path in advisory:
            assert html.startswith('<div class="advisory '), path
            assert " disabled" in html, path
            assert "Coming later" in html, path
        else:
            assert "advisory" not in html, path
            assert "<input disabled" not in html and "<select disabled" not in html, path


def test_a_stage_of_only_inactive_settings_says_so_while_folded(client: TestClient, tmp_path: Path) -> None:
    """Folded, the feature-engineering stage shows only its summary line; the note leads it."""
    body = served_use_case(client)
    rendered = render(body["advanced_settings"], body["config"], {}, tmp_path)
    stages = rendered["stages"]
    feature_stage = stages.split('data-stage="feature_engineering"', 1)[1].split("</summary>", 1)[0]
    assert '<div class="ss">Coming later' in feature_stage
    model_stage = stages.split('data-stage="model_search"', 1)[1].split("</summary>", 1)[0]
    assert "Coming later" not in model_stage


def test_the_form_never_sends_an_inactive_setting_as_an_override(client: TestClient, tmp_path: Path) -> None:
    body = served_use_case(client)
    moved = {**MOVED, "model_search.tuning_trials": 9}
    rendered = render(body["advanced_settings"], body["config"], moved, tmp_path)
    assert rendered["overrides"] == {"model_search.tuning_trials": 9}


# ---------------------------------------------------------------------------
# POST /runs: an override of an inactive setting is still accepted and recorded
# ---------------------------------------------------------------------------
def test_an_inactive_override_is_accepted_and_recorded_on_the_run(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/runs", json=run_body(upload(client), overrides=MOVED))
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    storage = LocalStorage(tmp_path / "data")
    record = storage.read_model(run_key(run_id, "run.json"), RunRecord)
    for path, value in MOVED.items():
        block, _, field = path.partition(".")
        assert record.overrides[block][field] == value, path
    stored = json.loads(storage.read_text(run_key(run_id, "run_config.json")))
    assert stored["config"]["features"]["max_features"] == 300
