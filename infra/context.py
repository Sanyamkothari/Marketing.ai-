"""Every knob this deployment has, read once, defaulted once, and refused when it is misspelt.

CDK context is a bag of strings with no schema. `cdk deploy -c db_storage_gb=50` and
`cdk deploy -c db_storage_bg=50` are equally acceptable to the toolkit: the second one synthesises
happily and quietly uses the default, and the only symptom is a database that is the wrong size
three weeks later. That is the same failure `engine/settings.py` refuses for environment variables
(DEC-304), and it is refused here for the same reason and in the same shape: a typed object, every
default in one place, and an unknown key is an error rather than a silent no-op (DEC-365).

Three things make this harder than "read a dict":

**Two spellings.** The Makefile and the deploy workflow are frozen and pass bare keys
(`-c env_name=dev`, `-c cdk_nag=true`, `-c image_digest=...`). A shared `cdk.json` or a team
convention may prefer the namespaced spelling `-c marketing-ai:env_name=dev`. Both are accepted and
mean the same thing; supplying both with *different* values is an error, because there is no
defensible answer to which one the operator meant.

**CDK's own keys share the bag.** Feature flags (`@aws-cdk/aws-s3:...`), toolkit keys
(`aws:cdk:enable-path-metadata`) and provider lookups (`availability-zones:account=...`) all live in
the same mapping. They are recognised structurally - a key that starts with `@` or contains `:` is
not ours - and ignored. What is left is a flat, unprefixed name, and a flat unprefixed name that we
do not know is a typo.

**The toolkit does not hand the app its context as a dict.** `constructs.Node` exposes
`try_get_context(key)` and no way to enumerate, so an unknown key cannot be *seen* through the App.
The CLI does pass the merged context (cdk.json, ~/.cdk.json, cdk.context.json and every `-c`) to the
app process in `CDK_CONTEXT_JSON`, which is where `context_from_environment()` reads it. Tests call
`AppContext.from_mapping` directly and never depend on that variable.

`env_name` is not a label. Section "what env_name changes" below is the complete list of what
differs between `dev` and `prod`, every entry of which is asserted in `tests/infra/test_env_name.py`
(DEC-367).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Final

__all__ = [
    "CDK_CONTEXT_ENV_VAR",
    "FARGATE_CPU_MEMORY",
    "KNOWN_KEYS",
    "NAMESPACE",
    "PROD",
    "REQUIRED_PRIVATE_ENDPOINTS",
    "AppContext",
    "ContextError",
    "context_from_environment",
]

NAMESPACE: Final[str] = "marketing-ai:"
"""The prefix that marks a context key as ours. The bare spelling is accepted too; see the docstring."""

CDK_CONTEXT_ENV_VAR: Final[str] = "CDK_CONTEXT_JSON"
"""Where the CDK CLI puts the merged context for the app process. Absent when the app is run directly."""

DEV: Final[str] = "dev"
PROD: Final[str] = "prod"
ENV_NAMES: Final[tuple[str, ...]] = (DEV, PROD)

FARGATE_CPU_MEMORY: Final[Mapping[int, tuple[int, int, int]]] = {
    256: (512, 2048, 512),
    512: (1024, 4096, 1024),
    1024: (2048, 8192, 1024),
    2048: (4096, 16384, 1024),
    4096: (8192, 30720, 1024),
    8192: (16384, 61440, 4096),
    16384: (32768, 122880, 8192),
}
"""CPU units -> (minimum memory MiB, maximum memory MiB, step).

Not a preference and not a measurement: this is the combination table AWS documents for the Fargate
launch type (Amazon ECS Developer Guide, "Task CPU and memory"). An invalid pair is refused by
`RegisterTaskDefinition` at deploy time, which is an hour into a pipeline; refusing it at synth time
costs nothing.
"""

REQUIRED_PRIVATE_ENDPOINTS: Final[tuple[str, ...]] = (
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
    "ssm",
)
"""Interface endpoints a task in a private subnet with no NAT gateway cannot start without.

