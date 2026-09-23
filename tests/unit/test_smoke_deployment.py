"""`scripts/smoke_deployment.py`: the last step of `deploy-dev.yml`, which Phase 4a named and never wrote.

The smoke test is small on purpose, so these tests pin the two things it exists to say: that the
deployment answers its health probe, and which sign-in state it is in - with `auth_mode=off` a
warning on dev, a failure anywhere else, and a prod deployment failing closed with 503
`AUTH_NOT_CONFIGURED` (DEC-702) a failure, because nobody can use it.

The HTTP layer is a fake `fetch`, and the one end-to-end test serves the real application through
FastAPI's test client, so what the smoke test reads as "sign-in is off" is what this checkout's API
actually answers. CloudFormation is a fake client: moto 5.2.3's CloudFormation backend imports
`openapi_spec_validator`, which the dev extra does not install. Nothing here reaches AWS or a network.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_deployment as smoke


def _fetch(answers: dict[str, tuple[int, Any]]) -> smoke.Fetch:
    def fetch(url: str) -> tuple[int, bytes]:
        for suffix, (status, body) in answers.items():
            if url.endswith(suffix):
                return status, json.dumps(body).encode("utf-8")
        raise ConnectionRefusedError

    return fetch


HEALTHY = (200, {"status": "ok", "version": "0.1.0"})


class FakeCloudFormation:
    def __init__(self, outputs: list[dict[str, str]]) -> None:
        self.outputs = outputs
        self.asked: list[str] = []

    def describe_stacks(self, StackName: str) -> dict[str, Any]:  # noqa: N803 - boto3's spelling
        self.asked.append(StackName)
        return {"Stacks": [{"StackName": StackName, "Outputs": self.outputs}]}


def test_signed_in_deployment_passes() -> None:
    probes, code = smoke.run(_fetch({"/healthz": HEALTHY, "/auth/me": (401, {})}), "http://x", "dev")
    assert code == 0
    assert [probe.status for probe in probes] == [smoke.OK, smoke.OK]
    assert "version 0.1.0" in probes[0].detail


def test_sign_in_off_is_a_warning_on_dev_and_a_failure_elsewhere() -> None:
    fetch = _fetch({"/healthz": HEALTHY, "/auth/me": (200, {"principal": {}})})
    dev, dev_code = smoke.run(fetch, "http://x", "dev")
    prod, prod_code = smoke.run(fetch, "http://x", "prod")
    assert (dev[1].status, dev_code) == (smoke.WARN, 0)
    assert (prod[1].status, prod_code) == (smoke.FAILED, 1)


def test_prod_failing_closed_is_named() -> None:
    body = {"detail": {"code": "AUTH_NOT_CONFIGURED", "message": "...", "path": None}}
    probes, code = smoke.run(_fetch({"/healthz": HEALTHY, "/auth/me": (503, body)}), "http://x", "prod")
    assert code == 1
    assert "AUTH_NOT_CONFIGURED" in probes[1].detail


def test_nothing_answering_is_a_failure_on_every_probe() -> None:
    probes, code = smoke.run(_fetch({}), "http://x", "dev")
    assert code == 1
    assert all(probe.status == smoke.FAILED for probe in probes)
    assert all("ConnectionRefusedError" in probe.detail for probe in probes)


def test_an_unhealthy_answer_fails() -> None:
    probes, code = smoke.run(_fetch({"/healthz": (502, {}), "/auth/me": (401, {})}), "http://x", "dev")
    assert code == 1
    assert probes[0].status == smoke.FAILED


def test_the_address_comes_from_the_compute_stack() -> None:
    cloudformation = FakeCloudFormation(
        [
            {"OutputKey": "ImageReference", "OutputValue": "r@sha256:0"},
            {"OutputKey": "ServiceUrl", "OutputValue": "http://alb.example/"},
        ]
    )
    out = io.StringIO()
    code = smoke.main(
        ["--env", "dev"],
        fetch=_fetch({"/healthz": HEALTHY, "/auth/me": (401, {})}),
        cloudformation=cloudformation,
        out=out,
    )
    assert code == 0
    assert cloudformation.asked == ["marketing-ai-dev-compute"]
    assert "url=http://alb.example\n" in out.getvalue()
    assert out.getvalue().rstrip().endswith("smoke test passed")


def test_a_stack_without_the_output_is_reported_without_a_message() -> None:
    out = io.StringIO()
    code = smoke.main(["--env", "dev"], fetch=_fetch({}), cloudformation=FakeCloudFormation([]), out=out)
    assert code == 1
    assert "could not read marketing-ai-dev-compute (LookupError)" in out.getvalue()


def test_against_this_checkouts_api(tmp_path: Path) -> None:
    """The real app, through the test client: auth off on dev reads as the warning it should be."""
    from fastapi.testclient import TestClient

    from api.main import create_app

    client = TestClient(create_app(data_dir=tmp_path))

    def fetch(url: str) -> tuple[int, bytes]:
        response = client.get(url.removeprefix("http://testserver"))
        return response.status_code, response.content

    probes, code = smoke.run(fetch, "http://testserver", "dev")
    assert code == 0
    assert probes[0].status == smoke.OK
    assert probes[1].status == smoke.WARN


@pytest.mark.parametrize(
    ("auth_mode", "env", "verdict", "code"),
    [
        ("local", "dev", smoke.OK, 0),
        ("local", "prod", smoke.OK, 0),
        ("off", "prod", smoke.FAILED, 1),
    ],
)
def test_the_real_app_in_every_sign_in_state(
    tmp_path: Path, auth_mode: str, env: str, verdict: str, code: int
) -> None:
    """The two answers the fakes above stand for - 401, and prod's 503 `AUTH_NOT_CONFIGURED` - are
    what this checkout's API really says, so a renamed code or a moved route fails here rather than
    in the last step of a deployment."""
    from fastapi.testclient import TestClient

    from api.main import create_app
    from engine.settings import Settings

    extra: dict[str, Any] = {"cors_origins": ("https://example.invalid",)} if env == "prod" else {}
    settings = Settings.model_validate({"auth_mode": auth_mode, "env": env, "data_dir": tmp_path, **extra})
    client = TestClient(create_app(data_dir=tmp_path, settings=settings))

    def fetch(url: str) -> tuple[int, bytes]:
        response = client.get(url.removeprefix("http://testserver"))
        return response.status_code, response.content

    probes, exit_code = smoke.run(fetch, "http://testserver", env)
    assert probes[0].status == smoke.OK
    assert probes[1].status == verdict, probes[1].line()
    assert exit_code == code
    if auth_mode == "off":
        assert "AUTH_NOT_CONFIGURED" in probes[1].detail


def test_the_stack_name_matches_the_infrastructure(repo_root: Path) -> None:
    compute = (repo_root / "infra" / "compute.py").read_text(encoding="utf-8")
    naming = (repo_root / "infra" / "naming.py").read_text(encoding="utf-8")
    assert f'"{smoke.SERVICE_URL_OUTPUT}",' in compute
    assert 'return f"{PRODUCT}-{env_name}-{component}"' in naming


def test_a_given_url_skips_cloudformation() -> None:
    out = io.StringIO()
    code = smoke.main(
        ["--env", "dev", "--url", "http://given.example/"],
        fetch=_fetch({"/healthz": HEALTHY, "/auth/me": (401, {})}),
        out=out,
    )
    assert code == 0
    assert "url=http://given.example\n" in out.getvalue()
