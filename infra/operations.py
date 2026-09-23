"""Phase 4b's resources: the audit lock, the scheduler, the alert topic, and the API's new rights.

Plan M46-M49 was built local-first - sign-in, an append-only audit log, DPDP retention and erasure,
schedules and alerts all run on a laptop - and each has an AWS half that only a deployment can
provide. This stack is that half, and nothing else:

* **An audit-export bucket with S3 Object Lock in COMPLIANCE mode.** `engine/audit/export.py`
  writes each export with `ObjectLockMode=COMPLIANCE` and a retain-until date (DEC-715); Object
  Lock can only be switched on when a bucket is *created*, so the bucket has to exist, locked,
  before the first export. The bucket's default retention is the same number of days as
  `Settings.audit_retention_days`, so an object that somehow arrives without a date is locked
  exactly as long as one that carries it.
* **An EventBridge Scheduler group, the role the scheduler assumes, and the task it runs.** A
  schedule fires an ECS `RunTask` of the product's own image running `scripts/fire_schedule.py`,
  in the same cluster, with the same task role as the API: a scheduled scoring run is the API's
  work done at a time nobody clicked, so it holds the API's rights and not a second, drifting copy
  of them. The scheduler's own role can start that one task-definition family in that one cluster
  and pass its two roles to ECS - nothing else.
* **An SNS topic for application alerts** (drift, performance drop, a failed scheduled job), with
  the deployment's `alert_email` subscribed when there is one - plan prerequisite P3.
* **The API task role's new statements**: write-only to the audit prefix, schedule management in
  this deployment's group, `iam:PassRole` on the scheduler role only, `sns:Publish` on the alert
  topic, and the lifecycle and version-delete rights the DPDP retention job and erasure requests
  need on the artefact bucket (`infra/policies.py` says why each is there).
* **The Parameter Store values that switch the engine over**: `scheduler_backend=eventbridge`,
  `alert_backend=sns`, `auth_mode`, and the names and ARNs above - under `/marketing-ai/<env>/`,
  never on the task definition, for the reason `infra/compute.py` gives (DEC-377): a variable on
  the task definition silently overrides the parameter of the same name for ever.

**Why a separate stack rather than four additions to existing ones.** Two reasons, one practical
and one structural. Practically, every Phase 4a stack is pinned by assertions that count what it
contains - one topic in observability, two buckets in storage, one task definition and one
`iam:PassRole` in compute - and those counts are the point of those tests: they are what makes a
resource that "quietly arrived" visible. Adding to those stacks would mean loosening them.
Structurally, the new rights are attached to the task role as a *separate* inline policy
(`ApiOperationsPolicy`) that lives here, so a reviewer reads Phase 4b's grants in one document,
and turning Phase 4b off is deleting one stack, not editing four. The price is one more arrow in
the chain - compute -> operations - and it points the same way as every other one.

Nothing here adds a resource to a Phase 4a stack. Two Phase 4a templates do change, both
visibly: the compute stack gains CloudFormation exports (the cluster ARN and the role ARNs this
stack references), which is how cross-stack references are made, and the storage stack's
access-log bucket policy gains one statement letting S3 deliver the audit bucket's access logs -
written out, with its conditions, in `StorageStack._allow_audit_bucket_access_logs` rather than
left to CDK, which would have added it with none.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.context import AppContext
from infra.naming import (
    AUDIT_EXPORT_PREFIX,
    JOB_CONTAINER_NAME,
    PRODUCT,
    audit_bucket_name,
    cluster_name,
    job_task_family,
    schedule_group_name,
    ssm_parameter_name,
)
from infra.policies import (
    alert_publish_statement,
    audit_export_write_statement,
    pass_role_statement,
    retention_statements,
    run_job_task_statements,
    schedule_management_statement,
)
from infra.storage import ABORT_INCOMPLETE_UPLOAD_DAYS, AUDIT_ACCESS_LOG_PREFIX, TLS_VERSION_FLOOR

__all__ = ["FIRE_SCHEDULE_COMMAND", "OperationsStack"]

FIRE_SCHEDULE_COMMAND: Final[tuple[str, ...]] = ("python", "-m", "scripts.fire_schedule")
"""The scheduled-job container's default command.

