"""Make a freshly deployed account ready to serve, and say plainly what it still cannot do.

`cdk deploy` creates the bucket, the database and the service. It does not create the schema, and it
cannot tell you whether the task role can actually do the things its policy appears to allow - an
IAM policy is a document, not a demonstration. This script closes both gaps:

  1. it probes every permission the running product needs, one call each, and reports each one as a
     line naming the IAM action that would fix it;
  2. it applies the Alembic migrations, so the metadata tables exist;
  3. it checks that the configuration the deployment will serve actually loads.

Every probe is read-only or cleans up after itself, so it is safe to run against a live deployment,
and `--dry-run` runs the whole thing against nothing at all. It exits non-zero when any probe fails,
and it names what failed rather than stopping at the first one: an operator wants the whole list,
not a sequence of one-at-a-time fixes.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from engine.config import list_use_case_ids, load_use_case
from engine.settings import ENV_VARS, Settings, SettingsError, load_settings, summary

COMMAND: Final[str] = "python -m scripts.aws_bootstrap"
PROBE_KEY: Final[str] = "_bootstrap/permission-probe.txt"
"""Written and deleted by the storage probe. Under its own prefix so it cannot collide with a run."""

OK: Final[str] = "ok"
FAILED: Final[str] = "FAILED"
SKIPPED: Final[str] = "skipped"


@dataclass(frozen=True, slots=True)
class Check:
    """One thing that was tried, and what happened."""

    name: str
    status: str
    detail: str
    remedy: str = ""

    def line(self) -> str:
        """The single line an operator reads."""
        rendered = f"  [{self.status:>7}] {self.name}: {self.detail}"
        return (
            f"{rendered}\n            fix: {self.remedy}"
            if self.status == FAILED and self.remedy
            else rendered
        )


def _probe(name: str, remedy: str, body: Callable[[], str]) -> Check:
    """Run one probe, turning any exception into a FAILED line that names the action to grant.

    The exception's *class* is reported and its message is not, for the same reason
    `engine/utils/logging.py` withholds one: an AWS error message routinely quotes the resource, the
    principal and sometimes the value that upset it (plan section 13.7).
    """
    try:
        return Check(name, OK, body(), remedy)
    except Exception as exc:  # every probe failure is a finding, not a crash
        return Check(name, FAILED, type(exc).__name__, remedy)


def check_configuration(settings: Settings) -> list[Check]:
    """The configuration the deployment will serve loads, and names at least one use case."""

    def body() -> str:
        ids = list_use_case_ids(settings.config_dir)
        for use_case_id in ids:
            load_use_case(use_case_id, settings.config_dir)
        return f"{len(ids)} use cases load"

    return [_probe("configuration", "check MARKETING_AI_CONFIG_DIR and that configs/ is in the image", body)]


def check_storage(settings: Settings) -> list[Check]:
    """The task role can write, read and delete an object under the product's prefix."""
    if settings.storage_backend != "s3":
        return [Check("storage", SKIPPED, f"storage_backend={settings.storage_backend}")]

    def body() -> str:
        from engine.settings import build_storage

        storage = build_storage(settings)
        storage.write_bytes(PROBE_KEY, b"bootstrap\n")
        read = storage.read_bytes(PROBE_KEY)
        storage.delete(PROBE_KEY)
        if read != b"bootstrap\n":
            raise RuntimeError("the object read back did not match what was written")
        return f"wrote, read and deleted s3://{settings.s3_bucket}/{PROBE_KEY}"

    return [
        _probe(
            "storage",
            "grant s3:PutObject, s3:GetObject, s3:DeleteObject and s3:PutObjectTagging on the "
            "bucket's contents, s3:ListBucket on the bucket, and kms:GenerateDataKey/Decrypt on "
            "the key when s3_kms_key_id is set",
            body,
        )
    ]


