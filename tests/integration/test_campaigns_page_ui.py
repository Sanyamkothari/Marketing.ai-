"""Plan M M176 (DEC-1606): the Campaigns list and the campaign page, in jsdom, and adding results through its tab.

Three node suites in `tests/integration/production/ui/campaigns/` (sharing that directory's jsdom install):

* `rules.test.mjs` - the pure rules: the one status-chip rule (Draft, Live, Waiting for results, Measured), short
  names, the next step, the evidence label in words, the contact list (search, 25 rows a page, "Why these
  customers?") and the bodies of the two writes;
* `demo.test.mjs` - the list and every tab of both Hillstrom campaigns, drawn against the API answers recorded from
  the validated demo (`tests/fixtures/ui_demo/api.json`), plus the older pages' "Add results from the campaign page";
* `results.test.mjs` - a world this file builds with the real app (a Phase 1 scoring run, five campaigns in four
  states): the chips and primary buttons per state, adding results through the Results tab (the requests it sends
  are pinned to the ones this file sent to the real app), a campaign whose results are not all in yet, and
  "Refresh the list" with its confirmation (the request it sends is the one this file sent to the real `POST /runs`).

Only those three files are run: the directory is shared with M177's wizard suites, which have their own runner.

The acceptance "adding results from the tab produces the same report as today's route" is checked here, in Python,
on the real app: the requests the tab sends (pinned in `results.test.mjs` against `expected.json`) give the same
report figures (every field but when and for which record it was made) as `POST /runs/{id}/campaign-results` on the
same file, and as Plan J's own campaign route (outcomes, then measure) on a twin campaign.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.schemas import RunRequest
from engine.config import RunMode
from engine.contracts import RunRecord, RunState, RunStatus
from engine.pipeline import STATUS_FILENAME
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import IncrementalityReport
from tests.fixtures.node import skip_without_jsdom
from tests.integration.measurement.support import PRIMARY_KEY, SENT, TARGET, USE_CASE, ok, propensity_run

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "campaigns"
SUITES: Final[tuple[str, ...]] = ("rules.test.mjs", "demo.test.mjs", "results.test.mjs")
"""M176's own suites. The directory is shared with M177 (wizard*.test.mjs, run by its own test), so no glob."""
RUN_ID: Final[str] = "r_20261007_17600001"
RESULTS_FILE: Final[str] = "october_results.csv"
DIFFERS_ONLY_IN: Final[set[str]] = {"computed_at", "campaign_id", "as_of", "test_plan_hash", "early_look"}
"""What may differ between two reports of the same file: when and for which record each was made, and which plan
it was judged by (the run's own route has no plan)."""


def _plan(analysis_date: str) -> dict[str, Any]:
    return {
        "metric": "came back within 90 days",
        "outcome_column": TARGET,
        "analysis_date": analysis_date,
        "mde_pp": 2.0,
        "base_rate": 0.1,
    }


