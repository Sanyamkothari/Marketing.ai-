"""The MCP demo world: the product with sign-in on, the verifier switched on, and Hillstrom's library campaign
audited through the product's own route (Plan K M133).

Nothing is hand-written: the Hillstrom sample (`library/hillstrom-email/sample.csv`, the committed tenth of the
public file) is split into the two files a client sends with `library.audit`'s own builders and posted as
`POST /uploads` twice and `POST /campaigns/audit` (the conversion case: each e-mail against no e-mail), then
the Value Proof Pack's value inputs are entered (`PUT /pilot/proof/{id}/value`). A second, non-demo campaign is
the scenario world's (`tests/agentic`), measured on the win-back use case; it must stay out of reach.

The configuration root is a copy of `configs/` with `agentic.enabled` and `mcp.enabled` on.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import create_app
from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings

PASSWORD: Final[str] = "correct horse battery staple"
FAST_ITERATIONS: Final[int] = 300
SAMPLE: Final[Path] = Path(__file__).resolve().parents[3] / "library" / "hillstrom-email" / "sample.csv"
POWER_BODY: Final[dict[str, Any]] = {
    "eligible": 6_400,
    "base_rate": 0.006,
    "holdout_shares": [0.1, 0.2, 1 / 3],
    "value_per_conversion": 2_000.0,
    "contact_cost": 1.0,
    "direction": "up",
}


def mcp_config_root(source: Path, target: Path, **mcp: Any) -> Path:
    """`configs/` with the agent-safe core and the MCP server switched on (and any `mcp:` setting given)."""
    shutil.copytree(source, target)
    (target / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: true\n", encoding="utf-8")
    path = target / "agentic" / "mcp.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["mcp"].update({"enabled": True, **mcp})
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def signed_in_app(config_root: Path, data_dir: Path, *, auth_mode: str = "local") -> FastAPI:
    """The real app on `data_dir` with sign-in `auth_mode` and a fast-hashing user store (as
    `tests/integration/production/access_support.local_app`, with a configuration root)."""
    settings = Settings(auth_mode=auth_mode, env="local", data_dir=data_dir)  # type: ignore[arg-type]
    app = create_app(config_root=config_root, data_dir=data_dir)
    app.state.settings = settings
    app.state.user_store = SqlUserStore(
        sqlite_engine(data_dir / PLATFORM_DB_FILENAME),
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        iterations=FAST_ITERATIONS,
    )
    return app


@dataclass
class Demo:
    config_root: Path
    data_dir: Path
    users: dict[str, str] = field(default_factory=dict)
    campaign_id: str = ""
    frame: pd.DataFrame | None = None

    def app(self, *, auth_mode: str = "local") -> FastAPI:
        return signed_in_app(self.config_root, self.data_dir, auth_mode=auth_mode)

    def token(self, who: str) -> str:
        store = SqlUserStore(
            sqlite_engine(self.data_dir / PLATFORM_DB_FILENAME),
            session_ttl_seconds=3600,
            iterations=FAST_ITERATIONS,
        )
        return store.create_session(self.users[who]).token

    def headers(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(who)}"}


def upload(client: TestClient, headers: dict[str, str], frame: pd.DataFrame, name: str, use_case: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    response = client.post(
        "/uploads",
        files={"file": (f"{name}.csv", payload, "text/csv")},
        data={"use_case": use_case, "mode": "score"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def audit_hillstrom(
    client: TestClient, headers: dict[str, str], frame: pd.DataFrame, *, name: str = "Hillstrom e-mail test"
) -> str:
    """Hillstrom's conversion case through `POST /campaigns/audit`, as `library.audit.run_audit` posts it."""
    from library.audit import AUDIT_CASES, HILLSTROM_AUDIT, assignment_file, outcomes_file

    spec = HILLSTROM_AUDIT
    case = next(item for item in AUDIT_CASES if item.key == "conversion")
    assert case.treated is not None
    outcomes_id = upload(client, headers, outcomes_file(frame, spec), "outcomes", spec.use_case)
    assignment_id = upload(client, headers, assignment_file(frame, spec, case), "assignment", spec.use_case)
    body = {
        "name": name,
        "primary_key": spec.journey.primary_key,
        "assignment": {
            "upload_id": assignment_id,
            "arm_column": case.arm_column,
            "control_value": spec.journey.levels[0],
            "treated_values": list(case.treated),
        },
        "outcomes": {
            "upload_id": outcomes_id,
            "outcome_column": case.outcome,
            "outcome_kind": case.kind,
            "positive_label": "1",
        },
        "assignment_basis": "random",
        "treatment_start": spec.treatment_start,
        "outcome_window_days": spec.outcome_window_days,
        "outcome_is_good": True,
    }
    response = client.post("/campaigns/audit", json=body, headers=headers)
    assert response.status_code == 201, response.text
    campaign_id = str(response.json()["campaign"]["campaign_id"])
    value = client.put(
        f"/pilot/proof/{campaign_id}/value",
        json={
            "value_per_outcome": 2_000.0,
            "outcome_is_good": True,
            "value_basis": "An assumed value per conversion, for the demo",
            "contact_cost": 1.0,
        },
        headers=headers,
    )
    assert value.status_code == 200, value.text
    return campaign_id


@pytest.fixture(scope="module")
def demo(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Demo:
    base = tmp_path_factory.mktemp("mcp-demo")
    world = Demo(config_root=mcp_config_root(config_root, base / "configs"), data_dir=base / "data")
    world.data_dir.mkdir()
    app = world.app()
    store = app.state.user_store
    world.users = {
        "analyst": store.create_user("asha", PASSWORD, roles=[Role.ANALYST], created_by="test").user_id,
        "viewer": store.create_user("vera", PASSWORD, roles=[Role.VIEWER], created_by="test").user_id,
        "approver": store.create_user("ravi", PASSWORD, roles=[Role.APPROVER], created_by="test").user_id,
    }
    world.frame = pd.read_csv(SAMPLE)
    with TestClient(app) as client:
        world.campaign_id = audit_hillstrom(client, world.headers("analyst"), world.frame)
    return world
