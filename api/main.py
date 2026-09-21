"""The FastAPI application: config-only endpoints in M1 (design §7).

`create_app` takes the configuration root and the data directory as arguments so a test can point the
whole app at a fixture tree; `app` is the module-level instance `uvicorn api.main:app` serves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import ALL_ROUTERS
from api.schemas import ErrorBody, ErrorResponse, HealthResponse
from engine import __version__
from engine.config import ConfigError

NOT_FOUND_CODES: Final[frozenset[str]] = frozenset(
    {"USE_CASE_NOT_FOUND", "USE_CASE_PLANNED", "INDUSTRY_NOT_FOUND"}
)
"""`ConfigError` codes that mean "no such thing" rather than "your document is wrong"."""


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
    for router in ALL_ROUTERS:
        app.include_router(router)

    @app.get("/healthz", response_model=HealthResponse, tags=["health"], summary="Liveness probe")
    def healthz() -> HealthResponse:
        """The engine version this process serves (DEC-024)."""
        return HealthResponse(status="ok", version=__version__)

    return app


app: FastAPI = create_app()