`scripts/entrypoint.sh` execs any word it does not recognise, so this runs the CLI directly. A
schedule overrides it with `containerOverrides[{"name": "job", "command": [...]}]` carrying the
schedule's own arguments; the default, run bare, is the CLI refusing a missing argument, which is
the right result for a `RunTask` somebody started by hand without saying what to fire.
"""


class OperationsStack(Stack):
    """The AWS half of Phase 4b: audit lock, scheduler, alerts, and the API's grants for them."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        context: AppContext,
        key: kms.IKey,
        bucket: s3.IBucket,
        log_bucket: s3.IBucket,
        cluster: ecs.ICluster,
        task_role: iam.IRole,
        execution_role: iam.IRole,
        image_reference: str,
        task_environment: Mapping[str, str],
        api_log_group: logs.ILogGroup,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context

        self.audit_key = self._audit_key(task_role=task_role)
        self.audit_bucket = self._audit_bucket(key=self.audit_key, log_bucket=log_bucket)
        self.alert_topic = self._alert_topic(key)
        self.job_task_definition = self._job_task_definition(
            task_role=task_role,
            execution_role=execution_role,
            image_reference=image_reference,
            task_environment=task_environment,
            api_log_group=api_log_group,
        )

        self.schedule_group = scheduler.CfnScheduleGroup(
            self, "ScheduleGroup", name=schedule_group_name(context.env_name)
        )
        self.schedule_group.apply_removal_policy(
            RemovalPolicy.DESTROY if context.removal_policy_destroy else RemovalPolicy.RETAIN
        )
        self.scheduler_role = self._scheduler_role(
            cluster=cluster, task_role=task_role, execution_role=execution_role
        )

        # Attached to the compute stack's task role from here, as its own inline policy, so the
        # compute stack's `DefaultPolicy` - and every assertion made about it - is untouched.
        self.api_policy = iam.Policy(
            self,
            "ApiOperationsPolicy",
            policy_name=f"{PRODUCT}-{context.env_name}-api-operations",
            roles=[task_role],
            statements=[
                audit_export_write_statement(self.audit_bucket.bucket_arn, AUDIT_EXPORT_PREFIX),
                schedule_management_statement(
                    account=self.account,
                    region=self.region,
                    group_name=schedule_group_name(context.env_name),
                ),
                pass_role_statement(
                    self.scheduler_role.role_arn,
                    service="scheduler.amazonaws.com",
                    sid="PassSchedulerRole",
                ),
                alert_publish_statement(self.alert_topic.topic_arn),
                *retention_statements(bucket.bucket_arn),
            ],
        )

        self.parameters = self._publish_parameters(cluster=cluster)

        CfnOutput(self, "AuditBucketName", value=self.audit_bucket.bucket_name, description="Audit exports")
        CfnOutput(self, "AlertTopicArn", value=self.alert_topic.topic_arn, description="Application alerts")
        CfnOutput(
            self,
            "ScheduleGroupName",
            value=schedule_group_name(context.env_name),
            description="EventBridge Scheduler group every schedule is created in",
        )
        CfnOutput(
            self,
            "SchedulerRoleArn",
            value=self.scheduler_role.role_arn,
            description="The role a schedule's target runs as (Settings.scheduler_role_arn)",
        )
        CfnOutput(
            self,
            "JobTaskDefinitionArn",
            value=self.job_task_definition_family_arn,
            description=(
                "The scheduled-job task definition *family* - a schedule names this, never a revision "
                f"- and its container is `{JOB_CONTAINER_NAME}`"
            ),
        )

    # ------------------------------------------------------------------
    @property
    def job_task_definition_family_arn(self) -> str:
        """`arn:aws:ecs:<region>:<account>:task-definition/<family>`, with no revision.

        What a schedule's `EcsParameters.TaskDefinitionArn` should carry: `RunTask` given no
        revision runs the latest ACTIVE one, so a schedule created today still fires after the next
        deployment registers a new revision and deregisters this one.
        """
        return f"arn:aws:ecs:{self.region}:{self.account}:task-definition/{job_task_family(self.context.env_name)}"

    def operations_parameters(self, **values: str) -> dict[str, str]:
        """`{field_name: value}` for every Phase 4b SSM parameter this stack writes.

        The companion of `ComputeStack.deployment_parameters`, and public for the same reason:
        `tests/infra/test_phase4b_parameters.py` builds a `Settings` from the two together.
        """
        return {
            "auth_mode": self.context.auth_mode,
            "audit_export_prefix": AUDIT_EXPORT_PREFIX,
            "audit_retention_days": str(self.context.audit_retention_days),
            "scheduler_backend": "eventbridge",
            "scheduler_group_name": schedule_group_name(self.context.env_name),
            "alert_backend": "sns",
            **values,
        }

    # ------------------------------------------------------------------
    def _audit_key(self, *, task_role: iam.IRole) -> kms.Key:
        """The audit exports' own key, retained at every `env_name` (DEC-727).

        The product key is `DESTROY` on a non-prod stack, and a key scheduled for deletion makes
        every COMPLIANCE-locked export under it unreadable 30 days after `cdk destroy` - Object Lock
        would keep bytes nobody can decrypt. This key outlives the stack exactly as the bucket does.
        The API's task role may only generate data keys with it (write the exports), granted in the
        key policy so no other stack's policy changes.
        """
        key = kms.Key(
            self,
            "AuditExportKey",
            description=f"{PRODUCT} {self.context.env_name} audit exports (retained with the Object Lock bucket)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="ApiWritesAuditExports",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(task_role.role_arn)],
                actions=["kms:GenerateDataKey"],
                resources=["*"],
            )
        )
        return key

    def _audit_bucket(self, *, key: kms.IKey, log_bucket: s3.IBucket) -> s3.Bucket:
        """The Object Lock bucket. Retained at every `env_name`, because it cannot be deleted anyway.

        A bucket holding a COMPLIANCE-locked version cannot be emptied until the lock lapses, so
        `RemovalPolicy.DESTROY` would not destroy it - it would fail the stack deletion instead,
        which is worse than keeping it on purpose.
        """
        context = self.context
        bucket = s3.Bucket(
            self,
            "AuditExports",
            bucket_name=audit_bucket_name(context.bucket_name_prefix, context.env_name, self.account),
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.compliance(
                Duration.days(context.audit_retention_days)
            ),
            versioned=True,  # Object Lock requires it; CDK would switch it on regardless
            encryption=s3.BucketEncryption.KMS,
            encryption_key=key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=TLS_VERSION_FLOOR,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
            # An imported reference, deliberately: handed the real bucket from the storage stack,
            # CDK would add an unconditioned delivery grant to that bucket's policy. The storage
            # stack writes the conditioned one itself (StorageStack._allow_audit_bucket_access_logs).
            server_access_logs_bucket=s3.Bucket.from_bucket_name(
                self, "AccessLogTarget", log_bucket.bucket_name
            ),
            server_access_logs_prefix=AUDIT_ACCESS_LOG_PREFIX,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-multipart-uploads",
                    enabled=True,
                    abort_incomplete_multipart_upload_after=Duration.days(ABORT_INCOMPLETE_UPLOAD_DAYS),
                ),
            ],
        )
        Annotations.of(bucket).acknowledge_warning(
            "@aws-cdk/aws-s3:accessLogsPolicyNotAdded",
            "The delivery grant is written, with conditions, by StorageStack._allow_audit_bucket_access_logs.",
        )
        # Object Lock protects each *version* until its date. A `DeleteObject` without a version id
        # is still allowed by S3 - it lays a delete marker over the export, which hides it from
        # every ordinary listing while the bytes sit underneath. This deny closes that, for every
        # principal: removing an audit export means first editing this policy, which is itself a
        # CloudTrail management event.
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyAuditExportDeletion",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:DeleteObject", "s3:DeleteObjectVersion"],
                resources=[bucket.arn_for_objects("*")],
            )
        )
        return bucket

    def _alert_topic(self, key: kms.IKey) -> sns.Topic:
        """The application's alert topic, encrypted with the product key, TLS only."""
        context = self.context
        topic = sns.Topic(
            self,
            "Alerts",
            topic_name=f"{PRODUCT}-{context.env_name}-alerts",
            display_name=f"{PRODUCT} {context.env_name} alerts",
            master_key=key,
            enforce_ssl=True,
        )
        if context.alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(context.alert_email))
        else:
            Annotations.of(self).add_warning(
                "No alert_email: drift, performance-drop and failed-schedule alerts are published to "
                "a topic nobody is subscribed to. Pass -c alert_email=<address> and confirm the "
                "subscription (plan prerequisite P3)."
            )
        return topic

    def _job_task_definition(
        self,
        *,
        task_role: iam.IRole,
        execution_role: iam.IRole,
        image_reference: str,
        task_environment: Mapping[str, str],
        api_log_group: logs.ILogGroup,
    ) -> ecs.FargateTaskDefinition:
        """The task EventBridge Scheduler starts: the API's image, roles and environment.

        The roles are re-imported as immutable references. CDK's container constructs *grant*
        things to the roles they are handed - the `awslogs` driver adds a log-write statement to
        the execution role - and on the real role objects that grant would land in the compute
        stack's `DefaultPolicy`. The execution role already writes to the API log group, which is
        where this container logs (under its own `job/` stream prefix, so the `ApiErrors` metric
        filter counts a failed firing as well), so there is nothing to grant.
        """
        context = self.context
        definition = ecs.FargateTaskDefinition(
            self,
            "JobTaskDefinition",
            family=job_task_family(context.env_name),
            cpu=context.job_cpu,
            memory_limit_mib=context.job_memory,
            execution_role=iam.Role.from_role_arn(
                self, "JobExecutionRole", execution_role.role_arn, mutable=False
            ),
            task_role=iam.Role.from_role_arn(self, "JobTaskRole", task_role.role_arn, mutable=False),
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        definition.add_container(
            JOB_CONTAINER_NAME,
            image=ecs.ContainerImage.from_registry(image_reference),
            essential=True,
            command=list(FIRE_SCHEDULE_COMMAND),
            # The same three variables as the API task and for the same reason (DEC-377): every
            # other setting comes from Parameter Store, which the environment would override.
            environment=dict(task_environment),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="job", log_group=api_log_group),
            readonly_root_filesystem=False,  # the same import graph as the API; see compute.py
        )
        return definition

    def _scheduler_role(
        self, *, cluster: ecs.ICluster, task_role: iam.IRole, execution_role: iam.IRole
    ) -> iam.Role:
        """The identity EventBridge Scheduler assumes to start the job task.

        The trust policy carries the confused-deputy conditions AWS documents for the scheduler:
        the schedule assuming it must be in this account *and* in this deployment's group, so a
        schedule another deployment (or another account) creates cannot borrow this role.
        """
        context = self.context
        group_arn = (
            f"arn:aws:scheduler:{self.region}:{self.account}:schedule-group/"
            f"{schedule_group_name(context.env_name)}"
        )
        role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{PRODUCT}-{context.env_name}-scheduler",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account, "aws:SourceArn": group_arn}},
            ),
            description=f"What EventBridge Scheduler may do for {PRODUCT} {context.env_name}: run the job task",
        )
        for statement in run_job_task_statements(
            account=self.account,
            region=self.region,
            cluster_arn=cluster.cluster_arn,
            cluster=cluster_name(context.env_name),
            family=job_task_family(context.env_name),
            task_role_arns=[task_role.role_arn, execution_role.role_arn],
        ):
            role.add_to_principal_policy(statement)
        return role

    def _publish_parameters(self, *, cluster: ecs.ICluster) -> dict[str, ssm.StringParameter]:
        """Write the Phase 4b settings under `/marketing-ai/<env>/`, flat, as compute.py does."""
        values = self.operations_parameters(
            audit_export_bucket=self.audit_bucket.bucket_name,
            # EventBridge Scheduler's ECS target is addressed by the *cluster*; the task
            # definition, network and overrides travel in the schedule's `EcsParameters`.
            scheduler_target_arn=cluster.cluster_arn,
            scheduler_role_arn=self.scheduler_role.role_arn,
            alert_sns_topic_arn=self.alert_topic.topic_arn,
        )
        created: dict[str, ssm.StringParameter] = {}
        for field_name, value in sorted(values.items()):
            created[field_name] = ssm.StringParameter(
                self,
                f"Parameter{_construct_id(field_name)}",
                parameter_name=ssm_parameter_name(self.context.env_name, field_name),
                string_value=value,
                description=f"Settings.{field_name} for the {self.context.env_name} deployment (Phase 4b)",
                tier=ssm.ParameterTier.STANDARD,
            )
            created[field_name].apply_removal_policy(
                RemovalPolicy.DESTROY if self.context.removal_policy_destroy else RemovalPolicy.RETAIN
            )
        return created


def _construct_id(field_name: str) -> str:
    """`audit_export_bucket` -> `AuditExportBucket`."""
    return "".join(part.capitalize() for part in field_name.split("_") if part)
