"""`POST /doctor/diagnosis` and `POST /doctor/handoff`: Data Doctor, the place (Plan L M151, DEC-1512).

Home's "Bring your data" uploads each file the ordinary way (`POST /uploads`, so intake, limits,
retention and erasure are an upload's) and then asks here what the files are and what they can power.
Picking a use case asks for a hand-off: the same file, read the same way (the same sheet, intake on or
off), becomes an upload of that use case, so its Guided setup opens on it with nothing to redo. The
original upload is left as it is.

Both routes need an Analyst, as uploading does: they read the files' columns and make uploads.

Plan L M157 (DEC-1573): the diagnosis also offers, for a use case whose outcome no file names, the
outcome an event file can define, as a sentence with its choices measured on the files
(`engine.doctor.outcomes`); `POST /doctor/outcome` records the person's explicit choice of one sentence
and returns its `LabelSpec` for the onboarding builder. Analyst too: it writes the agreement.

DEC-1596: the diagnosis also says what each file already says, with no AI (`engine.doctor.first_look`):
per ready use case the rate by the most informative columns, and per file with no outcome a recency
picture. No new route: both are counted on the rows the diagnosis already read.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Request, Response

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.agent_recipes import load_recipe
from api.routes.agent_renames import write_renamed_copy
from api.routes.runs import requested_by
from api.routes.uploads import (
    finish_upload,
    http_error,
    load_upload,
    load_upload_profile,
    profile_row_cap,
    use_case_config,
)
from api.schemas import ErrorResponse, UploadRecord, UploadResponse
from engine.access.roles import Role
from engine.config import (
    DEFAULT_INDUSTRY,
    ConfigError,
    RunMode,
    UseCaseConfig,
    UseCaseStatus,
    get_roles,
    list_industries,
    list_use_case_ids,
    load_industry,
)
from engine.doctor.contracts import Diagnosis, DiagnosisRequest, HandoffRequest
from engine.doctor.diagnose import ADVISE_ROWS, LoadedFile, Offer, diagnose_file, readiness
from engine.doctor.first_look import attach_recency
from engine.doctor.joins import how_files_fit
from engine.doctor.outcomes import (
    OutcomeAgreement,
    OutcomeFile,
    OutcomeRequest,
    attach_outcome_offers,
    load_outcome_catalogue,
    next_steps,
    offer_outcome,
)
from engine.doctor.renames import ColumnRename, RenamedCopyRequest
from engine.doctor.sample_rows import doctor_rows
from engine.intake.config import load_intake_config
from engine.intake.contracts import IntakeReport
from engine.intake.run import received_format_for
from engine.storage import Storage, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

router: APIRouter = APIRouter(tags=["doctor"])

_ERRORS: Final[dict[int | str, dict[str, object]]] = {
    404: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

register(
    {
        ("POST", "/doctor/diagnosis"): RoutePolicy(
            role=Role.ANALYST, action="doctor.diagnose", purpose="ask Data Doctor about uploaded files"
        ),
        ("POST", "/doctor/handoff"): RoutePolicy(
            role=Role.ANALYST,
            action="doctor.handoff",
            purpose="start a use case from a file Data Doctor looked at",
            object_type="upload",
        ),
        # Plan L #16 (DEC-1597): a copy of the file with the columns the person ticked renamed back.
        ("POST", "/uploads/{upload_id}/renamed-copy"): RoutePolicy(
            role=Role.ANALYST,
            action="doctor.renamed_copy",
            purpose="use a renamed column under its old name",
            object_type="upload",
            object_param="upload_id",
        ),
        ("POST", "/doctor/outcome"): RoutePolicy(
            role=Role.ANALYST,
            action="doctor.outcome",
            purpose="agree the outcome Data Doctor builds from an event file",
            object_type="upload",
        ),
    }
)


SKIPPED_USE_CASES: Final[frozenset[str]] = frozenset({"telco-churn"})
"""Trainable use cases the suggestions leave out (the owners took Telco Churn off the list)."""


def offers(root_path: Path) -> tuple[Offer, ...]:
    """The trainable use cases the files are checked against: Home's, in Home's order, then every other
    shipped one that learns from a yes/no column (not Telco Churn, not the uplift-only ones)."""
    industries = list_industries(root_path)
    chosen = DEFAULT_INDUSTRY if DEFAULT_INDUSTRY in industries else (industries[0] if industries else None)
    refs = (
        [(stage.name, ref) for stage, ref in load_industry(chosen, root_path).all_refs()]
        if chosen is not None
        else []
    )
    candidates = [(stage, ref.id) for stage, ref in refs if ref.status is UseCaseStatus.AVAILABLE]
    candidates += [("", use_case_id) for use_case_id in list_use_case_ids(root_path)]
    found: list[Offer] = []
    seen: set[str] = set(SKIPPED_USE_CASES)
    for stage, use_case_id in candidates:
        if use_case_id in seen:
            continue
        try:
            config = use_case_config(use_case_id, root_path)
        except ConfigError:
            continue
        if config.trainable_in_phase_1 and config.target.column and config.uplift.treatment_column is None:
            seen.add(use_case_id)
            found.append(Offer(config=config, stage=stage or config.lifecycle_stage))
    return tuple(found)


DIAGNOSIS_ROWS: Final[int] = 4_000_000
"""Rows one diagnosis reads across all its files (M161, DEC-1507): every file is held at once, so twenty large
uploads must not cost twenty full reads. A file gets an even share, never fewer than `ADVISE_ROWS`; what is
measured on fewer rows than a file has says so (`sampled`, the outcome's note)."""


def _load(storage: Storage, upload_id: str, cap: int) -> LoadedFile:
    record = load_upload(storage, upload_id)
    profile = load_upload_profile(storage, upload_id)
    # The whole file when it fits, else a random sample of the whole file (never its first rows).
    frame, exact, file_rows = doctor_rows(
        storage,
        source_key=record.source_key,
        file_format=record.file_format,
        upload_id=upload_id,
        row_count=profile.row_count,
        columns=[str(c.name) for c in profile.columns],
        cap=cap,
    )
    notes: tuple[str, ...] = ()
    if record.intake_key is not None and storage.exists(record.intake_key):
        notes = storage.read_model(record.intake_key, IntakeReport).notes
    return LoadedFile(
        upload_id=upload_id,
        file_name=record.file_name,
        frame=frame,
        profile=profile,
        notes=notes,
        exact=exact,
        file_rows=file_rows,
    )


@router.post(
    "/doctor/diagnosis",
    response_model=Diagnosis,
    responses=_ERRORS,
    summary="What each uploaded file is, and which use cases the files can power",
)
def diagnose(body: DiagnosisRequest, storage: StorageDep, root: ConfigRootDep) -> Diagnosis:
    available = offers(root)
    if not available:
        raise http_error(404, "DOCTOR_NO_USE_CASES", "There is no use case to check the files against.")
    any_config = available[0].config
    wanted = tuple(dict.fromkeys(body.upload_ids))
    cap = min(
        min(profile_row_cap(offer.config) for offer in available),
        max(ADVISE_ROWS, DIAGNOSIS_ROWS // len(wanted)),
    )
    files = [_load(storage, upload_id, cap) for upload_id in wanted]
    roles = get_roles(root)
    diagnosed = tuple(diagnose_file(loaded, any_config, roles) for loaded in files)
    # DEC-1596: a file with no use case's outcome gets its recency picture (the outcome's is per use case).
    diagnosed = attach_recency(
        diagnosed,
        [loaded.frame for loaded in files],
        [loaded.profile for loaded in files],
        [offer.config for offer in available],
    )
    # Plan L M157 (DEC-1573): a `missing` use case gets the outcome an event file can define, if any.
    verdicts = attach_outcome_offers(
        readiness(files, available, root),
        [
            OutcomeFile(diagnosis=file, frame=loaded.frame)
            for file, loaded in zip(diagnosed, files, strict=True)
        ],
        {offer.config.id: offer.config for offer in available},
        root,
    )
    return how_files_fit(
        Diagnosis(files=diagnosed, use_cases=verdicts), files, roles
    )  # M155 (DEC-1551, DEC-1552)


@router.post(
    "/doctor/handoff",
    response_model=UploadResponse,
    status_code=201,
    responses=_ERRORS,
    summary="The file as an upload of the chosen use case, read the same way, for its Guided setup",
)
def handoff(
    body: HandoffRequest, response: Response, storage: StorageDep, root: ConfigRootDep
) -> UploadResponse:
    config = use_case_config(body.use_case, root)
    record = load_upload(storage, body.upload_id)
    if record.use_case_id == config.id:
        response.status_code = 200
        profile = load_upload_profile(storage, record.upload_id)
        return UploadResponse(
            upload_id=record.upload_id, profile=profile, intake=_intake(storage, record.intake_key)
        )
    upload = clone_upload(
        storage, root, record, config, mode=record.mode if record.mode is RunMode.SCORE else RunMode.TRAIN
    )
    response.headers["Location"] = f"/uploads/{upload.upload_id}/profile"
    return upload


def clone_upload(
    storage: Storage, root: Path, record: UploadRecord, config: UseCaseConfig, *, mode: RunMode
) -> UploadResponse:
    """A new upload of `config`'s use case with the bytes of `record`, read the same way, for `mode`.

    The hand-off uses it to move a file to another use case; the campaign run uses it to score the same
    customers a model learned from (a score run needs an upload of its own, made for scoring).
    """
    previous = _intake(storage, record.intake_key)
    # The file as it arrived; an erasure deletes a kept original, and then the table read from it is used.
    kept = record.original_key is not None and storage.exists(record.original_key)
    received = received_format_for(record.file_name) if kept else record.file_format
    source = record.original_key if kept and record.original_key else record.source_key
    upload_id = new_upload_id()
    target = upload_key(upload_id, "original.xlsx" if received == "xlsx" else f"source.{received}")
    size = storage.size_bytes(source)
    if size > config.validation.max_file_size_mb * 1024 * 1024:
        raise http_error(
            413,
            "UPLOAD_TOO_LARGE",
            f"The file is larger than the {config.validation.max_file_size_mb} MB limit.",
        )
    with storage.open_read(source) as reader, storage.open_write(target) as writer:
        while chunk := reader.read(1 << 20):
            writer.write(chunk)
    return finish_upload(
        storage,
        config,
        upload_id=upload_id,
        source_key=target,
        file_format=received,
        file_name=record.file_name,
        file_size_bytes=size,
        mode=mode,
        synthetic=record.synthetic,
        intake=load_intake_config(root),
        sheet=previous.sheet if previous is not None else None,
        intake_enabled=not (previous is not None and previous.turned_off),
    )


def _intake(storage: Storage, key: str | None) -> IntakeReport | None:
    return storage.read_model(key, IntakeReport) if key is not None and storage.exists(key) else None


def outcome_agreement_key(entity_upload_id: str, use_case_id: str) -> str:
    """Where the person's agreement is kept: beside the customer list, so erasing the upload erases it."""
    return upload_key(entity_upload_id, f"outcome_{use_case_id}.json")


@router.post(
    "/doctor/outcome",
    response_model=OutcomeAgreement,
    status_code=201,
    responses={**_ERRORS, 409: {"model": ErrorResponse}},
    summary="Agree the outcome built from an event file, as the sentence the person picked",
)
def agree_outcome(body: OutcomeRequest, storage: StorageDep, root: ConfigRootDep) -> OutcomeAgreement:
    """Record the person's explicit choice of one sentence and return the `LabelSpec` it builds.

    The offer is measured again on the two files, so what is agreed is what the files say now: a choice
    the offer does not have is `422`, and a sentence that is not, word for word, the one this choice
    reads today (the file changed, or the screen is stale) is `409` with the sentence as it is now.
    Nothing is built here; the agreement's `next_steps` are the onboarding builder's calls.
    """
    if not body.agreed:
        raise http_error(
            422,
            "OUTCOME_NOT_AGREED",
            "Nothing is built from an outcome you have not agreed to. Pick one sentence and say yes.",
            "agreed",
        )
    config = use_case_config(body.use_case, root)
    catalogue = load_outcome_catalogue(root)
    template = catalogue.templates.get(config.id)
    if template is None:
        raise http_error(
            404, "OUTCOME_NO_SENTENCE", f"Data Doctor has no outcome sentence for {config.name}.", "use_case"
        )
    if body.entity_upload_id == body.event_upload_id:
        raise http_error(
            422,
            "OUTCOME_SAME_FILE",
            "The customer list and the event file must be two different files.",
            "event_upload_id",
        )
    roles = get_roles(root)
    cap = profile_row_cap(config)
    loaded = [_load(storage, upload_id, cap) for upload_id in (body.entity_upload_id, body.event_upload_id)]
    files = [OutcomeFile(diagnosis=diagnose_file(item, config, roles), frame=item.frame) for item in loaded]
    offer = offer_outcome(
        config,
        template,
        files,
        catalogue=catalogue,
        roles=roles,
        event_upload_id=body.event_upload_id,
        date_column=body.date_column,
    )
    if offer is None or offer.entity_upload_id != body.entity_upload_id:
        raise http_error(
            409,
            "OUTCOME_NOT_OFFERED",
            f"These two files cannot define the outcome of {config.name}: it needs a customer list with an "
            f"ID column and an event file with the date column {body.date_column!r} and the same IDs.",
        )
    choice = next((item for item in offer.choices if item.days == body.days), None)
    if choice is None:
        offered = ", ".join(str(item.days) for item in offer.choices)
        raise http_error(
            422, "OUTCOME_CHOICE_UNKNOWN", f"{body.days} days is not one of the choices ({offered}).", "days"
        )
    if choice.sentence != body.sentence:
        raise http_error(
            409,
            "OUTCOME_SENTENCE_CHANGED",
            f'The sentence for this choice now reads: "{choice.sentence}" Read it again and agree to that.',
            "sentence",
        )
    if choice.measured_on is None:
        raise http_error(
            409,
            "OUTCOME_NOT_MEASURABLE",
            f"{offer.event_file_name} runs to {offer.data_end}: no month-end has {body.days} days after it "
            "that are over, so no customer can be labelled yet. Pick a shorter window or bring a longer history.",
            "days",
        )
    agreement = OutcomeAgreement(
        agreement_id=f"oa_{secrets.token_hex(6)}",
        use_case_id=config.id,
        sentence=choice.sentence,
        label_spec=choice.label_spec,
        choice=choice,
        entity_upload_id=offer.entity_upload_id,
        entity_id_column=offer.entity_id_column,
        event_upload_id=offer.event_upload_id,
        event_id_column=offer.event_id_column,
        date_column=offer.date_column,
        event_role=offer.event_role,
        data_end=offer.data_end,
        agreed_at=utc_now(),
        next_steps=(),
    )
    agreement = agreement.model_copy(update={"next_steps": next_steps(agreement)})
    storage.write_model(outcome_agreement_key(offer.entity_upload_id, config.id), agreement)
    return agreement


@router.post(
    "/uploads/{upload_id}/renamed-copy",
    response_model=UploadResponse,
    status_code=201,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="A copy of the file with the columns the person ticked given their old names; the file is kept",
)
def renamed_copy(
    upload_id: str,
    body: RenamedCopyRequest,
    request: Request,
    response: Response,
    storage: StorageDep,
    root: ConfigRootDep,
) -> UploadResponse:
    """Plan L #16 (DEC-1597): "Your file has 'bill_amount' where last month had 'amount'. Use it?" - yes.

    The file is copied, every row, with those columns renamed, into a new upload of the same use case and
    mode; the upload asked about is left as it is. Saved steps then replay on the copy unchanged, and a copy
    with a remembered shape is offered its treatment. Any column of the file may be renamed to a name the
    file does not have: the person chooses, as with two close candidates the matcher never chooses between.
    """
    record = load_upload(storage, upload_id)
    config = use_case_config(record.use_case_id, root)
    if load_recipe(storage, upload_id) is not None:
        raise http_error(
            409,
            "DOCTOR_RENAME_PREPARED",
            "This file was prepared by Guided setup. Rename columns in the file you sent instead.",
        )
    columns = list(load_upload_profile(storage, upload_id).fingerprint.columns)
    olds = [r.column for r in body.renames]
    news = [r.new_name.strip() for r in body.renames]
    unknown = [name for name in olds if name not in columns]
    if unknown:
        raise http_error(422, "DOCTOR_RENAME_UNKNOWN", f"The file has no column {unknown[0]!r}.")
    if len(set(olds)) != len(olds) or len(set(news)) != len(news) or not all(news):
        raise http_error(
            422, "DOCTOR_RENAME_INVALID", "Rename each column once, each to a different, non-empty name."
        )
    taken = [new for new, old in zip(news, olds, strict=True) if new != old and new in columns]
    if taken or any(new == old for new, old in zip(news, olds, strict=True)):
        raise http_error(
            422,
            "DOCTOR_RENAME_INVALID",
            f"The file already has a column called {taken[0]!r}." if taken else "A new name must differ.",
        )
    renames = tuple(ColumnRename(column=old, new_name=new) for old, new in zip(olds, news, strict=True))
    copied = write_renamed_copy(storage, config, record, renames, requested_by=requested_by(request))
    set_audit_context(
        request,
        object_id=copied.upload_id,
        object_type="upload",
        details={"source_upload_id": upload_id, "count": len(renames)},
    )
    response.headers["Location"] = f"/uploads/{copied.upload_id}/profile"
    return copied
