"""The smoke test `.github/workflows/deploy-dev.yml` runs last: is the deployment answering, and how.

Phase 4a's workflow invoked this module before it existed, so every dispatch without `skip_smoke`
failed at its last step, and `docs/AWS_DEPLOYMENT.md` had to say so. M50's walk of the guide is
what finally wrote it, deliberately small: it proves what can be proved from outside the VPC
without a user, a password or a customer's file, and it leaves the acceptance walk-through (§5 of
the guide) to a person.

Three checks, each one line in the report, all of them run even when an earlier one fails:

* **The address.** `ServiceUrl` from the compute stack's outputs - the value the guide tells a person
  to open - unless `--url` names one. Reading it from CloudFormation, rather than taking it as an
  argument in CI, means the smoke test cannot pass against a stale address somebody pasted.
* **`GET /healthz` answers 200 with `status: ok`**, and the version it reports is printed. The load
  balancer health-checks the same route, so this is the one request that must work with no sign-in.
* **Sign-in is in the state the deployment claims.** `GET /auth/me` without a token: `401` means
  `auth_mode=local` is live (the expected answer behind a public load balancer); `200` means
  sign-in is off, which is reported as a warning on dev and a failure anywhere else; `503
  AUTH_NOT_CONFIGURED` is a prod deployment failing closed because sign-in was never configured
  (DEC-702) - a failure, because nobody can use it; anything else, including a `404` from an image
  that predates Phase 4b, is a failure that names the status.

It never sends a credential, never uploads anything and never starts a run, so it is safe to run
against any deployment at any time. HTTP goes through `urllib` rather than a client library so the
infrastructure venv, which is what CI runs this with, needs nothing it does not already have.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, TextIO

__all__ = [
    "COMMAND",
    "Probe",
    "check_health",
    "check_sign_in",
    "main",
    "service_url",
]

COMMAND: Final[str] = "python -m scripts.smoke_deployment"
PRODUCT: Final[str] = "marketing-ai"
"""Mirrors `infra.naming.PRODUCT`; not imported because `infra/` needs aws-cdk-lib."""

SERVICE_URL_OUTPUT: Final[str] = "ServiceUrl"
"""The compute stack's output `docs/AWS_DEPLOYMENT.md` §5 reads the same way."""

TIMEOUT_SECONDS: Final[int] = 15
DEV: Final[str] = "dev"

OK: Final[str] = "ok"
WARN: Final[str] = "warning"
FAILED: Final[str] = "FAILED"

Fetch = Callable[[str], tuple[int, bytes]]
"""`url -> (status, body)`. Never raises for an HTTP status; raises `OSError` when nothing answered."""


@dataclass(frozen=True, slots=True)
class Probe:
    """One line of the report."""

    name: str
    status: str
    detail: str

    def line(self) -> str:
        """What an operator reads."""
        return f"  [{self.status:>7}] {self.name}: {self.detail}"


def compute_stack_name(env: str) -> str:
    """Mirrors `infra.naming.stack_name(env, "compute")`."""
    return f"{PRODUCT}-{env}-compute"


def service_url(cloudformation: Any, env: str) -> str:
    """The compute stack's `ServiceUrl` output, without a trailing slash."""
    stacks = cloudformation.describe_stacks(StackName=compute_stack_name(env)).get("Stacks") or []
    outputs = (stacks[0].get("Outputs") if stacks else None) or []
    for output in outputs:
        if output.get("OutputKey") == SERVICE_URL_OUTPUT:
            return str(output["OutputValue"]).rstrip("/")
    raise LookupError(f"{compute_stack_name(env)} has no {SERVICE_URL_OUTPUT} output")


def urllib_fetch(url: str) -> tuple[int, bytes]:
    """A GET with no credentials; an HTTP error status is returned, not raised."""
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _json(body: bytes) -> dict[str, Any]:
    """The body as a JSON object, or an empty one when it is not."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_code(body: bytes) -> str:
    """`detail.code` from the API's error envelope, or an empty string."""
    detail = _json(body).get("detail")
    return str(detail.get("code", "")) if isinstance(detail, dict) else ""


def check_health(fetch: Fetch, base_url: str) -> Probe:
    """`GET /healthz` is 200 with `status: ok`."""
    try:
        status, body = fetch(f"{base_url}/healthz")
    except OSError as exc:
        return Probe("healthz", FAILED, f"nothing answered ({type(exc).__name__})")
    payload = _json(body)
    if status == 200 and payload.get("status") == "ok":
        return Probe("healthz", OK, f"200, version {payload.get('version', 'unknown')}")
    return Probe("healthz", FAILED, f"answered {status}, not 200 with status ok")


def check_sign_in(fetch: Fetch, base_url: str, env: str) -> Probe:
    """`GET /auth/me` with no token says which sign-in state the deployment is in."""
    try:
        status, body = fetch(f"{base_url}/auth/me")
    except OSError as exc:
        return Probe("sign-in", FAILED, f"nothing answered ({type(exc).__name__})")
    if status == 401:
        return Probe("sign-in", OK, "401 without a token: auth_mode=local is live")
    if status == 200:
        verdict = WARN if env == DEV else FAILED
        return Probe(
            "sign-in",
            verdict,
            "200 without a token: auth_mode=off, so every request acts as the all-roles local "
            "operator; set the auth_mode parameter to local and create the first Admin",
        )
    if status == 503 and _error_code(body) == "AUTH_NOT_CONFIGURED":
        return Probe(
            "sign-in",
            FAILED,
            "503 AUTH_NOT_CONFIGURED: sign-in is off on a prod deployment, so every route but the "
            "probe refuses; set the auth_mode parameter to local",
        )
    return Probe("sign-in", FAILED, f"answered {status}; expected 401 (or 200 on dev)")


def run(fetch: Fetch, base_url: str, env: str) -> tuple[list[Probe], int]:
    """Every probe, and the exit code: 0 when none failed."""
    probes = [check_health(fetch, base_url), check_sign_in(fetch, base_url, env)]
    return probes, int(any(probe.status == FAILED for probe in probes))


def build_parser() -> argparse.ArgumentParser:
    """`--env`, `--region`, `--url`."""
    parser = argparse.ArgumentParser(
        prog=COMMAND, description="Check that a deployment answers, and in which sign-in state."
    )
    parser.add_argument("--env", required=True, help="deployment name, e.g. dev")
    parser.add_argument("--region", default="ap-south-1", help="where the compute stack is")
    parser.add_argument("--url", default=None, help="skip CloudFormation and probe this base URL")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch | None = None,
    cloudformation: Any = None,
    out: TextIO | None = None,
) -> int:
    """Resolve the address, probe it, print the report; returns the exit code."""
    stream = out or sys.stdout
    args = build_parser().parse_args(argv)
    base_url = (args.url or "").rstrip("/")
    if not base_url:
        if cloudformation is None:
            import boto3

            cloudformation = boto3.client("cloudformation", region_name=args.region)
        try:
            base_url = service_url(cloudformation, args.env)
        except Exception as exc:  # a missing stack, no credentials: say which, never a message
            print(
                f"{COMMAND}: could not read {compute_stack_name(args.env)} ({type(exc).__name__})",
                file=stream,
            )
            return 1
    probes, code = run(fetch or urllib_fetch, base_url, args.env)
    print(f"{COMMAND}: env={args.env} url={base_url}", file=stream)
    for probe in probes:
        print(probe.line(), file=stream)
    print("smoke test passed" if code == 0 else "smoke test FAILED", file=stream)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
