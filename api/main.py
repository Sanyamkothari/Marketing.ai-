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
from fastapi.staticfiles import StaticFiles

from api.routes import ALL_ROUTERS
from api.schemas import ErrorBody, ErrorResponse, HealthResponse
from engine import __version__
from engine.config import ConfigError
from engine.settings import Settings, SettingsError

NOT_FOUND_CODES: Final[frozenset[str]] = frozenset(
    {"USE_CASE_NOT_FOUND", "USE_CASE_PLANNED", "INDUSTRY_NOT_FOUND"}
)
"""`ConfigError` codes that mean "no such thing" rather than "your document is wrong"."""

UI_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "ui"
"""The prototype screens, wired to this same app and served from `/ui` (plan §9)."""


async def config_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render a `ConfigError` as `{"detail": {"code", "message", "path"}}` with 404 or 422."""
    if not isinstance(exc, ConfigError):
        raise exc
    status_code = 404 if exc.code in NOT_FOUND_CODES else 422
    body = ErrorResponse(detail=ErrorBody(code=exc.code, message=exc.message, path=exc.path))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def settings_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render a `SettingsError` as the same envelope, with 503.

    A `SettingsError` means this deployment is described wrongly or incompletely: the bucket is
    missing, a parameter is misspelt, a backend was selected without what it needs. It is raised
    lazily, on the first request that needs a service, so without this handler an operator would get
    FastAPI's bare 500 and no idea which setting was at fault. 503 rather than 500 because the
    process is healthy and the *configuration* is not - and `path` carries the field name, never the
    value, for the same reason `SettingsError` itself refuses to quote one (DEC-303).
    """
    if not isinstance(exc, SettingsError):
        raise exc
    body = ErrorResponse(detail=ErrorBody(code=exc.code, message=exc.message, path=exc.field))
    return JSONResponse(status_code=503, content=body.model_dump(mode="json"))


def create_app(
    *,
    config_root: Path | None = None,
    data_dir: Path | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the application.

    `config_root` and `data_dir` are stored on `app.state` and read by `api.deps`; leaving them `None`
    falls back to `$MARKETING_AI_CONFIG_DIR` / `$MARKETING_AI_DATA_DIR` and then to the checkout.
    `settings` describes the deployment - which storage, metadata and job backends to build - and is
    loaded from the environment on first use when it is not given.

    `data_dir` still outranks everything: it is how the whole test suite points the app at a
    `tmp_path`, and a stray environment variable must not be able to redirect a test's artefacts
    into a bucket. Combining it with a non-local backend is therefore refused here rather than
    silently resolved one way or the other (DEC-307).

    CORS was wide open in Phase 1 because the UI is opened as a local file (DEC-024). It still is by
    default, so nothing about a laptop changes; a deployment narrows it through `cors_origins`, and
    `Settings` refuses `*` on a production deployment so the laptop's answer cannot be inherited by
    omission.
    """
    if settings is not None and data_dir is not None and settings.storage_backend.value != "local":
        raise SettingsError(
            "SETTINGS_CONFLICT",
            "create_app(data_dir=...) means the local filesystem; it cannot be combined with "
            f"storage_backend={settings.storage_backend.value}.",
            field="storage_backend",
        )
    app = FastAPI(title="Marketing AI", version=__version__)
    app.state.config_root = config_root
    app.state.data_dir = data_dir
    app.state.settings = settings
    app.state.storage = None
    app.state.registry = None
    app.state.jobs = None
    app.state.run_index = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins) if settings is not None else ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(ConfigError, config_error_handler)
    app.add_exception_handler(SettingsError, settings_error_handler)
    for router in ALL_ROUTERS:
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


app: FastAPI = create_app()
