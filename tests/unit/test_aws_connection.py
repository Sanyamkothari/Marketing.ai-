"""`engine.aws_connection`: the contract a person relies on when they choose whose AWS credentials to use.

`tests/integration/test_api_connection_security.py` attacks the HTTP boundary - other sites, DNS
rebinding, planted symlinks, a thread pool held hostage. This file pins what the module *promises*,
which is the part a later change is most likely to erode without anyone attacking anything.

**The choice can only ever be a name.** `AwsConnection` has two fields, a source and a profile name,
and refuses a third. A name is checked against a character set that is safe to print inside the
shell command a hint tells a person to run.

**Who may choose is decided by the deployment first.** Loopback alone never unlocks a deployment,
because a reverse proxy on the same host makes every request look like loopback.

**What is remembered is a name, readable by its owner, on a laptop only.** A deployed server
neither writes the file nor reads one that is already there, and a file that no longer parses means
the default chain - never a guessed profile.

**A check says who, never what.** A failure is a code and a sentence written in the module; the
exception's own text, which can quote an access key id, never reaches the report. A caller who could
not have chosen sees the account masked and no ARN at all.

**The choice reaches Bedrock.** A profile passed to `build_client` is the profile boto3's session is
built with, and the default chain is `None` - the behaviour before a profile could be chosen at all.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
from botocore import exceptions as boto
from pydantic import ValidationError

from engine.aws_connection import (
    FAILURES,
    AwsConnection,
    ConnectionLockedError,
    CredentialSource,
    LockReason,
    ModelStatus,
    available_profiles,
    check_connection,
    classify_failure,
    connection_path,
    editability,
    is_loopback,
    load_connection,
    mask_account,
    save_connection,
    valid_profile_name,
    valid_region,
)
from engine.config import LlmBackend, LlmConfig
from engine.llm import BedrockLLMClient, FakeLLMClient, LLMError, build_client
from engine.settings import Settings

ACCOUNT = "123456789012"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Analyst/alice"
KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AVAILABLE: dict[str, Any] = {
    "regionAvailability": "AVAILABLE",
    "entitlementAvailability": "AVAILABLE",
    "authorizationStatus": "AUTHORIZED",
    "agreementAvailability": {"status": "AVAILABLE"},
}


# ---------------------------------------------------------------------------
# Fakes: a boto3 session that answers from a script
# ---------------------------------------------------------------------------
class _Credentials:
    method = "sso"


class _Sts:
    def __init__(self, error: BaseException | None) -> None:
        self._error = error

    def get_caller_identity(self) -> dict[str, str]:
        if self._error is not None:
            raise self._error
        return {"Account": ACCOUNT, "Arn": ARN, "UserId": "AROAEXAMPLE:alice"}


class _Bedrock:
    def __init__(self, answers: dict[str, dict[str, Any] | BaseException]) -> None:
        self._answers = answers

    def get_foundation_model_availability(self, modelId: str) -> dict[str, Any]:  # noqa: N803 - boto3's name
        answer = self._answers[modelId]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Session:
    def __init__(
        self,
        *,
        credentials: _Credentials | None = None,
        sts_error: BaseException | None = None,
        answers: dict[str, dict[str, Any] | BaseException] | None = None,
    ) -> None:
        self._credentials = credentials if credentials is not None else _Credentials()
        self._sts = _Sts(sts_error)
        self._bedrock = _Bedrock(answers or {})
        self.asked: list[tuple[str | None, str]] = []

    def factory(self, profile: str | None, region: str) -> _Session:
        self.asked.append((profile, region))
        return self

    def get_credentials(self) -> _Credentials | None:
        return self._credentials

    def client(self, name: str, config: Any = None) -> Any:
        return {"sts": self._sts, "bedrock": self._bedrock}[name]


def _client_error(code: str, message: str = "") -> boto.ClientError:
    return boto.ClientError({"Error": {"Code": code, "Message": message}}, "SomeOperation")


def _settings(env: str, data_dir: Path) -> Settings:
    return Settings(env=env, data_dir=data_dir)


# ---------------------------------------------------------------------------
# The choice is a name
# ---------------------------------------------------------------------------
def test_a_profile_source_needs_a_profile_name() -> None:
    """Choosing "a profile" without naming one is not a choice."""
    with pytest.raises(ValidationError):
        AwsConnection(source=CredentialSource.PROFILE)


def test_the_default_chain_names_no_profile() -> None:
    """A profile beside the default chain would be ignored, so it is refused rather than kept."""
    with pytest.raises(ValidationError):
        AwsConnection(source=CredentialSource.DEFAULT_CHAIN, profile="alice")


@pytest.mark.parametrize("field", ["aws_secret_access_key", "aws_access_key_id", "aws_session_token"])
def test_a_choice_cannot_carry_a_key(field: str) -> None:
    """The model has nowhere to put a key, so a body that brings one is refused, not quietly trimmed."""
    with pytest.raises(ValidationError):
        AwsConnection.model_validate({"source": "profile", "profile": "alice", field: "anything"})


@pytest.mark.parametrize("name", ["alice", "sso-work", "team.prod", "a_b+c=d,e@f", "Dev01"])
def test_ordinary_profile_names_are_accepted(name: str) -> None:
    """Every character a real profile name uses is allowed."""
    assert valid_profile_name(name)
    assert AwsConnection(source=CredentialSource.PROFILE, profile=name).profile == name


@pytest.mark.parametrize("name", ["", "a b", "x;rm -rf ~", "$(whoami)", "`id`", "a|b", "-leading", "é"])
def test_a_name_that_means_something_to_a_shell_is_refused(name: str) -> None:
    """A name is printed inside `aws sso login --profile <name>`, so shell syntax cannot be one."""
    assert not valid_profile_name(name)


@pytest.mark.parametrize("region", ["ap-south-1", "us-east-1", "eu-central-2", "us-gov-west-1"])
def test_real_region_names_are_regions(region: str) -> None:
    """The shapes AWS uses are accepted."""
    assert valid_region(region)


@pytest.mark.parametrize("region", ["", "evil.example.com", "us-east-1.evil", "ap_south_1", "../x"])
def test_anything_that_could_steer_an_endpoint_is_not_a_region(region: str) -> None:
    """botocore builds a hostname from the region, so a dot or a slash never reaches it."""
    assert not valid_region(region)


# ---------------------------------------------------------------------------
# Who may choose
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "127.9.9.9", "::1", "localhost"])
def test_this_machine_is_loopback(host: str) -> None:
    """The whole of 127/8, IPv6 loopback and the name localhost are this machine."""
    assert is_loopback(host)


@pytest.mark.parametrize("host", [None, "", "0.0.0.0", "10.0.0.5", "192.168.1.20", "127.evil.example"])
def test_every_other_address_is_not(host: str | None) -> None:
    """An address is parsed, not pattern-matched, so a hostname containing "127" is not loopback."""
    assert not is_loopback(host)


def test_a_laptop_asked_from_itself_may_choose(tmp_path: Path) -> None:
    """The one case in which the identity is a person's to choose."""
    assert editability(_settings("local", tmp_path), "127.0.0.1") is None


