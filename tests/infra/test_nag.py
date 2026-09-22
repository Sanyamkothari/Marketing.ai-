"""cdk-nag, run offline, plus the hygiene rules the suppression list has to keep.

`make infra-nag` shells out to the CDK CLI and needs node. These assertions do the same work in
process - `AwsSolutionsChecks` is an `Aspect` and `Annotations.from_stack` reads what it recorded -
so a suppression that stops matching fails `make infra-test` too, a minute after it was written
rather than at the end of a pipeline. Both deployments are checked, because a suppression that only
covers `dev` is the mistake this module exists to catch.

The second half is about the list itself. A suppression is only worth what its reason is worth, so
the reason is held to a length no label reaches, every regex has to parse, and every regex has to be
anchored - an unanchored pattern silently covers findings nobody wrote it for, which is the one way
this file could make the deployment less safe than having no suppressions at all.
"""

from __future__ import annotations

import re

import pytest
from aws_cdk.assertions import Annotations, Match
from infra.app import STACK_ORDER, Deployment, build_app
from infra.context import AppContext
from infra.nag_suppressions import SUPPRESSIONS, Suppression, suppressions_for

from tests.infra.conftest import ACCOUNT, PROD_CONTEXT_KEYS

_ANY_AWS_SOLUTIONS_RULE = Match.string_like_regexp(r"AwsSolutions-.*")
_ANY_VALIDATION_FAILURE = Match.string_like_regexp(r"CdkNagValidationFailure.*")


@pytest.fixture(scope="module")
def nagged_dev(tmp_path_factory: pytest.TempPathFactory) -> Deployment:
    """A dev deployment synthesised with the AwsSolutions checks switched on."""
    context = AppContext.from_mapping({"env_name": "dev", "cdk_nag": "true"})
    return build_app(context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("nag-dev")))


@pytest.fixture(scope="module")
def nagged_prod(tmp_path_factory: pytest.TempPathFactory) -> Deployment:
    """The same for prod, where several of the dev suppressions must NOT be needed."""
    context = AppContext.from_mapping({**PROD_CONTEXT_KEYS, "cdk_nag": "true"})
    return build_app(context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("nag-prod")))


@pytest.mark.parametrize("component", STACK_ORDER)
def test_no_stack_has_an_unsuppressed_dev_finding(nagged_dev: Deployment, component: str) -> None:
    found = Annotations.from_stack(nagged_dev.stack(component)).find_error("*", _ANY_AWS_SOLUTIONS_RULE)
    assert not found, f"{component}: {[entry.entry.data for entry in found]}"


@pytest.mark.parametrize("component", STACK_ORDER)
def test_no_stack_has_an_unsuppressed_prod_finding(nagged_prod: Deployment, component: str) -> None:
    found = Annotations.from_stack(nagged_prod.stack(component)).find_error("*", _ANY_AWS_SOLUTIONS_RULE)
    assert not found, f"{component}: {[entry.entry.data for entry in found]}"


@pytest.mark.parametrize("component", STACK_ORDER)
def test_no_rule_failed_to_evaluate(nagged_dev: Deployment, component: str) -> None:
    """A rule that threw is a rule that checked nothing, so it may not pass silently as a warning."""
    stack = nagged_dev.stack(component)
    found = Annotations.from_stack(stack).find_warning("*", _ANY_VALIDATION_FAILURE)
    assert not found, f"{component}: {[entry.entry.data for entry in found]}"


def test_prod_does_not_carry_the_dev_only_suppressions(nagged_prod: Deployment) -> None:
    """A prod synthesis excuses nothing that only `env_name=dev` makes true.

    ECS4 survives into prod and is meant to: Container Insights is a per-task charge and is opt-in
    at every `env_name`, so the suppression follows `container_insights` rather than the name. The
    two that *are* decided by the name - no multi-AZ, no deletion protection - are gone.
    """
    rules = {suppression.rule for suppression in suppressions_for(nagged_prod)}
    assert rules == {"AwsSolutions-ECS4"}, f"prod carries: {sorted(rules)}"


