"""Every IAM statement this deployment grants, written out rather than generated.

CDK's `grant*` helpers are convenient and slightly too generous: `bucket.grantReadWrite(role)` hands
the role the whole bucket, `secret.grantRead(role)` is fine but `table.grantFullAccess(role)` is not,
and none of them leaves a reviewer anything to read except a synthesised blob. Phase 4a's rule is the
opposite one - the policy is the document, so it is written as a document (DEC-369).

Two properties are enforced by tests rather than by care:

**No `*` resource that is not on the list.** `RESOURCE_WILDCARD_ALLOW_LIST` names every action for
which AWS offers no resource-level alternative, with the reason beside it.
`tests/infra/test_iam.py` walks every identity policy in every synthesised stack and fails on an
`Allow` with `"Resource": "*"` whose actions are not all on that list. An ARN that contains a
wildcard *path* - `.../uploads/*`, `.../training-job/marketing-ai-*` - is a different thing and is
the point: it names a prefix.

**`s3:ListBucket` on the bucket itself.** Without it, S3 answers `403 AccessDenied` for a key that
is simply not there, because it will not confirm the absence of an object to a principal that may
not list. `S3Storage.exists()` then cannot tell "no such run" from "your role is wrong", and every
absent-artefact path in the product turns into a permissions bug report. The statement carries no
`s3:prefix` condition, deliberately: during a `GetObject` there is no `s3:prefix` in the request, so
a condition on it would fail closed and bring the 403 back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from aws_cdk import aws_iam as iam

from infra.naming import (
    JOB_NAME_PREFIX,
    METRIC_NAMESPACE,
    OBJECT_PREFIXES,
    ROW_LEVEL_PREFIXES,
    SSM_ROOT,
)

__all__ = [
    "RESOURCE_WILDCARD_ALLOW_LIST",
    "alert_publish_statement",
    "audit_export_write_statement",
    "bedrock_statements",
    "ec2_network_interface_statement",
    "ecr_pull_statements",
    "kms_use_statement",
    "log_write_statement",
    "metrics_statement",
    "pass_role_statement",
    "retention_statements",
    "run_job_task_statements",
    "s3_artefact_statements",
    "sagemaker_job_statements",
    "schedule_management_statement",
    "settings_read_statements",
]

RESOURCE_WILDCARD_ALLOW_LIST: Final[frozenset[str]] = frozenset(
    {
        # `ecr:GetAuthorizationToken` returns a token for the caller's registry, not for a named
        # repository; the IAM reference lists it with no resource types, so `*` is the only value
        # the statement can carry. Every other ECR action here is scoped to one repository ARN.
        "ecr:GetAuthorizationToken",
        # `cloudwatch:PutMetricData` has no resource types either. It is narrowed the only way AWS
        # offers, a `cloudwatch:namespace` condition, so the task can write MarketingAI metrics and
        # nothing else - it cannot, for instance, forge AWS/RDS datapoints.
        "cloudwatch:PutMetricData",
        # A SageMaker job with a VpcConfig makes the *service* create elastic network interfaces in
        # our subnets. They do not exist when this policy is written and their ids are not
        # predictable, which is why AWS's own documented SageMaker execution policy uses `*` for
        # exactly this set. Describe* here is the read half of the same mechanism.
        "ec2:CreateNetworkInterface",
        "ec2:CreateNetworkInterfacePermission",
        "ec2:DeleteNetworkInterface",
        "ec2:DeleteNetworkInterfacePermission",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeDhcpOptions",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
    }
)
"""Actions that may appear in an `Allow` whose `Resource` is `"*"`, and nothing else may.

Every entry is an action AWS documents with no resource types - not one that was inconvenient to
scope. Adding to this set is a deliberate act with a reason next to it; `tests/infra/test_iam.py`
compares it against every stack.
"""

_S3_OBJECT_ACTIONS: Final[tuple[str, ...]] = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:GetObjectTagging",
    "s3:DeleteObject",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
)
"""What the product does to an object.