def test_a_laptop_asked_from_the_network_may_not(tmp_path: Path) -> None:
    """A colleague opening the same URL over the LAN cannot switch whose account is billed."""
    assert editability(_settings("local", tmp_path), "192.168.1.20") is LockReason.REMOTE_CLIENT


@pytest.mark.parametrize("env", ["dev", "staging"])
def test_a_deployment_is_locked_even_from_loopback(env: str, tmp_path: Path) -> None:
    """A reverse proxy on the same host makes every request loopback; the deployment is checked first."""
    assert editability(_settings(env, tmp_path), "127.0.0.1") is LockReason.DEPLOYED


def test_an_account_is_masked_to_its_last_four_digits() -> None:
    """Enough to tell two accounts apart, not enough to name one."""
    assert mask_account(ACCOUNT) == "********9012"
    assert mask_account(None) is None


# ---------------------------------------------------------------------------
# What is remembered
# ---------------------------------------------------------------------------
def test_a_saved_choice_comes_back_and_only_its_owner_can_read_it(tmp_path: Path) -> None:
    """The file round-trips and is 0600: another user on the machine cannot see which profile is used."""
    settings = _settings("local", tmp_path)
    chosen = AwsConnection(source=CredentialSource.PROFILE, profile="alice")
    save_connection(settings, chosen)
    assert load_connection(settings) == chosen
    assert stat.S_IMODE(connection_path(settings).stat().st_mode) == 0o600


