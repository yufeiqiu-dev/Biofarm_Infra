"""Everything an environment stores: database, identity, images, credentials.

Split from AppStack on purpose. These resources hold state and outlive any
deployment of the application, so `cdk destroy` on the compute should never be
able to take the database with it.

Where credentials live, and why they live in two different places:

  - **The database password is in Secrets Manager.** RDS generates it there
    itself, and rotation is built in. The alternative is supplying a password,
    which means it exists somewhere in this repository or in CDK context and
    lands in the CloudFormation template in plaintext.
  - **Stripe keys are in SSM Parameter Store as SecureString.** App Runner reads
    either source, and standard SSM parameters are free where Secrets Manager is
    $0.40 per secret per month. Nothing here needs rotation or cross-account
    sharing, so the paid features would not be used.

Neither value is ever in this repository. CDK creates the SSM parameters with a
placeholder; the real values are written out of band. See the README.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct

from biofarm_infra.network_stack import NetworkStack
from config import EnvConfig

# Written by CDK so the parameter exists and App Runner can start; replaced out
# of band with the real key. Chosen to be obviously invalid rather than
# plausible - it fails the backend's own STRIPE_MODE check on sight.
PLACEHOLDER = "replace-me"

# Matches the local docker-compose Postgres, which is pinned to 16 for exactly
# this reason: a version difference between local and deployed only ever shows
# up as a bug that will not reproduce.
POSTGRES_VERSION = rds.PostgresEngineVersion.VER_16_4


class DataStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        cfg: EnvConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        # Created here rather than in AppStack because the database's ingress
        # rule needs to name it, and that rule is the primary isolation control
        # between environments. Keeping both in one stack means the pair cannot
        # drift apart across a partial deploy.
        self.app_security_group = ec2.SecurityGroup(
            self,
            "AppSecurityGroup",
            vpc=network.vpc,
            description=f"App Runner tasks for {cfg.name}",
            allow_all_outbound=True,
        )

        self._create_database(network, cfg)
        self._create_image_storage(cfg)
        self._create_user_pool(cfg)
        self._create_test_users(cfg)
        self._create_stripe_parameters(cfg)

    # --- database ---

    def _create_database(self, network: NetworkStack, cfg: EnvConfig) -> None:
        self.db_security_group = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=network.vpc,
            description=f"Postgres for {cfg.name}",
            allow_all_outbound=False,
        )

        # The single most important rule in this repository. Both environments
        # share one VPC, so this - together with the subnet ACLs in
        # NetworkStack - is what stops staging from reaching production data.
        # It names one security group; it must never be widened to a CIDR.
        # tests/test_data.py fails if it is.
        self.db_security_group.add_ingress_rule(
            peer=self.app_security_group,
            connection=ec2.Port.tcp(5432),
            description=f"Only {cfg.name} App Runner may reach this database",
        )

        self.database = rds.DatabaseInstance(
            self,
            "Database",
            # Named explicitly. Left to CloudFormation the identifier is
            # generated from the logical id and a random suffix, which is
            # unrecognisable in the console and - more to the point - cannot be
            # found by scripts/staging_power.py, whose whole job is to locate and
            # stop this instance. It would have reported "not found" and left the
            # database running, which is a silent bill rather than an error.
            instance_identifier=f"{cfg.resource_prefix}-db",
            engine=rds.DatabaseInstanceEngine.postgres(version=POSTGRES_VERSION),
            instance_type=ec2.InstanceType(cfg.db_instance_class),
            vpc=network.vpc,
            vpc_subnets=network.private_subnets_for(cfg.subnet_group),
            security_groups=[self.db_security_group],
            multi_az=cfg.db_multi_az,
            allocated_storage=20,
            max_allocated_storage=100,
            storage_encrypted=True,
            backup_retention=cdk.Duration.days(cfg.db_backup_retention_days),
            deletion_protection=cfg.db_deletion_protection,
            # Production keeps a final snapshot; staging is disposable, and a
            # forgotten snapshot per teardown is a slow storage bill.
            removal_policy=(
                cdk.RemovalPolicy.SNAPSHOT
                if cfg.db_deletion_protection
                else cdk.RemovalPolicy.DESTROY
            ),
            database_name="oasis",
            # Generated straight into Secrets Manager - never rendered into the
            # template, never in this repository, and rotatable in place.
            credentials=rds.Credentials.from_generated_secret(
                "biofarm",
                secret_name=f"{cfg.secret_prefix}/database",
            ),
            cloudwatch_logs_exports=["postgresql"],
            auto_minor_version_upgrade=True,
        )

    # --- product images ---

    def _create_image_storage(self, cfg: EnvConfig) -> None:
        """S3 behind CloudFront with Origin Access Control.

        The bucket stays private: the browser PUTs directly to S3 with a
        presigned URL, and reads come back through CloudFront. The backend never
        handles image bytes.
        """
        self.image_bucket = s3.Bucket(
            self,
            "ImageBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=(
                cdk.RemovalPolicy.RETAIN
                if cfg.db_deletion_protection
                else cdk.RemovalPolicy.DESTROY
            ),
            auto_delete_objects=not cfg.db_deletion_protection,
            cors=[
                s3.CorsRule(
                    # PUT only: this is the presigned upload path. Reads go
                    # through CloudFront, which needs no CORS grant from here.
                    allowed_methods=[s3.HttpMethods.PUT],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    max_age=3000,
                )
            ],
        )

        self.distribution = cloudfront.Distribution(
            self,
            "ImageDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                # S3BucketOrigin.with_origin_access_control replaces the older
                # origin access identity and writes the bucket policy itself.
                origin=origins.S3BucketOrigin.with_origin_access_control(self.image_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            # North America and Europe only. The catalogue is not global, and
            # this is the cheapest price class.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            comment=f"Biofarm product images ({cfg.name})",
        )

    # --- identity ---

    def _create_user_pool(self, cfg: EnvConfig) -> None:
        """A pool per environment, so staging sign-ins never touch real users."""
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{cfg.resource_prefix}-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=(
                cdk.RemovalPolicy.RETAIN
                if cfg.db_deletion_protection
                else cdk.RemovalPolicy.DESTROY
            ),
        )

        # Case-sensitive, and it must match require_admin in the backend and the
        # roles check in the frontend exactly.
        self.admin_group = cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="Admin",
            description="Grants access to the admin console.",
        )

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name=f"{cfg.resource_prefix}-web",
            # Public client: the SPA cannot hold a secret, and PKCE is what
            # protects the authorization code instead.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                # Callback URLs need the Amplify domain, which does not exist
                # until AppStack has deployed. AppStack adds them; see the
                # two-pass note in the README.
                callback_urls=["http://localhost:5174/auth/callback"],
                logout_urls=["http://localhost:5174/auth/callback"],
            ),
            prevent_user_existence_errors=True,
        )

        self.user_pool_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                # Must be globally unique across all of AWS. The account id keeps
                # it unique without needing a lookup or manual choice.
                domain_prefix=f"{cfg.resource_prefix}-{cdk.Aws.ACCOUNT_ID}",
            ),
        )

    # --- test users ---

    def _create_test_users(self, cfg: EnvConfig) -> None:
        """Accounts for the end-to-end suite. Staging only.

        CloudFormation cannot produce a usable account on its own.
        AWS::Cognito::UserPoolUser has no password property - not even a
        temporary one - so a user created by a deploy always lands in
        FORCE_CHANGE_PASSWORD, and the only API that can move it out of that
        state is AdminSetUserPassword. What the stack can do is create the
        account and generate a password beside it; scripts/seed_test_users.py
        makes the two agree afterwards.

        The password is generated by Secrets Manager rather than written here,
        so it never appears in this repository, in a template, or in `cdk diff`
        output - the same reasoning as the Stripe parameters above. Nothing
        grants the App Runner instance role access to these, so the running
        backend cannot read them either.
        """
        self.test_user_secrets: dict[str, secretsmanager.Secret] = {}

        for user in cfg.test_users:
            secret = secretsmanager.Secret(
                self,
                f"TestUser{user.name.title()}Secret",
                secret_name=f"{cfg.secret_prefix}/test-users/{user.name}",
                description=(
                    f"Password for the {cfg.name} end-to-end {user.name} account. "
                    f"Applied by scripts/seed_test_users.py."
                ),
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template=json.dumps({"email": user.email}),
                    generate_string_key="password",
                    password_length=24,
                    # The pool requires upper, lower and digits and does not
                    # require symbols. Excluding punctuation keeps the value
                    # safe to paste into a shell or a CI variable without
                    # quoting games, and require_each_included_type guarantees
                    # the policy is still satisfied.
                    exclude_punctuation=True,
                    require_each_included_type=True,
                ),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            self.test_user_secrets[user.name] = secret

            account = cognito.CfnUserPoolUser(
                self,
                f"TestUser{user.name.title()}",
                user_pool_id=self.user_pool.user_pool_id,
                username=user.email,
                # SUPPRESS because there is nobody to invite. Without it Cognito
                # tries to email a temporary password to an example.com address
                # that cannot receive it.
                message_action="SUPPRESS",
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=user.email
                    ),
                    # Marked verified so the account behaves like one that has
                    # been through sign-up, rather than being asked to confirm
                    # an address that cannot receive mail.
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
            )

            if user.admin:
                attachment = cognito.CfnUserPoolUserToGroupAttachment(
                    self,
                    f"TestUser{user.name.title()}AdminMembership",
                    user_pool_id=self.user_pool.user_pool_id,
                    username=user.email,
                    group_name="Admin",
                )
                # Both are needed. CloudFormation infers no ordering here, and
                # attaching before either the user or the group exists fails the
                # deploy.
                attachment.add_dependency(account)
                attachment.add_dependency(self.admin_group)

    # --- Stripe credentials ---

    def _create_stripe_parameters(self, cfg: EnvConfig) -> None:
        """Placeholders, written over out of band.

        SecureString cannot be created by CloudFormation, so these are String
        parameters holding a placeholder, and the real values are written with
        `aws ssm put-parameter --type SecureString --overwrite`, which converts
        them in place. That is deliberate: it means a real key never passes
        through a CloudFormation template, a `cdk diff`, or this repository.
        """
        self.stripe_parameters = {
            "STRIPE_SECRET_KEY": ssm.StringParameter(
                self,
                "StripeSecretKey",
                parameter_name=f"/{cfg.secret_prefix}/stripe-secret-key",
                string_value=PLACEHOLDER,
                description=f"Stripe {cfg.stripe_mode}-mode secret key. Overwrite as SecureString.",
            ),
            "STRIPE_WEBHOOK_SECRET": ssm.StringParameter(
                self,
                "StripeWebhookSecret",
                parameter_name=f"/{cfg.secret_prefix}/stripe-webhook-secret",
                string_value=PLACEHOLDER,
                description=(
                    f"Signing secret of the {cfg.stripe_mode}-mode webhook endpoint "
                    f"for this environment. Not the one `stripe listen` prints."
                ),
            ),
        }
