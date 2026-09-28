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
  cases, an ID that repeats) stops the session with the check's own message and suggestion;
* **setting proposals** - `engine.agent.recommend`.

Hiding a column is a recipe `drop_column` step, so scoring drops it the same way (DEC-1006).
`summarise` turns whatever the user has decided into the four lists read before Approve.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

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
from engine.agent.recipe import RecipeError, run_recipe
from engine.agent.recommend import DataFacts, recommend_settings
from engine.agent.tools import AgentContext, call_tool
from engine.agent.untrusted import MAX_NAME_CHARS, clean_text, display_name
from engine.config import (
    ColumnType,
    ConfigError,
    RunMode,
    SplitType,
    advanced_settings_schema,
    resolve_config,
)
from engine.stages import ingest

__all__ = [
    "ROLE_PRIMARY_KEY",
    "ROLE_TARGET",
    "STOP_CODES",
    "Advice",
    "advise",
    "run_overrides",
    "summarise",
]

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

    def option_proposal(self, **values: Any) -> Proposal:
        return Proposal(proposal_id=self.proposal_id(), **values)

    def ask(
        self, text: str, options: Sequence[QuestionOption], evidence: Sequence[str], *, blocking: bool = True
    ) -> None:
        self.questions.append(
            Question(
                question_id=self.question_id(),
                text=text,
                options=tuple(options),
                blocking=blocking,
                evidence_ids=tuple(evidence),
            )
        )


def _role(builder: _Builder, path: str, column: str, evidence: str, confidence: AgentConfidence) -> Proposal:
    what = "the column that identifies each row" if path == ROLE_PRIMARY_KEY else "the outcome to predict"
    return builder.propose(
        kind=ProposalKind.ROLE,
        title=f"Use '{display_name(column)}' as {what}",
        reason=(
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


def _choose_primary_key(
    builder: _Builder, profile: ToolResult, chosen: str | None
) -> tuple[str | None, str | None]:
    """(column, stop reason). Proposes or asks; stops when no column can identify a row."""
    if chosen is not None:
        _role(builder, ROLE_PRIMARY_KEY, chosen, profile.evidence_id, AgentConfidence.SURE)
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
    if hinted:
        # An ID-named column exists but repeats or has blanks: the Run button's own check says it best.
        checks = builder.call("check_data", {"primary_key": hinted[0]})
        reason = _stop_from_checks(checks)
        if reason is not None:
            return None, reason
    entity = builder.ctx.config.entity
    return None, (
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
    builder.ask(
        f"Which column says whether {definition.lower()}?",
        [_role_option(builder, ROLE_TARGET, column, roles.evidence_id) for column in options[:_MAX_OPTIONS]],
        [roles.evidence_id, profile.evidence_id],
    )
    return None, None


def _step(kind: RecipeStepKind, column: str, **params: Any) -> RecipeStep:
    return RecipeStep(order=1, kind=kind, column=column, params=params)


def _recipe_proposal(
    builder: _Builder, step: RecipeStep, title: str, reason: str, evidence: str, confidence: AgentConfidence
) -> Proposal:
    return builder.propose(
        kind=ProposalKind.RECIPE_STEP,
        title=title,
        reason=reason,
        step=step.model_copy(update={"reason": reason}),
        evidence_ids=(evidence,),
        confidence=confidence,
    )


def _examples(issue: Mapping[str, Any]) -> str:
    shown = [f'"{value}"' for value in list(issue.get("examples", []) or [])[:3]]
    return ", ".join(shown)


def _format_proposals(builder: _Builder, issues: ToolResult, protected: set[str]) -> None:
    limit = builder.ctx.config.agent.max_conversion_failure_pct
    for issue in _rows(issues, "issues"):
        column = str(issue["column"])
        if column in protected:
            continue
        kind = str(issue["kind"])
        evidence = issues.evidence_id
        non_empty = int(issue["non_empty"])
        failed = int(issue["failed"])
        share = float(issue["convert_share"])
        params: dict[str, Any] = dict(issue.get("params", {}) or {})
        if kind == "number_as_text":
            if non_empty and failed / non_empty * 100.0 > limit:
                bad = ", ".join(f'"{v}"' for v in list(issue.get("failed_examples", []) or [])[:3])
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
            )
        elif kind == "boolean_as_text":
            _recipe_proposal(
                builder,
                _step(RecipeStepKind.MAP_BOOLEAN, column, **params),
                f"Turn the yes / no values in '{display_name(column)}' into 1 and 0",
                f"The same answer is spelled several ways ({_examples(issue)}).",
                evidence,
                AgentConfidence.SURE,
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
            )


def _ticked(proposal: Proposal, tick_uncertain: bool) -> bool:
    return proposal.confidence is AgentConfidence.SURE or tick_uncertain


def recipe_steps(proposals: Iterable[Proposal]) -> tuple[RecipeStep, ...]:
    """The recipe steps of `proposals`, in the engine's fixed order and numbered 1..n."""
    from engine.agent.recipe import STEP_PHASE

    steps = [p.step for p in proposals if p.kind is ProposalKind.RECIPE_STEP and p.step is not None]
    steps.sort(key=lambda step: STEP_PHASE[step.kind])
    return tuple(step.model_copy(update={"order": order}) for order, step in enumerate(steps, start=1))


def _prepared_context(
    builder: _Builder, steps: Sequence[RecipeStep], primary_key: str | None, target: str | None
) -> AgentContext:
    """A context over the preview of the prepared file, so checks see cleaned values."""
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
        return ctx
    profile = ingest.profile_dataset(
        run.frame,
        ctx.config,
        upload_id=ctx.upload_id,
        file_name=ctx.profile.file_name,
        file_format=ctx.profile.file_format,
        file_size_bytes=ctx.profile.file_size_bytes,
        delimiter=ctx.profile.delimiter,
        encoding=ctx.profile.encoding,
        row_count=ctx.profile.row_count,
    )
    return replace(ctx, frame=run.frame, profile=profile)


def _leak_questions(builder: _Builder, checks: ToolResult) -> None:
    for check in _rows(checks, "checks"):
        if check["code"] != "LEAKAGE_SUSPECTED" or check["severity"] != "error" or not check["column"]:
            continue
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
    builder: _Builder, prepared: AgentContext, outcome: ToolResult | None, roles: ToolResult
) -> DataFacts:
    columns = {column.name: column for column in prepared.profile.columns}
    ordered = list(prepared.profile.time_column_candidates) + [
        name
        for name, column in columns.items()
        if column.inferred_type in {ColumnType.DATE, ColumnType.DATETIME}
    ]
    time_columns: list[str] = []
    reserved = {builder.ctx.config.actions.suppression.recently_contacted_column}
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
    }
    consent = tuple(c for c in _names(roles, "consent") if c not in taken)
    evidence = tuple(r.evidence_id for r in (roles, outcome) if r is not None)
    return DataFacts(
        rows=prepared.profile.row_count,
        positive_rate=float(rate) if isinstance(rate, (int, float)) else None,
        time_columns=tuple(time_columns),
        consent_candidates=consent,
        evidence_ids=evidence,
    )


