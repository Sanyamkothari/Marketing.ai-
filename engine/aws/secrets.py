"""Reading a deployment's configuration from SSM Parameter Store and Secrets Manager.

`engine/settings.py` defines *what* a deployment is; this module is one way of finding out, and the
only one that talks to AWS. It is kept behind the `ParameterSource` protocol so that
`settings_from_aws` can be tested with a dictionary and so that `engine.settings` never imports
boto3 (DEC-306).

Two stores, because they answer two different questions. Parameter Store holds the values an
operator is happy to read in the console - a bucket name, a role ARN, an instance type - as plain
`String` parameters under `/marketing-ai/<env>/`. Secrets Manager holds the one value that is a
credential, the database URL, inside a single JSON document. Splitting them that way means the task
role can be granted `ssm:GetParametersByPath` on the whole prefix and
`secretsmanager:GetSecretValue` on exactly one secret, which is a boundary a reviewer can check at a
glance (DEC-305).

Nothing here logs a value. `SecretsError` names the parameter path or the secret name and stops
there, for the same reason `SettingsError` does: the value is the part worth protecting.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Final, Protocol, cast

from engine.settings import ParameterSource, secret_name, ssm_prefix
from engine.utils.logging import get_logger

__all__ = [
    "AwsParameterSource",
    "SecretsError",
    "StaticParameterSource",
    "quiet_aws_wire_logs",
    "secret_name",
    "ssm_prefix",
]

_LOGGER = get_logger(__name__)

WIRE_LOGGERS: Final[tuple[str, ...]] = ("botocore", "boto3", "urllib3", "s3transfer")
"""Libraries whose DEBUG output prints request bodies, headers and signed URLs."""

MAX_PAGES: Final[int] = 50
"""A guard, not a service limit: 50 pages of 10 parameters is far more than a deployment describes."""


class SecretsError(Exception):
    """A parameter or secret could not be read, or was not what this product expects.

    `code` is one of SECRETS_ACCESS_DENIED, SECRETS_UNAVAILABLE or SECRETS_MALFORMED. `name` is the
    path or secret name that failed - never a value.
    """

    def __init__(self, code: str, message: str, *, name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.name = name


class _SsmClient(Protocol):
    """The one SSM call this module makes, typed so `mypy --strict` has something to check."""

    def get_parameters_by_path(self, **kwargs: Any) -> Mapping[str, Any]: ...


class _SecretsClient(Protocol):
    """The one Secrets Manager call this module makes."""

    def get_secret_value(self, **kwargs: Any) -> Mapping[str, Any]: ...


def quiet_aws_wire_logs() -> None:
    """Cap the AWS client loggers at INFO.

    botocore at DEBUG prints request headers, query strings and therefore pre-signed URLs, which are
    bearer credentials. Setting the engine's own level to DEBUG must not turn that on by accident,
    so the cap is applied once, explicitly, wherever an AWS client is first built (DEC-309).
    """
    for name in WIRE_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.INFO:
            logger.setLevel(logging.INFO)


class AwsParameterSource:
    """Reads `/marketing-ai/<env>/*` from Parameter Store and one JSON secret from Secrets Manager."""

    def __init__(
        self,
        *,
        region: str | None = None,
        ssm: _SsmClient | None = None,
        secrets: _SecretsClient | None = None,
    ) -> None:
        quiet_aws_wire_logs()
        self._region = region
        self._ssm = ssm
        self._secrets = secrets

    def _ssm_client(self) -> _SsmClient:
        if self._ssm is None:
            # A deliberate local import: boto3 is an optional dependency (DEC-306).
            import boto3

            # `cast` rather than a wider annotation: types-boto3 declares a client with a hundred
            # methods, and the narrow Protocol above is the point - it says, checkably, that this
            # module calls exactly one of them.
            self._ssm = cast("_SsmClient", boto3.client("ssm", region_name=self._region))
        return self._ssm

    def _secrets_client(self) -> _SecretsClient:
        if self._secrets is None:
            # A deliberate local import: boto3 is an optional dependency (DEC-306).
            import boto3

            self._secrets = cast("_SecretsClient", boto3.client("secretsmanager", region_name=self._region))
        return self._secrets

    def parameters(self, prefix: str) -> dict[str, str]:
        """Every parameter directly under `prefix`, keyed by its leaf name.

        `Recursive=False` on purpose: the prefix is a flat namespace of setting names, and a nested
        path would be a different phase's data sharing the same root. `WithDecryption=True` so a
        `SecureString` works if an operator chooses one, without this module caring which it got.
        An absent prefix is an empty mapping, not an error - a deployment may legitimately describe
        itself entirely through the environment.
        """
        client = self._ssm_client()
        found: dict[str, str] = {}
        token: str | None = None
        for _page in range(MAX_PAGES):
            request: dict[str, Any] = {"Path": prefix, "Recursive": False, "WithDecryption": True}
            if token is not None:
                request["NextToken"] = token
            try:
                response = client.get_parameters_by_path(**request)
            except Exception as exc:  # mapped below; the botocore exception types are an optional import
                raise _mapped(exc, name=prefix) from exc
            for parameter in response.get("Parameters", ()):
                name = str(parameter.get("Name", ""))
                if name:
                    found[name.rsplit("/", 1)[-1]] = str(parameter.get("Value", ""))
            token = response.get("NextToken")
            if not token:
                break
        else:  # pragma: no cover - the guard exists so a paging bug cannot spin forever
            raise SecretsError(
                "SECRETS_UNAVAILABLE",
                f"{prefix} returned more than {MAX_PAGES} pages of parameters; that is not a deployment.",
                name=prefix,
            )
        _LOGGER.info("settings source=ssm prefix=%s parameters=%d", prefix, len(found))
        return found

    def secret(self, name: str) -> dict[str, str]:
        """The named secret, parsed as a flat JSON object of string values.

        A missing secret is an empty mapping: an environment that keeps its database URL in an
        environment variable has no secret to read and should not have to create an empty one.
        A secret that exists but is not a flat JSON object is `SECRETS_MALFORMED`, because silently
        ignoring it would leave the deployment quietly misconfigured.
        """
        client = self._secrets_client()
        try:
            response = client.get_secret_value(SecretId=name)
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            error = _mapped(exc, name=name)
            if error.code == "SECRETS_UNAVAILABLE" and _error_code(exc) == "ResourceNotFoundException":
                _LOGGER.info("settings source=secretsmanager secret=%s absent", name)
                return {}
            raise error from exc
        raw = response.get("SecretString")
        if raw is None:
            raise SecretsError("SECRETS_MALFORMED", f"{name} holds binary, not a JSON document.", name=name)
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise SecretsError("SECRETS_MALFORMED", f"{name} is not valid JSON.", name=name) from exc
        if not isinstance(document, dict) or any(not isinstance(value, str) for value in document.values()):
            raise SecretsError(
                "SECRETS_MALFORMED", f"{name} must be a flat JSON object of strings.", name=name
            )
        _LOGGER.info("settings source=secretsmanager secret=%s keys=%d", name, len(document))
        return {str(key): value for key, value in document.items()}


class StaticParameterSource:
    """An in-memory `ParameterSource`, for tests and for `make aws-bootstrap --dry-run`."""

    def __init__(
        self,
        parameters: Mapping[str, str] | None = None,
        secrets: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._parameters = dict(parameters or {})
        self._secrets = {name: dict(values) for name, values in (secrets or {}).items()}

    def parameters(self, prefix: str) -> dict[str, str]:
        """Entries whose key starts with `prefix`, keyed by leaf name, mirroring the real source."""
        out: dict[str, str] = {}
        for key, value in self._parameters.items():
            if key.startswith(prefix):
                out[key.rsplit("/", 1)[-1]] = value
            elif "/" not in key:
                out[key] = value
        return out

    def secret(self, name: str) -> dict[str, str]:
        """The named secret, or an empty mapping."""
        return dict(self._secrets.get(name, {}))


def _error_code(exc: BaseException) -> str:
    """The AWS error code inside a `ClientError`, or an empty string for anything else."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def _mapped(exc: BaseException, *, name: str) -> SecretsError:
    """One AWS failure, as a `SecretsError` that names the path and not the value."""
    code = _error_code(exc)
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
        return SecretsError(
            "SECRETS_ACCESS_DENIED",
            f"This role may not read {name}. Grant it and redeploy; nothing was read.",
            name=name,
        )
    return SecretsError(
        "SECRETS_UNAVAILABLE",
        f"Could not read {name}: {type(exc).__name__}{f' ({code})' if code else ''}.",
        name=name,
    )


_: type[ParameterSource] = AwsParameterSource
__: type[ParameterSource] = StaticParameterSource
"""Both classes must satisfy the protocol; mypy checks these two lines so no test has to."""
