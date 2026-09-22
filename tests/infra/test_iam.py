"""Every `Allow` in every stack, checked against the rules `infra/policies.py` states.

The big one is the wildcard test. `RESOURCE_WILDCARD_ALLOW_LIST` names the actions AWS documents
with no resource types; this walks the seven synthesised stacks and fails on any other `Allow` with
`"Resource": "*"`. It catches two different mistakes with one assertion: a statement somebody wrote
lazily, and a `*` that arrived inside a construct nobody read - an `auto_delete_objects` Lambda, a
log-retention custom resource, a managed policy attached for convenience.
"""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Template
from infra.app import build_app
from infra.context import AppContext
from infra.naming import JOB_NAME_PREFIX, OBJECT_PREFIXES
from infra.policies import RESOURCE_WILDCARD_ALLOW_LIST

from tests.infra.conftest import ACCOUNT, as_list, literal_resources, rendered, statements


def test_no_allow_names_a_wildcard_resource_unless_it_is_on_the_list(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    offenders: list[tuple[str, str, list[Any]]] = []
    for label, templates in (("dev", dev_templates), ("prod", prod_templates)):
        for component, template in templates.items():
            for statement in statements(template, effect="Allow"):
                if "*" not in as_list(statement.get("Resource")):
                    continue
                actions = as_list(statement.get("Action"))
                if not set(actions) <= RESOURCE_WILDCARD_ALLOW_LIST:
                    offenders.append((label, component, actions))
    assert not offenders, (
        "these Allow statements name every resource in the account and are not on "
        f"RESOURCE_WILDCARD_ALLOW_LIST: {offenders}"
    )


def test_the_allow_list_is_not_carrying_dead_entries(dev_templates: dict[str, Template]) -> None:
    """An entry nothing uses is a permission nobody reviewed the removal of."""
    used: set[str] = set()
    for template in dev_templates.values():
        for statement in statements(template, effect="Allow"):
            if "*" in as_list(statement.get("Resource")):
                used.update(as_list(statement.get("Action")))
    assert used <= RESOURCE_WILDCARD_ALLOW_LIST
    assert used, "no wildcard statement at all - has the allow-list stopped being exercised?"


def test_s3_statements_name_the_real_prefixes(dev_templates: dict[str, Template]) -> None:
    for component in ("compute", "sagemaker"):
        body = rendered(statements(dev_templates[component], effect="Allow"))
        for prefix in OBJECT_PREFIXES:
            assert f"/{prefix}*" in body, f"{component} does not scope S3 to {prefix}"


def test_list_bucket_is_granted_on_the_bucket_itself(dev_templates: dict[str, Template]) -> None:
    """Without it a missing object answers 403 and `exists()` cannot tell absent from forbidden."""
    for component in ("compute", "sagemaker"):
        found = [
            statement
            for statement in statements(dev_templates[component], effect="Allow")
            if "s3:ListBucket" in as_list(statement.get("Action"))
        ]
        assert found, f"{component} cannot list the bucket"
        for statement in found:
            assert (
                "Condition" not in statement
            ), "an s3:prefix condition fails closed during GetObject, which brings the 403 back"


def test_sagemaker_is_scoped_to_the_job_name_prefix(dev_templates: dict[str, Template]) -> None:
    found = [
        statement
        for statement in statements(dev_templates["compute"], effect="Allow")
        if any(action.startswith("sagemaker:") for action in as_list(statement.get("Action")))
    ]
    assert found, "the task role cannot create a job"
    for statement in found:
        for resource in literal_resources(statement):
            assert resource.endswith(f"/{JOB_NAME_PREFIX}-*"), resource
        for kind in ("training-job", "processing-job"):
            assert any(kind in resource for resource in literal_resources(statement))


def test_no_sagemaker_list_action_is_granted_anywhere(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """`ListTrainingJobs` cannot be scoped to a prefix, and the runner never calls one."""
    for templates in (dev_templates, prod_templates):
        for template in templates.values():
            for statement in statements(template, effect="Allow"):
                for action in as_list(statement.get("Action")):
                    assert not action.startswith("sagemaker:List"), action


def test_pass_role_names_one_role_and_one_service(dev_templates: dict[str, Template]) -> None:
    found = [
        statement
        for statement in statements(dev_templates["compute"], effect="Allow")
        if "iam:PassRole" in as_list(statement.get("Action"))
    ]
    assert len(found) == 1
    condition = found[0]["Condition"]
    assert condition == {"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}}


def test_metric_writes_are_confined_to_the_product_namespace(
    dev_templates: dict[str, Template],
) -> None:
    for component in ("compute", "sagemaker"):
        found = [
            statement
            for statement in statements(dev_templates[component], effect="Allow")
            if "cloudwatch:PutMetricData" in as_list(statement.get("Action"))
        ]
        assert found, component
        for statement in found:
            assert statement["Condition"] == {"StringEquals": {"cloudwatch:namespace": "MarketingAI"}}


def test_the_task_may_not_create_a_log_group(dev_templates: dict[str, Template]) -> None:
    """A principal that can create its own group creates one that never expires."""
    for statement in statements(dev_templates["compute"], effect="Allow"):
        assert "logs:CreateLogGroup" not in as_list(statement.get("Action"))


def test_bedrock_is_denied_explicitly_when_no_model_is_named(
    dev_templates: dict[str, Template],
) -> None:
    """An absent Allow is indistinguishable from an oversight; a Deny survives a broad policy."""
    for component in ("compute", "sagemaker"):
        denies = [
            statement
            for statement in statements(dev_templates[component], effect="Deny")
            if any(action.startswith("bedrock:") for action in as_list(statement.get("Action")))
        ]
        assert denies, f"{component} has no explicit Bedrock deny"
        assert "*" in as_list(denies[0]["Resource"])
        allows = [
            statement
            for statement in statements(dev_templates[component], effect="Allow")
            if any(action.startswith("bedrock:") for action in as_list(statement.get("Action")))
        ]
        assert not allows


def test_bedrock_allows_exactly_the_named_models() -> None:

    deployment = build_app(
        AppContext.from_mapping(
            {
                "env_name": "dev",
                "bedrock_enabled": "true",
                "bedrock_model_ids": "anthropic.claude-3-5-sonnet-20240620-v1:0",
            }
        ),
        account=ACCOUNT,
    )
    body = rendered(statements(Template.from_stack(deployment.compute), effect="Allow"))
    assert "foundation-model/anthropic.claude-3-5-sonnet-20240620-v1:0" in body
    denies = [
        statement
        for statement in statements(Template.from_stack(deployment.compute), effect="Deny")
        if any(action.startswith("bedrock:") for action in as_list(statement.get("Action")))
    ]
    assert not denies


def test_bedrock_enabled_with_no_models_still_denies() -> None:

    deployment = build_app(
        AppContext.from_mapping({"env_name": "dev", "bedrock_enabled": "true"}), account=ACCOUNT
    )
    denies = [
        statement
        for statement in statements(Template.from_stack(deployment.compute), effect="Deny")
        if any(action.startswith("bedrock:") for action in as_list(statement.get("Action")))
    ]
    assert denies, "enabled with an empty model list must deny, never allow-all"


def test_no_aws_managed_policy_is_attached(
    dev_templates: dict[str, Template], prod_templates: dict[str, Template]
) -> None:
    """Every permission is a statement somebody can read in this repository."""
    for templates in (dev_templates, prod_templates):
        for component, template in templates.items():
            for resource in template.find_resources("AWS::IAM::Role").values():
                attached = resource["Properties"].get("ManagedPolicyArns", [])
                literals = [arn for arn in attached if isinstance(arn, str)]
                assert not literals, f"{component} attaches {literals}"


def test_the_secret_grant_names_one_arn(dev_templates: dict[str, Template]) -> None:
    for component in ("compute", "sagemaker"):
        found = [
            statement
            for statement in statements(dev_templates[component], effect="Allow")
            if "secretsmanager:GetSecretValue" in as_list(statement.get("Action"))
        ]
        assert len(found) == 1, component
        assert len(as_list(found[0]["Resource"])) == 1
