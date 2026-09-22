"""The service: a Fargate task behind a load balancer, and the parameters it configures itself from.

The task definition sets **three** environment variables. That is not minimalism for its own sake,
it is the only shape that leaves the SSM parameters meaning anything.

`Settings.from_aws` merges four sources, lowest first: SSM, the application secret, the process
environment, then explicit overrides (DEC-301). The environment beats SSM. So a task definition
that also sets `MARKETING_AI_S3_BUCKET` does not "agree with" the parameter of the same name - it
*overrides* it, permanently and invisibly, and an operator who edits
`/marketing-ai/<env>/s3_bucket` watches their change have no effect. Everything this deployment
knows therefore goes into Parameter Store, and the task definition carries only what has to be true
before any of it can be read: which deployment this is, which region to read it from, and that it
should read AWS at all (DEC-377).

`MARKETING_AI_SETTINGS_SOURCE` is the one name here that is not a `Settings` field. It is in
`NON_FIELD_ENV_VARS`, which is what makes it legal; a `MARKETING_AI_*` variable that is in neither
list makes the application refuse to start, so the task definition is checked against both in
`tests/infra/test_task_definition.py`.

The database password is not here in any form. The task reads the application secret itself, with
`secretsmanager:GetSecretValue` on exactly one ARN - so what the deployment hands the container is
a name, and what the container fetches is never rendered into a task definition that anyone with
`ecs:DescribeTaskDefinition` can read.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.context import AppContext
from infra.database import DATABASE_SCHEMA
from infra.naming import (
    JOB_NAME_PREFIX,
    PRODUCT,
    SETTINGS_ENV_VARS,
    ssm_parameter_name,
)
from infra.network import API_PORT, APP_SUBNET_GROUP
from infra.policies import (
    bedrock_statements,
    ecr_pull_statements,
    kms_use_statement,
    log_write_statement,
    metrics_statement,
    pass_role_statement,
    s3_artefact_statements,
    sagemaker_job_statements,
    settings_read_statements,
)

__all__ = [
    "HEALTH_CHECK_PATH",
    "START_PERIOD_SECONDS",
    "TARGET_CPU_PERCENT",
    "ComputeStack",
]

HEALTH_CHECK_PATH: Final[str] = "/healthz"
"""`api/main.py` serves it; it is a liveness probe and touches no backend."""

START_PERIOD_SECONDS: Final[int] = 120
"""How long a task may take to answer its first health check before failures count.

**Chosen, not measured.** The reason is that AutoGluon's import graph is heavy - it pulls
lightgbm, xgboost, catboost and scikit-learn before `api.main` finishes importing - so a task that
is starting normally is unresponsive for a while and a short grace period would kill it and start
another, forever. The number itself is the one `Dockerfile`'s HEALTHCHECK already uses, kept the
same so a container behaves the same way in both places. Nobody has timed this image's start on
Fargate; the first real deployment should, and should replace this with what it measured.
"""

HEALTH_CHECK_INTERVAL_SECONDS: Final[int] = 30
HEALTH_CHECK_TIMEOUT_SECONDS: Final[int] = 5
HEALTHY_THRESHOLD: Final[int] = 2
UNHEALTHY_THRESHOLD: Final[int] = 3
"""Load-balancer probe timings. Chosen to match `Dockerfile`'s HEALTHCHECK, not measured."""

TARGET_CPU_PERCENT: Final[int] = 60
"""Average CPU the autoscaler aims for.

Chosen. A target is a trade between headroom and cost, not a property of the workload, and 60%
leaves room for the burst a training request causes before a new task is in service.
"""

SCALE_COOLDOWN_SECONDS: Final[int] = 120
"""Chosen: long enough that a new task has finished its slow import before the next decision."""


