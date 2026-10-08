"""The advisor (Plan G §5.3, §7, DEC-1002): every proposal and question, from tool results, by rules.

No model is involved. The advisor calls the helper's own read tools, so every number it states is
a recorded `ToolResult` a proposal cites, and turns what they found into:

* **role proposals** - the ID column and the outcome column (training), or a question when the file
  leaves them open;
* **recipe proposals** - one per formatting problem the detector found, or a question when a fix
  needs a decision (which way round a date is, what to do with values that cannot be read);
* **checks on the prepared data** - the Run button's checks, run on a preview of the file with the
  sure fixes applied, so a column of numbers stored as text is not mistaken for an ID. A suspected
  leak becomes a blocking question; a problem no setting can fix (too few rows, too few "yes"
  cases, an ID that repeats) stops the session with the check's own message and suggestion, and so
  does any other error the suggested settings leave in place (they are checked again when one is
  there), so a session never reaches "ready" with an error the Run button will refuse;
* **setting proposals** - `engine.agent.recommend`, never naming a column a leak question may hide;
* **a question to combine rows** (level 3, M76) - when the use case allows `reshape`, a training
  file whose ID-named column repeats and that has a date column gets a blocking question instead of
  a stop: "Each customer appears on N rows on average. Combine them into one row per customer?".
  Choosing Combine adds a `combine_rows` step (`engine.agent.reshape`), and the advisor looks at the
  file again as it will be once combined; choosing Stop stops with the Run button's own message.

Hiding a column is a recipe `drop_column` step, so scoring drops it the same way (DEC-1006).
`summarise` turns whatever the user has decided into the four lists read before Approve.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from engine.agent.config import AgentLevel
from engine.agent.contracts import (
    AgentConfidence,
    AgentSummary,
    Proposal,
    ProposalKind,
    ProposalState,
    Question,
    QuestionOption,
    RecipeStep,
    RecipeStepKind,
    SessionStatus,
    ToolResult,
)
from engine.agent.formats import merge_from_pairs
from engine.agent.placeholders import PLACEHOLDER_KIND, number_text
from engine.agent.recipe import STEP_PHASE, RecipeError, run_recipe
from engine.agent.recommend import DataFacts, recommend_settings, suggest_reason_phrases
from engine.agent.reshape import choose_dates, plan_combine
from engine.agent.tools import AgentContext, AgentToolError, call_tool
from engine.agent.untrusted import MAX_NAME_CHARS, clean_text, display_name, quoted
from engine.config import (
    ColumnType,
    ConfigError,
    RunMode,
    SplitType,
    advanced_settings_schema,
    resolve_config,
)
from engine.stages import ingest
from engine.utils.logging import get_logger

__all__ = [
    "NEVER_TICKED_STEPS",
    "ROLE_PRIMARY_KEY",
    "ROLE_TARGET",
    "STOP_CODES",
    "Advice",
    "advise",
    "run_overrides",
    "summarise",
]

_LOGGER = get_logger(__name__)

ROLE_PRIMARY_KEY: Final[str] = "primary_key"
ROLE_TARGET: Final[str] = "target"
"""A role proposal's `path` names the `POST /runs` field it fills, not an override path."""

STOP_CODES: Final[frozenset[str]] = frozenset(
    {
        "PK_MISSING",
        "PK_NOT_UNIQUE",
        "PK_NULLS",
        "TARGET_MISSING",
        "TARGET_NOT_BINARY",
        "TARGET_CONSTANT",
        "TARGET_TOO_FEW_POSITIVES",
        "ROWS_TOO_FEW",
        "CONSENT_COLUMN_MISSING",
        "SCHEMA_MISMATCH",
    }
)
"""Errors no suggestion can fix: the session stops and says what data would."""

_ENGINE_HIDES: Final[Mapping[str, str]] = {
    "HIGH_NULL_COLUMN": "mostly empty",
    "CONSTANT_COLUMN": "the same value in every row",
    "HIGH_CARDINALITY_ID_LIKE": "looks like an ID, not a signal",
    "PII_DETECTED": "personal data",
}
"""Warnings the engine already acts on by itself: shown as hidden columns, never proposed."""

NEVER_TICKED_STEPS: Final[frozenset[RecipeStepKind]] = frozenset({RecipeStepKind.SET_MISSING})
"""Steps only the person may tick, whatever `agent.tick_uncertain` says: emptying a value is right only
when it really is a code for "unknown", which the file cannot prove (DEC-1224)."""

_MIN_TIME_DISTINCT: Final[int] = 3
_MAX_OPTIONS: Final[int] = 4


def _rows(result: ToolResult, key: str) -> list[dict[str, Any]]:
    """`result[key]` as a list of objects; tool results are plain JSON, typed loosely."""
    value: Any = result.result.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _names(result: ToolResult, key: str) -> list[str]:
    """`result[key]` as a list of strings."""
    value: Any = result.result.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def _value(result: ToolResult | None, key: str) -> Any:
    return None if result is None else result.result.get(key)


@dataclass(frozen=True)
class Advice:
    """Everything the advisor found for one file, before the user decides anything."""

    status: SessionStatus
    tool_results: tuple[ToolResult, ...]
    proposals: tuple[Proposal, ...]
    questions: tuple[Question, ...]
    assumptions: tuple[str, ...]
    engine_hidden: tuple[str, ...]
    stop_reason: str | None
    primary_key: str | None
    target: str | None


