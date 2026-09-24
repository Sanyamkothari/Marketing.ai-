"""Every cdk-nag finding this deployment does not fix, and the sentence that justifies it.

A suppression is a decision to accept a risk, so each one here carries the reason in the form a
reviewer needs: what the rule wants, why this deployment does something else, and what would have
to change for the suppression to go away. A suppression with the reason "not applicable" is worth
less than no suppression at all, because it looks like somebody thought about it.

Everything cdk-nag finds that *should* be fixed is fixed in the stack that raises it - the buckets
block public access and enforce TLS, the database is encrypted, private, backed up and moved off
PostgreSQL's default port, the roles are written as statement lists, the load balancer drops
invalid headers - and what is left is this list.

Two shapes of finding dominate it, and they are worth separating before reading further.

**`AwsSolutions-IAM5` on an ARN that contains a wildcard *path*.** `arn:...:s3:::bucket/uploads/*`
and `arn:...:training-job/marketing-ai-*` are not "a wildcard permission" in the sense the rule is
looking for. They are the scoping: the whole point of `infra/policies.py` is that the role reaches
four named prefixes rather than the bucket, and jobs whose name starts with the product's prefix
rather than every job in the account. cdk-nag cannot tell the difference between a wildcard that
widens and a wildcard that *is* the boundary, so each one is enumerated below with the reason it is
a boundary.

**`AwsSolutions-IAM5` with `Resource::*` outright.** This one is real, and it is why
`infra/policies.py` keeps `RESOURCE_WILDCARD_ALLOW_LIST` - the actions AWS documents with no
resource types - and why `tests/infra/test_iam.py` walks every `Allow` in every synthesised stack
and fails on a `*` whose actions are not all on that list. That test is strictly stronger than the
nag rule: it is specific to this product's allow-list, it runs offline on every `make infra-test`,
and it fails on a wildcard that arrives inside a construct nobody read. The suppression below
accepts the finding; the test is what actually holds the line.

`cdk-nag 3.0.2 is incompatible with aws-cdk-lib 2.270.0`: `npx cdk synth` dies inside the aspect
visitor with `TypeError: aspectApplication.aspect.visit is not a function`. Measured in this
session; `pyproject.toml` pins 2.38.2, which works (DEC-366).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, NamedTuple

from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo

from infra.naming import (
    AUDIT_EXPORT_PREFIX,
    JOB_CONTAINER_NAME,
    JOB_NAME_PREFIX,
    OBJECT_PREFIXES,
    PRODUCT,
    ROW_LEVEL_PREFIXES,
)

if TYPE_CHECKING:
    from infra.app import Deployment

__all__ = ["SUPPRESSIONS", "Suppression", "apply_suppressions", "suppressions_for"]


class Suppression(NamedTuple):
    """One accepted finding: which stack, which construct, which rule, and why."""

    stack: str
    """The component name of the stack, as `infra.app.STACK_ORDER` spells it."""

    path: str
    """The construct path *inside* that stack, or `""` for a stack-wide suppression."""

    rule: str
    """The cdk-nag rule id, e.g. `AwsSolutions-S1`."""

    reason: str
    """Why this deployment does not do what the rule asks. A sentence, not a label."""

    applies_to: tuple[str, ...] = ()
    """Exact findings inside the rule this suppression covers, when the rule is a granular one."""

    applies_to_regex: tuple[str, ...] = ()
    """The same, as ECMA-262 `/pattern/flags` strings.

    Used where the finding embeds something this module should not hard-code: a CDK logical-id hash
    (`<ArtefactsC012E42A.Arn>`), the region, the account, or `env_name`. Writing those out would
    make the suppression silently stop matching the day a construct id changed - and a suppression
    that stops matching is a nag run that fails loudly, which is survivable, whereas a suppression
    that matches *more* than it was written for is a permission nobody reviews again. Every pattern
    below is anchored with `^` and `$` for that reason.
    """


_OBJECT_PREFIX_ALTERNATION: Final[str] = "|".join(prefix.rstrip("/") for prefix in OBJECT_PREFIXES)
"""`uploads|runs|models|_bootstrap`, built from the one list `engine/storage.py` agrees with."""


def _cross_stack_ref(stem: str) -> str:
    """A reference to another stack's attribute, in both the spellings cdk-nag reports it in.

    The same `Fn::GetAtt` reaches this module's suppressions under two names, and which one depends
    on something no suppression should have to care about. Synthesised environment-agnostically -
    a bare `cdk synth`, which is what `make infra-synth` and `make infra-nag` run - the finding
    reads `<ArtefactsC012E42A.Arn>`. Synthesised into a concrete account, which is what
    `tests/infra/` does so that its ARN assertions are readable, CDK resolves the reference through
    a real export and the finding reads
    `marketing-ai-dev-storage:ExportsOutputFnGetAttArtefactsC012E42AArnBAB43CB7`.

    Matching only the first spelling is the failure this helper exists to prevent: `make infra-nag`
    would pass and `make infra-test` would not, for a deployment that is the same either way.
    """
    return rf"(?:<{stem}[0-9A-F]*\.Arn>" rf"|[a-z0-9-]+:ExportsOutputFnGetAtt{stem}[0-9A-F]*Arn[0-9A-F]*)"


_S3_PREFIX_FINDINGS: Final[str] = (
    rf"/^Resource::{_cross_stack_ref('Artefacts')}\/({_OBJECT_PREFIX_ALTERNATION})\/\*$/"
)
"""An artefact prefix. The `*` is every object *under* one of the product's four prefixes."""