def check_metadata(settings: Settings, *, migrate: bool) -> list[Check]:
    """The database is reachable, and the schema is at head."""
    if settings.metadata_backend != "postgres":
        return [Check("metadata", SKIPPED, f"metadata_backend={settings.metadata_backend}")]
    url = settings.postgres_dsn.get_secret_value() if settings.postgres_dsn else ""

    def connect() -> str:
        from sqlalchemy import create_engine, text

        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            version = connection.execute(text("select version()")).scalar_one()
        engine.dispose()
        return str(version).split(" on ")[0]

    checks = [
        _probe(
            "database",
            "check the security group, that the secret holds the current password, and that the "
            "URL uses TLS (the parameter group sets rds.force_ssl=1)",
            connect,
        )
    ]
    if not migrate:
        checks.append(Check("migrations", SKIPPED, "--no-migrate"))
        return checks

    def upgrade() -> str:
        from alembic import command
        from alembic.config import Config

        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        return "schema at head"

    checks.append(_probe("migrations", "run `make migrate` by hand and read the traceback", upgrade))
    return checks


def check_jobs(settings: Settings) -> list[Check]:
    """The task role may describe a SageMaker job.

    The probe asks about a job name that does not exist. A role that is allowed to look gets
    `ValidationException` - "no such job" - which is a pass; a role that is not gets
    `AccessDeniedException`, which is a failure. Describing a job that does not exist is the only
    way to test the permission without creating one and paying for it.
    """
    if settings.job_backend != "sagemaker":
        return [Check("jobs", SKIPPED, f"job_backend={settings.job_backend}")]

    def body() -> str:
        import boto3
        from botocore.exceptions import ClientError

        client = boto3.client("sagemaker", region_name=settings.aws_region)
        name = f"{settings.sagemaker_job_name_prefix}-bootstrap-probe-does-not-exist"
        try:
            client.describe_training_job(TrainingJobName=name)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"ValidationException", "ResourceNotFound", "ResourceNotFoundException"}:
                return "may describe training jobs"
            raise
        return "may describe training jobs"

    return [
        _probe(
            "jobs",
            "grant sagemaker:CreateTrainingJob, CreateProcessingJob, DescribeTrainingJob, "
            "DescribeProcessingJob, StopTrainingJob, StopProcessingJob and AddTags on "
            f"arn:aws:sagemaker:*:*:*/{settings.sagemaker_job_name_prefix}-*, plus iam:PassRole on "
            "the execution role",
            body,
        )
    ]


def run(settings: Settings, *, migrate: bool = True) -> tuple[list[Check], int]:
    """Every check, and the exit code: 0 when nothing failed."""
    checks = [
        *check_configuration(settings),
        *check_storage(settings),
        *check_metadata(settings, migrate=migrate),
        *check_jobs(settings),
    ]
    return checks, int(any(check.status == FAILED for check in checks))


def report(settings: Settings, checks: Sequence[Check]) -> str:
    """The whole report, including the settings summary - which never renders a secret."""
    lines = [f"{COMMAND}: {summary(settings)}", ""]
    lines.extend(check.line() for check in checks)
    failed = [check.name for check in checks if check.status == FAILED]
    lines.append("")
    lines.append(
        f"{len(checks) - len(failed)}/{len(checks)} checks passed"
        + (f"; still to fix: {', '.join(failed)}" if failed else "; this deployment is ready")
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """`--env`, `--no-migrate` and `--dry-run`; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog=COMMAND, description="Prepare a deployment and probe its permissions."
    )
    parser.add_argument("--env", default=None, help="deployment name; defaults to MARKETING_AI_ENV")
    parser.add_argument("--no-migrate", action="store_true", help="probe only; do not touch the schema")
    parser.add_argument("--dry-run", action="store_true", help="print what would be checked and stop")
    args = parser.parse_args(argv)
    # `--env` is layered over the process environment rather than set on the built object: the
    # loader has to see it (it picks the SSM prefix and the secret name from it), and a
    # `model_copy` afterwards would skip the validators that refuse an incoherent deployment.
    environ: Mapping[str, str] = {**os.environ, ENV_VARS["env"]: args.env} if args.env else os.environ
    try:
        settings = load_settings(environ)
    except SettingsError as exc:
        print(f"{COMMAND}: {exc.message}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"{COMMAND}: would check configuration, storage, metadata and jobs for {summary(settings)}")
        return 0
    checks, code = run(settings, migrate=not args.no_migrate)
    print(report(settings, checks), file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
