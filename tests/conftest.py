"""Shared fixtures.

These tests assert against the synthesized CloudFormation template. They need no
AWS account, no credentials and no network, which is the point: the security
properties this repo depends on - that staging cannot reach production's
database, that neither environment boots with a bypass enabled - are checked on
every run, not discovered after a deploy.
"""

from __future__ import annotations

import pathlib
import sys

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import build_app  # noqa: E402

@pytest.fixture(scope="session")
def synthesized():
    """The whole app, built once.

    Environment-agnostic, matching how it is deployed. Pinning an account here
    would make CDK resolve availability zones by calling EC2, so the tests would
    need credentials and a network to run.
    """
    return build_app()


@pytest.fixture(scope="session")
def templates(synthesized) -> dict[str, Template]:
    """Every stack in the app, keyed by stack name."""
    return {
        stack.stack_name: Template.from_stack(stack)
        for stack in synthesized.node.children
        if isinstance(stack, cdk.Stack)
    }
