"""Which AWS identity Bedrock is called as: chosen by name, checked, shown - never held.

Different people run this product with different AWS credentials, and the tempting way to let them
is a form with two boxes, access key and secret. This module exists so there is never that form.
Five rules shape it, and each is enforced here rather than hoped for in the screen above it.

**A secret never crosses HTTP.** The browser chooses a *source* - the default credential chain, or
an AWS CLI profile by name - and the server resolves it where the AWS tooling already keeps it:
`~/.aws/`, the SSO token cache, the instance or task role. The secret stays in the one place built to
hold, refresh and rotate it. A key typed into a web page is a long-lived credential in a browser, in
a request body, in whatever proxy logs bodies and in every HAR file attached to a bug report; an SSO
login a person refreshes with `aws sso login` is none of those things.

**Choosing an identity is only possible on the machine that owns the identities.** A profile may be
selected when the deployment is `local` *and* the request comes from a loopback address. A deployed
server is single tenant and unauthenticated (docs/AWS_DEPLOYMENT.md, section 9.2), so there is no
"person" to scope a choice to: letting a visitor pick among the operator's profiles would hand every
visitor the operator's identities, and there is nobody the choice could belong to. A deployment
calls Bedrock as its IAM task role, which is the gold standard this module is imitating on a laptop.

**A check proves the connection without spending anything.** `sts:GetCallerIdentity` needs no IAM
permission at all and says *who* the credentials are; `bedrock:GetFoundationModelAvailability`
says whether a model is enabled for that account in that region without invoking it. Neither costs
a token, so the connection test is not a metered call and does not have to pretend to be one.

**What is shown is identity, never material.** A report carries the account, the principal ARN, the
credential method ("sso", "iam-role", "shared-credentials-file") and per-model availability. It
never carries a key, a token or an AWS error message: some of those - `SignatureDoesNotMatch` above
all - quote the credential scope, which names the access key id. Every failure is mapped to a code
and a sentence written here. On a deployment even the identity is masked, because an unauthenticated
visitor has no business learning the account id and role name the product runs as.

**What is remembered is a name, and only on a laptop.** The one thing persisted is the chosen
profile name, under the local data directory, readable by its owner alone. Never the resolved
account or ARN (engine/settings.py, rule 1), and a deployed server does not read the file at all, so
nothing planted there can redirect it.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.settings import Settings
from engine.utils.logging import get_logger

__all__ = [
    "CONNECTION_FILENAME",
    "EDITABLE_ENV",
    "FAILURES",
    "LOCAL_STATE_DIR",
    "AwsConnection",
    "ConnectionLockedError",
    "ConnectionReport",
    "CredentialSource",
    "LockReason",
    "ModelCheck",
    "ModelRole",
    "ModelStatus",
    "SessionFactory",
    "available_profiles",
    "aws_session",
    "check_connection",
    "classify_failure",
    "connection_path",
    "editability",
    "is_loopback",
    "load_connection",
    "mask_account",
    "save_connection",
    "valid_profile_name",
    "valid_region",
]

_LOGGER = get_logger(__name__)

EDITABLE_ENV: Final[str] = "local"
"""The only deployment in which a person may choose which AWS identity is used."""

LOCAL_STATE_DIR: Final[str] = "local"
CONNECTION_FILENAME: Final[str] = "aws_connection.json"

_PROFILE_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+=,@-]{0,63}")
"""A profile name this module will accept, pass to boto3 and interpolate into a hint.

Narrower than what the AWS CLI tolerates, on purpose: a name goes into a shell command a person is
told to run (`aws sso login --profile <name>`), so a character that means something to a shell is
refused rather than quoted. Every real profile name seen in practice fits.
"""

_REGION: Final[re.Pattern[str]] = re.compile(r"[a-z]{2}(-[a-z]+)+-\d{1,2}")
"""An AWS region name. Checked before it reaches botocore, which builds an endpoint hostname from it."""

_CHECK_TIMEOUT_S: Final[int] = 10


class CredentialSource(StrEnum):
    """Where the credentials come from. Never the credentials themselves."""

    DEFAULT_CHAIN = "default_chain"
    PROFILE = "profile"


class LockReason(StrEnum):
    """Why the identity cannot be changed from this request."""

    DEPLOYED = "deployed"
    REMOTE_CLIENT = "remote_client"


class ModelStatus(StrEnum):
    """What the availability check could establish about one model."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"