def advise(
    ctx: AgentContext, *, primary_key: str | None = None, target: str | None = None, id_prefix: str = ""
) -> Advice:
    """Look at the file and propose everything; `primary_key` / `target` are the user's answers so far.

    `id_prefix` keeps evidence, proposal and question ids unique when a session asks more than once.
    A scoring file gets no fixes of its own: it is prepared by the model's saved recipe (DEC-1006).
    """
    builder = _Builder(ctx, prefix=id_prefix)
    config = ctx.config
    profile = builder.call("get_profile")
    roles = builder.call("find_roles")
    key, stop = _choose_primary_key(builder, profile, primary_key)
    outcome_column: str | None = None
    if ctx.mode is RunMode.TRAIN and stop is None:
        outcome_column, stop = _choose_target(builder, roles, profile, target, key)
    outcome: ToolResult | None = None
    if outcome_column is not None:
        outcome = builder.call("describe_outcome", {"column": outcome_column})
        label = _value(outcome, "positive_label")
        if label is not None:
            builder.assumptions.append(
                f"'{clean_text(str(label), MAX_NAME_CHARS)}' in '{display_name(outcome_column)}' means yes."
            )
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
        _format_proposals(builder, issues, protected)
    can_check = outcome_column is not None if ctx.mode is RunMode.TRAIN else ctx.schema is not None
    if stop is None and can_check:
        ticked = [p for p in builder.proposals if _ticked(p, config.agent.tick_uncertain)]
        prepared = _prepared_context(builder, recipe_steps(ticked), key, outcome_column)
        checks = builder.call("check_data", {"primary_key": key, "target": outcome_column}, ctx=prepared)
        stop = _stop_from_checks(checks)
        _notes_from_checks(builder, checks)
        if stop is None and ctx.mode is RunMode.TRAIN:
            _leak_questions(builder, checks)
            _settings(builder, prepared, outcome, roles, key, outcome_column)
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
) -> None:
    config = prepared.config
    facts = _facts(builder, prepared, outcome, roles)
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
    if ctx.mode is RunMode.TRAIN:
        actions.append(f"Train on {ctx.profile.row_count:,} rows, one per {entity}.")
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
