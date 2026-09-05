"""The staging start/stop script.

Tested against stubs rather than AWS. What matters here is the decision-making -
which call is made, in which order, and when nothing should be done at all -
because the failure modes are either a needless bill or an operation rejected
mid-transition.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import staging_power  # noqa: E402


class FakeRds:
    def __init__(self, status: str = "available", identifier: str = "biofarm-staging-db-abc"):
        self.instances = (
            [{"DBInstanceIdentifier": identifier, "DBInstanceStatus": status}] if status else []
        )
        self.started: list[str] = []
        self.stopped: list[str] = []

    def get_paginator(self, _name):
        instances = self.instances

        class Paginator:
            def paginate(self):
                return [{"DBInstances": instances}]

        return Paginator()

    def start_db_instance(self, DBInstanceIdentifier):  # noqa: N803 - boto3 casing
        self.started.append(DBInstanceIdentifier)

    def stop_db_instance(self, DBInstanceIdentifier):  # noqa: N803
        self.stopped.append(DBInstanceIdentifier)


class FakeAppRunner:
    def __init__(self, status: str = "RUNNING"):
        self.services = (
            [
                {
                    "ServiceName": staging_power.SERVICE_NAME,
                    "ServiceArn": "arn:aws:apprunner:us-east-2:1:service/x",
                    "Status": status,
                }
            ]
            if status
            else []
        )
        self.paused: list[str] = []
        self.resumed: list[str] = []

    def get_paginator(self, _name):
        services = self.services

        class Paginator:
            def paginate(self):
                return [{"ServiceSummaryList": services}]

        return Paginator()

    def pause_service(self, ServiceArn):  # noqa: N803
        self.paused.append(ServiceArn)

    def resume_service(self, ServiceArn):  # noqa: N803
        self.resumed.append(ServiceArn)


# --- starting and stopping ---

def test_down_stops_both():
    rds, apprunner = FakeRds("available"), FakeAppRunner("RUNNING")

    staging_power._set_service(apprunner, False)
    staging_power._set_database(rds, False)

    assert apprunner.paused
    assert rds.stopped == ["biofarm-staging-db-abc"]


def test_up_starts_both():
    rds, apprunner = FakeRds("stopped"), FakeAppRunner("PAUSED")

    staging_power._set_database(rds, True)
    staging_power._set_service(apprunner, True)

    assert rds.started == ["biofarm-staging-db-abc"]
    assert apprunner.resumed


def test_the_database_is_started_before_the_service(monkeypatch, capsys):
    """App Runner's health check runs SELECT 1. A service resumed against a
    stopped database fails readiness and can be marked unhealthy before the
    database ever catches up."""
    order: list[str] = []
    monkeypatch.setattr(staging_power, "_set_database", lambda *_: order.append("db"))
    monkeypatch.setattr(staging_power, "_set_service", lambda *_: order.append("svc"))
    monkeypatch.setattr(staging_power, "_clients", lambda *_: (None, None))
    monkeypatch.setattr(sys, "argv", ["staging_power.py", "up"])

    staging_power.main()

    assert order == ["db", "svc"]


def test_the_service_is_stopped_before_the_database(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(staging_power, "_set_database", lambda *_: order.append("db"))
    monkeypatch.setattr(staging_power, "_set_service", lambda *_: order.append("svc"))
    monkeypatch.setattr(staging_power, "_clients", lambda *_: (None, None))
    monkeypatch.setattr(sys, "argv", ["staging_power.py", "down"])

    staging_power.main()

    assert order == ["svc", "db"]


# --- doing nothing, correctly ---

def test_stopping_an_already_stopped_database_does_nothing():
    rds = FakeRds("stopped")
    staging_power._set_database(rds, False)
    assert not rds.stopped


def test_resuming_an_already_running_service_does_nothing():
    apprunner = FakeAppRunner("RUNNING")
    staging_power._set_service(apprunner, True)
    assert not apprunner.resumed


@pytest.mark.parametrize("state", sorted(staging_power.DB_BUSY_STATES))
def test_a_database_mid_transition_is_left_alone(state):
    """An instance already starting or stopping usually means someone else is
    working on it, and the API would reject the call anyway."""
    rds = FakeRds(state)
    staging_power._set_database(rds, True)
    staging_power._set_database(rds, False)
    assert not rds.started and not rds.stopped


def test_a_service_mid_operation_is_left_alone():
    apprunner = FakeAppRunner("OPERATION_IN_PROGRESS")
    staging_power._set_service(apprunner, False)
    assert not apprunner.paused


# --- an environment that is not deployed yet ---

def test_a_missing_database_is_reported_not_raised(capsys):
    rds = FakeRds(status="")
    staging_power._set_database(rds, True)
    assert "not found" in capsys.readouterr().out


def test_a_missing_service_is_reported_not_raised(capsys):
    apprunner = FakeAppRunner(status="")
    staging_power._set_service(apprunner, True)
    assert "not found" in capsys.readouterr().out


def test_status_signals_failure_when_nothing_is_deployed():
    assert staging_power.status(FakeRds(status=""), FakeAppRunner(status="")) == 1


def test_status_succeeds_when_both_exist():
    assert staging_power.status(FakeRds("available"), FakeAppRunner("RUNNING")) == 0


# --- API rejections ---

def test_an_api_error_is_printed_rather_than_crashing(capsys):
    """This runs interactively. A traceback tells you less than the message AWS
    returned."""
    rds = FakeRds("available")

    def refuse(**_):
        raise ClientError(
            {"Error": {"Code": "InvalidDBInstanceState", "Message": "cannot stop right now"}},
            "StopDBInstance",
        )

    rds.stop_db_instance = refuse
    staging_power._set_database(rds, False)

    assert "cannot stop right now" in capsys.readouterr().err


# --- the thing people forget ---

def test_the_seven_day_restart_is_stated_on_every_down(monkeypatch, capsys):
    """AWS force-starts a stopped RDS instance after 7 days and there is no way
    to turn that off, so `down` is a scheduled action rather than a switch. If
    staging is billing more than expected, this is the reason."""
    monkeypatch.setattr(staging_power, "_set_database", lambda *_: None)
    monkeypatch.setattr(staging_power, "_set_service", lambda *_: None)
    monkeypatch.setattr(staging_power, "_clients", lambda *_: (None, None))
    monkeypatch.setattr(sys, "argv", ["staging_power.py", "down"])

    staging_power.main()

    assert "7 days" in capsys.readouterr().out
