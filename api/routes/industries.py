"""`GET /industries` — the overview screen: one journey per industry file (plan §5).

Every file in `configs/industries/` is listed (DEC-098). The overview shows one journey at a time
and opens on `default_industry`, which is also listed first so a client that only ever read the
first entry - the Phase 1 overview did - still opens on the same journey it always has.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from api.deps import ConfigRootDep
from api.schemas import (
    IndustriesResponse,
    IndustryResponse,
    LegendEntry,
    StageResponse,
    UseCaseSummary,
)
from engine.config import (
    DEFAULT_INDUSTRY,
    Catalog,
    IndustryStage,
    IndustryUseCaseRef,
    UseCaseStatus,
    get_catalog,
    list_industries,
    load_industry,
    load_use_case,
)

router: APIRouter = APIRouter(tags=["industries"])


@router.get(
    "/industries",
    response_model=IndustriesResponse,
    summary="Every industry journey with its stages and use-case cards",
)
def read_industries(root: ConfigRootDep) -> IndustriesResponse:
    """The overview screen: stages in file order, available cards filled from their use-case YAML."""
    catalog = get_catalog(root)
    ordered = _default_first(list_industries(root))
    return IndustriesResponse(
        default_industry=ordered[0] if ordered else None,
        industries=tuple(_industry(industry_id, root, catalog) for industry_id in ordered),
    )


def _default_first(industry_ids: tuple[str, ...]) -> tuple[str, ...]:
    """`DEFAULT_INDUSTRY` first when that file exists, the rest in file-name order.

    A root without it (a test fixture, a client's own root) still opens on something: its first file.
    """
    if DEFAULT_INDUSTRY not in industry_ids:
        return industry_ids
    return (
        DEFAULT_INDUSTRY,
        *(industry_id for industry_id in industry_ids if industry_id != DEFAULT_INDUSTRY),
    )


def _industry(industry_id: str, root: Path, catalog: Catalog) -> IndustryResponse:
    industry = load_industry(industry_id, root)
    return IndustryResponse(
        id=industry.id,
        name=industry.name,
        journey_label=industry.journey_label,
        legend=tuple(
            LegendEntry(ai_type=ai_type, marker=spec.marker, stars=spec.stars, label=spec.label)
            for ai_type, spec in catalog.ai_types.items()
        ),
        stages=tuple(
            _stage(stage, order, root, catalog) for order, stage in enumerate(industry.stages, start=1)
        ),
    )


def _stage(stage: IndustryStage, order: int, root: Path, catalog: Catalog) -> StageResponse:
    spec = catalog.ai_types[stage.ai_type]
    return StageResponse(
        id=stage.id,
        order=order,
        name=stage.name,
        ai_type=stage.ai_type,
        marker=spec.marker,
        stars=spec.stars,
        type_label=spec.label,
        use_cases=tuple(_summary(ref, stage, root, catalog) for ref in stage.use_cases),
    )


def _summary(ref: IndustryUseCaseRef, stage: IndustryStage, root: Path, catalog: Catalog) -> UseCaseSummary:
    """A planned entry carries only the industry file's name and description (plan §2.1 principle 5)."""
    if ref.status is UseCaseStatus.PLANNED:
        spec = catalog.ai_types[stage.ai_type]
        return UseCaseSummary(
            id=ref.id,
            name=ref.name or "",
            description=ref.description or "",
            status=ref.status,
            ai_type=stage.ai_type,
            marker=spec.marker,
            stars=spec.stars,
            problem_type=None,
            entity=None,
            target_column=None,
            trainable_in_phase_1=False,
        )
    config = load_use_case(ref.id, root)
    spec = catalog.ai_types[config.ai_type]
    return UseCaseSummary(
        id=config.id,
        name=config.name,
        description=config.description,
        status=ref.status,
        ai_type=config.ai_type,
        marker=spec.marker,
        stars=spec.stars,
        problem_type=config.problem_type,
        entity=config.entity,
        target_column=config.target.column,
        trainable_in_phase_1=config.trainable_in_phase_1,
    )
