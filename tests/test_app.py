"""The running service: configuration, permissions, and the scheduled job.

Most of these guard against a deployment that comes up healthy and is wrong.
An App Runner service with AUTH_BYPASS enabled serves the entire admin API to
anyone who finds the URL, and nothing about the service's own status would say
so. The backend refuses to start in that state; these assert the infrastructure
never asks it to.
"""

from __future__ import annotations

import json

import pytest

from config import ENVIRONMENTS


@pytest.fixture(scope="module")
def apps(templates) -> dict:
    return {cfg.name: templates[f"Biofarm-App-{cfg.name}"] for cfg in ENVIRONMENTS}


def _service(template) -> dict:
    services = template.find_resources("AWS::AppRunner::Service")
    assert len(services) == 1
    return next(iter(services.values()))["Properties"]


def _env(template) -> dict:
    image = _service(template)["SourceConfiguration"]["ImageRepository"]["ImageConfiguration"]
    return {v["Name"]: v["Value"] for v in image["RuntimeEnvironmentVariables"]}


def _secret_names(template) -> set[str]:
    image = _service(template)["SourceConfiguration"]["ImageRepository"]["ImageConfiguration"]
    return {v["Name"] for v in image["RuntimeEnvironmentSecrets"]}


# --- the bypasses ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_neither_bypass_is_ever_enabled(apps, env_name):
    """AUTH_BYPASS makes every unauthenticated request an admin; STRIPE_BYPASS
    creates orders without charging. Both are silent."""
    env = _env(apps[env_name])
    assert env["AUTH_BYPASS"] == "false"
    assert env["STRIPE_BYPASS"] == "false"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_app_env_arms_the_backend_guard(apps, env_name):
    """APP_ENV=prod is what makes Settings refuse to boot with a bypass on.
    Staging uses it too - a staging environment serving a public admin API over
    real infrastructure is not a useful staging environment."""
    assert _env(apps[env_name])["APP_ENV"] == "prod"


def test_the_stripe_modes_differ_between_environments(apps):
    """Live keys in staging charge real cards; test keys in production take no
    money at all. The backend refuses to start if the key disagrees with this."""
    assert _env(apps["staging"])["STRIPE_MODE"] == "test"
    assert _env(apps["prod"])["STRIPE_MODE"] == "live"


