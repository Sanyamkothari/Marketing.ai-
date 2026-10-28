"""Data Doctor's intake notes (`ui/modules/intake/notes.js`, Plan L M150, DEC-1505) in jsdom, against REAL API responses.

The node test (`production/ui/intake/notes.test.mjs`, sharing that directory's jsdom install) draws the notes
box for a report-style workbook. Everything it draws from was answered by the app below:

* **upload_report** - the workbook as intake reads it: the "Customers" sheet, its title rows and its
  total and note rows left out, and a "Summary" sheet it could read instead;
* **upload_asis** - the same file uploaded again with `intake=false` ("Read it as it is instead");
* **upload_sheet** - the same file with `sheet=Summary` ("Read the sheet "Summary" instead");
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.fixtures.node import skip_without_jsdom
from tests.unit.intake.bench import CASES, write

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "intake"
USE_CASE: Final[str] = "targeted-advertisement"
XLSX: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    workbook = write(next(case for case in CASES if case.name == "26_xlsx_report"), root)
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        bodies["runs_empty"] = _ok(client.get("/runs", params={"use_case": USE_CASE}))
        bodies["models_empty"] = _ok(client.get("/models", params={"use_case": USE_CASE}))
        forms = {"report": {}, "asis": {"intake": "false"}, "sheet": {"sheet": "Summary"}}
        ids: dict[str, str] = {}
        for name, form in forms.items():
            upload = bodies[f"upload_{name}"] = _ok(
                client.post(
                    "/uploads",
                    files={"file": ("customers.xlsx", workbook.read_bytes(), XLSX)},
                    data={"use_case": USE_CASE, "mode": "train", **form},
                ),
                201,
            )
            ids[name] = upload["upload_id"]
        bodies["ids"] = ids
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def test_the_fixtures_are_the_world_the_screen_is_tested_in(tmp_path: Path, config_root: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root)

    def read(name: str) -> Any:
        return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))

    report = read("upload_report")["intake"]
    assert report["applied"] and report["sheet"] == "Customers"
    assert any('"Summary"' in note for note in report["notes"])
    asis = read("upload_asis")["intake"]
    assert asis["turned_off"] and not any("Skipped" in note for note in asis["notes"])
    assert read("upload_sheet")["intake"]["sheet"] == "Summary"


def test_intake_notes_in_jsdom(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
