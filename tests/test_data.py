"""Stored state: database, identity, images, credentials.

The first section is the one that matters most. Staging and production share a
VPC, so a single security group rule is what stands between a staging deployment
and production's customer orders. If `test_the_database_accepts_only_its_own_environment`
starts failing, something has widened that rule and the answer is not to widen
the test.
"""

from __future__ import annotations

import json

import pytest

from config import ENVIRONMENTS, PROD, STAGING


@pytest.fixture(scope="module")
def data(templates) -> dict:
    return {cfg.name: templates[f"Biofarm-Data-{cfg.name}"] for cfg in ENVIRONMENTS}


def _ingress_rules(template) -> list[dict]:
    """Ingress declared inline on a group and as standalone rules alike."""
    rules = []
    for group in template.find_resources("AWS::EC2::SecurityGroup").values():
        rules.extend(group["Properties"].get("SecurityGroupIngress", []))
    for rule in template.find_resources("AWS::EC2::SecurityGroupIngress").values():
        rules.append(rule["Properties"])
    return rules


# --- isolation between environments ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_database_accepts_only_its_own_environment(data, env_name):
    """Exactly one ingress rule on 5432, and it names a security group rather
    than a CIDR. A CIDR here would reach across the shared VPC."""
    postgres_rules = [r for r in _ingress_rules(data[env_name]) if r.get("FromPort") == 5432]

    assert len(postgres_rules) == 1, postgres_rules
    rule = postgres_rules[0]

    assert "SourceSecurityGroupId" in rule, (
        "database ingress is not scoped to a security group"
    )
    for cidr_key in ("CidrIp", "CidrIpv6"):
        assert cidr_key not in rule, f"database reachable by {cidr_key}: {rule}"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_nothing_is_open_to_the_internet(data, env_name):
    for rule in _ingress_rules(data[env_name]):
        assert rule.get("CidrIp") != "0.0.0.0/0", rule
        assert rule.get("CidrIpv6") != "::/0", rule


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_database_trusts_a_group_from_its_own_stack(data, env_name):
    """Both stacks come from the same code and therefore share logical ids -
    comparing those across stacks proves nothing, since a logical id is only
    unique within one stack. What matters is the reference *shape*: Fn::GetAtt
    resolves to a resource created here, while Fn::ImportValue would mean the two
    environments had been pointed at one shared security group, which is exactly
    the failure this suite exists to catch.
    """
    template = data[env_name]
    rule = next(r for r in _ingress_rules(template) if r.get("FromPort") == 5432)
    source = rule["SourceSecurityGroupId"]

    assert isinstance(source, dict), source
    assert "Fn::ImportValue" not in source, (
        "the database trusts a security group imported from another stack"
    )
    assert "Fn::GetAtt" in source, source

    logical_id = source["Fn::GetAtt"][0]
    assert logical_id in template.to_json()["Resources"], (
        f"{logical_id} is not defined in this stack"
    )


# --- the database ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_database_is_not_publicly_accessible(data, env_name):
    instances = data[env_name].find_resources("AWS::RDS::DBInstance")
    for instance in instances.values():
        assert instance["Properties"].get("PubliclyAccessible") in (False, None)


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_storage_is_encrypted(data, env_name):
    instances = data[env_name].find_resources("AWS::RDS::DBInstance")
    for instance in instances.values():
        assert instance["Properties"]["StorageEncrypted"] is True


def test_production_refuses_to_be_deleted(data):
    """Staging is disposable on purpose. Production must survive a careless
    `cdk destroy`, which is a single command with no second confirmation."""
    prod = next(iter(data["prod"].find_resources("AWS::RDS::DBInstance").values()))
    assert prod["Properties"]["DeletionProtection"] is True
    assert prod["DeletionPolicy"] == "Snapshot"

    staging = next(iter(data["staging"].find_resources("AWS::RDS::DBInstance").values()))
    assert staging["Properties"]["DeletionProtection"] is False


def test_the_engine_matches_the_version_used_locally(data):
    """Local development runs Postgres 16 in Docker, pinned for this reason: a
    version difference between local and deployed only ever surfaces as a bug
    that will not reproduce."""
    for env_name in data:
        instance = next(iter(data[env_name].find_resources("AWS::RDS::DBInstance").values()))
        assert instance["Properties"]["Engine"] == "postgres"
        assert str(instance["Properties"]["EngineVersion"]).startswith("16")


