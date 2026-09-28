"""The Connections page and Guided setup's "Pick from a connection" (Plan H M80) in jsdom, on REAL bodies.

The node tests (`production/ui/connections/*.test.mjs`, sharing that directory's jsdom install as the
agent, usecase and ops tests do) replay what the app below answered, in the order the screens ask:

* the catalogue, an S3 connection that tested fine (moto), and a PostgreSQL connection whose name
  carries markup and whose test really failed at its first step (nothing listens on port 1);
* for the picker: the connections list, the S3 listing, the preview of a file, its import - an ordinary
  upload - and the Guided-setup session the helper then starts on that upload.
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
TESTS_DIR: Final[Path] = NODE_DIR / "connections"
USE_CASE: Final[str] = "targeted-advertisement"
BUCKET: Final[str] = "acme-exports"
HOSTILE: Final[str] = '<img src=x onerror="window.__pwned=1">Sales DB'


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
        frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=2500))
        s3.put_object(Bucket=BUCKET, Key="exports/history.csv", Body=frame.to_csv(index=False).encode())
        s3.put_object(Bucket=BUCKET, Key="exports/notes.pdf", Body=b"%PDF")

        bodies["kinds"] = _ok(client.get("/connections/kinds"))
        bodies["use_case"] = _ok(client.get(f"/use-cases/{USE_CASE}"))
        bodies["runs_empty"] = _ok(client.get("/runs", params={"use_case": USE_CASE}))
        bodies["models_empty"] = _ok(client.get("/models", params={"use_case": USE_CASE}))
        bodies["ai"] = _ok(client.get("/connection/aws"))
        bodies["list_empty"] = _ok(client.get("/connections"))

        created = bodies["s3_created"] = _ok(
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
        s3_id = created["connection_id"]
        bodies["s3_tested"] = _ok(client.post(f"/connections/{s3_id}/test"))
        pg = bodies["pg_created"] = _ok(
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
        bodies["list"] = _ok(client.get("/connections"))
        bodies["browse"] = _ok(client.get(f"/connections/{s3_id}/browse"))
        bodies["preview"] = _ok(
            client.post(f"/connections/{s3_id}/preview", json={"path": "exports/history.csv"})
        )
        upload = bodies["imported"] = _ok(
            client.post(
                f"/connections/{s3_id}/import",
                json={"use_case": USE_CASE, "mode": "train", "path": "exports/history.csv"},
            ),
            201,
        )
        bodies["imported_start"] = _ok(
            client.post(f"/uploads/{upload['upload_id']}/agent-session", json={"use_case": USE_CASE}), 201
        )
    bodies["ids"] = {
        "s3": s3_id,
        "pg": pg["connection_id"],
        "upload": upload["upload_id"],
        "hostile": HOSTILE,
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
    assert _read(out, "s3_tested")["report"]["ok"]
    failed = _read(out, "pg_tested")
    assert not failed["report"]["ok"] and failed["report"]["steps"][0]["status"] == "failed"
    assert failed["connection"]["status"] == "failed" and failed["connection"]["failure"]
    assert {i["name"]: i["importable"] for i in _read(out, "browse")["items"]} == {
        "history.csv": True,
        "notes.pdf": False,
    }
    assert _read(out, "imported_start")["session"]["status"] in {"needs_review", "ready"}
    assert "testing" not in json.dumps(_read(out, "list")), "no secret in any body the screen sees"


def test_connections_screens_in_jsdom(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
