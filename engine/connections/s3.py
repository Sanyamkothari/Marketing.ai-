"""Amazon S3, and every store that speaks its protocol through an endpoint URL (Plan H M80).

One class, two catalogue cards: **Amazon S3** (the keys are optional - blank uses the AWS sign-in
already set up on the machine running Marketing AI, the same default chain the AI service uses) and
**S3-compatible storage** (an endpoint URL and keys are required): Google Cloud Storage through its
interoperability (HMAC) keys, Cloudflare R2, MinIO and the like.

Only these calls are ever made: `HeadBucket`, `ListObjectsV2`, `HeadObject`, `GetObject` (a byte range
for a preview), and - to tell whether the keys could write - `sts:GetCallerIdentity`,
`iam:SimulatePrincipalPolicy` and `GetBucketAcl`, all of which read. Nothing is ever written.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, BinaryIO, Final

from engine.connections.base import (
    CONNECT_TIMEOUT_S,
    MAX_BROWSE_ITEMS,
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_PARQUET_BYTES,
    READ_TIMEOUT_S,
    BrowseItem,
    BrowseResult,
    ConfigValue,
    ConnectorError,
    FetchResult,
    FieldSpec,
    FileFormat,
    KindInfo,
    LimitedWriter,
    Preview,
    Selection,
    StepName,
    StepStatus,
    TestReport,
    TestStep,
    bool_value,
    load_sdk,
    remaining,
    report,
    skipped_after,
    step,
    text_value,
    too_large,
)
from engine.connections.files import (
    file_format_of,
    importable,
    parent_of,
    parquet_too_big_to_preview,
    preview_bytes,
)
from engine.connections.net import check_url
from engine.utils.time import utc_now

__all__ = ["S3Connector"]

_CHUNK: Final[int] = 1 << 20
_LIST_PAGE: Final[int] = 1000

_KEYS_HELP: Final[str] = "Leave both keys blank to use the AWS sign-in already set up on this computer."

_WRITE_ACTIONS: Final[tuple[str, ...]] = ("s3:PutObject", "s3:DeleteObject")
_PUBLIC_GROUPS: Final[frozenset[str]] = frozenset(
    {
        "http://acs.amazonaws.com/groups/global/AllUsers",
        "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
    }
)


class S3Connector:
    """Amazon S3 (`kind="s3"`) or an S3-compatible store (`kind="s3_compatible"`)."""

    def __init__(self, *, compatible: bool = False) -> None:
        self.compatible = compatible
        self.kind = "s3_compatible" if compatible else "s3"
        self.label = "Other cloud storage (S3-compatible)" if compatible else "Amazon S3"

    # --- the catalogue ------------------------------------------------------------------------------
    def info(self) -> KindInfo:
        keys_required = self.compatible
        fields: list[FieldSpec] = []
        if self.compatible:
            fields.append(
                FieldSpec(
                    name="endpoint_url",
                    label="Storage address",
                    required=True,
                    placeholder="https://storage.googleapis.com",
                    help="Google Cloud Storage: https://storage.googleapis.com. Cloudflare R2: "
                    "https://<account>.r2.cloudflarestorage.com. MinIO: your server's address.",
                )
            )
        fields += [
            FieldSpec(name="bucket", label="Bucket name", required=True, placeholder="my-company-data"),
            FieldSpec(
                name="access_key_id",
                label="Access key ID",
                type="password",
                secret=True,
                required=keys_required,
                help=None if keys_required else _KEYS_HELP,
            ),
            FieldSpec(
                name="secret_access_key",
                label="Secret access key",
                type="password",
                secret=True,
                required=keys_required,
                help="Use keys that can only read. Marketing AI never changes your files.",
            ),
            FieldSpec(
                name="region",
                label="Region",
                advanced=True,
                placeholder="auto" if self.compatible else "ap-south-1",
                help="Found by itself for Amazon S3; set it only if the test asks you to.",
            ),
            FieldSpec(
                name="prefix",
                label="Start in folder",
                advanced=True,
                placeholder="exports/",
                help="Only files under this folder are shown.",
            ),
        ]
        if self.compatible:
            fields.append(
                FieldSpec(
                    name="path_style",
                    label="Use path-style addresses (MinIO)",
                    type="checkbox",
                    advanced=True,
                    default=False,
                )
            )
        return KindInfo(
            kind=self.kind,
            label=self.label,
            description=(
                "Google Cloud Storage, Cloudflare R2, MinIO and other stores that work like Amazon S3."
                if self.compatible
                else "CSV and Parquet files in an Amazon S3 bucket."
            ),
            group="store",
            tier="built_in",
            available=self.available(),
            fields=tuple(fields),
        )

    def available(self) -> bool:
        return load_sdk("boto3") is not None

    # --- clients ------------------------------------------------------------------------------------
    def _session(self, secrets: Mapping[str, str], region: str | None) -> Any:
        boto3 = load_sdk("boto3")
        if boto3 is None:  # pragma: no cover - boto3 is a runtime dependency
            raise ConnectorError("CONNECTION_NEEDS_ADDON", "The S3 add-on (boto3) is not installed.")
        key_id = secrets.get("access_key_id", "").strip()
        secret = secrets.get("secret_access_key", "").strip()
        if bool(key_id) != bool(secret):
            raise ConnectorError(
                "CONNECTION_KEYS_INCOMPLETE",
                "Only one of the two keys was given.",
                "Enter both the access key ID and the secret access key, or leave both blank.",
            )
        if self.compatible and not key_id:
            raise ConnectorError(
                "CONNECTION_KEYS_REQUIRED",
                "This storage service needs an access key ID and a secret access key.",
                "Create a key pair that can read the bucket in your storage provider's console "
                "(Google Cloud Storage calls them HMAC keys) and enter both.",
            )
        if key_id:
            return boto3.session.Session(
                aws_access_key_id=key_id, aws_secret_access_key=secret, region_name=region
            )
        return boto3.session.Session(region_name=region)

    def _botocore_config(self, config: Mapping[str, ConfigValue]) -> Any:
        from botocore.config import Config

        extra: dict[str, Any] = {}
        if self.compatible and bool_value(config, "path_style"):
            extra["s3"] = {"addressing_style": "path"}
        return Config(
            connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=READ_TIMEOUT_S,
            retries={"max_attempts": 2, "mode": "standard"},
            **extra,
        )

    def _region(self, config: Mapping[str, ConfigValue]) -> str | None:
        region = text_value(config, "region")
        if region:
            return region
        return "auto" if self.compatible else None

    def _client(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], service: str = "s3"
    ) -> Any:
        region = self._region(config)
        endpoint = text_value(config, "endpoint_url") if self.compatible else ""
        if self.compatible:
            check_url(endpoint)
        session = self._session(secrets, region)
        if service != "s3":
            return session.client(service, config=self._botocore_config(config))
        if not self.compatible and region is None:
            region = self._discover_region(session, config)
        return session.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint or None,
            config=self._botocore_config(config),
        )

    def _discover_region(self, session: Any, config: Mapping[str, ConfigValue]) -> str | None:
        """The bucket's region from `HeadBucket`'s `x-amz-bucket-region` header, which S3 sends even on
        a 301 or a 403; None when it cannot be told (the test then says what to set)."""
        from botocore.exceptions import BotoCoreError, ClientError

        probe = session.client("s3", region_name="us-east-1", config=self._botocore_config(config))
        try:
            answer = probe.head_bucket(Bucket=text_value(config, "bucket"))
            headers = answer.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        except ClientError as exc:
            headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        except BotoCoreError:
            return None
        region = headers.get("x-amz-bucket-region")
        return str(region) if region else None

    # --- test ---------------------------------------------------------------------------------------
    def test(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> TestReport:
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            ConnectTimeoutError,
            EndpointConnectionError,
            NoCredentialsError,
            PartialCredentialsError,
            ReadTimeoutError,
        )

        steps: list[TestStep] = []
        bucket = text_value(config, "bucket")
        prefix = text_value(config, "prefix")
        try:
            client = self._client(config, secrets)
        except ConnectorError as exc:
            failed_at = (
                StepName.REACH
                if exc.code.startswith("CONNECTION_ENDPOINT") or "HOST" in exc.code
                else StepName.SIGN_IN
            )
            if failed_at is StepName.SIGN_IN:
                steps.append(
                    step(StepName.REACH, StepStatus.SKIPPED, "Not tried: the sign-in details are incomplete.")
                )
            steps.append(step(failed_at, StepStatus.FAILED, exc.message, exc.fix))
            return report([*steps, *skipped_after(remaining(failed_at))], utc_now())

        # 1-2. reach and sign in: one HeadBucket answers both
        try:
            client.head_bucket(Bucket=bucket)
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError):
            steps.append(
                step(
                    StepName.REACH,
                    StepStatus.FAILED,
                    "The storage service did not answer.",
                    (
                        "Check the storage address and this computer's internet connection."
                        if self.compatible
                        else "Check this computer's internet connection, and the Region under More options."
                    ),
                )
            )
            return report([*steps, *skipped_after(remaining(StepName.REACH))], utc_now())
        except (NoCredentialsError, PartialCredentialsError):
            steps.append(step(StepName.REACH, StepStatus.OK, "The storage service answered."))
            steps.append(
                step(
                    StepName.SIGN_IN,
                    StepStatus.FAILED,
                    "No keys were entered and this computer has no AWS sign-in set up.",
                    "Enter an access key ID and secret access key that can read the bucket.",
                )
            )
            return report([*steps, *skipped_after(remaining(StepName.SIGN_IN))], utc_now())
        except ClientError as exc:
            steps.append(step(StepName.REACH, StepStatus.OK, "The storage service answered."))
            failed = self._head_failure(exc, bucket)
            steps.extend(failed)
            last = failed[-1].name
            return report([*steps, *skipped_after(remaining(last))], utc_now())
        except BotoCoreError:
            steps.append(
                step(
                    StepName.REACH,
                    StepStatus.FAILED,
                    "The storage service could not be reached with these settings.",
                    "Check the settings and try again.",
                )
            )
            return report([*steps, *skipped_after(remaining(StepName.REACH))], utc_now())
        steps.append(step(StepName.REACH, StepStatus.OK, "The storage service answered."))
        steps.append(step(StepName.SIGN_IN, StepStatus.OK, f"Signed in and opened the bucket {bucket}."))

        # 3. list
        try:
            listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=_LIST_PAGE)
        except (ClientError, BotoCoreError):
            steps.append(
                step(
                    StepName.LIST,
                    StepStatus.FAILED,
                    f"Signed in, but the files in {bucket} could not be listed.",
                    "Allow these keys to list the bucket (s3:ListBucket).",
                )
            )
            return report([*steps, *skipped_after(remaining(StepName.LIST))], utc_now())
        keys = [obj["Key"] for obj in listing.get("Contents", []) if importable(obj["Key"])]
        where = f"{bucket}/{prefix}" if prefix else bucket
        if keys:
            more = " or more" if listing.get("IsTruncated") else ""
            steps.append(
                step(
                    StepName.LIST, StepStatus.OK, f"Found {len(keys)}{more} CSV or Parquet files in {where}."
                )
            )
        else:
            steps.append(
                step(
                    StepName.LIST,
                    StepStatus.OK,
                    f"The bucket opened, but there are no CSV or Parquet files in {where} yet.",
                    "Put a CSV or Parquet export there, or change “Start in folder” under More options.",
                )
            )

        # 4. read a sample
        if keys:
            try:
                client.get_object(Bucket=bucket, Key=keys[0], Range="bytes=0-1023")["Body"].read()
                steps.append(step(StepName.READ_SAMPLE, StepStatus.OK, f"Read the start of {keys[0]}."))
            except (ClientError, BotoCoreError):
                steps.append(
                    step(
                        StepName.READ_SAMPLE,
                        StepStatus.FAILED,
                        "The files can be listed but not read.",
                        "Allow these keys to read files in the bucket (s3:GetObject).",
                    )
                )
        else:
            steps.append(step(StepName.READ_SAMPLE, StepStatus.SKIPPED, "Nothing to read yet."))

        # 5. read-only check
        steps.append(self._read_only_step(client, config, secrets, bucket))
        return report(steps, utc_now())

    def _head_failure(self, exc: Any, bucket: str) -> list[TestStep]:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        if code in {"InvalidAccessKeyId"}:
            return [
                step(
                    StepName.SIGN_IN,
                    StepStatus.FAILED,
                    "The access key ID was not recognised.",
                    "Copy the access key ID again; it may have been deleted or mistyped.",
                )
            ]
        if code in {"SignatureDoesNotMatch"}:
            return [
                step(
                    StepName.SIGN_IN,
                    StepStatus.FAILED,
                    "The secret access key does not match the access key ID.",
                    "Enter the secret access key again, exactly as it was given.",
                )
            ]
        if code in {"404", "NoSuchBucket", "NotFound"} or status == 404:
            return [
                step(StepName.SIGN_IN, StepStatus.OK, "Signed in."),
                step(
                    StepName.LIST,
                    StepStatus.FAILED,
                    f"There is no bucket called {bucket}.",
                    "Check the bucket name's spelling. Bucket names are lower case, such as my-company-data.",
                ),
            ]
        if code in {"301", "PermanentRedirect"} or status == 301:
            region = headers.get("x-amz-bucket-region")
            where = f" ({region})" if region else ""
            return [
                step(
                    StepName.SIGN_IN,
                    StepStatus.FAILED,
                    f"The bucket {bucket} is in another region{where}.",
                    (
                        f"Set Region under More options to {region}."
                        if region
                        else "Set Region under More options."
                    ),
                )
            ]
        return [
            step(
                StepName.SIGN_IN,
                StepStatus.FAILED,
                f"The keys were refused for the bucket {bucket}.",
                "Check both keys, and that they are allowed to read this bucket (s3:ListBucket and s3:GetObject).",
            )
        ]

    def _read_only_step(
        self, client: Any, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], bucket: str
    ) -> TestStep:
        verdict = None if self.compatible else self._simulate_writes(config, secrets, bucket)
        if verdict is True:
            return _can_write_warning(bucket)
        if verdict is False:
            return step(
                StepName.READ_ONLY_CHECK, StepStatus.OK, f"These keys cannot change files in {bucket}."
            )
        if self._bucket_is_public_writable(client, bucket):
            return step(
                StepName.READ_ONLY_CHECK,
                StepStatus.WARNING,
                f"Anyone on the internet can change files in {bucket}.",
                "Ask whoever owns the bucket to remove public write access. Marketing AI only reads.",
            )
        return step(
            StepName.READ_ONLY_CHECK,
            StepStatus.SKIPPED,
            "The storage service could not say whether these keys can change files. Marketing AI only reads.",
            "For safety, use keys that can only read.",
        )

    def _simulate_writes(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], bucket: str
    ) -> bool | None:
        """Ask IAM whether the caller could write or delete objects; None when IAM will not say."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            arn = str(self._client(config, secrets, "sts").get_caller_identity()["Arn"])
            source = _principal_arn(arn)
            if source is None:
                return None
            answer = self._client(config, secrets, "iam").simulate_principal_policy(
                PolicySourceArn=source,
                ActionNames=list(_WRITE_ACTIONS),
                ResourceArns=[f"arn:aws:s3:::{bucket}/*"],
            )
        except (ClientError, BotoCoreError, KeyError, NotImplementedError, ConnectorError):
            return None
        decisions = [str(r.get("EvalDecision", "")) for r in answer.get("EvaluationResults", [])]
        if not decisions:
            return None
        return any(d == "allowed" for d in decisions)

    @staticmethod
    def _bucket_is_public_writable(client: Any, bucket: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            acl = client.get_bucket_acl(Bucket=bucket)
        except (ClientError, BotoCoreError):
            return False
        for grant in acl.get("Grants", []):
            grantee = grant.get("Grantee", {})
            if grantee.get("URI") in _PUBLIC_GROUPS and grant.get("Permission") in {"WRITE", "FULL_CONTROL"}:
                return True
        return False

    # --- browse, preview, fetch --------------------------------------------------------------------
    def browse(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], path: str
    ) -> BrowseResult:
        from botocore.exceptions import BotoCoreError, ClientError

        bucket = text_value(config, "bucket")
        root = text_value(config, "prefix")
        prefix = path or root
        self._inside_root(prefix, root)
        client = self._client(config, secrets)
        items: list[BrowseItem] = []
        truncated = False
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=bucket, Prefix=prefix, Delimiter="/", PaginationConfig={"PageSize": _LIST_PAGE}
            ):
                for common in page.get("CommonPrefixes", []):
                    folder = str(common["Prefix"])
                    items.append(
                        BrowseItem(
                            name=folder[len(prefix) :].rstrip("/") or folder,
                            kind="folder",
                            path=folder,
                            importable=False,
                        )
                    )
                for obj in page.get("Contents", []):
                    key = str(obj["Key"])
                    if key.endswith("/"):
                        continue
                    items.append(
                        BrowseItem(
                            name=key[len(prefix) :],
                            kind="file",
                            path=key,
                            size_bytes=int(obj.get("Size", 0)),
                            importable=importable(key),
                        )
                    )
                if len(items) >= MAX_BROWSE_ITEMS:
                    truncated = True
                    break
        except (ClientError, BotoCoreError) as exc:
            raise _read_error(exc, bucket) from None
        parent = parent_of(prefix) if prefix and prefix != root else None
        return BrowseResult(
            path=prefix, parent=parent, items=tuple(items[:MAX_BROWSE_ITEMS]), truncated=truncated
        )

    def file_format(self, selection: Selection) -> FileFormat:
        return file_format_of(self._key(selection))

    def preview(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], selection: Selection
    ) -> Preview:
        from botocore.exceptions import BotoCoreError, ClientError

        key = self._key(selection)
        self._inside_root(key, text_value(config, "prefix"))
        file_format = file_format_of(key)
        bucket = text_value(config, "bucket")
        client = self._client(config, secrets)
        try:
            size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
            if file_format == "csv":
                end = min(size, PREVIEW_MAX_BYTES) - 1
                data = (
                    client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max(end, 0)}")["Body"].read()
                    if size
                    else b""
                )
                return preview_bytes(data, "csv", truncated=size > PREVIEW_MAX_BYTES)
            if size > PREVIEW_MAX_PARQUET_BYTES:
                raise parquet_too_big_to_preview()
            data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise _read_error(exc, bucket, key) from None
        return preview_bytes(data, "parquet", truncated=False)

    def fetch(
        self,
        config: Mapping[str, ConfigValue],
        secrets: Mapping[str, str],
        selection: Selection,
        sink: BinaryIO,
        *,
        limit_bytes: int,
        max_rows: int,  # noqa: ARG002 - a file is imported whole; ingest applies the row cap
    ) -> FetchResult:
        from botocore.exceptions import BotoCoreError, ClientError

        key = self._key(selection)
        self._inside_root(key, text_value(config, "prefix"))
        file_format = file_format_of(key)
        bucket = text_value(config, "bucket")
        client = self._client(config, secrets)
        name = key.rsplit("/", 1)[-1]
        writer = LimitedWriter(sink, limit_bytes, name)
        try:
            size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
            if size > limit_bytes:
                raise too_large(name, limit_bytes)
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            for chunk in body.iter_chunks(_CHUNK):
                writer.write(chunk)
        except (ClientError, BotoCoreError) as exc:
            raise _read_error(exc, bucket, key) from None
        return FetchResult(file_format=file_format, file_name=name, size_bytes=writer.written)

    @staticmethod
    def _key(selection: Selection) -> str:
        key = (selection.path or "").strip()
        if not key or key.endswith("/"):
            raise ConnectorError(
                "CONNECTION_PICK_A_FILE", "Pick a file first.", "Choose a CSV or Parquet file from the list."
            )
        return key

    @staticmethod
    def _inside_root(key: str, root: str) -> None:
        if root and not key.startswith(root):
            raise ConnectorError(
                "CONNECTION_OUTSIDE_FOLDER",
                "That file is outside the folder this connection is limited to.",
                "Pick a file from the list, or change “Start in folder” on the connection.",
            )


