"""Data Doctor's diagnosis (Plan L M151, DEC-1511): what each file is, and what the files can power.

Two halves, each built from code that already decides the same question elsewhere, so the place can
never disagree with the screen it hands off to:

* **What a file is** - the onboarding role detector (`engine.onboarding.sources.profile_source` and
  `detect_roles`) names it (a customer list, a log of bills, ...), with its reasons; the profile gives
  the ID column and rows per ID; `engine.agent.formats.find_format_issues` counts the columns Guided
  setup would offer to fix.
* **What the files can power** - for each trainable use case Home shows, the files whose columns name
  that use case's outcome (exactly, ignoring case, or by one of its `agent.column_hints.target_synonyms`)
  are run through Guided setup's own advisor (`engine.agent.advisor.advise`, rules only, no AI). Its
  verdict is the readiness: `ready` when it has nothing to ask, `check` when it will ask or suggest,
  `missing` / `too_small` when it stops, with its own reason. A use case no file names the outcome of is
  `missing`, and says what to collect from the use case's `target.definition` and `target.label_source`.

Advising is the slow part (it runs the Run button's checks), so only files that name the outcome are
advised, and only on their first `ADVISE_ROWS` rows - enough for every check's threshold, few enough to
answer in seconds. The row counts reported are always the whole file's.

DEC-1596: a `ready` or `check` verdict also carries its file's first look (`engine.doctor.first_look`):
the rate of "yes" and the rate by the most informative columns, counted by the Ask-your-data `rate_by`
tool on the same rows, with the advisor's leak findings left out. Only the chosen file's is counted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from engine.config import RunMode
from engine.doctor.contracts import FileDiagnosis, ReadinessReason, ReadinessStatus, UseCaseReadiness
from engine.doctor.first_look import FirstLook, advised_leaks, outcome_look
from engine.doctor.sufficiency import enough_data, with_alternatives
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import RoleCatalogue, UseCaseConfig
    from engine.contracts import DatasetProfile

__all__ = ["ADVISE_ROWS", "LoadedFile", "Offer", "diagnose_file", "outcome_columns", "readiness"]

_LOG = get_logger(__name__)

ADVISE_ROWS: Final[int] = 200_000
"""Rows of a file the advisor reads for a readiness verdict."""

KIND_MIN_CONFIDENCE: Final[float] = 0.1
"""Above onboarding's fallback (0.05, "no signal at all"), below its weakest real signal (0.1, half a date)."""

_STATUS_ORDER: Final[dict[str, int]] = {"ready": 0, "check": 1, "too_small": 2, "missing": 3}

_TOO_SMALL_WORDS: Final[re.Pattern[str]] = re.compile(
    r"too few|not enough|at least \d|fewer than", re.IGNORECASE
)


@dataclass(frozen=True)
class LoadedFile:
    """One upload as the diagnosis reads it: its rows (capped), its profile and what intake said."""

    upload_id: str
    file_name: str
    frame: pd.DataFrame
    profile: DatasetProfile
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Offer:
    """A use case Home offers, with the stage it is listed under."""

    config: UseCaseConfig
    stage: str


def diagnose_file(loaded: LoadedFile, config: UseCaseConfig, roles: RoleCatalogue) -> FileDiagnosis:
    """What one file is and how healthy it looks; `config` is read for its catalogue only."""
    from engine.agent.formats import find_format_issues
    from engine.onboarding.sources import profile_source

    profile = loaded.profile
    source = profile_source(
        loaded.frame,
        config,
        source_id=loaded.upload_id,
        client_id="data-doctor",
        file_name=loaded.file_name,
        row_count=profile.row_count,
        roles=roles,
    )
    best = source.role_candidates[0] if source.role_candidates else None
    # Any real signal names the file; only the detector's own fallback (a guess with no signal) does not.
    kind = best.role if best is not None and best.confidence >= KIND_MIN_CONFIDENCE else None
    spec = roles.roles.get(kind) if kind is not None else None
    key = source.key_candidates[0] if source.key_candidates else None
    personal = [column.name for column in profile.columns if column.pii_kinds]
    text_columns = [
        str(name)
        for name in loaded.frame.columns
        if str(name) not in personal and loaded.frame[name].dtype == object
    ]
    try:
        issues = find_format_issues(loaded.frame, columns=text_columns)
        fixable = len({issue.column for issue in issues})
    except Exception:  # the count is a courtesy; a column the detector cannot read is not a diagnosis
        _LOG.info("doctor.format_issues_skipped upload=%s", loaded.upload_id)
        fixable = 0
    return FileDiagnosis(
        upload_id=loaded.upload_id,
        file_name=loaded.file_name,
        rows=profile.row_count,
        columns=profile.column_count,
        kind=kind,
        kind_label=_kind_label(spec.description if spec is not None else ""),
        kind_reason="; ".join(best.reasons) if best is not None and kind is not None else "",
        id_column=key.column if key is not None else None,
        rows_per_id=round(float(key.rows_per_key), 2) if key is not None else None,
        date_columns=tuple(candidate.column for candidate in source.time_candidates),
        personal_columns=len(personal),
        fixable_columns=fixable,
        notes=loaded.notes,
    )