def test_the_file_holds_a_source_and_a_name_and_nothing_else(tmp_path: Path) -> None:
    """No account, no ARN, no key material: rule 1 of engine/settings.py, and all the model can hold."""
    settings = _settings("local", tmp_path)
    save_connection(settings, AwsConnection(source=CredentialSource.PROFILE, profile="alice"))
    text = connection_path(settings).read_text()
    assert text == '{"source":"profile","profile":"alice"}'


def test_choosing_the_default_again_forgets_the_file(tmp_path: Path) -> None:
    """The default chain is the absence of a choice, so it is stored as the absence of a file."""
    settings = _settings("local", tmp_path)
    save_connection(settings, AwsConnection(source=CredentialSource.PROFILE, profile="alice"))
    save_connection(settings, AwsConnection())
    assert not connection_path(settings).exists()
    assert load_connection(settings) == AwsConnection()


def test_a_file_that_no_longer_parses_is_the_default_chain_never_a_guess(tmp_path: Path) -> None:
    """Falling back to a guessed profile would be worse than falling back to the one AWS itself picks."""
    settings = _settings("local", tmp_path)
    path = connection_path(settings)
    path.parent.mkdir(parents=True)
    path.write_text('{"source": "profile", "profile": ')
    path.chmod(0o600)
    assert load_connection(settings) == AwsConnection()


@pytest.mark.parametrize("env", ["dev", "staging"])
def test_a_deployment_ignores_a_file_that_is_already_there(env: str, tmp_path: Path) -> None:
    """Nothing planted in a deployment's data directory can redirect whose account it calls Bedrock as."""
    local = _settings("local", tmp_path)
    save_connection(local, AwsConnection(source=CredentialSource.PROFILE, profile="alice"))
    assert load_connection(_settings(env, tmp_path)) == AwsConnection()


@pytest.mark.parametrize("env", ["dev", "staging"])
def test_a_deployment_refuses_to_write_one(env: str, tmp_path: Path) -> None:
    """The engine refuses on its own account, not only because a route happened to check first."""
    with pytest.raises(ConnectionLockedError):
        save_connection(_settings(env, tmp_path), AwsConnection(source=CredentialSource.PROFILE, profile="x"))


# ---------------------------------------------------------------------------
# Which profiles exist
# ---------------------------------------------------------------------------
@pytest.fixture
def aws_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "aws"
    home.mkdir()
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / "credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return home


def test_profiles_are_listed_from_both_files_by_name_only(aws_files: Path) -> None:
    """SSO, config and credentials-file profiles all appear, sorted, and a key is never part of a name."""
    (aws_files / "config").write_text(
        "[default]\nregion = ap-south-1\n"
        "[profile alice]\nregion = ap-south-1\n"
        "[profile sso-work]\nsso_session = corp\nsso_account_id = 111122223333\nsso_role_name = Analyst\n"
    )
    (aws_files / "credentials").write_text(
        f"[bob]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = abc/def+ghi\n"
    )
    profiles = available_profiles()
    assert profiles == ("alice", "bob", "default", "sso-work")
    assert KEY_ID not in repr(profiles)