_SSM_PATH_FINDINGS: Final[str] = (
    r"/^Resource::arn:aws:ssm:[a-z0-9-]+:.*:parameter\/marketing-ai\/[a-z]+\/\*$/"
)
"""Every parameter under this deployment's own path, and no other deployment's."""

_LOG_STREAM_FINDINGS: Final[str] = rf"/^Resource::{_cross_stack_ref('[A-Za-z]+Logs')}:\*$/"
"""Every log *stream* inside one named log group."""

_SAGEMAKER_LOG_FINDINGS: Final[str] = (
    r"/^Resource::arn:aws:logs:[a-z0-9-]+:.*:log-group:\/aws\/sagemaker\/\*(:log-stream:\*)?$/"
)
"""The log groups SageMaker itself creates for a job."""

_JOB_NAME_FINDINGS: Final[str] = (
    rf"/^Resource::arn:aws:sagemaker:[a-z0-9-]+:.*:(training|processing)-job\/{JOB_NAME_PREFIX}-\*$/"
)
"""Jobs whose name starts with the product's prefix."""

_ROW_LEVEL_PREFIX_ALTERNATION: Final[str] = "|".join(prefix.rstrip("/") for prefix in ROW_LEVEL_PREFIXES)
"""`uploads|runs`: the two prefixes an object *version* may be deleted in (Phase 4b)."""

_ROW_LEVEL_VERSION_FINDINGS: Final[str] = (
    rf"/^Resource::{_cross_stack_ref('Artefacts')}\/({_ROW_LEVEL_PREFIX_ALTERNATION})\/\*$/"
)
"""A row-level artefact prefix, for `s3:DeleteObjectVersion` only."""

_AUDIT_PREFIX_FINDINGS: Final[str] = rf"/^Resource::<AuditExports[0-9A-F]*\.Arn>\/{AUDIT_EXPORT_PREFIX}\/\*$/"
"""Every object under the audit prefix of the audit bucket, which lives in the same stack."""

_SCHEDULE_FINDINGS: Final[str] = (
    rf"/^Resource::arn:aws:scheduler:[a-z0-9-]+:.*:schedule\/{PRODUCT}-[a-z]+\/\*$/"
)
"""Every schedule in this deployment's schedule group, and no other group's."""

_JOB_TASK_FINDINGS: Final[str] = (
    rf"/^Resource::arn:aws:ecs:[a-z0-9-]+:.*:task-definition\/{PRODUCT}-[a-z]+-job:\*$/"
)
"""Every *revision* of the scheduled-job task-definition family."""

_JOB_TASK_TAG_FINDINGS: Final[str] = rf"/^Resource::arn:aws:ecs:[a-z0-9-]+:.*:task\/{PRODUCT}-[a-z]+\/\*$/"
"""Every task in this deployment's cluster - for `ecs:TagResource` during `RunTask` only."""

_WILDCARD_FINDING: Final[str] = "Resource::*"
"""The literal finding for an `Allow` on every resource. Exact, never a pattern."""

