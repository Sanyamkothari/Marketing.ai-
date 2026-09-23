"""The documents M48 produces: consent records and reports, retention plans, erasure outcomes.

Frozen pydantic models with `extra="forbid"` and a description per field, the way
`engine/contracts.py` writes every artefact - and kept out of that module's `ARTEFACT_REGISTRY` for
the reason `engine/generative/contracts.py` gives (DEC-210): the registry is pinned name for name by
`tests/unit/test_artefact_registry.py`, and a privacy artefact is not a Phase 1 one.
`PRIVACY_RUN_ARTEFACTS` is the parallel map; the only member is `consent_report.json`, which is
written *only* by a scoring run whose client and purpose have a consent ledger, so a run without a
ledger writes exactly the Phase 1 set (DEC-732).

**Nothing here carries a data principal's id.** A consent record, an erasure outcome and a retention
plan name a principal by `principal_hash` or not at all, so any of them can be logged, returned by an
API or written to the audit trail without becoming a copy of the personal data it is about.
`PrincipalFindings` lists *where* an id was found - storage keys, counts - and never the id.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

__all__ = [
    "CONSENT_REPORT_FILENAME",
    "PRIVACY_RUN_ARTEFACTS",
    "AccessExportManifest",
    "ConsentImportError",
    "ConsentImportReport",
    "ConsentRecord",
    "ConsentReport",
    "ConsentStatus",
    "ErasureOutcome",
    "ErasureRequestRecord",
    "PrincipalFindings",
    "PrincipalLocation",
    "RetentionAction",
    "RetentionCategory",
    "RetentionItem",
    "RetentionPlan",
    "RetentionResult",
    "RetentionSkip",
    "RetrainFlag",
    "StoreCount",
]

CONSENT_REPORT_FILENAME: Final[str] = "consent_report.json"


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
class ConsentStatus(StrEnum):
    """What a consent record says."""

    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class ConsentRecord(_Doc):
    """One row of the consent ledger, as the API and the access export show it."""

    seq: int = Field(description="Ledger sequence number; breaks ties between records at the same instant.")
    client_id: str = Field(description="Client whose ledger this is.")
    principal_hash: str = Field(description="Salted hash of the data principal's id; never the id.")
    purpose: str = Field(description="Purpose id from configs/privacy.yaml.")
    status: ConsentStatus = Field(description="`granted` or `withdrawn`.")
    source: str = Field(description="Where the record came from, e.g. `csv_import`, `web_form`.")
    recorded_at: AwareDatetime = Field(description="When the principal gave or withdrew consent, UTC.")
    expires_at: AwareDatetime | None = Field(default=None, description="When a grant lapses; null = never.")
    created_at: AwareDatetime = Field(description="When the record entered the ledger, UTC.")


class ConsentImportError(_Doc):
    """One problem with one row of a consent CSV. Names the row and column, never the value."""

    row: int = Field(description="Row of the file, counting the header as row 1.")
    column: str | None = Field(default=None, description="Column at fault, when it is one column.")
    code: str = Field(description="Machine-readable code, e.g. CONSENT_STATUS_INVALID.")
    message: str = Field(description="Business-language explanation; carries no cell value.")


class ConsentImportReport(_Doc):
    """What a bulk consent import did."""

    client_id: str = Field(description="Client the records were imported into.")
    rows_read: int = Field(description="Data rows in the file, header excluded.")
    rows_imported: int = Field(description="Rows written to the ledger; 0 when the import was refused.")
    errors: tuple[ConsentImportError, ...] = Field(description="Every problem found, in row order.")
    ignored_columns: tuple[str, ...] = Field(
        default=(), description="Header columns the importer does not read."
    )
    imported: bool = Field(description="True when the ledger was written.")


class ConsentReport(_Doc):
    """`consent_report.json` - how the consent ledger gated one scoring run (DEC-732)."""

    run_id: str = Field(description="Scoring run the report belongs to.")
    use_case_id: str = Field(description="Use case that was scored.")
    client_id: str = Field(description="Client whose ledger was consulted.")
    purpose: str = Field(description="Purpose the use case processes data for.")
    ledger_as_of: AwareDatetime = Field(description="Instant the ledger was read as of.")
    consent_column: str = Field(description="Column the ledger verdict was written to before actions ran.")
    replaced_file_column: bool = Field(
        description="True when the use case's own consent column existed and the ledger replaced its values."
    )
    principals_checked: int = Field(description="Scored rows whose principal was looked up.")
    principals_with_valid_consent: int = Field(description="Rows whose latest record is an unexpired grant.")
    excluded_no_consent: int = Field(description="Rows with no record at all for this purpose.")
    excluded_withdrawn: int = Field(description="Rows whose latest record is a withdrawal.")
    excluded_expired: int = Field(description="Rows whose latest record is a grant that has expired.")
    excluded_file_opt_out: int = Field(
        default=0,
        description=(
            "Rows the ledger consents but the file's own consent column marks false; the more "
            "restrictive record wins (DEC-738)."
        ),
    )
    excluded_total: int = Field(description="Rows suppressed as `consent_false` by the gate.")
    created_at: AwareDatetime = Field(description="When the report was written, UTC.")


PRIVACY_RUN_ARTEFACTS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {CONSENT_REPORT_FILENAME: ConsentReport}
)
"""Privacy artefacts a run directory may hold; disjoint from `ARTEFACT_REGISTRY` (DEC-732)."""


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
class RetentionCategory(StrEnum):
    """What kind of thing a retention item is."""

    UPLOAD = "upload"
    SOURCE = "source"
    DATASET = "dataset"
    RUN_ROW_LEVEL = "run_row_level"
    RUN_SAMPLES = "run_samples"


class RetentionAction(StrEnum):
    """What the job does to an item."""

    DELETE = "delete"
    STRIP_SAMPLES = "strip_samples"


class RetentionItem(_Doc):
    """One storage key the retention job will delete, or whose samples it will empty."""

    key: str = Field(description="Storage key.")
    category: RetentionCategory = Field(description="Upload, source, dataset or run artefact.")
    action: RetentionAction = Field(description="`delete`, or `strip_samples` for a kept aggregate report.")
    owner_id: str = Field(description="Upload, source, dataset or run id the key belongs to.")
    use_case_id: str | None = Field(default=None, description="Use case whose retention setting applied.")
    client_id: str | None = Field(default=None, description="Client, when the owner records one.")
    created_at: AwareDatetime = Field(description="The owner's creation time, from its own record.")
    retention_days: int = Field(description="The `governance.retention_days` that applied.")
    fields: tuple[str, ...] = Field(
        default=(), description="Dotted field paths emptied; `strip_samples` only."
    )


class RetentionSkip(_Doc):
    """Something the planner looked at and deliberately did not schedule."""

    owner_id: str = Field(description="Upload, source, dataset or run id.")
    category: RetentionCategory = Field(description="What kind of owner it is.")
    reason_code: str = Field(description="`UNDATED` or `IN_USE`.")


class RetentionPlan(_Doc):
    """Everything one retention pass would do. A dry run is this document; `apply` executes it exactly."""

    plan_id: str = Field(description="Id of this plan; the audit event is recorded under it.")
    planned_at: AwareDatetime = Field(description="The `now` the plan was computed for.")
    items: tuple[RetentionItem, ...] = Field(description="Every key to act on, sorted by key.")
    skipped: tuple[RetentionSkip, ...] = Field(default=(), description="Owners not scheduled, and why.")

    def planned_keys(self, action: RetentionAction | None = None) -> tuple[str, ...]:
        """The planned keys, optionally only those of one action."""
        return tuple(item.key for item in self.items if action is None or item.action is action)

    def counts(self) -> dict[str, int]:
        """Items per category, for a summary line and the audit event."""
        totals: dict[str, int] = {}
        for item in self.items:
            totals[item.category.value] = totals.get(item.category.value, 0) + 1
        return totals


class RetentionResult(_Doc):
    """What `apply_retention` did with a plan."""

    plan_id: str = Field(description="The plan that was applied.")
    applied_at: AwareDatetime = Field(description="When it was applied, UTC.")
    deleted: tuple[str, ...] = Field(description="Keys deleted.")
    stripped: tuple[str, ...] = Field(description="Keys whose sample fields were emptied.")
    already_gone: tuple[str, ...] = Field(description="Planned keys that no longer existed.")
    registry_rows_deleted: int = Field(
        default=0, description="Onboarding source rows removed from the client store."
    )


# ---------------------------------------------------------------------------
# Erasure and access
# ---------------------------------------------------------------------------
class PrincipalLocation(_Doc):
    """One file that holds a principal's data, and how much of it."""

    key: str = Field(description="Storage key.")
    store: str = Field(description="Which store the key belongs to (`engine.privacy.layout.Store`).")
    file_kind: str = Field(description="`csv`, `parquet`, `json`, `text` or `binary`.")
    key_columns: tuple[str, ...] | None = Field(
        default=None, description="Primary-key columns matched on; null when the file records none."
    )
    rows: int = Field(default=0, description="Rows that belong to the principal.")
    cells: int = Field(default=0, description="Other cells or JSON values equal to the id.")
    occurrences: int = Field(default=0, description="Occurrences of the id inside longer text.")
    rewritable: bool = Field(
        default=True, description="False for a binary file (a model) that cannot be rewritten."
    )
    needs_review: bool = Field(
        default=False,
        description=(
            "True when the id is numeric and the file records no key column, so whether a cell equal "
            "to it is this person or a count cannot be decided; the file is reported, not rewritten "
            "(DEC-737)."
        ),
    )


