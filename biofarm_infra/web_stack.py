"""Frontend hosting: an Amplify app that builds the branch and serves it.

**The GitHub token is supplied at deploy time, not stored here.**

`AWS::Amplify::App` requires `AccessToken` or `OauthToken` when the app is
created - there is no declarative way to connect a repository. The obvious idea,
holding the token in an SSM SecureString and referencing it with
`{{resolve:ssm-secure:...}}`, does not work: that pattern is supported on an
allowlist of eleven resource properties (DirectoryService, ElastiCache, IAM,
Firehose, OpsWorks, RDS, Redshift) and Amplify is not among them. CloudFormation
would store the reference string verbatim as the token.

So it is a NoEcho stack parameter, passed on the deploy command:

    npx aws-cdk deploy Biofarm-Web-staging --profile <p> \\
        --parameters GitHubAccessToken=$GITHUB_TOKEN

NoEcho keeps it out of the console, stack events and describe-stacks, and Amplify
itself does not retain it - the token authorizes the Amplify GitHub App once. Read
it from an environment variable rather than typing it, so it stays out of shell
history too.

One Amplify app per environment rather than one app with two branches. It matches
how every other resource here is arranged, and it means a staging build cannot
touch the production app at all.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_amplify as amplify
from constructs import Construct

from biofarm_infra.app_stack import AppStack
from biofarm_infra.data_stack import DataStack
from config import EnvConfig, FRONTEND_REPO, GITHUB_OWNER

# Vite writes to dist/. npm ci rather than npm install so the lockfile is
# authoritative - a build that quietly resolves a different dependency tree than
# the one tested is the thing this avoids.
BUILD_SPEC = """version: 1
frontend:
  phases:
    preBuild:
      commands:
        - npm ci
    build:
      commands:
        - npm run build
  artifacts:
    baseDirectory: dist
    files:
      - '**/*'
  cache:
    paths:
      - node_modules/**/*
"""


class WebStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        application: AppStack,
        cfg: EnvConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        token = cdk.CfnParameter(
            self,
            "GitHubAccessToken",
            type="String",
            no_echo=True,
            description=(
                "GitHub personal access token authorizing the Amplify GitHub App. "
                "Used once at creation and not retained by Amplify."
            ),
        )

        app = amplify.CfnApp(
            self,
            "App",
            name=f"{cfg.resource_prefix}-frontend",
            repository=f"https://github.com/{GITHUB_OWNER}/{FRONTEND_REPO}",
            access_token=token.value_as_string,
            platform="WEB",
            build_spec=BUILD_SPEC,
            custom_rules=[
                # Without this every deep link 404s on refresh. The router is
                # client-side, so /orders/<id> is not a file Amplify can serve;
                # 404-200 rewrites it to index.html and lets React Router read
                # the path. Reloading an order page is the first thing anyone
                # does, so this is not an edge case.
                amplify.CfnApp.CustomRuleProperty(
                    source="</^[^.]+$|\\.(?!(css|gif|ico|jpg|js|png|txt|svg|woff|woff2|ttf|map|json|webp)$)([^.]+$)/>",
                    target="/index.html",
                    status="200",
                )
            ],
        )

        amplify.CfnBranch(
            self,
            "Branch",
            app_id=app.attr_app_id,
            branch_name=cfg.branch,
            stage="PRODUCTION" if cfg.name == "prod" else "BETA",
            enable_auto_build=True,
            environment_variables=self._environment(data, application, app),
        )

        self.default_domain = f"{cfg.branch}.{app.attr_default_domain}"

        cdk.CfnOutput(
            self,
            "FrontendUrl",
            value=f"https://{self.default_domain}",
            description="Add this to CORS_ORIGINS on the backend, then deploy again.",
        )

    def _environment(
        self, data: DataStack, application: AppStack, app: amplify.CfnApp
    ) -> list:
        """The VITE_* values, baked into the bundle at build time.

        All of these end up in JavaScript the browser downloads, so none of them
        can be secret - including the Stripe publishable key, which is designed
        to be public. The secret key never comes near this stack.

        Taken from constructs rather than copied strings, for the same reason as
        COGNITO_USER_POOL_CLIENT_ID in AppStack: a copied value that drifts
        rejects every authenticated request with an error that reads like a
        broken login rather than a typo.
        """
        cfg = self.cfg
        # The branch's own URL, referenced from the app resource in this stack
        # rather than guessed. It also has to be registered as a Cognito callback
        # URL, which is a console step - see the README.
        origin = f"https://{cfg.branch}.{app.attr_default_domain}"
        values = {
            "VITE_API_BASE_URL": f"https://{application.service.attr_service_url}/api/v1",
            "VITE_COGNITO_USER_POOL_ID": data.user_pool.user_pool_id,
            "VITE_COGNITO_USER_POOL_CLIENT_ID": data.user_pool_client.user_pool_client_id,
            # Host only. Amplify's auth library prepends https://, and including
            # the scheme produces a malformed redirect that fails silently.
            "VITE_COGNITO_DOMAIN": f"{data.user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
            "VITE_COGNITO_REDIRECT_SIGN_IN": f"{origin}/auth/callback",
            "VITE_COGNITO_REDIRECT_SIGN_OUT": f"{origin}/auth/callback",
            "VITE_STRIPE_BYPASS": "false",
            # Publishable, not secret - it ships inside the bundle by design.
            # Written by hand after the first deploy; see the README.
            "VITE_STRIPE_PUBLISHABLE_KEY": "",
        }
        return [
            amplify.CfnBranch.EnvironmentVariableProperty(name=name, value=value)
            for name, value in values.items()
        ]