def test_a_profile_this_module_could_not_use_is_not_offered(aws_files: Path) -> None:
    """Showing a name and then refusing it is worse than not showing it."""
    (aws_files / "config").write_text("[profile fine]\n[profile has space]\n")
    (aws_files / "credentials").write_text("")
    assert available_profiles() == ("fine",)


def test_a_malformed_config_lists_nothing_rather_than_breaking_the_screen(aws_files: Path) -> None:
    """A broken ~/.aws/config is the AWS CLI's problem to report, not a 500 on this settings screen."""
    (aws_files / "config").write_text("[profile unterminated\nregion = x\n")
    (aws_files / "credentials").write_text("")
    assert available_profiles() == ()


# ---------------------------------------------------------------------------
# Naming a failure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (boto.ProfileNotFound(profile="ghost"), "PROFILE_NOT_FOUND"),
        (boto.SSOTokenLoadError(error_msg="expired"), "SSO_LOGIN_REQUIRED"),
        (boto.UnauthorizedSSOTokenError(), "SSO_LOGIN_REQUIRED"),
        (boto.TokenRetrievalError(provider="sso", error_msg="expired"), "SSO_LOGIN_REQUIRED"),
        (boto.NoCredentialsError(), "NO_CREDENTIALS"),
        (boto.PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"), "NO_CREDENTIALS"),
        (
            boto.CredentialRetrievalError(provider="custom-process", error_msg="exit 1"),
            "CREDENTIALS_UNAVAILABLE",
        ),
        (
            boto.EndpointConnectionError(endpoint_url="https://sts.ap-south-1.amazonaws.com"),
            "AWS_UNREACHABLE",
        ),
        (_client_error("ExpiredToken"), "CREDENTIALS_EXPIRED"),
        (_client_error("InvalidClientTokenId"), "CREDENTIALS_INVALID"),
        (_client_error("SignatureDoesNotMatch"), "CREDENTIALS_INVALID"),
        (_client_error("SomethingNew"), "CONNECTION_FAILED"),
        (RuntimeError("anything else"), "CONNECTION_FAILED"),
    ],
)
def test_each_failure_is_named_by_its_type_and_code(exc: BaseException, code: str) -> None:
    """The code decides the sentence a person reads, so each family of failure gets its own."""
    assert classify_failure(exc) == code
    assert code in FAILURES


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
MODELS: dict[str, str] = {"generation": "gen-model", "judge": "gen-model", "embedding": "embed-model"}


def test_a_working_connection_says_who_and_where(monkeypatch: pytest.MonkeyPatch) -> None:
    """Account, principal, credential method and region - the identity a person needs to recognise."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    session = _Session(answers={"gen-model": AVAILABLE, "embed-model": AVAILABLE})
    report = check_connection(
        AwsConnection(source=CredentialSource.PROFILE, profile="alice"),
        region="ap-south-1",
        model_ids=MODELS,
        mask_identity=False,
        session_factory=session.factory,
    )
    assert report.ok
    assert (report.account_id, report.principal_arn) == (ACCOUNT, ARN)
    assert (report.credential_method, report.profile, report.region) == ("sso", "alice", "ap-south-1")
    assert session.asked == [("alice", "ap-south-1")]
    assert {check.status for check in report.models} == {ModelStatus.AVAILABLE}


def test_a_caller_who_could_not_choose_sees_only_a_masked_account() -> None:
    """No ARN, no profile, four digits of the account: enough to confirm, not enough to learn."""
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={},
        mask_identity=True,
        session_factory=_Session().factory,
    )
    assert report.ok and report.identity_masked
    assert report.account_id == "********9012"
    assert report.principal_arn is None
    assert report.profile is None


def test_the_default_chain_reports_the_aws_profile_it_is_using(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the default chain the profile in effect is AWS_PROFILE, and saying so avoids a surprise."""
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    session = _Session()
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={},
        mask_identity=False,
        session_factory=session.factory,
    )
    assert report.profile == "from-env"
    assert session.asked == [(None, "ap-south-1")]


