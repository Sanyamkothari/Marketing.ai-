"""The metadata database: private, encrypted, backed up, and reachable only over TLS.

`rds.force_ssl=1` is the parameter that matters most and is the easiest to leave out. Without it a
client that forgets `sslmode=require` connects in clear text and nothing complains; with it the
server refuses, so the mistake is a connection error on day one rather than a finding in an audit.
The URL this stack composes names `sslmode=require` as well, so both ends say the same thing.

Two secrets, and the reason is worth reading before changing it.

`marketing-ai/<env>/db` is the **credential**. It is generated here, it is what RDS knows about, and
it is what the AWS single-user rotation function rewrites. Nothing but the database and the
rotation function ever reads it.

`marketing-ai/<env>/app` is the **application document** `engine/aws/secrets.py` fetches, whose keys
are `Settings` field names. It holds one key, `database_url`, composed at deploy time from the
credential secret through a CloudFormation dynamic reference.

That second secret is a seam, not a design, and it is written down as one (DEC-376): the composed
URL is a *snapshot*. The AWS rotation function changes `password` in the credential secret and
knows nothing about this document, so after the first rotation the URL carries the previous
password until someone redeploys this stack. The fix is not in this file - it is for
`engine.settings` to accept the standard RDS credential document (`username`, `password`, `host`,
`port`, `dbname`) and build the URL itself, at which point this stack keeps one secret and rotation
is end-to-end. Until then the rotation schedule is set to the longest interval that is still a
rotation, and `infra/README.md` says plainly what has to happen when it fires.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.context import AppContext
from infra.naming import PRODUCT, db_instance_identifier, secret_name
from infra.network import POSTGRES_PORT

__all__ = [
    "DATABASE_NAME",
    "DATABASE_SCHEMA",
    "DATABASE_USERNAME",
    "PASSWORD_LENGTH",
    "ROTATION_DAYS",
    "DatabaseStack",
]

DATABASE_NAME: Final[str] = "marketing_ai"
DATABASE_USERNAME: Final[str] = "marketing_ai_app"
DATABASE_SCHEMA: Final[str] = "marketing_ai"
"""The schema Alembic owns; `Settings.postgres_schema` is set to it."""

PASSWORD_LENGTH: Final[int] = 40
"""Chosen, not measured. Long enough that its alphanumeric-only alphabet costs nothing."""

ROTATION_DAYS: Final[int] = 90
"""How often the credential rotates.