`PutObjectTagging` because `scripts/aws_bootstrap.py` probes it; `AbortMultipartUpload` and
`ListMultipartUploadParts` because a predictor directory is large enough for boto3 to switch to a
multipart upload on its own, and a role that can start one but not finish or abandon one leaves
paid-for garbage in the bucket that the lifecycle rule then has to clean up.
"""

_S3_BUCKET_ACTIONS: Final[tuple[str, ...]] = ("s3:ListBucket", "s3:GetBucketLocation")
"""What the product does to the bucket. See the module docstring for why `ListBucket` is here."""


def s3_artefact_statements(
    bucket_arn: str, *, prefixes: Sequence[str] = OBJECT_PREFIXES
) -> list[iam.PolicyStatement]:
    """Object access under the product's four prefixes, plus listing the bucket.

    `prefixes` is `uploads/`, `runs/`, `models/` and `_bootstrap/` - the whole of what
    `engine/storage.py` and `scripts/aws_bootstrap.py` write. Naming them instead of the bucket's
    whole contents means a prefix a later phase invents is denied until someone adds it here, which
    is the direction this mistake should fall.
    """
    return [
        iam.PolicyStatement(
            sid="ArtefactObjects",
            effect=iam.Effect.ALLOW,
            actions=list(_S3_OBJECT_ACTIONS),
            resources=[f"{bucket_arn}/{prefix}*" for prefix in prefixes],
        ),
        iam.PolicyStatement(
            sid="ArtefactBucket",
            effect=iam.Effect.ALLOW,
            actions=list(_S3_BUCKET_ACTIONS),
            resources=[bucket_arn],
        ),
    ]


def kms_use_statement(key_arn: str, *, sid: str = "ArtefactKey") -> iam.PolicyStatement:
    """Use the customer-managed key for SSE-KMS, and for nothing administrative.

    `GenerateDataKey` is the write half of SSE-KMS and `Decrypt` the read half; `DescribeKey` is
    what boto3 calls to find out the key's spec before an upload. Nothing here can schedule the key
    for deletion, change its policy or create a grant.
    """
    return iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.ALLOW,
        actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        resources=[key_arn],
    )


def settings_read_statements(
    *, account: str, region: str, env_name: str, secret_arn: str
) -> list[iam.PolicyStatement]:
    """Read `/marketing-ai/<env>/*` from Parameter Store and the one application secret.

    Two stores because they answer two questions, and the split is what makes this policy
    checkable at a glance: the parameters are the deployment description an operator is happy to
    read in the console, the secret is the one credential (`engine/aws/secrets.py`).
    `GetParametersByPath` is authorised against the path *without* its trailing slash, which is why
    the two resources below are not the same string.
    """
    path = f"arn:aws:ssm:{region}:{account}:parameter{SSM_ROOT}/{env_name}"
    return [
        iam.PolicyStatement(
            sid="DeploymentParameters",
            effect=iam.Effect.ALLOW,
            actions=["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"],
            resources=[path, f"{path}/*"],
        ),
        iam.PolicyStatement(
            sid="DeploymentSecret",
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[secret_arn],
        ),
    ]


def sagemaker_job_statements(
    *, account: str, region: str, job_name_prefix: str = JOB_NAME_PREFIX
) -> list[iam.PolicyStatement]:
    """Create, describe, stop and tag jobs whose name starts with the product's prefix.

    There is no `List*` action here and there is not meant to be. `SageMakerJobRunner` only ever
    describes a job it already knows the name of, so listing would be a capability granted for
    nothing - and `sagemaker:ListTrainingJobs` cannot be scoped to a name prefix, so granting it
    would mean showing this role every job in the account, including other tenants' in a shared one.
    """
    resources = [
        f"arn:aws:sagemaker:{region}:{account}:training-job/{job_name_prefix}-*",
        f"arn:aws:sagemaker:{region}:{account}:processing-job/{job_name_prefix}-*",
    ]
    return [
        iam.PolicyStatement(
            sid="ProductJobs",
            effect=iam.Effect.ALLOW,
            actions=[
                "sagemaker:CreateTrainingJob",
                "sagemaker:CreateProcessingJob",
                "sagemaker:DescribeTrainingJob",
                "sagemaker:DescribeProcessingJob",
                "sagemaker:StopTrainingJob",
                "sagemaker:StopProcessingJob",
                "sagemaker:AddTags",
            ],
            resources=resources,
        )
    ]


def pass_role_statement(
    role_arn: str, *, service: str = "sagemaker.amazonaws.com", sid: str = "PassJobRole"
) -> iam.PolicyStatement:
    """Hand exactly one role to exactly one service.

    `iam:PassRole` without the `iam:PassedToService` condition is the classic privilege-escalation
    hole: it lets the holder attach that role to anything that will assume it.
    """
    return iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.ALLOW,
        actions=["iam:PassRole"],
        resources=[role_arn],
        conditions={"StringEquals": {"iam:PassedToService": service}},
    )


def log_write_statement(log_group_arns: Sequence[str], *, sid: str = "WriteLogs") -> iam.PolicyStatement:
    """Create streams in, and write to, named log groups only.

    `CreateLogGroup` is absent on purpose: the groups are created by the observability stack with a
    retention, and a principal that can create its own would create one that never expires.
    """
    return iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.ALLOW,
        actions=["logs:CreateLogStream", "logs:PutLogEvents"],
        resources=[arn for group in log_group_arns for arn in (group, f"{group}:*")],
    )


def metrics_statement(*, namespace: str = METRIC_NAMESPACE) -> iam.PolicyStatement:
    """`cloudwatch:PutMetricData`, narrowed by the only condition key AWS offers for it."""
    return iam.PolicyStatement(
        sid="ProductMetrics",
        effect=iam.Effect.ALLOW,
        actions=["cloudwatch:PutMetricData"],
        resources=["*"],
        conditions={"StringEquals": {"cloudwatch:namespace": namespace}},
    )


def ecr_pull_statements(repository_arn: str) -> list[iam.PolicyStatement]:
    """Pull one repository's images. The auth token is the one action that cannot be scoped."""
    return [
        iam.PolicyStatement(
            sid="EcrAuth",
            effect=iam.Effect.ALLOW,
            actions=["ecr:GetAuthorizationToken"],
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="EcrPull",
            effect=iam.Effect.ALLOW,
            actions=[
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
            ],
            resources=[repository_arn],
        ),
    ]


