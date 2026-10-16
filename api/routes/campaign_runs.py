"""Start a campaign and leave: `POST /campaign-runs` learns from the customers, scores them, builds the contact
list and records the campaign on the server (`engine/campaign_run.py`), so the browser can close.

Every step uses the code its own route uses: training and scoring are `api.routes.runs.start_run`, the scoring
file is `api.routes.doctor.clone_upload`, the list is `ensure_treat_list` and the campaign is `open_campaign`.
Registering the test plan and downloading the list stay the person's "Approve and download".
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Annotated, Any, Final

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from pydantic import Field, model_validator

from api.access import set_audit_context
from api.access_policy import RoutePolicy, register
from api.deps import (
    ConfigRootDep,
    StorageDep,
    get_config_root,
    get_jobs,
    get_registry,
    get_settings,
    get_storage,
)
from api.routes.campaigns import default_outcome_window, get_campaign_store
from api.routes.doctor import clone_upload
from api.routes.runs import _refuse_uplift_training, load_run, read_artefact, requested_by, start_run
from api.routes.schedules import app_request
from api.routes.uploads import http_error, load_upload, use_case_config
from api.schemas import ErrorResponse, RunCreatedResponse, RunRequest
from engine.access.roles import Role
from engine.campaign_results import TopCustomer, top_customers
from engine.campaign_run import (
    PREFIX,
    CampaignRun,
    CampaignRunRunner,
    CampaignRunState,
    CampaignRunView,
    Speed,
    StepActions,
    StepError,
    overrides_for,
)
from engine.config import PrimaryKey, ResolvedConfig, RunMode, StrictBase, resolve_config
from engine.contracts import FeatureSchema, ModelStatus, ModelVersion
from engine.decide.treat_list import TreatListError, ensure_treat_list
from engine.doctor.case import CASE_ID_PATTERN, DEFAULT_CLIENT_ID, CaseError, CaseStore
from engine.doctor.visit import is_approved
from engine.measurement.cycle import MEASURE_NOT_OFFERED, CycleError, open_campaign
from engine.model_approval import model_not_approved
from engine.runs import RUN_CONFIG_FILENAME
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

__all__ = ["POLICIES", "install_campaign_runs", "router"]

_LOGGER = get_logger(__name__)
_SLOT: Final[str] = "campaign_run_runner"
_LOCK: Final[threading.Lock] = threading.Lock()

router: APIRouter = APIRouter(tags=["campaign-runs"])

POLICIES: Final[dict[tuple[str, str], RoutePolicy]] = {
    ("POST", "/campaign-runs"): RoutePolicy(
        role=Role.ANALYST,
        action="campaign_runs.start",
        object_type="campaign_run",
        purpose="start a campaign run",
    ),
    ("GET", "/campaign-runs"): RoutePolicy(
        role=Role.VIEWER, action="campaign_runs.list", purpose="see campaign runs"
    ),
    ("GET", "/campaign-runs/{campaign_run_id}"): RoutePolicy(
        role=Role.VIEWER,
        action="campaign_runs.read",
        object_type="campaign_run",
        object_param="campaign_run_id",
        purpose="see a campaign run",
    ),
    ("GET", "/campaign-runs/{campaign_run_id}/top-customers"): RoutePolicy(
        role=Role.ANALYST,
        action="campaign_runs.top_customers",
        object_type="campaign_run",
        object_param="campaign_run_id",
        audit_reads=True,
        purpose="see the first customers of a campaign run's contact list",
    ),
    ("POST", "/campaign-runs/{campaign_run_id}/restart"): RoutePolicy(
        role=Role.ANALYST,
        action="campaign_runs.restart",
        object_type="campaign_run",
        object_param="campaign_run_id",
        purpose="start a stopped campaign run again",
    ),
    ("POST", "/campaign-runs/{campaign_run_id}/cancel"): RoutePolicy(
        role=Role.ANALYST,
        action="campaign_runs.cancel",
        object_type="campaign_run",
        object_param="campaign_run_id",
        purpose="cancel a campaign run",
    ),
}
register(POLICIES)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


class CampaignRunRequest(StrictBase):
    """Body of `POST /campaign-runs`: the prepared file (or built dataset), the goal and how long to train."""

    use_case: str
    upload_id: str | None = None
    dataset_id: str | None = None
    client_id: str | None = None
    case_id: str | None = Field(
        default=None, pattern=CASE_ID_PATTERN, description="The Data Doctor case the prepared file came from."
    )
    primary_key: PrimaryKey | None = None
    target: str | None = None
    speed: Speed = Speed.BALANCED
    overrides: dict[str, Any] = Field(default_factory=dict)
    model_version_id: str | None = None  # score with this model instead of learning a new one
    confirm_cost: bool = False

    @model_validator(mode="after")
    def _a_run_could_start(self) -> CampaignRunRequest:
        RunRequest(
            use_case=self.use_case,
            upload_id=self.upload_id,
            dataset_id=self.dataset_id,
            client_id=self.client_id,
            primary_key=self.primary_key,
        )  # the same one-source rules as POST /runs
        return self


class RestartRequest(StrictBase):
    """Body of `POST /campaign-runs/{id}/restart`: only a cost the person now agrees to."""

    confirm_cost: bool = False


class CampaignRunListResponse(StrictBase):
    campaign_runs: tuple[CampaignRunView, ...]


class TopCustomersResponse(StrictBase):
    """The first customers of the contact list, best first; or, with none, why they cannot be shown."""

    campaign_run_id: str
    customers: tuple[TopCustomer, ...] = ()
    unavailable: str | None = None


# ---------------------------------------------------------------------------
# The runner and what it calls
# ---------------------------------------------------------------------------
def get_runner(app: FastAPI) -> CampaignRunRunner:
    """One runner per app, built from the same services the routes use."""
    with _LOCK:
        existing: CampaignRunRunner | None = getattr(app.state, _SLOT, None)
        if existing is None:
            request = app_request(app)
            existing = CampaignRunRunner(get_storage(request), get_jobs(request), _actions(app))
            setattr(app.state, _SLOT, existing)
        return existing


def _reason(exc: HTTPException) -> StepError:
    detail: Any = exc.detail
    if not isinstance(detail, dict):
        detail = {}
    return StepError(str(detail.get("code", "RUN_REFUSED")), str(detail.get("message", exc.detail)))


def _started(result: object) -> str:
    """The run id of a started run; a refusal (the data no longer passes its checks) as a `StepError`."""
    if isinstance(result, RunCreatedResponse):
        return result.run_id
    body = json.loads(bytes(getattr(result, "body", b"{}")))
    detail = body.get("detail", {})
    raise StepError(
        str(detail.get("code", "VALIDATION_FAILED")),
        f"{detail.get('message', 'The data did not pass the checks.')} Open the Data Doctor and check it again.",
    )


def _missing_columns(storage: Storage, champion: ModelVersion, learned: ModelVersion) -> list[str]:
    """The columns the champion needs that the file `learned` was trained on does not have (it cannot score it)."""
    try:
        needed = [
            c.name for c in storage.read_model(champion.schema_key, FeatureSchema).columns if c.required
        ]
        offered = {c.name for c in storage.read_model(learned.schema_key, FeatureSchema).columns}
    except Exception:  # an unreadable schema: keep the champion, as before
        return []
    return [name for name in needed if name not in offered]


def _model_for_file(
    storage: Storage, champion: ModelVersion, learned: ModelVersion, *, approval_required: bool
) -> str:
    """The model to score next month's file with: the champion, unless it cannot read the file.

    The model just learned can then stand in only when it may be used with nobody choosing it: while approval is
    required, a version no Approver has seen is not handed to the contact list (`engine.model_approval`).
    """
    missing = _missing_columns(storage, champion, learned)
    if not missing:
        return champion.model_id
    if approval_required and model_not_approved(learned, unattended=True):
        names = ", ".join(f"'{n}'" for n in missing[:5]) + (" and others" if len(missing) > 5 else "")
        raise StepError(
            "MODEL_NOT_APPROVED",
            f"Last month's model needs {names}, which this file does not have. The model just learned can read "
            "this file but has not been approved yet, so no contact list was made. An Approver can make it the "
            "model in use on the Approvals page; then start this again.",
        )
    return learned.model_id  # a renamed or dropped column: only the new model can read this file


def _actions(app: FastAPI) -> StepActions:
    request = app_request(app)

    def run(body: RunRequest, who: str | None) -> str:
        try:
            return _started(
                start_run(
                    body,
                    root=get_config_root(request),
                    storage=get_storage(request),
                    registry=get_registry(request),
                    jobs=get_jobs(request),
                    settings=get_settings(request),
                    request=request,
                    user_id=who,
                    enforce_roles=False,  # checked against the person's roles when the campaign run was taken
                )
            )
        except HTTPException as exc:
            raise _reason(exc) from exc

    def source(record: CampaignRun) -> dict[str, Any]:
        return {
            "use_case": record.use_case_id,
            "dataset_id": record.dataset_id,
            "client_id": record.client_id,
            "primary_key": record.primary_key,
            "confirm_cost": record.confirm_cost,
        }

    def start_train(record: CampaignRun) -> str:
        return run(
            RunRequest(
                **source(record),
                mode=RunMode.TRAIN,
                upload_id=record.upload_id,
                target=record.target,
                overrides=overrides_for(record.overrides, record.speed, get_config_root(request)),
            ),
            record.requested_by,
        )

    def do_clone_upload(record: CampaignRun) -> str:
        storage, root = get_storage(request), get_config_root(request)
        assert record.upload_id is not None
        upload = load_upload(storage, record.upload_id)
        config = use_case_config(record.use_case_id, root)
        return clone_upload(storage, root, upload, config, mode=RunMode.SCORE).upload_id

    def start_score(record: CampaignRun) -> str:
        return run(
            RunRequest(
                **source(record),
                mode=RunMode.SCORE,
                upload_id=record.score_upload_id,
                model_version_id=record.model_version_id,
                overrides=record.overrides,
            ),
            record.requested_by,
        )

    def usable_model(train_run_id: str) -> str | None:
        """The model just learned if the contact list may use it (champion, or waiting for approval)."""
        trained = load_run(get_storage(request), train_run_id)
        registry = get_registry(request)
        if trained.model_version_id is not None:
            version = registry.get(trained.model_version_id)
            if version.status in (ModelStatus.CHAMPION, ModelStatus.PENDING_APPROVAL):
                return version.model_id
        champion = registry.get_champion(
            trained.use_case_id
        )  # the new model was not better: use the current one
        if champion is None:
            return None
        if trained.model_version_id is None:
            return champion.model_id
        try:
            resolved = get_storage(request).read_model(
                run_key(train_run_id, RUN_CONFIG_FILENAME), ResolvedConfig
            )
            approval_required = resolved.config.governance.approval_required
        except StorageError:
            approval_required = True  # fails closed
        return _model_for_file(
            get_storage(request),
            champion,
            registry.get(trained.model_version_id),
            approval_required=approval_required,
        )

    def build_list(score_run_id: str) -> None:
        try:
            ensure_treat_list(get_storage(request), score_run_id, config_root=get_config_root(request))
        except TreatListError as exc:
            raise StepError(exc.code, str(exc)) from exc

    def do_open_campaign(record: CampaignRun) -> str | None:
        storage, root = get_storage(request), get_config_root(request)
        assert record.score_run_id is not None
        scored = load_run(storage, record.score_run_id)
        store = get_campaign_store(request)
        recorded = store.list(
            run_id=scored.run_id, limit=1
        )  # a tick repeated after a crash records nothing twice
        if recorded:
            return recorded[0].campaign_id
        config = use_case_config(scored.use_case_id, root)
        assert scored.finished_at is not None
        try:
            return open_campaign(
                storage,
                store,
                scored,
                config,
                finished_at=scored.finished_at,
                treatment_start=None,
                bands=None,
                outcome_window_days=default_outcome_window(config),
                name=None,
                created_by=record.requested_by or "local",
                now=utc_now(),
            ).campaign_id
        except CycleError as exc:
            if exc.code == MEASURE_NOT_OFFERED:
                return None  # a goal that holds nobody back has a list and no campaign
            raise StepError(exc.code, exc.message) from exc

    return StepActions(
        start_train=start_train,
        clone_upload=do_clone_upload,
        start_score=start_score,
        usable_model=usable_model,
        build_list=build_list,
        open_campaign=do_open_campaign,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _require_approved_case(storage: StorageDep, body: CampaignRunRequest) -> None:
    """Nothing is published to training from a Data Doctor case its person has not approved as it stands."""
    if body.case_id is None or not body.case_id.startswith("case_"):  # guided_/doctor_ are read-only adapters
        return
    try:
        case = CaseStore(storage).get(body.client_id or DEFAULT_CLIENT_ID, body.case_id)
    except CaseError as exc:
        raise http_error(404 if exc.code == "CASE_NOT_FOUND" else 409, exc.code, exc.message) from exc
    if not is_approved(case):
        raise http_error(
            409,
            "CASE_NOT_APPROVED",
            "This Data Doctor check has not been approved as it stands. Approve it first, then start the campaign.",
        )


@router.post(
    "/campaign-runs",
    response_model=CampaignRunView,
    status_code=202,
    responses=_ERRORS,
    summary="Learn from the customers, score them, build the contact list and record the campaign",
)
def create_campaign_run(
    body: CampaignRunRequest, request: Request, response: Response, root: ConfigRootDep, storage: StorageDep
) -> CampaignRunView:
    """Takes the request and starts the first step; the rest goes on without the browser."""
    # Imported here: api.access imports api.routes.uploads, whose package imports the runs module.
    from api.access import require_roles_for_run_overrides

    own = use_case_config(body.use_case, root)  # 404 for a planned or unknown id
    _require_approved_case(storage, body)
    resolved = resolve_config(body.use_case, overrides_for(body.overrides, body.speed, root), root=root)
    require_roles_for_run_overrides(request, use_case=own, resolved=resolved.config)
    if body.model_version_id is None:
        _refuse_uplift_training(RunMode.TRAIN, resolved)
    if body.upload_id is not None:
        load_upload(storage, body.upload_id)  # 404 before anything is recorded
    runner = get_runner(request.app)
    source = body.upload_id or body.dataset_id
    for active in runner.store.list(state=CampaignRunState.RUNNING):
        if active.use_case_id == body.use_case and (active.upload_id or active.dataset_id) == source:
            raise http_error(
                409,
                "CAMPAIGN_RUN_ACTIVE",
                f"A campaign is already being prepared from this data ({active.campaign_run_id}).",
            )
    record = runner.store.create(
        use_case_id=body.use_case,
        upload_id=body.upload_id,
        dataset_id=body.dataset_id,
        client_id=body.client_id,
        case_id=body.case_id,
        primary_key=body.primary_key,
        target=body.target,
        speed=body.speed,
        overrides=body.overrides,
        confirm_cost=body.confirm_cost,
        requested_by=requested_by(request),
        model_version_id=body.model_version_id,
    )
    set_audit_context(request, object_id=record.campaign_run_id, details={"use_case": body.use_case})
    started = runner.advance(record.campaign_run_id) or record
    runner.ensure_running()
    response.headers["Location"] = f"/campaign-runs/{record.campaign_run_id}"
    return runner.view(started)


@router.get(
    "/campaign-runs",
    response_model=CampaignRunListResponse,
    summary="Campaign runs, newest first",
)
def list_campaign_runs(
    request: Request,
    state: Annotated[CampaignRunState | None, Query(description="Keep only runs in this state.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    score_run_id: Annotated[
        str | None, Query(description="Keep only the run that made this contact list.")
    ] = None,
) -> CampaignRunListResponse:
    runner = get_runner(request.app)
    return CampaignRunListResponse(
        campaign_runs=tuple(
            runner.view(run) for run in runner.store.list(state=state, limit=limit, score_run_id=score_run_id)
        )
    )


def _read(request: Request, campaign_run_id: str, *, advance: bool) -> CampaignRunView:
    runner = get_runner(request.app)
    run = runner.advance(campaign_run_id, wait=False) if advance else runner.store.get(campaign_run_id)
    if run is None:
        raise http_error(404, "CAMPAIGN_RUN_NOT_FOUND", f"No campaign run with id {campaign_run_id!r}.")
    runner.ensure_running()
    return runner.view(run)


@router.get(
    "/campaign-runs/{campaign_run_id}",
    response_model=CampaignRunView,
    responses=_ERRORS,
    summary="Where a campaign run is: its steps, time left and, when done, its campaign",
)
def read_campaign_run(campaign_run_id: str, request: Request) -> CampaignRunView:
    """Reading also moves the run on, so a run is never stuck because nothing else was watching it."""
    return _read(request, campaign_run_id, advance=True)


@router.get(
    "/campaign-runs/{campaign_run_id}/top-customers",
    response_model=TopCustomersResponse,
    responses=_ERRORS,
    summary="The first customers of a finished campaign run's contact list, best first",
)
def read_top_customers(
    campaign_run_id: str,
    request: Request,
    storage: StorageDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> TopCustomersResponse:
    """Read through the contact list's own download route, so its role and model-approval rules apply unchanged.

    A list that is not finished, or is held (for example its new model still waits for approval), answers with an
    empty list and the reason in plain words, never an error: the summary then says why it shows no names.
    """
    run = get_runner(request.app).store.get(campaign_run_id)
    if run is None:
        raise http_error(404, "CAMPAIGN_RUN_NOT_FOUND", f"No campaign run with id {campaign_run_id!r}.")
    if run.score_run_id is None:
        return TopCustomersResponse(
            campaign_run_id=campaign_run_id, unavailable="The contact list is not built yet."
        )
    try:
        csv_bytes = read_artefact(run.score_run_id, "treat_list.csv", storage, request).body
    except HTTPException as exc:
        if exc.status_code == 401:
            raise
        detail: Any = exc.detail
        message = detail.get("message") if isinstance(detail, dict) else None
        return TopCustomersResponse(
            campaign_run_id=campaign_run_id,
            unavailable=str(message or "The contact list cannot be shown right now."),
        )
    return TopCustomersResponse(
        campaign_run_id=campaign_run_id, customers=tuple(top_customers(bytes(csv_bytes), limit))
    )


@router.post(
    "/campaign-runs/{campaign_run_id}/restart",
    response_model=CampaignRunView,
    status_code=202,
    responses=_ERRORS,
    summary="Start a stopped or failed campaign run again, as a new one with the same choices",
)
def restart_campaign_run(
    campaign_run_id: str,
    request: Request,
    response: Response,
    root: ConfigRootDep,
    storage: StorageDep,
    body: RestartRequest | None = None,
) -> CampaignRunView:
    """A run the server restarted under, or one that needs a cost confirmed, goes on from the start."""
    old = get_runner(request.app).store.get(campaign_run_id)
    if old is None:
        raise http_error(404, "CAMPAIGN_RUN_NOT_FOUND", f"No campaign run with id {campaign_run_id!r}.")
    if old.state in (CampaignRunState.RUNNING, CampaignRunState.DONE):
        raise http_error(
            409, "CAMPAIGN_RUN_NOT_STOPPED", "This campaign run has not stopped, so it cannot restart."
        )
    return create_campaign_run(
        CampaignRunRequest(
            use_case=old.use_case_id,
            upload_id=old.upload_id,
            dataset_id=old.dataset_id,
            client_id=old.client_id,
            case_id=old.case_id,
            primary_key=old.primary_key,
            target=old.target,
            speed=old.speed,
            overrides=old.overrides,
            confirm_cost=old.confirm_cost or bool(body and body.confirm_cost),
            model_version_id=old.model_version_id if old.train_run_id is None else None,
        ),
        request,
        response,
        root,
        storage,
    )


@router.post(
    "/campaign-runs/{campaign_run_id}/cancel",
    response_model=CampaignRunView,
    responses=_ERRORS,
    summary="Stop a campaign run; a model it already learned stays in the registry",
)
def cancel_campaign_run(campaign_run_id: str, request: Request) -> CampaignRunView:
    runner = get_runner(request.app)
    if runner.cancel(campaign_run_id) is None:
        raise http_error(404, "CAMPAIGN_RUN_NOT_FOUND", f"No campaign run with id {campaign_run_id!r}.")
    return _read(request, campaign_run_id, advance=False)


# ---------------------------------------------------------------------------
# Startup: pick up what a restart left behind
# ---------------------------------------------------------------------------
def _resume(app: FastAPI) -> None:
    """A run still "running" with its job gone is stopped by its first tick; the rest carry on."""
    try:
        if not get_storage(app_request(app)).list_keys(PREFIX):
            return  # nothing was ever started here: build nothing
        get_runner(app).ensure_running()
    except Exception as exc:  # the first request reports a settings problem with its own envelope
        log_failure(_LOGGER, "campaign_run.resume", exc, level=logging.ERROR)


def _stop(app: FastAPI) -> None:
    runner: CampaignRunRunner | None = getattr(app.state, _SLOT, None)
    if runner is not None:
        runner.stop()


def install_campaign_runs(app: FastAPI) -> None:
    """`PHASE_APP_HOOKS` entry: resume with the app, stop with the app."""
    app.router.on_startup.append(lambda: _resume(app))
    app.router.on_shutdown.append(lambda: _stop(app))
