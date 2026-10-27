"""Two changes about the same thing are never both in the plan: what a tick leaves out (F5).

A swap is a waiting change with `replaces` naming the change it would take the place of. The person's yes to the
swap (`visit.decide`, or `visit.confirm` for a change that needs its own yes) leaves the old one out; their yes to
the old one, after the swap was accepted, leaves the swap out. An answer that is given again (the question was
asked again because the option chosen is gone) leaves out the change of the answer before. One place, so every path
that ticks a change does the same.

Nothing here reads a cell or calls a model: it works on the case's records alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from engine.agent.contracts import ProposalState
from engine.doctor.case import CaseItem, EventDraft, TimelineKind
from engine.doctor.protocol import AGENT

__all__ = ["leave_out_for", "leave_out_of_answer"]

ANSWERED_AGAIN = "the question was answered again"


def leave_out_for(
    items: Sequence[CaseItem], ticked: CaseItem, *, actor: str, now: datetime
) -> tuple[dict[str, CaseItem], list[EventDraft]]:
    """The changes a yes to `ticked` leaves out: the one it replaces, and any accepted swap that replaces it.

    Returns the changed items by id and the timeline events; `ticked` itself is not touched."""
    out: dict[str, CaseItem] = {}
    events: list[EventDraft] = []
    for other in items:
        if other.item_id == ticked.item_id or other.state is ProposalState.REJECTED:
            continue
        if other.item_id == ticked.replaces:
            pass  # the swap is ticked: the old one goes
        elif other.replaces == ticked.item_id and other.state is ProposalState.ACCEPTED:
            pass  # the old one is ticked again after its swap was accepted: the swap goes
        else:
            continue
        out[other.item_id] = other.model_copy(
            update={
                "state": ProposalState.REJECTED,
                "decided_by": actor,
                "decided_at": now,
                "parked_for": None,
                "superseded_by": ticked.item_id,
            }
        )
        events.append(
            EventDraft(
                kind=TimelineKind.ITEM_DECIDED,
                upload_id=other.upload_id,
                item_id=other.item_id,
                details={"state": "rejected", "swapped_for": ticked.item_id},
            )
        )
    return out, events


def leave_out_of_answer(
    items: Sequence[CaseItem], question_id: str, new_id: str | None, *, now: datetime
) -> list[CaseItem]:
    """`items` with the change of an earlier answer to `question_id` left out (the Doctor's own withdrawal, not the
    person's no): an answer's change is `<question id>:<option id>`."""
    prefix = f"{question_id}:"
    return [
        (
            i.model_copy(
                update={
                    "state": ProposalState.REJECTED,
                    "decided_by": AGENT,
                    "decided_at": now,
                    "parked_for": None,
                    "withdrawn_because": ANSWERED_AGAIN,
                    "superseded_by": new_id,
                }
            )
            if i.item_id.startswith(prefix) and i.item_id != new_id and i.state is not ProposalState.REJECTED
            else i
        )
        for i in items
    ]
