"""A one-way mirror of this product's registry into the SageMaker Model Registry. Off by default.

The SageMaker Model Registry is where an AWS-native MLOps team expects to find a model: it is what
their pipelines, their approval workflow and their console are pointed at. This product's registry
is not that, and is not going to become that - the champion rule, the stale-approval refusal and
the `pending_approval` state are this product's meaning of "approved", and handing that decision to
another system would be handing over the rule with it.

So the relationship is a mirror and the direction is fixed (DEC-348):

**One way.** `mirror()` writes; nothing here reads a model package back, and no SageMaker approval
can change a champion. Anything else would be two registries racing to own one decision, and the
one that wins would be the one nobody wrote the rule in.

**Off by default.** `enabled=False`, so a deployment that has not asked for the mirror makes no
SageMaker call at all. A product that writes into a shared account's model registry without being
asked is a product doing something an operator has to clean up.

**Never fatal.** Every failure is caught and logged. A mirror is a copy of a fact, so losing the
copy costs a console page its freshness, while raising here would cost a customer their model
registration for the sake of that page. `S3ModelRegistry` also guards the call, so the promise
holds even if this module is one day given a bug.

Honesty about what has been tested: these two calls have been exercised against `moto`, which
accepts a versioned model package carrying only metadata. They have **not** been run against real
SageMaker - this repository has no AWS account (plan section 13.3). `InferenceSpecification` is
therefore optional and only sent when a deployment supplies both an image and a model data URL,
because this product has no honest value for either: an AutoGluon predictor is published as a
*directory*, and `ModelDataUrl` wants a single `model.tar.gz`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, Self, cast

from engine.contracts import ModelStatus
from engine.settings import Settings
from engine.utils.logging import get_logger, log_failure

if TYPE_CHECKING:
    from engine.contracts import ModelVersion

__all__ = [
    "APPROVAL_STATUS",
    "SageMakerModelRegistryMirror",
    "SageMakerRegistryConfig",
]

_LOGGER = get_logger(__name__)

APPROVAL_STATUS: Final[dict[ModelStatus, str]] = {
    ModelStatus.CANDIDATE: "PendingManualApproval",
    ModelStatus.PENDING_APPROVAL: "PendingManualApproval",
    ModelStatus.CHAMPION: "Approved",
    ModelStatus.ARCHIVED: "Rejected",
}
"""This product's lifecycle, in SageMaker's three words.

SageMaker offers `Approved`, `Rejected` and `PendingManualApproval` and this product has four
states, so the map is lossy in one place on purpose: `candidate` and `pending_approval` both become
`PendingManualApproval`, because the difference between them is *this* registry's business - one
beat the champion and is waiting for a human, the other did not - and SageMaker has no word for it.
`archived` becomes `Rejected` rather than being left alone, so a console reader never sees a model
this product retired still sitting there as approved.
"""

_ALREADY_EXISTS: Final[frozenset[str]] = frozenset({"ValidationException", "ResourceInUse"})
"""Error codes that mean "the group is already there", which is the state this code wanted."""


class _SageMakerClient(Protocol):
    """The two SageMaker calls this module makes, typed so `mypy --strict` has something to check."""

    def create_model_package_group(self, **kwargs: Any) -> dict[str, Any]: ...

    def create_model_package(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SageMakerRegistryConfig:
    """What the mirror needs, derived from `Settings` and from an explicit decision to turn it on."""

    enabled: bool = False
    """Off unless somebody says otherwise. Nothing is created and no call is made while it is False."""

    group_prefix: str = "marketing-ai"
    """Model package groups are named `<prefix>-<use case id>`, one group per use case."""

    region: str | None = None
    """Region the model packages are created in; `None` leaves it to the client's own resolution."""

    image_uri: str | None = None
    """Inference image. With `model_data_url`, it becomes an `InferenceSpecification`."""

    model_data_url: str | None = None
    """S3 URL of a `model.tar.gz`. This product publishes a directory, so it is normally `None`."""

    tags: dict[str, str] = field(default_factory=dict)
    """Cost-allocation tags every group carries: the same ones the rest of the deployment uses."""

    @classmethod
    def from_settings(cls, settings: Settings, *, enabled: bool = False) -> Self:
        """The config this deployment describes, with the mirror off unless the caller turns it on.

        `enabled` is an argument rather than a `Settings` field because there is no field for it:
        `Settings` describes where things live, and nothing in it says "also write to SageMaker".
        A deployment that wants the mirror therefore has to construct it deliberately, which is the
        right amount of friction for a write into a shared account (DEC-348).
        """
        tags = {"product": settings.sagemaker_job_name_prefix, "env": settings.env.value}
        if settings.client_id is not None:
            tags["client"] = settings.client_id
        return cls(
            enabled=enabled,
            group_prefix=settings.sagemaker_job_name_prefix,
            region=settings.region,
            tags=tags,
        )

    def group_name(self, use_case_id: str) -> str:
        """The model package group one use case's versions go into."""
        return f"{self.group_prefix}-{use_case_id}"


