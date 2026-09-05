"""The running application: App Runner, its network path, and the daily sweep.

Built on the L1 `CfnService` rather than the L2. `aws-cdk.aws-apprunner-alpha` is
still an alpha module - 2.268.0a0, "Development Status :: 4 - Beta" - and its API
can change between releases. This repository exists to still work when it is run
against a new account much later, so it should not depend on anything that moves.
The cost is verbosity; the L1 maps one-to-one onto CloudFormation and will not
change under it.

Two things here are worth understanding before changing them:

**The service is not reachable from the internet on any port it chooses.** App
Runner terminates TLS and forwards to the container port; the VPC connector
governs *outbound* traffic only. Everything inbound is App Runner's own edge.

**Two values cannot exist on the first deploy.** CORS_ORIGINS needs the Amplify
domain, and Amplify's VITE_API_BASE_URL needs the App Runner domain. They are
circular, so the first deploy leaves both at their defaults and a second fills
them in. See the README.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    aws_scheduler as scheduler,
    aws_apprunner as apprunner,
)
from constructs import Construct

from biofarm_infra.cicd_stack import CicdStack
from biofarm_infra.data_stack import DataStack
from biofarm_infra.network_stack import NetworkStack
from config import EnvConfig

CONTAINER_PORT = "8000"

# CloudWatch only accepts a fixed set of retention periods, and CDK names them in
# words rather than numbers. Looking the enum up by a constructed attribute name
# silently returned None for every value and fell through to a default, so the
# per-environment setting did nothing at all - staging kept a month of logs it was
# configured not to keep. An explicit map, and a KeyError rather than a fallback,
# because that is precisely what hid the bug.
LOG_RETENTION = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def log_retention(days: int) -> logs.RetentionDays:
    try:
        return LOG_RETENTION[days]
    except KeyError:
        raise ValueError(
            f"CloudWatch does not accept a {days}-day retention. "
            f"Valid values here: {sorted(LOG_RETENTION)}"
        ) from None

# The readiness endpoint, which runs SELECT 1. Pointing App Runner at the shallow
# /health instead would keep a service with a dead database in rotation, which is
# the exact thing splitting the two endpoints was for.
HEALTH_CHECK_PATH = "/api/v1/health/ready"


class AppStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        data: DataStack,
        cicd: CicdStack,
        cfg: EnvConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        instance_role = self._instance_role(data, cfg)
        connector = self._vpc_connector(network, data, cfg)

        self.service = apprunner.CfnService(
            self,
            "Service",
            service_name=f"{cfg.resource_prefix}-backend",
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                # Deployments are started by CI after it pushes a new image, not
                # by ECR events. An automatic trigger would deploy whatever
                # landed under the tag, including a push that was not meant for
                # this environment.
                auto_deployments_enabled=False,
                authentication_configuration=apprunner.CfnService.AuthenticationConfigurationProperty(
                    access_role_arn=self._ecr_access_role(cicd).role_arn,
                ),
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=f"{cicd.repository.repository_uri}:{cfg.name}",
                    image_repository_type="ECR",
                    image_configuration=apprunner.CfnService.ImageConfigurationProperty(
                        port=CONTAINER_PORT,
                        runtime_environment_variables=self._environment(data, cfg),
                        runtime_environment_secrets=self._secrets(data),
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu=cfg.apprunner_cpu,
                memory=cfg.apprunner_memory,
                instance_role_arn=instance_role.role_arn,
            ),
            network_configuration=apprunner.CfnService.NetworkConfigurationProperty(
                egress_configuration=apprunner.CfnService.EgressConfigurationProperty(
                    egress_type="VPC",
                    vpc_connector_arn=connector.attr_vpc_connector_arn,
                ),
            ),
            health_check_configuration=apprunner.CfnService.HealthCheckConfigurationProperty(
                protocol="HTTP",
                path=HEALTH_CHECK_PATH,
                interval=10,
                timeout=5,
                healthy_threshold=1,
                unhealthy_threshold=5,
            ),
        )

        # A standalone Policy owned by this stack, rather than
        # cicd.deploy_role.add_to_policy(). That method appends to the role's
        # default policy, which lives in CicdStack - and since this stack already
        # reads the ECR repository from CicdStack, it would make the two stacks
        # depend on each other. CloudFormation has no way to deploy a cycle.
        iam.Policy(
            self,
            "DeployPolicy",
            statements=[
                iam.PolicyStatement(
                    sid=f"Deploy{cfg.name.capitalize()}",
                    actions=["apprunner:StartDeployment", "apprunner:DescribeService"],
                    resources=[self.service.attr_service_arn],
                )
            ],
            roles=[cicd.deploy_role],
        )

        self._schedule_cleanup(network, data, cicd, cfg)

        cdk.CfnOutput(
            self,
            "ServiceUrl",
            value=f"https://{self.service.attr_service_url}",
            description="Backend base URL. Feed this to the frontend as VITE_API_BASE_URL.",
        )

    # --- identity ---

    def _ecr_access_role(self, cicd: CicdStack) -> iam.Role:
        """App Runner's own pull role, distinct from the instance role.

        The build service pulls the image; the running task never does. Keeping
        them apart means the container cannot read the repository at runtime.
        """
        role = iam.Role(
            self,
            "EcrAccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
            description="Lets App Runner pull the backend image.",
        )
        cicd.repository.grant_pull(role)
        return role

    def _instance_role(self, data: DataStack, cfg: EnvConfig) -> iam.Role:
        """What the running container may do.

        Everything is scoped to this environment's own resources. That is what
        stops staging reading production's Stripe keys or writing to its bucket,
        and it is the reason the SSM parameters are namespaced per environment.
        """
        role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
            description=f"Runtime permissions for the {cfg.name} backend.",
        )

        # Object-level only. The application never lists the bucket - verified
        # against the real one, where the local IAM user has no ListBucket and
        # every code path still works - so granting it would be permission the
        # code does not use.
        data.image_bucket.grant_read_write(role, "products/*")

        data.database.secret.grant_read(role)
        for parameter in data.stripe_parameters.values():
            parameter.grant_read(role)

        return role

    # --- network path ---

    def _vpc_connector(
        self, network: NetworkStack, data: DataStack, cfg: EnvConfig
    ) -> apprunner.CfnVpcConnector:
        """Puts the service's outbound traffic inside this environment's subnets.

        Note what this does *not* do: it does not make the service private. It
        routes egress through the VPC, which is what lets the container reach a
        database with no public address - and is also why the NAT exists, since
        Stripe and Cognito are then reached the long way round.
        """
        subnets = network.vpc.select_subnets(subnet_group_name=cfg.subnet_group)
        return apprunner.CfnVpcConnector(
            self,
            "VpcConnector",
            subnets=subnets.subnet_ids,
            security_groups=[data.app_security_group.security_group_id],
            vpc_connector_name=f"{cfg.resource_prefix}-connector",
        )

    # --- configuration ---

    def _environment(self, data: DataStack, cfg: EnvConfig) -> list:
        """Plain environment variables: everything that is not a credential.

        APP_ENV is "prod" in both environments deliberately. It is what arms the
        guard in the backend's Settings that refuses to boot with a bypass
        enabled, and a staging environment serving a public admin API over real
        infrastructure would not be a useful staging environment.
        """
        values = {
            "APP_ENV": cfg.app_env,
            "AUTH_BYPASS": "false",
            "STRIPE_BYPASS": "false",
            "STRIPE_MODE": cfg.stripe_mode,
            "DB_HOST": data.database.db_instance_endpoint_address,
            "DB_PORT": data.database.db_instance_endpoint_port,
            "DB_NAME": "oasis",
            "DB_USER": "biofarm",
            "COGNITO_REGION": self.region,
            "COGNITO_USER_POOL_ID": data.user_pool.user_pool_id,
            # Taken from the construct, not copied as a string. A mismatch here
            # rejects every authenticated request with "Token was not issued for
            # this application", which reads like a broken login rather than a
            # typo.
            "COGNITO_USER_POOL_CLIENT_ID": data.user_pool_client.user_pool_client_id,
            "AWS_REGION": self.region,
            "S3_BUCKET_NAME": data.image_bucket.bucket_name,
            "CLOUDFRONT_URL": f"https://{data.distribution.distribution_domain_name}",
            # Filled in on the second deploy, once Amplify has a domain.
            "CORS_ORIGINS": '["http://localhost:5174"]',
        }
        return [
            apprunner.CfnService.KeyValuePairProperty(name=name, value=value)
            for name, value in values.items()
        ]

    def _secrets(self, data: DataStack) -> list:
        """Credentials, referenced by ARN so no value reaches this template.

        The database password is extracted from its JSON field rather than
        injecting the whole secret: the container needs one value, and the same
        secret also holds the host, port and username, which are already plain
        environment variables above.
        """
        return [
            apprunner.CfnService.KeyValuePairProperty(
                name="DB_PASSWORD",
                value=f"{data.database.secret.secret_arn}:password::",
            ),
            *(
                apprunner.CfnService.KeyValuePairProperty(
                    name=name, value=parameter.parameter_arn
                )
                for name, parameter in data.stripe_parameters.items()
            ),
        ]

    # --- the daily sweep ---

    def _schedule_cleanup(
        self, network: NetworkStack, data: DataStack, cicd: CicdStack, cfg: EnvConfig
    ) -> None:
        """Runs `python -m app.jobs.cleanup` once a day.

        App Runner has no scheduled-task facility, so this runs the same image as
        a Fargate task with the command overridden. Deliberately not a new
        authenticated endpoint on the service: that would be a second way into
        the data, reachable from the internet, for a housekeeping job.

        The cluster is free and a run costs about a cent. (Closes P1-3.)
        """
        cluster = ecs.Cluster(
            self, "JobCluster", vpc=network.vpc, cluster_name=f"{cfg.resource_prefix}-jobs"
        )

        task = ecs.FargateTaskDefinition(
            self, "CleanupTask", cpu=256, memory_limit_mib=512
        )
        task.add_container(
            "cleanup",
            image=ecs.ContainerImage.from_ecr_repository(cicd.repository, cfg.name),
            command=["python", "-m", "app.jobs.cleanup"],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="cleanup",
                log_retention=log_retention(cfg.log_retention_days),
            ),
            environment={
                "DB_HOST": data.database.db_instance_endpoint_address,
                "DB_PORT": data.database.db_instance_endpoint_port,
                "DB_NAME": "oasis",
                "DB_USER": "biofarm",
                "APP_ENV": cfg.app_env,
                "AUTH_BYPASS": "false",
                "STRIPE_BYPASS": "false",
                "STRIPE_MODE": cfg.stripe_mode,
                "COGNITO_REGION": self.region,
                "COGNITO_USER_POOL_ID": data.user_pool.user_pool_id,
                "COGNITO_USER_POOL_CLIENT_ID": data.user_pool_client.user_pool_client_id,
                "AWS_REGION": self.region,
                "S3_BUCKET_NAME": data.image_bucket.bucket_name,
                "CLOUDFRONT_URL": f"https://{data.distribution.distribution_domain_name}",
            },
            secrets={
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    data.database.secret, "password"
                ),
                **{
                    name: ecs.Secret.from_ssm_parameter(parameter)
                    for name, parameter in data.stripe_parameters.items()
                },
            },
        )

        scheduler_role = iam.Role(
            self,
            "CleanupSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description=f"Lets EventBridge Scheduler run the {cfg.name} cleanup task.",
        )
        task.grant_run(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "CleanupSchedule",
            name=f"{cfg.resource_prefix}-cleanup",
            description="Sweeps checkout sessions that never became orders.",
            schedule_expression="cron(30 4 * * ? *)",
            schedule_expression_timezone="UTC",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="FLEXIBLE", maximum_window_in_minutes=60
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=cluster.cluster_arn,
                role_arn=scheduler_role.role_arn,
                ecs_parameters=scheduler.CfnSchedule.EcsParametersProperty(
                    task_definition_arn=task.task_definition_arn,
                    launch_type="FARGATE",
                    task_count=1,
                    network_configuration=scheduler.CfnSchedule.NetworkConfigurationProperty(
                        awsvpc_configuration=scheduler.CfnSchedule.AwsVpcConfigurationProperty(
                            subnets=network.vpc.select_subnets(
                                subnet_group_name=cfg.subnet_group
                            ).subnet_ids,
                            security_groups=[data.app_security_group.security_group_id],
                            assign_public_ip="DISABLED",
                        )
                    ),
                ),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=2,
                    maximum_event_age_in_seconds=3600,
                ),
            ),
        )
