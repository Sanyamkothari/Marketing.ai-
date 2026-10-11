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
from engine.doctor.contracts import FileDiagnosis, ReadinessStatus, UseCaseReadiness
from engine.doctor.first_look import FirstLook, advised_leaks, outcome_look
from engine.doctor.sufficiency import enough_data, with_alternatives
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import RoleCatalogue, UseCaseConfig
    from engine.contracts import DatasetProfile

__all__ = ["ADVISE_ROWS", "LoadedFile", "Offer", "diagnose_file", "readiness"]

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


def _outcome_named(frame: pd.DataFrame, config: UseCaseConfig) -> str | None:
    """The column that names this use case's outcome: exact, ignoring case, or by a synonym."""
    from engine.agent.tools import _name_matches

    names = [str(column) for column in frame.columns]
    wanted = config.target.column
    if wanted is None:
        return None
    if wanted in names:
        return wanted
    folded = [name for name in names if name.casefold() == wanted.casefold()]
    if folded:
        return folded[0]
    synonyms = [name for name in names if _name_matches(name, config.agent.column_hints.target_synonyms)]
    return synonyms[0] if len(synonyms) == 1 else None


def readiness(
    files: Sequence[LoadedFile], offers: Sequence[Offer], root: Path
) -> tuple[UseCaseReadiness, ...]:
    """Each offered use case's verdict over all files, best first."""
    verdicts = [_one_use_case(files, offer, root) for offer in offers]
    order = {offer.config.id: index for index, offer in enumerate(offers)}
    ranked = tuple(sorted(verdicts, key=lambda v: (_STATUS_ORDER[v.status], order[v.use_case_id])))
    return with_alternatives(ranked)  # Plan L M158 (DEC-1582): the use cases the same files do power


def _one_use_case(files: Sequence[LoadedFile], offer: Offer, root: Path) -> UseCaseReadiness:
    config = offer.config
    named = [(loaded, column) for loaded in files if (column := _outcome_named(loaded.frame, config))]
    if not named:
        return UseCaseReadiness(
            use_case_id=config.id,
            name=config.name,
            stage=offer.stage,
            status="missing",
            upload_id=None,
            summary=f"None of your files has the outcome {config.name} learns from.",
            needs=_needs(config),
        )
    advised = [_advised(loaded, column, config, root) for loaded, column in named]
    advised.sort(key=lambda pair: (_STATUS_ORDER[pair[0].status], pair[0].questions, pair[0].suggestions))
    verdict, look = advised[0]
    # DEC-1596: the first look is counted for the chosen file only, so the diagnosis stays bounded.
    first = look() if look is not None else None
    return verdict.model_copy(update={"stage": offer.stage, "first_look": first})


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
