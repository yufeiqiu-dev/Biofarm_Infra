"""Everything that differs between staging and production.

The stacks read from here and contain no `if name == "prod"` branching of their
own. That is the point: when the two environments drift, they drift in one file
you can read top to bottom, rather than across four stacks.

Nothing here is a secret. Secret values live in Secrets Manager and reach the
application by ARN — see the DataStack and the README.
"""

from __future__ import annotations

from dataclasses import dataclass

# Both environments share one VPC, so each needs its own private subnet group.
# The names are load-bearing: NetworkStack creates subnet groups under these
# names and the NACLs isolate one from the other by selecting on them.
PROD_SUBNET_GROUP = "private-prod"
STAGING_SUBNET_GROUP = "private-staging"

VPC_CIDR = "10.0.0.0/16"

# The GitHub org/repos allowed to assume the deploy role. Anything not listed
# here cannot push an image or start a deployment.
GITHUB_OWNER = "yufeiqiu-dev"
BACKEND_REPO = "Biofarm_Backend"
FRONTEND_REPO = "Biofarm_Frontend"


@dataclass(frozen=True)
class EnvConfig:
    """One deployed environment."""

    name: str
    """Short name used in stack ids, resource names and tags: "staging" | "prod"."""

    app_env: str
    """The backend's APP_ENV. Both environments use "prod", because that is what
    arms the guard in Settings that refuses to boot with a bypass enabled - a
    staging environment running with AUTH_BYPASS on would be a public admin API
    with real infrastructure behind it. Staging is not a relaxed production."""

    branch: str
    """The git branch that deploys here, in both application repos."""

    subnet_group: str
    """Which private subnet group in the shared VPC this environment lives in."""

    db_instance_class: str
    """RDS instance class. Both are t4g.micro today; the field exists so
    production can grow without staging following it."""

    db_backup_retention_days: int

    db_deletion_protection: bool
    """Production must refuse `cdk destroy` on the database."""

    db_multi_az: bool

    apprunner_cpu: str
    apprunner_memory: str

    log_retention_days: int

    stripe_mode: str
    """"test" or "live", passed to the backend as STRIPE_MODE.

    The backend refuses to start if STRIPE_SECRET_KEY is from the other mode.
    Staging runs test mode with STRIPE_BYPASS=false, so it exercises the real
    webhook, capture and refund paths without money moving.

    Note that Stripe webhook endpoints are per-mode: each environment needs its
    own endpoint created in the matching mode, and each issues its own signing
    secret. They are not interchangeable.
    """

    stopped_when_idle: bool
    """Staging is powered down outside active testing (see scripts/staging_power.py).

    Worth knowing before relying on it: AWS force-starts a stopped RDS instance
    after 7 days, so this is a recurring schedule rather than a one-off action.
    """

    @property
    def resource_prefix(self) -> str:
        return f"biofarm-{self.name}"

    @property
    def secret_prefix(self) -> str:
        """Secrets Manager path prefix. The App Runner instance role is scoped to
        exactly this prefix, so one environment cannot read another's secrets."""
        return f"biofarm/{self.name}"


STAGING = EnvConfig(
    name="staging",
    app_env="prod",
    branch="staging",
    subnet_group=STAGING_SUBNET_GROUP,
    db_instance_class="t4g.micro",
    db_backup_retention_days=1,
    db_deletion_protection=False,
    db_multi_az=False,
    apprunner_cpu="0.25 vCPU",
    apprunner_memory="0.5 GB",
    log_retention_days=7,
    stripe_mode="test",
    stopped_when_idle=True,
)

PROD = EnvConfig(
    name="prod",
    app_env="prod",
    branch="main",
    subnet_group=PROD_SUBNET_GROUP,
    db_instance_class="t4g.micro",
    db_backup_retention_days=7,
    db_deletion_protection=True,
    db_multi_az=False,
    apprunner_cpu="0.25 vCPU",
    apprunner_memory="0.5 GB",
    log_retention_days=30,
    stripe_mode="live",
    stopped_when_idle=False,
)

ENVIRONMENTS = (STAGING, PROD)
