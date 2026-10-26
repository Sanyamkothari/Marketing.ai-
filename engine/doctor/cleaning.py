"""The Data Doctor cleans, with no AI: Guided setup's format fixes, the Indian-format checks and the mixed-currency
stop, as the visit's TREAT proposals.

One detector (`engine.doctor.visit.DetectorPort`) reads each file three ways and gives back **one proposal per
problem**:

* Guided setup's advisor (`engine.agent.advisor.advise`): number-as-text, mixed dates, yes/no words, spellings,
  placeholder values. Its sure fixes start ticked (the same gate Guided setup uses); a "please check" fix, and every
  step the policy never ticks, waits for the person; its questions (day or month first, unreadable numbers) are asked
  through the protocol.
* The Indian-format checks (`engine.doctor.indian_values`): haan / nahi, lakh / crore. They read more than the
  advisor does, so on a column both fix, this one wins and the advisor's step is dropped.
* The mixed-currency stop (`engine.doctor.currency`): a column holding rupees and dollars is never read as one
  number. The visit stops with a question and nothing is merged; the other checks leave that column alone.

Every fix is a `recipe_step` item whose payload carries the step, the shape `prescription_from_case` reads, so a
ticked item is applied when the person approves.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue

from engine.agent.contracts import AgentConfidence, ProposalKind, ProposalState, RecipeStep, RecipeStepKind
from engine.config import ConfigError, RunMode, load_use_case
from engine.doctor.protocol import Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from pathlib import Path

    from engine.agent.advisor import Advice
    from engine.agent.contracts import Proposal, Question
    from engine.config import UseCaseConfig
    from engine.doctor.case import Case, CaseItem
    from engine.doctor.currency import CurrencyFinding
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.indian_values import LocaleStepProposal
    from engine.doctor.visit import Detected, DetectorPort

__all__ = ["cleaning_detector"]

_LOG = get_logger(__name__)

_FORMAT_TOOL: Final[str] = "find_format_issues"
"""The advisor's format look: only the proposals and questions that rest on it are cleaning."""
_NUMBER_OR_YES_NO: Final[frozenset[RecipeStepKind]] = frozenset(
    {RecipeStepKind.PARSE_NUMBER, RecipeStepKind.MAP_BOOLEAN}
)


def cleaning_detector(
    offers: Sequence[Offer] = (), *, config_root: Path | None = None, clock: Callable[[], datetime] = utc_now
) -> DetectorPort:
    """The visit's cleaning detector. `offers` give the advisor its use case when no goal is chosen yet."""

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        config = _config(offers, brief, config_root)
        items: list[CaseItem] = []
        asks: list[Ask] = []
        for loaded in files:
            found_items, found_asks = _clean(case, loaded, config, config_root, clock())
            items.extend(found_items)
            asks.extend(found_asks)
        return Detected(asks=tuple(asks), items=tuple(items))

    return detect


def _config(offers: Sequence[Offer], brief: GoalBrief | None, root: Path | None) -> UseCaseConfig | None:
    if brief is not None:
        try:
            return load_use_case(brief.goal_id, root)
        except ConfigError:
            return None
    return offers[0].config if offers else None


def _decided(case: Case, kind: str, upload_id: str) -> str | None:
    for item in case.items:
        if item.kind == kind and item.upload_id == upload_id and item.state is ProposalState.ACCEPTED:
            column = item.payload.get("column")
            return column if isinstance(column, str) else None
    return None


def _clean(
    case: Case, loaded: LoadedFile, config: UseCaseConfig | None, root: Path | None, now: datetime
) -> tuple[list[CaseItem], list[Ask]]:
    from engine.doctor.currency import check_currency
    from engine.doctor.indian_values import propose_indian_booleans, propose_indian_numbers

    upload_id = loaded.upload_id
    frame = loaded.frame
    key = _decided(case, "key", upload_id)
    outcome = _decided(case, "outcome_column", upload_id)
    held = _order_log_columns(case, loaded)
    items: list[CaseItem] = []
    asks: list[Ask] = []

    # 1. A column with two currencies stops here; nothing below touches it.
    mixed = {
        finding.column: finding
        for finding in check_currency(frame, primary_key=key, target=outcome).findings
        if finding.code == "CURRENCY_MIXED"
    }
    for finding in mixed.values():
        asks.append(_mixed_ask(finding, upload_id, len(frame)))

    # 2. The Indian-format checks, then 3. the advisor on the columns they did not take.
    locale = [
        proposal
        for proposal in (
            *propose_indian_numbers(frame, primary_key=key, target=outcome),
            *propose_indian_booleans(frame, primary_key=key, target=outcome),
        )
        if proposal.column not in mixed and proposal.column not in held
    ]
    indian = [proposal for proposal in locale if proposal.step is not None]
    never = _never_ticked(config, root)
    for found in indian:
        items.append(_indian_item(found, upload_id, never, now))
    # A finding with no fix (an unknown spelling, only one side seen) is never dropped: the person sees it.
    items.extend(_locale_flag(found, upload_id, now) for found in locale if found.step is None)
    if config is not None:
        # The Indian checks' columns: the advisor's number or yes/no fix (or question) there is the same problem.
        taken = {found.column for found in indian}
        advice_items, advice_asks = _advised(loaded, config, root, key, outcome, never, now)
        for item in advice_items:
            step = _step_of(item)
            if step is None or step.column in mixed:
                continue
            if step.column in held and step.kind in _NUMBER_OR_YES_NO:
                continue  # a customer or order number is a label written in digits, never read as a number
            if step.column in taken and step.kind in _NUMBER_OR_YES_NO:
                continue
            items.append(item)
        asks.extend(
            a for a in advice_asks if a.column not in mixed and a.column not in taken and a.column not in held
        )
    return items, asks


