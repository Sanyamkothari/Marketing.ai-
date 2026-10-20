"""The three-way split: which customers the agent decides for, which a simple rule handles, and
which we never contact (plan-v2 section 8).

A customer's group comes from a salted SHA-256 of their id, so it is the same on every run, on every
machine and in every process, with no table to keep in sync. The hash is turned into a number `u` in
[0, 1) and cut into three bands:

    [0, holdout_share)                     -> holdout
    [1 - baseline_share, 1)                -> baseline
    everything in between                  -> agent

Holdout grows up from 0 and baseline grows down from 1, so changing one share never moves a customer
between the *other* two groups: raising the holdout from 5% to 10% only takes customers from the
agent group, and the baseline group is untouched. (Lowering a share does move customers back to the
agent, which spoils the comparison for them; pick the shares once.)

The salt should be fixed per business. A new salt reshuffles everyone, which is what you want for a
fresh experiment and never what you want in the middle of one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from autonomy.contracts import CustomerState, Envelope, Group

__all__ = ["assign_group", "group_assigner", "split_score", "with_group"]

_SCALE = float(2**53)


def split_score(customer_id: str, salt: str) -> float:
    """A stable number in [0, 1) for this customer under this salt (the top 53 bits of SHA-256).

    53 bits, not 64: a float holds 53 bits exactly, and dividing a 64-bit number by 2**64 can round
    up to exactly 1.0, which would put a customer in the baseline even with `baseline_share=0`.
    """
    digest = hashlib.sha256(f"{salt}\x1f{customer_id}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / _SCALE


def _check_shares(holdout_share: float, baseline_share: float) -> None:
    if not 0.0 <= holdout_share <= 1.0 or not 0.0 <= baseline_share <= 1.0:
        raise ValueError("holdout_share and baseline_share must each be between 0 and 1")
    if holdout_share + baseline_share >= 1.0:
        raise ValueError("holdout + baseline must leave room for the agent")


def assign_group(customer_id: str, salt: str, holdout_share: float, baseline_share: float) -> Group:
    """The customer's group: holdout, baseline or agent, the same on every run."""
    _check_shares(holdout_share, baseline_share)
    u = split_score(customer_id, salt)
    if u < holdout_share:
        return Group.HOLDOUT
    if u >= 1.0 - baseline_share:
        return Group.BASELINE
    return Group.AGENT


def with_group(
    state: CustomerState, *, salt: str, holdout_share: float, baseline_share: float
) -> CustomerState:
    """The state with its `group` set by `assign_group`; the same object when it already matches."""
    group = assign_group(state.customer_id, salt, holdout_share, baseline_share)
    if state.group is group:
        return state
    return state.model_copy(update={"group": group})


def group_assigner(
    salt: str,
    *,
    envelope: Envelope | None = None,
    holdout_share: float | None = None,
    baseline_share: float | None = None,
) -> Callable[[CustomerState], CustomerState]:
    """A `state -> state` function for `autonomy.loop.run_days(assign_group=...)`.

    Shares come from the arguments, else from the envelope, else the plan's 10% / 10%.
    """
    holdout = holdout_share if holdout_share is not None else (envelope.holdout_share if envelope else 0.10)
    baseline = (
        baseline_share if baseline_share is not None else (envelope.baseline_share if envelope else 0.10)
    )
    _check_shares(holdout, baseline)

    def assign(state: CustomerState) -> CustomerState:
        return with_group(state, salt=salt, holdout_share=holdout, baseline_share=baseline)

    return assign
