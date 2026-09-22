"""The role a training or processing job assumes, and nothing else.

There is no SageMaker *resource* in this stack, and that is the design rather than an omission. A
job is created at run time by `SageMakerJobRunner` from a `JobSpec`, with a name derived from the
run - so the only thing a deployment can create ahead of time is the identity the job runs as, and
the boundary that identity is scoped to.

That boundary is a name prefix. Every job this product creates is called
`marketing-ai-<something>`, the runner only ever *describes* a job whose name it already holds, and
`infra/policies.py` scopes the caller's SageMaker statements to `training-job/marketing-ai-*` and
`processing-job/marketing-ai-*`. No `List*` action is granted anywhere: the runner does not need
one, and `sagemaker:ListTrainingJobs` cannot be scoped to a prefix, so granting it would mean
showing this deployment every job in the account.

The execution role itself is the *job's* identity, not the caller's. It reads and writes artefacts,
pulls the image, writes logs, and - because a VPC-attached job makes SageMaker create ENIs in our
subnets - carries the one EC2 statement AWS documents with no resource types (DEC-370).
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.context import AppContext
from infra.naming import PRODUCT
from infra.policies import (
    bedrock_statements,
    ec2_network_interface_statement,
    ecr_pull_statements,
    kms_use_statement,
    log_write_statement,
    metrics_statement,
    s3_artefact_statements,
    settings_read_statements,
)

__all__ = ["SageMakerStack"]


class SageMakerStack(Stack):
    """The SageMaker execution role: what a job container may do, written as statements."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        context: AppContext,
        bucket: s3.IBucket,
        key: kms.IKey,
        repository: ecr.IRepository,
        jobs_log_group: logs.ILogGroup,
        application_secret: secretsmanager.ISecret,
        jobs_security_group: ec2.ISecurityGroup,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context
        self.jobs_security_group = jobs_security_group

        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{PRODUCT}-{context.env_name}-sagemaker-execution",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description=f"Identity a {PRODUCT} {context.env_name} training or processing job runs as",
        )

        statements: list[iam.PolicyStatement] = [
            *s3_artefact_statements(bucket.bucket_arn),
            kms_use_statement(key.key_arn),
            *settings_read_statements(
                account=self.account,
                region=self.region,
                env_name=context.env_name,
                secret_arn=application_secret.secret_arn,
            ),
            *ecr_pull_statements(repository.repository_arn),
            # SageMaker writes a job's container output to log groups it owns and creates -
            # `/aws/sagemaker/TrainingJobs` and `/aws/sagemaker/ProcessingJobs` - so unlike the
            # Fargate task this role does need `CreateLogGroup`. It is scoped to that path, so it
            # cannot create a group anywhere else and cannot touch the product's own groups except
            # to write into the one named here.
            iam.PolicyStatement(
                sid="JobLogGroups",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/sagemaker/*",
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/sagemaker/*:log-stream:*",
                ],
            ),
            log_write_statement([jobs_log_group.log_group_arn], sid="ProductJobLogs"),
            metrics_statement(),
            ec2_network_interface_statement(),
            *bedrock_statements(
                enabled=context.bedrock_enabled,
                model_ids=context.bedrock_model_ids,
                region=context.region,
            ),
        ]
        for statement in statements:
            self.execution_role.add_to_principal_policy(statement)

        CfnOutput(
            self,
            "ExecutionRoleArn",
            value=self.execution_role.role_arn,
            description="MARKETING_AI_SAGEMAKER_ROLE_ARN for this deployment",
        )
        CfnOutput(
            self,
            "JobsSecurityGroupId",
            value=jobs_security_group.security_group_id,
            description="MARKETING_AI_SAGEMAKER_SECURITY_GROUP_IDS for this deployment",
        )