class ComputeStack(Stack):
    """The ECS cluster, the task definition, the load balancer, and the deployment's parameters."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        context: AppContext,
        vpc: ec2.IVpc,
        app_subnets: ec2.SubnetSelection,
        service_security_group: ec2.ISecurityGroup,
        alb_security_group: ec2.ISecurityGroup,
        jobs_security_group: ec2.ISecurityGroup,
        bucket: s3.IBucket,
        log_bucket: s3.IBucket,
        key: kms.IKey,
        repository: ecr.IRepository,
        api_log_group: logs.ILogGroup,
        application_secret: secretsmanager.ISecret,
        sagemaker_role: iam.IRole,
        alarm_topic_arn: str,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context
        self.image_reference = self._image_reference(repository)

        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=f"{PRODUCT}-{context.env_name}",
            vpc=vpc,
            # Billed per metric collected, per task. Genuinely useful and not free, so it is a
            # context key (`-c container_insights=true`) rather than a default either way; the
            # metric filters in the observability stack cover what this product actually alarms on.
            container_insights_v2=(
                ecs.ContainerInsights.ENABLED
                if context.container_insights
                else ecs.ContainerInsights.DISABLED
            ),
        )

        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{PRODUCT}-{context.env_name}-task-execution",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="What the ECS agent needs to start a task: pull the image, open a log stream",
        )
        # Written out rather than `AmazonECSTaskExecutionRolePolicy`, which grants
        # `ecr:*`-style access to every repository in the account and `logs:CreateLogGroup`.
        for statement in (
            *ecr_pull_statements(repository.repository_arn),
            kms_use_statement(key.key_arn, sid="ImageKey"),
            log_write_statement([api_log_group.log_group_arn], sid="ApiLogStreams"),
        ):
            self.execution_role.add_to_principal_policy(statement)

        self.task_role = iam.Role(
            self,
            "TaskRole",
            role_name=f"{PRODUCT}-{context.env_name}-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="What the running API may do",
        )
        for statement in (
            *s3_artefact_statements(bucket.bucket_arn),
            kms_use_statement(key.key_arn),
            *settings_read_statements(
                account=self.account,
                region=self.region,
                env_name=context.env_name,
                secret_arn=application_secret.secret_arn,
            ),
            *sagemaker_job_statements(account=self.account, region=self.region),
            pass_role_statement(sagemaker_role.role_arn),
            log_write_statement([api_log_group.log_group_arn]),
            metrics_statement(),
            *bedrock_statements(
                enabled=context.bedrock_enabled,
                model_ids=context.bedrock_model_ids,
                region=context.region,
            ),
        ):
            self.task_role.add_to_principal_policy(statement)

        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            family=f"{PRODUCT}-{context.env_name}-api",
            cpu=context.api_cpu,
            memory_limit_mib=context.api_memory,
            execution_role=self.execution_role,
            task_role=self.task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        self.container = self.task_definition.add_container(
            "api",
            image=ecs.ContainerImage.from_registry(self.image_reference),
            essential=True,
            environment=self.task_environment(),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=api_log_group),
            port_mappings=[ecs.PortMapping(container_port=API_PORT, protocol=ecs.Protocol.TCP)],
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    'python -c "import urllib.request,sys; '
                    f"sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:{API_PORT}"
                    f"{HEALTH_CHECK_PATH}', timeout=4).status == 200 else 1)\"",
                ],
                interval=Duration.seconds(HEALTH_CHECK_INTERVAL_SECONDS),
                timeout=Duration.seconds(HEALTH_CHECK_TIMEOUT_SECONDS),
                retries=UNHEALTHY_THRESHOLD,
                start_period=Duration.seconds(START_PERIOD_SECONDS),
            ),
            readonly_root_filesystem=False,  # AutoGluon and pyarrow both write to a temp directory
        )

        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            load_balancer_name=f"{PRODUCT}-{context.env_name}",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            # A header the load balancer cannot parse is dropped rather than forwarded, so a
            # request-smuggling attempt cannot reach the application to be interpreted differently.
            drop_invalid_header_fields=True,
            idle_timeout=Duration.seconds(180),
        )
        self.load_balancer.log_access_logs(log_bucket, prefix="alb")

        self.service = ecs.FargateService(
            self,
            "Service",
            service_name=f"{PRODUCT}-{context.env_name}-api",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=context.api_min_tasks,
            security_groups=[service_security_group],
            vpc_subnets=app_subnets,
            assign_public_ip=False,
            health_check_grace_period=Duration.seconds(START_PERIOD_SECONDS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            enable_execute_command=False,
            min_healthy_percent=100 if context.api_min_tasks > 1 else 0,
        )

        self.listener = self._listener()
        self.target_group = self.listener.add_targets(
            "ApiTargets",
            port=API_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path=HEALTH_CHECK_PATH,
                healthy_http_codes="200",
                interval=Duration.seconds(HEALTH_CHECK_INTERVAL_SECONDS),
                timeout=Duration.seconds(HEALTH_CHECK_TIMEOUT_SECONDS),
                healthy_threshold_count=HEALTHY_THRESHOLD,
                unhealthy_threshold_count=UNHEALTHY_THRESHOLD,
            ),
        )

        scaling = self.service.auto_scale_task_count(
            min_capacity=context.api_min_tasks,
            max_capacity=context.api_max_tasks,
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=TARGET_CPU_PERCENT,
            scale_in_cooldown=Duration.seconds(SCALE_COOLDOWN_SECONDS),
            scale_out_cooldown=Duration.seconds(SCALE_COOLDOWN_SECONDS),
        )

        self.parameters = self._publish_parameters(
            bucket=bucket,
            key=key,
            sagemaker_role=sagemaker_role,
            jobs_security_group=jobs_security_group,
            vpc=vpc,
        )

        self.availability_alarm = cloudwatch.CfnAlarm(
            self,
            "NoHealthyTargets",
            alarm_name=f"{PRODUCT}-{context.env_name}-no-healthy-targets",
            alarm_description=(
                "No task is answering /healthz. This is not a tuning threshold: zero healthy "
                "targets is the service being down, whatever anybody's opinion about latency is."
            ),
            namespace="AWS/ApplicationELB",
            metric_name="HealthyHostCount",
            dimensions=[
                cloudwatch.CfnAlarm.DimensionProperty(
                    name="LoadBalancer", value=self.load_balancer.load_balancer_full_name
                ),
                cloudwatch.CfnAlarm.DimensionProperty(
                    name="TargetGroup", value=self.target_group.target_group_full_name
                ),
            ],
            statistic="Minimum",
            period=60,
            evaluation_periods=3,
            threshold=1,
            comparison_operator="LessThanThreshold",
            treat_missing_data="breaching",
            actions_enabled=True,
            alarm_actions=[alarm_topic_arn],
            ok_actions=[alarm_topic_arn],
        )

        CfnOutput(
            self,
            "ServiceUrl",
            value=f"{'https' if context.https_only else 'http'}://"
            f"{context.domain_name or self.load_balancer.load_balancer_dns_name}",
            description="Where the API answers",
        )
        CfnOutput(self, "ImageReference", value=self.image_reference, description="What the task runs")

    # ------------------------------------------------------------------
    def task_environment(self) -> dict[str, str]:
        """The task definition's environment: three variables, every one of them justified.

        A public method because `tests/infra/test_task_definition.py` compares the *synthesised*
        task definition against the frozen table, and this is the list the assertion is about.
        """
        return {
            # Not a Settings field; it is in NON_FIELD_ENV_VARS, and it is what turns on the SSM
            # and Secrets Manager reads that fill everything else.
            "MARKETING_AI_SETTINGS_SOURCE": "aws",
            SETTINGS_ENV_VARS["env"]: self.context.env_name,
            # Which region's Parameter Store to read. `Settings` would also accept AWS_REGION, but
            # naming its own variable means the deployment is not relying on what the ECS agent
            # happens to inject.
            SETTINGS_ENV_VARS["region"]: self.context.region,
        }

    def deployment_parameters(self, **values: str) -> dict[str, str]:
        """`{field_name: value}` for every SSM parameter this deployment writes."""
        context = self.context
        parameters: dict[str, str] = {
            "storage_backend": "s3",
            "metadata_backend": "postgres",
            "postgres_schema": DATABASE_SCHEMA,
            "job_backend": "sagemaker",
            "sagemaker_train_instance_type": context.sagemaker_instance_train,
            "sagemaker_processing_instance_type": context.sagemaker_instance_process,
            "sagemaker_max_concurrent_jobs": str(context.max_concurrent_jobs),
            "sagemaker_job_name_prefix": JOB_NAME_PREFIX,
            "bedrock_enabled": "true" if context.bedrock_enabled else "false",
            "log_level": "INFO",
            # The CloudWatch shape. `engine/utils/logging.py` writes one JSON object per line, which
            # is what the metric filters in the observability stack match on.
            "log_format": "json",
            "metrics_backend": "emf",
            "cors_origins": self.cors_origins(),
            **values,
        }
        if context.client_id:
            parameters["client_id"] = context.client_id
        if context.bedrock_model_ids:
            parameters["bedrock_model_ids"] = ",".join(context.bedrock_model_ids)
        return parameters

    def cors_origins(self) -> str:
        """Which origins the API answers.

        `Settings` refuses `*` on a prod deployment (DEC-307), and `AppContext.validate` refuses a
        prod deployment with no `domain_name`, so there is always an origin to name here.
        """
        if self.context.domain_name:
            return f"https://{self.context.domain_name}"
        return "*"

    # ------------------------------------------------------------------
    def _image_reference(self, repository: ecr.IRepository) -> str:
        if self.context.image_digest:
            return self.context.image_digest
        Annotations.of(self).add_warning(
            "No image_digest: this synthesis names the repository's `latest` tag, which is not a "
            "deployable answer - a tag can be moved after it was tested. `make aws-deploy` passes "
            "-c image_digest=$(cat .image-digest); a bare `cdk synth` does not, which is why this "
            "warning is normal during a lint or a nag run and is not normal during a deployment."
        )
        return f"{repository.repository_uri}:latest"

    def _listener(self) -> elbv2.ApplicationListener:
        """HTTPS when there is a certificate; otherwise plain HTTP, loudly, and in dev only."""
        if self.context.certificate_arn:
            return self.load_balancer.add_listener(
                "Https",
                port=443,
                protocol=elbv2.ApplicationProtocol.HTTPS,
                certificates=[
                    elbv2.ListenerCertificate.from_arn(self.context.certificate_arn),
                ],
                ssl_policy=elbv2.SslPolicy.TLS13_RES,
                open=False,
            )
        Annotations.of(self).add_warning(
            "HTTP-ONLY LOAD BALANCER. No certificate_arn was given, so this deployment serves "
            "every upload, every pre-signed download URL and every API response in clear text over "
            "the public internet. `AppContext.validate` refuses this for env_name=prod; it is "
            "allowed here only because this is a dev deployment. Pass -c certificate_arn=<acm arn> "
            "-c domain_name=<name> to fix it."
        )
        return self.load_balancer.add_listener(
            "Http",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
        )

    def _publish_parameters(
        self,
        *,
        bucket: s3.IBucket,
        key: kms.IKey,
        sagemaker_role: iam.IRole,
        jobs_security_group: ec2.ISecurityGroup,
        vpc: ec2.IVpc,
    ) -> dict[str, ssm.StringParameter]:
        """Write `/marketing-ai/<env>/<field>` for everything the deployment knows.

        Flat and leaf-named, because `AwsParameterSource.parameters` calls `GetParametersByPath`
        with `Recursive=False`: a parameter one level deeper would be written, would look right in
        the console, and would never be read. `ssm_parameter_name` refuses a leaf that is not a
        `Settings` field, which is the same check from the other side.
        """
        subnet_ids = ",".join(vpc.select_subnets(subnet_group_name=APP_SUBNET_GROUP).subnet_ids)
        values = self.deployment_parameters(
            s3_bucket=bucket.bucket_name,
            s3_kms_key_id=key.key_arn,
            sagemaker_role_arn=sagemaker_role.role_arn,
            sagemaker_image_uri=self.image_reference,
            sagemaker_subnet_ids=subnet_ids,
            sagemaker_security_group_ids=jobs_security_group.security_group_id,
        )
        created: dict[str, ssm.StringParameter] = {}
        for field_name, value in sorted(values.items()):
            created[field_name] = ssm.StringParameter(
                self,
                f"Parameter{_construct_id(field_name)}",
                parameter_name=ssm_parameter_name(self.context.env_name, field_name),
                string_value=value,
                description=f"Settings.{field_name} for the {self.context.env_name} deployment",
                tier=ssm.ParameterTier.STANDARD,
            )
            created[field_name].apply_removal_policy(
                RemovalPolicy.DESTROY if self.context.removal_policy_destroy else RemovalPolicy.RETAIN
            )
        return created


def _construct_id(field_name: str) -> str:
    """`s3_bucket` -> `S3Bucket`."""
    return "".join(part.capitalize() for part in field_name.split("_") if part)