@dataclass
class _Builder:
    ctx: AgentContext
    prefix: str = ""
    decided_steps: tuple[RecipeStep, ...] = ()
    results: list[ToolResult] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    engine_hidden: list[str] = field(default_factory=list)
    _evidence: Iterable[int] = field(default_factory=lambda: itertools.count(1))
    _proposal: Iterable[int] = field(default_factory=lambda: itertools.count(1))
    _question: Iterable[int] = field(default_factory=lambda: itertools.count(1))

    def call(
        self, tool: str, args: Mapping[str, Any] | None = None, ctx: AgentContext | None = None
    ) -> ToolResult:
        result = call_tool(
            ctx or self.ctx, tool, args, evidence_id=f"{self.prefix}e{next(iter(self._evidence))}"
        )
        self.results.append(result)
        return result

    def proposal_id(self) -> str:
        return f"{self.prefix}p{next(iter(self._proposal))}"

    def question_id(self) -> str:
        return f"{self.prefix}q{next(iter(self._question))}"

    def propose(self, **values: Any) -> Proposal:
        proposal = Proposal(proposal_id=self.proposal_id(), **values)
        self.proposals.append(proposal)
        return proposal

    def preview_steps(self) -> tuple[RecipeStep, ...]:
        """The steps the preview runs: the ticked suggestions and the steps the person accepted.

        A step the person accepted replaces a ticked one of the same kind on the same column.
        """
        ticked = recipe_steps(p for p in self.proposals if _ticked(p, self.ctx.config.agent.tick_uncertain))
        decided = {(step.kind, step.column) for step in self.decided_steps}
        return ordered_steps(
            [*(step for step in ticked if (step.kind, step.column) not in decided), *self.decided_steps]
        )

    def asked_date_columns(self) -> set[str]:
        """The columns a question asks the day/month order of (an option adds their `parse_date`)."""
        return {
            option.proposal.step.column
            for question in self.questions
            for option in question.options
            if option.proposal is not None
            and option.proposal.step is not None
            and option.proposal.step.kind is RecipeStepKind.PARSE_DATE
        }

    def option_proposal(self, **values: Any) -> Proposal:
        return Proposal(proposal_id=self.proposal_id(), **values)

    def ask(
        self,
        text: str,
        options: Sequence[QuestionOption],
        evidence: Sequence[str],
        *,
        blocking: bool = True,
        examples: Sequence[str] = (),
    ) -> None:
        self.questions.append(
            Question(
                question_id=self.question_id(),
                text=text,
                options=tuple(options),
                blocking=blocking,
                evidence_ids=tuple(evidence),
                examples=tuple(examples),
            )
        )


def _role(
    builder: _Builder,
    path: str,
    column: str,
    evidence: str,
    confidence: AgentConfidence,
    reason: str | None = None,
) -> Proposal:
    what = "the column that identifies each row" if path == ROLE_PRIMARY_KEY else "the outcome to predict"
    return builder.propose(
        kind=ProposalKind.ROLE,
        title=f"Use '{display_name(column)}' as {what}",
        reason=reason
        or (
            "It is unique and never empty, and its name looks like an ID."
            if path == ROLE_PRIMARY_KEY
            else "Its name matches what this use case predicts."
        ),
        path=path,
        value=column,
        suggested_value=column,
        evidence_ids=(evidence,),
        confidence=confidence,
    )


def _role_option(builder: _Builder, path: str, column: str, evidence: str) -> QuestionOption:
    return QuestionOption(
        # A stable digest: `hash()` of a str changes per process, and two columns could collide.
        option_id="use-" + hashlib.sha256(column.encode("utf-8")).hexdigest()[:12],
        label=display_name(column),
        effect=f"Use '{display_name(column)}'.",
        proposal=builder.option_proposal(
            kind=ProposalKind.ROLE,
            title=f"Use '{display_name(column)}' as {'the ID column' if path == ROLE_PRIMARY_KEY else 'the outcome'}",
            reason="You chose it.",
            path=path,
            value=column,
            suggested_value=column,
            evidence_ids=(evidence,),
            confidence=AgentConfidence.SURE,
        ),
    )


_COMBINED_KEY_REASON: Final[str] = "Its name looks like an ID, and once the rows are combined it is unique."


def _repeats(builder: _Builder, column: str) -> bool:
    """Whether a value of `column` appears on more than one row."""
    return bool(builder.ctx.column(column).dropna().duplicated().any())


def _choose_primary_key(
    builder: _Builder, profile: ToolResult, chosen: str | None, *, reshape: bool = False
) -> tuple[str | None, str | None]:
    """(column, stop reason). Proposes or asks; stops when no column can identify a row.

    With `reshape` (the use case allows combining rows and nobody has declined it), an ID-named
    column that repeats is proposed as the key instead of stopping: `advise` then asks whether to
    combine the rows. `reshape` is also set once the rows are combined, for the combined key's reason.
    """
    if chosen is not None:
        repeating = reshape and _repeats(builder, chosen)
        _role(
            builder,
            ROLE_PRIMARY_KEY,
            chosen,
            profile.evidence_id,
            AgentConfidence.SURE,
            _COMBINED_KEY_REASON if repeating else None,
        )
        return chosen, None
    candidates = _names(profile, "primary_key_candidates")
    hints = {h.casefold() for h in builder.ctx.config.primary_key_hints}
    if candidates:
        best = candidates[0]
        sure = best.casefold() in hints or len(candidates) == 1
        _role(
            builder,
            ROLE_PRIMARY_KEY,
            best,
            profile.evidence_id,
            AgentConfidence.SURE if sure else AgentConfidence.CHECK,
        )
        return best, None
    hinted = [str(c) for c in builder.ctx.frame.columns if str(c).casefold() in hints]
    if hinted and reshape and _repeats(builder, hinted[0]):
        _role(
            builder,
            ROLE_PRIMARY_KEY,
            hinted[0],
            profile.evidence_id,
            AgentConfidence.SURE,
            _COMBINED_KEY_REASON,
        )
        return hinted[0], None
    if hinted:
        # An ID-named column exists but repeats or has blanks: the Run button's own check says it best.
        reason = _repeating_key_stop(builder, hinted[0])
        if reason is not None:
            return None, reason
    return None, _no_key_stop(builder)


def _repeating_key_stop(builder: _Builder, key: str) -> str | None:
    """The Run button's own message about an ID column that repeats or has blanks, or None."""
    return _stop_from_checks(builder.call("check_data", {"primary_key": key}))


def _no_key_stop(builder: _Builder) -> str:
    entity = builder.ctx.config.entity
    return (
        f"No column holds a different value on every row, so the file does not say which {entity} each row is. "
        f"The model needs one row per {entity} with an ID column; if a {entity} appears on several rows, "
        "combine them into one row first, or build the data from raw tables."
    )


