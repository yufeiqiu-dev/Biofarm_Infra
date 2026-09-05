"""The GitHub Actions trust policy.

This role can push images and start deployments. Its trust policy is the only
thing deciding who may assume it, so a mistake here is not a broken build - it is
an unrelated repository, or a pull request branch anyone can open, gaining the
ability to deploy to production.
"""

from __future__ import annotations

import json

import pytest
from aws_cdk.assertions import Match

from config import ENVIRONMENTS

OIDC_HOST = "token.actions.githubusercontent.com"


@pytest.fixture(scope="module")
def cicd(templates):
    return templates["Biofarm-Cicd"]


@pytest.fixture(scope="module")
def trust_policy(cicd) -> dict:
    roles = cicd.find_resources("AWS::IAM::Role")
    assert len(roles) == 1, "expected exactly one role in this stack"
    return next(iter(roles.values()))["Properties"]["AssumeRolePolicyDocument"]


def test_the_oidc_provider_is_the_native_resource(cicd):
    """The iam.OpenIdConnectProvider L2 is backed by a custom resource, which
    drags a Lambda and a second role into this stack to call an API
    CloudFormation supports directly."""
    cicd.resource_count_is("AWS::IAM::OIDCProvider", 1)
    cicd.resource_count_is("AWS::Lambda::Function", 0)


def test_only_the_deploying_branches_can_assume_the_role(trust_policy):
    subjects = trust_policy["Statement"][0]["Condition"]["StringLike"][f"{OIDC_HOST}:sub"]

    expected = {
        f"repo:yufeiqiu-dev/{repo}:ref:refs/heads/{cfg.branch}"
        for repo in ("Biofarm_Backend", "Biofarm_Frontend")
        for cfg in ENVIRONMENTS
    }
    assert set(subjects) == expected


def test_no_wildcard_reaches_the_subject_condition(trust_policy):
    """`repo:owner/name:*` would let any ref deploy, including a pull request
    branch opened by anyone. The condition uses StringLike only because the
    value is a list, so a stray wildcard would be silently honoured."""
    subjects = trust_policy["Statement"][0]["Condition"]["StringLike"][f"{OIDC_HOST}:sub"]
    for subject in subjects:
        assert "*" not in subject, subject
        assert "?" not in subject, subject
        assert subject.startswith("repo:yufeiqiu-dev/"), subject


def test_the_audience_is_pinned(trust_policy):
    """Without this condition the role trusts any token GitHub issues for any
    audience, which is the documented way to get this wrong."""
    audience = trust_policy["Statement"][0]["Condition"]["StringEquals"][f"{OIDC_HOST}:aud"]
    assert audience == "sts.amazonaws.com"


def test_the_principal_is_the_provider_and_the_action_is_web_identity(trust_policy):
    statement = trust_policy["Statement"][0]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Effect"] == "Allow"
    assert "Federated" in statement["Principal"]


def test_no_long_lived_credential_is_created(cicd):
    """The whole reason for OIDC. An IAM user here would mean an access key
    living in GitHub's secret store indefinitely, with no rotation and no signal
    if it leaked."""
    cicd.resource_count_is("AWS::IAM::User", 0)
    cicd.resource_count_is("AWS::IAM::AccessKey", 0)


def test_every_permission_is_scoped_to_a_resource(cicd):
    """CI can push images and start deployments, and nothing else.

    `ecr:GetAuthorizationToken` is the single exception: it has no resource to
    scope to, by design of the API. Every other statement must name one, or the
    role has quietly become account-wide.
    """
    policies = cicd.find_resources("AWS::IAM::Policy")
    statements = [
        s
        for policy in policies.values()
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert statements, "expected at least one policy statement"

    for statement in statements:
        actions = statement["Action"]
        actions = [actions] if isinstance(actions, str) else actions
        assert "*" not in actions, statement

        if statement["Resource"] == "*":
            assert actions == ["ecr:GetAuthorizationToken"], (
                f"unscoped statement beyond the login call: {statement}"
            )


def test_ci_can_push_images_but_not_delete_the_repository(cicd):
    """A deploy role that can delete the repository turns a compromised workflow
    into a loss of every image, including the one production is running."""
    rendered = json.dumps(cicd.to_json())
    for forbidden in ("ecr:DeleteRepository", "ecr:BatchDeleteImage", "ecr:PutLifecyclePolicy"):
        assert forbidden not in rendered, forbidden
    assert "ecr:PutImage" in rendered


def test_sessions_are_short(cicd):
    roles = cicd.find_resources("AWS::IAM::Role")
    duration = next(iter(roles.values()))["Properties"]["MaxSessionDuration"]
    assert duration <= 3600, "a deploy role should not hold a long session"
