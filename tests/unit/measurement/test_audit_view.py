"""Plan J M103's screens, run by node against the app's real answers (`audit_view.test.mjs`).

The pure builders (`ui/modules/decide/audit.js` and the campaign page that draws its cards) need node, not a
browser: the Python half runs an audit of each kind, a contact file and a programme through the app and writes
the answers as the fixtures the JavaScript half reads, so the page is tested on what the server really says.
Without node the test skips with its reason (`REQUIRE_JSDOM=1` makes that a failure).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from engine.config import load_use_case
from engine.holdout.salt import resolve_holdout
from engine.holdout.spec import HoldoutConfig
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from tests.fixtures.node import skip_without_node
from tests.integration.measurement.support import USE_CASE, ok, upload

BEFORE_PERIOD = datetime(2025, 12, 15, 9, 0, tzinfo=UTC)
"""The universal holdout was first used before the programme's period began."""

SALT = "audit-view-salt-0000001"
TEST = Path(__file__).resolve().parent / "audit_view.test.mjs"


def _write(out: Path, name: str, body: Any) -> None:
    (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    config = load_use_case(USE_CASE)
    config = config.model_copy(
        update={
            "actions": config.actions.model_copy(
                update={"holdout": HoldoutConfig(scope="universal", fraction=0.1)}
            )
        }
    )
    settings = Settings(data_dir=data_dir, holdout_salt=SecretStr(SALT))
    resolve_holdout(
        config, settings, sqlite_engine(data_dir / PLATFORM_DB_FILENAME), at=BEFORE_PERIOD, record=True
    )
    app = create_app(config_root=config_root, data_dir=data_dir)
    app.state.settings = settings
    rng = np.random.default_rng(5)
    with TestClient(app) as client:

        def audit(
            sim: Any, assignment: pd.DataFrame, basis: str = "random", contacts: pd.DataFrame | None = None
        ) -> Any:
            body: dict[str, Any] = {
                "primary_key": "customer_id",
                "assignment": {"upload_id": upload(client, assignment), "arm_column": "group"},
                "outcomes": {
                    "upload_id": upload(client, sim.outcomes),
                    "outcome_column": "converted",
                    "treatment_date_column": "treatment_date",
                },
                "assignment_basis": basis,
                "treatment_start": "2026-04-01T00:00:00Z",
                "outcome_window_days": OUTCOME_WINDOW_DAYS,
                "as_of": AS_OF.isoformat(),
            }
            if contacts is not None:
                body["contact"] = {"upload_id": upload(client, contacts), "contacted_column": "contacted"}
            return ok(client.post("/campaigns/audit", json=body), 201)

        sim = population(6_000, 0.10, 0.05, seed=31, control_share=0.2, compliance=0.6, contamination=0.1)
        scores = sim.scores
        details = pd.DataFrame(
            {
                "customer_id": scores["customer_id"],
                "group": np.where(scores["control_group"], 0, 1),
                "age": rng.integers(18, 80, len(scores)),
                "region": rng.choice(list("ABCD"), len(scores)),
            }
        )
        _write(out, "verified", audit(sim, details))
        _write(out, "declared", audit(sim, details[["customer_id", "group"]]))
        # chosen by the customer's own details: descriptive only
        loyal = rng.normal(size=len(scores))
        chosen = rng.random(len(scores)) < 1.0 / (1.0 + np.exp(-2.5 * loyal))
        targeted = details.assign(group=chosen.astype(int), loyalty=loyal)
        _write(out, "descriptive", audit(sim, targeted))
        reached = pd.DataFrame(
            {"customer_id": scores["customer_id"], "contacted": sim.received_treatment.astype(int)}
        )
        _write(out, "contacts", audit(sim, details, contacts=reached))
        everyone = pd.DataFrame(
            {"customer_id": scores["customer_id"], "converted": (rng.random(len(scores)) < 0.1).astype(int)}
        )
        _write(
            out,
            "programme",
            ok(
                client.post(
                    "/campaigns/programme",
                    json={
                        "period": {"start": "2026-01-01", "end": "2026-03-31"},
                        "primary_key": "customer_id",
                        "outcome": {"upload_id": upload(client, everyone), "outcome_column": "converted"},
                    },
                ),
                201,
            ),
        )
    return out


def test_the_fixtures_are_the_world_the_screens_are_tested_in(tmp_path: Path, config_root: Path) -> None:
    out = write_fixtures(tmp_path, config_root)

    def read(name: str) -> Any:
        return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))

    assert [read(n)["audit"]["label"] for n in ("verified", "declared", "descriptive")] == [
        "Causal",
        "Random by your statement, not verified",
        "Descriptive only",
    ]
    assert read("descriptive")["verdict"] is None
    assert read("contacts")["contacts"]["complier"] is not None
    assert read("programme")["programme"]["label"] == "Causal, whole programme"


def test_the_audit_screens_in_node(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_node()
    out = write_fixtures(tmp_path, config_root)
    result = subprocess.run(
        [node, "--test", str(TEST)],
        cwd=TEST.parent,
        env={**os.environ, "CP_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]


def test_the_page_module_imports_in_node_without_a_browser() -> None:
    """`audit.js` is pure: nothing at import time touches a document, so it can be run by node alone."""
    node = skip_without_node()
    script = (
        "import('"
        + (Path(__file__).resolve().parents[3] / "ui/modules/decide/audit.js").as_uri()
        + "').then(m => { if (typeof m.auditPageHtml !== 'function') process.exit(1); })"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