`ecr.api` and `ecr.dkr` are the image pull (plus the S3 gateway endpoint, which carries the layer
blobs and is always created). `logs` is `awslogs`, which Fargate needs *before* the container is
running - without it the task stops with a log-driver error, not a log line. `secretsmanager` and
`ssm` are `load_settings()` with `MARKETING_AI_SETTINGS_SOURCE=aws`: the process exits on its first
statement, before it can say why.
"""


class ContextError(Exception):
    """A context key is unknown, or its value does not parse, or the combination cannot work.

    Raised during synthesis, so the operator sees it instead of a stack that deploys into something
    they did not ask for.
    """


def _as_bool(key: str, raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ContextError(f"{key}: expected a boolean (true/false), got {raw!r}.")


def _as_int(key: str, raw: object) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise ContextError(f"{key}: expected a whole number, got {raw!r}.") from None


def _as_float(key: str, raw: object) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        raise ContextError(f"{key}: expected a number, got {raw!r}.") from None


def _as_str(key: str, raw: object) -> str:
    text = str(raw).strip()
    if not text:
        raise ContextError(f"{key}: expected a non-empty string.")
    return text


def _as_tuple(key: str, raw: object) -> tuple[str, ...]:
    """A list, or a comma-separated string - the only spelling `-c` can carry."""
    if isinstance(raw, (list, tuple)):
        return tuple(_as_str(key, item) for item in raw)
    text = str(raw).strip()
    if not text:
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class AppContext:
    """The whole deployment description, typed, with every default supplied here and nowhere else.

    Every field is a context key of the same name, readable as `-c <name>=<value>` or
    `-c marketing-ai:<name>=<value>`. Fields whose default depends on `env_name` are documented
    beside the field and resolved in `from_mapping`, never at the point of use, so there is exactly
    one place to read to know what a `dev` deployment is.

    ## what env_name changes

    | | dev | prod |
    |---|---|---|
    | `db_multi_az` (default) | false | true |
    | `db_deletion_protection` | false | true |
    | `removal_policy_destroy` | true (bucket, key and database are destroyed with the stack) | false |
    | `https_only` | false unless `certificate_arn` is given | always; a deployment without a certificate is refused |
    | `log_retention_days` | 30 | 365 |
    | `cors_origins` (written to SSM) | `*` unless `domain_name` is given | always `https://<domain_name>`, because prod requires one |

    Nothing else is conditional on the name. `db_multi_az` is the only one of the five that can be
    overridden, because a dev deployment that wants to rehearse a failover should be able to.
    """

    env_name: str = DEV
    region: str = "ap-south-1"
    client_id: str | None = None

    bucket_name_prefix: str = "marketing-ai"
    kms_key_alias: str = ""  # defaulted from env_name: alias/marketing-ai-<env_name>

    db_instance_class: str = "db.t4g.small"
    db_storage_gb: int = 50
    db_multi_az: bool = False  # defaulted from env_name
    db_backup_retention_days: int = 7

    api_cpu: int = 2048
    api_memory: int = 8192
    api_min_tasks: int = 1
    api_max_tasks: int = 3
    # Container Insights is billed per metric collected per task, so it is opt-in rather than on by
    # accident - and opt-in rather than on-in-prod, because "prod can afford it" is a guess about a
    # bill nobody here has seen. AwsSolutions-ECS4 asks for it; leaving it off is a suppression that
    # `infra/nag_suppressions.py` adds only for the deployment that actually has it off.
    container_insights: bool = False

    sagemaker_instance_train: str = "ml.m5.2xlarge"
    sagemaker_instance_process: str = "ml.m5.xlarge"
    max_concurrent_jobs: int = 3

    bedrock_enabled: bool = False
    bedrock_model_ids: tuple[str, ...] = ()

    nat_gateways: int = 1
    vpc_endpoints: tuple[str, ...] = ()

    alert_email: str | None = None
    monthly_budget_usd: float | None = None
    alarm_thresholds: tuple[str, ...] = ()

    domain_name: str | None = None
    certificate_arn: str | None = None

    image_digest: str | None = None
    cdk_nag: bool = False

    # derived; not context keys
    log_retention_days: int = field(default=30)
    https_only: bool = field(default=False)
    db_deletion_protection: bool = field(default=False)
    removal_policy_destroy: bool = field(default=True)

    @property
    def is_prod(self) -> bool:
        """Whether this is the production deployment. The only thing that reads `env_name` directly."""
        return self.env_name == PROD

    @property
    def alarm_threshold_values(self) -> dict[str, str]:
        """`-c alarm_thresholds=Name=value,Other=value` as a mapping.

        `infra/observability/alarms.json` deliberately commits no threshold for an alarm whose
        threshold would be a measurement nobody has taken - it leaves a `${Name}` substitution and
        says why. This is where the operator supplies one. An alarm whose substitution is not
        supplied here is *not created*, and the observability stack says which context value would
        have created it: a threshold invented by this deployment would read as though somebody had
        measured it (plan section 13.3, DEC-379).
        """
        values: dict[str, str] = {}
        for entry in self.alarm_thresholds:
            name, separator, value = entry.partition("=")
            if not separator or not name.strip() or not value.strip():
                raise ContextError(
                    f"alarm_thresholds: {entry!r} is not `Name=value`. Spell the whole list as "
                    "-c alarm_thresholds=StageDurationSecondsAlarmThresholdSeconds=900,Other=1"
                )
            values[name.strip()] = value.strip()
        return values

    @property
    def private_subnets_have_egress(self) -> bool:
        """Whether the application subnets can reach the internet, i.e. whether a NAT gateway exists."""
        return self.nat_gateways > 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AppContext:
        """Build the context from CDK's context bag, refusing anything unrecognised.

        `raw` is the whole bag, CDK's own keys included. Ours are the bare names below and the same
        names under `marketing-ai:`; a `@`-prefixed or colon-carrying key belongs to the toolkit and
        is skipped. Anything else is a misspelling of one of ours and is an error.
        """
        values = _select_our_keys(raw)
        env_name = _as_str("env_name", values.pop("env_name", DEV)).lower()
        if env_name not in ENV_NAMES:
            raise ContextError(f"env_name: must be one of {', '.join(ENV_NAMES)}, got {env_name!r}.")
        is_prod = env_name == PROD

        built: dict[str, Any] = {"env_name": env_name}
        for name, converter in _CONVERTERS.items():
            if name in values:
                built[name] = converter(name, values.pop(name))
        if values:  # unreachable: _select_our_keys only admits names in _CONVERTERS
            raise ContextError(f"unhandled context keys: {', '.join(sorted(values))}.")

        built.setdefault("db_multi_az", is_prod)
        if not built.get("kms_key_alias"):
            built["kms_key_alias"] = f"alias/marketing-ai-{env_name}"
        built["log_retention_days"] = 365 if is_prod else 30
        built["db_deletion_protection"] = is_prod
        built["removal_policy_destroy"] = not is_prod
        built["https_only"] = bool(built.get("certificate_arn"))

        context = cls(**built)
        context.validate()
        return context

    def validate(self) -> None:
        """Every combination that cannot work, refused at synth time with the fix in the message."""
        if self.is_prod and not self.certificate_arn:
            raise ContextError(
                "certificate_arn: a prod deployment must terminate TLS. An HTTP-only load balancer "
                "puts every upload, every download URL and every session on the public internet in "
                "clear text. Pass -c certificate_arn=<acm arn> (and -c domain_name=<name>), or "
                "deploy this as -c env_name=dev."
            )
        if self.is_prod and not self.domain_name:
            raise ContextError(
                "domain_name: a prod deployment must name the origin it answers. `Settings` refuses "
                "cors_origins='*' on a prod deployment (DEC-307), and '*' is the only other answer "
                "this stack could compose. Pass -c domain_name=<name>."
            )
        if self.domain_name and not self.certificate_arn:
            raise ContextError(
                "domain_name was given without certificate_arn: a name with no certificate cannot "
                "be served over HTTPS. Supply both, or neither."
            )
        if self.nat_gateways < 0:
            raise ContextError("nat_gateways: must be zero or more.")
        if not self.private_subnets_have_egress:
            missing = [name for name in REQUIRED_PRIVATE_ENDPOINTS if name not in self.vpc_endpoints]
            if missing:
                raise ContextError(
                    "nat_gateways=0 leaves the application subnets with no route to the internet, and "
                    f"these interface endpoints are missing: {', '.join(missing)}. Without them a "
                    "Fargate task cannot pull its image, cannot start its log driver and cannot read "
                    "its settings - it fails before it logs anything. Add them with "
                    f"-c vpc_endpoints={','.join(sorted({*self.vpc_endpoints, *REQUIRED_PRIVATE_ENDPOINTS}))} "
                    "or give it a NAT gateway with -c nat_gateways=1."
                )
        if self.api_cpu not in FARGATE_CPU_MEMORY:
            raise ContextError(
                f"api_cpu: {self.api_cpu} is not a Fargate CPU size; "
                f"choose one of {', '.join(str(cpu) for cpu in sorted(FARGATE_CPU_MEMORY))}."
            )
        low, high, step = FARGATE_CPU_MEMORY[self.api_cpu]
        if not (low <= self.api_memory <= high and (self.api_memory - low) % step == 0):
            raise ContextError(
                f"api_memory: {self.api_memory} MiB is not valid for api_cpu={self.api_cpu}; "
                f"Fargate allows {low}-{high} MiB in steps of {step}."
            )
        if self.api_min_tasks < 1:
            raise ContextError("api_min_tasks: must be at least 1; zero tasks is a deleted service.")
        if self.api_max_tasks < self.api_min_tasks:
            raise ContextError("api_max_tasks: must be at least api_min_tasks.")
        if self.db_storage_gb < 20:
            raise ContextError("db_storage_gb: RDS for PostgreSQL does not allocate less than 20 GiB.")
        if self.db_backup_retention_days < 1:
            raise ContextError(
                "db_backup_retention_days: must be at least 1. Zero disables automated backups, and "
                "a deployment holding a customer's data has no business doing that silently."
            )
        if self.max_concurrent_jobs < 1:
            raise ContextError("max_concurrent_jobs: must be at least 1.")
        self.alarm_threshold_values  # noqa: B018  - parses and raises on a malformed entry
        if self.monthly_budget_usd is not None:
            if self.monthly_budget_usd <= 0:
                raise ContextError("monthly_budget_usd: must be greater than zero.")
            if not self.alert_email:
                raise ContextError(
                    "monthly_budget_usd was given without alert_email. A budget nobody is told about "
                    "is a number in a console, not a control."
                )
        if self.image_digest is not None and "@sha256:" not in self.image_digest:
            raise ContextError(
                "image_digest: expected the full image reference `<registry>/<repository>@sha256:...` "
                "that scripts/build_push_image.sh writes to .image-digest. A tag can be moved after "
                "it was tested; a digest cannot."
            )

    def tags(self) -> dict[str, str]:
        """The cost-allocation tags every resource in every stack carries.

        `use_case` is deliberately absent. One deployment serves every use case, so at this level it
        is not known; it is carried on the things that *do* belong to one - `JobSpec.tags` puts it on
        a SageMaker job, and the object prefixes carry it in S3 (DEC-375).
        """
        tags = {"product": "marketing-ai", "env": self.env_name}
        if self.client_id:
            tags["client"] = self.client_id
        return tags


def context_from_environment(environ: Mapping[str, str] | None = None) -> AppContext:
    """The context the CDK CLI handed this process, or the defaults when it is run directly."""
    env = os.environ if environ is None else environ
    raw_json = env.get(CDK_CONTEXT_ENV_VAR, "").strip()
    if not raw_json:
        return AppContext.from_mapping({})
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ContextError(f"{CDK_CONTEXT_ENV_VAR} is not valid JSON: {exc.msg}.") from None
    if not isinstance(parsed, dict):
        raise ContextError(f"{CDK_CONTEXT_ENV_VAR} must be a JSON object.")
    return AppContext.from_mapping(parsed)


def _select_our_keys(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Our keys, in either spelling, with a typo raising rather than being ignored."""
    namespaced: dict[str, Any] = {}
    bare: dict[str, Any] = {}
    for key, value in raw.items():
        if key.startswith(NAMESPACE):
            name = key[len(NAMESPACE) :]
            if name not in KNOWN_KEYS:
                raise ContextError(_unknown_key_message(key, name))
            namespaced[name] = value
        elif key in KNOWN_KEYS:
            bare[key] = value
        elif key.startswith("@") or ":" in key:
            continue  # a CDK feature flag, a toolkit key or a provider lookup; not ours
        else:
            raise ContextError(_unknown_key_message(key, key))
    for name, value in namespaced.items():
        if name in bare and str(bare[name]) != str(value):
            raise ContextError(
                f"{name} was given twice with different values: as {name}={bare[name]!r} and as "
                f"{NAMESPACE}{name}={value!r}. Remove one."
            )
    bare.update(namespaced)
    return bare


