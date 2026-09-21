"""`GET /use-cases/{id}` and the two template downloads (design §7.2, §7.3).

The use-case body is a projection of one merged `UseCaseConfig`: nothing here invents a default, a
label or a copy string, and no branch inspects the use-case id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from api.deps import ConfigRootDep
from api.schemas import (
    ApiChoice,
    ErrorResponse,
    LimitsBlock,
    ModeOption,
    RunningRows,
    SetupBlock,
    TargetBlock,
    UseCaseResponse,
)
from engine.config import (
    TIME_LIKE_PATTERN,
    Catalog,
    ConfigError,
    ModeCopy,
    RunMode,
    UseCaseConfig,
    UseCaseStatus,
    advanced_settings_schema,
    get_catalog,
    list_industries,
    list_use_case_ids,
    load_industry,
    load_use_case,
)
from engine.pipeline import running_rows
from engine.templates import render_template_csv, render_template_readme, template_filenames

router: APIRouter = APIRouter(tags=["use-cases"])

ColumnsQuery = Annotated[
    str | None, Query(description="Comma-separated header of the uploaded file; populates column widgets.")
]
PrimaryKeyQuery = Annotated[str | None, Query(description="Chosen primary key; excluded from features.")]
TargetQuery = Annotated[str | None, Query(description="Chosen target column; excluded from features.")]

_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}


@router.get(
    "/use-cases/{use_case_id}",
    response_model=UseCaseResponse,
    responses=_NOT_FOUND,
    summary="One merged use-case configuration, its Setup copy and its advanced-settings schema",
)
def read_use_case(
    use_case_id: str,
    root: ConfigRootDep,
    columns: ColumnsQuery = None,
    primary_key: PrimaryKeyQuery = None,
    target: TargetQuery = None,
) -> UseCaseResponse:
    """The whole Setup screen for one use case. A planned or unknown id is a 404 with a code."""
    config = _use_case(use_case_id, root)
    catalog = get_catalog(root)
    spec = catalog.ai_types[config.ai_type]
    return UseCaseResponse(
        id=config.id,
        name=config.name,
        description=config.description,
        lifecycle_stage=config.lifecycle_stage,
        ai_type=config.ai_type,
        marker=spec.marker,
        stars=spec.stars,
        problem_type=config.problem_type,
        problem_type_label=catalog.problem_types[config.problem_type].label,
        entity=config.entity,
        trainable_in_phase_1=config.trainable_in_phase_1,
        target=TargetBlock(
            column=config.target.column,
            positive_label=config.target.positive_label,
            definition=config.target.definition,
            label_source=config.target.label_source,
            label=_fill(config.ui.target_label, config.entity),
        ),
        primary_key_hints=config.primary_key_hints,
        time_column_hints=config.time_column_hints,
        time_like_pattern=TIME_LIKE_PATTERN,
        limits=LimitsBlock(
            min_rows=config.validation.min_rows,
            min_positive=config.validation.min_positive,
            max_file_size_mb=config.validation.max_file_size_mb,
        ),
        setup=_setup(config, catalog),
        pages=config.ui.pages,
        running_rows=RunningRows(train=running_rows(RunMode.TRAIN), score=running_rows(RunMode.SCORE)),
        output=config.output,
        config=config,
        advanced_settings=advanced_settings_schema(
            config, columns=_split_columns(columns), primary_key=primary_key, target=target
        ),
    )


@router.get(
    "/use-cases/{use_case_id}/template.csv",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="The upload template of one use case as CSV",
)
@router.head("/use-cases/{use_case_id}/template.csv", response_class=Response, include_in_schema=False)
def read_template_csv(use_case_id: str, root: ConfigRootDep) -> Response:
    """Byte-identical to the committed `templates/<stem>_template.csv` (a test asserts it).

    `HEAD` is registered explicitly because FastAPI does not add it to `GET` routes: a download client
    (or `curl -I`) probes the file's headers first, and the server drops the body for a `HEAD`. It is
    kept out of the schema so the endpoint table documents one resource, not a header probe.
    """
    config = _use_case(use_case_id, root)
    filename = _template_filename(config, readme=False)
    return Response(
        content=render_template_csv(config),
        media_type="text/csv; charset=utf-8",
        headers=_download_headers(filename),
    )


@router.get(
    "/use-cases/{use_case_id}/template_README.md",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="The upload template's README of one use case as Markdown",
)
@router.head("/use-cases/{use_case_id}/template_README.md", response_class=Response, include_in_schema=False)
def read_template_readme(use_case_id: str, root: ConfigRootDep) -> Response:
    """Byte-identical to the committed `templates/<stem>_template_README.md` (DEC-024)."""
    config = _use_case(use_case_id, root)
    filename = _template_filename(config, readme=True)
    return Response(
        content=render_template_readme(config),
        media_type="text/markdown; charset=utf-8",
        headers=_download_headers(filename),
    )


def _download_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }


def _template_filename(config: UseCaseConfig, *, readme: bool) -> str:
    """The committed filename, or a 404 `TEMPLATE_EMPTY` when the use case declares no columns."""
    if not config.template.columns:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "TEMPLATE_EMPTY",
                "message": f"{config.name} does not define an upload template.",
                "path": "template.columns",
            },
        )
    csv_name, readme_name = template_filenames(config)
    return readme_name if readme else csv_name


def _use_case(use_case_id: str, root: Path) -> UseCaseConfig:
    """The merged config of `use_case_id`, or a `ConfigError` the app renders as a 404."""
    if use_case_id in list_use_case_ids(root):
        return load_use_case(use_case_id, root)
    if _is_planned(use_case_id, root):
        raise ConfigError("USE_CASE_PLANNED", "This use case is not available yet.")
    raise ConfigError("USE_CASE_NOT_FOUND", f"There is no use case called {use_case_id!r}.", path=use_case_id)


def _is_planned(use_case_id: str, root: Path) -> bool:
    """True when an industry file carries `use_case_id` as a planned entry (DEC-003)."""
    for industry_id in list_industries(root):
        for _stage, ref in load_industry(industry_id, root).all_refs():
            if ref.id == use_case_id and ref.status is UseCaseStatus.PLANNED:
                return True
    return False


def _setup(config: UseCaseConfig, catalog: Catalog) -> SetupBlock:
    """The Setup-screen copy and choices, all of it from the merged config and the catalog."""
    return SetupBlock(
        modes=tuple(_mode(config, mode) for mode in RunMode),
        target_label=_fill(config.ui.target_label, config.entity),
        problem_type_choices=tuple(
            ApiChoice(
                value=problem_type.value,
                label=spec.label,
                enabled=spec.enabled,
                help=None if spec.enabled else "Available in a later phase.",
            )
            for problem_type, spec in catalog.problem_types.items()
        ),
        model_choices=_model_choices(config, catalog),
        template_url=f"/use-cases/{config.id}/template.csv",
        template_readme_url=f"/use-cases/{config.id}/template_README.md",
    )


def _model_choices(config: UseCaseConfig, catalog: Catalog) -> tuple[ApiChoice, ...]:
    """`catalog.automl_choice` first, then the candidate pool in its configured order (DEC-019)."""
    automl = catalog.automl_choice
    choices = [ApiChoice(value=automl.value, label=automl.label)]
    pool = config.model_search.candidate_pool
    missing = catalog.missing_requirements(pool)
    for family in pool:
        label = catalog.family_label(family)
        available = family not in missing
        choices.append(
            ApiChoice(
                value=family.value,
                label=label,
                enabled=available,
                help=(
                    None
                    if available
                    else (
                        f"The {label} model needs the optional 'nn' extra. "
                        "Run `make setup EXTRAS=nn`, or deselect it."
                    )
                ),
            )
        )
    return tuple(choices)


def _mode(config: UseCaseConfig, mode: RunMode) -> ModeOption:
    """One mode's copy, with `{entity}` substituted (DEC-022)."""
    ui = config.ui
    entity = config.entity
    return ModeOption(
        value=mode,
        label=_fill(_copy(ui.mode_labels, mode), entity),
        help=_fill(_copy(ui.mode_help, mode), entity),
        dataset_hint=_fill(_copy(ui.dataset_hint, mode), entity),
        columns_hint=_fill(_copy(ui.columns_hint, mode), entity),
        run_button=_fill(_copy(ui.run_button, mode), entity),
    )


def _copy(copy: ModeCopy, mode: RunMode) -> str:
    return copy.train if mode is RunMode.TRAIN else copy.score


def _fill(text: str, entity: str) -> str:
    """`{entity}` is the one placeholder the copy block understands."""
    return text.replace("{entity}", entity)


def _split_columns(columns: str | None) -> tuple[str, ...] | None:
    """`a,b,c` -> `("a", "b", "c")`; absent or blank -> `None` (the UI disables the column widgets)."""
    if columns is None:
        return None
    parsed = tuple(part.strip() for part in columns.split(",") if part.strip())
    return parsed or None
