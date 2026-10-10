"""Editing by hand in Guided setup (`ui/modules/agent/edit.js`, Plan L M154, DEC-1541) in jsdom, against REAL bodies.

The node test (`production/ui/agent/edit/edit.test.mjs`, a sub-directory so Guided setup's own runner, which globs
`agent/*.test.mjs`, does not pick it up) opens "Edit by hand" on a messy Targeted Advertisement file and replays:

* **start** - the session as started;
* **rows** - the first ten rows (`POST .../rows`);
* **edit_everywhere** - a spelling changed everywhere (`map_values`), the session as answered;
* **edit_cell** - one cell corrected (`correct_cells`, found by the ID column), the session as answered;
* **edit_refused** - leaving out rows by the outcome column, refused with 422.
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
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "agent" / "edit"
USE_CASE: Final[str] = "targeted-advertisement"


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    frame = messy_frame()
    frame["city"] = ["Bombay" if i % 3 == 0 else "Pune" for i in range(len(frame))]
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        bodies["runs_empty"] = _ok(client.get("/runs", params={"use_case": USE_CASE}))
        bodies["models_empty"] = _ok(client.get("/models", params={"use_case": USE_CASE}))
        upload = bodies["upload"] = _ok(
            client.post(
                "/uploads",
                files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
                data={"use_case": USE_CASE, "mode": "train"},
            ),
            201,
        )
        base = f"/uploads/{upload['upload_id']}/agent-session"
        bodies["start"] = _ok(client.post(base, json={"use_case": USE_CASE}), 201)
        bodies["rows"] = _ok(client.post(f"{base}/rows", json={"offset": 0, "limit": 10}))
        bodies["edit_everywhere"] = _ok(
            client.post(
                f"{base}/edits",
                json={"kind": "map_values", "column": "city", "params": {"mapping": {"Bombay": "Mumbai"}}},
            )
        )
        key = bodies["rows"]["key_column"]
        first = bodies["rows"]["rows"][0]
        columns = bodies["rows"]["columns"]
        bodies["edit_cell"] = _ok(
            client.post(
                f"{base}/edits",
                json={
                    "kind": "correct_cells",
                    "column": "city",
                    "params": {
                        "cells": [
                            {
                                "key": first[columns.index(key)],
                                "old": first[columns.index("city")],
                                "new": "Agra",
                            }
                        ]
                    },
                },
            )
        )
        refused = client.post(
            f"{base}/edits",
            json={
                "kind": "remove_rows",
                "column": "converted_30d",
                "params": {"operator": "equals", "values": ["1"]},
            },
        )
        assert refused.status_code == 422, refused.text
        bodies["edit_refused"] = refused.json()
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def test_the_fixtures_are_the_world_the_screen_is_tested_in(tmp_path: Path, config_root: Path) -> None:
    out = write_fixtures(tmp_path, config_root)
    rows = json.loads((out / "rows.json").read_text(encoding="utf-8"))
    assert rows["key_column"] == "customer_id" and len(rows["rows"]) == 10
    session = json.loads((out / "edit_everywhere.json").read_text(encoding="utf-8"))["session"]
    assert any(p.get("source") == "person" and p["state"] == "accepted" for p in session["proposals"])


def test_editing_by_hand_in_jsdom(tmp_path: Path, config_root: Path) -> None:
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
