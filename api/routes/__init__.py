"""The routers `api.main.create_app` mounts, in the order they appear in `docs/API.md`."""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from api.routes.industries import router as industries_router
from api.routes.use_cases import router as use_cases_router

ALL_ROUTERS: Final[tuple[APIRouter, ...]] = (industries_router, use_cases_router)

__all__ = ["ALL_ROUTERS"]