class PrincipalFindings(_Doc):
    """Where a principal's data is. Keys and counts only; the id itself is never in here."""

    searched_keys: int = Field(description="Storage keys examined.")
    locations: tuple[PrincipalLocation, ...] = Field(description="Every file holding the principal, by key.")
    models: tuple[str, ...] = Field(description="Model versions whose training data included the principal.")
    training_runs: tuple[str, ...] = Field(description="Training runs whose inputs included the principal.")
    models_exposure_unknown: tuple[str, ...] = Field(
        default=(),
        description=(
            "Model versions whose training inputs retention has already deleted, so whether they "
            "held the principal can no longer be checked (DEC-709)."
        ),
    )

    def stores(self) -> dict[str, int]:
        """Files found per store."""
        totals: dict[str, int] = {}
        for location in self.locations:
            totals[location.store] = totals.get(location.store, 0) + 1
        return totals


class StoreCount(_Doc):
    """What erasure did in one store."""

    files: int = Field(default=0, description="Files rewritten or deleted.")
    rows: int = Field(default=0, description="Rows deleted or tombstoned.")
    cells: int = Field(default=0, description="Cells or values masked, plus text occurrences replaced.")


class ErasureOutcome(_Doc):
    """The result of one erasure request."""

    request_id: str = Field(description="Id of the request; the audit event's object id.")
    status: str = Field(
        description=(
            "`completed`, `completed_with_exceptions` (binary files or files needing review remain, "
            "or a model's training data could no longer be checked) or `failed`."
        )
    )
    principal_hash: str = Field(description="Salted hash of the principal's id.")
    client_id: str | None = Field(default=None, description="Client the request was made for.")
    mode: str = Field(description="`delete` or `tombstone`.")
    store_counts: dict[str, StoreCount] = Field(description="Store -> what was done there.")
    rows_deleted: int = Field(description="Rows removed.")
    rows_tombstoned: int = Field(description="Rows kept with the identifier replaced.")
    cells_masked: int = Field(description="Other cells, values and text occurrences replaced.")
    files_rewritten: int = Field(description="Files rewritten in their own format.")
    files_deleted: int = Field(description="Files deleted outright (LLM cache entries).")
    unrewritable_keys: tuple[str, ...] = Field(
        description="Binary files still holding the id; see models_flagged."
    )
    models_flagged: tuple[str, ...] = Field(
        description="Model versions flagged for retraining at the next cycle."
    )
    models_exposure_unknown: tuple[str, ...] = Field(
        default=(),
        description=(
            "Of `models_flagged`, the versions flagged because their training inputs are gone and "
            "could not be checked, not because the principal was found in them (DEC-709)."
        ),
    )
    consent_records_deleted: int = Field(
        default=0, description="Hashed ledger rows removed (policy `delete`)."
    )
    failed_stores: tuple[str, ...] = Field(
        default=(),
        description="Stores that still failed after every retry (Plan D, DEC-863); the request is `failed`.",
    )
    requested_by: str = Field(description="`Principal.user_id` of whoever asked.")
    requested_at: AwareDatetime = Field(description="When the request was received, UTC.")
    completed_at: AwareDatetime | None = Field(default=None, description="When it finished, UTC.")


