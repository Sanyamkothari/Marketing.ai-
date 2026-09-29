"""The Guided setup tab (`ui/modules/agent/`, Plan G M75) in jsdom, against REAL API responses.

The node test (`production/ui/agent/guided.test.mjs`, which shares that directory's jsdom install as
`usecase/`, `ops/` and `approvals/` do) drives the real `ui/usecase.js` with the real agent module
registered. Everything its fake API answers with was answered by the app below, in the order the
screen asks for it:

* **main** - a messy Targeted Advertisement file: the session as started, one chat turn, the leak
  question answered "hide", every pending suggestion decided as its box starts (the helper's `sure`
  ones ticked, its `check` ones not), the preview, Approve, the prepared upload's profile, and the
  `POST /runs` the Setup form's Run then sends;
* **conflict** - the same file with the outcome unticked, whose Approve answers `409` with the Run
  button's own checks;
* **stopped** - a file too small to learn from, where the helper stops and says why.

`sent.test.mjs` (same directory, run by the same glob) covers "What the AI looked at" and the access
line under the chat input: it takes the recorded chat bodies and adds the newer `sent` / `data_access` /
`third_party` fields by hand, so an unchanged recording is also its "older API" case.

Two things are not recorded but derived in the node test from these bodies: a session whose titles
and chat reply carry markup and markdown (escaping), and the decisions the screen must send (from the
recorded session, by the same rule the recording used).
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
from api.routes import runs
from engine.jobs import CancelToken
from tests.fixtures.agent_bench.make_messy import messy_frame
from tests.fixtures.make_data import GenerationSpec, generate
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "production" / "ui"
"""The node package holding jsdom (shared with the Phase 4b screens' tests)."""

TESTS_DIR: Final[Path] = NODE_DIR / "agent"
"""Guided setup's jsdom tests."""

USE_CASE: Final[str] = "targeted-advertisement"
GENERATIVE: Final[str] = "ai-onboarding-assistant"
CHAT: Final[str] = "What about 'monthly_spend'?"
AUTOML: Final[str] = "__automl__"


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _upload(client: TestClient, frame: Any) -> Any:
    return _ok(
        client.post(
            "/uploads",
            files={"file": ("history.csv", frame.to_csv(index=False).encode(), "text/csv")},
            data={"use_case": USE_CASE, "mode": "train"},
        ),
        201,
    )


def _as_ticked(session: dict[str, Any], *, untick: str | None = None) -> list[dict[str, str]]:
    """What the screen sends for the boxes as they start: pending `sure` accepted, the rest rejected.

    `untick` names a role path (`target`) whose box the person cleared."""
    return [
        {
            "proposal_id": p["proposal_id"],
            "state": (
                "accepted"
                if p["confidence"] == "sure" and not (untick and p["path"] == untick)
                else "rejected"
            ),
        }
        for p in session["proposals"]
        if p["state"] == "pending"
    ]


def _answer_leak(client: TestClient, upload_id: str, session: dict[str, Any]) -> Any:
    question = next(q for q in session["questions"] if q["blocking"] and q["answer"] is None)
    option = next(o for o in question["options"] if o["option_id"] == "hide")
    return _ok(
        client.post(
            f"/uploads/{upload_id}/agent-session/answers",
            json={"question_id": question["question_id"], "option_id": option["option_id"]},
        )
    )


def write_fixtures(root: Path, config_root: Path) -> Path:
    """Every body the node test replays, answered by the real app."""
    out = root / "fixtures"
    out.mkdir(parents=True)
    bodies: dict[str, Any] = {}
    ids: dict[str, str] = {}
    with TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        bodies["use_case_generative"] = _ok(client.get(f"/use-cases/{GENERATIVE}"))
        bodies["runs_empty"] = _ok(client.get("/runs", params={"use_case": USE_CASE}))
        bodies["models_empty"] = _ok(client.get("/models", params={"use_case": USE_CASE}))

        # main: start, chat, answer, decide as ticked, preview, approve, then Run.
        bodies["upload_main"] = _upload(client, messy_frame())
        main = ids["main"] = bodies["upload_main"]["upload_id"]
        base = f"/uploads/{main}/agent-session"
        bodies["main_start"] = _ok(client.post(base, json={"use_case": USE_CASE}), 201)
        bodies["main_chat"] = _ok(client.post(f"{base}/messages", json={"text": CHAT}))
        bodies["main_answer"] = _answer_leak(client, main, bodies["main_chat"]["session"])
        bodies["main_decided"] = _ok(
            client.post(f"{base}/decisions", json={"decisions": _as_ticked(bodies["main_answer"]["session"])})
        )
        bodies["main_preview"] = _ok(client.post(f"{base}/preview"))
        applied = bodies["main_apply"] = _ok(client.post(f"{base}/apply"))
        derived = ids["derived"] = applied["upload_id"]
        bodies["derived_profile"] = _ok(client.get(f"/uploads/{derived}/profile"))
        created = bodies["run_created"] = _ok(
            client.post(
                "/runs",
                json={
                    "use_case": USE_CASE,
                    "mode": "train",
                    "upload_id": derived,
                    "primary_key": applied["primary_key"],
                    "target": applied["target"],
                    "overrides": applied["overrides"],
                    "model_choice": AUTOML,
                },
            ),
            202,
        )
        ids["run"] = created["run_id"]
        bodies["run"] = _ok(client.get(f"/runs/{created['run_id']}"))

        # conflict: the outcome unticked, so Approve answers with the Run button's checks.
        bodies["upload_conflict"] = _upload(client, messy_frame())
        conflict = ids["conflict"] = bodies["upload_conflict"]["upload_id"]
        base = f"/uploads/{conflict}/agent-session"
        bodies["conflict_start"] = _ok(client.post(base, json={"use_case": USE_CASE}), 201)
        bodies["conflict_answer"] = _answer_leak(client, conflict, bodies["conflict_start"]["session"])
        bodies["conflict_decided"] = _ok(
            client.post(
                f"{base}/decisions",
                json={"decisions": _as_ticked(bodies["conflict_answer"]["session"], untick="target")},
            )
        )
        refused = client.post(f"{base}/apply")
        assert refused.status_code == 409, refused.text
        bodies["conflict_apply"] = refused.json()

        # stopped: too few rows to learn from.
        bodies["upload_stopped"] = _upload(client, generate(GenerationSpec(use_case_id=USE_CASE, rows=900)))
        stopped = ids["stopped"] = bodies["upload_stopped"]["upload_id"]
        bodies["stopped_start"] = _ok(
            client.post(f"/uploads/{stopped}/agent-session", json={"use_case": USE_CASE}), 201
        )
    bodies["ids"] = {**ids, "chat": CHAT, "automl": AUTOML}
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


@pytest.fixture
def idle_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /runs` is real up to the job, which does nothing: nothing trains."""

    def idle(*_args: Any, **_kwargs: Any) -> Any:
        def job(cancel: CancelToken) -> None:
            del cancel

        return job

    monkeypatch.setattr(runs, "build_job_fn", idle)


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.usefixtures("idle_runs")
def test_the_fixtures_are_the_world_the_screen_is_tested_in(tmp_path: Path, config_root: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root)
    start = _read(out, "main_start")
    assert start["chat"]["backend"] == "fake"
    session = start["session"]
    assert session["status"] == "needs_review"
    kinds = {p["kind"] for p in session["proposals"]}
    assert {"role", "recipe_step", "setting"} <= kinds
    assert {p["confidence"] for p in session["proposals"]} == {"sure", "check"}
    assert any(q["blocking"] for q in session["questions"])
    reply = _read(out, "main_chat")["session"]["transcript"][-1]
    assert reply["role"] == "agent" and reply["evidence_ids"], "the chat reply cites its evidence"
    assert _read(out, "main_decided")["session"]["status"] == "ready"
    applied = _read(out, "main_apply")
    assert applied["upload_id"] != _read(out, "ids")["main"], "Approve prepared a copy"
    assert applied["overrides"], "an accepted setting reaches the Run request"
    assert applied["receipt"]["steps"]
    conflict = _read(out, "conflict_apply")
    assert conflict["detail"]["code"] == "VALIDATION_FAILED"
    assert "TARGET_MISSING" in [c["code"] for c in conflict["validation"]["checks"]]
    stopped = _read(out, "stopped_start")["session"]
    assert stopped["status"] == "stopped" and stopped["stop_reason"]
    assert not _read(out, "use_case_generative")["advanced_settings"]["stages"]


@pytest.mark.usefixtures("idle_runs")
def test_guided_setup_in_jsdom(tmp_path: Path, config_root: Path) -> None:
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
