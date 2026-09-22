"""The task definition may set only names the application will accept, and does.

`engine/settings.py` refuses to start when it finds a `MARKETING_AI_*` variable that is neither a
field nor in `NON_FIELD_ENV_VARS` (DEC-304). That makes a typo in a CDK file a deployment that does
not boot - the container exits, the load balancer never gets a healthy target, and the only clue is
one line in a log group somebody has to find. So the check happens here, against the synthesised
container definition, in the time a unit test takes.

The same rule applies to the SSM parameters, from the other direction: a parameter whose leaf is
not a field name is written, looks right in the console, and is silently ignored on read, because
`AwsParameterSource.parameters` maps leaves onto field names and drops what it does not recognise.
"""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk.assertions import Template
from infra.naming import NON_FIELD_ENV_VARS, SETTINGS_ENV_VARS, SETTINGS_FIELDS

from tests.infra.conftest import sole


def container(templates: dict[str, Template]) -> dict[str, Any]:
    definition = sole(templates["compute"], "AWS::ECS::TaskDefinition")
    containers = definition["ContainerDefinitions"]
    assert len(containers) == 1
    return dict(containers[0])


def environment_names(templates: dict[str, Template]) -> set[str]:
    return {entry["Name"] for entry in container(templates).get("Environment", [])}


def test_every_environment_variable_is_one_the_application_accepts(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    allowed = set(SETTINGS_ENV_VARS.values()) | NON_FIELD_ENV_VARS
    for templates in (dev_templates, prod_templates):
        names = environment_names(templates)
        unknown = {name for name in names if name.startswith("MARKETING_AI_")} - allowed
        assert not unknown, (
            f"{', '.join(sorted(unknown))} would make the application refuse to start; "
            "see the frozen table in infra/naming.py"
        )


def test_the_task_definition_sets_only_what_must_be_true_before_ssm_can_be_read(
    dev_templates: dict[str, Template],
) -> None:
    """Anything else would override the parameter of the same name, invisibly (DEC-377)."""
    assert environment_names(dev_templates) == {
        "MARKETING_AI_SETTINGS_SOURCE",
        "MARKETING_AI_ENV",
        "MARKETING_AI_REGION",
    }


def test_the_one_non_field_variable_is_a_declared_non_field(
    dev_templates: dict[str, Template],
) -> None:
    names = environment_names(dev_templates)
    assert names - set(SETTINGS_ENV_VARS.values()) == {"MARKETING_AI_SETTINGS_SOURCE"}
    assert "MARKETING_AI_SETTINGS_SOURCE" in NON_FIELD_ENV_VARS


def test_the_settings_source_selects_aws(dev_templates: dict[str, Template]) -> None:
    values = {entry["Name"]: entry["Value"] for entry in container(dev_templates)["Environment"]}
    assert values["MARKETING_AI_SETTINGS_SOURCE"] == "aws"
    assert values["MARKETING_AI_ENV"] == "dev"
    assert values["MARKETING_AI_REGION"] == "ap-south-1"


def test_no_secret_is_injected_into_the_task_definition(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """The task fetches the application document itself; the deployment hands it a name."""
    for templates in (dev_templates, prod_templates):
        assert not container(templates).get("Secrets")


def test_every_parameter_leaf_is_a_settings_field(dev_templates: dict[str, Template]) -> None:
    template = dev_templates["compute"]
    for resource in template.find_resources("AWS::SSM::Parameter").values():
        name = resource["Properties"]["Name"]
        assert name.startswith("/marketing-ai/dev/"), name
        leaf = name.rsplit("/", 1)[-1]
        assert leaf in SETTINGS_FIELDS, f"{leaf} is not a Settings field; it would be ignored on read"


def test_parameters_are_flat_under_the_deployment_path(dev_templates: dict[str, Template]) -> None:
    """`GetParametersByPath` is called with `Recursive=False`; a child path is never read."""
    for resource in dev_templates["compute"].find_resources("AWS::SSM::Parameter").values():
        name = resource["Properties"]["Name"]
        assert name.count("/") == 3, f"{name} is nested; the application reads one level only"


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("storage_backend", "s3"),
        ("metadata_backend", "postgres"),
        ("job_backend", "sagemaker"),
        ("log_format", "json"),
        ("metrics_backend", "emf"),
        ("sagemaker_job_name_prefix", "marketing-ai"),
        ("sagemaker_train_instance_type", "ml.m5.2xlarge"),
        ("sagemaker_processing_instance_type", "ml.m5.xlarge"),
        ("sagemaker_max_concurrent_jobs", "3"),
    ],
)
def test_the_backends_the_deployment_selects(
    dev_templates: dict[str, Template], field_name: str, expected: str
) -> None:
    for resource in dev_templates["compute"].find_resources("AWS::SSM::Parameter").values():
        if resource["Properties"]["Name"].endswith(f"/{field_name}"):
            assert resource["Properties"]["Value"] == expected
            return
    raise AssertionError(f"no parameter for {field_name}")


def test_no_database_url_parameter_exists(dev_templates: dict[str, Template]) -> None:
    """It is a credential. It lives in Secrets Manager, and Parameter Store holds only the rest."""
    names = {
        resource["Properties"]["Name"]
        for resource in dev_templates["compute"].find_resources("AWS::SSM::Parameter").values()
    }
    assert "/marketing-ai/dev/database_url" not in names