def _order_log_columns(case: Case, loaded: LoadedFile) -> frozenset[str]:
    """The columns of an order log that name a shopper or an order (v3 phase 0): the one a person chose as the
    shopper and the file's order numbers. Empty unless the person has chosen one, so nothing moves otherwise.
    No cleaning step turns these into numbers: `C536379` would blank, and a customer is a label, not a quantity.
    """
    chosen = _decided(case, "order_log_customer", loaded.upload_id)
    if chosen is None:
        return frozenset()
    from engine.doctor.order_log import key_columns

    return key_columns(loaded, chosen)


def _never_ticked(config: UseCaseConfig | None, root: Path | None) -> frozenset[RecipeStepKind]:
    """The step kinds only the person may tick: Guided setup's list, and the data policy's when it is on."""
    from engine.agent.advisor import NEVER_TICKED_STEPS
    from engine.doctor.policy import never_ticked_steps

    return NEVER_TICKED_STEPS if config is None else never_ticked_steps(config, root)


def _step_of(item: CaseItem) -> RecipeStep | None:
    raw = item.payload.get("step")
    return RecipeStep.model_validate(raw) if isinstance(raw, dict) else None


def _fix_item(
    step: RecipeStep,
    *,
    title: str,
    upload_id: str,
    sure: bool,
    never: frozenset[RecipeStepKind],
    now: datetime,
    counts: dict[str, JsonValue],
) -> CaseItem:
    from engine.doctor.visit import _item

    return _item(
        "recipe_step",
        {"upload_id": upload_id, "path": None, "value": None, "step": step.model_dump(mode="json"), **counts},
        title=title,
        upload_id=upload_id,
        confidence=AgentConfidence.SURE if sure else AgentConfidence.CHECK,
        evidence=(),
        now=now,
        ticked=sure and step.kind not in never,
    )


def _locale_flag(found: LocaleStepProposal, upload_id: str, now: datetime) -> CaseItem:
    """A locale finding with no step: a flag on the approval page, so a column left unread is not left unsaid."""
    from engine.doctor.detect_time_keys import CHECK_KIND
    from engine.doctor.visit import _item

    payload: dict[str, JsonValue] = {
        "upload_id": upload_id,
        "code": "LOCALE_UNREAD",
        "level": "flag",
        "column": found.column,
        "numbers": {"non_empty": found.non_empty, "unknown": found.unknown_words},
    }
    return _item(
        CHECK_KIND,
        payload,
        title=f"'{found.column}' is left as it is: {found.reason}.",
        upload_id=upload_id,
        confidence=AgentConfidence.CHECK,
        evidence=(),
        now=now,
    )


def _indian_item(
    found: LocaleStepProposal, upload_id: str, never: frozenset[RecipeStepKind], now: datetime
) -> CaseItem:
    assert found.step is not None
    what = (
        f"Read lakh, crore, K and M in '{found.column}' as numbers"
        if found.tool == "parse_indian_numbers"
        else f"Read haan, nahi and the other yes/no words in '{found.column}' as yes and no"
    )
    return _fix_item(
        found.step,
        title=f"{what}: {found.convertible:,} of {found.non_empty:,} values read. {found.reason}.",
        upload_id=upload_id,
        sure=found.confidence == "sure",
        never=never,
        now=now,
        counts={"non_empty": found.non_empty, "convertible": found.convertible, "failed": found.failed},
    )


