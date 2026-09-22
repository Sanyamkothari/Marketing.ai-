"""The VPC, and the decision that actually costs money: interface endpoints or a NAT gateway.

Everything that runs here runs in private subnets. That leaves one question - how a private subnet
reaches the AWS APIs it cannot work without - and it has exactly two answers, which are priced
differently and fail differently:

* **A NAT gateway** routes every private subnet to the internet. One knob, everything works,
  including calls to services nobody thought about.
* **Interface endpoints** put a private ENI for one named service in each subnet. Nothing else is
  reachable, which is a security property and a trap: the failure is not "slower", it is a task that
  stops before it writes a log line.

So it is a parameter, `nat_gateways` and `vpc_endpoints`, with a guard in `AppContext.validate`
that refuses the combination where a Fargate task could not pull its image, start its log driver or
read its settings (DEC-372). The guard is in the context object rather than here because it has to
fire before any construct is created, so the operator gets a sentence instead of a stack trace.

Neither price is written down in this repository, here or in the README: interface endpoints are
billed per endpoint, per Availability Zone, per hour, plus per GB processed, and a NAT gateway is
billed per hour plus per GB processed. The *shape* of the charge is the part worth knowing when
choosing; the rate is AWS's to publish and this repository has not measured it (plan section 13.3).

The security groups live here, all but one: the database's own group is created in
`infra/database.py`, because attaching a rotation function to the database adds a rule to it and a
stack may only be written to by stacks that come after it in the chain.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.context import AppContext
from infra.naming import PRODUCT, log_retention

__all__ = ["APP_SUBNET_GROUP", "DB_SUBNET_GROUP", "KNOWN_INTERFACE_ENDPOINTS", "NetworkStack"]

KNOWN_INTERFACE_ENDPOINTS: Final[frozenset[str]] = frozenset(
    {
        "ecr.api",
        "ecr.dkr",
        "logs",
        "monitoring",
        "kms",
        "secretsmanager",
        "ssm",
        "sagemaker.api",
        "sagemaker.runtime",
        "sts",
        "bedrock-runtime",
    }
)
"""Interface endpoints `-c vpc_endpoints=` may name.

A closed list, for the same reason `AppContext` refuses an unknown context key: `-c
vpc_endpoints=ecr.dkr,secrets-manager` would otherwise synthesise an endpoint for a service that
does not exist under that name and fail at deploy time, half an hour later.
"""

API_PORT: Final[int] = 8000
"""The port the container listens on; `Dockerfile` EXPOSEs it and `make run` uses it."""

POSTGRES_PORT: Final[int] = 5433
"""The port the metadata database listens on. Chosen, not measured, and deliberately not 5432.

