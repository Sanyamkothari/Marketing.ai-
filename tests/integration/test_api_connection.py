"""`/connection/aws`: the ordinary behaviour of the AWS connection screen's API.

`test_api_connection_security.py` is the adversary - another site, a rebinding page, a planted
symlink, a hostage thread pool. This file is the user: a person on their own laptop choosing a
profile, a colleague on the LAN who may look but not change, and a deployment that calls Bedrock as
its IAM role and cannot be pointed anywhere else from a browser.

**A secret never comes back.** Six ways of pasting one - an extra field, a secret used as a key,
one nested under `connection`, a key id as the profile name, a bare string, a body that is not JSON
- and in none of them does the secret appear in the response text or its headers. FastAPI's own
`422` repeats its input, which is why these routes do not use it; this is the test that says so.

**Nothing is cacheable.** Every response - a success, a 403, a 413, a 422 - carries
`Cache-Control: no-store`, because a header set on the injected response is dropped when a route
raises, and an error would otherwise be the one answer a cache could keep.

**The choice is where the jobs look for it.** A profile saved here is the profile the next
generative job builds its Bedrock client with.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api.routes.connection as connection_routes
from api.main import create_app
from engine import aws_connection
from engine.aws_connection import AwsConnection, CredentialSource

pytestmark = pytest.mark.integration

LOCAL_BASE = "http://127.0.0.1:8000"
ACCOUNT = "123456789012"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
KEY_ID = "AKIAIOSFODNN7EXAMPLE"


class _Credentials:
    method = "sso"


class _Client:
    def __init__(self, profile: str | None) -> None:
        self._profile = profile or "default"

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/{self._profile}"}


class _Session:
    def __init__(self, profile: str | None, region: str) -> None:
        self._profile = profile

    def get_credentials(self) -> _Credentials:
        return _Credentials()

    def client(self, name: str, config: Any = None) -> _Client:
        return _Client(self._profile)


@pytest.fixture(autouse=True)
def laptop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local install with two AWS CLI profiles, no AWS_PROFILE, and a fake AWS behind the check."""
    aws = tmp_path / "aws"
    aws.mkdir()
    (aws / "config").write_text("[default]\nregion = ap-south-1\n[profile alice]\n[profile bob]\n")
    (aws / "credentials").write_text("")
    for name in [name for name in os.environ if name.startswith("MARKETING_AI_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("MARKETING_AI_ENV", "local")
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws / "credentials"))
    monkeypatch.setattr(
        connection_routes,
        "check_connection",
        functools.partial(aws_connection.check_connection, session_factory=_Session),
    )


def _client(peer: str = "127.0.0.1", base_url: str = LOCAL_BASE) -> TestClient:
    return TestClient(create_app(), client=(peer, 50000), base_url=base_url)


@pytest.fixture
def me() -> TestClient:
    """The person at the laptop, using the page the product serves."""
    return _client()


@pytest.fixture
def colleague() -> TestClient:
    """Someone on the same network opening the laptop's URL."""
    return _client(peer="192.168.1.20", base_url="http://192.168.1.10:8000")


# ---------------------------------------------------------------------------
# The person at the laptop
# ---------------------------------------------------------------------------
def test_on_my_own_laptop_i_may_choose_and_see_my_profiles(me: TestClient) -> None:
    """Editable, with every profile name the AWS CLI knows about, and the default chain in force."""
    state = me.get("/connection/aws").json()
    assert state["editable"] is True
    assert state["locked_reason"] is None
    assert state["profiles"] == ["alice", "bob", "default"]
    assert state["connection"] == {"source": "default_chain", "profile": None}


def test_a_chosen_profile_is_remembered_and_forgetting_it_returns_to_the_default(me: TestClient) -> None:
    """Save, read back, reset: the whole life of a choice through the API."""
    saved = me.put("/connection/aws", json={"source": "profile", "profile": "alice"})
    assert saved.status_code == 200, saved.text
    assert me.get("/connection/aws").json()["connection"] == {"source": "profile", "profile": "alice"}
    reset = me.delete("/connection/aws")
    assert reset.status_code == 200
    assert me.get("/connection/aws").json()["connection"]["source"] == "default_chain"


