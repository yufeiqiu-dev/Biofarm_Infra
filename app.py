#!/usr/bin/env python3
"""CDK entry point.

    cdk synth                          # both environments, offline
    cdk diff  --profile <sso-profile>  # against a real account
    cdk deploy Biofarm-Network Biofarm-Data-staging Biofarm-App-staging --profile <sso-profile>

Account and region come from the CLI profile (`CDK_DEFAULT_*`), so the same code
deploys to any account without editing anything. Nothing here is account-specific.

The construction lives in `build_app()` rather than at module level so the tests
can build the same tree and assert against it without synthesizing to disk.
"""

import os

import aws_cdk as cdk

from biofarm_infra.cicd_stack import CicdStack
from config import ENVIRONMENTS


def build_app(env: cdk.Environment | None = None) -> cdk.App:
    app = cdk.App()

    # Resolved from whichever profile the CLI is using. Deploying to a different
    # account is a matter of `--profile`, not a code change.
    aws_env = env or cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    )

    # Account-level and shared. An account may hold only one OIDC provider per
    # issuer URL, which is itself why this is not something each environment
    # creates for itself.
    CicdStack(app, "Biofarm-Cicd", env=aws_env)

    for _cfg in ENVIRONMENTS:
        # Per-environment stacks are added here as they are built. Tags go on
        # each stack rather than on the app - tagging the app inside this loop
        # would leave every stack carrying whichever environment it ended on.
        pass

    cdk.Tags.of(app).add("Project", "Biofarm")
    cdk.Tags.of(app).add("ManagedBy", "cdk")
    return app


if __name__ == "__main__":
    build_app().synth()
