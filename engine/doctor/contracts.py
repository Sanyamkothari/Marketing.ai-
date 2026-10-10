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
from engine.doctor.sufficiency import NextSteps

__all__ = [
    "Diagnosis",
    "DiagnosisRequest",
    "FileDiagnosis",
    "HandoffRequest",
    "ReadinessStatus",
    "UseCaseReadiness",
]

ReadinessStatus = Literal["ready", "check", "missing", "too_small"]


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
    # Plan L M158 (DEC-1580): an added, defaulted field - what to do when the data is not enough.
    next_steps: NextSteps | None = Field(
        default=None,
        description=(
            "When the use case is not ready: what is short and by how much, what can be done now and what "
            "to collect from whom (`engine.doctor.sufficiency`). Null when it is ready."
        ),
    )


class Diagnosis(StrictBase):
    """The answer of `POST /doctor/diagnosis`."""

    files: tuple[FileDiagnosis, ...]
    use_cases: tuple[UseCaseReadiness, ...] = Field(description="Ready first, then check, then the rest.")


class DiagnosisRequest(StrictBase):
    """Body of `POST /doctor/diagnosis`."""

    upload_ids: tuple[str, ...] = Field(min_length=1, max_length=20, description="The files to look at.")


class HandoffRequest(StrictBase):
    """Body of `POST /doctor/handoff`."""

    upload_id: str = Field(min_length=1, max_length=64, description="The file Data Doctor looked at.")
    use_case: str = Field(min_length=1, max_length=64, description="The use case to start with it.")