def test_i_can_try_a_profile_before_saving_it(me: TestClient) -> None:
    """A candidate is tested as itself and nothing is written until I press save."""
    report = me.post(
        "/connection/aws/test", json={"connection": {"source": "profile", "profile": "bob"}}
    ).json()
    assert report["ok"] is True
    assert report["principal_arn"] == f"arn:aws:iam::{ACCOUNT}:user/bob"
    assert report["account_id"] == ACCOUNT
    assert me.get("/connection/aws").json()["connection"]["source"] == "default_chain"


def test_a_profile_that_is_not_on_this_machine_cannot_be_saved(me: TestClient) -> None:
    """Saving a name the AWS CLI does not know would only move the failure to the next job."""
    response = me.put("/connection/aws", json={"source": "profile", "profile": "nobody"})
    assert (response.status_code, response.json()["detail"]["code"]) == (422, "PROFILE_NOT_FOUND")


def test_a_region_that_is_not_one_is_refused(me: TestClient) -> None:
    """The region decides the endpoint hostname, so only a region-shaped name reaches boto3."""
    response = me.post("/connection/aws/test", json={"region": "evil.example.com"})
    assert (response.status_code, response.json()["detail"]["code"]) == (422, "REGION_INVALID")


def test_a_use_case_that_calls_no_model_has_nothing_to_check(me: TestClient) -> None:
    """Checking telco-churn's models would be checking nothing; it says so instead of answering vacuously."""
    response = me.post("/connection/aws/test", json={"use_case_id": "telco-churn"})
    assert (response.status_code, response.json()["detail"]["code"]) == (409, "NOT_A_GENERATIVE_USE_CASE")


# ---------------------------------------------------------------------------
# A colleague on the network
# ---------------------------------------------------------------------------
def test_a_colleague_sees_no_profiles_and_cannot_choose(colleague: TestClient) -> None:
    """The laptop's profile names are nobody else's business, and a menu they could not use is worse."""
    state = colleague.get("/connection/aws").json()
    assert (state["editable"], state["locked_reason"]) == (False, "remote_client")
    assert state["profiles"] == []
    assert state["aws_profile_env"] is None


@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_colleague_cannot_change_whose_account_is_billed(colleague: TestClient, method: str) -> None:
    """Both ways of changing the choice are refused from another machine."""
    kwargs: dict[str, Any] = {"json": {"source": "default_chain"}} if method == "put" else {}
    response = getattr(colleague, method)("/connection/aws", **kwargs)
    assert (response.status_code, response.json()["detail"]["code"]) == (403, "CONNECTION_LOCKED")


def test_a_colleague_may_test_the_connection_but_sees_it_masked(colleague: TestClient) -> None:
    """Confirming the laptop can reach Bedrock is useful; learning its owner's ARN is not needed."""
    report = colleague.post("/connection/aws/test", json={}).json()
    assert report["ok"] is True
    assert report["identity_masked"] is True
    assert report["account_id"] == "********9012"
    assert report["principal_arn"] is None


# ---------------------------------------------------------------------------
# A deployment
# ---------------------------------------------------------------------------
@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A dev deployment asked from loopback - the reverse-proxy case, which must still be locked."""
    monkeypatch.setenv("MARKETING_AI_ENV", "dev")
    return _client()


def test_a_deployment_is_locked_and_lists_nothing(deployment: TestClient) -> None:
    """It calls Bedrock as its IAM task role; no profile is offered, even to a loopback caller."""
    state = deployment.get("/connection/aws").json()
    assert (state["editable"], state["locked_reason"], state["profiles"]) == (False, "deployed", [])


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/connection/aws", {"source": "profile", "profile": "alice"}),
        ("delete", "/connection/aws", None),
        ("post", "/connection/aws/test", {"connection": {"source": "profile", "profile": "alice"}}),
    ],
)
def test_nothing_on_a_deployment_can_point_bedrock_at_another_identity(
    deployment: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Saving, forgetting and even testing a candidate are all refused; only the role is ever used."""
    kwargs: dict[str, Any] = {"json": body} if body is not None else {}
    response = getattr(deployment, method)(path, **kwargs)
    assert (response.status_code, response.json()["detail"]["code"]) == (403, "CONNECTION_LOCKED")


def test_a_deployment_can_still_confirm_it_reaches_aws(deployment: TestClient) -> None:
    """Testing what is in force is allowed, and masked, because no caller of a deployment is its owner."""
    report = deployment.post("/connection/aws/test", json={}).json()
    assert report["ok"] is True and report["identity_masked"] is True


