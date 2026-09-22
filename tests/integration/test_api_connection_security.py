"""The attacks the security review of `/connection/aws` found, each replayed against the fixed code.

**Why this file exists apart from the connection tests.** `test_api_connection.py` proves the feature
does what it promises to a person using it; this file proves it does nothing for anyone else. Every
test here began as a reproduction that worked against the first version of the routes, so each one
is named for the attack and fails if the hole reopens.

**The main finding was a web page.** The API is unauthenticated and CORS is open on a laptop, and a
page from any site, open in the same browser, sends its requests from loopback. Before the fix such
a page could list the operator's profiles, read every identity unmasked with a `text/plain` POST that
needs no preflight, and `PUT` the profile Bedrock is called as. A DNS-rebinding page could do the same
without CORS at all. The fix makes the `Host`, the browser's `Origin` and the absence of forwarding
part of "from this machine"; the page the product itself serves still works, and that is proved too.

**The rest were at the edges of the body and the process.** An oversized body was buffered whole
before its length was checked, a deep one was a bare `500`, a secret access key is a valid profile
name about half the time and was quoted back as one, a duplicated field could hide a key, the
connection check could hold every worker thread the API has, the saved file was written through a
predictable name, and on `prod` both the connection screen and every generative job failed with a
`503` because they rebuilt the settings from an environment that is not a valid prod configuration.

**How identity is faked.** `check_connection` is wrapped with a session factory that answers
`GetCallerIdentity` with a fixed account, so the tests can see exactly what a response would have
revealed without any AWS call; the profiles are real entries in a throwaway `AWS_CONFIG_FILE`.
"""

from __future__ import annotations

import asyncio
import functools
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import api.routes.connection as connection_routes
from api.main import create_app
from engine import aws_connection
from engine.aws_connection import AwsConnection, CredentialSource, connection_path, load_connection
from engine.settings import Settings

pytestmark = pytest.mark.integration

LOCAL_BASE = "http://127.0.0.1:8000"
LOOPBACK_PEER = ("127.0.0.1", 50000)
EVIL_ORIGIN = "https://evil.example"
ACCOUNT = "123456789012"
SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEYab"
"""Forty characters, mixed case and digits, and no `/`: a valid profile name as well as a secret."""


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------
class _FakeCredentials:
    method = "shared-credentials-file"


class _FakeClient:
    def __init__(self, profile: str | None) -> None:
        self._profile = profile

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/{self._profile or 'default'}"}

    def get_foundation_model_availability(self, modelId: str) -> dict[str, str]:  # noqa: N803 - boto3's name
        return {
            "regionAvailability": "AVAILABLE",
            "entitlementAvailability": "AVAILABLE",
            "authorizationStatus": "AUTHORIZED",
        }