def ec2_network_interface_statement() -> iam.PolicyStatement:
    """What a SageMaker job with a `VpcConfig` needs so the service can attach it to our subnets."""
    return iam.PolicyStatement(
        sid="JobNetworkInterfaces",
        effect=iam.Effect.ALLOW,
        actions=[
            "ec2:CreateNetworkInterface",
            "ec2:CreateNetworkInterfacePermission",
            "ec2:DeleteNetworkInterface",
            "ec2:DeleteNetworkInterfacePermission",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DescribeDhcpOptions",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSubnets",
            "ec2:DescribeVpcs",
        ],
        resources=["*"],
    )


def bedrock_statements(*, enabled: bool, model_ids: Iterable[str], region: str) -> list[iam.PolicyStatement]:
    """Allow the named models, or deny Bedrock outright. Never an absent `Allow`.

    An empty `bedrock_model_ids` is a statement, not a gap. Leaving the permission out would be
    indistinguishable from forgetting it, and the difference matters the day somebody attaches a
    broad managed policy to the task role for an unrelated reason: an explicit `Deny` survives that,
    an absent `Allow` does not. The model list is a context key rather than a `Settings` field,
    because which models a role may invoke is an IAM question; `Settings.bedrock_model_id` is the
    one model completions go to, and `llm_backend` is what switches the engine on. The rule here is
    "empty means deny, never allow-all" (DEC-371).
    """
    wanted = [model_id.strip() for model_id in model_ids if model_id.strip()]
    if not enabled or not wanted:
        return [
            iam.PolicyStatement(
                sid="NoGenerativeModels",
                effect=iam.Effect.DENY,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
            )
        ]
    return [
        iam.PolicyStatement(
            sid="NamedGenerativeModels",
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[_bedrock_arn(model_id, region) for model_id in wanted],
        )
    ]


