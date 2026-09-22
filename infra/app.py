"""The CDK application: seven stacks, one direction, one context object.

`cdk synth` runs this file from inside `infra/`, so the repository root is put on `sys.path` before
anything is imported - that is what lets `infra.network` and `tests/infra/` name the same modules.

The dependency chain is a straight line and is declared, not inferred:

    network -> storage -> observability -> database -> sagemaker -> compute -> budgets

Two of those edges exist for reasons that are not visible in the resources:

* **observability before database.** RDS creates `/aws/rds/instance/<id>/postgresql` itself, with no
  retention, the first time it exports a log. Whoever creates that group first decides how long the
  logs are kept, so the observability stack has to win the race - which is a deployment *order*,
  and the only way to state a deployment order is a stack dependency.
* **sagemaker before compute.** The task role's `iam:PassRole` names the execution role, so the
  role has to exist first. The alternative - one stack holding both - would mean redeploying the
  load balancer to change a job's permissions.

Nothing points backwards. A cycle between CloudFormation stacks is not a slow deployment, it is a
deployment that cannot be performed at all, and the way to never have one is for the arrows to have
a single direction that somebody can read off a page.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    # `cdk synth` runs `python3 app.py` with `infra/` as the working directory, so the repository
    # root - the only place `infra` is importable as a package from - is not on the path yet.
    sys.path.insert(0, str(_REPO_ROOT))

import os  # noqa: E402
from typing import Final  # noqa: E402

import aws_cdk as cdk  # noqa: E402
from cdk_nag import AwsSolutionsChecks  # noqa: E402

from infra.budgets import BudgetsStack  # noqa: E402
from infra.compute import ComputeStack  # noqa: E402
from infra.context import AppContext, context_from_environment  # noqa: E402
from infra.database import DatabaseStack  # noqa: E402
from infra.nag_suppressions import apply_suppressions  # noqa: E402
from infra.naming import stack_name  # noqa: E402
from infra.network import NetworkStack  # noqa: E402
from infra.observability import ObservabilityStack  # noqa: E402
from infra.sagemaker import SageMakerStack  # noqa: E402
from infra.storage import StorageStack  # noqa: E402

__all__ = ["FEATURE_FLAGS", "STACK_ORDER", "Deployment", "build_app", "main"]

FEATURE_FLAGS: Final[dict[str, object]] = {
    # Deliver server access logs by bucket policy rather than by ACL. With
    # `BUCKET_OWNER_ENFORCED` object ownership - which is what turns ACLs off, and is the setting
    # AWS now recommends - the ACL route is not available at all, and CDK raises
    # "objectOwnership must be set to ObjectWriter when accessControl is LogDeliveryWrite".
    "@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy": True,
    # Only ever deploy into the commercial partition; this keeps synthesised ARNs literal instead
    # of `Fn::Sub`-ing `${AWS::Partition}` into every one of them, which makes the IAM assertions
    # in tests/infra/ read like the policies they are checking.
    "@aws-cdk/core:target-partitions": ["aws"],
}
"""Feature flags this application depends on, passed to `App` so that a synth by the CDK CLI and a
synth inside a test see exactly the same behaviour. `infra/cdk.json` carries no flags of its own:
one list, in code, that a test also exercises."""

STACK_ORDER: Final = (
    "network",
    "storage",
    "observability",
    "database",
    "sagemaker",
    "compute",
    "budgets",
)
"""The chain, in deployment order. `tests/infra/test_app.py` asserts the graph matches it."""


class Deployment:
    """The seven stacks of one deployment, so a test can reach any of them by name."""

    def __init__(self, app: cdk.App, context: AppContext, environment: cdk.Environment) -> None:
        self.app = app
        self.context = context

        def name(component: str) -> str:
            return stack_name(context.env_name, component)

        self.network = NetworkStack(app, name("network"), context=context, env=environment)
        self.storage = StorageStack(app, name("storage"), context=context, env=environment)
        self.observability = ObservabilityStack(
            app, name("observability"), context=context, key=self.storage.key, env=environment
        )
        self.database = DatabaseStack(
            app,
            name("database"),
            context=context,
            vpc=self.network.vpc,
            subnets=self.network.db_subnets,
            rotation_subnets=self.network.app_subnets,
            clients=[self.network.service_security_group, self.network.jobs_security_group],
            key=self.storage.key,
            env=environment,
        )
        self.sagemaker = SageMakerStack(
            app,
            name("sagemaker"),
            context=context,
            bucket=self.storage.bucket,
            key=self.storage.key,
            repository=self.storage.repository,
            jobs_log_group=self.observability.jobs_log_group,
            application_secret=self.database.app_secret,
            jobs_security_group=self.network.jobs_security_group,
            env=environment,
        )
        self.compute = ComputeStack(
            app,
            name("compute"),
            context=context,
            vpc=self.network.vpc,
            app_subnets=self.network.app_subnets,
            service_security_group=self.network.service_security_group,
            alb_security_group=self.network.alb_security_group,
            jobs_security_group=self.network.jobs_security_group,
            bucket=self.storage.bucket,
            log_bucket=self.storage.log_bucket,
            key=self.storage.key,
            repository=self.storage.repository,
            api_log_group=self.observability.api_log_group,
            application_secret=self.database.app_secret,
            sagemaker_role=self.sagemaker.execution_role,
            alarm_topic_arn=self.observability.alarm_topic.topic_arn,
            env=environment,
        )
        self.budgets = BudgetsStack(app, name("budgets"), context=context, env=environment)

        previous: str | None = None
        for component in STACK_ORDER:
            if previous is not None:
                self.stack(component).add_stack_dependency(
                    self.stack(previous), f"{component} is deployed after {previous}"
                )
            previous = component

    def stack(self, component: str) -> cdk.Stack:
        """One stack by its component name; the names are `STACK_ORDER`."""
        stack = getattr(self, component)
        if not isinstance(stack, cdk.Stack):  # pragma: no cover - a typo in STACK_ORDER
            raise KeyError(component)
        return stack

    def stacks(self) -> list[cdk.Stack]:
        """Every stack, in deployment order."""
        return [self.stack(component) for component in STACK_ORDER]


def build_app(
    context: AppContext,
    *,
    account: str | None = None,
    outdir: str | None = None,
) -> Deployment:
    """The whole application for one deployment.

    `account` is explicit so a test can synthesise a concrete environment offline; a real synthesis
    leaves it to `CDK_DEFAULT_ACCOUNT`, and an environment-agnostic stack renders the account as
    `Ref: AWS::AccountId`, which is correct and deploys anywhere.
    """
    app = cdk.App(outdir=outdir, context=dict(FEATURE_FLAGS))
    environment = cdk.Environment(
        account=account or os.environ.get("CDK_DEFAULT_ACCOUNT"), region=context.region
    )
    deployment = Deployment(app, context, environment)

    for tag, value in context.tags().items():
        cdk.Tags.of(app).add(tag, value)

    if context.cdk_nag:
        cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
        apply_suppressions(deployment)
    return deployment


def main() -> None:
    """`cdk synth` entry point."""
    context = context_from_environment()
    build_app(context).app.synth()


if __name__ == "__main__":
    main()