def _kind_label(description: str) -> str:
    text = description.strip().rstrip(".")
    return text[:1].lower() + text[1:] if text else "a table Data Doctor could not place"


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(name).casefold()).strip("_")


_LEVEL_ORDER: Final[dict[str, int]] = {"target": 0, "synonym": 1, "guess": 2}


@dataclass(frozen=True)
class OutcomeColumn:
    """A column that may be a use case's 'yes': how its name matched, and whether it holds two values."""

    column: str
    level: str  # target (the use case's own column), synonym (a recorded one) or guess (a word in the name)
    two_valued: bool


def outcome_columns(frame: pd.DataFrame, config: UseCaseConfig) -> list[OutcomeColumn]:
    """The one way to find a use case's outcome: its target name, then its synonyms, then a word of the
    name; each must hold two values (a guess must; a named column that does not is kept only if nothing
    two-valued is). Only the best name level is returned, so `purchased` never competes with a guess."""
    from engine.agent.tools import _name_matches

    wanted = config.target.column
    if wanted is None:
        return []
    synonyms = config.agent.column_hints.target_synonyms
    found: list[OutcomeColumn] = []
    for column in frame.columns:
        name = str(column)
        if _norm(name) == _norm(wanted):
            level = "target"
        elif _norm(name) in {_norm(word) for word in synonyms}:
            level = "synonym"
        elif _name_matches(name, synonyms):
            level = "guess"
        else:
            continue
        found.append(OutcomeColumn(name, level, int(frame[column].nunique(dropna=True)) == 2))
    pool = [c for c in found if c.two_valued] or [c for c in found if c.level != "guess"]
    best = min((_LEVEL_ORDER[c.level] for c in pool), default=0)
    return [c for c in pool if _LEVEL_ORDER[c.level] == best]


def _outcome_named(frame: pd.DataFrame, config: UseCaseConfig) -> str | None:
    """The first column that may name this use case's outcome, or None."""
    found = outcome_columns(frame, config)
    return found[0].column if found else None


_YES_WORDS: Final[frozenset[str]] = frozenset({"1", "1.0", "true", "yes", "y", "t"})


def _yes_count(frame: pd.DataFrame, column: str, config: UseCaseConfig, file_rows: int) -> int | None:
    """How many of the file's rows are a 'yes' in `column`, scaled up when only the first rows were read."""
    series = frame[column].dropna()
    if series.empty:
        return None
    label = config.target.positive_label
    yes = _YES_WORDS | ({str(label).casefold()} if label is not None else set())
    hits = int(series.map(lambda value: str(value).casefold() in yes).sum())
    return round(hits / len(series) * max(file_rows, len(frame)))


def _plural(entity: str) -> str:
    return entity if entity.endswith("s") else f"{entity}s"


def readiness(
    files: Sequence[LoadedFile], offers: Sequence[Offer], root: Path
) -> tuple[UseCaseReadiness, ...]:
    """Each offered use case's verdict over all files, best first: ready before check before not enough,
    a question after a plain check, then the most 'yes' examples."""
    verdicts = [_one_use_case(files, offer, root) for offer in offers]
    order = {offer.config.id: index for index, offer in enumerate(offers)}
    ranked = tuple(
        sorted(
            verdicts,
            key=lambda v: (_rank_of(v), -(v.yes_count or 0), order[v.use_case_id]),
        )
    )
    return with_alternatives(ranked)  # Plan L M158 (DEC-1582): the use cases the same files do power


_RANK: Final[dict[str, int]] = {"ready": 0, "check": 1, "too_small": 3, "missing": 4}


def _rank_of(verdict: UseCaseReadiness) -> int:
    """A question about the outcome column ranks between a plain check and not enough data."""
    return 2 if verdict.reason_code in {"GUESSED_COLUMN", "TWO_COLUMNS"} else _RANK[verdict.status]


