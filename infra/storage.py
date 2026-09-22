"""The bucket every artefact lives in, the key it is encrypted with, and the image repository.

Three resources, and one bucket policy that is easy to get subtly wrong.

The obvious way to force encryption is to deny any `PutObject` that does not carry
`x-amz-server-side-encryption: aws:kms`. That is the snippet in every blog post, and it would break
this product. `S3Storage` sets the encryption headers **only when `s3_kms_key_id` is configured**;
with it unset - which is a perfectly valid deployment, and is what a local-to-S3 migration looks
like on its first day - every `PutObject` is header-less and would be refused. Worse, the refusal
would arrive as `AccessDenied` on a write, which reads like a broken role.

A header-less PUT is not unencrypted. S3 applies the bucket's default encryption, which is
SSE-KMS with this key, and the object lands encrypted exactly as intended. So the policy denies the
PUT that carries the *wrong* answer, not the one that declines to answer: `StringNotEquals` guarded
by `Null: false`, so the condition can only match a request that actually set the header (DEC-373).
The same shape is applied to the key id, so a caller cannot name a different KMS key.

`enforce_ssl` adds the other deny - `aws:SecureTransport: false` - and `minimum_tls_version` raises
the floor to 1.2. Neither is a substitute for the other: one refuses plaintext HTTP, the other
refuses an obsolete cipher suite over HTTPS.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.context import AppContext
from infra.naming import PRODUCT

__all__ = ["ABORT_INCOMPLETE_UPLOAD_DAYS", "StorageStack"]

ABORT_INCOMPLETE_UPLOAD_DAYS: Final[int] = 7
"""How long a half-finished multipart upload may sit before S3 abandons it.

Chosen, not measured. A predictor directory is large enough that boto3 uploads it in parts, and a
process killed between the first part and the completion leaves parts that are stored, billed and
invisible in the console's object list. Seven days is long enough that a retry of a genuinely slow
upload is never cut off and short enough that the bill notices; no run in this product takes days.
"""

TLS_VERSION_FLOOR: Final[float] = 1.2
"""The oldest TLS version the bucket answers. 1.0 and 1.1 are deprecated by the IETF (RFC 8996)."""

IMAGE_TAG_HISTORY: Final[int] = 20
"""How many untagged image versions ECR keeps. Chosen: enough to roll back a few deployments."""


class StorageStack(Stack):
    """The artefact bucket, its customer-managed key, the access-log bucket and the ECR repository."""

    def __init__(self, scope: Construct, construct_id: str, *, context: AppContext, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context
        removal = RemovalPolicy.DESTROY if context.removal_policy_destroy else RemovalPolicy.RETAIN

        self.key = kms.Key(
            self,
            "ArtefactKey",
            alias=context.kms_key_alias,
            description=f"SSE-KMS for the {PRODUCT} {context.env_name} artefact bucket and database",
            enable_key_rotation=True,
            removal_policy=removal,
            # A deleted key is an unreadable bucket, so even in dev the window is the maximum AWS
            # offers rather than the minimum: the mistake this protects against is noticing late.
            pending_window=Duration.days(30) if context.removal_policy_destroy else None,
        )

        # SSE-S3, not this key. A log-delivery bucket is written by an AWS service principal, and
        # giving that principal a grant on the product's key widens the key's blast radius to reach
        # something that holds no customer data. It also keeps ALB access logging working, which
        # requires SSE-S3 on the target bucket.
        self.log_bucket = s3.Bucket(
            self,
            "AccessLogs",
            bucket_name=f"{context.bucket_name_prefix}-{context.env_name}-{self.account}-logs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=TLS_VERSION_FLOOR,
            versioned=False,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal,
            # Never `auto_delete_objects`: it is a custom-resource Lambda with a standing grant to
            # empty this bucket, living in the account for the lifetime of the deployment so that a
            # teardown needs one fewer command. A non-empty bucket failing to delete is the correct
            # outcome for a store holding a customer's data.
            auto_delete_objects=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-access-logs",
                    enabled=True,
                    expiration=Duration.days(context.log_retention_days),
                )
            ],
        )

        self.bucket = s3.Bucket(
            self,
            "Artefacts",
            bucket_name=f"{context.bucket_name_prefix}-{context.env_name}-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            # One data key per request would be one KMS call per artefact, and a run writes
            # nineteen. The bucket key makes it one per bucket per short interval, with the same
            # customer key doing the encryption.
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=TLS_VERSION_FLOOR,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal,
            auto_delete_objects=False,
            server_access_logs_bucket=self.log_bucket,
            server_access_logs_prefix="s3-access/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-multipart-uploads",
                    enabled=True,
                    abort_incomplete_multipart_upload_after=Duration.days(ABORT_INCOMPLETE_UPLOAD_DAYS),
                ),
            ],
        )
        self._deny_wrong_encryption_headers()

        self.repository = ecr.Repository(
            self,
            "Images",
            repository_name=PRODUCT,
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.KMS,
            encryption_key=self.key,
            # RETAIN even in dev, and deliberately not `empty_on_delete`. The CI pushes an image
            # into this repository *before* `cdk deploy` runs, so a destroy that took the repository
            # with it would break the next deployment of the same environment. `empty_on_delete`
            # would also bring a custom-resource Lambda holding broad ECR permissions, which is a
            # standing grant bought to make one manual teardown tidier.
            removal_policy=RemovalPolicy.RETAIN,
            empty_on_delete=False,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    rule_priority=1,
                    description="Keep a rollback window of untagged images",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_count=IMAGE_TAG_HISTORY,
                )
            ],
        )

        CfnOutput(self, "BucketName", value=self.bucket.bucket_name, description="Artefact bucket")
        CfnOutput(self, "KeyArn", value=self.key.key_arn, description="Customer-managed key")
        CfnOutput(
            self,
            "RepositoryUri",
            value=self.repository.repository_uri,
            description="ECR_REGISTRY/ECR_REPOSITORY for scripts/build_push_image.sh",
        )

    def _deny_wrong_encryption_headers(self) -> None:
        """Refuse a PUT that names the wrong encryption, and let a silent one take the default.

        Two statements, each with two conditions that must both hold:

        * `Null: {"<header>": "false"}` - the header **is** present. Without this the statement
          would match a header-less PUT, which is the one this product actually makes when
          `s3_kms_key_id` is unset.
        * `StringNotEquals` - and it says something other than what this bucket is.

        Together: "if you are going to tell me how to encrypt this, tell me the truth."
        """
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyIncorrectEncryptionHeader",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[self.bucket.arn_for_objects("*")],
                conditions={
                    "Null": {"s3:x-amz-server-side-encryption": "false"},
                    "StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"},
                },
            )
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyIncorrectKmsKey",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[self.bucket.arn_for_objects("*")],
                conditions={
                    "Null": {"s3:x-amz-server-side-encryption-aws-kms-key-id": "false"},
                    "StringNotEquals": {"s3:x-amz-server-side-encryption-aws-kms-key-id": self.key.key_arn},
                },
            )
        )
