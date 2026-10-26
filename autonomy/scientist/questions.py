"""The menu of live questions the Scientist may ask (plan-v3 foundation C).

`menu.py` lists the candidate policies the Scientist compares on a campaign that already ran. This is
its sibling for experiments the agent runs itself, on a small random group, while it works. A
question is one of a fixed few, never free text from a model:

* `offer`: which offer earns more profit per customer for a goal's audience (free shipping or 10% off
  or no offer at all).
* `channel`: which channel earns more profit per customer (email or WhatsApp).

Each question has two to four arms. An arm is an offer id or a channel name, and it must already be
inside the envelope the marketer approved: an experiment never widens what the agent may do. An offer
that needs a person's approval is not testable, because every send of it would need that approval.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from autonomy.contracts import Envelope

__all__ = [
    "ID_PATTERN",
    "MAX_ARMS",
    "MIN_ARMS",
    "QUESTION_MENU",
    "RESERVED_ARM_IDS",
    "Question",
    "QuestionError",
    "QuestionKind",
    "check_arm",
    "check_question",
    "describe_question",
]

MIN_ARMS = 2
MAX_ARMS = 4
ID_PATTERN = r"^[A-Za-z0-9_-]{1,48}$"
"""Experiment and arm ids go into the policy version tag of every decision, so they stay plain."""

RESERVED_ARM_IDS = frozenset({"holdout"})
"""The tag of the matched holdout customers in a decision log; no arm may use it."""


class QuestionError(ValueError):
    """A question or arm set that the menu does not allow."""


class QuestionKind(StrEnum):
    OFFER = "offer"
    CHANNEL = "channel"


@dataclass(frozen=True)
class Question:
    """One entry of the menu: what is compared."""

    kind: QuestionKind
    template: str


QUESTION_MENU: dict[QuestionKind, Question] = {
    QuestionKind.OFFER: Question(
        QuestionKind.OFFER, "Which offer earns more profit per customer for {audience}?"
    ),
    QuestionKind.CHANNEL: Question(
        QuestionKind.CHANNEL, "Which channel earns more profit per customer for {audience}?"
    ),
}


def describe_question(kind: QuestionKind, audience: str) -> str:
    """The question in the marketer's words, for the experiment's record."""
    return QUESTION_MENU[kind].template.format(audience=audience or "this goal's audience")


def check_question(kind: QuestionKind, arm_ids: Sequence[str], envelope: Envelope) -> None:
    """Raise `QuestionError` unless these arms are a valid set for this kind of question.

    Two to four distinct arms with plain ids; every arm inside the approved envelope; no offer that
    needs a person's approval.
    """
    if not MIN_ARMS <= len(arm_ids) <= MAX_ARMS:
        raise QuestionError(f"a question needs {MIN_ARMS} to {MAX_ARMS} arms, not {len(arm_ids)}")
    if len(set(arm_ids)) != len(arm_ids):
        raise QuestionError("the arms of a question must all be different")
    for arm in arm_ids:
        check_arm(kind, arm, envelope)


def check_arm(kind: QuestionKind, arm: str, envelope: Envelope) -> None:
    """Raise `QuestionError` unless this one arm is usable: a plain id, inside the approved envelope,
    and (for an offer) not one that needs a person's approval."""
    if not re.fullmatch(ID_PATTERN, arm) or arm in RESERVED_ARM_IDS:
        raise QuestionError(f"{arm!r} is not a usable arm id")
    if kind is QuestionKind.OFFER:
        offers = {o.offer_id: o for o in envelope.offers}
        if arm != "none" and arm not in offers:
            raise QuestionError(f"offer {arm!r} is not inside the approved envelope")
        if arm in offers and offers[arm].needs_approval:
            raise QuestionError(f"offer {arm!r} needs a person's approval, so it cannot be tested")
    elif arm not in {c.value for c in envelope.channels}:
        raise QuestionError(f"channel {arm!r} is not inside the approved envelope")
