"""What Data Doctor says about a set of files (Plan L M151, DEC-1510).

`Diagnosis` answers two questions a person without a data team asks first: *what is each of my files?*
and *what can they power?* Every figure is measured on the files; every sentence is built from those
figures or is the advisor's own (`engine.agent.advisor.advise`), so the place never promises more than
Guided setup will then do.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from engine.config import StrictBase
from engine.doctor.first_look import FirstLook
from engine.doctor.outcomes import OutcomeOffer
from engine.doctor.sufficiency import NextSteps

__all__ = [
    "ColumnDrift",
    "ColumnRename",
    "Diagnosis",
    "DiagnosisRequest",
    "FileDiagnosis",
    "FileLink",
    "FileStack",
    "HandoffRequest",
    "ReadinessReason",
    "ReadinessStatus",
    "UseCaseReadiness",
]

ReadinessStatus = Literal["ready", "check", "missing", "too_small"]
ReadinessReason = Literal["NO_OUTCOME", "GUESSED_COLUMN", "TWO_COLUMNS", "NOT_ENOUGH", "STOPPED"]


class FileDiagnosis(StrictBase):
    """One file: what it looks like, what it probably is, and its health."""

    upload_id: str = Field(description="The upload the file is.")
    file_name: str = Field(description="The file's name as it arrived.")
    rows: int = Field(description="Data rows, counted.")
    columns: int = Field(description="Columns.")
    kind: str | None = Field(
        description="What the file most likely is (an onboarding role), or null when nothing fits."
    )
    kind_label: str = Field(description="`kind` in plain words: 'a customer list', 'a log of bills', ...")
    kind_reason: str = Field(description="Why: the file name and the columns that matched.")
    id_column: str | None = Field(description="The column that names each row's customer, when one is found.")
    rows_per_id: float | None = Field(
        description="Rows per value of `id_column` (1.0: one row per customer)."
    )
    date_columns: tuple[str, ...] = Field(description="Columns read as dates.")
    personal_columns: int = Field(description="Columns holding personal data, hidden from every AI.")
    fixable_columns: int = Field(description="Columns whose format Guided setup would offer to fix.")
    notes: tuple[str, ...] = Field(description="What intake did on the way in (`IntakeReport.notes`).")
    # DEC-1596: an added, defaulted field - for a file with no use case's outcome, when customers last
    # appear and how often, counted with no AI. Null otherwise.
    first_look: FirstLook | None = Field(
        default=None,
        description=(
            "For a file with no use case's outcome but an ID and a date column: recency and frequency, "
            "counted (`engine.doctor.first_look`). Null otherwise."
        ),
    )


class UseCaseReadiness(StrictBase):
    """Whether the files can power one use case, and the best file to start it from."""

    use_case_id: str
    name: str
    stage: str = Field(description="The lifecycle stage Home lists it under.")
    status: ReadinessStatus = Field(
        description=(
            "`ready`: Guided setup has nothing to ask; `check`: it will ask or suggest before training; "
            "`missing`: something it needs is not in any file; `too_small`: there is not enough of it."
        )
    )
    upload_id: str | None = Field(description="The file to start from; null when none fits.")
    summary: str = Field(description="One plain sentence: what is ready, or what is missing.")
    needs: tuple[str, ...] = Field(
        default=(), description="What to collect, in plain words, when something is missing."
    )
    outcome_column: str | None = Field(default=None, description="The column Guided setup would learn from.")
    questions: int = Field(default=0, description="Questions Guided setup will ask before training.")
    suggestions: int = Field(default=0, description="Suggestions Guided setup will show before training.")
    # Plan L M157 (DEC-1572): for a `missing` use case whose outcome an event file can define, the
    # sentence to agree, with its choices measured on the files. Null otherwise; never built from.
    outcome_offer: OutcomeOffer | None = Field(
        default=None,
        description="The outcome an event file can define, as a sentence to agree; null when none.",
    )

    # Plan L M158 (DEC-1580): an added, defaulted field - what to do when the data is not enough.
    next_steps: NextSteps | None = Field(
        default=None,
        description=(
            "When the use case is not ready: what is short and by how much, what can be done now and what "
            "to collect from whom (`engine.doctor.sufficiency`). Null when it is ready."
        ),
    )

    # Added, defaulted fields: why this use case is where it is in the list, so a person can compare.
    reason_code: ReadinessReason | None = Field(
        default=None,
        description=(
            "Why it is not plainly ready: `NO_OUTCOME` (no file names the outcome), `GUESSED_COLUMN` (one "
            "column might be it: a question), `TWO_COLUMNS` (several might be it: a question), "
            "`NOT_ENOUGH` (too little history), `STOPPED` (Guided setup cannot use the file). Null otherwise."
        ),
    )
    customers: int | None = Field(default=None, description="Rows (customers) in the file to start from.")
    yes_count: int | None = Field(default=None, description="How many of them are a 'yes', when it is known.")
    why: str = Field(default="", description="One plain line: what the file shows for this use case.")
    first_fix: str | None = Field(
        default=None, description="The first thing to do when it is not ready, in plain words."
    )
    candidate_columns: tuple[str, ...] = Field(
        default=(), description="For a question: the columns that might name the outcome."
    )

    also_fits: str | None = Field(
        default=None,
        description=(
            "The Home goal this one overlaps (same lifecycle stage), by name: this goal is still offered, and the "
            "person sees that the same files also fit that goal. Null for Home's own goals."
        ),
        exclude_if=lambda value: value is None,
    )

    # DEC-1596: an added, defaulted field - what the file already says about the outcome, with no AI.
    first_look: FirstLook | None = Field(
        default=None,
        description=(
            "For a `ready` or `check` verdict: the file's rate of 'yes' and the rate by its most informative "
            "columns, counted on the rows read (`engine.doctor.first_look`). Null otherwise, and also for the "
            "lower-ranked verdicts of a table that powers many goals, left out to stay within the answer's size "
            "budget: a null here does not mean the verdict is not ready."
        ),
    )


class FileLink(StrictBase):
    """How one event file meets the customer list (Plan L M155, DEC-1551): every share is measured."""

    upload_id: str = Field(description="The event file (a log of bills, tickets, ...).")
    file_name: str
    column: str = Field(description="The event file's column that names the customer.")
    list_upload_id: str = Field(description="The customer list it was measured against.")
    list_file_name: str
    list_column: str = Field(description="The customer list's ID column.")
    coverage: float = Field(
        description="Share of the event rows naming a customer whose customer is in the list, as written."
    )
    fix: str | None = Field(
        default=None,
        description="In plain words, what to do to the IDs so they meet; null when none is needed.",
    )
    transform: str | None = Field(
        default=None,
        description=(
            "The onboarding mapping transform that is the fix (`strip`, `lower`, `lstrip_zeros`), when one is; "
            "null for no fix or for a fix that also removes a prefix."
        ),
    )
    fixed_coverage: float | None = Field(
        default=None, description="The same share once `fix` is applied, measured; null when there is no fix."
    )
    sampled: bool = Field(
        default=False,
        description="True when a file was longer than the rows read, so the shares are of those rows.",
    )
    summary: str = Field(description="One plain sentence.")


class ColumnRename(StrictBase):
    """A column the first file of a stack has under another name in a later one."""

    before: str
    after: str


class ColumnDrift(StrictBase):
    """How one file of a stack differs from the stack's first file."""

    upload_id: str
    file_name: str
    added: tuple[str, ...] = Field(default=(), description="Columns this file has and the first does not.")
    missing: tuple[str, ...] = Field(
        default=(), description="Columns the first file has and this one does not."
    )
    renamed: tuple[ColumnRename, ...] = Field(
        default=(), description="Columns read as renamed: same place or a close name, and the same type."
    )


