"""A standalone app for the v2 API, for local development and the demo.

`create_app(services)` returns a FastAPI app with the `/v2` router, CORS open to localhost, and the
marketer app (ui/v2) served at `/v2/app` when that folder exists. Wave 2 mounts the same router
in `api/main.py` instead (with `attach_services`), so nothing here is needed in production.

    python -m api.v2.app                    # the demo API on http://127.0.0.1:8010
    python -m api.v2.app --openapi          # rewrite docs/v2/openapi.json and exit
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.v2.deps import Services, attach_services, build_demo_services
from api.v2.routes import router

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "ui" / "v2"
OPENAPI_PATH = REPO_ROOT / "docs" / "v2" / "openapi.json"
LOCAL_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
"""Pages served from this machine on any port; the UI's own dev server is one of them."""
DEFAULT_PORT = 8010
SCHEMA_NOW = datetime(2026, 1, 1, tzinfo=UTC)
"""The demo's "now" when it is built only to print the schema (the schema does not depend on it)."""


def create_app(
    services: Services, *, ui_dir: Path | None = None, cors_origin_regex: str = LOCAL_ORIGINS
) -> FastAPI:
    app = FastAPI(
        title="Autonomy API",
        version="2.0.0",
        description="The v2 marketer app's API: goals, plans, approvals, decisions, proof and webhooks.",
    )
    attach_services(app, services)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=cors_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # The UI is mounted before the router: the router lives under the same /v2 prefix, and an
    # included router that claims its whole prefix would otherwise answer /v2/app/... with a 404.
    folder = UI_DIR if ui_dir is None else ui_dir
    if folder.is_dir():
        app.mount("/v2/app", StaticFiles(directory=folder, html=True), name="v2-app")
    app.include_router(router)
    return app


def export_openapi(path: Path = OPENAPI_PATH, app: FastAPI | None = None) -> dict[str, Any]:
    """Write the API's OpenAPI schema (the contract the UI codes against) and return it."""
    if app is None:
        app = create_app(build_demo_services(SCHEMA_NOW, n_customers=10, days=1))
    schema: dict[str, Any] = app.openapi()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return schema


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m api.v2.app", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--openapi", nargs="?", const=str(OPENAPI_PATH), help="write the OpenAPI schema here and exit"
    )
    args = parser.parse_args(argv)
    if args.openapi:
        export_openapi(Path(args.openapi))
        print(f"wrote {args.openapi}")
        return

    import uvicorn  # only needed to serve; the light CI environment does not install it

    # The edge of the system: the one place the demo reads the clock.
    now = datetime.now(UTC)
    services = build_demo_services(now, clock=lambda: datetime.now(UTC))
    uvicorn.run(create_app(services), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