Chosen, and chosen *long* on purpose: see the module docstring. Every rotation currently requires a
redeploy of this stack to recompose `marketing-ai/<env>/app`, so the interval is set to the longest
one that is still a rotation rather than to a number that sounds diligent and pages someone monthly.
Shorten it the moment `engine.settings` can read the credential document directly.
"""


class DatabaseStack(Stack):
    """One RDS for PostgreSQL instance, its credential, and the document the application reads."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        context: AppContext,
        vpc: ec2.IVpc,
        subnets: ec2.SubnetSelection,
        rotation_subnets: ec2.SubnetSelection,
        clients: Sequence[ec2.ISecurityGroup],
        key: kms.IKey,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context
        removal = RemovalPolicy.DESTROY if context.removal_policy_destroy else RemovalPolicy.RETAIN

        # Owned here, not in the network stack: `add_rotation_single_user` adds an ingress rule for
        # the rotation function, and a stack may not write into one that comes before it.
        self.security_group = ec2.SecurityGroup(
            self,
            "SecurityGroup",
            vpc=vpc,
            description=f"{PRODUCT} {context.env_name} metadata database",
            allow_all_outbound=False,
        )
        for client in clients:
            self.security_group.add_ingress_rule(
                client, ec2.Port.tcp(POSTGRES_PORT), "From an application principal"
            )

        self.credential_secret = secretsmanager.Secret(
            self,
            "Credential",
            secret_name=f"{PRODUCT}/{context.env_name}/db",
            description=f"RDS credential for {PRODUCT} {context.env_name}; rotated by AWS",
            encryption_key=key,
            removal_policy=removal,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=f'{{"username": "{DATABASE_USERNAME}"}}',
                generate_string_key="password",
                password_length=PASSWORD_LENGTH,
                # Alphanumeric only. The password ends up inside a URL, where `@`, `:`, `/`, `?`
                # and `#` are delimiters; RDS separately refuses `/`, `"`, `@` and spaces. An
                # escaping bug here is a deployment that cannot connect and a password nobody can
                # print to find out why.
                exclude_punctuation=True,
            ),
        )

        self.parameter_group = rds.ParameterGroup(
            self,
            "Parameters",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16),
            description=f"{PRODUCT} {context.env_name}: TLS required",
            parameters={
                "rds.force_ssl": "1",
                # Log every statement that changes data, so a support question about a run's
                # metadata has an answer. Not `all`: that logs every SELECT the API makes.
                "log_statement": "mod",
                "log_connections": "1",
                "log_disconnections": "1",
            },
        )

        self.instance = rds.DatabaseInstance(
            self,
            "Postgres",
            instance_identifier=db_instance_identifier(context.env_name),
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16),
            instance_type=_instance_type(context.db_instance_class),
            vpc=vpc,
            vpc_subnets=subnets,
            security_groups=[self.security_group],
            publicly_accessible=False,
            # Not PostgreSQL's default 5432. This is the cheapest kind of hardening and not much of
            # one - the instance is in an isolated subnet whose security group admits two named
            # security groups and nothing else, so nothing is scanning it either way. It is here
            # because it costs nothing, because AwsSolutions-RDS11 asks for it, and because the
            # port never has to be typed by hand: the application reads it from the composed URL
            # below, which is built from the instance's own endpoint attribute.
            port=POSTGRES_PORT,
            credentials=rds.Credentials.from_secret(self.credential_secret),
            database_name=DATABASE_NAME,
            parameter_group=self.parameter_group,
            allocated_storage=context.db_storage_gb,
            max_allocated_storage=context.db_storage_gb * 4,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            storage_encryption_key=key,
            multi_az=context.db_multi_az,
            deletion_protection=context.db_deletion_protection,
            removal_policy=removal,
            backup_retention=Duration.days(context.db_backup_retention_days),
            delete_automated_backups=context.removal_policy_destroy,
            copy_tags_to_snapshot=True,
            auto_minor_version_upgrade=True,
            # The log group this exports into is created, with a retention, by the observability
            # stack - which is deployed first for exactly that reason. No `cloudwatch_logs_retention`
            # here: that property makes CDK add a custom-resource Lambda holding
            # `logs:PutRetentionPolicy` on every log group in the account.
            cloudwatch_logs_exports=["postgresql"],
        )

        self.instance.add_rotation_single_user(
            automatically_after=Duration.days(ROTATION_DAYS),
            exclude_characters=_PUNCTUATION,
            # The rotation function calls Secrets Manager and then the database. The isolated
            # database subnets can reach neither, so it goes in the application subnets - which
            # have a NAT gateway, or the `secretsmanager` interface endpoint that
            # `AppContext.validate` insists on when there is none.
            vpc_subnets=rotation_subnets,
        )

        # `unsafe_unwrap` is the right call and the name is a warning worth reading: it puts the
        # literal `{{resolve:secretsmanager:...}}` dynamic reference into the template instead of a
        # value, so the password is resolved by CloudFormation at deploy time and never appears in
        # `cdk synth` output, in the CloudFormation console, or in a pipeline log. What it does not
        # do is re-resolve after a rotation - see the module docstring.
        password = self.credential_secret.secret_value_from_json("password").unsafe_unwrap()
        url = (
            f"postgresql://{DATABASE_USERNAME}:{password}"
            f"@{self.instance.db_instance_endpoint_address}:{self.instance.db_instance_endpoint_port}"
            f"/{DATABASE_NAME}?sslmode=require"
        )
        self.app_secret = secretsmanager.Secret(
            self,
            "ApplicationSecret",
            secret_name=secret_name(context.env_name),
            description=(
                "The document engine/aws/secrets.py reads; its keys are Settings field names. "
                "Recomposed by a deploy of this stack after the credential rotates."
            ),
            encryption_key=key,
            removal_policy=removal,
            secret_object_value={"database_url": SecretValue.unsafe_plain_text(url)},
        )

        CfnOutput(
            self,
            "EndpointAddress",
            value=self.instance.db_instance_endpoint_address,
            description="Private endpoint; reachable from the app subnets only",
        )
        CfnOutput(
            self,
            "ApplicationSecretArn",
            value=self.app_secret.secret_arn,
            description="What the task role may read; the task never sees the credential secret",
        )


_PUNCTUATION: Final[str] = " %+~`#$&*()|[]{}:;<>?!'/\"@\\,=^"
"""Characters the rotated password may not contain, for the reason the generator excludes them."""


def _instance_type(db_instance_class: str) -> ec2.InstanceType:
    """`db.t4g.small` -> an `InstanceType`.

    RDS spells its sizes with a `db.` prefix and CDK's `InstanceType` does not, so the context key
    keeps the spelling an operator reads in the RDS console and the prefix is stripped here.
    """
    name = db_instance_class.strip()
    if not name.startswith("db."):
        raise ValueError(
            f"db_instance_class={db_instance_class!r}: an RDS instance class starts with 'db.', "
            "as it is written in the RDS console and the pricing pages."
        )
    return ec2.InstanceType(name[len("db.") :])
