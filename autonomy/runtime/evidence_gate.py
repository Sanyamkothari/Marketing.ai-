"""The evidence gate on Autopilot: no autopilot without proof (docs/v4/CALIBRATION.md, ARCHITECTURE_REVIEW section 8).

A goal may be switched to Autopilot only when both hold:

1. **The holdout says it pays.** The measured incremental profit (agent group against the people we never
   contact, `ProofSummary.incremental_profit`) has a 95% lower bound above zero, and the goal has been acting
   for at least `MIN_WINDOW_DAYS` (the 14 days of `autonomy.runtime.promotion.PromotionRule.min_days`).
2. **The agent's forecasts hold up.** `ProofSummary.calibration` is `ok` or `over_1_5x`. `over_2x` (it
   expects more than twice what the holdout shows) and `not_enough_data` (nothing to check it against) refuse.

Otherwise `autopilot_gate` says no with a plain reason, and the goal stays on approve-each-batch.

**The one exception** is the email path of docs/v3/DECISIONS.md V-1 (an email mistake costs little and
teaches us): a goal that sends by email only may start on Autopilot without proof, but only when the shop
owner explicitly confirms "start on autopilot without proof". `unproven_start` decides it; the confirmation
is written to the audit log by the caller. WhatsApp is never exempt (a burned number cannot be undone).

Pure functions of the proof and the time passed in; nothing here reads a clock or a store.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from autonomy.contracts import Channel, ProofSummary

__all__ = [
    "MIN_WINDOW_DAYS",
    "GateVerdict",
    "autopilot_gate",
    "unproven_start",
]

MIN_WINDOW_DAYS = 14.0
"""Days a goal must have been acting before its holdout comparison can open Autopilot (`PromotionRule.min_days`)."""

_ALLOWED_CALIBRATION = ("ok", "over_1_5x")


@dataclass(frozen=True)
class GateVerdict:
    """Whether Autopilot may be switched on, and the plain reason either way."""

    allowed: bool
    reason: str


def _plain(value: float) -> str:
    return f"{value:,.0f}"


def autopilot_gate(
    proof: ProofSummary | None,
    window_days: float | None,
    *,
    min_days: float = MIN_WINDOW_DAYS,
    money: Callable[[float], str] = _plain,
) -> GateVerdict:
    """May Autopilot be switched on, given this goal's proof and how many days it has been acting?

    `window_days` is the days the goal has been acting at its current plan (None: it has not started).
    `money` writes an amount for the reason (the shop's currency); the default is a plain rounded number."""
    stay = "The goal stays on Approve each batch."
    profit = None if proof is None else proof.incremental_profit
    if proof is None or profit is None:
        return GateVerdict(
            False,
            "Autopilot needs proof first: there is no measured comparison with the people we never contact yet. "
            + stay,
        )
    if window_days is None or window_days < min_days:
        have = 0 if window_days is None else int(max(window_days, 0.0))
        return GateVerdict(
            False,
            f"Autopilot needs at least {min_days:.0f} days of measured results; this goal has {have}. " + stay,
        )
    if profit.low <= 0:
        return GateVerdict(
            False,
            f"The extra profit measured against the people we never contact ({money(profit.point)}, "
            f"somewhere between {money(profit.low)} and {money(profit.high)}) is not clearly above zero yet. "
            + stay,
        )
    if proof.calibration == "over_2x":
        return GateVerdict(
            False,
            "The agent expects more than twice what the people we never contact show it really earns, so its "
            "own forecasts cannot be trusted to act alone yet. " + stay,
        )
    if proof.calibration not in _ALLOWED_CALIBRATION:
        return GateVerdict(
            False,
            "We cannot yet check the agent's own forecasts against what really happened, so Autopilot is not "
            "open yet. " + stay,
        )
    return GateVerdict(True, "The holdout shows it pays and the agent's forecasts hold up.")


def unproven_start(channels: Collection[Channel], *, confirmed: bool) -> GateVerdict:
    """The email-only exception: may a goal start on Autopilot with no proof, because the owner said so?

    Allowed only for a goal that sends by email alone and only with the owner's explicit confirmation."""
    if not channels or set(channels) != {Channel.EMAIL}:
        return GateVerdict(False, "Only a goal that sends by email alone may start on Autopilot without proof.")
    if not confirmed:
        return GateVerdict(
            False,
            "Starting on Autopilot without proof needs the shop owner to confirm it explicitly "
            "('start on autopilot without proof').",
        )
    return GateVerdict(True, "The shop owner confirmed starting on Autopilot without proof (email only).")