def _principal_arn(arn: str) -> str | None:
    """The IAM user or role behind an STS caller ARN (an assumed role's session is not simulated)."""
    parts = arn.split(":")
    if len(parts) < 6 or (parts[2] != "sts" and parts[2] != "iam"):
        return None
    resource = parts[5]
    if resource.startswith("assumed-role/"):
        role = resource.split("/")[1]
        return f"arn:{parts[1]}:iam::{parts[4]}:role/{role}"
    if resource.startswith(("user/", "role/")):
        return arn
    return None


def _can_write_warning(bucket: str) -> TestStep:
    return step(
        StepName.READ_ONLY_CHECK,
        StepStatus.WARNING,
        f"These keys can also change or delete files in {bucket}. Marketing AI only reads.",
        "For safety, create keys that can only read (s3:ListBucket and s3:GetObject) and use those.",
    )


def _read_error(exc: Any, bucket: str, key: str | None = None) -> ConnectorError:
    from botocore.exceptions import ClientError

    code = ""
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
    if code in {"NoSuchKey", "404", "NotFound"}:
        return ConnectorError(
            "CONNECTION_OBJECT_NOT_FOUND",
            f"The file {key} is no longer in {bucket}." if key else f"Nothing was found in {bucket}.",
            "Open the list again and pick a file that is there.",
            status=404,
        )
    if code in {"NoSuchBucket"}:
        return ConnectorError(
            "CONNECTION_BUCKET_NOT_FOUND",
            f"There is no bucket called {bucket}.",
            "Edit the connection and check the bucket name.",
            status=404,
        )
    if code in {"AccessDenied", "403", "Forbidden", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
        return ConnectorError(
            "CONNECTION_ACCESS_DENIED",
            f"The keys were refused for {bucket}.",
            "Test the connection to see which permission is missing.",
            status=409,
        )
    return ConnectorError(
        "CONNECTION_FAILED",
        f"The storage service could not be read ({bucket}).",
        "Test the connection to see what is wrong.",
        status=502,
    )