def _unknown_key_message(key: str, name: str) -> str:
    near = [known for known in sorted(KNOWN_KEYS) if _close(name, known)]
    hint = f" Did you mean {' or '.join(near)}?" if near else ""
    return (
        f"unknown context key {key!r}. Every key this application reads is listed in "
        f"infra/context.py, and an unrecognised one is refused rather than ignored, because a typo "
        f"in -c is otherwise invisible until someone wonders why a setting had no effect.{hint}"
    )


def _close(name: str, known: str) -> bool:
    """A cheap "did you mean": same letters, or one is a prefix of the other."""
    return sorted(name) == sorted(known) or name.startswith(known[:6]) or known.startswith(name[:6])


_CONVERTERS: Final[Mapping[str, Any]] = {
    "region": _as_str,
    "client_id": _as_str,
    "bucket_name_prefix": _as_str,
    "kms_key_alias": _as_str,
    "db_instance_class": _as_str,
    "db_storage_gb": _as_int,
    "db_multi_az": _as_bool,
    "db_backup_retention_days": _as_int,
    "api_cpu": _as_int,
    "api_memory": _as_int,
    "api_min_tasks": _as_int,
    "api_max_tasks": _as_int,
    "container_insights": _as_bool,
    "sagemaker_instance_train": _as_str,
    "sagemaker_instance_process": _as_str,
    "max_concurrent_jobs": _as_int,
    "bedrock_enabled": _as_bool,
    "bedrock_model_ids": _as_tuple,
    "nat_gateways": _as_int,
    "vpc_endpoints": _as_tuple,
    "alert_email": _as_str,
    "monthly_budget_usd": _as_float,
    "alarm_thresholds": _as_tuple,
    "domain_name": _as_str,
    "certificate_arn": _as_str,
    "image_digest": _as_str,
    "cdk_nag": _as_bool,
}
"""Every context key except `env_name`, and how to read its value. `env_name` is read first because
the defaults of the others depend on it."""

_DERIVED_FIELDS: Final[frozenset[str]] = frozenset(
    {"log_retention_days", "https_only", "db_deletion_protection", "removal_policy_destroy"}
)
"""Fields of `AppContext` that `env_name` decides and no context key can set."""

KNOWN_KEYS: Final[frozenset[str]] = frozenset({"env_name", *_CONVERTERS})
"""Every context key this application accepts, in either spelling."""

assert {
    f.name for f in fields(AppContext)
} == KNOWN_KEYS | _DERIVED_FIELDS, "every AppContext field is either a context key or derived from env_name"