def test_production_keeps_a_week_of_backups(data):
    prod = next(iter(data["prod"].find_resources("AWS::RDS::DBInstance").values()))
    assert prod["Properties"]["BackupRetentionPeriod"] == PROD.db_backup_retention_days
    assert PROD.db_backup_retention_days >= 7


# --- credentials ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_database_password_is_generated_not_written(data, env_name):
    """A password supplied from this repository would appear in the template, in
    `cdk diff` output, and in the console. RDS generating it into Secrets Manager
    means it exists nowhere a human has typed it."""
    secrets = data[env_name].find_resources("AWS::SecretsManager::Secret")
    assert secrets, "expected a generated database secret"
    for secret in secrets.values():
        properties = secret["Properties"]
        assert "SecretString" not in properties, "a literal secret value is in the template"
        assert "GenerateSecretString" in properties


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_stripe_parameters_hold_only_a_placeholder(data, env_name):
    """CDK creates the parameter so App Runner can start; the real key is
    written out of band as a SecureString, so it never passes through a
    CloudFormation template or this repository."""
    parameters = data[env_name].find_resources("AWS::SSM::Parameter")
    assert len(parameters) == 2, "expected the secret key and the webhook secret"

    for parameter in parameters.values():
        value = parameter["Properties"]["Value"]
        assert value == "replace-me", value
        assert not str(value).startswith("sk_")
        assert not str(value).startswith("whsec_")


def test_the_environments_do_not_share_a_parameter_path(data):
    """Sharing a path would let one environment's App Runner role read the
    other's Stripe keys - including live keys from staging."""
    def paths(template):
        return {
            p["Properties"]["Name"]
            for p in template.find_resources("AWS::SSM::Parameter").values()
        }

    assert not (paths(data["staging"]) & paths(data["prod"]))
    assert all("staging" in p for p in paths(data["staging"]))
    assert all("prod" in p for p in paths(data["prod"]))


def test_no_stripe_key_appears_anywhere_in_a_template(templates):
    """A blunt sweep of every stack, because the expensive mistake here is a key
    reaching a template by a route nobody thought to check."""
    for name, template in templates.items():
        rendered = json.dumps(template.to_json())
        for marker in ("sk_live_", "sk_test_", "whsec_", "pk_live_"):
            assert marker not in rendered, f"{marker} found in {name}"


# --- images ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_image_bucket_is_private(data, env_name):
    """Reads go through CloudFront. A public bucket would bypass it, losing the
    cache and billing S3 egress directly."""
    buckets = data[env_name].find_resources("AWS::S3::Bucket")
    for bucket in buckets.values():
        config = bucket["Properties"]["PublicAccessBlockConfiguration"]
        assert all(config[k] is True for k in config), config


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_cloudfront_uses_origin_access_control(data, env_name):
    data[env_name].resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_bucket_only_allows_presigned_uploads_cross_origin(data, env_name):
    """PUT is the presigned upload path. GET through CORS would mean the browser
    can read from S3 directly, which is what CloudFront is for."""
    bucket = next(iter(data[env_name].find_resources("AWS::S3::Bucket").values()))
    rules = bucket["Properties"]["CorsConfiguration"]["CorsRules"]
    for rule in rules:
        assert rule["AllowedMethods"] == ["PUT"], rule


# --- identity ---

def test_each_environment_has_its_own_user_pool(data):
    """Staging sharing production's pool would mean test sign-ups landing among
    real customers, and a staging Admin group granting production access."""
    for env_name in data:
        data[env_name].resource_count_is("AWS::Cognito::UserPool", 1)
        data[env_name].resource_count_is("AWS::Cognito::UserPoolClient", 1)


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_admin_group_is_named_exactly_Admin(data, env_name):
    """Case-sensitive, and checked in three places: require_admin in the backend,
    the roles check in the frontend, and here."""
    groups = data[env_name].find_resources("AWS::Cognito::UserPoolGroup")
    assert [g["Properties"]["GroupName"] for g in groups.values()] == ["Admin"]


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_web_client_holds_no_secret(data, env_name):
    """A single-page application cannot keep one. PKCE protects the code instead."""
    client = next(iter(data[env_name].find_resources("AWS::Cognito::UserPoolClient").values()))
    assert client["Properties"].get("GenerateSecret") in (False, None)