class StoreProgress(_Doc):
    """How far a background erasure has got in one store (Plan D M54, DEC-863)."""

    store: str = Field(description="The store, e.g. `uploads`, `runs`, or `consent_ledger`.")
    status: Literal["pending", "running", "done", "failed"] = Field(description="Where this store stands.")
    files_total: int = Field(default=0, description="Files in this store that held the principal.")
    files_done: int = Field(default=0, description="Of those, files rewritten or deleted so far.")
    attempts: int = Field(default=0, description="Attempts made on this store, retries included.")
    error_code: str | None = Field(default=None, description="Why the store failed, after its last attempt.")
    updated_at: AwareDatetime | None = Field(default=None, description="When this row last changed.")


class ErasureRequestRecord(_Doc):
    """One row of the `erasure_request` register, as the API and the access export show it."""

    request_id: str
    client_id: str | None = None
    principal_hash: str
    status: str
    mode: str
    store_counts: dict[str, StoreCount] = Field(default_factory=dict)
    models_flagged: tuple[str, ...] = ()
    rows_deleted: int = 0
    rows_tombstoned: int = 0
    cells_masked: int = 0
    files_rewritten: int = 0
    files_deleted: int = 0
    requested_by: str
    requested_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    error_code: str | None = None
    # Plan D M54 (DEC-863): an added, defaulted field - a background erasure's progress per store.
    progress: tuple[StoreProgress, ...] = Field(default=(), description="Per-store progress, in store order.")