_PREFIX_IS_THE_BOUNDARY: Final[str] = (
    "The `*` in this ARN is a key prefix, not a widened permission: it is the boundary itself. "
    f"infra/policies.py grants objects under {', '.join(OBJECT_PREFIXES)} rather than the bucket, "
    "which is the whole of what engine/storage.py and scripts/aws_bootstrap.py write, so a prefix a "
    "later phase invents is denied until somebody adds it to OBJECT_PREFIXES on purpose. Naming "
    "individual object ARNs is not possible - the objects are created by the running product and "
    "their keys carry run ids that do not exist when this policy is written. "
    "tests/infra/test_iam.py asserts these four prefixes appear and that no other S3 grant does."
)

_SSM_IS_THE_BOUNDARY: Final[str] = (
    "The `*` is the leaf of this deployment's own parameter path, /marketing-ai/<env>/*, and the "
    "leaves are Settings field names (engine/settings.py). It is a boundary rather than a "
    "widening: a dev task cannot read prod's parameters, and neither can read anything else in a "
    "shared Parameter Store namespace. The individual leaves cannot be named here because the set "
    "of them is engine/settings.py's field list, which this stack does not import - and because "
    "GetParametersByPath is authorised against the path, not against each leaf it returns."
)

_LOG_STREAM_IS_THE_BOUNDARY: Final[str] = (
    "The `*` is the log *stream* inside one named log group, and a stream name is chosen by the "
    "ECS agent or by SageMaker at task start - it cannot be known when the policy is written. The "
    "log group itself is named exactly, which is the part that matters: this role cannot write "
    "into any other group, and logs:CreateLogGroup is deliberately absent from "
    "infra/policies.py::log_write_statement so it cannot make itself one that never expires."
)

_SAGEMAKER_LOG_IS_THE_BOUNDARY: Final[str] = (
    "/aws/sagemaker/* is the group name SageMaker composes for a job's own logs; the service picks "
    "the suffix and the execution role must be able to write to it before there is any job to read "
    "the name from. It is scoped to the SageMaker log-group namespace in this account and region "
    "and to CreateLogStream/PutLogEvents, so the worst it permits is writing a log line into a "
    "group SageMaker would have created anyway."
)

_JOB_NAME_IS_THE_BOUNDARY: Final[str] = (
    f"The `*` completes the job-name prefix `{JOB_NAME_PREFIX}-`, which is the IAM resource "
    "boundary this product is built around: engine/aws/sagemaker_jobs.py names every job it "
    "creates with that prefix, Settings.sagemaker_job_name_prefix is the single source of it, and "
    "there is no List* action in the grant, so this role cannot even see a job outside the prefix, "
    "let alone stop one. Naming jobs individually is impossible - a job name contains a run id "
    "generated at request time."
)

_WILDCARD_IS_UNAVOIDABLE: Final[str] = (
    "Every action in these statements is one AWS documents with no resource types, so `*` is the "
    "only value the Resource element can take: ecr:GetAuthorizationToken (a token for the caller's "
    "registry, not for a repository), cloudwatch:PutMetricData (narrowed the only way AWS offers, "
    "a cloudwatch:namespace condition), and the ec2 network-interface actions a SageMaker job with "
    "a VpcConfig requires because the service creates the ENIs and their ids do not exist when the "
    "policy is written. This is not taken on trust: infra/policies.py freezes the set as "
    "RESOURCE_WILDCARD_ALLOW_LIST with the reason beside each entry, and "
    "tests/infra/test_iam.py walks every Allow in all eight synthesised stacks - dev and prod - "
    'and fails on any `Resource: "*"` whose actions are not all on that list, including one that '
    "arrives inside a construct nobody read. That test is narrower than this rule and it is what "
    "actually holds the line; a second test fails if the allow-list grows an entry nothing uses."
)


_OPERATIONS_TASK_POLICY_REASON: Final[str] = (
    "Each `*` in this policy is the last segment of a name that is itself the boundary. "
    f"`<audit bucket>/{AUDIT_EXPORT_PREFIX}/*` is every export under the one prefix "
    "engine/audit/export.py writes, with PutObject and PutObjectRetention only - no read, no "
    "delete, and the bucket's own policy denies deletion to every principal; an export's key is "
    "the timestamp of the export, so it cannot be named in advance. "
    f"`schedule/{PRODUCT}-<env>/*` is every schedule in this deployment's own EventBridge Scheduler "
    "group - a schedule ARN is group/name, the name is generated per schedule by the application, "
    "and ListSchedules is deliberately not granted, so this role cannot even see another group. "
    "`<artefact bucket>/uploads/*` and `/runs/*` carry s3:DeleteObjectVersion alone: the DPDP "
    "erasure and retention paths (plan M48) must remove the noncurrent version of a customer's "
    "rows, because on a versioned bucket a plain delete leaves the bytes behind, and models/ and "
    "_bootstrap/ are excluded because neither holds a customer row (infra/naming.ROW_LEVEL_PREFIXES)."
)