def _write(out: Path, name: str, body: Any) -> None:
    (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def _answer(response: Any) -> dict[str, Any]:
    """A response as the fake API replays it: its status and body."""
    return {"status": response.status_code, "body": response.json()}


def _upload(client: TestClient, payload: bytes, name: str) -> Any:
    return client.post(
        "/uploads", files={"file": (name, payload, "text/csv")}, data={"use_case": USE_CASE, "mode": "score"}
    )


def refresh_body(run: dict[str, Any]) -> dict[str, Any]:
    """`requests.js` `refreshRequest` for a run read from `GET /runs/{id}` (an uploaded file, no cost answer)."""
    return {
        "use_case": run["use_case_id"],
        "mode": "score",
        "primary_key": run["primary_key"],
        "overrides": run.get("overrides") or {},
        "upload_id": run["upload_id"],
    }


def build_world(root: Path, config_root: Path) -> tuple[Path, dict[str, Any]]:
    """Write the fixtures the jsdom suite replays; return them with the reports the equivalence test compares."""
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    seeded = propensity_run(storage, RUN_ID, rows=4_000)
    # `GET /runs/{id}` reads the status beside the record; the seeded run finished, so its status says done.
    storage.write_model(
        run_key(RUN_ID, STATUS_FILENAME),
        RunStatus(
            run_id=RUN_ID,
            mode=RunMode.SCORE,
            state=RunState.DONE,
            updated_at=SENT,
            stages=(),
            progress_pct=100,
        ),
    )
    results_csv = seeded.outcomes.to_csv(index=False, lineterminator="\n").encode()
    (out / RESULTS_FILE).write_bytes(results_csv)
    found: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        # The run's customers as a real scoring upload, so "Refresh the list" names a file the app has.
        customers = seeded.scores[[PRIMARY_KEY]].to_csv(index=False, lineterminator="\n").encode()
        scoring = _upload(client, customers, "winback_campaign.csv")
        assert scoring.status_code == 201, scoring.text
        record = storage.read_model(run_key(RUN_ID, RUN_FILENAME), RunRecord)
        storage.write_model(
            run_key(RUN_ID, RUN_FILENAME),
            record.model_copy(update={"upload_id": scoring.json()["upload_id"]}),
        )
        for name, path in (
            ("industries", "/industries"),
            ("use_case", f"/use-cases/{USE_CASE}"),
            ("me_off", "/auth/me"),
            ("demo", "/pilot/demo"),
            ("help", "/pilot/help"),
            ("clients", "/clients"),
            ("healthz", "/healthz"),
            ("datasets", "/datasets"),
        ):
            _write(out, name, ok(client.get(path)))
        _write(out, "models", ok(client.get("/models", params={"use_case": USE_CASE})))
        _write(out, "runs", ok(client.get("/runs", params={"limit": 100})))
        run = ok(client.get(f"/runs/{RUN_ID}"))
        _write(out, "run", run)
        # "Refresh the list" on the real app: the body is a valid `RunRequest` and reaches the scoring checks (no
        # champion model in this world, so the app answers which model is missing), with and without the cost answer.
        refresh = refresh_body(run["run"])
        RunRequest.model_validate(refresh)
        RunRequest.model_validate({**refresh, "confirm_cost": True})
        refreshed = client.post("/runs", json=refresh)
        _write(out, "refresh_real", _answer(refreshed))
        found["refresh"] = refreshed
        found["refresh_confirmed"] = client.post("/runs", json={**refresh, "confirm_cost": True})
        summary = client.get(f"/runs/{RUN_ID}/artefacts/treat_list_summary.json")
        _write(out, "summary", _answer(summary))

        def create(name: str, **extra: Any) -> str:
            body = {"run_id": RUN_ID, "name": name, **extra}
            return str(ok(client.post("/campaigns", json=body), 201)["campaign"]["campaign_id"])

        # Five campaigns from the one list, in the four states of the chip. The Draft is a list made yesterday
        # whose plan is not fixed (as M177's wizard leaves one); the March list has no plan and its outcome window
        # closed long ago, so it waits for results rather than staying a Draft for ever.
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        draft = create("Win-back, July list", treatment_start=yesterday)
        old = create("Win-back, March list")
        live = create("Win-back, August list")
        ok(client.post(f"/campaigns/{live}/plan", json=_plan("2027-01-15")), 201)
        due = create("Win-back, May list")
        ok(client.post(f"/campaigns/{due}/plan", json=_plan("2026-09-15")), 201)
        fresh = create("Win-back, this week", treatment_start=yesterday)
        twin = create("Win-back, May list (twin)")
        ok(client.post(f"/campaigns/{twin}/plan", json=_plan("2026-09-15")), 201)
        for name, cid in (("draft", draft), ("old", old), ("live", live), ("due", due), ("fresh", fresh)):
            _write(out, f"view_{name}", ok(client.get(f"/campaigns/{cid}")))
        # The list before any result is added (the twin is this test's own comparison, not part of the world).
        listed = ok(client.get("/campaigns"))["campaigns"]
        _write(out, "campaigns", {"campaigns": [c for c in listed if c["campaign_id"] != twin]})

        # Adding results to `due` as its Results tab does: the file, then its outcomes as the plan registered them,
        # then the measurement with an empty body (now).
        plan = ok(client.get(f"/campaigns/{due}"))["plan"]
        upload = client.post(
            "/uploads",
            files={"file": (RESULTS_FILE, results_csv, "text/csv")},
            data={"use_case": USE_CASE, "mode": "score"},
        )
        assert upload.status_code == 201, upload.text
        upload_id = upload.json()["upload_id"]
        outcomes_body = {"upload_id": upload_id, "outcome_column": plan["outcome_column"]}
        if plan.get("positive_label") is not None:
            outcomes_body["positive_label"] = plan["positive_label"]
        outcomes = client.post(f"/campaigns/{due}/outcomes", json=outcomes_body)
        measure = client.post(f"/campaigns/{due}/measure", json={})
        _write(out, "upload_due", {"status": 201, "body": upload.json()})
        _write(out, "outcomes_due", _answer(outcomes))
        _write(out, "measure_due", _answer(measure))
        _write(out, "view_due_after", ok(client.get(f"/campaigns/{due}")))
        _write(out, "proof_due", _answer(client.get(f"/pilot/proof/{due}", params={"format": "json"})))
        found["tab_report"] = measure.json()["report"]

        # Today's routes on the same file: the run's own campaign results, and Plan J's campaign route on a twin.
        found["run_report"] = ok(
            client.post(
                f"/runs/{RUN_ID}/campaign-results",
                json={
                    "upload_id": upload_id,
                    "outcome_column": TARGET,
                    "outcome_window_days": 90,
                    "as_of": found["tab_report"]["as_of"],
                },
            )
        )
        ok(client.post(f"/campaigns/{twin}/outcomes", json={"upload_id": upload_id}))
        found["twin_report"] = ok(
            client.post(f"/campaigns/{twin}/measure", json={"as_of": found["tab_report"]["as_of"]})
        )["report"]

        # A campaign that went out yesterday: its results file goes in, its result is not ready (a day, no number).
        fresh_upload = _upload(client, results_csv, RESULTS_FILE)
        _write(out, "upload_fresh", {"status": 201, "body": fresh_upload.json()})
        _write(
            out,
            "outcomes_fresh",
            _answer(
                client.post(
                    f"/campaigns/{fresh}/outcomes", json={"upload_id": fresh_upload.json()["upload_id"]}
                )
            ),
        )
        _write(out, "measure_fresh", _answer(client.post(f"/campaigns/{fresh}/measure", json={})))
        _write(out, "view_fresh_after", ok(client.get(f"/campaigns/{fresh}")))
    ids = {"draft": draft, "old": old, "live": live, "due": due, "fresh": fresh, "run": RUN_ID}
    _write(out, "ids", ids)
    # The requests the Results tab must send to give that report (`results.test.mjs` pins them).
    _write(
        out,
        "expected",
        {
            "upload": {"use_case": USE_CASE, "mode": "score", "file": RESULTS_FILE},
            "outcomes": {**outcomes_body, "upload_id": "<the upload's id>"},
            "measure": {},
            "refresh": refresh,
        },
    )
    return out, found


def _figures(report: dict[str, Any]) -> dict[str, Any]:
    return IncrementalityReport.model_validate(report).model_dump(exclude=DIFFERS_ONLY_IN)


def test_results_added_on_the_tab_give_the_same_report_as_todays_routes(
    tmp_path: Path, config_root: Path
) -> None:
    out, found = build_world(tmp_path, config_root)
    tab = found["tab_report"]
    assert tab["early_look"] is False and tab["treated_rows"] > 0 and tab["control_rows"] > 0
    assert _figures(tab) == _figures(found["run_report"]), "the run's own campaign results"
    assert _figures(tab) == _figures(found["twin_report"]), "Plan J's campaign route on a twin campaign"
    measure = json.loads((out / "measure_due.json").read_text(encoding="utf-8"))
    assert measure["status"] == 200 and measure["body"]["campaign"]["status"] == "measured"
    fresh = json.loads((out / "measure_fresh.json").read_text(encoding="utf-8"))
    assert fresh["status"] == 409 and fresh["body"]["detail"]["code"] == "CAMPAIGN_NOT_MATURED"


def test_refresh_the_list_sends_a_request_the_real_runs_route_accepts(
    tmp_path: Path, config_root: Path
) -> None:
    """The body "Refresh the list" sends (pinned in `results.test.mjs` to `expected.json`) passes `RunRequest`
    (`extra="forbid"`), finds the run's file, and is answered on its merits: here, that the goal has no approved
    model to score with yet. Never a 422 (a body the route refuses) nor a 404 (a file it cannot find)."""
    out, found = build_world(tmp_path, config_root)
    expected = json.loads((out / "expected.json").read_text(encoding="utf-8"))
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))["run"]
    assert expected["refresh"] == refresh_body(run)
    for answer in (found["refresh"], found["refresh_confirmed"]):
        assert answer.status_code in {202, 409}, answer.text
        if answer.status_code == 409:
            assert answer.json()["detail"]["code"] in {"CHAMPION_NOT_FOUND", "RUN_COST_NEEDS_CONFIRMATION"}


def test_the_campaign_screens_in_jsdom(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out, _ = build_world(tmp_path, config_root)
    tests = [str(TESTS_DIR / name) for name in SUITES]
    assert all(Path(t).is_file() for t in tests), tests
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "M176_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