class RetrainFlag(_Doc):
    """A model version flagged because its training data included an erased principal."""

    flag_id: int = Field(description="Row id.")
    model_id: str = Field(description="Model version to retrain.")
    request_id: str = Field(description="Erasure request that raised the flag.")
    reason: str = Field(description="Code for why, e.g. `training_input:upload`.")
    created_at: AwareDatetime = Field(description="When the flag was raised.")
    cleared_at: AwareDatetime | None = Field(
        default=None, description="When retraining cleared it; null = open."
    )


class AccessExportManifest(_Doc):
    """`manifest.json` inside an access-request export."""

    request_id: str = Field(description="Id of this access request.")
    principal_hash: str = Field(description="Salted hash of the principal's id.")
    client_id: str | None = Field(default=None, description="Client the export was made for.")
    generated_at: AwareDatetime = Field(description="When the export was produced, UTC.")
    files: tuple[str, ...] = Field(description="Every file inside the archive, in order.")
    locations: tuple[PrincipalLocation, ...] = Field(description="Every store location the data came from.")
    models: tuple[str, ...] = Field(description="Model versions trained on data including the principal.")
    consent_records: int = Field(description="Consent-ledger rows included.")
    erasure_requests: int = Field(description="Earlier erasure requests included.")
    not_extractable: tuple[str, ...] = Field(
        default=(), description="Binary files (models) that hold the id and cannot be rendered as records."
    )
