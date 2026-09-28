"""Plan H (DEC-1101, DEC-1120): every deployment gets a connections key, and nothing ever replaces it.

A prod API refuses to save a connection's password without `MARKETING_AI_CONNECTIONS_KEY`, so the
deployment has to supply it. As with the privacy salt, the database stack generates it once as its own
secret - letters and digits, from which the engine derives the Fernet key - never rotated, retained
when the stack goes, and carried into the application secret as a deploy-time reference.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.database import CONNECTIONS_KEY_LENGTH

from tests.infra.conftest import rendered, resources


def _by_name(template: Template) -> dict[str, dict[str, Any]]:
    return {
        resource["Properties"]["Name"]: resource
        for resource in resources(template, "AWS::SecretsManager::Secret").values()
    }


def test_the_key_is_generated_retained_and_never_rotated(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    for env, templates in (("dev", dev_templates), ("prod", prod_templates)):
        secret = _by_name(templates["database"])[f"marketing-ai/{env}/connections-key"]
        generated = secret["Properties"]["GenerateSecretString"]
        assert generated["PasswordLength"] == CONNECTIONS_KEY_LENGTH >= 32
        assert generated["ExcludePunctuation"] is True  # letters and digits: what the engine derives from
        assert (secret["DeletionPolicy"], secret["UpdateReplacePolicy"]) == ("Retain", "Retain"), env
        assert "SecretString" not in secret["Properties"], "no literal value in the template"
        schedules = resources(templates["database"], "AWS::SecretsManager::RotationSchedule")
        assert all(
            "ConnectionsKey" not in str(schedule["Properties"]["SecretId"]) for schedule in schedules.values()
        )


def test_the_application_secret_carries_the_key_by_reference(prod_templates: dict[str, Template]) -> None:
    app = _by_name(prod_templates["database"])["marketing-ai/prod/app"]
    body = rendered(app["Properties"]["SecretString"])
    assert "connections_key" in body and "privacy_salt" in body and "postgres_dsn" in body
    assert "ConnectionsKey" in body, "resolved from the generated key secret at deploy time"
