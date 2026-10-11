"""Plan M M184 (DEC-1614 (d), (e)): copy approval keeps separation of duties and, with agentic on, the ledger.

* With sign-in on, the person who asked for the copy (or for one template's words again) may not approve it:
  `403 SEPARATION_OF_DUTIES`, the rule model approval has (DEC-862). Another Approver may.
* With `agentic.enabled` on, every copy approval is a signed `copy_template.approved` entry on the decision ledger
  (the template id and the sha256 of its words, never the words), written before the approval is stored; with
  sign-in off it says `identity_verified: false`. With agentic off nothing is written there.
* With sign-in off nothing is refused: the local operator is everyone, as before.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.agentic.canonical import digest
from engine.agentic.ledger import DecisionLedger, signer_for_data_dir, verify_ledger
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from tests.integration.production.access_support import FAST_ITERATIONS, bearer, make_user
from tests.integration.test_api_generative_copy_identity import (
    COPY_ALLOWED_FIELDS,
    _approve_url,
    _generate_copy,
    _pending,
    _write_copy_run,
)

pytestmark = pytest.mark.integration


def _root(config_root: Path, target: Path, *, agentic: bool) -> Path:
    """`configs/`, agentic on or off, with copy allowed only the fixture run's own columns (so a regenerate
    renders every row, as `test_api_generative_copy_identity.narrow_copy_config` does)."""
    shutil.copytree(config_root, target)
    if agentic:
        (target / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: true\n", encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    text = path.read_text(encoding="utf-8")
    old = "allowed_fields: [first_name, plan_type, last_offer]"
    assert old in text
    path.write_text(
        text.replace(old, f"allowed_fields: [{', '.join(COPY_ALLOWED_FIELDS)}]"), encoding="utf-8"
    )
    return target


def _app(data_dir: Path, root: Path, *, sign_in: bool) -> Any:
    """An app on `root`; with sign-in on, built as `access_support.local_app` builds one."""
    app = create_app(config_root=root, data_dir=data_dir)
    if sign_in:
        settings = Settings(auth_mode="local", env="local", data_dir=data_dir, allow_fake_ai=True)
        app.state.settings = settings
        app.state.user_store = SqlUserStore(
            sqlite_engine(data_dir / PLATFORM_DB_FILENAME),
            session_ttl_seconds=settings.auth_session_ttl_seconds,
            iterations=FAST_ITERATIONS,
        )
    return app


def _copy_entries(data_dir: Path) -> list[Any]:
    engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
    ledger = DecisionLedger(engine, signer=None, policy_version="unused")
    return [entry for entry in ledger.entries() if entry.kind == "copy_template.approved"]


def test_the_person_who_asked_for_the_copy_cannot_approve_it(tmp_path: Path, config_root: Path) -> None:
    root = _root(config_root, tmp_path / "configs", agentic=False)
    data = tmp_path / "data"
    run_id = _write_copy_run(data, root)
    app = _app(data, root, sign_in=True)
    asha = make_user(app, "asha.rao", [Role.ANALYST, Role.APPROVER])
    ravi = make_user(app, "ravi.iyer", [Role.APPROVER])
    with TestClient(app) as client:
        batch = _generate_copy(client, run_id, bearer(app, asha))
        assert batch["requested_by"] == asha
        target = _pending(batch)[0]["template_id"]

        refused = client.post(
            _approve_url(run_id, target), json={"approved_by": "x"}, headers=bearer(app, asha)
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
        stored = client.get(f"/runs/{run_id}/artefacts/copy_batch.json", headers=bearer(app, asha)).json()
        assert (
            next(t for t in stored["templates"] if t["template_id"] == target)["status"] == "pending_review"
        )

        approved = client.post(
            _approve_url(run_id, target), json={"approved_by": "x"}, headers=bearer(app, ravi)
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_by"] == "ravi.iyer"
    assert _copy_entries(data) == [], "agentic is off: nothing is written to the decision ledger"


def test_whoever_regenerated_a_template_cannot_approve_its_new_words(
    tmp_path: Path, config_root: Path
) -> None:
    root = _root(config_root, tmp_path / "configs", agentic=False)
    data = tmp_path / "data"
    run_id = _write_copy_run(data, root)
    app = _app(data, root, sign_in=True)
    asha = make_user(app, "asha.rao", [Role.ANALYST, Role.APPROVER])
    ravi = make_user(app, "ravi.iyer", [Role.ANALYST, Role.APPROVER])
    with TestClient(app) as client:
        batch = _generate_copy(client, run_id, bearer(app, asha))
        target = _pending(batch)[0]["template_id"]
        again = client.post(
            f"/runs/{run_id}/campaign-copy/templates/{target}/regenerate", headers=bearer(app, ravi)
        )
        assert again.status_code == 200, again.text
        assert again.json()["regenerated_by"] == ravi

        refused = client.post(
            _approve_url(run_id, target), json={"approved_by": "x"}, headers=bearer(app, ravi)
        )
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"
        approved = client.post(
            _approve_url(run_id, target), json={"approved_by": "x"}, headers=bearer(app, asha)
        )
        assert approved.status_code == 200, approved.text


def test_with_agentic_on_each_approval_is_signed_on_the_ledger(tmp_path: Path, config_root: Path) -> None:
    root = _root(config_root, tmp_path / "configs", agentic=True)
    data = tmp_path / "data"
    run_id = _write_copy_run(data, root)
    app = _app(data, root, sign_in=True)
    asha = make_user(app, "asha.rao", [Role.ANALYST])
    ravi = make_user(app, "ravi.iyer", [Role.APPROVER])
    with TestClient(app) as client:
        batch = _generate_copy(client, run_id, bearer(app, asha))
        template = _pending(batch)[0]
        approved = client.post(
            _approve_url(run_id, template["template_id"]),
            json={"approved_by": "x"},
            headers=bearer(app, ravi),
        )
        assert approved.status_code == 200, approved.text

    (entry,) = _copy_entries(data)
    assert entry.signature, "an approval is signed"
    assert entry.actor == ravi
    assert entry.body["run_id"] == run_id and entry.body["template_id"] == template["template_id"]
    assert entry.body["template_sha256"] == digest({"subject": template["subject"], "text": template["text"]})
    assert entry.body["decided_by"] == ravi and entry.body["proposed_by"] == asha
    assert "identity_verified" not in entry.body, "a signed-in decision is not marked"
    assert template["text"] not in str(entry.body), "never the words"
    assert verify_ledger(
        sqlite_engine(data / PLATFORM_DB_FILENAME), signer_for_data_dir(data, create=False)
    ).ok


def test_with_sign_in_off_nothing_is_refused_and_the_ledger_says_who_is_not_known(
    tmp_path: Path, config_root: Path
) -> None:
    root = _root(config_root, tmp_path / "configs", agentic=True)
    data = tmp_path / "data"
    run_id = _write_copy_run(data, root)
    with TestClient(_app(data, root, sign_in=False)) as client:
        batch = _generate_copy(client, run_id)
        assert "requested_by" not in batch, "with sign-in off nobody is recorded as asking"
        target = _pending(batch)[0]["template_id"]
        approved = client.post(_approve_url(run_id, target), json={"approved_by": "Asha Rao"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_by"] == "Asha Rao"
    (entry,) = _copy_entries(data)
    assert entry.body["identity_verified"] is False
