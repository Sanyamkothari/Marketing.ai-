"""The "Ask your data" panel (`ui/modules/agent/ask.js`, Plan I DEC-1255) in jsdom, against REAL API responses.

The node test (`production/ui/agent/ask/ask.test.mjs`, sharing that directory's jsdom install; a sub-directory
so Guided setup's runner, which globs `agent/*.test.mjs` with its own fixtures, does not pick it up) draws the
panel on a run's Data page and replays what the app below answered:

* **ask_empty** - `GET /uploads/{id}/ask` before the first question;
* **ask_rate** - the question "Show 'converted_30d' by 'region'", which the test model answers with `rate_by`:
  a reply and one chart;
* **ask_suppressed** - "Show 'converted_30d' by 'monthly_spend'" on a file with three empty spend cells: a
  range chart whose `(empty)` group, and the smallest range with it, show no figures;
* **ask_off** - the same `GET` from an app with no AI service connected (`chat.available: false`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.settings import Settings
from tests.fixtures.node import skip_without_jsdom
from tests.integration.agent.test_ask_api import ask_frame, upload

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "agent" / "ask"
USE_CASE: Final[str] = "targeted-advertisement"


def _ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        upload_id = upload(client)
        bodies["ask_empty"] = _ok(client.get(f"/uploads/{upload_id}/ask"))
        bodies["ask_rate"] = _ok(
            client.post(
                f"/uploads/{upload_id}/ask/messages", json={"text": "Show 'converted_30d' by 'region'"}
            )
        )
        gappy = ask_frame()
        gappy.loc[[0, 1, 2], "monthly_spend"] = None
        gappy_id = upload(client, gappy)
        bodies["ask_suppressed"] = _ok(
            client.post(
                f"/uploads/{gappy_id}/ask/messages", json={"text": "Show 'converted_30d' by 'monthly_spend'"}
            )
        )
        bodies["ids"] = {"upload": upload_id, "gappy": gappy_id}
    settings = Settings(connections_key=SecretStr("F" * 40), allow_fake_ai=False)
    with TestClient(create_app(config_root=config_root, data_dir=root / "off", settings=settings)) as off:
        off_id = upload(off)
        bodies["ask_off"] = _ok(off.get(f"/uploads/{off_id}/ask"))
        bodies["ids"]["off"] = off_id
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_the_fixtures_are_the_world_the_panel_is_tested_in(tmp_path: Path, config_root: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root)
    assert _read(out, "ask_empty")["session"] is None
    assert _read(out, "ask_empty")["chat"]["available"] is True
    rate = _read(out, "ask_rate")
    assert [c["function"] for c in rate["charts"]] == ["positive_rate"]
    assert len(rate["charts"][0]["bars"]) == 4
    suppressed = _read(out, "ask_suppressed")["charts"][0]
    assert [b["label"] for b in suppressed["bars"] if b["suppressed"]][-1] == "(empty)"
    assert sum(1 for b in suppressed["bars"] if b["suppressed"]) == 2
    assert _read(out, "ask_off")["chat"]["available"] is False


def test_the_ask_panel_in_jsdom(tmp_path: Path, config_root: Path) -> None:
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
