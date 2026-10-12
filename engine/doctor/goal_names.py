"""What a goal is called in a sentence a person approves (Plan O M247, DEC-1817).

The sentence Approve records (`engine.doctor.visit.approval_sentence`) is shown and checked by the server, word for
word, so the goal's name in it must be the one the screens show, not its id: `ui/labels.js` `GOAL_NAMES` (Plan M
§4.2, DEC-1602) by use-case id, else the use case's configured `name` (the goal brief's `goal_name`), else the id.

`GOAL_DISPLAY_NAMES` repeats `GOAL_NAMES` for the server; `tests/unit/doctor/test_goal_names.py` reads
`ui/labels.js` and fails when the two differ, so the copy cannot drift.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

__all__ = ["GOAL_DISPLAY_NAMES", "goal_display_name"]

GOAL_DISPLAY_NAMES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "targeted-advertisement": "Find customers likely to buy",
        "win-back-campaign": "Win back lapsed customers",
        "telco-churn": "Keep customers likely to leave",
        "payment-propensity": "Get customers to pay on time",
        "rca": "Understand why customers leave",
        "hillstrom-email": "Choose the best e-mail for each customer",
        "ai-onboarding-assistant": "Answer customer questions (AI assistant)",
        "card-default-propensity": "Find card accounts likely to miss a payment",
        "first-purchase-propensity": "Find browsers likely to buy for the first time",
        "criteo-uplift": "Show ads where they change what people do",
    }
)
"""`ui/labels.js` `GOAL_NAMES`, kept equal by a test."""


def goal_display_name(goal_id: str, configured: str | None = None) -> str:
    """The goal's name as the screens show it: its display name, else its configured name, else its id."""
    return GOAL_DISPLAY_NAMES.get(goal_id) or (configured or "").strip() or goal_id