def _choose_target(
    builder: _Builder, roles: ToolResult, profile: ToolResult, chosen: str | None, primary_key: str | None
) -> tuple[str | None, str | None]:
    """(column, stop reason) for a training file."""
    if chosen is not None:
        _role(builder, ROLE_TARGET, chosen, roles.evidence_id, AgentConfidence.SURE)
        return chosen, None
    exact = [c["column"] for c in _rows(roles, "target_exact")]
    folded = [c["column"] for c in _rows(roles, "target_case_insensitive")]
    synonyms = [c["column"] for c in _rows(roles, "target_by_synonym")]
    if exact or folded:
        column = str((exact or folded)[0])
        _role(builder, ROLE_TARGET, column, roles.evidence_id, AgentConfidence.SURE)
        return column, None
    if len(synonyms) == 1:
        column = str(synonyms[0])
        _role(builder, ROLE_TARGET, column, roles.evidence_id, AgentConfidence.CHECK)
        return column, None
    # Every column, not only the page `get_profile` lists: the same profile the tool reports from.
    options = [str(c) for c in synonyms] or [
        c.name
        for c in builder.ctx.profile.columns
        if c.distinct_count == 2 and c.name != primary_key and not c.pii_kinds
    ]
    definition = builder.ctx.config.target.definition or "the outcome"
    if not options:
        configured = builder.ctx.config.target.column
        return None, (
            f"No column says whether {definition.lower()}. The model learns from past rows where the answer "
            f"is known, so the file needs that column (this use case calls it '{display_name(configured)}'), with two values "
            "such as 1 and 0."
        )
    if len(options) == 1:
        # A question needs two choices: one candidate is proposed for the person to accept instead.
        _role(
            builder,
            ROLE_TARGET,
            options[0],
            roles.evidence_id,
            AgentConfidence.CHECK,
            reason=f"It is the only column with two values, so it may say whether {definition.lower()}. Check it.",
        )
        return options[0], None
    builder.ask(
        f"Which column says whether {definition.lower()}?",
        [_role_option(builder, ROLE_TARGET, column, roles.evidence_id) for column in options[:_MAX_OPTIONS]],
        [roles.evidence_id, profile.evidence_id],
    )
    return None, None


def _step(kind: RecipeStepKind, column: str, **params: Any) -> RecipeStep:
    return RecipeStep(order=1, kind=kind, column=column, params=params)


def _recipe_proposal(
    builder: _Builder,
    step: RecipeStep,
    title: str,
    reason: str,
    evidence: str,
    confidence: AgentConfidence,
    examples: Sequence[str] = (),
) -> Proposal:
    return builder.propose(
        kind=ProposalKind.RECIPE_STEP,
        title=title,
        reason=reason,
        step=step.model_copy(update={"reason": reason}),
        evidence_ids=(evidence,),
        confidence=confidence,
        examples=tuple(examples),
    )


_QUOTED_EXAMPLES: Final[frozenset[str]] = frozenset(
    {"boolean_as_text", "category_variants", "untrimmed_text"}
)
"""Issue kinds whose examples `formats.py` already quotes: `'Basic' → 'BASIC'`, `'Y' (749)`."""


def _examples(issue: Mapping[str, Any], key: str = "examples") -> str:
    """Up to three of an issue's examples for a reason: `'1,200', '3,400'` - one quoting style
    (`quoted`), never a Python tuple and never a pair wrapped in a second pair of quotes."""
    values = [str(value) for value in list(issue.get(key, []) or [])[:3]]
    ready = key == "examples" and str(issue.get("kind")) in _QUOTED_EXAMPLES
    return ", ".join(values if ready else [quoted(value) for value in values])


def _note_unknown_hidden_columns(builder: _Builder) -> None:
    """A column named in `agent.always_hide_columns` that the file does not have hides nothing: say so.

    A typo (`custmer_notes`) would otherwise be a silent no-op and the column it meant would reach the chat
    model. The note is a warning in the session's assumptions and in the log; it never blocks the session."""
    have = {str(c).casefold() for c in builder.ctx.frame.columns} | {
        display_name(c).casefold() for c in builder.ctx.frame.columns
    }
    for entry in builder.ctx.config.agent.always_hide_columns:
        if entry.casefold() not in have:
            _LOGGER.warning("agent.always_hide_columns names a column the file does not have")
            builder.assumptions.append(
                f"Warning: agent.always_hide_columns lists '{display_name(entry)}', which is not a column of this "
                "file, so it hides nothing. Check the spelling; the column you meant is not hidden from the chat."
            )


def _cells(issue: Mapping[str, Any], key: str = "examples") -> tuple[str, ...]:
    """The cell values `_examples(issue, key)` writes into a sentence, each as one string.

    A proposal or question carries them (`examples`) so the copy of the sentence that goes to the chat
    model can be built from them - each one its shape or a hidden marker - instead of by reading quotes
    back out of prose."""
    if key == "examples" and str(issue.get("kind")) in _QUOTED_EXAMPLES:
        return tuple(str(value) for value in issue.get("example_cells", []) or [])
    return tuple(str(value) for value in list(issue.get(key, []) or [])[:3])


def _values_text(texts: Sequence[str]) -> str:
    """`'99'`, `'99' and '999'`, `'-1', '99' and '999'`."""
    shown = [quoted(text) for text in texts]
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def _placeholder_proposal(
    builder: _Builder, issues: ToolResult, issue: Mapping[str, Any], target: str | None
) -> None:
    """Suggest emptying a column's placeholder codes, with what that does to the data (DEC-1220, DEC-1222).

    Always `check`, and never ticked for the person (`NEVER_TICKED_STEPS`). The impact is a tool result the
    suggestion cites, so every number the screen shows next to it was measured on the file.
    """
    column = str(issue["column"])
    values = [float(v) for v in dict(issue.get("params", {}) or {}).get("values", [])]
    if not values:
        return
    low, high = float(issue["other_minimum"]), float(issue["other_maximum"])
    impact = builder.call(
        "describe_placeholder_values",
        {"column": column, "values": values},
        ctx=replace(builder.ctx, target=target),
    )
    texts = [number_text(v) for v in values]
    shown = _values_text(texts)
    rows = int(issue["convertible"])
    lowest, highest = number_text(low), number_text(high)
    edges: tuple[str, ...]
    if all(v > high for v in values):
        edges, where = (highest,), f"far above every other value (the largest is {quoted(highest)})"
    elif all(v < low for v in values):
        edges, where = (lowest,), f"far below every other value (the smallest is {quoted(lowest)})"
    else:
        edges = (lowest, highest)
        where = f"far outside every other value (they run from {quoted(lowest)} to {quoted(highest)})"
    what = (
        "It is probably a code for unknown, not a real value"
        if len(values) == 1
        else "They are probably codes for unknown, not real values"
    )
    reason = (
        f"{rows} rows in '{display_name(column)}' hold {shown}, {where}. {what}. Tick it only if that is "
        "right: the rows are kept and those cells become empty."
    )
    builder.propose(
        kind=ProposalKind.RECIPE_STEP,
        title=f"Treat {shown} in '{display_name(column)}' as missing",
        reason=reason,
        step=_step(
            RecipeStepKind.SET_MISSING, column, values=[int(v) if v.is_integer() else v for v in values]
        ).model_copy(update={"reason": reason}),
        evidence_ids=(issues.evidence_id, impact.evidence_id),
        confidence=AgentConfidence.CHECK,
        examples=tuple(dict.fromkeys((*texts, *edges))),
    )