ModelRole = Literal["generation", "judge", "embedding"]


class AwsConnection(BaseModel):
    """The identity choice: a source, and a profile name when the source is a profile.

    `extra="forbid"` is load-bearing. A body that carries `aws_secret_access_key`, or anything else,
    is refused rather than silently dropped, so a person who pastes a key learns that this product
    does not take one instead of believing it has been stored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: CredentialSource = Field(
        default=CredentialSource.DEFAULT_CHAIN,
        description="`default_chain` (environment, AWS_PROFILE, SSO, instance or task role) or `profile`.",
    )
    profile: str | None = Field(
        default=None, description="The AWS CLI profile to use. Set exactly when `source` is `profile`."
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.source is CredentialSource.PROFILE:
            if self.profile is None or not valid_profile_name(self.profile):
                raise ValueError("a profile source needs a profile name made of letters, digits and _.+=,@-")
        elif self.profile is not None:
            raise ValueError("a profile is named only when the source is 'profile'")
        return self


class ModelCheck(BaseModel):
    """Whether one model is usable by the checked identity in the checked region."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ModelRole = Field(description="What the engine uses this model for.")
    model_id: str = Field(description="The model id that was checked.")
    status: ModelStatus = Field(description="available, unavailable, or unverified when AWS would not say.")
    detail: str = Field(description="One sentence a person can act on. Written here, never quoted from AWS.")


