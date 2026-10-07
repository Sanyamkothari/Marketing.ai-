"""Plan J M92 (DEC-1302): every deployment gets a holdout salt, and no deploy ever replaces it.

A use case with a persistent holdout refuses to score without `MARKETING_AI_HOLDOUT_SALT`, and a
changed salt is refused too (`HOLDOUT_SALT_CHANGED`), so the salt has to survive every deploy. A key
added by hand to the application secret would not: the database stack recomposes that document on
every deploy. As with the privacy salt and the connections key, the stack generates it once as its own
secret - never rotated, retained when the stack goes - and carries it into the application secret as a
deploy-time reference.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.database import HOLDOUT_SALT_LENGTH
from infra.naming import SETTINGS_FIELDS, holdout_salt_secret_name

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
        secret = _by_name(templates["database"])[holdout_salt_secret_name(env)]
        generated = secret["Properties"]["GenerateSecretString"]
        assert generated["PasswordLength"] == HOLDOUT_SALT_LENGTH >= 32
        assert generated["ExcludePunctuation"] is True
        assert (secret["DeletionPolicy"], secret["UpdateReplacePolicy"]) == ("Retain", "Retain"), env
        assert "SecretString" not in secret["Properties"], "no literal value in the template"
        schedules = resources(templates["database"], "AWS::SecretsManager::RotationSchedule")
        assert all(
            "HoldoutSalt" not in str(schedule["Properties"]["SecretId"]) for schedule in schedules.values()
        )


def test_the_application_secret_carries_the_salt_by_reference(prod_templates: dict[str, Template]) -> None:
    app = _by_name(prod_templates["database"])["marketing-ai/prod/app"]
    body = rendered(app["Properties"]["SecretString"])
    assert "holdout_salt" in body and "privacy_salt" in body and "connections_key" in body
    assert "HoldoutSalt" in body, "resolved from the generated salt secret at deploy time"


def test_the_key_is_the_settings_field_name() -> None:
    assert "holdout_salt" in SETTINGS_FIELDS
    assert holdout_salt_secret_name("prod") == "marketing-ai/prod/holdout-salt"
