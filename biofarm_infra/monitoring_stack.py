"""Account-wide cost and health alerting. One instance, deployed once.

Two independent signals land on the same subscriber:

- **A budget**, because the failure mode that actually happens here is not a
  runaway attack, it is staging left running, or a NAT instance stuck
  mid-reboot - the kind of thing nobody notices until the bill arrives. Two
  thresholds: 80% of actual spend, so there is still most of the month left to
  react, and 100% of *forecasted* spend, which catches a cost that is on track
  to blow the budget before it actually has.
- **One SNS topic**, which the per-environment alarms in DataStack and AppStack
  publish to. One topic for everything rather than one per environment or per
  signal: at this volume pager fatigue is not a real risk, and splitting them
  would only mean remembering which topic a new alarm belongs on.

Other stacks take this one as a constructor argument the same way AppStack
takes CicdStack - a plain reference, not a cross-stack import, so nothing here
needs an account-wide resource name to be guessed at from another stack.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    aws_budgets as budgets,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
)
from constructs import Construct

from config import ALERT_EMAIL

MONTHLY_BUDGET_USD = 50


class MonitoringStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name="biofarm-alerts",
            display_name="Biofarm infrastructure alerts",
        )
        self.alert_topic.add_subscription(subscriptions.EmailSubscription(ALERT_EMAIL))

        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=MONTHLY_BUDGET_USD, unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                # Actual spend already past 80% - most of the month is still
                # ahead, so this is the "look now, not urgent yet" signal.
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=ALERT_EMAIL
                        )
                    ],
                ),
                # Forecasted spend on track to clear the whole budget - this is
                # the one that fires before the month is even over, on the
                # trend rather than the total so far.
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="FORECASTED",
                        comparison_operator="GREATER_THAN",
                        threshold=100,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=ALERT_EMAIL
                        )
                    ],
                ),
            ],
        )

        cdk.CfnOutput(
            self,
            "AlertTopicArn",
            value=self.alert_topic.topic_arn,
            description="Subscribe anything else here later - pager, Slack, a second address.",
        )