class ConnectionReport(BaseModel):
    """The outcome of a connection check: who, where, which models - and never a secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool = Field(description="True when AWS accepted the credentials.")
    source: CredentialSource = Field(description="The credential source that was checked.")
    profile: str | None = Field(
        default=None,
        description="The profile in effect, including one named by AWS_PROFILE. Null when masked.",
    )
    region: str = Field(description="The region the check ran against.")
    credential_method: str | None = Field(
        default=None, description="How botocore found the credentials, e.g. `sso`, `iam-role`, `env`."
    )
    account_id: str | None = Field(
        default=None, description="The AWS account. Masked to its last four digits on a deployment."
    )
    principal_arn: str | None = Field(
        default=None, description="Who the credentials are. Omitted when masked."
    )
    identity_masked: bool = Field(
        description="True when account and principal were withheld from this caller."
    )
    models: tuple[ModelCheck, ...] = Field(default=(), description="One entry per configured model.")
    error_code: str | None = Field(default=None, description="Set when `ok` is false: a code from FAILURES.")
    message: str | None = Field(default=None, description="What went wrong, in a sentence written here.")
    hint: str | None = Field(default=None, description="What to run or change to fix it.")


class ConnectionLockedError(Exception):
    """Raised by `save_connection` outside a local deployment."""


FAILURES: Final[Mapping[str, tuple[str, str]]] = {
    "PROFILE_NOT_FOUND": (
        "There is no AWS profile named '{profile}' on the machine running Marketing AI.",
        "Create it there with `aws configure --profile {profile}` or `aws configure sso --profile {profile}`.",
    ),
    "NO_CREDENTIALS": (
        "No AWS credentials were found on the machine running Marketing AI.",
        "Run `aws configure` or `aws sso login` in a terminal on that machine, or choose a named profile.",
    ),
    "SSO_LOGIN_REQUIRED": (
        "The AWS SSO session for these credentials has expired or was never started.",
        "Run `aws sso login{profile_flag}` in a terminal on the machine running Marketing AI.",
    ),
    "CREDENTIALS_EXPIRED": (
        "These AWS credentials have expired.",
        "Refresh them - `aws sso login{profile_flag}`, or new temporary credentials - and test again.",
    ),
    "CREDENTIALS_INVALID": (
        "AWS rejected these credentials.",
        "Check them with `aws sts get-caller-identity{profile_flag}`; the key may be deactivated or rotated.",
    ),
    "CREDENTIALS_UNAVAILABLE": (
        "AWS credentials for this source could not be obtained.",
        "Run `aws sts get-caller-identity{profile_flag}` on that machine to see the full reason.",
    ),
    "AWS_UNREACHABLE": (
        "AWS could not be reached from the machine running Marketing AI.",
        "Check the network, any proxy, and that the region is one this account uses.",
    ),
    "CONNECTION_FAILED": (
        "The connection check failed for a reason AWS did not name.",
        "Run `aws sts get-caller-identity{profile_flag}` on that machine to see the full error.",
    ),
}
"""Code -> (message, hint). The only text a failed check ever returns (see the module docstring)."""

_EXPIRED_CODES: Final[frozenset[str]] = frozenset(
    {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "TokenRefreshRequired"}
)
_INVALID_CODES: Final[frozenset[str]] = frozenset(
    {
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "IncompleteSignature",
        "AuthFailure",
    }
)
_DENIED_CODES: Final[frozenset[str]] = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
)
_UNKNOWN_ID_CODES: Final[frozenset[str]] = frozenset({"ValidationException", "ResourceNotFoundException"})


SessionFactory = Callable[[str | None, str], Any]
"""(profile or None, region) -> a boto3 `Session`. Injected by tests; typed `Any` to keep boto3's own
types from leaking into a module that only ever asks it three questions."""


# ---------------------------------------------------------------------------
# Small predicates
# ---------------------------------------------------------------------------
def valid_profile_name(name: str) -> bool:
    """True when `name` is safe to pass to boto3 and to print inside a shell command."""
    return _PROFILE_NAME.fullmatch(name) is not None


def valid_region(region: str) -> bool:
    """True when `region` has the shape of an AWS region name."""
    return _REGION.fullmatch(region) is not None


def is_loopback(host: str | None) -> bool:
    """True when a request came from this machine.

    Parsed as an address rather than compared with "127.0.0.1", so `::1` and the rest of 127/8 count
    and a hostname that merely contains "127" does not.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def editability(settings: Settings, client_host: str | None) -> LockReason | None:
    """`None` when this request may choose the identity, or the reason it may not.

    The deployment is checked first and independently: a reverse proxy on the same host makes every
    request look like loopback, so loopback alone must never be what unlocks a deployed server.
    """
    if settings.env != EDITABLE_ENV:
        return LockReason.DEPLOYED
    if not is_loopback(client_host):
        return LockReason.REMOTE_CLIENT
    return None


def mask_account(account_id: str | None) -> str | None:
    """`123456789012` -> `********9012`: enough to tell two accounts apart, not enough to name one."""
    if not account_id:
        return None
    return "*" * max(len(account_id) - 4, 0) + account_id[-4:]


# ---------------------------------------------------------------------------
# Profiles and the remembered choice
# ---------------------------------------------------------------------------
def available_profiles() -> tuple[str, ...]:
    """Every profile name botocore can see on this machine, sorted - names only, never their contents.

    Reads `~/.aws/config` and `~/.aws/credentials` (or `AWS_CONFIG_FILE` and
    `AWS_SHARED_CREDENTIALS_FILE`) through botocore rather than parsing them here, so SSO profiles,
    `credential_process` profiles and `source_profile` chains are listed exactly as the AWS CLI
    lists them. A name this module would refuse to use is left out rather than shown and then refused.
    """
    try:
        import botocore.session

        names = botocore.session.Session().available_profiles
    except Exception as exc:  # a malformed config file must not take the settings screen down with it
        _LOGGER.warning("aws_connection.profiles_unreadable kind=%s", type(exc).__name__)
        return ()
    return tuple(sorted(name for name in names if valid_profile_name(name)))


def connection_path(settings: Settings) -> Path:
    """Where the chosen profile name is remembered: machine-local state under the data directory."""
    return settings.data_dir / LOCAL_STATE_DIR / CONNECTION_FILENAME


