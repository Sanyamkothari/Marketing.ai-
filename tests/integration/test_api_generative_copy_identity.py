"""M91 (d): copy approval records the signed-in principal, and `copy_messages.csv` can be cut to approved rows.

Before the fix `POST .../templates/{id}/approve` stored whatever `body.approved_by` said, so with
sign-in on a person could approve copy in somebody else's name. The route now reads the principal
the way the model-approval route does (`kind == "user"`); with sign-in off the typed name is kept.
`approved_only` is opt-in: without it the download is byte-for-byte what it always was.
"""

from __future__ import annotations

import io
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.access.roles import Role
from engine.config import RunMode
from engine.storage import LocalStorage
from tests.fixtures.make_run import RunSpec, write_run
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

COPY_USE_CASE = "win-back-campaign"
COPY_ROWS = 240
COPY_ALLOWED_FIELDS = ("snapshot_date", "band")
TIMEOUT_S = 20.0


def _write_copy_run(data_dir: Path, config_root: Path) -> str:
    return write_run(
        LocalStorage(data_dir),
        RunSpec(
            use_case_id=COPY_USE_CASE,
            mode=RunMode.SCORE,
            rows=COPY_ROWS,
            positive_rate=0.3,
            config_root=config_root,
        ),
    )


def _generate_copy(client: TestClient, run_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.post(
        f"/runs/{run_id}/campaign-copy",
        json={"overrides": {"allowed_fields": list(COPY_ALLOWED_FIELDS)}},
        headers=headers or {},
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        status = client.get(f"/runs/{run_id}/artefacts/copy_status.json", headers=headers or {}).json()
        if status.get("state") in ("done", "failed"):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("campaign copy did not finish")
    assert status["state"] == "done", status
    batch = client.get(f"/runs/{run_id}/artefacts/copy_batch.json", headers=headers or {})
    assert batch.status_code == 200, batch.text
    return dict(batch.json())


def _pending(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in batch["templates"] if t["status"] == "pending_review"]


def _approve_url(run_id: str, template_id: str) -> str:
    return f"/runs/{run_id}/campaign-copy/templates/{template_id}/approve"


def _messages(client: TestClient, run_id: str, headers: dict[str, str], **params: Any) -> pd.DataFrame:
    response = client.get(f"/runs/{run_id}/copy_messages.csv", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)


@pytest.fixture
def signed_in(tmp_path: Path, config_root: Path) -> Iterator[tuple[TestClient, str, dict[str, str]]]:
    """An app with sign-in on, a finished copy run, and the headers of a user who is Analyst and Approver."""
    app = local_app(tmp_path, allow_fake_ai=True)
    run_id = _write_copy_run(tmp_path, config_root)
    user_id = make_user(app, "priya.nair", [Role.ANALYST, Role.APPROVER])
    with TestClient(app) as client:
        yield client, run_id, bearer(app, user_id)


def test_with_sign_in_on_the_approval_records_the_principal_whatever_the_body_says(
    signed_in: tuple[TestClient, str, dict[str, str]],
) -> None:
    client, run_id, headers = signed_in
    batch = _generate_copy(client, run_id, headers)
    target = _pending(batch)[0]

    response = client.post(
        _approve_url(run_id, target["template_id"]), json={"approved_by": "Somebody Else"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["approved_by"] == "priya.nair"
    stored = client.get(f"/runs/{run_id}/artefacts/copy_batch.json", headers=headers).json()
    stored_template = next(t for t in stored["templates"] if t["template_id"] == target["template_id"])
    assert stored_template["approved_by"] == "priya.nair"


def test_with_sign_in_off_the_typed_name_is_still_recorded(tmp_path: Path, config_root: Path) -> None:
    run_id = _write_copy_run(tmp_path, config_root)
    with TestClient(create_app(config_root=config_root, data_dir=tmp_path)) as client:
        batch = _generate_copy(client, run_id)
        target = _pending(batch)[0]
        response = client.post(_approve_url(run_id, target["template_id"]), json={"approved_by": "Asha Rao"})
    assert response.status_code == 200, response.text
    assert response.json()["approved_by"] == "Asha Rao"


def test_approved_only_keeps_exactly_the_rows_of_approved_templates(
    signed_in: tuple[TestClient, str, dict[str, str]],
) -> None:
    client, run_id, headers = signed_in
    batch = _generate_copy(client, run_id, headers)
    pending = _pending(batch)
    assert len(pending) >= 2, "the fixture must give one template to approve and one to leave"
    approved = pending[0]["template_id"]
    client.post(_approve_url(run_id, approved), json={"approved_by": "x"}, headers=headers)

    everything = _messages(client, run_id, headers)
    default = _messages(client, run_id, headers, approved_only="false")
    only = _messages(client, run_id, headers, approved_only="true")

    assert default.equals(everything)
    assert set(everything["template_id"]) > {approved}
    assert len(only) > 0
    assert set(only["template_id"]) == {approved}
    assert len(only) == int((everything["template_id"] == approved).sum())
    assert list(only.columns) == list(everything.columns)


def test_approved_only_before_any_approval_returns_the_header_and_no_rows(
    signed_in: tuple[TestClient, str, dict[str, str]],
) -> None:
    client, run_id, headers = signed_in
    _generate_copy(client, run_id, headers)
    everything = _messages(client, run_id, headers)
    only = _messages(client, run_id, headers, approved_only="true")
    assert len(only) == 0
    assert list(only.columns) == list(everything.columns)


def test_approved_only_before_any_copy_is_still_404(
    signed_in: tuple[TestClient, str, dict[str, str]],
) -> None:
    client, run_id, headers = signed_in
    response = client.get(
        f"/runs/{run_id}/copy_messages.csv", params={"approved_only": "true"}, headers=headers
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ARTEFACT_NOT_FOUND"


def test_approved_only_rows_say_approved_and_leave_out_refused_renderings(
    signed_in: tuple[TestClient, str, dict[str, str]],
) -> None:
    client, run_id, headers = signed_in
    batch = _generate_copy(client, run_id, headers)
    approved = _pending(batch)[0]["template_id"]
    client.post(_approve_url(run_id, approved), json={"approved_by": "x"}, headers=headers)

    everything = _messages(client, run_id, headers)
    only = _messages(client, run_id, headers, approved_only="true")

    assert set(everything.loc[everything["template_id"] == approved, "status"]) == {"pending_review"}
    assert len(only) > 0
    assert set(only["status"]) == {"approved"}
    assert (only["block_reason"] == "").all()
    assert (only["rendered_text"] != "").all()


@pytest.fixture
def narrow_copy_config(tmp_path: Path, config_root: Path) -> Path:
    """A copy of `configs/` whose win-back copy may use only the columns the fixture run really has."""
    patched = tmp_path / "configs"
    shutil.copytree(config_root, patched)
    path = patched / "use_cases" / "win_back_campaign.yaml"
    text = path.read_text(encoding="utf-8")
    old = "allowed_fields: [first_name, plan_type, last_offer]"
    assert old in text
    path.write_text(
        text.replace(old, f"allowed_fields: [{', '.join(COPY_ALLOWED_FIELDS)}]"), encoding="utf-8"
    )
    return patched


def test_a_regenerated_then_approved_template_exports_its_current_text_not_the_old_rendering(
    tmp_path: Path, narrow_copy_config: Path
) -> None:
    run_id = _write_copy_run(tmp_path, narrow_copy_config)
    with TestClient(create_app(config_root=narrow_copy_config, data_dir=tmp_path)) as client:
        batch = _generate_copy(client, run_id)
        target = _pending(batch)[0]
        template_id = target["template_id"]
        before = _messages(client, run_id, {})
        old_rows = before.loc[before["template_id"] == template_id]

        # The fake backend writes the same prose for the same ask, so the ask changes: a person narrows the
        # fields copy may use between the generation and the regenerate, as a real model's reply would differ.
        config_file = narrow_copy_config / "use_cases" / "win_back_campaign.yaml"
        config_file.write_text(
            config_file.read_text(encoding="utf-8").replace(
                f"allowed_fields: [{', '.join(COPY_ALLOWED_FIELDS)}]", "allowed_fields: [band]"
            ),
            encoding="utf-8",
        )
        regenerated = client.post(f"/runs/{run_id}/campaign-copy/templates/{template_id}/regenerate")
        assert regenerated.status_code == 200, regenerated.text
        fresh = regenerated.json()
        assert fresh["text"] != target["text"], "the fixture must give a regenerate that changes the text"
        approved = client.post(_approve_url(run_id, template_id), json={"approved_by": "x"})
        assert approved.status_code == 200, approved.text

        only = _messages(client, run_id, {}, approved_only="true")
        everything = _messages(client, run_id, {})
        stored = client.get(f"/runs/{run_id}/artefacts/copy_batch.json").json()

    assert len(only) == len(old_rows) > 0
    assert set(only["template_id"]) == {template_id}
    assert not set(only["rendered_text"]) & set(old_rows["rendered_text"])
    for text in only["rendered_text"]:
        assert not _unrendered(text), text
        assert _same_prose(text, fresh), (text, fresh["text"])
    assert len(everything) == len(before), "a regenerate must not add or drop other templates' rows"
    others = everything.loc[everything["template_id"] != template_id].reset_index(drop=True)
    kept = before.loc[before["template_id"] != template_id].reset_index(drop=True)
    assert others.equals(kept)
    assert stored["messages_rendered"] == len(everything)


def _unrendered(text: str) -> bool:
    return "{{" in text


def _same_prose(rendered: str, template: dict[str, Any]) -> bool:
    """The rendering is the template's text with each `{{field}}` filled: every literal stretch survives."""
    import re

    literals = [part for part in re.split(r"{{[^}]*}}", template["text"]) if part.strip()]
    return all(part in rendered for part in literals)
