"""Start and stop the staging environment.

Staging is deliberately minimal: its database is stopped and its App Runner
service paused outside active testing, which takes the environment from roughly
$28 a month to near the storage floor. This is what makes that decision real
rather than aspirational.

    python scripts/staging_power.py up     --profile biofarm
    python scripts/staging_power.py down   --profile biofarm
    python scripts/staging_power.py status --profile biofarm

**AWS force-starts a stopped RDS instance after 7 days.** There is no way to
turn that off. So `down` is something you run on a schedule, not a switch that
stays off - and if staging is billing more than expected, this is the first
thing to check.

Both operations are asynchronous. The commands return once AWS has accepted the
request; `status` is how you find out whether it finished.
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

# Matches EnvConfig.resource_prefix for staging. Kept as a literal rather than
# imported so this script stays runnable without the CDK dependencies installed.
DB_IDENTIFIER_PREFIX = "biofarm-staging"
SERVICE_NAME = "biofarm-staging-backend"

# RDS states in which a request would be rejected. Reported rather than retried:
# an instance mid-transition usually means someone else is working on it.
DB_BUSY_STATES = {"starting", "stopping", "modifying", "backing-up"}


def _clients(profile: str | None, region: str | None):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("rds"), session.client("apprunner")


def _find_database(rds) -> dict | None:
    """Located by identifier prefix, because CloudFormation appends a suffix."""
    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for instance in page["DBInstances"]:
            if instance["DBInstanceIdentifier"].startswith(DB_IDENTIFIER_PREFIX):
                return instance
    return None


def _find_service(apprunner) -> dict | None:
    paginator = apprunner.get_paginator("list_services")
    for page in paginator.paginate():
        for service in page["ServiceSummaryList"]:
            if service["ServiceName"] == SERVICE_NAME:
                return service
    return None


def status(rds, apprunner) -> int:
    database = _find_database(rds)
    service = _find_service(apprunner)

    if database is None:
        print("database:    not found - has staging been deployed?")
    else:
        print(f"database:    {database['DBInstanceStatus']}  ({database['DBInstanceIdentifier']})")

    if service is None:
        print("app runner:  not found - has staging been deployed?")
    else:
        print(f"app runner:  {service['Status'].lower()}  ({service['ServiceName']})")

    return 0 if database and service else 1


def _set_database(rds, want_running: bool) -> None:
    database = _find_database(rds)
    if database is None:
        print("database:    not found, skipping")
        return

    identifier = database["DBInstanceIdentifier"]
    state = database["DBInstanceStatus"]

    if state in DB_BUSY_STATES:
        print(f"database:    {state} already - leaving it alone")
        return

    target = "available" if want_running else "stopped"
    if state == target:
        print(f"database:    already {state}")
        return

    action = rds.start_db_instance if want_running else rds.stop_db_instance
    try:
        action(DBInstanceIdentifier=identifier)
        print(f"database:    {'starting' if want_running else 'stopping'} {identifier}")
    except ClientError as error:
        print(f"database:    {error.response['Error']['Message']}", file=sys.stderr)


def _set_service(apprunner, want_running: bool) -> None:
    service = _find_service(apprunner)
    if service is None:
        print("app runner:  not found, skipping")
        return

    arn = service["ServiceArn"]
    state = service["Status"]

    if want_running and state == "RUNNING":
        print("app runner:  already running")
        return
    if not want_running and state == "PAUSED":
        print("app runner:  already paused")
        return
    if state in {"OPERATION_IN_PROGRESS", "CREATE_FAILED", "DELETED"}:
        print(f"app runner:  {state.lower()} - leaving it alone")
        return

    action = apprunner.resume_service if want_running else apprunner.pause_service
    try:
        action(ServiceArn=arn)
        print(f"app runner:  {'resuming' if want_running else 'pausing'} {SERVICE_NAME}")
    except ClientError as error:
        print(f"app runner:  {error.response['Error']['Message']}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    parser.add_argument("--profile", help="AWS profile, typically an SSO one")
    parser.add_argument("--region", help="Defaults to the profile's region")
    args = parser.parse_args()

    rds, apprunner = _clients(args.profile, args.region)

    if args.action == "status":
        return status(rds, apprunner)

    want_running = args.action == "up"

    # The database first when starting, last when stopping: App Runner's health
    # check runs SELECT 1, so a service resumed against a stopped database fails
    # readiness and can be marked unhealthy before the database catches up.
    if want_running:
        _set_database(rds, True)
        _set_service(apprunner, True)
    else:
        _set_service(apprunner, False)
        _set_database(rds, False)

    print("\nBoth operations are asynchronous; run `status` to see when they finish.")
    if not want_running:
        print("Note: AWS force-starts a stopped RDS instance after 7 days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
