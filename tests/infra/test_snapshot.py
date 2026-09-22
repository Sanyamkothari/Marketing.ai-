"""A snapshot of what each stack contains, so an accidental resource cannot arrive quietly.

Not a snapshot of the whole template. A full CloudFormation snapshot for seven stacks is tens of
thousands of lines that churn on every CDK upgrade, so nobody reads the diff and the test degrades
into "run it again with --update". This snapshot records the thing a reviewer actually wants to be
told about: the set of logical ids and their resource types, per stack.

That is enough to catch what matters - a construct that quietly brought a Lambda, a role, a second
bucket or a custom resource - and small enough that the diff is the review.

Refresh it deliberately with `MARKETING_AI_UPDATE_SNAPSHOTS=1 make infra-test`, and read the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

import pytest
from aws_cdk.assertions import Template
from infra.app import STACK_ORDER, Deployment

SNAPSHOT_DIR: Final[Path] = Path(__file__).resolve().parent / "snapshots"
UPDATE_ENV_VAR: Final[str] = "MARKETING_AI_UPDATE_SNAPSHOTS"


def census(deployment: Deployment) -> dict[str, dict[str, str]]:
    """`{stack component: {logical id: resource type}}` for the whole deployment."""
    return {
        component: {
            logical_id: resource["Type"]
            for logical_id, resource in sorted(
                Template.from_stack(deployment.stack(component)).to_json()["Resources"].items()
            )
        }
        for component in STACK_ORDER
    }


def check(name: str, actual: dict[str, dict[str, str]]) -> None:
    path = SNAPSHOT_DIR / f"{name}.json"
    rendered = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if os.environ.get(UPDATE_ENV_VAR):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        pytest.skip(f"{UPDATE_ENV_VAR} is set: {path.name} rewritten, review the diff")
    if not path.exists():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        pytest.fail(f"{path} did not exist; it has been written - review it and commit it")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"the synthesised resources no longer match {path.name}. If the change is intended, "
        f"rerun with {UPDATE_ENV_VAR}=1 and commit the diff."
    )


def test_dev_resources_are_what_was_reviewed(dev: Deployment) -> None:
    check("dev", census(dev))


def test_prod_resources_are_what_was_reviewed(prod: Deployment) -> None:
    check("prod", census(prod))


def test_no_stack_is_empty(dev: Deployment, prod: Deployment) -> None:
    """A template with no `Resources` section is not a deployable template."""
    for deployment in (dev, prod):
        for component, resources in census(deployment).items():
            assert resources, f"{component} would synthesise an undeployable template"
