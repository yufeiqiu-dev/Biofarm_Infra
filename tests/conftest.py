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

# A fixed account and region. Some constructs behave differently for an
# environment-agnostic stack - VPC availability zones become tokens rather than
# concrete names - and assertions against tokens test nothing.
TEST_ENV = cdk.Environment(account="111122223333", region="us-east-2")


@pytest.fixture(scope="session")
def synthesized() -> cdk.assertions.Template | None:
    """The whole app, built once."""
    return build_app(env=TEST_ENV)


@pytest.fixture(scope="session")
def templates(synthesized) -> dict[str, Template]:
    """Every stack in the app, keyed by stack name."""
    return {
        stack.stack_name: Template.from_stack(stack)
        for stack in synthesized.node.children
        if isinstance(stack, cdk.Stack)
    }
