"""A monthly cost budget, only if somebody named an amount.

`monthly_budget_usd` has no default, and the absence is the point. A budget is a statement about
what this deployment is *allowed* to cost, and nobody in this repository knows that number - it
depends on the customer, the account and what else is in it. A plausible-looking default would be
the same mistake as a fabricated metric: it would look like a decision somebody made (plan section
13.3, DEC-378).

So: no `monthly_budget_usd`, no budget, and a stack output that says so in words rather than an
empty stack that looks like a bug. With one, `AppContext.validate` has already refused the
combination of an amount and no `alert_email`, because a budget nobody is told about is a number in
a console.

The two notification thresholds below are percentages of the operator's own number, not amounts, so
they carry no claim about what anything costs.

**This budget only sees tagged spend if the `product` cost-allocation tag is activated.** Activation
is an account-level action in Billing and Cost Management that CloudFormation cannot perform and
that takes up to 24 hours to take effect; until then the filter matches nothing and the budget
reports zero. `infra/README.md` says so next to the first-deployment checklist.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.context import AppContext
from infra.naming import PRODUCT

__all__ = ["ACTUAL_THRESHOLD_PERCENT", "FORECAST_THRESHOLD_PERCENT", "BudgetsStack"]

ACTUAL_THRESHOLD_PERCENT: Final[int] = 80
"""Notify when *actual* spend passes this share of the budget. Chosen: leaves room to react."""

FORECAST_THRESHOLD_PERCENT: Final[int] = 100
"""Notify when the *forecast* for the month reaches the budget. Chosen: the earliest honest warning."""


class BudgetsStack(Stack):
    """One monthly cost budget filtered to this product's tag, or nothing at all."""

    def __init__(self, scope: Construct, construct_id: str, *, context: AppContext, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        self.context = context

        # The decision is recorded whether or not there is a budget, and it is recorded as a
        # resource rather than only as an output. A CloudFormation template with no `Resources`
        # section is not a deployable template, so "no budget" has to leave *something* behind -
        # and the most useful something is the answer itself, where an operator looking for
        # "what is this deployment allowed to cost" can read it. The path is outside
        # `/marketing-ai/<env>/`: it is not a Settings field and the application never reads it.
        self.record = ssm.StringParameter(
            self,
            "BudgetRecord",
            parameter_name=f"/{PRODUCT}/cost/{context.env_name}/monthly-budget-usd",
            string_value=("none" if context.monthly_budget_usd is None else str(context.monthly_budget_usd)),
            description="What -c monthly_budget_usd said when this deployment was last synthesised",
        )
        self.record.apply_removal_policy(
            RemovalPolicy.DESTROY if context.removal_policy_destroy else RemovalPolicy.RETAIN
        )

        if context.monthly_budget_usd is None:
            self.budget: budgets.CfnBudget | None = None
            CfnOutput(
                self,
                "Budget",
                value=(
                    "none - no monthly_budget_usd was given. This deployment has no cost guardrail; "
                    "pass -c monthly_budget_usd=<amount> -c alert_email=<address> to add one."
                ),
                description="Whether a cost budget exists",
            )
            return

        # `alert_email` is not optional here: AppContext.validate refuses the combination.
        assert context.alert_email is not None
        self.budget = budgets.CfnBudget(
            self,
            "MonthlyCost",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{PRODUCT}-{context.env_name}-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=context.monthly_budget_usd,
                    unit="USD",
                ),
                cost_filters={"TagKeyValue": [f"user:product${PRODUCT}"]},
                cost_types=budgets.CfnBudget.CostTypesProperty(
                    # Credits and refunds are not this deployment's doing, and counting them makes
                    # the budget describe the account's accounting rather than this product's usage.
                    include_credit=False,
                    include_refund=False,
                    include_discount=True,
                    include_tax=True,
                    include_subscription=True,
                    use_amortized=False,
                    use_blended=False,
                ),
            ),
            notifications_with_subscribers=[
                self._notification("ACTUAL", ACTUAL_THRESHOLD_PERCENT, context.alert_email),
                self._notification("FORECASTED", FORECAST_THRESHOLD_PERCENT, context.alert_email),
            ],
        )
        CfnOutput(
            self,
            "Budget",
            value=f"{context.monthly_budget_usd} USD per month, filtered to product={PRODUCT}",
            description="Whether a cost budget exists",
        )

    @staticmethod
    def _notification(
        notification_type: str, threshold: int, email: str
    ) -> budgets.CfnBudget.NotificationWithSubscribersProperty:
        return budgets.CfnBudget.NotificationWithSubscribersProperty(
            notification=budgets.CfnBudget.NotificationProperty(
                notification_type=notification_type,
                comparison_operator="GREATER_THAN",
                threshold=threshold,
                threshold_type="PERCENTAGE",
            ),
            subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=email)],
        )