def _format_proposals(
    builder: _Builder, issues: ToolResult, protected: set[str], target: str | None = None
) -> None:
    limit = builder.ctx.config.agent.max_conversion_failure_pct
    for issue in _rows(issues, "issues"):
        column = str(issue["column"])
        if column in protected:
            continue
        kind = str(issue["kind"])
        evidence = issues.evidence_id
        if kind == PLACEHOLDER_KIND:
            _placeholder_proposal(builder, issues, issue, target)
            continue
        non_empty = int(issue["non_empty"])
        failed = int(issue["failed"])
        share = float(issue["convert_share"])
        params: dict[str, Any] = dict(issue.get("params", {}) or {})
        if "merge" in params:
            params["merge"] = merge_from_pairs(params["merge"])  # the tool lists pairs; the step keeps a dict
        if kind == "number_as_text":
            if non_empty and failed / non_empty * 100.0 > limit:
                bad = _examples(issue, "failed_examples")
                builder.ask(
                    f"Most of '{display_name(column)}' looks like numbers, but {failed} of {non_empty} values cannot be read "
                    f"(for example {bad}). What should happen to this column?",
                    [
                        QuestionOption(
                            option_id="hide",
                            label="Hide it",
                            effect="The model will not use this column.",
                            proposal=builder.option_proposal(
                                kind=ProposalKind.RECIPE_STEP,
                                title=f"Hide '{display_name(column)}'",
                                reason="You chose to hide it.",
                                step=_step(RecipeStepKind.DROP_COLUMN, column),
                                evidence_ids=(evidence,),
                                confidence=AgentConfidence.SURE,
                            ),
                        ),
                        QuestionOption(
                            option_id="keep", label="Leave it as text", effect="It is used as categories."
                        ),
                    ],
                    [evidence],
                    blocking=False,
                    examples=_cells(issue, "failed_examples"),
                )
                continue
            _recipe_proposal(
                builder,
                _step(RecipeStepKind.PARSE_NUMBER, column, **params),
                f"Turn '{display_name(column)}' into numbers",
                f"It is stored as text ({_examples(issue)}), so the model cannot use it as a number; "
                f"{int(issue['convertible'])} of {non_empty} values convert.",
                evidence,
                AgentConfidence.SURE if share >= 0.95 else AgentConfidence.CHECK,
                _cells(issue),
            )
        elif kind == "mixed_dates":
            dayfirst = params.get("dayfirst")
            if dayfirst is None:
                sample = _examples(issue)
                builder.ask(
                    f"In '{display_name(column)}' ({sample}), which comes first: the day or the month?",
                    [
                        QuestionOption(
                            option_id=order,
                            label=label,
                            effect=effect,
                            proposal=builder.option_proposal(
                                kind=ProposalKind.RECIPE_STEP,
                                title=f"Read '{display_name(column)}' as dates, {label.lower()}",
                                reason="You told the helper which way round the dates are.",
                                step=_step(RecipeStepKind.PARSE_DATE, column, dayfirst=order == "day-first"),
                                evidence_ids=(evidence,),
                                confidence=AgentConfidence.SURE,
                            ),
                        )
                        for order, label, effect in (
                            ("day-first", "Day first", "01/02/2024 is 1 February 2024."),
                            ("month-first", "Month first", "01/02/2024 is 2 January 2024."),
                        )
                    ],
                    [evidence],
                    examples=_cells(issue),
                )
                continue
            order = "day first" if dayfirst else "month first"
            _recipe_proposal(
                builder,
                _step(RecipeStepKind.PARSE_DATE, column, dayfirst=bool(dayfirst)),
                f"Read '{display_name(column)}' as dates, {order}",
                f"The dates are written in more than one way ({_examples(issue)}); values such as a day above 12 "
                f"show they are {order}.",
                evidence,
                AgentConfidence.SURE,
                _cells(issue),
            )
        elif kind == "boolean_as_text":
            _recipe_proposal(
                builder,
                _step(RecipeStepKind.MAP_BOOLEAN, column, **params),
                f"Turn the yes / no values in '{display_name(column)}' into 1 and 0",
                f"The same answer is spelled several ways: {_examples(issue)}.",
                evidence,
                AgentConfidence.SURE,
                _cells(issue),
            )
        elif kind in {"category_variants", "untrimmed_text"}:
            merging = bool(params.get("merge"))
            _recipe_proposal(
                builder,
                _step(RecipeStepKind.NORMALISE_TEXT, column, **params),
                (
                    f"Merge different spellings in '{display_name(column)}'"
                    if merging
                    else f"Remove extra spaces in '{display_name(column)}'"
                ),
                (
                    f"The same value is written differently ({_examples(issue)}), so it would count as different values."
                    if merging
                    else "Some values start or end with spaces, so they would not match the same value without them."
                ),
                evidence,
                AgentConfidence.SURE,
                _cells(issue) if merging else (),
            )


def _ticked(proposal: Proposal, tick_uncertain: bool) -> bool:
    if proposal.step is not None and proposal.step.kind in NEVER_TICKED_STEPS:
        return False
    return proposal.confidence is AgentConfidence.SURE or tick_uncertain


