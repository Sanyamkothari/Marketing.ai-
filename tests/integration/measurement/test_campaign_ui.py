"""Plan J M94's screens in jsdom, against REAL API responses: Results lists campaigns, a campaign's page.

The node tests (`tests/integration/production/ui/measurement/*.test.mjs`, sharing that directory's
jsdom install as `simple/` and `measure/` do) load the page as `ui/index.html` does - every module
in its script order - and check what a person sees:

* Results lists the campaigns beside the runs, newest first, each linking to its page; with no
  campaign, Results is drawn exactly as before (no empty card);
* a campaign's page shows the plan the server froze - its holdout, power and hash - and only those
  values; before a plan exists, an Analyst gets the form and registering it posts the person's
  decisions and draws the server's answer;
* an early look is labelled and shows no verdict; a final result shows the verdict's headline.

Every body the fake API answers with was answered by the app below over a store holding a Phase 1
propensity run (`support.py`), with sign-in off.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.storage import LocalStorage
from tests.fixtures.node import skip_without_jsdom
from tests.integration.measurement.support import MATURE, USE_CASE, ok, propensity_run, upload

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "measurement"
RUN_ID: Final[str] = "r_20261007_94ui0001"


def _write(out: Path, name: str, body: Any) -> None:
    (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def _plan(**extra: Any) -> dict[str, Any]:
    return {
        "metric": "came back within 90 days",
        "outcome_column": "reactivated_90d",
        "analysis_date": "2026-09-15",
        "mde_pp": 2.0,
        "base_rate": 0.1,
        **extra,
    }


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    seeded = propensity_run(storage, RUN_ID, rows=4_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
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
        _write(out, "campaigns_empty", ok(client.get("/campaigns")))
        outcomes = upload(client, seeded.outcomes)

        # A campaign with outcomes and no plan yet: the form, then the registered plan.
        fresh = ok(client.post("/campaigns", json={"run_id": RUN_ID, "name": "Win-back, May"}), 201)
        fresh_id = fresh["campaign"]["campaign_id"]
        _write(out, "fresh", ok(client.post(f"/campaigns/{fresh_id}/outcomes", json={"upload_id": outcomes})))
        _write(out, "fresh_plans", ok(client.get(f"/campaigns/{fresh_id}/plan")))
        _write(
            out, "fresh_plan_registered", ok(client.post(f"/campaigns/{fresh_id}/plan", json=_plan()), 201)
        )
        _write(out, "fresh_after", ok(client.get(f"/campaigns/{fresh_id}")))
        _write(out, "fresh_plans_after", ok(client.get(f"/campaigns/{fresh_id}/plan")))

        # A campaign read before its analysis date (an early look), then on it (final).
        dated = ok(client.post("/campaigns", json={"run_id": RUN_ID, "name": "Win-back, June"}), 201)
        dated_id = dated["campaign"]["campaign_id"]
        ok(client.post(f"/campaigns/{dated_id}/outcomes", json={"upload_id": outcomes}))
        ok(client.post(f"/campaigns/{dated_id}/plan", json=_plan(mde_pp=0.3)), 201)
        _write(out, "dated_plans", ok(client.get(f"/campaigns/{dated_id}/plan")))
        _write(
            out,
            "early",
            ok(client.post(f"/campaigns/{dated_id}/measure", json={"as_of": MATURE.isoformat()})),
        )
        final_at = datetime(2026, 9, 15, tzinfo=UTC).isoformat()
        _write(out, "final", ok(client.post(f"/campaigns/{dated_id}/measure", json={"as_of": final_at})))
        _write(out, "campaigns", ok(client.get("/campaigns")))
    _write(out, "ids", {"fresh": fresh_id, "dated": dated_id, "run": RUN_ID})
    return out


def test_the_fixtures_are_the_world_the_screens_are_tested_in(tmp_path: Path, config_root: Path) -> None:
    out = write_fixtures(tmp_path, config_root)

    def read(name: str) -> Any:
        return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))

    assert read("campaigns_empty") == {"campaigns": []}
    assert [c["name"] for c in read("campaigns")["campaigns"]] == ["Win-back, June", "Win-back, May"]
    assert read("fresh_plans") == {"plan": None, "versions": []}
    assert read("fresh_after")["plan"]["plan_hash"] == read("fresh_plan_registered")["plan_hash"]
    assert read("early")["report"]["early_look"] is True and read("early")["verdict"] is None
    assert read("final")["report"]["early_look"] is False and read("final")["verdict"] is not None
    assert read("dated_plans")["plan"]["warnings"] == ["PLAN_UNDERPOWERED"]


def test_the_campaign_screens_in_jsdom(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "CP_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]
