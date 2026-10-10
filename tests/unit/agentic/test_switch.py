"""`agentic.enabled` off (the default): the product is byte-identical to Plan J's (design §11).

No `/agentic` route exists, the OpenAPI document is the one a configuration without Plan K's files gives, no
table is created, nothing is written under the data folder, the ledger module is not even imported by the
app, and the in-process entry point refuses with `AGENTIC_DISABLED`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from api.main import create_app
from engine.access.roles import LOCAL_OPERATOR
from engine.agentic.config import agentic_enabled, load_agentic_config
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import FakeSigner
from engine.agentic.service import agentic_context
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.storage import LocalStorage

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_shipped_switch_is_off(config_root: Path, tmp_path: Path) -> None:
    assert load_agentic_config(config_root).agentic.enabled is False
    assert agentic_enabled(tmp_path) is False, "a configuration root without the file is off"


def test_no_agentic_route_and_the_same_openapi_as_before_plan_k(config_root: Path, tmp_path: Path) -> None:
    before = tmp_path / "configs-before-plan-k"
    shutil.copytree(config_root, before, ignore=shutil.ignore_patterns("agentic"))
    assert not (before / "agentic").exists()
    with_switch = create_app(config_root=config_root, data_dir=tmp_path / "a")
    without = create_app(config_root=before, data_dir=tmp_path / "b")
    assert json.dumps(with_switch.openapi(), sort_keys=True) == json.dumps(without.openapi(), sort_keys=True)
    assert not [path for path in with_switch.openapi()["paths"] if path.startswith("/agentic")]
    with TestClient(with_switch) as client:
        assert client.get("/agentic/proposals").status_code == 404
        assert client.post("/agentic/proposals/prop_1/approve", json={}).status_code == 404


def test_no_table_and_no_file_is_created(config_root: Path, tmp_path: Path) -> None:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as client:
        body = {"eligible": 1_000, "base_rate": 0.1, "holdout_shares": [0.1]}
        assert client.post("/measurement/power-preview", json=body).status_code == 200
        assert client.get("/campaigns").status_code == 200
    assert not (tmp_path / "agentic").exists()
    tables = inspect(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)).get_table_names()
    assert "agentic_decision_log" not in tables


def test_the_app_does_not_import_the_agent_core(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "from fastapi.testclient import TestClient\n"
        "from sqlmodel import SQLModel\n"
        "from api.main import create_app\n"
        f"client = TestClient(create_app(data_dir={str(tmp_path)!r}))\n"
        "assert client.get('/healthz').status_code == 200\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('engine.agentic') or m == 'api.routes.agentic')\n"
        "print(loaded, 'agentic_decision_log' in SQLModel.metadata.tables)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": "/usr/bin:/bin",
            "MARKETING_AI_CONFIG_DIR": str(REPO_ROOT / "configs"),
        },
    )
    assert completed.returncode == 0, completed.stderr
    # Only the switch's reader is imported (it reads a YAML file); no table class, no route module.
    assert completed.stdout.strip().splitlines()[-1] == (
        "['engine.agentic', 'engine.agentic.canonical', 'engine.agentic.config', 'engine.agentic.errors'] False"
    )


def test_the_entry_point_refuses_while_off(config_root: Path, tmp_path: Path) -> None:
    with pytest.raises(AgenticError) as caught:
        agentic_context(
            storage=LocalStorage(tmp_path),
            principal=LOCAL_OPERATOR,
            config_root=config_root,
            platform_engine=sqlite_engine(tmp_path / PLATFORM_DB_FILENAME),
            signer=FakeSigner(),
        )
    assert caught.value.code == "AGENTIC_DISABLED" and caught.value.status == 404
    assert (
        not (tmp_path / PLATFORM_DB_FILENAME).exists()
        or "agentic_decision_log"
        not in inspect(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)).get_table_names()
    )