def ordered_steps(steps: Iterable[RecipeStep]) -> tuple[RecipeStep, ...]:
    """`steps` in the engine's fixed order (stable within a phase) and numbered 1..n."""
    ordered = sorted(steps, key=lambda step: STEP_PHASE[step.kind])
    return tuple(step.model_copy(update={"order": order}) for order, step in enumerate(ordered, start=1))


def recipe_steps(proposals: Iterable[Proposal]) -> tuple[RecipeStep, ...]:
    """The recipe steps of `proposals`, in the engine's fixed order and numbered 1..n."""
    return ordered_steps(
        p.step for p in proposals if p.kind is ProposalKind.RECIPE_STEP and p.step is not None
    )


def _prepared_context(
    builder: _Builder,
    steps: Sequence[RecipeStep],
    primary_key: str | None,
    target: str | None,
    *,
    strict: bool = False,
) -> AgentContext:
    """A context over the preview of the prepared file, so checks see cleaned (and combined) values.

    A recipe that cannot run on the preview leaves the file as it is, unless `strict`: combined rows
    are the whole point of a combine, so a combine that fails raises and the advisor stops on it.
    """
    ctx = builder.ctx
    if not steps:
        return ctx
    try:
        run = run_recipe(
            ctx.frame,
            steps,
            upload_id=ctx.upload_id,
            primary_key=primary_key,
            target=target,
            levels=ctx.config.agent.levels,
            max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
        )
    except RecipeError:
        if strict:
            raise
        return ctx
    combined = any(step.kind is RecipeStepKind.COMBINE_ROWS for step in steps)
    profile = ingest.profile_dataset(
        run.frame,
        ctx.config,
        upload_id=ctx.upload_id,
        file_name=ctx.profile.file_name,
        file_format=ctx.profile.file_format,
        file_size_bytes=ctx.profile.file_size_bytes,
        delimiter=ctx.profile.delimiter,
        encoding=ctx.profile.encoding,
        row_count=len(run.frame) if combined else ctx.profile.row_count,
    )
    return replace(ctx, frame=run.frame, profile=profile)


def _ask_to_combine(
    builder: _Builder, profile: ToolResult, key: str, target: str, protected: set[str]
) -> str | None:
    """Ask whether to combine the rows of each entity (M76); the stop reason when they cannot be.

    The plan is made on the preview with the sure fixes applied, so a number stored as text is
    added up as a number; the features it freezes are listed in the step's parameters.
    """
    ctx = builder.ctx
    prepared = _prepared_context(builder, builder.preview_steps(), key, target)
    others = {name for name in protected if name in prepared.frame.columns} - {key, target}
    dates = choose_dates(
        prepared.profile, exclude=[key, target, *sorted(others)], snapshot_hint=ctx.config.split.time_column
    )
    if dates is None:
        entity = ctx.config.entity
        reason = _repeating_key_stop(builder, key) or _no_key_stop(builder)
        return (
            f"{reason} The rows could be combined into one per {entity}, but that needs a date column "
            "saying when each row happened, and this file has none."
        )
    time_column, snapshot_column = dates
    params = plan_combine(
        prepared.frame,
        prepared.profile,
        key=key,
        time_column=time_column,
        snapshot_column=snapshot_column,
        outcome=target,
        carry=sorted(others - {time_column, snapshot_column}),
    )
    repeats = builder.call("describe_repeats", {"column": key})
    per, most = _value(repeats, "rows_per_id"), _value(repeats, "most_rows")
    entity = ctx.config.entity
    as_of = (
        f"the latest '{display_name(snapshot_column)}' of each {entity}"
        if snapshot_column is not None
        else f"each {entity}'s latest '{display_name(time_column)}'"
    )
    reason = (
        f"Each {entity} appears on {per:g} rows on average (up to {most}), and the model needs one row per "
        f"{entity}. Per '{display_name(key)}', the rows are counted and their values added up, averaged or taken from the "
        f"latest row, using only rows dated on or before {as_of}; '{display_name(target)}' is read from the {entity}'s "
        "latest row."
    )
    if snapshot_column is None:
        reason += (
            " Every row counts, so the file should hold only rows known before the outcome was measured."
        )
    evidence = (repeats.evidence_id, profile.evidence_id)
    step = RecipeStep(order=1, kind=RecipeStepKind.COMBINE_ROWS, column=key, params=params, reason=reason)
    builder.ask(
        f"Each {entity} appears on {per:g} rows on average. Combine them into one row per {entity}?",
        [
            QuestionOption(
                option_id="combine",
                label="Combine (recommended)",
                effect=f"One row per {entity}, built only from its rows on or before its snapshot date.",
                proposal=builder.option_proposal(
                    kind=ProposalKind.RECIPE_STEP,
                    title=f"Combine the rows into one row per {entity}",
                    reason=reason,
                    step=step,
                    evidence_ids=evidence,
                    confidence=AgentConfidence.SURE,
                ),
            ),
            QuestionOption(
                option_id="stop",
                label="Stop",
                effect=f"Nothing is changed; the file cannot be used until it has one row per {entity}.",
            ),
        ],
        evidence,
    )
    return None


def _leaks(checks: ToolResult) -> list[dict[str, Any]]:
    """The suspected leaks that are errors: each becomes a question whether to hide the column."""
    return [
        check
        for check in _rows(checks, "checks")
        if check["code"] == "LEAKAGE_SUSPECTED" and check["severity"] == "error" and check["column"]
    ]


def _leak_questions(builder: _Builder, checks: ToolResult) -> None:
    for check in _leaks(checks):
        column = str(check["column"])
        builder.ask(
            f"'{display_name(column)}' almost perfectly predicts the outcome, so it may contain the answer "
            "(for example, something recorded after the outcome happened). Hide it?",
            [
                QuestionOption(
                    option_id="hide",
                    label="Hide it (recommended)",
                    effect="The model will not see this column.",
                    proposal=builder.option_proposal(
                        kind=ProposalKind.RECIPE_STEP,
                        title=f"Hide '{display_name(column)}'",
                        reason="It may contain the answer.",
                        step=_step(RecipeStepKind.DROP_COLUMN, column),
                        evidence_ids=(checks.evidence_id,),
                        confidence=AgentConfidence.SURE,
                    ),
                ),
                QuestionOption(
                    option_id="keep",
                    label="Keep it, it is known in advance",
                    effect="The warning is recorded as expected.",
                    proposal=builder.option_proposal(
                        kind=ProposalKind.ACKNOWLEDGEMENT,
                        title=f"Keep '{display_name(column)}'",
                        reason="You confirmed it is known before the outcome.",
                        path="validation.acknowledged",
                        value=str(check["acknowledge"] or f"LEAKAGE_SUSPECTED:{column}"),
                        suggested_value=str(check["acknowledge"] or f"LEAKAGE_SUSPECTED:{column}"),
                        evidence_ids=(checks.evidence_id,),
                        confidence=AgentConfidence.SURE,
                    ),
                ),
            ],
            [checks.evidence_id],
        )