class _FakeSession:
    def __init__(self, profile: str | None, region: str) -> None:
        self._profile = profile

    def get_credentials(self) -> _FakeCredentials:
        return _FakeCredentials()

    def client(self, name: str, config: Any = None) -> _FakeClient:
        return _FakeClient(self._profile)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def local_machine(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A laptop with two profiles, no AWS_PROFILE, and a fake AWS behind the connection check."""
    aws = tmp_path / "aws"
    aws.mkdir()
    (aws / "config").write_text("[default]\nregion = ap-south-1\n[profile dev]\n[profile prod-admin]\n")
    (aws / "credentials").write_text("")
    for name in [name for name in os.environ if name.startswith("MARKETING_AI_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("MARKETING_AI_ENV", "local")
    monkeypatch.setenv("MARKETING_AI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws / "credentials"))
    monkeypatch.setattr(
        connection_routes,
        "check_connection",
        functools.partial(aws_connection.check_connection, session_factory=_FakeSession),
    )


@pytest.fixture
def client(config_root: Path) -> Iterator[TestClient]:
    """The browser on the laptop: a loopback peer, talking to the page the product serves."""
    app = create_app(config_root=config_root)
    with TestClient(app, client=LOOPBACK_PEER, base_url=LOCAL_BASE) as test_client:
        yield test_client


def _saved(data_dir: Path) -> str | None:
    path = data_dir / "local" / "aws_connection.json"
    return path.read_text() if path.exists() else None


def _code(response: httpx.Response) -> str:
    return str(response.json()["detail"]["code"])


# ---------------------------------------------------------------------------
# A web page in the same browser
# ---------------------------------------------------------------------------
def test_a_page_from_another_site_cannot_switch_the_profile(client: TestClient, data_dir: Path) -> None:
    """A cross-origin PUT from loopback is refused, and nothing is written."""
    response = client.put(
        "/connection/aws",
        json={"source": "profile", "profile": "prod-admin"},
        headers={"Origin": EVIL_ORIGIN},
    )
    assert response.status_code == 403
    assert _code(response) == "CONNECTION_LOCKED"
    assert response.headers["cache-control"] == "no-store"
    assert _saved(data_dir) is None


def test_a_page_from_another_site_cannot_forget_the_profile(client: TestClient, data_dir: Path) -> None:
    """A cross-origin DELETE cannot put the operator back on the default chain either."""
    assert client.put("/connection/aws", json={"source": "profile", "profile": "dev"}).status_code == 200
    response = client.delete("/connection/aws", headers={"Origin": EVIL_ORIGIN})
    assert response.status_code == 403
    assert _saved(data_dir) is not None


def test_a_page_from_another_site_is_not_shown_the_profile_list(client: TestClient) -> None:
    """A cross-origin GET is answered as for another machine: no profiles, no AWS_PROFILE."""
    body = client.get("/connection/aws", headers={"Origin": EVIL_ORIGIN}).json()
    assert body["editable"] is False
    assert body["locked_reason"] == "remote_client"
    assert body["profiles"] == []
    assert body["aws_profile_env"] is None


def test_a_text_plain_test_from_another_site_sees_only_a_masked_identity(client: TestClient) -> None:
    """The POST a page can send without a preflight gets the masked report, never the account or ARN."""
    response = client.post(
        "/connection/aws/test", content="{}", headers={"Origin": EVIL_ORIGIN, "Content-Type": "text/plain"}
    )
    assert response.status_code == 200
    assert response.json()["identity_masked"] is True
    assert ACCOUNT not in response.text


def test_a_page_from_another_site_cannot_test_a_profile_of_its_choosing(client: TestClient) -> None:
    """Naming a candidate profile is a choice, so a cross-origin page is refused it."""
    response = client.post(
        "/connection/aws/test",
        content='{"connection": {"source": "profile", "profile": "prod-admin"}}',
        headers={"Origin": EVIL_ORIGIN, "Content-Type": "text/plain"},
    )
    assert response.status_code == 403
    assert ACCOUNT not in response.text


def test_a_dns_rebinding_page_is_treated_as_another_machine(client: TestClient, data_dir: Path) -> None:
    """A same-origin page under an attacker's hostname sends that hostname as `Host`, and is locked."""
    rebound = {"Host": "rebind.evil.example:8000"}
    assert client.get("/connection/aws", headers=rebound).json()["profiles"] == []
    response = client.put("/connection/aws", json={"source": "profile", "profile": "dev"}, headers=rebound)
    assert response.status_code == 403
    assert _saved(data_dir) is None


@pytest.mark.parametrize(
    "origin", ["null", "http://127.0.0.1.evil.example", "file://", "http://evil@127.0.0.1"]
)
def test_an_origin_that_merely_mentions_loopback_is_not_local(client: TestClient, origin: str) -> None:
    """`null`, a lookalike hostname and userinfo tricks all fail the origin check."""
    response = client.put("/connection/aws", json={}, headers={"Origin": origin})
    assert response.status_code == 403


def test_a_forwarded_request_cannot_choose_even_from_loopback(client: TestClient) -> None:
    """A request that came through a proxy or tunnel is not local, whatever its peer says."""
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        response = client.put("/connection/aws", json={}, headers={header: "127.0.0.1"})
        assert response.status_code == 403, header


def test_a_header_sent_twice_is_not_trusted(client: TestClient) -> None:
    """Two `Origin` headers name no single page, so neither is believed."""
    response = client.put(
        "/connection/aws",
        json={},
        headers=[("Origin", "http://127.0.0.1:8000"), ("Origin", EVIL_ORIGIN)],  # type: ignore[arg-type]
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("localhost:8000", "http://localhost:8000"),
        ("[::1]:8000", "http://[::1]:8000"),
        ("localhost:8000", None),
    ],
)
def test_the_page_the_product_serves_can_still_choose_a_profile(
    config_root: Path, data_dir: Path, host: str, origin: str | None
) -> None:
    """The fix locks out other pages, not the product's own screen on loopback."""
    # `Host` is set by hand because the test client cannot parse an IPv6 base URL.
    headers = {"Host": host, **({"Origin": origin} if origin else {})}
    with TestClient(create_app(config_root=config_root), client=LOOPBACK_PEER, base_url=LOCAL_BASE) as local:
        response = local.put("/connection/aws", json={"source": "profile", "profile": "dev"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["editable"] is True
    assert load_connection(Settings.from_env()) == AwsConnection(
        source=CredentialSource.PROFILE, profile="dev"
    )


@pytest.mark.parametrize(
    ("authority", "local"),
    [
        ("localhost", True),
        ("LOCALHOST:8000", True),
        ("127.0.0.1:8000", True),
        ("127.8.9.10", True),
        ("[::1]:8000", True),
        (None, True),
        ("localhost.evil.example", False),
        ("127.0.0.1.evil.example:8000", False),
        ("evil.example@127.0.0.1", False),
        ("0.0.0.0:8000", False),
        ("192.168.1.10:8000", False),
        ("", False),
        ("localhost:8000/path", False),
    ],
)
def test_only_a_host_that_names_this_machine_is_local(authority: str | None, local: bool) -> None:
    """The `Host` check accepts loopback names and addresses and nothing that only resembles one."""
    assert aws_connection.is_local_authority(authority) is local


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------
def test_an_oversized_body_is_refused_before_it_is_read(config_root: Path) -> None:
    """A chunked body is abandoned at the limit instead of being buffered whole."""
    app = create_app(config_root=config_root)
    received = 0

    async def receive() -> dict[str, Any]:
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b" " * 65536, "more_body": received < 1000}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "path": "/connection/aws",
        "raw_path": b"/connection/aws",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": LOOPBACK_PEER,
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 413
    assert received == 1


def test_a_declared_oversized_body_is_refused_from_its_length(client: TestClient) -> None:
    """A `Content-Length` over the limit is enough; nothing needs to be read."""
    response = client.put("/connection/aws", content=b"{" + b" " * 5000 + b"}")
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    ["[" * 2000 + "]" * 2000, '{"a":' * 600 + "1" + "}" * 600, "[" * 600 + "]" * 600],
    ids=["past-the-json-parser", "deep-object", "deep-array"],
)
def test_a_deeply_nested_body_is_a_422_not_a_500(client: TestClient, body: str) -> None:
    """Nesting is refused as malformed, with the same no-store error as any other bad body."""
    response = client.put("/connection/aws", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert _code(response) == "BODY_INVALID"
    assert response.headers["cache-control"] == "no-store"


def test_a_secret_access_key_given_as_a_profile_name_is_never_quoted_back(client: TestClient) -> None:
    """A secret that is also a valid profile name is refused as a credential, not echoed as a profile."""
    assert aws_connection.valid_profile_name(SECRET_ACCESS_KEY)
    for path, body in (
        ("/connection/aws", {"source": "profile", "profile": SECRET_ACCESS_KEY}),
        ("/connection/aws/test", {"connection": {"source": "profile", "profile": SECRET_ACCESS_KEY}}),
    ):
        response = client.request("PUT" if path.endswith("aws") else "POST", path, json=body)
        assert response.status_code == 422
        assert _code(response) == "CREDENTIALS_NOT_ACCEPTED"
        assert SECRET_ACCESS_KEY not in response.text


def test_a_session_token_anywhere_is_refused_as_a_credential(client: TestClient) -> None:
    """A long base64 run is a session token, and is never repeated."""
    token = "IQoJb3JpZ2luX2VjE" + "A" * 120 + "/xyz+=="
    response = client.post("/connection/aws/test", json={"use_case_id": token})
    assert _code(response) == "CREDENTIALS_NOT_ACCEPTED"
    assert token not in response.text


def test_a_key_hidden_behind_a_duplicated_field_is_still_recognised(client: TestClient) -> None:
    """JSON keeps the last of two equal keys; the key id in the first is caught all the same."""
    body = '{"source": "default_chain", "profile": "AKIAIOSFODNN7EXAMPLE", "profile": null}'
    response = client.put("/connection/aws", content=body, headers={"Content-Type": "application/json"})
    assert _code(response) == "CREDENTIALS_NOT_ACCEPTED"
    assert "AKIAIOSFODNN7EXAMPLE" not in response.text


def test_a_duplicated_field_is_refused_rather_than_half_read(client: TestClient, data_dir: Path) -> None:
    """Two values for one field is a malformed body, not a choice between them."""
    body = '{"source": "profile", "profile": "prod-admin", "source": "default_chain", "profile": null}'
    response = client.put("/connection/aws", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert _code(response) == "BODY_INVALID"
    assert "prod-admin" not in response.text
    assert _saved(data_dir) is None


def test_an_unknown_use_case_id_is_not_repeated_in_the_error(client: TestClient) -> None:
    """The error names the problem, not the id the caller sent, and is not cacheable."""
    response = client.post("/connection/aws/test", json={"use_case_id": "a-value-only-the-caller-knows"})
    assert response.status_code == 404
    assert _code(response) == "USE_CASE_NOT_FOUND"
    assert "a-value-only-the-caller-knows" not in response.text
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------
def test_connection_checks_beyond_the_limit_are_refused_rather_than_queued(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held-open checks cannot exhaust the worker threads: the one over the limit gets 429 at once."""
    release = threading.Event()
    entered = threading.Semaphore(0)
    limit = connection_routes._MAX_CONCURRENT_CHECKS

    def held_check(*_args: Any, **_kwargs: Any) -> aws_connection.ConnectionReport:
        entered.release()
        release.wait(10)
        return aws_connection.ConnectionReport(
            ok=True, source=CredentialSource.DEFAULT_CHAIN, region="ap-south-1", identity_masked=True
        )

    monkeypatch.setattr(connection_routes, "check_connection", held_check)
    app = create_app(config_root=config_root)

    async def scenario() -> tuple[list[httpx.Response], httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app, client=("10.0.0.9", 1))
        async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE) as http:
            held = [asyncio.create_task(http.post("/connection/aws/test", json={})) for _ in range(limit)]
            for _ in range(limit):
                assert await asyncio.to_thread(entered.acquire, timeout=10)
            refused = await http.post("/connection/aws/test", json={})
            release.set()
            finished = list(await asyncio.gather(*held))
            after = await http.post("/connection/aws/test", json={})
            return finished, refused, after

    finished, refused, after = asyncio.run(scenario())
    assert refused.status_code == 429
    assert _code(refused) == "CHECK_BUSY"
    assert refused.headers["retry-after"] == "5"
    assert refused.headers["cache-control"] == "no-store"
    assert [response.status_code for response in finished] == [200] * limit
    assert after.status_code == 200


def test_an_aws_error_code_that_is_not_code_shaped_is_not_repeated() -> None:
    """The availability detail repeats AWS's error code only when it looks like one."""
    from botocore.exceptions import ClientError

    class RefusingClient:
        def get_foundation_model_availability(self, modelId: str) -> None:  # noqa: N803 - boto3's name
            raise ClientError({"Error": {"Code": "Credential=AKIA... scope", "Message": "x"}}, "Get")

    class RefusingSession:
        def client(self, name: str, config: Any = None) -> RefusingClient:
            return RefusingClient()

    check = aws_connection._check_model(RefusingSession(), "generation", "some.model", "ap-south-1")
    assert "Credential" not in check.detail
    assert "no code" in check.detail


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
def _local_settings() -> Settings:
    return Settings.from_env()


def test_the_choice_is_never_written_through_a_planted_symlink(data_dir: Path, tmp_path: Path) -> None:
    """A symlink at the old, predictable temporary name no longer redirects the write."""
    settings = _local_settings()
    target = connection_path(settings)
    target.parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("important\n")
    target.with_suffix(".tmp").symlink_to(victim)
    aws_connection.save_connection(settings, AwsConnection(source=CredentialSource.PROFILE, profile="dev"))
    assert victim.read_text() == "important\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_loose_leftover_temporary_file_cannot_loosen_the_saved_file(data_dir: Path) -> None:
    """The saved file is 0600 even when a world-readable file sits at the old temporary name."""
    settings = _local_settings()
    target = connection_path(settings)
    target.parent.mkdir(parents=True)
    leftover = target.with_suffix(".tmp")
    leftover.write_text("{}")
    leftover.chmod(0o644)
    aws_connection.save_connection(settings, AwsConnection(source=CredentialSource.PROFILE, profile="dev"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.glob(".aws_connection.json.*")) == []


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="file ownership is a POSIX notion")
def test_a_choice_file_others_can_write_is_ignored(data_dir: Path) -> None:
    """A group- or world-writable file could have been planted, so it is read as no choice at all."""
    settings = _local_settings()
    target = connection_path(settings)
    target.parent.mkdir(parents=True)
    target.write_text('{"source": "profile", "profile": "prod-admin"}')
    target.chmod(0o666)
    assert load_connection(settings) == AwsConnection()
    target.chmod(0o600)
    assert load_connection(settings).profile == "prod-admin"


def test_a_symlinked_choice_file_is_ignored(data_dir: Path, tmp_path: Path) -> None:
    """The choice is read only from a regular file, never through a link to somewhere else."""
    settings = _local_settings()
    target = connection_path(settings)
    target.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"source": "profile", "profile": "prod-admin"}')
    elsewhere.chmod(0o600)
    target.symlink_to(elsewhere)
    assert load_connection(settings) == AwsConnection()


# ---------------------------------------------------------------------------
# A deployment
# ---------------------------------------------------------------------------
@pytest.fixture
def prod_environment(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """The three variables a prod task definition carries, and a choice file planted on its disk."""
    monkeypatch.setenv("MARKETING_AI_ENV", "prod")
    monkeypatch.setenv("MARKETING_AI_SETTINGS_SOURCE", "aws")
    monkeypatch.setenv("MARKETING_AI_AWS_REGION", "ap-south-1")
    planted = data_dir / "local" / "aws_connection.json"
    planted.parent.mkdir(parents=True)
    planted.write_text('{"source": "profile", "profile": "prod-admin"}')
    planted.chmod(0o600)


def test_the_connection_screen_answers_on_prod(prod_environment: None, config_root: Path) -> None:
    """On prod the routes use the deployment's own settings, so GET says `deployed` instead of 503."""
    settings = Settings(env="prod", cors_origins=("https://marketing.example.com",))
    app = create_app(config_root=config_root)
    # Set after construction: `create_app(settings=...)` would also reconfigure the process's logging,
    # and that would leak into every test that runs after this one.
    app.state.settings = settings
    with TestClient(app, client=("10.0.0.5", 1)) as deployed:
        response = deployed.get("/connection/aws")
    assert response.status_code == 200
    assert response.json()["locked_reason"] == "deployed"
    assert response.json()["connection"] == {"source": "default_chain", "profile": None}


def test_a_generative_job_on_prod_uses_the_task_role_without_building_settings(
    prod_environment: None,
) -> None:
    """`profile_in_force` is the default chain on prod, and does not raise or read the planted file."""
    with pytest.raises(Exception, match="CORS"):
        Settings.from_env()
    assert aws_connection.profile_in_force() is None


def test_a_generative_job_on_a_laptop_uses_the_saved_profile(data_dir: Path) -> None:
    """`profile_in_force` returns what the connection screen saved, which is the point of saving it."""
    aws_connection.save_connection(
        _local_settings(), AwsConnection(source=CredentialSource.PROFILE, profile="dev")
    )
    assert aws_connection.profile_in_force() == "dev"
