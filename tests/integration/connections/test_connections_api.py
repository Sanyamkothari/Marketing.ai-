"""`/connections` end to end (Plan H M80): create, test, browse, preview, import - and then Guided setup.

S3 is moto; the import lands as an ordinary upload, so `POST /uploads/{id}/agent-session` must work on
it exactly as on a file posted to `/uploads`. Throughout, the secret must appear in no response, no
listing, no error and no audit event.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from moto import mock_aws

from api.main import create_app
from engine.connections import snowflake as snowflake_module
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = pytest.mark.integration

USE_CASE = "targeted-advertisement"
BUCKET = "acme-exports"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
KEY_ID = "AKIAIOSFODNN7EXAMPLE"
HISTORY = generate(GenerationSpec(use_case_id=USE_CASE, rows=2500)).to_csv(index=False).encode()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key="exports/history.csv", Body=HISTORY)
        yield s3


@pytest.fixture
def client(config_root: Path, tmp_path: Path, aws: Any) -> Iterator[TestClient]:
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data")) as test_client:
        yield test_client


def _create(client: TestClient, **extra: Any) -> dict[str, Any]:
    body = {
        "kind": "s3",
        "name": "Exports",
        "config": {"bucket": BUCKET, "prefix": "exports/"},
        "secrets": {"access_key_id": KEY_ID, "secret_access_key": SECRET_KEY},
        **extra,
    }
    response = client.post("/connections", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _no_secret(text: str) -> None:
    assert SECRET_KEY not in text and KEY_ID not in text


def test_the_catalogue_lists_every_service_with_its_form(client: TestClient) -> None:
    kinds = {k["kind"]: k for k in client.get("/connections/kinds").json()["kinds"]}
    assert set(kinds) >= {
        "s3",
        "s3_compatible",
        "postgres",
        "redshift",
        "mysql",
        "snowflake",
        "bigquery",
        "azure_blob",
        "ai_service",
    }
    assert kinds["s3"]["available"] and kinds["s3"]["tier"] == "built_in"
    assert kinds["snowflake"]["tier"] == "optional" and kinds["snowflake"]["addon"] == "snowflake"
    assert kinds["ai_service"]["screen"] == "#/connections/ai/product" and not kinds["ai_service"]["creatable"]
    secret_fields = [f["name"] for f in kinds["postgres"]["fields"] if f["secret"]]
    assert secret_fields == ["password"]


def test_create_test_browse_preview_import_then_guided_setup(client: TestClient, tmp_path: Path) -> None:
    created = _create(client)
    _no_secret(json.dumps(created))
    assert created["secrets_saved"] == ["access_key_id", "secret_access_key"]
    assert created["status"] == "not_tested" and created["config"] == {"bucket": BUCKET, "prefix": "exports/"}
    cid = created["connection_id"]
    for path in (f"/connections/{cid}", "/connections"):
        response = client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        _no_secret(response.text)
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert SECRET_KEY.encode() not in path.read_bytes(), path

    tested = client.post(f"/connections/{cid}/test")
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["report"]["ok"] and body["connection"]["status"] == "connected"
    assert [s["name"] for s in body["report"]["steps"]] == [
        "reach",
        "sign_in",
        "list",
        "read_sample",
        "read_only_check",
    ]
    _no_secret(tested.text)

    listing = client.get(f"/connections/{cid}/browse").json()
    assert listing["path"] == "exports/"
    item = next(i for i in listing["items"] if i["name"] == "history.csv")
    assert item["importable"]

    preview = client.post(f"/connections/{cid}/preview", json={"path": item["path"]})
    assert preview.status_code == 200, preview.text
    assert 0 < len(preview.json()["rows"]) <= 20

    imported = client.post(
        f"/connections/{cid}/import", json={"use_case": USE_CASE, "mode": "train", "path": item["path"]}
    )
    assert imported.status_code == 201, imported.text
    upload = imported.json()
    assert set(upload) == {"upload_id", "profile"} and upload["profile"]["row_count"] == 2500
    assert imported.headers["location"] == f"/uploads/{upload['upload_id']}/profile"
    source = json.loads(
        (tmp_path / "data" / "uploads" / upload["upload_id"] / "connection_source.json").read_text()
    )
    assert source["connection_id"] == cid and source["path"] == "exports/history.csv"

    session = client.post(f"/uploads/{upload['upload_id']}/agent-session", json={"use_case": USE_CASE})
    assert session.status_code == 201, session.text
    imported_session = session.json()["session"]
    assert imported_session["status"] in {"needs_review", "ready"}, imported_session.get("stop_reason")
    # The same bytes posted to /uploads get the same advice: an import is an upload in every respect.
    posted = client.post(
        "/uploads",
        files={"file": ("history.csv", HISTORY, "text/csv")},
        data={"use_case": USE_CASE, "mode": "train"},
    )
    assert posted.status_code == 201
    assert posted.json()["profile"]["fingerprint"] == upload["profile"]["fingerprint"]
    direct = client.post(
        f"/uploads/{posted.json()['upload_id']}/agent-session", json={"use_case": USE_CASE}
    ).json()
    assert [p["title"] for p in direct["session"]["proposals"]] == [
        p["title"] for p in imported_session["proposals"]
    ]

    audit = client.get("/audit/events")
    if audit.status_code == 200:
        _no_secret(audit.text)


def test_an_update_keeps_blank_secrets_and_a_delete_forgets_everything(client: TestClient) -> None:
    cid = _create(client)["connection_id"]
    updated = client.put(
        f"/connections/{cid}",
        json={
            "name": "Renamed",
            "config": {"bucket": BUCKET},
            "secrets": {"access_key_id": "", "secret_access_key": ""},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Renamed" and updated.json()["secrets_saved"] == [
        "access_key_id",
        "secret_access_key",
    ]
    assert client.post(f"/connections/{cid}/test").json()["report"]["ok"]
    cleared = client.put(
        f"/connections/{cid}",
        json={"config": {"bucket": BUCKET}, "clear_secrets": ["access_key_id", "secret_access_key"]},
    )
    assert cleared.json()["secrets_saved"] == []
    assert client.delete(f"/connections/{cid}").status_code == 204
    assert client.get(f"/connections/{cid}").json()["detail"]["code"] == "CONNECTION_NOT_FOUND"


def test_a_bad_body_is_refused_without_repeating_a_secret(client: TestClient) -> None:
    cases = [
        {"kind": "s3", "config": {"bucket": BUCKET, "secret_access_key": SECRET_KEY}, "secrets": {}},
        {
            "kind": "s3",
            "config": {"bucket": BUCKET},
            "secrets": {"access_key_id": KEY_ID, "nonsense": SECRET_KEY},
        },
        {"kind": "s3", "config": {"bucket": BUCKET}, "secrets": [SECRET_KEY], "extra": KEY_ID},
        {"kind": "nope", "secrets": {"password": SECRET_KEY}},
    ]
    for body in cases:
        response = client.post("/connections", json=body)
        assert response.status_code == 422, response.text
        _no_secret(response.text)
    raw = client.post(
        "/connections",
        content=f'{{"kind": "s3", "secrets": {{"password": "{SECRET_KEY}"',
        headers={"content-type": "application/json"},
    )
    assert raw.status_code == 422 and raw.json()["detail"]["code"] == "BODY_INVALID"
    _no_secret(raw.text)
    huge = client.post(
        "/connections", content=b"{" + b" " * 70_000 + b"}", headers={"content-type": "application/json"}
    )
    assert huge.status_code == 413


def test_a_missing_bucket_is_a_failed_test_with_its_fix(client: TestClient) -> None:
    cid = _create(client, config={"bucket": "no-such-bucket", "region": "us-east-1"})["connection_id"]
    body = client.post(f"/connections/{cid}/test").json()
    assert not body["report"]["ok"]
    assert body["connection"]["status"] == "failed"
    assert body["connection"]["failure"] == "There is no bucket called no-such-bucket."
    assert (
        client.get("/connections").json()["connections"][0]["failure"]
        == "There is no bucket called no-such-bucket."
    )


def test_an_import_bigger_than_the_use_case_allows_leaves_nothing_behind(
    client: TestClient, aws: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes import connections as route

    cid = _create(client)["connection_id"]

    real = route.use_case_config

    def tiny(use_case: str, root: Path) -> Any:
        config = real(use_case, root)
        return config.model_copy(
            update={"validation": config.validation.model_copy(update={"max_file_size_mb": 0})}
        )

    monkeypatch.setattr(route, "use_case_config", tiny)
    response = client.post(
        f"/connections/{cid}/import", json={"use_case": USE_CASE, "path": "exports/history.csv"}
    )
    assert response.status_code == 413 and response.json()["detail"]["code"] == "CONNECTION_TOO_LARGE"
    assert not [
        p for p in (tmp_path / "data").rglob("uploads/*/*") if p.is_file()
    ], "a refused import leaves no file"
    assert (
        client.post(
            f"/connections/{cid}/import", json={"use_case": "no-such-use-case", "path": "x.csv"}
        ).status_code
        == 404
    )


def test_a_missing_add_on_is_listed_and_refused_with_the_install_command(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snowflake_module, "load_sdk", lambda _name: None)
    created = client.post(
        "/connections",
        json={
            "kind": "snowflake",
            "config": {"account": "org-acct", "warehouse": "WH", "database": "DB", "user": "r"},
            "secrets": {"password": "pw"},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "needs_addon"
    tested = client.post(f"/connections/{created.json()['connection_id']}/test")
    assert tested.status_code == 409 and tested.json()["detail"]["code"] == "CONNECTION_NEEDS_ADDON"
    assert "marketing-ai[snowflake]" in tested.json()["detail"]["message"]


def test_a_changed_key_asks_for_the_password_again(
    config_root: Path, tmp_path: Path, aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def app_with(key: str) -> TestClient:
        # Through the environment, as a deployment sets it: `create_app(settings=...)` would also
        # configure the process's logging, which is not this test's business.
        monkeypatch.setenv("MARKETING_AI_CONNECTIONS_KEY", key)
        return TestClient(create_app(config_root=config_root, data_dir=tmp_path / "data"))

    with app_with(Fernet.generate_key().decode()) as first:
        cid = _create(first)["connection_id"]
    with app_with(Fernet.generate_key().decode()) as second:
        view = second.get(f"/connections/{cid}").json()
        assert view["secrets_readable"] is False
        refused = second.post(f"/connections/{cid}/test")
        assert (
            refused.status_code == 409 and refused.json()["detail"]["code"] == "CONNECTION_SECRETS_UNREADABLE"
        )
        blank = second.put(f"/connections/{cid}", json={"config": {"bucket": BUCKET}, "secrets": {}})
        assert blank.status_code == 200 and blank.json()["secrets_readable"] is False
        again = second.put(
            f"/connections/{cid}",
            json={
                "config": {"bucket": BUCKET},
                "secrets": {"access_key_id": KEY_ID, "secret_access_key": SECRET_KEY},
            },
        )
        assert again.status_code == 200 and again.json()["secrets_readable"] is True
        assert second.post(f"/connections/{cid}/test").json()["report"]["ok"]
