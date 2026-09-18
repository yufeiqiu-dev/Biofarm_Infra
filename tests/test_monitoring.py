"""The account-wide budget and the shared alert topic.

Deployed once, not per environment - see monitoring_stack.py. What matters
here is that a runaway cost actually reaches someone: a budget with no
subscriber, or one pointed at a placeholder address, would pass every other
check and still notify nobody.
"""

from __future__ import annotations

import json

import pytest

from config import ALERT_EMAIL


@pytest.fixture(scope="module")
def monitoring(templates):
    return templates["Biofarm-Monitoring"]


def test_there_is_exactly_one_budget(monitoring):
    monitoring.resource_count_is("AWS::Budgets::Budget", 1)


def test_the_budget_is_fifty_dollars_a_month(monitoring):
    budgets = monitoring.find_resources("AWS::Budgets::Budget")
    data = next(iter(budgets.values()))["Properties"]["Budget"]
    assert data["BudgetType"] == "COST"
    assert data["TimeUnit"] == "MONTHLY"
    assert data["BudgetLimit"] == {"Amount": 50, "Unit": "USD"}


def test_the_budget_notifies_on_actual_and_forecasted_spend(monitoring):
    """Two thresholds, not one: 80% of what has actually been spent, which
    still leaves most of the month to react, and 100% of the *forecasted*
    total, which catches a trend on track to blow the budget before the month
    is even over."""
    budgets = monitoring.find_resources("AWS::Budgets::Budget")
    notifications = next(iter(budgets.values()))["Properties"][
        "NotificationsWithSubscribers"
    ]
    assert len(notifications) == 2

    by_type = {n["Notification"]["NotificationType"]: n for n in notifications}
    assert set(by_type) == {"ACTUAL", "FORECASTED"}

    actual = by_type["ACTUAL"]["Notification"]
    assert actual["Threshold"] == 80
    assert actual["ComparisonOperator"] == "GREATER_THAN"

    forecasted = by_type["FORECASTED"]["Notification"]
    assert forecasted["Threshold"] == 100
    assert forecasted["ComparisonOperator"] == "GREATER_THAN"


def test_every_notification_actually_reaches_someone(monitoring):
    """A budget with no subscriber - or a placeholder address - would pass
    every check above and still tell nobody."""
    budgets = monitoring.find_resources("AWS::Budgets::Budget")
    notifications = next(iter(budgets.values()))["Properties"][
        "NotificationsWithSubscribers"
    ]
    for notification in notifications:
        subscribers = notification["Subscribers"]
        assert subscribers, "a notification with no subscriber notifies nobody"
        for subscriber in subscribers:
            assert subscriber["SubscriptionType"] == "EMAIL"
            assert subscriber["Address"] == ALERT_EMAIL
            assert "@" in subscriber["Address"]


def test_there_is_exactly_one_alert_topic(monitoring):
    monitoring.resource_count_is("AWS::SNS::Topic", 1)


def test_the_alert_topic_has_an_email_subscriber(monitoring):
    subscriptions = monitoring.find_resources("AWS::SNS::Subscription")
    assert len(subscriptions) == 1

    subscription = next(iter(subscriptions.values()))["Properties"]
    assert subscription["Protocol"] == "email"
    assert subscription["Endpoint"] == ALERT_EMAIL


def test_no_alert_email_appears_anywhere_else(templates):
    """A second, silently-diverging copy of this address in another stack
    would be a maintenance trap - only MonitoringStack should own it."""
    for name, template in templates.items():
        if name == "Biofarm-Monitoring":
            continue
        assert ALERT_EMAIL not in json.dumps(template.to_json()), name
