"""Make the stack's test accounts usable.

Why this exists at all: CloudFormation cannot finish the job.
`AWS::Cognito::UserPoolUser` has no password property - not a permanent one and
not even a temporary one - so an account created by a deploy always lands in
FORCE_CHANGE_PASSWORD, and the only API that moves it out of that state is
AdminSetUserPassword. DataStack creates the account and generates a password
beside it in Secrets Manager; this reads that password and applies it.

Run it after `cdk deploy` of a staging DataStack, and again whenever the pool is
recreated. It is idempotent: setting the same password twice is a no-op from the
caller's point of view, so it is safe in CI before every run.

    python scripts/seed_test_users.py --profile <p>
    python scripts/seed_test_users.py --profile <p> --show-password customer

Staging only. Production has no test users configured and this refuses to run
against it, because a seeded account with a machine-readable password is a way
in and production must not have one.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import ENVIRONMENTS, STAGING  # noqa: E402


def _session(profile: str | None, region: str | None):
    return boto3.Session(profile_name=profile, region_name=region)


def _find_user_pool(cognito, prefix: str) -> str:
    """The pool id for an environment, by name.

    Looked up rather than passed in so this needs nothing but a profile - the
    name is set by DataStack and is derived from the environment.
    """
    wanted = f"{prefix}-users"
    paginator = cognito.get_paginator("list_user_pools")
    for page in paginator.paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == wanted:
                return pool["Id"]
    raise SystemExit(
        f"No user pool named {wanted}. Deploy Biofarm-Data-staging first, "
        f"and check the profile points at the right account."
    )


def _password(secrets, secret_name: str) -> tuple[str, str]:
    try:
        raw = secrets.get_secret_value(SecretId=secret_name)["SecretString"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            raise SystemExit(
                f"No secret at {secret_name}. It is created by DataStack, so "
                f"either the stack has not been deployed or the profile is "
                f"pointing somewhere else."
            ) from error
        raise
    payload = json.loads(raw)
    return payload["email"], payload["password"]


def seed(profile: str | None, region: str | None, show: str | None) -> int:
    cfg = STAGING
    if not cfg.test_users:
        raise SystemExit("No test users configured for staging.")

    session = _session(profile, region)
    cognito = session.client("cognito-idp")
    secrets = session.client("secretsmanager")

    pool_id = _find_user_pool(cognito, cfg.resource_prefix)
    print(f"user pool {pool_id}")

    failures = 0
    for user in cfg.test_users:
        secret_name = f"{cfg.secret_prefix}/test-users/{user.name}"
        email, password = _password(secrets, secret_name)

        try:
            cognito.admin_set_user_password(
                UserPoolId=pool_id,
                Username=email,
                Password=password,
                Permanent=True,
            )
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "UserNotFoundException":
                print(
                    f"  {email}: not found. The account is created by DataStack; "
                    f"deploy it before seeding.",
                    file=sys.stderr,
                )
            else:
                print(f"  {email}: {code}: {error}", file=sys.stderr)
            failures += 1
            continue

        status = cognito.admin_get_user(UserPoolId=pool_id, Username=email)[
            "UserStatus"
        ]
        role = "admin" if user.admin else "customer"
        print(f"  {email}  {status}  ({role})")

        if status != "CONFIRMED":
            print(
                f"  {email}: expected CONFIRMED, got {status}", file=sys.stderr
            )
            failures += 1

        if show == user.name:
            # Only on request and only for one account, so a routine run does not
            # scatter passwords through CI logs.
            print(f"  password: {password}")

    if failures:
        # Non-zero so CI stops here rather than running a suite that cannot log
        # in and reporting it as a hundred test failures.
        print(f"{failures} account(s) not usable", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="AWS profile with Cognito admin rights.")
    parser.add_argument("--region", help="Overrides the profile's region.")
    parser.add_argument(
        "--show-password",
        metavar="NAME",
        help=(
            "Print one account's password, by name (e.g. customer). For local "
            "debugging; leave it off in CI."
        ),
    )
    args = parser.parse_args()

    names = {u.name for cfg in ENVIRONMENTS for u in cfg.test_users}
    if args.show_password and args.show_password not in names:
        raise SystemExit(
            f"Unknown account {args.show_password!r}. Known: {', '.join(sorted(names))}"
        )

    return seed(args.profile, args.region, args.show_password)


if __name__ == "__main__":
    raise SystemExit(main())