SUPPRESSIONS: Final[tuple[Suppression, ...]] = (
    Suppression(
        stack="storage",
        path="AccessLogs/Resource",
        rule="AwsSolutions-S1",
        reason=(
            "This *is* the server access log bucket. S3 will not deliver a bucket's access logs "
            "into that same bucket, and pointing it at a third bucket only moves the question one "
            "hop. Its contents are log records with no customer data in them, it blocks public "
            "access, enforces TLS 1.2 and expires objects on the deployment's log retention."
        ),
    ),
    Suppression(
        stack="compute",
        path="TaskDefinition/Resource",
        rule="AwsSolutions-ECS2",
        reason=(
            "The rule asks for no plain environment variables. The three this task definition "
            "carries are MARKETING_AI_SETTINGS_SOURCE, MARKETING_AI_ENV and MARKETING_AI_AWS_REGION: "
            "which deployment this is and where to read it from. They are not credentials and they "
            "are already public in the stack name. Every value that is a secret is fetched by the "
            "task itself from one Secrets Manager ARN, and no secret is rendered into this task "
            "definition in any form - see infra/compute.py."
        ),
    ),
    Suppression(
        stack="network",
        path="AlbSecurityGroup/Resource",
        rule="AwsSolutions-EC23",
        reason=(
            "This is the public entry point of a public API, so 0.0.0.0/0 is the requirement "
            "rather than the oversight - the alternative is an API nobody can reach. What the rule "
            "is really guarding against, a large open port range, is not what this group does: it "
            "opens exactly one port, 443 when a certificate was supplied and 80 only on a dev "
            "deployment (AppContext.validate refuses a prod deployment without a certificate), and "
            "its egress is restricted to the tasks' security group on the API port alone. "
            "Narrowing the source further would mean knowing the customer's address range, which "
            "is a deployment-time fact this repository cannot hold."
        ),
    ),
    Suppression(
        stack="network",
        path="EndpointSecurityGroup/Resource",
        rule="CdkNagValidationFailure",
        reason=(
            "AwsSolutions-EC23 cannot evaluate this group because its one ingress rule is written "
            'against the VPC\'s own CIDR, which synthesises to {"Fn::GetAtt": ["Vpc", '
            '"CidrBlock"]} - an intrinsic the rule resolves to a non-primitive and gives up on. '
            "The rule would have passed: the source is the VPC's CIDR rather than 0.0.0.0/0, the "
            "port is 443 alone, and the group has no egress. Suppressed so that a real validation "
            "failure elsewhere is visible instead of being one line in a wall of known noise."
        ),
    ),
    Suppression(
        stack="database",
        path="PrivacySalt/Resource",
        rule="AwsSolutions-SMG4",
        reason=(
            "Plan D (DEC-860): this secret is the salt of every principal hash the product stores - "
            "the consent ledger, the erasure register, the audit trail. It is not a credential and "
            "grants access to nothing; rotating it would not limit an exposure but would make every "
            "hash already written unmatchable, so an erased person's request could no longer be found "
            "and a consent could no longer be checked. It is generated once, encrypted with the "
            "deployment's key and retained."
        ),
    ),
    Suppression(
        stack="database",
        path="ApplicationSecret/Resource",
        rule="AwsSolutions-SMG4",
        reason=(
            "The credential this deployment holds does rotate: marketing-ai/<env>/db is generated "
            "by the database stack, is what RDS knows about, and carries an AWS single-user "
            "rotation schedule - AwsSolutions-SMG4 passes on it. This second secret is not a "
            "credential of anything. It is the application document engine/aws/secrets.py reads, "
            "whose keys are Settings field names, and its one key is a database_url composed at "
            "deploy time from the credential secret through a CloudFormation dynamic reference. "
            "There is no service behind it for a rotation function to rotate against, and pointing "
            "AWS's rotation function at it would rewrite the URL to a password RDS does not have. "
            "This is a known seam and it is written down as one (DEC-376): the composed URL is a "
            "snapshot, so it goes stale when the credential rotates and a redeploy of this stack "
            "recomposes it. The suppression goes away when engine.settings can read the standard "
            "RDS credential document and build the URL itself, at which point this secret does not "
            "exist. infra/README.md says what an operator must do when the rotation fires."
        ),
    ),
    Suppression(
        stack="compute",
        path="TaskRole/DefaultPolicy/Resource",
        rule="AwsSolutions-IAM5",
        applies_to=(_WILDCARD_FINDING,),
        applies_to_regex=(
            _S3_PREFIX_FINDINGS,
            _SSM_PATH_FINDINGS,
            _LOG_STREAM_FINDINGS,
            _JOB_NAME_FINDINGS,
        ),
        reason=(
            f"{_PREFIX_IS_THE_BOUNDARY} {_SSM_IS_THE_BOUNDARY} {_LOG_STREAM_IS_THE_BOUNDARY} "
            f"{_JOB_NAME_IS_THE_BOUNDARY} {_WILDCARD_IS_UNAVOIDABLE}"
        ),
    ),
    Suppression(
        stack="compute",
        path="ExecutionRole/DefaultPolicy/Resource",
        rule="AwsSolutions-IAM5",
        applies_to=(_WILDCARD_FINDING,),
        applies_to_regex=(_LOG_STREAM_FINDINGS,),
        reason=(
            "This is the ECS agent's role - pull the image, open a log stream - and it is written "
            "out by hand precisely to avoid the managed AmazonECSTaskExecutionRolePolicy, which "
            f"grants ecr:* against every repository in the account and logs:CreateLogGroup. "
            f"{_LOG_STREAM_IS_THE_BOUNDARY} {_WILDCARD_IS_UNAVOIDABLE}"
        ),
    ),
    Suppression(
        stack="operations",
        path="JobTaskDefinition/Resource",
        rule="AwsSolutions-ECS2",
        reason=(
            f"The scheduled-job container (`{JOB_CONTAINER_NAME}`) carries exactly the three "
            "environment variables the API task does - MARKETING_AI_SETTINGS_SOURCE, "
            "MARKETING_AI_ENV and MARKETING_AI_AWS_REGION - for the same reason: they say which "
            "deployment this is and where to read it from, are not credentials, and are already "
            "public in the stack name. Everything else, secrets included, is read by the task "
            "itself from Parameter Store and one Secrets Manager ARN (DEC-377), and a variable here "
            "would override the parameter of the same name for ever."
        ),
    ),
    Suppression(
        stack="operations",
        path="ApiOperationsPolicy/Resource",
        rule="AwsSolutions-IAM5",
        applies_to_regex=(_AUDIT_PREFIX_FINDINGS, _SCHEDULE_FINDINGS, _ROW_LEVEL_VERSION_FINDINGS),
        reason=_OPERATIONS_TASK_POLICY_REASON,
    ),
    Suppression(
        stack="operations",
        path="SchedulerRole/DefaultPolicy/Resource",
        rule="AwsSolutions-IAM5",
        applies_to_regex=(_JOB_TASK_FINDINGS, _JOB_TASK_TAG_FINDINGS),
        reason=(
            "EventBridge Scheduler's role may start one task-definition family and nothing else. "
            f"`task-definition/{PRODUCT}-<env>-job:*` - the `*` is the revision number, which "
            "CloudFormation assigns on every deployment, and a schedule names the family so it keeps "
            "working after one; the ecs:cluster condition confines RunTask to this deployment's "
            f"cluster. `task/{PRODUCT}-<env>/*` - the `*` is a task id ECS generates at RunTask "
            "time, and the grant is ecs:TagResource under an ecs:CreateAction=RunTask condition, "
            "so it can tag the task it is starting and cannot retag anything that already exists. "
            "The trust policy pins aws:SourceAccount and the schedule group's ARN."
        ),
    ),
    Suppression(
        stack="sagemaker",
        path="ExecutionRole/DefaultPolicy/Resource",
        rule="AwsSolutions-IAM5",
        applies_to=(_WILDCARD_FINDING,),
        applies_to_regex=(
            _S3_PREFIX_FINDINGS,
            _SSM_PATH_FINDINGS,
            _LOG_STREAM_FINDINGS,
            _SAGEMAKER_LOG_FINDINGS,
        ),
        reason=(
            f"{_PREFIX_IS_THE_BOUNDARY} {_SSM_IS_THE_BOUNDARY} {_LOG_STREAM_IS_THE_BOUNDARY} "
            f"{_SAGEMAKER_LOG_IS_THE_BOUNDARY} {_WILDCARD_IS_UNAVOIDABLE}"
        ),
    ),
)
"""The suppressions that hold whatever the context is.

Findings that depend on the context - a dev database with no multi-AZ and no deletion protection, a
cluster whose Container Insights are off - are added by `suppressions_for` only for the deployment
that actually has them, so a prod synthesis carries no suppression covering a risk it does not take.
"""