class SageMakerModelRegistryMirror:
    """Writes each registered version into the SageMaker Model Registry as a model package.

    Satisfies `engine.aws.s3_registry.ModelMirror` structurally; `S3ModelRegistry` takes it as an
    optional constructor argument and never learns what it is.

    The client is built lazily, on the first call that is actually going to make one, so importing
    this module costs nothing and a disabled mirror never loads boto3 (DEC-306).
    """

    def __init__(self, config: SageMakerRegistryConfig, *, client: _SageMakerClient | None = None) -> None:
        self._config = config
        self._client = client
        self._groups: set[str] = set()

    @property
    def enabled(self) -> bool:
        """Whether this mirror does anything at all."""
        return self._config.enabled

    def mirror(self, version: ModelVersion) -> str | None:
        """Write `version` as a model package; the package ARN, or `None` when nothing was written.

        `None` covers every uninteresting outcome - the mirror is off, or the call failed and was
        logged - because the caller's only correct response to all of them is the same: carry on.
        The registry write has already happened and is not in question here.
        """
        if not self._config.enabled:
            return None
        group = self._config.group_name(version.use_case_id)
        try:
            client = self._sagemaker_client()
            self._ensure_group(client, group)
            response = client.create_model_package(**self._package_request(version, group))
        except Exception as exc:
            log_failure(_LOGGER, f"sagemaker_registry.mirror model={version.model_id}", exc)
            return None
        arn = response.get("ModelPackageArn")
        return None if arn is None else str(arn)

    def _package_request(self, version: ModelVersion, group: str) -> dict[str, Any]:
        """The `create_model_package` arguments for one version.

        `CustomerMetadataProperties` is a flat map of strings, which is exactly the shape of "the
        facts a console reader would want": which run, which metric, what it scored, where the
        files are. No customer data goes in it - every value here is an id, a metric name or a
        number this product measured (plan section 13.7).
        """
        metadata = {
            "model_id": version.model_id,
            "use_case_id": version.use_case_id,
            "version": str(version.version),
            "run_id": version.run_id,
            "status": str(version.status),
            "metric": str(version.metric),
            "test_score": repr(version.test_score),
            "predictor_key": version.predictor_key,
            "schema_key": version.schema_key,
            "engine_version": version.engine_version,
            "autogluon_version": version.autogluon_version,
        }
        request: dict[str, Any] = {
            "ModelPackageGroupName": group,
            "ModelPackageDescription": (
                f"{version.model_id} v{version.version} of {version.use_case_id}, "
                f"{version.metric}={version.test_score!r}"
            ),
            "ModelApprovalStatus": APPROVAL_STATUS[version.status],
            "CustomerMetadataProperties": metadata,
        }
        specification = self._inference_specification()
        if specification is not None:
            request["InferenceSpecification"] = specification
        return request

    def _inference_specification(self) -> dict[str, Any] | None:
        """An `InferenceSpecification`, or `None` when this deployment cannot state one honestly.

        It needs a container image *and* a `ModelDataUrl` pointing at a single `model.tar.gz`. This
        product publishes an AutoGluon predictor as a directory, so unless a deployment has packed
        one and told this config where it is, there is no URL to give and an invented one would be
        a model package that cannot be deployed (plan section 13.3).
        """
        if self._config.image_uri is None or self._config.model_data_url is None:
            return None
        return {
            "Containers": [{"Image": self._config.image_uri, "ModelDataUrl": self._config.model_data_url}],
            "SupportedContentTypes": ["text/csv"],
            "SupportedResponseMIMETypes": ["text/csv"],
        }

    def _ensure_group(self, client: _SageMakerClient, group: str) -> None:
        """Create the use case's model package group, unless it is already there.

        "Already there" is the expected outcome for every version after the first, so it is not a
        failure: the code that follows wanted a group to exist, and one does. The names are cached
        per instance so a long-lived process makes the call once rather than once per registration.
        """
        if group in self._groups:
            return
        try:
            client.create_model_package_group(
                ModelPackageGroupName=group,
                ModelPackageGroupDescription=f"Marketing AI model versions for {group}.",
                Tags=[{"Key": key, "Value": value} for key, value in sorted(self._config.tags.items())],
            )
        except Exception as exc:
            if _error_code(exc) not in _ALREADY_EXISTS:
                raise
            _LOGGER.info("sagemaker_registry.group exists group=%s", group)
        self._groups.add(group)

    def _sagemaker_client(self) -> _SageMakerClient:
        """The boto3 client, built on first use.

        `cast` because boto3's own stubs type these two calls with TypedDict keyword arguments,
        which is a narrower shape than the `**kwargs: Any` protocol above and therefore not a
        structural match. The protocol is the contract this module is tested against; the cast says
        so once, here, rather than weakening it.
        """
        client = self._client
        if client is None:
            from engine.aws.secrets import quiet_aws_wire_logs

            quiet_aws_wire_logs()
            import boto3

            client = cast("_SageMakerClient", boto3.client("sagemaker", region_name=self._config.region))
            self._client = client
        return client


def _error_code(exc: BaseException) -> str:
    """The AWS error code inside a `ClientError`, or an empty string for anything else."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""