class FileStack(StrictBase):
    """Files that look like the same report for different periods, and can be stacked (DEC-1552)."""

    upload_ids: tuple[str, ...] = Field(
        description="In file-name order; the first is the one compared against."
    )
    file_names: tuple[str, ...]
    kind: str | None = Field(description="What the files are (the same for all of them).")
    columns: tuple[str, ...] = Field(description="Columns every file has under the same name.")
    rows: int = Field(description="Rows of all the files together, counted.")
    drift: tuple[ColumnDrift, ...] = Field(
        default=(),
        description="One entry per file whose columns differ from the first's; empty when all agree.",
    )
    summary: str = Field(description="One plain sentence.")


class Diagnosis(StrictBase):
    """The answer of `POST /doctor/diagnosis`."""

    files: tuple[FileDiagnosis, ...]
    use_cases: tuple[UseCaseReadiness, ...] = Field(description="Ready first, then check, then the rest.")
    links: tuple[FileLink, ...] = Field(
        default=(), description="How each event file's customer IDs meet the customer list's (M155)."
    )
    stacks: tuple[FileStack, ...] = Field(
        default=(), description="Files that are the same report for different periods (M155)."
    )


class DiagnosisRequest(StrictBase):
    """Body of `POST /doctor/diagnosis`."""

    upload_ids: tuple[str, ...] = Field(min_length=1, max_length=20, description="The files to look at.")


class HandoffRequest(StrictBase):
    """Body of `POST /doctor/handoff`."""

    upload_id: str = Field(min_length=1, max_length=64, description="The file Data Doctor looked at.")
    use_case: str = Field(min_length=1, max_length=64, description="The use case to start with it.")
