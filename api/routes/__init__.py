"""The routers `api.main.create_app` mounts, in the order they appear in `docs/API.md`."""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from api.routes.industries import router as industries_router
from api.routes.models import router as models_router
from api.routes.runs import router as runs_router
from api.routes.uploads import router as uploads_router
from api.routes.use_cases import router as use_cases_router

ALL_ROUTERS: Final[tuple[APIRouter, ...]] = (
    industries_router,
    use_cases_router,
    uploads_router,
    runs_router,
    models_router,
)

__all__ = ["ALL_ROUTERS"]