def _stop_from_checks(checks: ToolResult) -> str | None:
    for check in _rows(checks, "checks"):
        if check["severity"] == "error" and check["code"] in STOP_CODES and not check.get("acknowledged"):
            suggestion = str(check.get("suggestion") or "")
            return f"{check['message']} {suggestion}".strip()
    return None


def _open_errors(checks: ToolResult, asked: set[str]) -> list[dict[str, Any]]:
    """Errors that would make the Run button refuse, other than the leaks a question asks about."""
    return [
        check
        for check in _rows(checks, "checks")
        if check["severity"] == "error"
        and not check.get("acknowledged")
        and not (check["code"] == "LEAKAGE_SUSPECTED" and check["column"] in asked)
    ]


def _unfixed_stop(
    builder: _Builder, checks: ToolResult, prepared: AgentContext, key: str | None, target: str | None
) -> str | None:
    """The stop reason when an error is left that no stop, question or suggested setting deals with.

    The checks are run again with the settings that start ticked, so an error a suggestion fixes (a
    random split for a file without the use case's date column) does not stop the session.
    """
    asked = {str(check["column"]) for check in _leaks(checks)} if builder.ctx.mode is RunMode.TRAIN else set()
    if not _open_errors(checks, asked):
        return None
    tick_uncertain = builder.ctx.config.agent.tick_uncertain
    overrides = {
        str(p.path): p.value
        for p in builder.proposals
        if p.kind is ProposalKind.SETTING and _ticked(p, tick_uncertain)
    }
    # The suggestions resolve together by construction; should they not, the first check stands.
    with contextlib.suppress(AgentToolError):
        if overrides:
            checks = builder.call(
                "check_data", {"primary_key": key, "target": target, "overrides": overrides}, ctx=prepared
            )
    remaining = _open_errors(checks, asked)
    if not remaining:
        return None
    first = remaining[0]
    return f"{first['message']} {first.get('suggestion') or ''}".strip()


def _notes_from_checks(builder: _Builder, checks: ToolResult) -> None:
    reasons: dict[str, list[str]] = {}
    for check in _rows(checks, "checks"):
        code, column = str(check["code"]), check.get("column")
        if code in _ENGINE_HIDES and column:
            reasons.setdefault(str(column), []).append(_ENGINE_HIDES[code])
        elif code == "SUPPRESSION_COLUMN_MISSING":
            builder.assumptions.append(str(check["message"]))
    # One line per column; personal data first, because it is the reason that matters most.
    for column, why in reasons.items():
        ordered = sorted(dict.fromkeys(why), key=lambda reason: reason != _ENGINE_HIDES["PII_DETECTED"])
        builder.engine_hidden.append(f"{display_name(column)}: {', '.join(ordered)}")


def _facts(
    builder: _Builder,
    prepared: AgentContext,
    outcome: ToolResult | None,
    roles: ToolResult,
    *,
    exclude: Iterable[str] = (),
    checks: ToolResult | None = None,
) -> DataFacts:
    """What `recommend_settings` may know; `exclude` are columns no setting may name (suspected leaks).

    `checks` is the Run button's check of the prepared file: a date column it cannot read is told
    from one it reads but that has too few dates, so a suggestion never says the wrong one.
    """
    columns = {column.name: column for column in prepared.profile.columns}
    ordered = list(prepared.profile.time_column_candidates) + [
        name
        for name, column in columns.items()
        if column.inferred_type in {ColumnType.DATE, ColumnType.DATETIME}
    ]
    time_columns: list[str] = []
    reserved = {builder.ctx.config.actions.suppression.recently_contacted_column, *exclude}
    for name in ordered:
        column = columns.get(name)
        if (
            column is not None
            and name not in time_columns
            and name not in reserved
            and column.inferred_type in {ColumnType.DATE, ColumnType.DATETIME}
            and column.distinct_count >= _MIN_TIME_DISTINCT
        ):
            time_columns.append(name)
    rate = _value(outcome, "positive_rate")
    taken = {
        builder.ctx.config.actions.suppression.opt_out_column,
        builder.ctx.config.actions.suppression.recently_contacted_column,
        *exclude,
    }
    consent = tuple(c for c in _names(roles, "consent") if c not in taken)
    evidence = tuple(r.evidence_id for r in (roles, outcome) if r is not None)
    trouble, trouble_evidence = _time_column_trouble(
        prepared, columns, time_columns, reserved, set(exclude), checks
    )
    return DataFacts(
        rows=prepared.profile.row_count,
        positive_rate=float(rate) if isinstance(rate, (int, float)) else None,
        time_columns=tuple(time_columns),
        consent_candidates=consent,
        evidence_ids=evidence,
        columns=tuple(str(c) for c in prepared.frame.columns),
        time_column_trouble=trouble,
        time_column_evidence_ids=trouble_evidence,
    )


