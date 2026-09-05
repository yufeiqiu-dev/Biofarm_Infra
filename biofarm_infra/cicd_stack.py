"""GitHub Actions' way into this account.

The alternative is an IAM user with a long-lived access key pasted into GitHub
secrets. That key is then a permanent credential sitting in a third-party system,
it does not rotate, and nothing tells you if it leaks. OIDC replaces it with a
short-lived token GitHub mints per workflow run, which AWS validates against the
trust policy below - so there is no standing credential to steal.

The trust policy is where the security actually lives. `sub` is restricted to
specific repositories and branches, so a workflow in a fork, on a pull request,
or in an unrelated repository under the same owner cannot assume this role.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr, aws_iam as iam
from constructs import Construct

from config import BACKEND_REPO, ENVIRONMENTS, FRONTEND_REPO, GITHUB_OWNER

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"


class CicdStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # CfnOIDCProvider, not the iam.OpenIdConnectProvider L2. The L2 is backed
        # by a custom resource, so it adds a Lambda function and a second IAM
        # role to this stack purely to call an API that CloudFormation supports
        # natively - more to maintain, and it fails outright in an account whose
        # policies forbid the Lambda.
        #
        # ThumbprintList is deliberately omitted: it is optional, and when absent
        # IAM retrieves the intermediate CA thumbprint itself. Hardcoding
        # GitHub's thumbprint is the older advice and ages badly - it changes,
        # and a stale value breaks every deployment at once.
        #
        # An account can hold only one OIDC provider per issuer URL. If this
        # account already has one, added by hand or by another project, creation
        # fails and the fix is to import it here rather than create it.
        provider = iam.CfnOIDCProvider(
            self,
            "GitHubOidcProvider",
            url=GITHUB_OIDC_URL,
            client_id_list=[GITHUB_OIDC_AUDIENCE],
        )

        # Only these repo/branch combinations may assume the role. Deploying
        # branches are listed explicitly rather than with a wildcard: `repo:*`
        # would let a pull request branch, which anyone can open, deploy.
        deploy_subjects = [
            f"repo:{GITHUB_OWNER}/{repo}:ref:refs/heads/{cfg.branch}"
            for repo in (BACKEND_REPO, FRONTEND_REPO)
            for cfg in ENVIRONMENTS
        ]

        self.deploy_role = iam.Role(
            self,
            "GitHubDeployRole",
            role_name="biofarm-github-deploy",
            description="Assumed by GitHub Actions to push images and start App Runner deployments.",
            max_session_duration=cdk.Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.attr_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_URL.removeprefix('https://')}:aud": GITHUB_OIDC_AUDIENCE,
                    },
                    # StringLike, not StringEquals, only because the value is a
                    # list; each entry is still a fully-qualified ref with no
                    # wildcard in it.
                    "StringLike": {
                        f"{GITHUB_OIDC_URL.removeprefix('https://')}:sub": deploy_subjects,
                    },
                },
            ),
        )

        # Permissions are added by the stacks that own the resources - AppStack
        # grants push on its ECR repository and StartDeployment on its service -
        # so this role's policy stays a description of what CI actually does,
        # rather than a wildcard maintained by hand.
        # ecr:GetAuthorizationToken is not granted here: grant_pull_push below
        # adds it, and stating it twice produced two identical statements in the
        # policy - noise that makes the next reader wonder which one matters.
        self.deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="FindTheService",
                # The workflow looks the service up by name to get its ARN, and
                # ListServices takes no resource either - it is the call that
                # tells you which ARNs exist. Without it the deploy job fails
                # with AccessDenied *after* the image is already in ECR, and
                # never reaches the friendlier "deploy the stack first" branch.
                # Read-only: it returns names, ARNs and status, nothing else.
                actions=["apprunner:ListServices"],
                resources=["*"],
            )
        )

        # One repository for both environments, not one each. Promoting staging
        # to production then means retagging a digest that has already been
        # tested, rather than rebuilding and hoping the result is identical.
        self.repository = ecr.Repository(
            self,
            "BackendRepository",
            repository_name="biofarm-backend",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.MUTABLE,
            # Untagged images accumulate on every push, and ECR bills for
            # storage. Tagged ones are what the services actually run.
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Expire untagged images",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=cdk.Duration.days(7),
                ),
                ecr.LifecycleRule(
                    description="Keep the last 20 tagged images for rollback",
                    tag_status=ecr.TagStatus.ANY,
                    max_image_count=20,
                ),
            ],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        self.repository.grant_pull_push(self.deploy_role)

        cdk.CfnOutput(
            self,
            "EcrRepositoryUri",
            value=self.repository.repository_uri,
            description="Push target for the backend image.",
        )

        cdk.CfnOutput(
            self,
            "DeployRoleArn",
            value=self.deploy_role.role_arn,
            description="Set as AWS_DEPLOY_ROLE_ARN in both repositories' GitHub Actions variables.",
        )