def _bedrock_arn(model_id: str, region: str) -> str:
    """A model id as an ARN; an id that is already one is left alone (inference profiles are ARNs)."""
    if model_id.startswith("arn:"):
        return model_id
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def statement_actions(statement: Mapping[str, object]) -> list[str]:
    """The `Action` of a rendered statement, as a list however it was spelt.

    Used by `tests/infra/test_iam.py`; here rather than in the test because the shape it normalises
    is this module's output.
    """
    action = statement.get("Action", [])
    if isinstance(action, str):
        return [action]
    if isinstance(action, list):
        return [item for item in action if isinstance(item, str)]
    return []


# --- Phase 4b ------------------------------------------------------------------------------------
# What the running API needs for sign-in, the audit trail, DPDP retention and erasure, scheduling
# and alerts (plan M46-M49), written in the same shape as everything above: one function per
# capability, every resource named, and a docstring that says why each action is there and why the
# obvious neighbouring action is not.


def audit_export_write_statement(bucket_arn: str, prefix: str) -> iam.PolicyStatement:
    """Write audit exports under one prefix of the Object Lock bucket, and do nothing else there.

    `PutObjectRetention` is not optional: `S3AuditExportSink` sends `ObjectLockMode=COMPLIANCE` and
    a retain-until date *on the PutObject itself*, and S3 authorises those two headers against
    `s3:PutObjectRetention` - without it every export is `AccessDenied`.

    Deliberately absent: every `Delete*` (an audit trail its writer can delete is not one), every
    `Get*` and `ListBucket` (the export is evidence for somebody else to read; the API reads the
    audit table, never the export), and `BypassGovernanceRetention` (compliance mode has no bypass,
    and granting it would only matter if somebody quietly switched the bucket to governance mode).
    """
    return iam.PolicyStatement(
        sid="WriteAuditExports",
        effect=iam.Effect.ALLOW,
        actions=["s3:PutObject", "s3:PutObjectRetention"],
        resources=[f"{bucket_arn}/{prefix.strip('/')}/*"],
    )


def alert_publish_statement(topic_arn: str) -> iam.PolicyStatement:
    """Publish to the application's alert topic, and to no other topic.

    Not the CloudWatch alarm topic. That topic's messages are CloudWatch's, and a role that could
    publish there could forge an "OK" for an alarm that is firing. Two topics, one email address:
    the recipient sees both, and only CloudWatch can speak on the alarm channel.
    """
    return iam.PolicyStatement(
        sid="PublishAlerts",
        effect=iam.Effect.ALLOW,
        actions=["sns:Publish"],
        resources=[topic_arn],
    )


def schedule_management_statement(*, account: str, region: str, group_name: str) -> iam.PolicyStatement:
    """Create, change, read and delete schedules in this deployment's group only.

    A schedule ARN is `schedule/<group>/<name>`, so the group is the boundary and the `*` is the
    schedule name, which the application generates. `ListSchedules` is absent: the application
    keeps its own schedule table and only ever addresses a schedule it created, and a list call
    would show this role every other group's schedules in the account.
    """
    return iam.PolicyStatement(
        sid="ManageSchedules",
        effect=iam.Effect.ALLOW,
        actions=[
            "scheduler:CreateSchedule",
            "scheduler:UpdateSchedule",
            "scheduler:DeleteSchedule",
            "scheduler:GetSchedule",
        ],
        resources=[f"arn:aws:scheduler:{region}:{account}:schedule/{group_name}/*"],
    )