def _one_use_case(files: Sequence[LoadedFile], offer: Offer, root: Path) -> UseCaseReadiness:
    config = offer.config
    found = [(loaded, c) for loaded in files for c in outcome_columns(loaded.frame, config)]
    if not found:
        return UseCaseReadiness(
            use_case_id=config.id,
            name=config.name,
            stage=offer.stage,
            status="missing",
            upload_id=None,
            summary=f"None of your files has the outcome {config.name} learns from.",
            needs=_needs(config),
            reason_code="NO_OUTCOME",
            why=f"None of your files has the outcome {config.name} learns from.",
            first_fix=_needs(config)[0],
        )
    best = min(_LEVEL_ORDER[c.level] for _, c in found)
    found = [(loaded, c) for loaded, c in found if _LEVEL_ORDER[c.level] == best]
    # A name alone is a guess, and two different columns are a choice: both are asked, never assumed.
    if best == _LEVEL_ORDER["guess"] or len({_norm(c.column) for _, c in found}) > 1:
        return _asked(found, offer)
    named = [(loaded, c.column) for loaded, c in found]
    advised = [_advised(loaded, column, config, root) for loaded, column in named]
    advised.sort(key=lambda pair: (_STATUS_ORDER[pair[0].status], pair[0].questions, pair[0].suggestions))
    verdict, look = advised[0]
    # DEC-1596: the first look is counted for the chosen file only, so the diagnosis stays bounded.
    first = look() if look is not None else None
    chosen = next(loaded for loaded, _ in named if loaded.upload_id == verdict.upload_id)
    return verdict.model_copy(
        update={"stage": offer.stage, "first_look": first, **_facts(verdict, chosen, config)}
    )


def _facts(verdict: UseCaseReadiness, loaded: LoadedFile, config: UseCaseConfig) -> dict[str, object]:
    """The customer and 'yes' counts, the one-line why and the first fix, for a verdict on one file."""
    column = verdict.outcome_column or ""
    rows = loaded.profile.row_count
    yes = _yes_count(loaded.frame, column, config, rows) if column in loaded.frame.columns else None
    entity = _plural(config.entity or "customer")
    why = f"{loaded.file_name} has {rows:,} {entity}"
    why += f"; {yes:,} of them are a yes in {column}." if yes is not None else f" and a column {column}."
    fix: str | None = None
    steps = verdict.next_steps
    if verdict.status == "check":
        parts = []
        if verdict.questions:
            parts.append(f"answer {verdict.questions} question{'s' if verdict.questions != 1 else ''}")
        if verdict.suggestions:
            parts.append(f"review {verdict.suggestions} suggestion{'s' if verdict.suggestions != 1 else ''}")
        todo = " and ".join(parts) or "check the rest"
        fix = f"Open Guided setup and {todo}."
    elif steps is not None:
        fix = (
            steps.shortfalls[0].sentence
            if steps.shortfalls
            else (steps.collect[0] if steps.collect else None)
        )
    code: dict[str, object] = {}
    if verdict.status == "too_small":
        code["reason_code"] = "NOT_ENOUGH"
    elif verdict.status == "missing":
        code["reason_code"] = "STOPPED"
    return {"customers": rows, "yes_count": yes, "why": why, "first_fix": fix, **code}


def _asked(found: list[tuple[LoadedFile, OutcomeColumn]], offer: Offer) -> UseCaseReadiness:
    """No assumption: one guessed column, or several candidates, become a question for the person."""
    config = offer.config
    columns = tuple(dict.fromkeys(c.column for _, c in found))
    loaded = found[0][0]
    rows = loaded.profile.row_count
    meaning = config.target.definition.strip().rstrip(".").lower() or f"the {config.name} outcome"
    if len(columns) == 1:
        text = f"{loaded.file_name} has a column {columns[0]}. Is it whether {meaning}?"
        code: ReadinessReason = "GUESSED_COLUMN"
    else:
        shown = ", ".join(columns[:5])
        text = f"{loaded.file_name} has several columns that might be it ({shown}). Which one says whether {meaning}?"
        code = "TWO_COLUMNS"
    counts = [
        n
        for file, c in found
        if (n := _yes_count(file.frame, c.column, config, file.profile.row_count)) is not None
    ]
    return UseCaseReadiness(
        use_case_id=config.id,
        name=config.name,
        stage=offer.stage,
        status="check",
        upload_id=loaded.upload_id,
        summary=text,
        questions=1,
        reason_code=code,
        customers=rows,
        yes_count=max(counts) if counts else None,
        why=text,
        first_fix="Tell us which column it is.",
        candidate_columns=columns,
    )


