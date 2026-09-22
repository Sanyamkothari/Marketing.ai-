"""The FastAPI application: every endpoint plan §8 lists, plus the screens that call them.

`create_app` mounts the five routers of `api.routes` - industries, use-cases, uploads, runs and models -
and adds the `/healthz` probe here rather than in a router of its own, because a liveness check that
lived behind the same imports as the routes it is meant to vouch for would answer for them instead of
for the process. The UI is mounted on the same app at `/ui` (plan §9) so one process serves both halves
of the product. `create_app` takes the configuration root and the data directory as arguments so a test
can point the whole app at a fixture tree; `app` is the module-level instance `uvicorn api.main:app`
serves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routes import ALL_ROUTERS
from api.schemas import ErrorBody, ErrorResponse, HealthResponse
from engine import __version__
from engine.config import ConfigError

NOT_FOUND_CODES: Final[frozenset[str]] = frozenset(
    {"USE_CASE_NOT_FOUND", "USE_CASE_PLANNED", "INDUSTRY_NOT_FOUND"}
)
"""`ConfigError` codes that mean "no such thing" rather than "your document is wrong"."""

UI_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "ui"
"""The prototype screens, wired to this same app and served from `/ui` (plan §9)."""

PHASE_ROUTERS: Final[list[APIRouter]] = []
"""Routers the phase branches mount, appended from their own blocks at the foot of this module.

`ALL_ROUTERS` is the Phase 1 set and its tuple stays closed; a branch appends here instead, so
three branches can each add a router without any of them editing a line another branch wrote.
Mounted after `ALL_ROUTERS`, in the order the blocks appear.
"""


async def config_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render a `ConfigError` as `{"detail": {"code", "message", "path"}}` with 404 or 422."""
    if not isinstance(exc, ConfigError):
        raise exc
    status_code = 404 if exc.code in NOT_FOUND_CODES else 422
    body = ErrorResponse(detail=ErrorBody(code=exc.code, message=exc.message, path=exc.path))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def create_app(*, config_root: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    """Build the application.

    `config_root` and `data_dir` are stored on `app.state` and read by `api.deps`; leaving them `None`
    falls back to `$MARKETING_AI_CONFIG_DIR` / `$MARKETING_AI_DATA_DIR` and then to the checkout.
    CORS is wide open in Phase 1 because the UI is opened as a local file (DEC-024).
    """
    app = FastAPI(title="Marketing AI", version=__version__)
    app.state.config_root = config_root
    app.state.data_dir = data_dir
    app.state.storage = None
    app.state.registry = None
    app.state.jobs = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(ConfigError, config_error_handler)
    for router in (*ALL_ROUTERS, *PHASE_ROUTERS):
        app.include_router(router)
    if UI_DIR.is_dir():
        # The UI is plain HTML and ES modules: a module cannot be fetched over `file://`, so the
        # same process that answers the API also serves them (DEC-024 keeps CORS open regardless).
        app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/healthz", response_model=HealthResponse, tags=["health"], summary="Liveness probe")
    def healthz() -> HealthResponse:
        """The engine version this process serves (DEC-024)."""
        return HealthResponse(status="ok", version=__version__)

    return app


# ===========================================================================
# Shared file (PARALLEL_WORK_PROTOCOL.md §4): three branches edit it at once.
# Add code only inside your own block, at its end. Never edit above your
# block, never reorder, never reformat the rest of the file.
# A router goes in `PHASE_ROUTERS` (defined above), not in `ALL_ROUTERS`:
#     from api.routes.onboarding import router as onboarding_router
#     PHASE_ROUTERS.append(onboarding_router)
# `tests/unit/test_shared_file_markers.py` fails if a block goes missing.
# ===========================================================================

# ---- PHASE-2 (onboarding) — append only below this line ----
# ---- END PHASE-2 ----

# ---- PHASE-3A (generative) — append only below this line ----
from api.routes.generative import router as generative_router  # noqa: E402

PHASE_ROUTERS.append(generative_router)
# ---- END PHASE-3A ----

# ---- PHASE-4A (aws) — append only below this line ----
# ---- END PHASE-4A ----

app: FastAPI = create_app()