One constant, read twice: `infra/database.py` passes it to the instance and uses it for the client
ingress rule, so the two can never drift. Nobody ever types it - the application is handed a URL
composed from the instance's own endpoint attributes - which is what makes moving off the default
free. It is worth very little on its own (the instance is in an isolated subnet reachable from two
named security groups), and it is what AwsSolutions-RDS11 asks for.
"""

APP_SUBNET_GROUP: Final[str] = "app"
DB_SUBNET_GROUP: Final[str] = "db"
"""Subnet group names. Named rather than typed, because with `nat_gateways=0` the application
subnets change type - isolated instead of "private with egress" - and a selection by type would
quietly pick a different set of subnets in the two configurations."""


class NetworkStack(Stack):
    """The VPC, its endpoints, its flow logs, and every security group in the deployment."""

    def __init__(self, scope: Construct, construct_id: str, *, context: AppContext, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context

        unknown = sorted(set(context.vpc_endpoints) - KNOWN_INTERFACE_ENDPOINTS)
        if unknown:
            raise ValueError(
                f"vpc_endpoints names {', '.join(unknown)}, which this application does not know. "
                f"Known: {', '.join(sorted(KNOWN_INTERFACE_ENDPOINTS))}."
            )

        app_subnet_type = (
            ec2.SubnetType.PRIVATE_WITH_EGRESS
            if context.private_subnets_have_egress
            else ec2.SubnetType.PRIVATE_ISOLATED
        )
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{PRODUCT}-{context.env_name}",
            ip_addresses=ec2.IpAddresses.cidr("10.60.0.0/16"),
            # Two, because every stateful thing below is two-AZ: the database's standby goes in the
            # second one and the load balancer needs a subnet in each. A third would add a third
            # NAT gateway or a third endpoint ENI per service for no third copy of anything.
            max_azs=2,
            nat_gateways=context.nat_gateways,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                # /22 because every Fargate task and every VPC-attached SageMaker job takes an ENI,
                # and a training job takes one per instance.
                ec2.SubnetConfiguration(name=APP_SUBNET_GROUP, subnet_type=app_subnet_type, cidr_mask=22),
                ec2.SubnetConfiguration(
                    name=DB_SUBNET_GROUP, subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )

        self.flow_log_group = logs.LogGroup(
            self,
            "FlowLogs",
            log_group_name=f"/{PRODUCT}/{context.env_name}/vpc-flow-logs",
            retention=log_retention(context.log_retention_days),
            removal_policy=RemovalPolicy.DESTROY if context.removal_policy_destroy else RemovalPolicy.RETAIN,
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(self.flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        self.app_subnets = ec2.SubnetSelection(subnet_group_name=APP_SUBNET_GROUP)
        self.db_subnets = ec2.SubnetSelection(subnet_group_name=DB_SUBNET_GROUP)

        # Security groups ----------------------------------------------------
        self.alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self.vpc,
            description="Public entry point for the marketing-ai API",
            allow_all_outbound=False,
        )
        self.service_security_group = ec2.SecurityGroup(
            self,
            "ServiceSecurityGroup",
            vpc=self.vpc,
            description="The Fargate tasks running the marketing-ai API",
            allow_all_outbound=True,
        )
        self.jobs_security_group = ec2.SecurityGroup(
            self,
            "JobsSecurityGroup",
            vpc=self.vpc,
            description="SageMaker training and processing jobs attached to this VPC",
            allow_all_outbound=True,
        )
        # Exactly the port the listener listens on, and no other. Opening 443 on a load balancer
        # that has no certificate would look like TLS in a console screenshot and answer nothing.
        if context.https_only:
            self.alb_security_group.add_ingress_rule(
                ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS from the internet"
            )
        else:
            self.alb_security_group.add_ingress_rule(
                ec2.Peer.any_ipv4(),
                ec2.Port.tcp(80),
                "PLAIN HTTP from the internet - dev only, unencrypted; see infra/README.md",
            )
        self.alb_security_group.add_egress_rule(
            self.service_security_group,
            ec2.Port.tcp(API_PORT),
            "To the tasks",
        )
        self.service_security_group.add_ingress_rule(
            self.alb_security_group,
            ec2.Port.tcp(API_PORT),
            "From the load balancer only",
        )
        # The database's own security group is NOT here, and that is a dependency-direction
        # decision rather than an oversight. `addRotationSingleUser` makes the database stack add an
        # ingress rule for the rotation function, so whichever stack owns that group is written to
        # by the database stack - and if it were this one, the arrow would point backwards and
        # CloudFormation would refuse the whole app with a dependency cycle. The group lives in
        # `infra/database.py`, next to the thing it protects, and the rules referring to the groups
        # above point forwards.

        # Endpoints ----------------------------------------------------------
        # The S3 gateway endpoint is always created. It is a route-table entry rather than an ENI,
        # so it is neither a per-AZ resource nor priced like one, and every artefact this product
        # reads or writes goes through it - including the image layer blobs an ECR pull fetches.
        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        self.endpoint_security_group = ec2.SecurityGroup(
            self,
            "EndpointSecurityGroup",
            vpc=self.vpc,
            description="Interface endpoint ENIs; HTTPS from inside the VPC only",
            allow_all_outbound=False,
        )
        self.endpoint_security_group.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from inside the VPC",
        )
        self.interface_endpoints: dict[str, ec2.InterfaceVpcEndpoint] = {}
        for name in sorted(set(context.vpc_endpoints)):
            self.interface_endpoints[name] = self.vpc.add_interface_endpoint(
                f"Endpoint{_construct_id(name)}",
                service=ec2.InterfaceVpcEndpointAwsService(name),
                subnets=self.app_subnets,
                security_groups=[self.endpoint_security_group],
                private_dns_enabled=True,
            )

        CfnOutput(self, "VpcId", value=self.vpc.vpc_id, description="The deployment's VPC")
        CfnOutput(
            self,
            "EgressModel",
            value=(
                f"nat_gateways={context.nat_gateways}; "
                f"interface_endpoints={','.join(sorted(context.vpc_endpoints)) or 'none'}"
            ),
            description="How private subnets reach AWS APIs; see infra/README.md for the cost shape",
        )


def _construct_id(endpoint_name: str) -> str:
    """`ecr.api` -> `EcrApi`; a construct id may not contain a dot."""
    return "".join(part.capitalize() for part in endpoint_name.replace("-", ".").split("."))
