"""Offline assertions about the synthesised CloudFormation, run by `make infra-test`.

These tests need `aws-cdk-lib` and nothing else: no AWS account, no credentials, no network.
`aws_cdk.assertions.Template.from_stack` renders a stack in-process, so everything here is a
statement about what would be deployed, checked in the time a unit test takes.
"""

from __future__ import annotations