def _time_column_trouble(
    prepared: AgentContext,
    columns: Mapping[str, Any],
    time_columns: Sequence[str],
    reserved: set[str | None],
    leaks: set[str],
    checks: ToolResult | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Why the use case's own date column is in the file but not a usable date column, and the proof."""
    name = prepared.config.split.time_column
    if not name or name not in prepared.frame.columns or name in time_columns:
        return None, ()
    measured = (checks.evidence_id,) if checks is not None else ()
    if checks is not None and any(
        check["code"] == "TIME_COLUMN_UNPARSEABLE"
        and check["severity"] == "error"
        and check["column"] == name
        for check in _rows(checks, "checks")
    ):
        return "unreadable", measured  # the Run button refuses it whatever the split type
    if name in leaks:
        return "leak", measured
    if name in reserved:
        return "reserved", ()
    column = columns.get(name)
    if (
        column is not None
        and column.inferred_type in {ColumnType.DATE, ColumnType.DATETIME}
        and column.distinct_count < _MIN_TIME_DISTINCT
    ):
        return "few", ()
    return "unusable", ()


def _describe_outcome(builder: _Builder, column: str, ctx: AgentContext | None = None) -> ToolResult:
    outcome = builder.call("describe_outcome", {"column": column}, ctx=ctx)
    label = _value(outcome, "positive_label")
    if label is not None:
        builder.assumptions.append(
            f"'{clean_text(str(label), MAX_NAME_CHARS)}' in '{display_name(column)}' means yes."
        )
    return outcome


def advise(
    ctx: AgentContext,
    *,
    primary_key: str | None = None,
    target: str | None = None,
    id_prefix: str = "",
    combine: RecipeStep | None = None,
    combine_declined: bool = False,
    decided_steps: Sequence[RecipeStep] = (),
) -> Advice:
    """Look at the file and propose everything; `primary_key` / `target` are the user's answers so far.

    `id_prefix` keeps evidence, proposal and question ids unique when a session asks more than once.
    A scoring file gets no fixes of its own: it is prepared by the model's saved recipe (DEC-1006).

    `combine` is the `combine_rows` step the user chose (M76): everything after the roles and the
    format fixes - the checks, the questions about leaks, the settings - is then about the combined
    file. `combine_declined` means the user answered Stop, so a repeating ID stops the session.
    `decided_steps` are the recipe steps the person already accepted (an answered day/month
    question's `parse_date`): the preview runs them, so the combine reads the dates they convert.
    """
    builder = _Builder(ctx, prefix=id_prefix, decided_steps=tuple(decided_steps))
    config = ctx.config
    profile = builder.call("get_profile")
    roles = builder.call("find_roles")
    _note_unknown_hidden_columns(builder)
    reshape = ctx.mode is RunMode.TRAIN and AgentLevel.RESHAPE in config.agent.levels and not combine_declined
    chosen_key = primary_key if primary_key is not None else combine.column if combine is not None else None
    key, stop = _choose_primary_key(builder, profile, chosen_key, reshape=reshape)
    to_combine = combine is None and reshape and key is not None and _repeats(builder, key)
    outcome_column: str | None = None
    if ctx.mode is RunMode.TRAIN and stop is None:
        outcome_column, stop = _choose_target(builder, roles, profile, target, key)
    outcome: ToolResult | None = None
    if outcome_column is not None and combine is None and not to_combine:
        outcome = _describe_outcome(builder, outcome_column)
    protected = {name for name in (key, outcome_column) if name} | {
        name
        for name in (
            config.actions.suppression.opt_out_column,
            config.actions.suppression.recently_contacted_column,
            config.governance.consent_column,
        )
        if name
    }
    if ctx.mode is RunMode.TRAIN:
        issues = builder.call("find_format_issues")
        _format_proposals(builder, issues, protected, outcome_column)
    can_check = outcome_column is not None if ctx.mode is RunMode.TRAIN else ctx.schema is not None
    if to_combine and key is not None and outcome_column is not None and stop is None:
        # Nothing else is checked on rows that are about to be combined: the answer re-advises.
        stop = _ask_to_combine(builder, profile, key, outcome_column, protected)
    elif stop is None and can_check:
        steps = ordered_steps([*builder.preview_steps(), *([combine] if combine is not None else [])])
        waiting = False
        try:
            prepared = _prepared_context(builder, steps, key, outcome_column, strict=combine is not None)
        except RecipeError as exc:
            prepared = ctx
            # A date the day/month question is about is read once it is answered; the answer re-advises.
            waiting = exc.code == "RECIPE_VALUES_UNCONVERTED" and exc.column in builder.asked_date_columns()
            if not waiting:
                stop = f"The rows could not be combined: {exc.message}"
        if stop is None and not waiting:
            if outcome_column is not None and outcome is None:
                outcome = _describe_outcome(builder, outcome_column, prepared)  # one row per entity now
            checks = builder.call("check_data", {"primary_key": key, "target": outcome_column}, ctx=prepared)
            stop = _stop_from_checks(checks)
            _notes_from_checks(builder, checks)
            if stop is None and ctx.mode is RunMode.TRAIN:
                _leak_questions(builder, checks)
                # A column the person may hide is never the one a suggested setting depends on.
                leaks = [str(check["column"]) for check in _leaks(checks)]
                _settings(
                    builder, prepared, outcome, roles, key, outcome_column, exclude=leaks, checks=checks
                )
            if stop is None:
                stop = _unfixed_stop(builder, checks, prepared, key, outcome_column)
    status = (
        SessionStatus.STOPPED
        if stop is not None
        else SessionStatus.NEEDS_REVIEW if builder.proposals or builder.questions else SessionStatus.READY
    )
    return Advice(
        status=status,
        tool_results=tuple(builder.results),
        proposals=tuple(builder.proposals),
        questions=tuple(builder.questions),
        assumptions=tuple(dict.fromkeys(builder.assumptions)),
        engine_hidden=tuple(builder.engine_hidden),
        stop_reason=stop,
        primary_key=key,
        target=outcome_column,
    )


def _settings(
    builder: _Builder,
    prepared: AgentContext,
    outcome: ToolResult | None,
    roles: ToolResult,
    key: str | None,
    target: str | None,
    *,
    exclude: Iterable[str] = (),
    checks: ToolResult | None = None,
) -> None:
    config = prepared.config
    facts = _facts(builder, prepared, outcome, roles, exclude=exclude, checks=checks)
    schema = advanced_settings_schema(
        config, columns=tuple(str(c) for c in prepared.frame.columns), primary_key=key, target=target
    )
    for rec in recommend_settings(config, facts, schema, config_root=prepared.config_root):
        builder.propose(
            kind=ProposalKind.SETTING,
            title=rec.title,
            reason=rec.reason,
            path=rec.path,
            value=rec.value,
            suggested_value=rec.value,
            evidence_ids=rec.evidence_ids,
            confidence=rec.confidence,
        )
    if (
        facts.positive_rate is not None
        and facts.positive_rate < 0.10
        and config.model_search.imbalance.value == "auto"
    ):
        builder.assumptions.append(
            f"Only {facts.positive_rate:.1%} of rows are 'yes'; the model search gives them extra weight automatically."
        )
    _reason_wording(builder, prepared, key, target, exclude)


_REASON_WORDING_SHOWN: Final[int] = 3
"""How many columns the wording note names; the rest are counted."""


def _plain_phrase(phrase: str) -> str:
    """A reason phrase as a person reads it: the template's `{value}` is "the customer's value"."""
    return phrase.replace("{value}", "the customer's value")


def _reason_wording(
    builder: _Builder,
    prepared: AgentContext,
    key: str | None,
    target: str | None,
    exclude: Iterable[str],
) -> None:
    """Note the columns whose reasons have no plain wording yet, with wording to check (Plan J M98, DEC-1308).

    Reasons in the treat list use `configs/decide/reasons.yaml`; a column it does not cover keeps the
    model's own text. The wording is a **check** suggestion (`suggest_reason_phrases`), recorded as an
    assumption because no run setting can hold it: accepting a box would change nothing, and the helper
    does not pretend otherwise. The key, the outcome, suspected leaks and the columns the helper hides
    are not model inputs, so they are left out.
    """
    config = builder.ctx.config
    skipped = {
        key,
        target,
        config.governance.consent_column,
        config.split.time_column,
        *prepared.profile.time_column_candidates,
        config.actions.suppression.opt_out_column,
        config.actions.suppression.recently_contacted_column,
        *exclude,
        *(
            p.step.column
            for p in builder.proposals
            if p.step is not None and p.step.kind is RecipeStepKind.DROP_COLUMN
        ),
    }
    columns = [str(c) for c in prepared.frame.columns if c not in skipped]
    suggestions = suggest_reason_phrases(columns, config_root=prepared.config_root)
    if not suggestions:
        return
    shown = ", ".join(quoted(display_name(s.feature)) for s in suggestions[:_REASON_WORDING_SHOWN])
    more = len(suggestions) - _REASON_WORDING_SHOWN
    names = f"{shown} and {more} more" if more > 0 else shown
    first = suggestions[0]
    builder.assumptions.append(
        f"Reasons for {names} have no plain wording yet, so the treat list shows the model's own text. "
        f"To check, for {quoted(display_name(first.feature))}: {quoted(_plain_phrase(first.up_phrase))} and "
        f"{quoted(_plain_phrase(first.down_phrase))}. Ask your administrator to add wording you agree with "
        "to the reasons wording."
    )


# ---------------------------------------------------------------------------
# What the user has decided, as a run request and as the pre-Approve summary
# ---------------------------------------------------------------------------
def run_overrides(proposals: Iterable[Proposal]) -> dict[str, Any]:
    """Accepted settings and acknowledgements as `POST /runs` overrides (roles are separate fields)."""
    overrides: dict[str, Any] = {}
    acknowledged: list[str] = []
    for proposal in proposals:
        if proposal.state is not ProposalState.ACCEPTED:
            continue
        if proposal.kind is ProposalKind.SETTING and proposal.path:
            overrides[proposal.path] = proposal.value
        elif proposal.kind is ProposalKind.ACKNOWLEDGEMENT:
            acknowledged.append(str(proposal.value))
    if acknowledged:
        overrides["validation.acknowledged"] = sorted(set(acknowledged))
    return overrides


def summarise(
    ctx: AgentContext,
    proposals: Sequence[Proposal],
    *,
    assumptions: Sequence[str],
    engine_hidden: Sequence[str],
) -> AgentSummary:
    """The four lists read before Approve, from the proposals the user accepted (DEC-1010)."""
    accepted = [p for p in proposals if p.state is ProposalState.ACCEPTED]
    decisions = tuple(p.title for p in accepted if p.kind is not ProposalKind.ROLE)
    roles = {p.path: str(p.value) for p in accepted if p.kind is ProposalKind.ROLE}
    hidden = [
        f"{display_name(p.step.column)}: {p.reason}"
        for p in accepted
        if p.step is not None and p.step.kind is RecipeStepKind.DROP_COLUMN
    ]
    hidden += list(engine_hidden)
    actions: list[str] = []
    try:
        config = resolve_config(ctx.use_case_id, run_overrides(proposals), root=ctx.config_root).config
    except ConfigError:
        config = ctx.config
    entity = config.entity
    combine = next(
        (p.step for p in accepted if p.step is not None and p.step.kind is RecipeStepKind.COMBINE_ROWS),
        None,
    )
    if ctx.mode is RunMode.TRAIN and combine is not None:
        actions.append(
            f"Combine the {ctx.profile.row_count:,} rows into one row per {entity} by '{combine.column}', "
            f"using only rows dated on or before each {entity}'s snapshot, and train on those."
        )
    elif ctx.mode is RunMode.TRAIN:
        actions.append(f"Train on {ctx.profile.row_count:,} rows, one per {entity}.")
    if ctx.mode is RunMode.TRAIN:
        if roles.get(ROLE_TARGET):
            actions.append(f"Learn to predict '{roles[ROLE_TARGET]}'.")
        test_pct = round(config.split.test_fraction * 100)
        if config.split.type is SplitType.TIME_BASED and config.split.time_column:
            actions.append(
                f"Hold back the newest {test_pct}% of rows by '{display_name(config.split.time_column)}' to test the model."
            )
        else:
            actions.append(f"Hold back {test_pct}% of rows at random to test the model.")
        metric = config.catalog.metric_label(config.model_search.metric)
        actions.append(
            f"Try several model types for up to {config.model_search.time_limit_minutes} minutes and keep the one "
            f"with the best {metric}."
        )
        if config.governance.approval_required:
            actions.append("A new model replaces the one in use only after it is approved.")
    else:
        actions.append(f"Score {ctx.profile.row_count:,} rows with the model in use.")
    if roles.get(ROLE_PRIMARY_KEY):
        actions.append(f"Identify each row by '{roles[ROLE_PRIMARY_KEY]}'.")
    return AgentSummary(
        decisions=decisions,
        assumptions=tuple(assumptions),
        intended_actions=tuple(actions),
        hidden_columns=tuple(dict.fromkeys(hidden)),
    )
