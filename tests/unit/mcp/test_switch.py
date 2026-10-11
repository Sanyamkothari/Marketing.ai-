"""`mcp.enabled` (Plan K M133, DEC-1404 (a)): off - the default - nothing is mounted and the MCP SDK is never
imported; on, `/mcp` is there and refuses anyone not signed in."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import create_app

PROBE = """
import sys
from api.main import create_app
app = create_app()
paths = {getattr(route, "path", None) for route in app.routes}
print("/mcp" in paths, "mcp" in sys.modules, "api.mcp.http" in sys.modules, "api.mcp.server" in sys.modules)
"""


def _root(config_root: Path, target: Path, *, mcp: bool, agentic: bool = True) -> Path:
    shutil.copytree(config_root, target)
    # Both switches live in agentic.yaml (DEC-1400 (d)); mcp.yaml keeps the shipped settings.
    (target / "agentic" / "agentic.yaml").write_text(
        f"agentic:\n  enabled: {str(agentic).lower()}\nmcp:\n  enabled: {str(mcp).lower()}\n", "utf-8"
    )
    return target


def test_off_by_default_nothing_is_mounted_and_the_sdk_is_not_imported(repo_root: Path) -> None:
    probe = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=repo_root, capture_output=True, text=True, timeout=300, check=True
    )
    assert probe.stdout.split() == ["False", "False", "False", "False"], probe.stderr[-2000:]


def test_the_core_on_and_mcp_off_mounts_nothing(config_root: Path, tmp_path: Path) -> None:
    app = create_app(config_root=_root(config_root, tmp_path / "configs", mcp=False), data_dir=tmp_path / "d")
    assert "/mcp" not in {getattr(route, "path", None) for route in app.routes}
    response = TestClient(app).post("/mcp", json={})
    assert response.status_code == 404


def test_on_mcp_is_mounted_and_asks_for_sign_in(config_root: Path, tmp_path: Path) -> None:
    app = create_app(config_root=_root(config_root, tmp_path / "configs", mcp=True), data_dir=tmp_path / "d")
    assert "/mcp" in {getattr(route, "path", None) for route in app.routes}
    with TestClient(app) as client:  # the default settings: sign-in off on a laptop
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"
    assert "/mcp" not in TestClient(app).get("/openapi.json").json()["paths"], "not an API route"
