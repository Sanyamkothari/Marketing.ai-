"""`scripts/aws_bootstrap.py`: the three defects M50's walk of the deployment guide found in it.

`make aws-bootstrap` is the step of `docs/AWS_DEPLOYMENT.md` that says a deployment is ready, so a
defect in it is a deployment declared ready that is not. Walking the guide as a first-time operator
would, with no account behind it, found three:

* run as the guide said (`--env dev`, no `MARKETING_AI_SETTINGS_SOURCE=aws`), every AWS probe was
  skipped and the summary still read "4/4 checks passed; this deployment is ready";
* the database probe built its engine from the raw composed URL, which SQLAlchemy reads as psycopg
  2 - not installed - so it could never pass on a real deployment;
* the migration step dropped `postgres_schema`, putting the tables somewhere the image's own
  `migrate` entrypoint would not look.

Plus one crash: Parameter Store refusing (no credentials) escaped as a traceback rather than the
one-line refusal every other settings problem gets. Nothing here reaches AWS: the settings are built
from a mapping, the database is SQLite, and the one Postgres path is checked by what it would
connect with, not by connecting.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import pytest

from engine.aws.secrets import StaticParameterSource
from engine.settings import Settings
from scripts import aws_bootstrap
from scripts.aws_bootstrap import FAILED, OK, SKIPPED, Check, alembic_config, report, run


def _settings(**values: Any) -> Settings:
    return Settings.model_validate(values)


def test_a_named_deployment_with_every_backend_local_is_not_ready() -> None:
    """The guide's own command, run without the AWS loader, used to print "ready" having checked nothing."""
    settings = _settings(env="dev")
    checks, code = run(settings)
    assert code == 1
    deployment = [check for check in checks if check.name == "deployment"]
    assert deployment and deployment[0].status == FAILED
    assert "MARKETING_AI_SETTINGS_SOURCE=aws" in deployment[0].remedy
    assert "this deployment is ready" not in report(settings, checks)


def test_a_laptop_with_every_backend_local_is_still_fine() -> None:
    """`env=local` is the one name on which nothing to probe is the right answer."""
    checks, code = run(_settings(env="local"))
    assert code == 0
    assert not [check for check in checks if check.name == "deployment"]


def test_a_skip_is_counted_as_a_skip_not_as_a_pass() -> None:
    checks = [
        Check("configuration", OK, "8 use cases load"),
        Check("storage", SKIPPED, "storage_backend=local"),
    ]
    summary = report(_settings(env="local"), checks).splitlines()[-1]
    assert summary.startswith("1/2 checks passed, 1 skipped")


def test_parameter_store_refusing_is_a_one_line_refusal_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credentials is the most likely first failure on a new machine; it must read as one line."""
    from engine.aws.secrets import SecretsError

    def refuse(*_args: Any, **_kwargs: Any) -> Settings:
        raise SecretsError("SECRETS_UNAVAILABLE", "Could not read /marketing-ai/dev/: NoCredentialsError.")

    monkeypatch.setattr(aws_bootstrap, "load_settings", refuse)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        code = aws_bootstrap.main(["--env", "dev"])
    assert code == 1
    assert stderr.getvalue().strip().endswith("Could not read /marketing-ai/dev/: NoCredentialsError.")


def test_the_database_probe_connects_the_way_the_application_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare `postgresql://` URL must reach psycopg 3 and the configured schema, as DEC-343 says."""
    seen: dict[str, Any] = {}

    def fake_engine(config: Any) -> Any:
        seen["url"] = config.url
        seen["schema"] = config.schema_name
        raise ConnectionRefusedError  # stop before any network; the probe reports the class

    monkeypatch.setattr("engine.aws.postgres.postgres_engine", fake_engine)
    settings = _settings(
        env="dev",
        metadata_backend="postgres",
        postgres_dsn="postgresql://app:secret@db.invalid:5439/marketing?sslmode=require",
        postgres_schema="marketing_ai",
    )
    checks = aws_bootstrap.check_metadata(settings, migrate=False)
    assert checks[0].status == FAILED and checks[0].detail == "ConnectionRefusedError"
    assert seen["url"].startswith("postgresql+psycopg://")
    assert seen["schema"] == "marketing_ai"


def test_the_migrations_carry_the_schema_with_the_url() -> None:
    settings = _settings(
        env="dev",
        metadata_backend="postgres",
        postgres_dsn="postgresql://app:secret@db.invalid:5439/marketing",
        postgres_schema="marketing_ai",
    )
    config = alembic_config(settings)
    arguments = list(config.cmd_opts.x)  # type: ignore[union-attr]
    assert arguments == [
        "url=postgresql+psycopg://app:secret@db.invalid:5439/marketing",
        "schema=marketing_ai",
    ]
    assert config.get_main_option("sqlalchemy.url") in (None, "")


def test_the_migration_step_really_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on SQLite: the `-x` path the script now uses reaches `alembic/env.py` and runs."""
    from sqlalchemy import create_engine, inspect

    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    database = tmp_path / "bootstrap.db"
    settings = _settings(env="local", metadata_backend="postgres", postgres_dsn=f"sqlite:///{database}")

    def sqlite_engine(config: Any) -> Any:
        return create_engine(config.url)

    monkeypatch.setattr("engine.aws.postgres.postgres_engine", sqlite_engine)
    checks = {check.name: check for check in aws_bootstrap.check_metadata(settings, migrate=True)}
    # The connection probe asks `select version()`, which is Postgres's; only the migration is under test.
    assert checks["migrations"].status == OK, checks["migrations"].line()
    assert "alembic_version" in inspect(create_engine(f"sqlite:///{database}")).get_table_names()


def test_the_aws_loader_is_what_a_deployment_is_read_with() -> None:
    """With the parameters a deployment writes, every AWS probe is attempted rather than skipped."""
    source = StaticParameterSource(
        parameters={
            "/marketing-ai/dev/storage_backend": "s3",
            "/marketing-ai/dev/s3_bucket": "example-bucket",
            "/marketing-ai/dev/aws_region": "ap-south-1",
        }
    )
    from engine.settings import load_settings

    settings = load_settings(
        {"MARKETING_AI_SETTINGS_SOURCE": "aws", "MARKETING_AI_ENV": "dev"}, source=source
    )
    assert settings.storage_backend == "s3"
    assert (
        aws_bootstrap.check_something_was_probed(settings, [Check("storage", FAILED, "NoCredentialsError")])
        == []
    )