def load_connection(settings: Settings) -> AwsConnection:
    """The identity choice in force.

    Outside a local deployment the answer is always the default chain - the task role - and the file
    is not even opened. On a laptop, a missing file is the default chain, and a file that no longer
    parses is too: falling back to a guessed profile would be worse than falling back to the one AWS
    itself would pick.
    """
    if settings.env != EDITABLE_ENV:
        return AwsConnection()
    path = connection_path(settings)
    try:
        return AwsConnection.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AwsConnection()
    except (OSError, ValueError) as exc:
        _LOGGER.warning("aws_connection.unreadable kind=%s", type(exc).__name__)
        return AwsConnection()


def save_connection(settings: Settings, connection: AwsConnection) -> None:
    """Remember `connection`, or forget it when it is the default.

    Written atomically and readable by its owner only. The file holds a source and a profile name
    and nothing else, because that is all `AwsConnection` can hold.
    """
    if settings.env != EDITABLE_ENV:
        raise ConnectionLockedError(settings.env)
    path = connection_path(settings)
    if connection == AwsConnection():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(connection.model_dump_json())
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def aws_session(profile: str | None, region: str) -> Any:
    """A boto3 session for `profile` (or the default chain) in `region`. Raises `ProfileNotFound`."""
    import boto3

    return boto3.Session(profile_name=profile, region_name=region)


