"""The single authoring path and deterministic default author for the Data Doctor.

One author:
* With an AI: the AI agent loop writes the plan.
* Without an AI: the deterministic default author turns fact tools' suggestions into the same plan,
  through the same authoring models and validation.
* In both cases, zero items are pre-accepted (decided_by=""). The plan starts with items pending
  person approval (ProposalState.PENDING), or recommended unconfirmed.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from engine.config import StrictBase
from engine.doctor.case import CaseDoubt, CaseItem, CaseQuestion, EventDraft
from engine.doctor.visit_contracts import OptionEffect


class AuthorResult(StrictBase):
    items: tuple[CaseItem, ...] = ()
    questions: tuple[tuple[CaseQuestion, tuple[OptionEffect, ...]], ...] = ()
    doubts: tuple[CaseDoubt, ...] = ()
    requests: tuple[Any, ...] = ()
    events: tuple[EventDraft, ...] = ()
    reasked: set[str] = Field(default_factory=set)
    model_calls: int = 0
    tokens: int = 0
    summary: str = ""
    reply: str = ""
    blocked_by: str | None = None
    error_codes: tuple[str, ...] = ()
    state_digest: str = ""
    unchanged: bool = False
