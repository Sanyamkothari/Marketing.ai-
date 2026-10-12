"""The one Data Doctor screen in jsdom, against REAL API answers (Plan O M247, DEC-1817).

The node suites in `production/ui/datadoctor/` (sharing the jsdom install of `production/ui/`) draw the screen
(`ui/modules/datadoctor/`) on the answers written here into $DDS_FIXTURES:

* `screen_<scenario>.json` - `GET /cases/{case_id}/screen` for every shape the screen draws, built by the real visit
  and protocol (`tests/unit/doctor/screen_support.py`): the key question with "I don't know", a yes/no question with
  what each answer changes, a doubt in three parts, a request for more data, a small choice decided for the person,
  Approve with its exact sentence, changes that need their own yes, ready, not ready, a remembered answer with its
  date, and the same answer revoked;
* `me_on.json` / `me_off.json` - `GET /auth/me` with `data_doctor.unified` on and off (the switch the UI follows);
* `use_case.json`, `industries.json`, `models.json`, `runs.json`, `ai_service.json` - what the wizard, a goal's
  setup and the chat read, from the same app with no AI service connected.

`screen.test.mjs` checks each question, approval and doubt shape, Approve's exact sentence, Revoke, the chat with
no AI service (the way to Settings › AI service, not Connections), the accessibility checks of Plan M M181 and the
visible-text check; `places.test.mjs` opens the same case in the three places - the Data page, the wizard's
Customers step and a goal's setup - and shows that with the switch off today's screens are drawn unchanged.
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
from engine.doctor import case as doctor_case
from tests.fixtures.node import skip_without_jsdom
from tests.unit.doctor.screen_support import SCENARIOS, build_all

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "datadoctor"
GOAL: Final[str] = "targeted-advertisement"


def _ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def write_fixtures(root: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=root / "off")) as client:
        bodies["me_off"] = _ok(client.get("/auth/me"))
    monkeypatch.setattr(doctor_case, "data_doctor_unified", lambda root=None: True)
    # No AI service: the suite's stand-in AI (tests/conftest.py) is left out, as a new deployment has none.
    monkeypatch.delenv("MARKETING_AI_ALLOW_FAKE_AI", raising=False)
    with TestClient(create_app(config_root=config_root, data_dir=root / "on")) as client:
        bodies["me_on"] = _ok(client.get("/auth/me"))
        bodies["use_case"] = _ok(client.get(f"/use-cases/{GOAL}"))
        bodies["industries"] = _ok(client.get("/industries"))
        bodies["models"] = _ok(client.get("/models", params={"use_case": GOAL}))
        bodies["runs"] = _ok(client.get("/runs", params={"use_case": GOAL}))
        bodies["ai_service"] = _ok(client.get("/ai-service"))
    for name, body in build_all(root / "stores", config_root).items():
        bodies[f"screen_{name}"] = body
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def test_the_fixtures_are_the_world_the_screen_is_tested_in(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs without node: what the node suites rely on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root, monkeypatch)

    def read(name: str) -> Any:
        return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))

    assert read("me_on")["data_doctor"] is True and "data_doctor" not in read("me_off")
    assert read("ai_service")["slots"]["product"]["connected"] is False, "no AI service connected"
    keys = {name: read(f"screen_{name}")["view"]["verdict"]["key"] for name in SCENARIOS}
    assert keys == {
        "empty": "needs_files",
        "question": "needs_answers",
        "assumed": "needs_approval",
        "approve": "needs_approval",
        "approve_class": "needs_approval",
        "ready": "ready",
        "not_ready": "not_ready",
        "standing": "needs_answers",
        "revoked": "needs_answers",
    }
    question = read("screen_question")["case"]
    assert {q["code"] for q in question["questions"]} >= {
        "KEY_AMBIGUOUS",
        "UNKNOWN_BOOLEAN",
        "DATA_REQUESTED",
    }
    assert question["doubts"] and question["diagnosis"]["use_cases"]
    assert read("screen_standing")["view"]["standing"][0]["revoked"] is False
    assert read("screen_revoked")["view"]["standing"][0]["revoked"] is True


def test_the_data_doctor_screen_in_jsdom(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root, monkeypatch)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert len(tests) >= 2, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", "--test-concurrency=1", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "DDS_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-12000:] + result.stderr[-3000:]