def _needs(config: UseCaseConfig) -> tuple[str, ...]:
    entity = config.entity or "customer"
    definition = config.target.definition.strip().rstrip(".")
    what = (
        f"a column saying, for each {entity}, whether: {definition.lower()}"
        if definition
        else (f"a column named {config.target.column} with the outcome for each {entity}")
    )
    needs = [what[:1].upper() + what[1:] + "."]
    if config.target.label_source:
        needs.append(f"It usually comes from: {config.target.label_source}.")
    needs.append(
        f"One row per {entity}, with an ID column and the outcome from the past, is enough to start."
    )
    return tuple(needs)


def _advised(
    loaded: LoadedFile, column: str, config: UseCaseConfig, root: Path
) -> tuple[UseCaseReadiness, Callable[[], FirstLook | None] | None]:
    """The verdict on one file, and (for `ready` / `check`) how to count its first look (DEC-1596)."""
    from engine.agent.advisor import advise
    from engine.agent.contracts import AgentConfidence, ProposalState, SessionStatus
    from engine.agent.tools import AgentContext
    from engine.stages.ingest import profile_dataset

    frame = loaded.frame.head(ADVISE_ROWS)
    profile = profile_dataset(
        frame,
        config,
        upload_id=loaded.upload_id,
        file_name=loaded.file_name,
        file_format=loaded.profile.file_format,
        file_size_bytes=loaded.profile.file_size_bytes,
        delimiter=loaded.profile.delimiter,
        encoding=loaded.profile.encoding,
        row_count=len(frame),
    )
    ctx = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=root,
        upload_id=loaded.upload_id,
        mode=RunMode.TRAIN,
        profile=profile,
        frame=frame,
    )
    try:
        advice = advise(ctx)
    except Exception as exc:  # a file the advisor cannot read is reported, never a 500 for every use case
        _LOG.info(
            "doctor.advise_failed upload=%s use_case=%s error=%s",
            loaded.upload_id,
            config.id,
            type(exc).__name__,
        )
        return (
            UseCaseReadiness(
                use_case_id=config.id,
                name=config.name,
                stage="",
                status="check",
                upload_id=loaded.upload_id,
                summary=f"{loaded.file_name} has {column}; open Guided setup to check the rest.",
                outcome_column=column,
            ),
            None,
        )
    # Questions Approve waits on, and suggestions Guided setup marks "Please check" (never ticked for the
    # person); its `sure` suggestions are one "Accept recommended" away and do not count against ready.
    questions = sum(1 for question in advice.questions if question.answer is None and question.blocking)
    suggestions = sum(
        1
        for proposal in advice.proposals
        if proposal.state is ProposalState.PENDING and proposal.confidence is AgentConfidence.CHECK
    )
    if advice.status is SessionStatus.STOPPED:
        reason = advice.stop_reason or "Guided setup cannot use this file as it is."
        small = bool(_TOO_SMALL_WORDS.search(reason))
        stopped = UseCaseReadiness(
            use_case_id=config.id,
            name=config.name,
            stage="",
            status="too_small" if small else "missing",
            upload_id=loaded.upload_id,
            summary=f"{loaded.file_name}: {reason}",
            outcome_column=column,
            # Plan L M158 (DEC-1581): never just a stop - what is short, what to do now, what to collect.
            next_steps=enough_data(
                frame,
                outcome=column,
                config=config,
                root=root,
                file_name=loaded.file_name,
                file_rows=loaded.profile.row_count,
            ),
        )
        return stopped, None
    status: ReadinessStatus = "ready" if questions == 0 and suggestions == 0 else "check"
    if status == "ready":
        summary = f"Ready: {loaded.file_name} has {column} and everything else Guided setup needs."
    else:
        parts = []
        if questions:
            parts.append(f"{questions} question{'s' if questions != 1 else ''} to answer")
        if suggestions:
            parts.append(f"{suggestions} suggestion{'s' if suggestions != 1 else ''} to review")
        summary = f"Ready after {' and '.join(parts) or 'a quick review'}: {loaded.file_name} has {column}."
    verdict = UseCaseReadiness(
        use_case_id=config.id,
        name=config.name,
        stage="",
        status=status,
        upload_id=loaded.upload_id,
        summary=summary,
        outcome_column=column,
        questions=questions,
        suggestions=suggestions,
    )

    def look() -> FirstLook | None:
        try:
            return outcome_look(
                ctx,
                outcome=column,
                file_name=loaded.file_name,
                file_rows=loaded.profile.row_count,
                primary_key=advice.primary_key,
                leaks=advised_leaks(advice),
            )
        except (
            Exception
        ) as exc:  # a courtesy: a first look that cannot be counted leaves the verdict as it is
            _LOG.info("doctor.first_look_skipped upload=%s error=%s", loaded.upload_id, type(exc).__name__)
            return None

    return verdict, look
