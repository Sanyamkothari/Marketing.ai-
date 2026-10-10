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
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Response

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.uploads import (
    finish_upload,
    http_error,
    load_upload,
    load_upload_profile,
    profile_row_cap,
    use_case_config,
)
from api.schemas import ErrorResponse, UploadResponse
from engine.access.roles import Role
from engine.config import (
    DEFAULT_INDUSTRY,
    ConfigError,
    RunMode,
    UseCaseStatus,
    get_roles,
    list_industries,
    load_industry,
)
from engine.doctor.contracts import Diagnosis, DiagnosisRequest, HandoffRequest
from engine.doctor.diagnose import LoadedFile, Offer, diagnose_file, readiness
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
from engine.intake.config import load_intake_config
from engine.intake.contracts import IntakeReport
from engine.intake.run import received_format_for
from engine.stages import ingest
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
        ("POST", "/doctor/outcome"): RoutePolicy(
            role=Role.ANALYST,
            action="doctor.outcome",
            purpose="agree the outcome Data Doctor builds from an event file",
            object_type="upload",
        ),
    }
)


def offers(root_path: Path) -> tuple[Offer, ...]:
    """The trainable use cases Home lists (the default journey's available cards), in Home's order."""
    industries = list_industries(root_path)
    chosen = DEFAULT_INDUSTRY if DEFAULT_INDUSTRY in industries else (industries[0] if industries else None)
    if chosen is None:
        return ()
    found: list[Offer] = []
    seen: set[str] = set()
    for stage, ref in load_industry(chosen, root_path).all_refs():
        if ref.status is not UseCaseStatus.AVAILABLE or ref.id in seen:
            continue
        try:
            config = use_case_config(ref.id, root_path)
        except ConfigError:
            continue
        if config.trainable_in_phase_1 and config.target.column:
            seen.add(ref.id)
            found.append(Offer(config=config, stage=stage.name))
    return tuple(found)


def _load(storage: Storage, upload_id: str, cap: int) -> LoadedFile:
    record = load_upload(storage, upload_id)
    profile = load_upload_profile(storage, upload_id)
    frame = ingest.read_upload(
        storage, record.source_key, file_format=record.file_format, max_rows=None, row_cap=cap
    ).frame
    notes: tuple[str, ...] = ()
    if record.intake_key is not None and storage.exists(record.intake_key):
        notes = storage.read_model(record.intake_key, IntakeReport).notes
    return LoadedFile(
        upload_id=upload_id, file_name=record.file_name, frame=frame, profile=profile, notes=notes
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
    cap = min(profile_row_cap(offer.config) for offer in available)
    files = [_load(storage, upload_id, cap) for upload_id in dict.fromkeys(body.upload_ids)]
    roles = get_roles(root)
    diagnosed = tuple(diagnose_file(loaded, any_config, roles) for loaded in files)
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
    upload = finish_upload(
        storage,
        config,
        upload_id=upload_id,
        source_key=target,
        file_format=received,
        file_name=record.file_name,
        file_size_bytes=size,
        mode=record.mode if record.mode is RunMode.SCORE else RunMode.TRAIN,
        synthetic=record.synthetic,
        intake=load_intake_config(root),
        sheet=previous.sheet if previous is not None else None,
        intake_enabled=not (previous is not None and previous.turned_off),
    )
    response.headers["Location"] = f"/uploads/{upload_id}/profile"
    return upload


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
