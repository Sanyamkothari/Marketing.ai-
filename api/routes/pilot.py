"""Plan E: the pilot's routes (M59-M64). Every one reads artefacts; none trains, scores or checks.

* `GET /pilot/help` - the plain-language catalogue the tooltips read (every warning code, every
  advanced setting, the glossary).
* `GET /pilot/data-request` and `GET /pilot/templates/{role}` - the client-facing data request and
  its header-only templates, for the use cases asked for.
* `GET /pilot/readiness/{dataset_id}` - the data readiness report of one dataset build.
* `GET /pilot/results` - the business results report of a use case's champion (or one model).
* `GET /pilot/roi/{run_id}`, `PUT /pilot/roi/{run_id}` - the value view of a campaign and the
  client's value inputs, stored with the run.
* `GET /pilot/demo`, `GET /pilot/demo/raw/{variant}` - demo mode and the demo's raw tables.
* `POST /pilot/feedback`, `GET /pilot/feedback/export` - the feedback loop.

A report comes as `format=html` (the default: one self-contained page), `pdf` (the same page as a
file, rendered on the server) or `json` (the laid-out document). Storage is the Phase 4a interface,
so every route works the same in laptop mode and on AWS.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from api.access import PrincipalDep, set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, RegistryDep, SettingsDep, StorageDep
from api.routes.clients import get_client_store
from api.routes.uploads import http_error
from api.schemas import ErrorResponse, PilotDemoResponse, PilotFeedbackRequest, PilotFeedbackResponse
from engine.access.roles import Role
from engine.audit.events import content_hash
from engine.config import ConfigError, list_use_case_ids
from engine.pilot.demo import DEMO_RAW_PREFIX, load_demo
from engine.pilot.document import ReportDocument, render_html, render_pdf
from engine.pilot.help import HelpCatalogue, load_help
from engine.pilot.roi import RoiInputs, RoiView
from engine.utils.time import utc_now

router = APIRouter(tags=["pilot"])

_V: Final[Role] = Role.VIEWER
_AN: Final[Role] = Role.ANALYST
_AD: Final[Role] = Role.ADMIN

POLICIES: dict[tuple[str, str], RoutePolicy] = {
    ("GET", "/pilot/help"): RoutePolicy(role=_V, action="pilot.help_read", purpose="read the help texts"),
    ("GET", "/pilot/data-request"): RoutePolicy(
        role=_V, action="pilot.data_request_read", purpose="download the data request"
    ),
    ("GET", "/pilot/templates/{role}"): RoutePolicy(
        role=_V, action="pilot.template_read", purpose="download a data template"
    ),
    ("GET", "/pilot/readiness/{dataset_id}"): RoutePolicy(
        role=_V,
        action="pilot.readiness_read",
        object_type="dataset",
        object_param="dataset_id",
        purpose="see a data readiness report",
    ),
    ("GET", "/pilot/results"): RoutePolicy(
        role=_V, action="pilot.results_read", purpose="see a results report"
    ),
    ("GET", "/pilot/roi/{run_id}"): RoutePolicy(
        role=_V,
        action="pilot.roi_read",
        object_type="run",
        object_param="run_id",
        purpose="see a campaign's value",
    ),
    ("PUT", "/pilot/roi/{run_id}"): RoutePolicy(
        role=_AN,
        action="pilot.roi_inputs_save",
        object_type="run",
        object_param="run_id",
        purpose="enter a campaign's value inputs",
    ),
    ("GET", "/pilot/demo"): RoutePolicy(
        role=_V, action="pilot.demo_read", purpose="see the demo environment"
    ),
    ("GET", "/pilot/demo/raw/{variant}"): RoutePolicy(
        role=_V, action="pilot.demo_raw_download", purpose="download the demo's raw tables"
    ),
    ("POST", "/pilot/feedback"): RoutePolicy(
        role=_V, action="pilot.feedback_create", object_type="feedback", purpose="give feedback"
    ),
    ("GET", "/pilot/feedback/export"): RoutePolicy(
        role=_AD, action="pilot.feedback_export", audit_reads=True, purpose="export the pilot feedback"
    ),
}
"""This router's rows of the access table (DEC-704): reading a report is Viewer, entering value
inputs is Analyst, exporting everyone's feedback is Admin and audited."""

