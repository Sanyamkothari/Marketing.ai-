"""The production entry points carry the connectors: the worker runs their jobs and holds sends on
stale data, and the API serves the Connect routes over the same engine (mounted under /v2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from autonomy.connectors.sync import JOB_KINDS
from autonomy.runtime.service import build_worker
from autonomy.runtime.settings import RuntimeSettings, build_connector_engine
from autonomy.runtime.stores import SqlAlertStore
from autonomy.runtime.worker import GATEWAY_KIND, SEND_KIND, SendHandler
from autonomy.store import SqlEventStore, memory_engine

KEY = "zuFhGz46VLdLK_sr2rzzaQLE5Kowyi6HZkQNzmIihLo="  # a Fernet key made for this test only


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "AUTONOMY_DATABASE_URL": f"sqlite:///{tmp_path / 'autonomy.db'}",
        "AUTONOMY_CONNECTORS": "true",
        "AUTONOMY_BUSINESS_ID": "biz_1",
        "MARKETING_AI_CONNECTIONS_KEY": KEY,
        **extra,
    }


def test_connectors_without_the_encryption_key_refuse_to_start() -> None:
    db = memory_engine()
    settings = RuntimeSettings(connectors=True)
    with pytest.raises(ValueError, match="MARKETING_AI_CONNECTIONS_KEY"):
        build_connector_engine(
            settings, db=db, events=SqlEventStore(db), alerts=SqlAlertStore(db), environ={}
        )


def test_the_worker_runs_the_connector_jobs_and_holds_sends_on_stale_data(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    worker = build_worker(environ=env(tmp_path), start=datetime(2026, 10, 5, 8, 0, tzinfo=UTC))
    assert set(JOB_KINDS) <= set(worker.handlers)
    assert {"klaviyo_delivery_watch", "shopify_privacy"} <= set(worker.handlers)
    sender = worker.handlers[SEND_KIND]
    assert isinstance(sender, SendHandler) and sender.hold is not None and sender.queue is not None
    assert GATEWAY_KIND in worker.handlers  # connected shops send through the gateway (Klaviyo, then SES)


def test_the_production_api_serves_connect_over_the_shared_engine(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.v2.app import create_production_app, mount_production

    app = create_production_app(env(tmp_path))
    with TestClient(app) as client:
        body = client.get("/v2/connectors").json()
        assert {p["kind"] for p in body["platforms"]} >= {"shopify", "klaviyo"}
    host = FastAPI()
    mount_production(host, env(tmp_path, AUTONOMY_V2_API_TOKEN="op-token"))
    with TestClient(host) as client:
        # Outside the host's access control: everything but platform callbacks needs the operator token.
        assert client.get("/v2/connectors").status_code == 401
        assert client.post("/v2/goals/g/approve").status_code == 401
        assert client.post("/v2/runtime/tick").status_code == 401
        assert client.get("/v2/connectors", headers={"Authorization": "Bearer op-token"}).status_code == 200
        assert client.get("/v2/connectors", headers={"Authorization": "Bearer wrong"}).status_code == 401
        # Platform callbacks reach their own signature checks (refused there, not by the guard).
        guard = "Sign in with the v2 operator token."
        hook = client.post("/v2/connectors/webhooks/shopify", content=b"{}")
        assert hook.status_code < 500 and guard not in hook.text
        callback = client.get("/v2/connectors/shopify/callback", follow_redirects=False)
        assert guard not in callback.text
        assert guard in client.get("/v2/connectors").text


def test_the_mounted_api_refuses_operator_routes_when_no_token_is_set(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.v2.app import mount_production

    host = FastAPI()
    mount_production(host, env(tmp_path))
    with TestClient(host) as client:
        assert client.get("/v2/connectors").status_code == 503
        assert client.delete("/v2/connectors/connections/c1").status_code == 503


def test_api_main_mounts_v2_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("sqlmodel", reason="api.main needs the full v1 environment")
    from fastapi import FastAPI

    import api.main as main

    monkeypatch.delenv("AUTONOMY_V2_API", raising=False)
    plain = FastAPI()
    main._install_autonomy_v2(plain)
    assert not any(getattr(r, "path", "") == "/v2" for r in plain.routes)
