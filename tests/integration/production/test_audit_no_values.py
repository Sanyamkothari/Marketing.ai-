"""No customer value reaches the audit trail (plan M47; DEC-705), proven the way Phase 1 proved it for logs.

`tests/unit/test_logging_audit.py` drives the stages over a frame whose every cell is a sentinel and
searches every log record for it. This does the same for the audit trail: a file whose cells all
carry `SENTINEL` is uploaded through the real API, request bodies and a sign-in attempt carry it
too, and then every place the trail can surface is searched byte by byte - the SQLite file and its
WAL, the rows as the API returns them, the CSV download and a JSON-lines export - as is every log
record the access layer wrote. The only thing allowed to depend on a value is a hash of a response.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.audit.export import EXPORT_DIRECTORY
from engine.platform_db import PLATFORM_DB_FILENAME
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration

SENTINEL = "QZLEAK"
DEMO_ID = "targeted-advertisement"


def sentinel_csv(rows: int = 40) -> bytes:
    lines = ["customer_id,snapshot_date,tenure_months,segment,converted_30d"]
    for index in range(rows):
        lines.append(
            f"{SENTINEL}-C{index:03d},2026-08-01,{index % 30 + 1},{SENTINEL}-seg-{index % 3},{index % 2}"
        )
    return ("\n".join(lines) + "\n").encode()


def test_no_uploaded_value_reaches_the_audit_trail_its_exports_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = local_app(tmp_path)
    admin = make_user(app, "admin", [Role.ADMIN])
    analyst = make_user(app, "asha", [Role.ANALYST])
    client = TestClient(app, raise_server_exceptions=False)
    analyst_headers = bearer(app, analyst)
    admin_headers = bearer(app, admin)

    with caplog.at_level(logging.DEBUG):
        uploaded = client.post(
            "/uploads",
            files={"file": (f"{SENTINEL}-export.csv", sentinel_csv(), "text/csv")},
            data={"use_case": DEMO_ID, "mode": "train"},
            headers=analyst_headers,
        )
        assert uploaded.status_code in (200, 201), uploaded.text
        upload_id = uploaded.json()["upload_id"]
        # A malformed upload whose cells are sentinels: the failure path is where values leak.
        client.post(
            "/uploads",
            files={"file": ("broken.csv", f"a,a\n{SENTINEL},{SENTINEL}\n".encode(), "text/csv")},
            data={"use_case": DEMO_ID, "mode": "train"},
            headers=analyst_headers,
        )
        client.post(
            "/runs",
            json={
                "use_case": DEMO_ID,
                "upload_id": upload_id,
                "primary_key": f"{SENTINEL}-column",
                "mode": "nonsense",
            },
            headers=analyst_headers,
        )
        client.post("/runs", json={"use_case": f"{SENTINEL}-uc"}, headers=analyst_headers)
        client.post("/auth/login", json={"username": f"{SENTINEL}-user", "password": f"{SENTINEL}-password"})
        client.post(
            "/models/m-1/approve", json={"approved_by": f"{SENTINEL} person"}, headers=analyst_headers
        )
        exported = client.post("/audit/exports", json={}, headers=admin_headers)
        assert exported.status_code == 201
        csv_text = client.get("/audit/events.csv", headers=admin_headers).text
        listed = client.get("/audit/events", params={"limit": 1000}, headers=admin_headers).text

    events = audit_log_at(tmp_path).query(AuditQuery(limit=1000))
    assert {event.action for event in events} >= {
        "uploads.create",
        "runs.create",
        "auth.login",
        "models.approve",
    }
    assert len(events) == 8  # two uploads, two runs, a sign-in, an approval, the export, the download

    stored = b"".join(path.read_bytes() for path in tmp_path.glob(f"{PLATFORM_DB_FILENAME}*"))
    exports = b"".join(path.read_bytes() for path in (tmp_path / EXPORT_DIRECTORY).glob("*.jsonl"))
    assert exports, "the export wrote nothing, so searching it would prove nothing"
    places = {
        "platform.db (+WAL)": stored,
        "export": exports,
        "csv download": csv_text.encode(),
        "event list": listed.encode(),
        "event rows": "".join(event.model_dump_json() for event in events).encode(),
    }
    # The upload itself is stored by the upload route, in `uploads/`, which is not the audit trail.
    for name, blob in places.items():
        assert SENTINEL.encode() not in blob, f"a sentinel value reached the {name}"

    access_records = [
        record
        for record in caplog.records
        if record.name.startswith(("api.access", "api.routes.auth", "api.routes.audit"))
    ]
    for record in access_records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.args)