# ---------------------------------------------------------------------------
# A secret never comes back
# ---------------------------------------------------------------------------
PASTED: list[tuple[str, dict[str, Any]]] = [
    ("an extra field", {"json": {"source": "profile", "profile": "alice", "aws_secret_access_key": SECRET}}),
    ("a secret as a key", {"json": {SECRET: "x"}}),
    ("a key id as the profile", {"json": {"source": "profile", "profile": KEY_ID}}),
    ("a bare string", {"content": f'"{SECRET}"', "headers": {"content-type": "application/json"}}),
    ("not json at all", {"content": f"key={SECRET}", "headers": {"content-type": "application/json"}}),
]


@pytest.mark.parametrize(("label", "request_kwargs"), PASTED, ids=[label for label, _ in PASTED])
def test_a_pasted_secret_is_refused_and_never_repeated(
    me: TestClient, label: str, request_kwargs: dict[str, Any]
) -> None:
    """Whatever shape the paste takes, the answer is a 422 that does not contain what was pasted."""
    response = me.put("/connection/aws", **request_kwargs)
    assert response.status_code == 422, label
    exposed = response.text + " ".join(f"{name}: {value}" for name, value in response.headers.items())
    assert SECRET not in exposed and KEY_ID not in exposed, f"{label} was echoed back"


def test_a_secret_nested_under_the_candidate_is_refused_and_never_repeated(me: TestClient) -> None:
    """A key one level down is still a key pasted into a web page."""
    body = {"connection": {"source": "default_chain", "aws_secret_access_key": SECRET}}
    response = me.post("/connection/aws/test", json=body)
    assert (response.status_code, response.json()["detail"]["code"]) == (422, "CREDENTIALS_NOT_ACCEPTED")
    assert SECRET not in response.text


def test_a_pasted_key_is_told_what_to_do_instead_and_to_rotate_it(me: TestClient) -> None:
    """The refusal is useful: it names the right way, and treats the pasted key as exposed."""
    response = me.put("/connection/aws", json={"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET})
    detail = response.json()["detail"]
    assert detail["code"] == "CREDENTIALS_NOT_ACCEPTED"
    assert "aws configure" in detail["message"] and "rotate" in detail["message"]


# ---------------------------------------------------------------------------
# Nothing is cacheable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("who", "method", "path", "kwargs", "status"),
    [
        ("me", "get", "/connection/aws", {}, 200),
        ("me", "put", "/connection/aws", {"json": {"source": "default_chain"}}, 200),
        ("me", "post", "/connection/aws/test", {"json": {}}, 200),
        ("me", "put", "/connection/aws", {"json": {"aws_secret_access_key": SECRET}}, 422),
        ("me", "put", "/connection/aws", {"content": "x" * 5000}, 413),
        ("me", "post", "/connection/aws/test", {"json": {"region": "nope"}}, 422),
        ("colleague", "delete", "/connection/aws", {}, 403),
    ],
)
def test_every_answer_is_no_store(
    me: TestClient,
    colleague: TestClient,
    who: str,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    status: int,
) -> None:
    """Successes and errors alike: an identity is not something a shared browser or proxy should keep."""
    response = getattr(me if who == "me" else colleague, method)(path, **kwargs)
    assert response.status_code == status
    assert response.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# The choice is where the jobs look for it
# ---------------------------------------------------------------------------
def test_the_saved_profile_is_the_one_a_job_builds_its_client_with(me: TestClient) -> None:
    """Saving here changes whose credentials the next generative job uses - nothing else has to."""
    me.put("/connection/aws", json={"source": "profile", "profile": "alice"})
    assert aws_connection.profile_in_force() == "alice"
    me.delete("/connection/aws")
    assert aws_connection.profile_in_force() is None


def test_the_file_a_choice_writes_holds_only_the_choice(me: TestClient, tmp_path: Path) -> None:
    """No account and no ARN are persisted - only what `AwsConnection` can hold."""
    me.post("/connection/aws/test", json={})
    me.put("/connection/aws", json={"source": "profile", "profile": "bob"})
    written = (tmp_path / "data" / "local" / "aws_connection.json").read_text()
    assert AwsConnection.model_validate_json(written) == AwsConnection(
        source=CredentialSource.PROFILE, profile="bob"
    )
    assert ACCOUNT not in written