register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
_REPORT_RESPONSES: dict[int | str, dict[str, object]] = {
    **_ERRORS,
    200: {
        "description": "The report: an HTML page (default), a PDF file, or the laid-out document as JSON.",
        "content": {"text/html": {}, "application/pdf": {}, "application/json": {}},
    },
}

ReportFormat = Literal["html", "pdf", "json"]
FormatQuery = Annotated[ReportFormat, Query(alias="format", description="html (default), pdf or json.")]
_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]+")


def _report(document: ReportDocument, fmt: ReportFormat, stem: str) -> Response:
    if fmt == "json":
        return Response(document.model_dump_json(), media_type="application/json")
    if fmt == "pdf":
        name = _SAFE_NAME.sub("_", stem) + ".pdf"
        return Response(
            render_pdf(document),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return HTMLResponse(render_html(document))


def _use_cases(requested: list[str] | None, root: ConfigRootDep) -> tuple[str, ...] | None:
    if not requested:
        return None
    known = set(list_use_case_ids(root))
    unknown = [use_case for use_case in requested if use_case not in known]
    if unknown:
        raise http_error(404, "USE_CASE_NOT_FOUND", f"Unknown use case: {', '.join(unknown)}.", "use_case")
    return tuple(requested)


UseCasesQuery = Annotated[
    list[str] | None, Query(description="Use case id; repeat for several. Default: the pilot's two.")
]


# ---------------------------------------------------------------------------
# M59, M60, M63: help, the data request kit
# ---------------------------------------------------------------------------
@router.get(
    "/pilot/help",
    response_model=HelpCatalogue,
    responses=_ERRORS,
    summary="The plain-language help: every warning code, every setting, the glossary",
)
def read_help(root: ConfigRootDep) -> HelpCatalogue:
    return load_help(root)


@router.get(
    "/pilot/data-request",
    response_class=PlainTextResponse,
    responses={
        **_ERRORS,
        200: {
            "content": {"text/markdown": {}, "application/json": {}},
            "description": "The data request: Markdown (default), or its facts as JSON for the screens.",
        },
    },
    summary="The client-facing data request for the pilot's use cases",
)
def read_data_request(
    root: ConfigRootDep,
    use_case: UseCasesQuery = None,
    fmt: Annotated[Literal["md", "json"], Query(alias="format", description="md (default) or json.")] = "md",
) -> Response:
    from engine.pilot.data_request import build_data_request, load_wording, render_markdown

    ids = _use_cases(use_case, root)
    request = build_data_request(ids, root)
    if fmt == "json":
        return Response(request.model_dump_json(), media_type="application/json")
    body = render_markdown(request, load_wording(root))
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="DATA_REQUEST.md"'},
    )


@router.get(
    "/pilot/templates/{role}",
    response_class=PlainTextResponse,
    responses={**_ERRORS, 200: {"content": {"text/csv": {}}, "description": "Header-only CSV template."}},
    summary="The header-only CSV template of one requested table",
)
def read_role_template(role: str, root: ConfigRootDep, use_case: UseCasesQuery = None) -> Response:
    from engine.pilot.data_request import (
        build_data_request,
        load_wording,
        render_role_template,
        template_filename,
    )

    ids = _use_cases(use_case, root)
    wording = load_wording(root)
    request = build_data_request(ids, root)
    table = next((t for t in request.tables if t.role == role), None)
    if table is None:
        raise http_error(
            404, "TEMPLATE_NOT_REQUESTED", f"The data request asks for no {role!r} table.", "role"
        )
    return Response(
        render_role_template(table),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{template_filename(role, wording)}"'},
    )