def classify_failure(exc: BaseException) -> str:
    """The `FAILURES` code for an exception raised while resolving or using credentials.

    Decided by type and by AWS's error *code*, never by the message, which is exactly the part that
    must not be trusted or repeated.
    """
    from botocore import exceptions as boto

    if isinstance(exc, boto.ProfileNotFound):
        return "PROFILE_NOT_FOUND"
    if isinstance(exc, boto.SSOTokenLoadError | boto.UnauthorizedSSOTokenError | boto.TokenRetrievalError):
        return "SSO_LOGIN_REQUIRED"
    if isinstance(exc, boto.SSOError):
        return "SSO_LOGIN_REQUIRED"
    if isinstance(exc, boto.NoCredentialsError | boto.PartialCredentialsError):
        return "NO_CREDENTIALS"
    if isinstance(exc, boto.CredentialRetrievalError):
        return "CREDENTIALS_UNAVAILABLE"
    if isinstance(exc, boto.EndpointConnectionError | boto.ConnectTimeoutError | boto.ReadTimeoutError):
        return "AWS_UNREACHABLE"
    if isinstance(exc, boto.ProxyConnectionError | boto.SSLError):
        return "AWS_UNREACHABLE"
    if isinstance(exc, boto.ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _EXPIRED_CODES:
            return "CREDENTIALS_EXPIRED"
        if code in _INVALID_CODES:
            return "CREDENTIALS_INVALID"
    return "CONNECTION_FAILED"


def check_connection(
    connection: AwsConnection,
    *,
    region: str,
    model_ids: Mapping[ModelRole, str],
    mask_identity: bool,
    session_factory: SessionFactory = aws_session,
) -> ConnectionReport:
    """Resolve `connection`, ask AWS who it is, and ask Bedrock whether each model is enabled.

    Free by construction: `GetCallerIdentity` needs no permission and `GetFoundationModelAvailability`
    invokes nothing. `model_ids` with an empty id are skipped, which is what a fake backend's
    configuration gives - there is nothing on AWS to check it against.
    """
    if not valid_region(region):
        raise ValueError(f"{region!r} is not an AWS region name")
    profile = connection.profile if connection.source is CredentialSource.PROFILE else None
    effective_profile = profile or os.environ.get("AWS_PROFILE") or None
    try:
        session = session_factory(profile, region)
        credentials = session.get_credentials()
        if credentials is None:
            from botocore.exceptions import NoCredentialsError

            raise NoCredentialsError
        method = getattr(credentials, "method", None)
        identity = session.client("sts", config=_client_config()).get_caller_identity()
    except Exception as exc:
        code = classify_failure(exc)
        _LOGGER.info("aws_connection.check_failed code=%s kind=%s", code, type(exc).__name__)
        return _failure(code, connection, region, effective_profile, mask_identity)

    account = str(identity.get("Account", "")) or None
    arn = str(identity.get("Arn", "")) or None
    models = tuple(
        _check_model(session, role, model_id, region) for role, model_id in model_ids.items() if model_id
    )
    _LOGGER.info("aws_connection.checked method=%s models=%d", method, len(models))
    return ConnectionReport(
        ok=True,
        source=connection.source,
        profile=None if mask_identity else effective_profile,
        region=region,
        credential_method=str(method) if method else None,
        account_id=mask_account(account) if mask_identity else account,
        principal_arn=None if mask_identity else arn,
        identity_masked=mask_identity,
        models=models,
    )


def _check_model(session: Any, role: ModelRole, model_id: str, region: str) -> ModelCheck:
    """One model's availability, or `unverified` with the reason AWS would not say.

    A denied availability check is not a failed connection. The deployment's task role is scoped to
    `bedrock:InvokeModel` on named models and nothing else, so it will usually be refused this
    question while being perfectly able to use the model - reporting that as "unavailable" would
    send an operator chasing a permission they do not need.
    """
    from botocore.exceptions import ClientError

    try:
        answer = session.client("bedrock", config=_client_config()).get_foundation_model_availability(
            modelId=model_id
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _DENIED_CODES:
            detail = (
                "This identity may not ask Bedrock about model access "
                "(bedrock:GetFoundationModelAvailability). The model may still work; the first real call "
                "will tell."
            )
        elif code in _UNKNOWN_ID_CODES:
            detail = (
                "Bedrock's availability check does not recognise this id. Cross-region inference profile "
                "ids (us., eu., apac.) cannot be checked this way."
            )
        else:
            detail = f"The availability check was refused ({code or 'no code'})."
        return ModelCheck(role=role, model_id=model_id, status=ModelStatus.UNVERIFIED, detail=detail)
    except Exception as exc:
        _LOGGER.info("aws_connection.model_check_failed kind=%s", type(exc).__name__)
        return ModelCheck(
            role=role,
            model_id=model_id,
            status=ModelStatus.UNVERIFIED,
            detail="The availability check could not run.",
        )

    problems: list[str] = []
    if answer.get("regionAvailability") != "AVAILABLE":
        problems.append(f"it is not offered in {region}")
    if answer.get("entitlementAvailability") != "AVAILABLE":
        problems.append("model access has not been granted to this account (Bedrock console, Model access)")
    if answer.get("authorizationStatus") != "AUTHORIZED":
        problems.append("this identity is not authorised to use it")
    agreement = (answer.get("agreementAvailability") or {}).get("status")
    if agreement not in (None, "AVAILABLE"):
        problems.append("its end-user licence agreement has not been accepted")
    if problems:
        return ModelCheck(
            role=role,
            model_id=model_id,
            status=ModelStatus.UNAVAILABLE,
            detail="Unavailable: " + "; ".join(problems) + ".",
        )
    return ModelCheck(
        role=role,
        model_id=model_id,
        status=ModelStatus.AVAILABLE,
        detail="Enabled for this account and region.",
    )


def _failure(
    code: str,
    connection: AwsConnection,
    region: str,
    effective_profile: str | None,
    mask_identity: bool,
) -> ConnectionReport:
    message, hint = FAILURES[code]
    shown = effective_profile or "default"
    flag = f" --profile {effective_profile}" if effective_profile else ""
    return ConnectionReport(
        ok=False,
        source=connection.source,
        profile=None if mask_identity else effective_profile,
        region=region,
        identity_masked=mask_identity,
        error_code=code,
        message=message.format(profile=shown),
        hint=hint.format(profile=shown, profile_flag=flag),
    )


def _client_config() -> Any:
    """Short timeouts and no retries: a person is waiting on this, and a retry would only hide the answer."""
    from botocore.config import Config

    return Config(
        connect_timeout=_CHECK_TIMEOUT_S, read_timeout=_CHECK_TIMEOUT_S, retries={"max_attempts": 1}
    )