def test_prod_with_container_insights_on_carries_no_conditional_suppression_at_all(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The fully-hardened deployment: every conditional suppression has a finding that is gone."""
    context = AppContext.from_mapping({**PROD_CONTEXT_KEYS, "cdk_nag": "true", "container_insights": "true"})
    deployment = build_app(context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("nag-hard")))
    assert suppressions_for(deployment) == ()
    for component in STACK_ORDER:
        stack = deployment.stack(component)
        assert not Annotations.from_stack(stack).find_error("*", _ANY_AWS_SOLUTIONS_RULE), component


def test_dev_carries_exactly_the_three_it_should(nagged_dev: Deployment) -> None:
    rules = {suppression.rule for suppression in suppressions_for(nagged_dev)}
    assert rules == {"AwsSolutions-RDS3", "AwsSolutions-RDS10", "AwsSolutions-ECS4"}


def test_a_dev_deployment_that_rehearses_failover_drops_only_the_multi_az_suppression(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """`db_multi_az` is overridable, so RDS3 and RDS10 cannot both be keyed on `env_name`."""
    context = AppContext.from_mapping({"env_name": "dev", "db_multi_az": "true", "cdk_nag": "true"})
    deployment = build_app(context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("nag-failover")))
    rules = {suppression.rule for suppression in suppressions_for(deployment)}
    assert "AwsSolutions-RDS3" not in rules, "suppressing a finding this deployment does not raise"
    assert "AwsSolutions-RDS10" in rules, "deletion protection is still off; RDS10 still fires"
    for component in STACK_ORDER:
        stack = deployment.stack(component)
        assert not Annotations.from_stack(stack).find_error("*", _ANY_AWS_SOLUTIONS_RULE), component


def _every_suppression(deployment: Deployment) -> tuple[Suppression, ...]:
    return (*SUPPRESSIONS, *suppressions_for(deployment))


def test_every_suppression_names_a_stack_that_exists(nagged_dev: Deployment) -> None:
    for suppression in _every_suppression(nagged_dev):
        assert suppression.stack in STACK_ORDER, suppression


def test_every_reason_is_an_argument_rather_than_a_label(nagged_dev: Deployment) -> None:
    """cdk-nag itself only demands ten characters, which "not needed" clears."""
    for suppression in _every_suppression(nagged_dev):
        reason = suppression.reason
        assert len(reason) >= 120, f"{suppression.rule} on {suppression.path}: {reason!r}"
        assert reason[0].isupper() and reason.rstrip().endswith("."), suppression.rule
        assert not re.search(
            r"\b(n/?a|not applicable|by design|as designed|false positive|wont fix|acceptable)\b",
            reason,
            re.IGNORECASE,
        ), f"{suppression.rule}: a label, not a reason"


def test_every_regex_parses_and_is_anchored(nagged_dev: Deployment) -> None:
    """`/pattern/flags` is the only shape cdk-nag's `toRegEx` accepts, and `^...$` is ours."""
    seen = 0
    for suppression in _every_suppression(nagged_dev):
        for raw in suppression.applies_to_regex:
            assert raw.startswith("/"), raw
            body, _, flags = raw[1:].rpartition("/")
            assert body, f"{raw} has no pattern between its delimiters"
            assert flags == "", f"{raw}: this module writes no flags; toRegEx would accept them"
            assert body.startswith("^") and body.endswith("$"), f"{raw} is not anchored"
            re.compile(body)  # ECMA-262 and Python disagree at the edges; these use the common subset
            seen += 1
    assert seen, "no regex suppressions at all - has the IAM5 coverage been dropped?"


def test_no_suppression_hides_a_bare_wildcard_without_the_test_that_replaces_it() -> None:
    """`Resource::*` is accepted only because `tests/infra/test_iam.py` is stricter than the rule."""
    wildcard = [s for s in SUPPRESSIONS if "Resource::*" in s.applies_to]
    assert wildcard, "the wildcard suppressions have gone - so should this test"
    for suppression in wildcard:
        assert "RESOURCE_WILDCARD_ALLOW_LIST" in suppression.reason
        assert "test_iam" in suppression.reason