# ---------------------------------------------------------------------------
# M60: data readiness
# ---------------------------------------------------------------------------
@router.get(
    "/pilot/readiness/{dataset_id}",
    responses=_REPORT_RESPONSES,
    summary="The data readiness report of one dataset build: verdict, coverage, history, problems and fixes",
)
def read_readiness(
    dataset_id: str, request: Request, storage: StorageDep, root: ConfigRootDep, fmt: FormatQuery = "html"
) -> Response:
    from engine.pilot.readiness import ReadinessNotFoundError, collect_readiness, readiness_document

    try:
        facts = collect_readiness(storage, get_client_store(request), dataset_id, root=root)
    except ReadinessNotFoundError as exc:
        raise http_error(404, "DATASET_NOT_FOUND", f"No build of dataset {dataset_id!r} was found.") from exc
    return _report(readiness_document(facts, root=root), fmt, f"readiness_{dataset_id}")


# ---------------------------------------------------------------------------
# M61: business results
# ---------------------------------------------------------------------------
@router.get(
    "/pilot/results",
    responses=_REPORT_RESPONSES,
    summary="The business results report of a use case's champion, or of one model",
)
def read_results(
    request: Request,
    storage: StorageDep,
    registry: RegistryDep,
    root: ConfigRootDep,
    use_case: Annotated[str | None, Query(description="Report on this use case's champion.")] = None,
    model_id: Annotated[str | None, Query(description="Report on this model version instead.")] = None,
    fmt: FormatQuery = "html",
) -> Response:
    from engine.pilot.results import ResultsNotFoundError, collect_results, results_document

    if (use_case is None) == (model_id is None):
        raise http_error(422, "RESULTS_REQUEST_INVALID", "Give exactly one of use_case and model_id.")
    try:
        facts = collect_results(
            storage,
            registry,
            use_case_id=use_case,
            model_id=model_id,
            client_store=get_client_store(request),
            root=root,
        )
    except ResultsNotFoundError as exc:
        raise http_error(404, "RESULTS_NOT_FOUND", f"There is no model to report on: {exc}.") from exc
    except ConfigError as exc:
        raise http_error(404, exc.code, exc.message) from exc
    return _report(results_document(facts, root=root), fmt, f"results_{facts.model.model_id}")


# ---------------------------------------------------------------------------
# M62: value and ROI
# ---------------------------------------------------------------------------
def _roi_view(request: Request, storage: StorageDep, run_id: str, root: ConfigRootDep) -> RoiView:
    from engine.pilot.roi import compute_roi
    from engine.storage import StorageError

    try:
        return compute_roi(storage, run_id, client_store=get_client_store(request), root=root)
    except StorageError as exc:
        raise http_error(404, "RUN_NOT_FOUND", f"No run {run_id!r} was found.") from exc


@router.get(
    "/pilot/roi/{run_id}",
    responses={**_REPORT_RESPONSES, 200: {**_REPORT_RESPONSES[200], "model": RoiView}},
    summary="A campaign's measured effect and its value in rupees, as a range",
)
def read_roi(
    run_id: str,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    fmt: Annotated[
        Literal["json", "html", "pdf"],
        Query(alias="format", description="json (default, the view), html or pdf."),
    ] = "json",
) -> Response:
    from engine.pilot.roi import roi_document

    view = _roi_view(request, storage, run_id, root)
    if fmt == "json":
        return Response(view.model_dump_json(), media_type="application/json")
    demo = load_demo(storage)
    campaign = next((c for c in demo.campaigns if c.score_run_id == run_id), None) if demo else None
    client_name = demo.client_name if campaign is not None and demo is not None else ""
    document = roi_document(
        view,
        client_name=client_name,
        campaign_title=campaign.title if campaign else "",
        simulated_note=campaign.simulated_effect if campaign else "",
    )
    return _report(document, fmt, f"value_{run_id}")


@router.put(
    "/pilot/roi/{run_id}",
    response_model=RoiView,
    responses=_ERRORS,
    summary="Save a campaign's value inputs (what an extra customer is worth, offer and contact costs)",
)
def save_roi(
    run_id: str,
    body: RoiInputs,
    request: Request,
    storage: StorageDep,
    root: ConfigRootDep,
    principal: PrincipalDep,
) -> RoiView:
    from engine.pilot.roi import save_roi_inputs
    from engine.runs import RUN_FILENAME
    from engine.storage import StorageError, run_key

    try:
        found = storage.exists(run_key(run_id, RUN_FILENAME))
    except StorageError:  # an id that is not a key (`..`, a backslash) names no run
        found = False
    if not found:
        raise http_error(404, "RUN_NOT_FOUND", f"No run {run_id!r} was found.")
    # Who entered the figures is who is signed in - the same person the audit row names - never a
    # name the request body supplies.
    stamped = body.model_copy(update={"entered_by": principal.username, "entered_at": utc_now()})
    saved = save_roi_inputs(storage, run_id, stamped)
    set_audit_context(request, object_id=run_id, object_type="run", after_hash=content_hash(saved))
    return _roi_view(request, storage, run_id, root)


