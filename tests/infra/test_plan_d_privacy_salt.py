"""Plan D (ruling R3, DEC-860): every deployment gets a privacy salt, and nothing ever replaces it.

`engine.settings.Settings.privacy_salt` has no default and a production API refuses to start without
one, so the deployment has to supply it. The database stack generates it once as its own secret -
never rotated, retained when the stack goes - and the application secret carries it as a deploy-time
dynamic reference, exactly as it carries the database password: the value is in no template.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.database import PRIVACY_SALT_LENGTH

from tests.infra.conftest import rendered, resources


def _by_name(template: Template) -> dict[str, dict[str, Any]]:
    return {
        resource["Properties"]["Name"]: resource
        for resource in resources(template, "AWS::SecretsManager::Secret").values()
    }


def test_the_salt_is_generated_retained_and_never_rotated(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for env, templates in (("dev", dev_templates), ("prod", prod_templates)):
        salt = _by_name(templates["database"])[f"marketing-ai/{env}/privacy-salt"]
        generated = salt["Properties"]["GenerateSecretString"]
        assert generated["PasswordLength"] == PRIVACY_SALT_LENGTH >= 16
        assert generated["ExcludePunctuation"] is True
        assert (salt["DeletionPolicy"], salt["UpdateReplacePolicy"]) == ("Retain", "Retain"), env
        assert "SecretString" not in salt["Properties"], "no literal value in the template"
        schedules = resources(templates["database"], "AWS::SecretsManager::RotationSchedule")
        assert all(
            "PrivacySalt" not in str(schedule["Properties"]["SecretId"]) for schedule in schedules.values()
        )


def test_the_application_secret_carries_the_salt_by_reference(prod_templates: dict[str, Template]) -> None:
    app = _by_name(prod_templates["database"])["marketing-ai/prod/app"]
    body = rendered(app["Properties"]["SecretString"])
    assert "privacy_salt" in body and "postgres_dsn" in body
    assert "{{resolve:secretsmanager:" in body
    assert "PrivacySalt" in body, "resolved from the generated salt secret at deploy time"
