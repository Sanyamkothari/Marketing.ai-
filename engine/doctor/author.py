"""The single authoring path and deterministic default author for the Data Doctor.

One author:
* With an AI: the AI agent loop writes the plan.
* Without an AI: the deterministic default author turns fact tools' suggestions into the same plan,
  through the same authoring models and validation.
* In both cases, zero items are pre-accepted (decided_by=""). The plan starts with items pending
  person approval (ProposalState.PENDING), or recommended unconfirmed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from engine.agent.contracts import (
    AgentConfidence,
    ProposalSource,
    ProposalState,
)
from engine.config import StrictBase, load_use_case
from engine.doctor.case import (
    Case,
    CaseDoubt,
    CaseItem,
    CaseOption,
    CaseQuestion,
    EventDraft,
    payload_digest,
)
from engine.doctor.decisions import decisions, sets_of_effect
from engine.doctor.tools_facts import (
    FactQuestion,
    FactSuggestion,
    extract_consent_facts,
    extract_goal_build_facts,
    extract_key_facts,
    extract_time_facts,
)
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief


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


def create_author_item(
    suggestion: FactSuggestion,
    *,
    source: ProposalSource,
    now: datetime,
    replaces: str | None = None,
) -> CaseItem:
    """Create a CaseItem through the single authoring path.

    Crucially: the item is ALWAYS ProposalState.PENDING with decided_by=None.
    Neither author pre-accepts items with decided_by="". The person approves every change.
    """
    payload = dict(suggestion.payload)
    confidence = AgentConfidence.SURE if suggestion.confidence == "sure" else AgentConfidence.CHECK
    if suggestion.needs_yes:
        payload["needs_yes"] = True
    if suggestion.ticked:
        payload["recommended_ticked"] = True
    if replaces:
        payload["replaces"] = replaces

    item_id = f"{suggestion.kind}.{suggestion.goal_id or 'base'}.{payload_digest(payload)[:16]}"
    return CaseItem(
        item_id=item_id[:128],
        kind=suggestion.kind,
        title=suggestion.title,
        source=source,
        confidence=confidence,
        state=ProposalState.PENDING,  # Never pre-accepted!
        upload_id=suggestion.upload_id,
        evidence_ids=(),
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=now,
        goal_id=suggestion.goal_id,
        decided_by=None,
        decided_at=None,
    )


def create_author_question(
    q: FactQuestion,
) -> tuple[CaseQuestion, tuple[OptionEffect, ...]]:
    """Create a CaseQuestion through the single authoring path."""
    effects: list[OptionEffect] = []
    options: list[CaseOption] = []
    for label, payload_dict in q.choices:
        opt_id = str(payload_dict.get("option_id") or f"opt_{len(options)}")
        kind = payload_dict.get("kind")
        payload = payload_dict.get("payload")
        title = payload_dict.get("title", "")
        effect = OptionEffect(
            option_id=opt_id,
            kind=str(kind) if kind is not None else None,
            title=title,
            payload=payload if isinstance(payload, dict) else {},
        )
        effects.append(effect)
        options.append(
            CaseOption(
                option_id=opt_id,
                label=label,
                effect_digest=payload_digest(effect.payload) if effect.kind is not None else None,
                sets=sets_of_effect(effect.kind),
                safe=opt_id == q.safe,
            )
        )

    question_id = f"q.{q.code.lower()}.{q.goal_id or 'base'}.{payload_digest(dict(q.choices))[:16]}"[:128]
    question = CaseQuestion(
        question_id=question_id,
        code=q.code,
        upload_id=q.upload_id,
        column=q.column,
        text=q.text,
        options=tuple(options),
        blocking=q.blocking,
        recommended=q.recommended,
        why=q.why,
        goal_id=q.goal_id,
    )
    return question, tuple(effects)


def run_default_author(
    case: Case,
    files: tuple[LoadedFile, ...],
    brief: GoalBrief | None,
    clock: Callable[[], datetime],
    config_root: Path | None = None,
    event_roles: frozenset[str] = frozenset(),
) -> AuthorResult:
    """The deterministic default author when no AI is present.

    Derives the same plan from fact tools through the same actions and models:
    * Key facts -> key proposals (pending) or key questions
    * Goal build facts -> build SQL step (pending, needs_yes) or audience question
    * Time facts -> as_of_column proposal (pending)
    * Consent facts -> consent_column proposal (pending)
    """
    now = clock()
    items: list[CaseItem] = []
    questions: list[tuple[CaseQuestion, tuple[OptionEffect, ...]]] = []
    doubts: list[CaseDoubt] = []

    dec = decisions(case)
    source = ProposalSource.RULES

    # 1. Base facts per file: key columns
    for loaded in files:
        upload_id = loaded.upload_id
        if dec.key(upload_id) is not None:
            continue
        facts = extract_key_facts(loaded, event_roles)
        for s in facts.suggestions:
            items.append(create_author_item(s, source=source, now=now))
        for q in facts.questions:
            # Check if this question was already asked/answered
            existing = next(
                (
                    existing_q
                    for existing_q in case.questions
                    if existing_q.code == q.code and existing_q.upload_id == q.upload_id
                ),
                None,
            )
            if existing is None:
                questions.append(create_author_question(q))

    # 2. Goal facts (if goal chosen)
    if brief is not None:
        goal_id = brief.goal_id
        config = load_use_case(goal_id, config_root)

        # Buildable outcome
        build_facts = extract_goal_build_facts(files, config, config_root)
        if build_facts.enough and build_facts.suggested_build is not None:
            # Check if buildable step already exists in plan
            existing_build = next(
                (
                    i
                    for i in case.items
                    if i.goal_id == goal_id and i.kind == "recipe_step" and "buildable" in i.payload
                ),
                None,
            )
            if existing_build is None:
                items.append(create_author_item(build_facts.suggested_build, source=source, now=now))

        # Time column
        if dec.pair("as_of_column", goal_id) is None:
            primary_file = files[0] if files else None
            if primary_file is not None:
                time_facts = extract_time_facts(primary_file, goal_id=goal_id)
                for s in time_facts.suggestions:
                    items.append(create_author_item(s, source=source, now=now))
                for q in time_facts.questions:
                    questions.append(create_author_question(q))

        # Consent column
        if dec.pair("consent", goal_id) is None:
            primary_file = files[0] if files else None
            if primary_file is not None:
                consent_facts = extract_consent_facts(primary_file, goal_id=goal_id)
                for s in consent_facts.suggestions:
                    items.append(create_author_item(s, source=source, now=now))

    return AuthorResult(
        items=tuple(items),
        questions=tuple(questions),
        doubts=tuple(doubts),
    )