def retention_statements(
    bucket_arn: str, *, prefixes: Sequence[str] = ROW_LEVEL_PREFIXES
) -> list[iam.PolicyStatement]:
    """What the DPDP retention job and erasure requests need beyond the ordinary artefact grant.

    Three things, each for a reason `s3_artefact_statements` does not cover:

    * **Lifecycle configuration on the artefact bucket.** Plan M48 puts S3 lifecycle rules per
      client prefix behind the retention job as a backstop, and `PutLifecycleConfiguration`
      replaces the bucket's *whole* configuration - so the job reads it first
      (`GetLifecycleConfiguration`) and keeps every rule it does not own, including the
      `abort-incomplete-multipart-uploads` rule `infra/storage.py` created.
    * **Deleting object versions under the row-level prefixes.** The bucket is versioned, so a
      plain delete leaves the customer's bytes behind as a noncurrent version. Erasure has to be
      able to remove the version itself - on `uploads/` and `runs/` only; see
      `infra/naming.ROW_LEVEL_PREFIXES`.
    * **Listing versions** so the job can find them. Bucket-level, like `ListBucket`, and for the
      same reason it carries no prefix condition.
    """
    return [
        iam.PolicyStatement(
            sid="RetentionLifecycle",
            effect=iam.Effect.ALLOW,
            actions=[
                "s3:GetLifecycleConfiguration",
                "s3:PutLifecycleConfiguration",
                "s3:ListBucketVersions",
            ],
            resources=[bucket_arn],
        ),
        iam.PolicyStatement(
            sid="EraseObjectVersions",
            effect=iam.Effect.ALLOW,
            actions=["s3:DeleteObjectVersion"],
            resources=[f"{bucket_arn}/{prefix}*" for prefix in prefixes],
        ),
    ]


def run_job_task_statements(
    *,
    account: str,
    region: str,
    cluster_arn: str,
    cluster: str,
    family: str,
    task_role_arns: Sequence[str],
) -> list[iam.PolicyStatement]:
    """What EventBridge Scheduler's role needs to start the scheduled-job task, and nothing more.

    * `ecs:RunTask` on one task-definition *family*, and only in this deployment's cluster - the
      `ecs:cluster` condition is what stops the role running the job task somewhere else. Two
      spellings of the one family: `:*` is any revision (CloudFormation picks a new one on every
      deployment), and the bare family ARN is what a schedule that names no revision - the
      recommended shape, see `infra/naming.job_task_family` - is authorised against. Which of the
      two IAM evaluates for a revision-less `RunTask` is not documented clearly enough to bet a
      deployment's schedules on, and both name the same family.
    * `ecs:TagResource` on tasks in that cluster, and only as part of `RunTask`: a schedule that
      propagates the cost-allocation tags needs it, and the `ecs:CreateAction` condition means it
      cannot retag anything that already exists.
    * `iam:PassRole` on the task's two roles, to ECS tasks only. `RunTask` passes both the task
      role and the execution role, so both must be passable; the condition stops either being
      handed to any other service.
    """
    return [
        iam.PolicyStatement(
            sid="RunJobTask",
            effect=iam.Effect.ALLOW,
            actions=["ecs:RunTask"],
            resources=[
                f"arn:aws:ecs:{region}:{account}:task-definition/{family}",
                f"arn:aws:ecs:{region}:{account}:task-definition/{family}:*",
            ],
            conditions={"ArnEquals": {"ecs:cluster": cluster_arn}},
        ),
        iam.PolicyStatement(
            sid="TagJobTask",
            effect=iam.Effect.ALLOW,
            actions=["ecs:TagResource"],
            resources=[f"arn:aws:ecs:{region}:{account}:task/{cluster}/*"],
            conditions={"StringEquals": {"ecs:CreateAction": "RunTask"}},
        ),
        iam.PolicyStatement(
            sid="PassJobTaskRoles",
            effect=iam.Effect.ALLOW,
            actions=["iam:PassRole"],
            resources=list(task_role_arns),
            conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
        ),
    ]