def test_an_aws_error_message_never_reaches_the_report() -> None:
    """Some AWS messages quote the credential scope, which names the key id; only codes are used."""
    leaky = _client_error(
        "SignatureDoesNotMatch", f"Credential={KEY_ID}/20260922/ap-south-1/sts/aws4_request"
    )
    report = check_connection(
        AwsConnection(source=CredentialSource.PROFILE, profile="alice"),
        region="ap-south-1",
        model_ids={},
        mask_identity=False,
        session_factory=_Session(sts_error=leaky).factory,
    )
    assert not report.ok
    assert report.error_code == "CREDENTIALS_INVALID"
    assert KEY_ID not in report.model_dump_json()


def test_a_failure_hint_names_the_profile_to_log_in_with() -> None:
    """The hint is a command a person can paste, so it carries the profile they chose."""
    report = check_connection(
        AwsConnection(source=CredentialSource.PROFILE, profile="sso-work"),
        region="ap-south-1",
        model_ids={},
        mask_identity=False,
        session_factory=_Session(sts_error=boto.UnauthorizedSSOTokenError()).factory,
    )
    assert report.error_code == "SSO_LOGIN_REQUIRED"
    assert report.hint is not None and "aws sso login --profile sso-work" in report.hint


def test_missing_credentials_are_a_named_failure_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A laptop with no credentials at all gets a sentence, and the check itself does not raise."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    session = _Session()
    session._credentials = None
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={},
        mask_identity=False,
        session_factory=session.factory,
    )
    assert (report.ok, report.error_code) == (False, "NO_CREDENTIALS")


@pytest.mark.parametrize(
    ("field", "value", "phrase"),
    [
        ("regionAvailability", "NOT_AVAILABLE", "not offered in ap-south-1"),
        ("entitlementAvailability", "NOT_AVAILABLE", "model access has not been granted"),
        ("authorizationStatus", "NOT_AUTHORIZED", "not authorised"),
        ("agreementAvailability", {"status": "PENDING"}, "agreement has not been accepted"),
    ],
)
def test_each_reason_a_model_is_unavailable_is_said_in_words(field: str, value: Any, phrase: str) -> None:
    """Each of Bedrock's four availability answers maps to the fix a person would actually make."""
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={"generation": "gen-model"},
        mask_identity=False,
        session_factory=_Session(answers={"gen-model": {**AVAILABLE, field: value}}).factory,
    )
    (check,) = report.models
    assert check.status is ModelStatus.UNAVAILABLE
    assert phrase in check.detail


def test_being_refused_the_availability_question_is_unverified_not_unavailable() -> None:
    """A task role scoped to InvokeModel is refused this question and can still use the model."""
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={"generation": "gen-model"},
        mask_identity=False,
        session_factory=_Session(
            answers={"gen-model": _client_error("AccessDeniedException", KEY_ID)}
        ).factory,
    )
    (check,) = report.models
    assert check.status is ModelStatus.UNVERIFIED
    assert KEY_ID not in check.detail


def test_an_inference_profile_id_is_unverified_rather_than_called_broken() -> None:
    """The availability API does not know cross-region ids; that is a limit of the check, not the model."""
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={"generation": "apac.some-model"},
        mask_identity=False,
        session_factory=_Session(answers={"apac.some-model": _client_error("ValidationException")}).factory,
    )
    (check,) = report.models
    assert check.status is ModelStatus.UNVERIFIED
    assert "inference profile" in check.detail


def test_a_model_the_configuration_does_not_name_is_not_checked() -> None:
    """The fake backend's configuration names no models, and there is nothing on AWS to ask about."""
    report = check_connection(
        AwsConnection(),
        region="ap-south-1",
        model_ids={"generation": "", "judge": "", "embedding": ""},
        mask_identity=False,
        session_factory=_Session().factory,
    )
    assert report.ok and report.models == ()