def suppressions_for(deployment: Deployment) -> tuple[Suppression, ...]:
    """`SUPPRESSIONS`, plus the ones this particular deployment's context makes necessary.

    Each conditional suppression is keyed on the *property the rule checks*, not on `env_name`.
    That distinction has teeth: `db_multi_az` is overridable, so `-c env_name=dev -c
    db_multi_az=true` is a dev deployment with multi-AZ on and no RDS3 finding to suppress, while
    its deletion protection is still off and RDS10 still fires. Keying both on `env_name` would
    have attached one suppression to a finding that no longer exists and left the other uncovered.
    """
    context = deployment.context
    extra: list[Suppression] = []
    if not context.db_multi_az:
        extra.append(
            Suppression(
                stack="database",
                path="Postgres/Resource",
                rule="AwsSolutions-RDS3",
                reason=(
                    "Multi-AZ is off because this deployment did not ask for it. It is what "
                    "env_name=dev defaults to and it is the one env_name default that can be "
                    "overridden (-c db_multi_az=true), because a dev deployment that wants to "
                    "rehearse a failover should be able to. A prod synthesis defaults it on and "
                    "never reaches this suppression. The risk accepted is that a dev deployment "
                    "loses its metadata database for the duration of an AZ failure; the artefacts "
                    "are in S3 and are not affected, and the schema is rebuilt by `make migrate`."
                ),
            )
        )
    if not context.db_deletion_protection:
        extra.append(
            Suppression(
                stack="database",
                path="Postgres/Resource",
                rule="AwsSolutions-RDS10",
                reason=(
                    "Deletion protection is off because removal_policy_destroy is on, and both are "
                    "what env_name=dev means: a dev deployment is expected to be torn down and "
                    "rebuilt, and a dev stack that cannot be destroyed is a support ticket. A prod "
                    "synthesis sets both the other way and does not reach this suppression - see "
                    "the `what env_name changes` table in infra/context.py."
                ),
            )
        )
    if not context.container_insights:
        extra.append(
            Suppression(
                stack="compute",
                path="Cluster/Resource",
                rule="AwsSolutions-ECS4",
                reason=(
                    "Container Insights is billed per metric collected per task, so this "
                    "deployment leaves it to an explicit `-c container_insights=true` rather than "
                    "turning on a recurring charge by default. Turning it on in prod only would "
                    "have been a guess about a bill nobody here has seen. What the product "
                    "actually alarms on does not depend on it: the observability stack creates the "
                    "log groups and the metric filters, and the application emits its own metrics "
                    "as CloudWatch EMF (MARKETING_AI_METRICS_BACKEND=emf). What is genuinely lost "
                    "is per-task CPU and memory utilisation, which is what an operator would want "
                    "before changing api_cpu or api_memory - so turn it on before tuning those."
                ),
            )
        )
    return tuple(extra)


def apply_suppressions(deployment: Deployment) -> None:
    """Apply `SUPPRESSIONS`, plus the ones that only this deployment's context needs."""
    for suppression in (*SUPPRESSIONS, *suppressions_for(deployment)):
        _apply(deployment, suppression)


def _apply(deployment: Deployment, suppression: Suppression) -> None:
    stack = deployment.stack(suppression.stack)
    applies_to: list[str | RegexAppliesTo] = [
        *suppression.applies_to,
        *(RegexAppliesTo(regex=pattern) for pattern in suppression.applies_to_regex),
    ]
    entry = NagPackSuppression(
        id=suppression.rule,
        reason=suppression.reason,
        applies_to=applies_to or None,
    )
    if not suppression.path:
        NagSuppressions.add_stack_suppressions(stack, [entry])
        return
    NagSuppressions.add_resource_suppressions_by_path(
        stack, f"/{stack.stack_name}/{suppression.path}", [entry]
    )
