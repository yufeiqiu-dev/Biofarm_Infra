#!/usr/bin/env python3
"""CDK entry point.

    cdk synth                          # both environments, offline
    cdk diff  --profile <sso-profile>  # against a real account
    cdk deploy Biofarm-Network Biofarm-Data-staging Biofarm-App-staging --profile <sso-profile>

The stacks are environment-agnostic: no account or region is baked in, so the
same code deploys to whichever account the CLI profile points at. Standing up a
new account is `--profile`, not an edit.

The construction lives in `build_app()` rather than at module level so the tests
can build the same tree and assert against it without synthesizing to disk.
"""

import aws_cdk as cdk

from biofarm_infra.cicd_stack import CicdStack
from biofarm_infra.network_stack import NetworkStack
from config import ENVIRONMENTS


def build_app(env: cdk.Environment | None = None) -> cdk.App:
    app = cdk.App()

    # Environment-agnostic by default, and that is the whole point of this repo:
    # a stack with no account or region baked in deploys to whichever account the
    # CLI profile points at, so a new account needs `--profile`, not an edit.
    #
    # It also keeps `cdk synth` and the tests completely offline. Pinning an
    # account makes CDK resolve availability zones by calling EC2, so synth then
    # needs credentials, permissions, and a network - and fails in CI for reasons
    # that have nothing to do with the change being tested.
    #
    # The cost of this choice: context lookups (Vpc.from_lookup, hosted zones,
    # AMI lookups) are unavailable, because they need a real account to read. No
    # stack here uses one; the NAT AMI resolves through an SSM parameter at
    # deploy time rather than a lookup at synth time.
    aws_env = env

    # Account-level and shared. An account may hold only one OIDC provider per
    # issuer URL, which is itself why this is not something each environment
    # creates for itself.
    CicdStack(app, "Biofarm-Cicd", env=aws_env)

    # One VPC for every environment. See network_stack.py for what that costs
    # and what is done to make it safe.
    NetworkStack(app, "Biofarm-Network", env=aws_env)

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