def _advised(
    loaded: LoadedFile,
    config: UseCaseConfig,
    root: Path | None,
    key: str | None,
    outcome: str | None,
    never: frozenset[RecipeStepKind],
    now: datetime,
) -> tuple[list[CaseItem], list[Ask]]:
    """Guided setup's format fixes for the file (its `find_format_issues` proposals and questions) and its leak finds."""
    from engine.agent.advisor import advise
    from engine.agent.tools import AgentContext

    ctx = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=root,
        upload_id=loaded.upload_id,
        mode=RunMode.TRAIN,
        profile=loaded.profile,
        frame=loaded.frame,
    )
    try:
        advice = advise(ctx, primary_key=key, target=outcome, id_prefix=f"c{loaded.upload_id[-6:]}-")
    except Exception as exc:  # a file the advisor cannot read is not a reason to stop the visit
        _LOG.info("doctor.cleaning_advise_failed upload=%s error=%s", loaded.upload_id, type(exc).__name__)
        return [], []
    cited = {r.evidence_id for r in advice.tool_results if r.tool == _FORMAT_TOOL}
    items = [
        _fix_item(
            proposal.step,
            title=f"{proposal.title}. {proposal.reason}",
            upload_id=loaded.upload_id,
            sure=proposal.confidence is AgentConfidence.SURE,
            never=never,
            now=now,
            counts={},
        )
        for proposal in advice.proposals
        if proposal.kind is ProposalKind.RECIPE_STEP
        and proposal.step is not None
        and cited & set(proposal.evidence_ids)
    ]
    asks = [
        _guided_ask(question, loaded.upload_id, len(loaded.frame))
        for question in advice.questions
        if question.answer is None and cited & set(question.evidence_ids)
    ]
    # A column an open question quotes (day or month first) is not read yet, so its leak look is not trusted yet.
    skip = {key, outcome, *(a.column for a in asks)}
    skip.update(name for name in map(str, loaded.frame.columns) if any(f"'{name}'" in a.text for a in asks))
    items.extend(_leak_items(advice, loaded.upload_id, skip, never, now))
    return items, asks


def _leak_items(
    advice: Advice, upload_id: str, skip: set[str | None], never: frozenset[RecipeStepKind], now: datetime
) -> list[CaseItem]:
    """A column the advisor suspects holds the answer: a proposal to remove it.

    One the leak check proves (it tells yes from no almost by itself) is the recommended default, ticked and
    stated in the plan; the person can untick it. One only suspected stays a check for the person to tick.

    An ID-named column (`customer_id` repeated over monthly rows) is not proposed: removing it loses who the row is
    about; the first look still lists it among the columns that may hold the answer.
    """
    from engine.doctor.first_look import advised_leaks
    from engine.stages.ingest import is_key_like_name

    proven = set(advised_leaks(advice, proven=True))
    return [
        _fix_item(
            RecipeStep(
                order=1,
                kind=RecipeStepKind.DROP_COLUMN,
                column=column,
                reason=f"'{column}' may already hold the answer, so a model would learn from it what it cannot know.",
            ),
            title=f"Remove '{column}': it looks like it gives away the outcome (it tells yes from no almost by itself).",
            upload_id=upload_id,
            sure=column in proven,
            never=never,
            now=now,
            counts={},
        )
        for column in advised_leaks(advice)
        if column not in skip and not is_key_like_name(column)
    ]


def _option_effect(option_id: str, proposal: Proposal | None, upload_id: str) -> OptionEffect:
    if proposal is None or proposal.step is None:
        return OptionEffect(option_id=option_id)
    return OptionEffect(
        option_id=option_id,
        kind="recipe_step",
        title=proposal.title,
        payload={
            "upload_id": upload_id,
            "path": None,
            "value": None,
            "step": proposal.step.model_dump(mode="json"),
        },
    )


def _guided_ask(question: Question, upload_id: str, rows: int) -> Ask:
    """A Guided-setup question (day or month first, unreadable numbers) as a protocol ask."""
    steps = [o.proposal.step for o in question.options if o.proposal is not None and o.proposal.step]
    column = steps[0].column if steps else None
    dates = any(step.kind is RecipeStepKind.PARSE_DATE for step in steps)
    return Ask(
        code="GUIDED_DATE_ORDER" if dates else "GUIDED_FORMAT",
        subject={"upload_id": upload_id, "column": column, "text": question.text},
        text=question.text,
        why="",
        options=tuple(
            AskOption(
                label=option.label,
                effect=_option_effect(f"c{index}", option.proposal, upload_id),
                safe=option.proposal is None,
                says=option.effect,
            )
            for index, option in enumerate(question.options)
        ),
        rows=rows,
        upload_id=upload_id,
        column=column,
        blocking=question.blocking,
    )


def _mixed_ask(finding: CurrencyFinding, upload_id: str, rows: int) -> Ask:
    """CURRENCY_MIXED: a blocking question. No answer converts or adds the amounts up; the column stays as it is."""
    options = [
        AskOption(
            label=option.label,
            effect=OptionEffect(option_id=f"c{index}"),
            safe=option.key == "new_file",
        )
        for index, option in enumerate(finding.question.options)
    ]
    return Ask(
        code="CURRENCY_MIXED",
        subject={"upload_id": upload_id, "column": finding.column},
        text=finding.question.text,
        why="Amounts in different currencies are never added together.",
        options=tuple(options),
        rows=rows,
        upload_id=upload_id,
        column=finding.column,
        blocking=True,
        meaning_bearing=True,
    )
