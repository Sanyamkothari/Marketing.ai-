"""Step 4, "Measure the campaign" (Plan H M83), in jsdom against REAL API responses.

The node test (`production/ui/measure/measure.test.mjs`, sharing that directory's jsdom install as
`usecase/` does) renders `ui/usecase.js`'s Results view of a finished scoring run with the measure
module loaded, uploads an outcomes file and learns who to contact; every body its fake API answers
with was answered by the app below over a seeded store: the use cases (one that contacts customers,
one operational), the step before and after the upload, a campaign whose outcome window is not over,
one too small to learn from, and the step once the uplift model has been learned.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.storage import LocalStorage, run_key
from tests.fixtures.node import skip_without_jsdom
from tests.integration.uplift.test_measure_campaign import (
    FAST,
    LATER,
    SENT,
    USE_CASE,
    _upload,
    seed_scored_run,
)

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "measure"
RUN_ID: Final[str] = "r_20260928_3b000001"
SMALL_RUN_ID: Final[str] = "r_20260928_3b000002"
OPS_USE_CASE: Final[str] = "order-fulfillment"


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        seeded = seed_scored_run(client, storage, run_id=RUN_ID)
        small = seed_scored_run(client, storage, run_id=SMALL_RUN_ID, rows=3_000)
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        bodies["use_case_ops"] = _ok(client.get(f"/use-cases/{OPS_USE_CASE}"))
        bodies["run"] = json.loads(storage.read_bytes(run_key(RUN_ID, "run.json")))
        bodies["run_small"] = json.loads(storage.read_bytes(run_key(SMALL_RUN_ID, "run.json")))
        bodies["measure_before"] = _ok(client.get(f"/runs/{RUN_ID}/measure"))
        csv = seeded.outcomes.to_csv(index=False, lineterminator="\n").encode()
        bodies["upload_outcomes"] = _ok(
            client.post(
                "/uploads",
                files={"file": ("outcomes.csv", csv, "text/csv")},
                data={"use_case": USE_CASE, "mode": "score"},
            ),
            201,
        )
        upload_id = bodies["upload_outcomes"]["upload_id"]
        bodies["measure_early"] = _ok(
            client.post(
                f"/runs/{RUN_ID}/measure",
                json={"upload_id": upload_id, "as_of": (SENT + timedelta(days=10)).isoformat()},
            )
        )
        bodies["measure_after"] = _ok(
            client.post(f"/runs/{RUN_ID}/measure", json={"upload_id": upload_id, "as_of": LATER.isoformat()})
        )
        small_upload = _upload(client, small.outcomes, name="small_outcomes.csv")
        bodies["measure_small"] = _ok(
            client.post(
                f"/runs/{SMALL_RUN_ID}/measure", json={"upload_id": small_upload, "as_of": LATER.isoformat()}
            )
        )
        bodies["learn"] = _ok(client.post(f"/runs/{RUN_ID}/measure/learn", json={"overrides": FAST}), 202)
        deadline = time.monotonic() + 600
        while True:
            view = _ok(client.get(f"/runs/{RUN_ID}/measure"))
            if view["uplift_run"] and view["uplift_run"]["state"] in {"done", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline, "the uplift run did not finish"
            time.sleep(0.2)
        bodies["measure_learned"] = view
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def fixtures(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Written once: the learned model takes a few seconds to train."""
    return write_fixtures(tmp_path_factory.mktemp("measure-ui"), config_root)


def test_the_fixtures_are_the_world_the_step_is_tested_in(fixtures: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    out = fixtures
    assert _read(out, "use_case")["config"]["actions"]["contacts_customers"] is True
    assert _read(out, "use_case_ops")["config"]["actions"]["contacts_customers"] is False
    before = _read(out, "measure_before")
    assert before["offered"] is True and before["report"] is None and before["held_back"] > 0
    assert _read(out, "measure_after")["verdict"]["kind"] == "added"
    assert _read(out, "measure_after")["learn"]["ready"] is True
    assert _read(out, "measure_early")["verdict"]["headline"] == "Outcome window not over yet"
    small = _read(out, "measure_small")
    assert small["learn"]["ready"] is False and small["learn"]["reason"].startswith("To learn who to contact")
    learned = _read(out, "measure_learned")
    assert learned["uplift_run"] == {"run_id": _read(out, "learn")["run_id"], "state": "done"}


def test_the_measure_step_in_jsdom(fixtures: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = fixtures
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