# ---------------------------------------------------------------------------
# M63: demo mode
# ---------------------------------------------------------------------------
@router.get(
    "/pilot/demo",
    response_model=PilotDemoResponse,
    responses=_ERRORS,
    summary="Whether demo mode is on, and what the seeded demo contains",
)
def read_demo(storage: StorageDep, current: SettingsDep) -> PilotDemoResponse:
    manifest = load_demo(storage) if current.demo_mode else None
    return PilotDemoResponse(
        demo_mode=current.demo_mode,
        seeded=manifest is not None,
        manifest=manifest,
        how_to_seed="" if manifest is not None or not current.demo_mode else "make demo-seed",
    )


@router.get(
    "/pilot/demo/raw/{variant}",
    responses={**_ERRORS, 200: {"content": {"application/zip": {}}, "description": "The demo's raw tables."}},
    summary="The demo's raw tables as a zip, for trying the pre-flight check (clean or broken)",
)
def read_demo_raw(variant: str, storage: StorageDep, current: SettingsDep) -> Response:
    demo = load_demo(storage) if current.demo_mode else None
    if demo is None or variant not in demo.raw_variants:
        raise http_error(404, "DEMO_NOT_AVAILABLE", "Demo mode is off, or no demo has been seeded.")
    prefix = f"{DEMO_RAW_PREFIX}/{variant}/"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for key in sorted(storage.list_keys(prefix)):
            archive.writestr(f"demo_telecom_{variant}/{key.removeprefix(prefix)}", storage.read_bytes(key))
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="demo_telecom_{variant}.zip"'},
    )


# ---------------------------------------------------------------------------
# M64: feedback
# ---------------------------------------------------------------------------
@router.post(
    "/pilot/feedback",
    response_model=PilotFeedbackResponse,
    status_code=201,
    responses=_ERRORS,
    summary="Record feedback on a screen (stored with the platform's data; contact details masked)",
)
def create_feedback(
    body: PilotFeedbackRequest,
    request: Request,
    storage: StorageDep,
    current: SettingsDep,
    principal: PrincipalDep,
) -> PilotFeedbackResponse:
    from engine.pilot.feedback import new_feedback, save_feedback

    try:
        entry = new_feedback(
            screen=body.screen,
            category=body.category,
            text=body.text,
            actor_id=principal.user_id,
            demo=current.demo_mode,
            now=utc_now(),
        )
    except ValueError as exc:
        raise http_error(422, "FEEDBACK_INVALID", "The screen must be the page's route.", "screen") from exc
    save_feedback(storage, entry)
    set_audit_context(
        request, object_id=entry.feedback_id, object_type="feedback", after_hash=content_hash(entry)
    )
    return PilotFeedbackResponse(feedback_id=entry.feedback_id, redacted=entry.redacted)


@router.get(
    "/pilot/feedback/export",
    responses={
        **_ERRORS,
        200: {
            "content": {"text/csv": {}, "application/x-ndjson": {}},
            "description": "Every feedback entry.",
        },
    },
    summary="Export every feedback entry for the pilot team (pilot_feedback.csv or .jsonl)",
)
def export_feedback(
    storage: StorageDep,
    fmt: Annotated[
        Literal["csv", "jsonl"], Query(alias="format", description="csv (default) or jsonl.")
    ] = "csv",
) -> Response:
    from engine.pilot.feedback import export_csv, export_jsonl, list_feedback

    entries = list_feedback(storage)
    if fmt == "jsonl":
        return Response(
            export_jsonl(entries),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="pilot_feedback.jsonl"'},
        )
    return Response(
        export_csv(entries),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="pilot_feedback.csv"'},
    )
