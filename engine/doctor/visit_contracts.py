"""The visit's record inside a case (Plan O M235, DEC-1805): what the state alone does not say.

`Case.state` says where the visit is (`VisitState`, M231). :class:`CaseVisit` is the rest of what makes the visit
resumable and idempotent: which automatic state last completed for which inputs (`done`), which uploads INTAKE has
read (`taken_in`), what the visit is waiting for (`waiting_for`), the person's actions already applied (`ops`, for
a retried request), what each open question's options would add (`offers`), what the data is missing for, and the
last hand-off. It lives in `case.json`, so a transition and its record are written in one atomic write
(`engine.doctor.visit`).

This module imports nothing from `engine.doctor.case`, which imports it: the case declares a field of this type.
Nothing here holds a cell value: ids, digests, counts, codes and configured names only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from engine.config import StrictBase

__all__ = ["CaseVisit", "HandOffRecord", "OptionEffect", "VisitStop", "WaitingFor"]

WaitingFor = Literal["files", "answer", "approval", "next_file"]
"""What the visit waits for: files to start, an answer (ASK), an approval (APPROVE), next month's file (FOLLOW_UP)."""


class OptionEffect(StrictBase):
    """What choosing one option of a question adds: a proposal the person's answer ticks, or nothing."""

    option_id: str = Field(description="The option.")
    kind: str | None = Field(default=None, description="The item kind it adds; null = leave as is.")
    title: str = Field(default="", description="The item's title, in one plain sentence.")
    payload: dict[str, JsonValue] = Field(default_factory=dict, description="What it would do, as data.")


class VisitStop(StrictBase):
    """Why the visit could not go on in its state (a tool's refusal); a resume tries the state again."""

    code: str
    message: str
    state: str = Field(description="The state that stopped.")


class HandOffRecord(StrictBase):
    """Where the prepared data went: a training run, an uplift run, or back to the goal picker (no goal)."""

    target: Literal["runs", "uplift_runs", "choose_goal"]
    goal_id: str | None = None
    upload_ids: tuple[str, ...] = ()
    prescription_hash: str | None = None
    run_id: str | None = Field(default=None, description="The run started, when the hand-off started one.")


class CaseVisit(StrictBase):
    """`case.visit`: everything the visit needs to resume, beyond `case.state`."""

    done: dict[str, str] = Field(
        default_factory=dict,
        description="Per automatic state, the digest of the inputs it last completed for; a state whose inputs "
        "are unchanged is not run again.",
    )
    taken_in: tuple[str, ...] = Field(default=(), description="Uploads INTAKE has read, in order.")
    waiting_for: WaitingFor | None = Field(default=None, description="What the visit waits for, if anything.")
    ops: tuple[str, ...] = Field(
        default=(), description="The person's last actions applied, by idempotency key (at most 64)."
    )
    transitions: int = Field(default=0, ge=0, description="State changes so far.")
    offers: dict[str, tuple[OptionEffect, ...]] = Field(
        default_factory=dict, description="Per open or answered question id, what each option adds."
    )
    missing_for: tuple[str, ...] = Field(
        default=(), description="Goals the files are missing something for (no goal chosen yet)."
    )
    data_request: str | None = Field(
        default=None, description="Evidence id of the data request for those goals, when there is one."
    )
    combined: tuple[str, ...] = Field(
        default=(), description="The approved joins and stacks already built, by item id."
    )
    hand_off: HandOffRecord | None = Field(default=None, description="The last hand-off.")
    stopped: VisitStop | None = Field(default=None, description="Why the last attempt stopped, if it did.")
