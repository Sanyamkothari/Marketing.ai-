"""Plan G routes: the dry-run data checks now, the Guided-setup session from M74.

`POST /uploads/{upload_id}/checks` answers "what would the Run button say about this file, with
this ID column, outcome and these settings?" without creating a run or writing anything. It
follows `POST /runs` step for step - the same config resolution, the same role rule on overrides,
the same mode check, the same model-version resolver for a scoring file, and the same validators
through `engine.agent.checks.check_plan` - so the two cannot disagree (Plan G §9, M71).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import Field

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep, get_registry
from api.routes.runs import read_frame, score_version
from api.routes.uploads import http_error, load_upload, load_upload_profile, profile_row_cap, use_case_config
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.agent.checks import check_plan
from engine.config import PrimaryKey, RunMode, StrictBase, resolve_config, sole_key
from engine.contracts import FeatureSchema, ValidationReport

__all__ = ["ChecksRequest", "router"]

router = APIRouter(tags=["agent"])

register(
    {
        # Analyst: a dry run resolves overrides exactly as starting a run does (DEC-1011).
        ("POST", "/uploads/{upload_id}/checks"): RoutePolicy(
            role=Role.ANALYST,
            action="uploads.checks",
            purpose="check an upload",
            object_type="upload",
            object_param="upload_id",
        ),
    }
)


class ChecksRequest(StrictBase):
    """The roles and settings to check an upload against; the same fields `POST /runs` takes."""

    use_case: str = Field(description="Use-case id.")
    primary_key: PrimaryKey | None = Field(default=None, description="The ID column.")
    target: str | None = Field(default=None, description="The outcome column; training files only.")
    model_version_id: str | None = Field(
        default=None,
        description="Scoring files only: the model version to check against; default the one in use.",
    )
    overrides: dict[str, object] = Field(
        default_factory=dict, description="Run overrides, as for `POST /runs`."
    )


@router.post(
    "/uploads/{upload_id}/checks",
    response_model=ValidationReport,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Run the data checks for an upload without starting a run",
)
def check_upload(
    upload_id: str,
    body: ChecksRequest,
    root: ConfigRootDep,
    storage: StorageDep,
    request: Request,
) -> ValidationReport:
    """The report `POST /runs` would give, always with 200; `passed` says whether Run would start.

    Nothing is written: not a run directory, and not the upload's `validation.json`, which stays the
    record of the last real Run. The registry is opened only for a scoring file, because opening it
    creates the local registry database.
    """
    use_case_config(body.use_case, root)  # 404 for a planned or unknown id, before anything is read
    resolved = resolve_config(body.use_case, body.overrides, root=root)
    config = resolved.config
    from api.access import require_roles_for_run_overrides  # as in POST /runs: avoids an import cycle

    require_roles_for_run_overrides(request, use_case=use_case_config(body.use_case, root), resolved=config)
    upload = load_upload(storage, upload_id)
    profile = load_upload_profile(storage, upload_id)
    primary_key = sole_key(body.primary_key, what="A check") if body.primary_key is not None else None
    schema: FeatureSchema | None = None
    if upload.mode is RunMode.SCORE:
        version = score_version(get_registry(request), config=config, version_id=body.model_version_id)
        schema = storage.read_model(version.schema_key, FeatureSchema)
    elif body.model_version_id is not None:
        raise http_error(
            409, "UPLOAD_MODE_MISMATCH", "A model version is only used to check a file uploaded for scoring."
        )
    return check_plan(
        read_frame(storage, upload.source_key, upload.file_format, profile_row_cap(config)),
        config,
        mode=upload.mode,
        primary_key=primary_key,
        target=body.target,
        upload_id=upload_id,
        row_count=profile.row_count,
        schema=schema,
    )
