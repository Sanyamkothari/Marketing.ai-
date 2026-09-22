"""Two synthesised deployments, built once, and the helpers every assertion module shares.

Synthesising the whole app costs a second or so - jsii starts a Node process and CDK renders seven
stacks - so the two deployments a test might want are session-scoped and every module reads the
same objects. Nothing here mutates them.

The account is a literal rather than `None`. An environment-agnostic stack renders the account as
`Ref: AWS::AccountId`, which is correct and deployable but turns every ARN in every assertion into
an `Fn::Join`, and an assertion nobody can read is an assertion nobody maintains.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.util import find_spec
from typing import Any, Final

import pytest

# `aws-cdk-lib` is the `deploy` extra. It lives in `.venv-infra`, which `make infra-setup` builds
# and `make infra-test` runs from - deliberately, so a laptop and the main gate never pay for CDK
# (DEC-364). But `testpaths` is `["tests"]`, so the main venv's `make test` walks this directory
# too, and the import below then died with a bare ModuleNotFoundError: one collection error that
# took the entire fast suite down with it, on CI and on every machine that had not run
# `make infra-setup`.
#
# `collect_ignore_glob` is the fix rather than `importorskip`. Both were written, by two sessions,
# and they differ in exactly one case. `pytest.importorskip("aws_cdk")` raises `Skipped` while the
# *conftest* is being imported: when pytest reached this directory by walking `tests/` - which is
# what `make test` does - that is reported as one skip and the run exits 0; when the directory is
# named on the command line (`pytest tests/infra`), the same Skipped escapes and the run exits 1.
# Both were measured on this tree. `collect_ignore_glob`, set before the imports, tells pytest
# there is nothing here to collect in either case, so it is the robust one - and `make infra-test`,
# which has the extra, still collects everything.
#
# The extra is `deploy`, not `aws`: `aws` carries boto3 for the runtime, `deploy` carries
# aws-cdk-lib for synthesising infra/, so `pip install -e '.[dev,aws]'` does not fix this.
_HAS_CDK: Final[bool] = find_spec("aws_cdk") is not None
collect_ignore_glob: Final[list[str]] = [] if _HAS_CDK else ["*.py"]

if _HAS_CDK:
    from aws_cdk.assertions import Template
    from infra.app import STACK_ORDER, Deployment, build_app
    from infra.context import AppContext

ACCOUNT: Final[str] = "123456789012"
"""A syntactically valid account id; the tests never talk to it."""

REGION: Final[str] = "ap-south-1"

PROD_CONTEXT_KEYS: Final[dict[str, str]] = {
    "env_name": "prod",
    "certificate_arn": f"arn:aws:acm:{REGION}:{ACCOUNT}:certificate/11111111-2222-3333-4444-555555555555",
    "domain_name": "marketing.example.com",
    "alert_email": "ops@example.com",
    "client_id": "acme",
    "monthly_budget_usd": "750",
}
"""The smallest prod deployment `AppContext.validate` accepts; see infra/context.py for why."""


@pytest.fixture(scope="session")
def dev_context() -> AppContext:
    """The defaults, with nothing supplied but the name."""
    return AppContext.from_mapping({"env_name": "dev"})


@pytest.fixture(scope="session")
def prod_context() -> AppContext:
    """A production deployment: certificate, domain, alerting and a budget."""
    return AppContext.from_mapping(dict(PROD_CONTEXT_KEYS))


@pytest.fixture(scope="session")
def dev(dev_context: AppContext, tmp_path_factory: pytest.TempPathFactory) -> Deployment:
    return build_app(dev_context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("cdk-out-dev")))


@pytest.fixture(scope="session")
def prod(prod_context: AppContext, tmp_path_factory: pytest.TempPathFactory) -> Deployment:
    return build_app(prod_context, account=ACCOUNT, outdir=str(tmp_path_factory.mktemp("cdk-out-prod")))


@pytest.fixture(scope="session")
def dev_templates(dev: Deployment) -> dict[str, Template]:
    """Every dev stack rendered, keyed by component name."""
    return templates_of(dev)


@pytest.fixture(scope="session")
def prod_templates(prod: Deployment) -> dict[str, Template]:
    """Every prod stack rendered, keyed by component name."""
    return templates_of(prod)


def templates_of(deployment: Deployment) -> dict[str, Template]:
    """`{component: Template}` for the whole deployment."""
    return {name: Template.from_stack(deployment.stack(name)) for name in STACK_ORDER}


def resources(template: Template, resource_type: str) -> dict[str, Any]:
    """Every resource of one type, keyed by logical id."""
    found = template.find_resources(resource_type)
    return dict(found)


def sole(template: Template, resource_type: str) -> dict[str, Any]:
    """The properties of the only resource of that type, failing if there is not exactly one."""
    found = resources(template, resource_type)
    assert len(found) == 1, f"expected exactly one {resource_type}, found {len(found)}"
    return dict(next(iter(found.values()))["Properties"])


def statements(template: Template, *, effect: str | None = None) -> list[dict[str, Any]]:
    """Every statement of every *identity* policy in the stack.

    Resource policies - a bucket policy, a KMS key policy, a topic policy - are deliberately left
    out. `"Resource": "*"` in a key policy means "this key" and is the only spelling AWS accepts;
    reading it as a wildcard would make the rule in `infra/policies.py` unstatable.
    """
    collected: list[dict[str, Any]] = []
    for resource_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::Role"):
        for resource in resources(template, resource_type).values():
            properties = resource.get("Properties", {})
            documents = []
            if "PolicyDocument" in properties:
                documents.append(properties["PolicyDocument"])
            for inline in properties.get("Policies", []) or []:
                documents.append(inline["PolicyDocument"])
            for document in documents:
                for statement in document.get("Statement", []):
                    if effect is None or statement.get("Effect") == effect:
                        collected.append(dict(statement))
    return collected


def as_list(value: Any) -> list[Any]:
    """`Action` and `Resource` are a string or a list; this is both, as a list."""
    if isinstance(value, list):
        return list(value)
    return [value]


def literal_resources(statement: Mapping[str, Any]) -> list[str]:
    """The statement's resources that are plain strings; tokens (`Fn::Join`, `Ref`) are skipped."""
    return [item for item in as_list(statement.get("Resource")) if isinstance(item, str)]


def rendered(value: Any) -> str:
    """A template fragment as one searchable string, tokens and all."""
    return json.dumps(value, sort_keys=True)