# --- credentials never appear as plain values ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_credentials_are_references_not_values(apps, env_name):
    """Anything in RuntimeEnvironmentVariables is plaintext in the template, in
    `cdk diff`, and in the console."""
    assert _secret_names(apps[env_name]) == {
        "DB_PASSWORD",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
    }

    plain = _env(apps[env_name])
    for name in ("DB_PASSWORD", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        assert name not in plain, f"{name} is a plaintext environment variable"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_only_the_password_field_of_the_database_secret_is_injected(apps, env_name):
    """The same secret also holds host, port and username, which are already
    plain variables. Injecting the whole thing would hand the container more
    than it needs for no benefit."""
    image = _service(apps[env_name])["SourceConfiguration"]["ImageRepository"][
        "ImageConfiguration"
    ]
    password = next(
        v for v in image["RuntimeEnvironmentSecrets"] if v["Name"] == "DB_PASSWORD"
    )
    rendered = json.dumps(password["Value"])
    assert ":password::" in rendered, rendered


# --- the database connection ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_database_parts_are_all_present(apps, env_name):
    """The backend assembles DATABASE_URL from these. A missing one fails at
    container start, where the only diagnostic is the error message."""
    env = _env(apps[env_name])
    for name in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER"):
        assert name in env, name
    assert "DATABASE_URL" not in env, "the URL cannot be set here; the password is a secret"


# --- Cognito wiring ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_cognito_client_id_is_a_reference_not_a_copied_string(apps, env_name):
    """A mismatch rejects every authenticated request with 'Token was not issued
    for this application', which reads like a broken login rather than a typo.
    Taking it from the construct makes the mismatch impossible."""
    value = _env(apps[env_name])["COGNITO_USER_POOL_CLIENT_ID"]
    assert isinstance(value, dict), f"hardcoded client id: {value!r}"


def test_the_environments_use_different_user_pools(apps):
    staging = json.dumps(_env(apps["staging"])["COGNITO_USER_POOL_ID"], sort_keys=True)
    prod = json.dumps(_env(apps["prod"])["COGNITO_USER_POOL_ID"], sort_keys=True)
    # Both are Fn::ImportValue of a per-environment export, so the export names
    # differ even though the shape is identical.
    assert staging != prod


# --- health checks ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_health_check_touches_the_database(apps, env_name):
    """/health is shallow and answers ok unconditionally. Pointing App Runner at
    it would keep a service with a dead database in rotation, which is exactly
    what splitting the two endpoints was for."""
    path = _service(apps[env_name])["HealthCheckConfiguration"]["Path"]
    assert path == "/api/v1/health/ready"


# --- deployment ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_deployments_are_not_triggered_by_an_image_push(apps, env_name):
    """Both environments share one ECR repository. Auto-deploy would ship
    whatever landed under the tag, including a push meant for the other one."""
    source = _service(apps[env_name])["SourceConfiguration"]
    assert source["AutoDeploymentsEnabled"] is False


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_egress_goes_through_this_environments_vpc(apps, env_name):
    network = _service(apps[env_name])["NetworkConfiguration"]
    assert network["EgressConfiguration"]["EgressType"] == "VPC"
    apps[env_name].resource_count_is("AWS::AppRunner::VpcConnector", 1)


# --- the scheduled sweep ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_cleanup_job_is_scheduled(apps, env_name):
    """P1-3: the job existed as a CLI entry point with nothing calling it."""
    schedules = apps[env_name].find_resources("AWS::Scheduler::Schedule")
    assert len(schedules) == 1
    assert "cron(" in next(iter(schedules.values()))["Properties"]["ScheduleExpression"]


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_cleanup_job_runs_the_same_image_as_the_service(apps, env_name):
    """Not a second code path, and not a new authenticated endpoint into the
    data - the same container with the command overridden."""
    task = next(iter(apps[env_name].find_resources("AWS::ECS::TaskDefinition").values()))
    container = task["Properties"]["ContainerDefinitions"][0]
    assert container["Command"] == ["python", "-m", "app.jobs.cleanup"]


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_cleanup_task_has_no_public_address(apps, env_name):
    schedule = next(iter(apps[env_name].find_resources("AWS::Scheduler::Schedule").values()))
    vpc_config = schedule["Properties"]["Target"]["EcsParameters"]["NetworkConfiguration"][
        "AwsvpcConfiguration"
    ]
    assert vpc_config["AssignPublicIp"] == "DISABLED"


# --- permissions ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_no_policy_in_the_stack_grants_a_wildcard_action(apps, env_name):
    for logical_id, policy in apps[env_name].find_resources("AWS::IAM::Policy").items():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            assert "*" not in actions, f"{logical_id}: {actions}"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_container_cannot_list_the_whole_bucket(apps, env_name):
    """The application never lists it - checked against the real bucket, where
    the local IAM user has no ListBucket and every path still works."""
    rendered = json.dumps(apps[env_name].to_json())
    assert "s3:ListBucket" not in rendered


# --- log retention ---

def test_log_retention_differs_per_environment(apps):
    """This was silently broken: the enum was looked up by a constructed
    attribute name that never matched, so every environment fell through to the
    same default and the per-environment setting did nothing. A config value that
    is quietly ignored is worse than one that is wrong."""
    from config import PROD, STAGING

    def retention(template):
        groups = template.find_resources("AWS::Logs::LogGroup")
        assert groups, "expected a log group for the cleanup task"
        return next(iter(groups.values()))["Properties"]["RetentionInDays"]

    assert retention(apps["staging"]) == STAGING.log_retention_days
    assert retention(apps["prod"]) == PROD.log_retention_days
    assert retention(apps["staging"]) != retention(apps["prod"])


def test_an_unsupported_retention_is_refused_rather_than_defaulted():
    """CloudWatch accepts only a fixed set of periods. Falling back to a default
    is how the original bug stayed invisible."""
    import pytest as _pytest

    from biofarm_infra.app_stack import log_retention

    with _pytest.raises(ValueError, match="does not accept"):
        log_retention(11)


# --- transactional email ---

def _instance_role_statements(template) -> list[dict]:
    """Policy statements attached to the App Runner instance role."""
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _ses_statements(template) -> list[dict]:
    out = []
    for statement in _instance_role_statements(template):
        actions = statement.get("Action", [])
        actions = actions if isinstance(actions, list) else [actions]
        if any(str(a).startswith("ses:") for a in actions):
            out.append(statement)
    return out


@pytest.mark.parametrize("cfg", ENVIRONMENTS, ids=lambda c: c.name)
def test_the_backend_can_send_its_own_mail_and_nobody_elses(templates, cfg):
    """A wildcard ses:SendEmail lets a compromised container send as any
    identity in the account - for a domain identity, every address at the
    company. The From address is pinned instead, so staging cannot send mail
    that appears to come from production."""
    statements = _ses_statements(templates[f"Biofarm-App-{cfg.name}"])
    assert statements, "the instance role cannot send email at all"

    for statement in statements:
        condition = statement.get("Condition", {})
        pinned = condition.get("StringEquals", {}).get("ses:FromAddress")
        assert pinned == cfg.email_from, statement


@pytest.mark.parametrize("cfg", ENVIRONMENTS, ids=lambda c: c.name)
def test_neither_environment_runs_with_email_bypassed(templates, cfg):
    """The backend refuses to boot with it on under APP_ENV=prod, which both
    environments run - so this failing means a deploy that will not start."""
    template = templates[f"Biofarm-App-{cfg.name}"]
    service = list(template.find_resources("AWS::AppRunner::Service").values())[0]
    env = service["Properties"]["SourceConfiguration"]["ImageRepository"][
        "ImageConfiguration"
    ]["RuntimeEnvironmentVariables"]
    values = {pair["Name"]: pair["Value"] for pair in env}

    assert values["EMAIL_BYPASS"] == "false"
    assert values["EMAIL_FROM"] == cfg.email_from