def test_a_region_that_is_not_a_region_is_refused_before_boto3_sees_it() -> None:
    """The check refuses to build an endpoint from something that is not a region name."""
    with pytest.raises(ValueError, match="not an AWS region"):
        check_connection(
            AwsConnection(),
            region="evil.example",
            model_ids={},
            mask_identity=False,
            session_factory=_Session().factory,
        )


# ---------------------------------------------------------------------------
# The choice reaches Bedrock
# ---------------------------------------------------------------------------
BEDROCK_LLM = LlmConfig(
    backend=LlmBackend.BEDROCK,
    region="ap-south-1",
    generation_model_id="gen-model",
    judge_model_id="gen-model",
    embedding_model_id="embed-model",
)


def _capture_sessions(monkeypatch: pytest.MonkeyPatch, raise_for: str | None = None) -> list[dict[str, Any]]:
    import boto3

    built: list[dict[str, Any]] = []

    class _BotoSession:
        def __init__(self, profile_name: str | None = None, region_name: str | None = None) -> None:
            if raise_for is not None and profile_name == raise_for:
                raise boto.ProfileNotFound(profile=profile_name)
            built.append({"profile": profile_name, "region": region_name})

        def client(self, name: str, config: Any = None) -> object:
            built[-1]["service"] = name
            return object()

    monkeypatch.setattr(boto3, "Session", _BotoSession)
    return built


def test_the_chosen_profile_is_the_one_boto3_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile passed to build_client is the profile the Bedrock session is built with."""
    built = _capture_sessions(monkeypatch)
    client = build_client(BEDROCK_LLM, profile="alice")
    assert isinstance(client, BedrockLLMClient)
    _ = client.client
    assert built == [{"profile": "alice", "region": "ap-south-1", "service": "bedrock-runtime"}]


def test_no_choice_is_the_default_chain_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` is boto3's own default chain, so nobody who never opens the screen sees a change."""
    built = _capture_sessions(monkeypatch)
    _ = build_client(BEDROCK_LLM).client
    assert built[0]["profile"] is None


def test_a_profile_that_has_gone_is_an_llm_error_not_a_botocore_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile deleted after it was chosen fails as this package's own error, which the jobs handle."""
    _capture_sessions(monkeypatch, raise_for="gone")
    with pytest.raises(LLMError) as raised:
        _ = build_client(BEDROCK_LLM, profile="gone").client
    assert raised.value.code == "LLM_UNAVAILABLE"


def test_the_fake_backend_ignores_a_profile() -> None:
    """A profile names an AWS identity, and the fake calls nothing on AWS."""
    assert isinstance(build_client(LlmConfig(), profile="alice"), FakeLLMClient)


def test_a_chosen_profile_without_keys_is_told_the_exact_command_for_that_profile() -> None:
    """A person who picked `team-prod` is told to configure `team-prod`, not to go and pick a profile."""
    session = _Session()
    session._credentials = None
    report = check_connection(
        AwsConnection(source=CredentialSource.PROFILE, profile="team-prod"),
        region="ap-south-1",
        model_ids={},
        mask_identity=False,
        session_factory=session.factory,
    )
    assert report.message is not None and "'team-prod'" in report.message
    assert report.hint is not None and "aws configure --profile team-prod" in report.hint


def test_a_masked_caller_is_not_told_the_profile_name_in_the_failure_either() -> None:
    """The report hides the profile from a masked caller, so its sentences must not reveal it instead."""
    session = _Session()
    session._credentials = None
    report = check_connection(
        AwsConnection(source=CredentialSource.PROFILE, profile="team-prod"),
        region="ap-south-1",
        model_ids={},
        mask_identity=True,
        session_factory=session.factory,
    )
    assert "team-prod" not in report.model_dump_json()
