"""The Data page (Plan M, M179, DEC-1609) in jsdom, on REAL bodies.

The node tests (`production/ui/data/*.test.mjs`, sharing that directory's jsdom install as the connections and
glossary tests do) replay what the app below answered:

* the catalogue of kinds, an S3 connection that tested fine (moto) and a PostgreSQL connection whose name carries
  markup and whose test really failed at its first step (nothing listens on port 1);
* `GET /uploads` after a file posted directly and a file imported from the S3 connection - the two ways a file
  becomes an upload - and `GET /connections` for the same world.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from api.main import create_app
from tests.fixtures.make_data import GenerationSpec, generate
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "data"
USE_CASE: Final[str] = "targeted-advertisement"
BUCKET: Final[str] = "acme-exports"
HOSTILE: Final[str] = '<img src=x onerror="window.__pwned=1">Sales DB'
HOSTILE_FILE: Final[str] = "<b onmouseover=window.__pwned=1>march</b>.csv"


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def write_fixtures(root: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    bodies: dict[str, Any] = {}
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws(), TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=600))
        s3.put_object(Bucket=BUCKET, Key="exports/history.csv", Body=frame.to_csv(index=False).encode())

        bodies["kinds"] = _ok(client.get("/connections/kinds"))
        bodies["uploads_empty"] = _ok(client.get("/uploads"))
        bodies["connections_empty"] = _ok(client.get("/connections"))

        s3_created = _ok(
            client.post(
                "/connections",
                json={
                    "kind": "s3",
                    "name": "Exports",
                    "config": {"bucket": BUCKET, "prefix": "exports/"},
                    "secrets": {"access_key_id": "testing", "secret_access_key": "testing"},
                },
            ),
            201,
        )
        s3_id = s3_created["connection_id"]
        bodies["s3_tested"] = _ok(client.post(f"/connections/{s3_id}/test"))
        pg = _ok(
            client.post(
                "/connections",
                json={
                    "kind": "postgres",
                    "name": HOSTILE,
                    "config": {"host": "127.0.0.1", "port": 1, "database": "crm", "user": "reader"},
                    "secrets": {"password": "pw"},
                },
            ),
            201,
        )
        bodies["pg_tested"] = _ok(client.post(f"/connections/{pg['connection_id']}/test"))
        bodies["connections_before_import"] = _ok(client.get("/connections"))

        direct = _ok(
            client.post(
                "/uploads",
                data={"use_case": USE_CASE, "mode": "train"},
                files={"file": (HOSTILE_FILE, frame.head(300).to_csv(index=False).encode(), "text/csv")},
            ),
            201,
        )
        imported = _ok(
            client.post(
                f"/connections/{s3_id}/import",
                json={"use_case": USE_CASE, "mode": "train", "path": "exports/history.csv"},
            ),
            201,
        )
        bodies["uploads"] = _ok(client.get("/uploads"))
        bodies["connections"] = _ok(client.get("/connections"))
    bodies["ids"] = {
        "s3": s3_id,
        "pg": pg["connection_id"],
        "direct": direct["upload_id"],
        "imported": imported["upload_id"],
        "hostile": HOSTILE,
        "hostile_file": HOSTILE_FILE,
    }
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_the_fixtures_are_the_world_the_screens_are_tested_in(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs without node: what the node tests rely on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root, monkeypatch)
    ids = _read(out, "ids")
    assert _read(out, "uploads_empty") == {"uploads": []}
    assert _read(out, "s3_tested")["report"]["ok"]
    assert not _read(out, "pg_tested")["report"]["ok"]
    listed = {u["upload_id"]: u for u in _read(out, "uploads")["uploads"]}
    assert set(listed) == {ids["direct"], ids["imported"]}
    assert listed[ids["direct"]]["from_connection_id"] is None
    assert listed[ids["imported"]]["from_connection_id"] == ids["s3"]
    assert listed[ids["direct"]]["file_name"] == HOSTILE_FILE
    assert {c["status"] for c in _read(out, "connections")["connections"]} == {"connected", "failed"}
    assert "testing" not in json.dumps(_read(out, "connections")), "no secret in any body the screen sees"


def test_data_screens_in_jsdom(tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root, monkeypatch)
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
