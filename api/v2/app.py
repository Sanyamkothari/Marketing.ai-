"""A standalone app for the v2 API, for local development and the demo.

`create_app(services)` returns a FastAPI app with the `/v2` router, CORS open to localhost, and the
marketer app (ui/v2) served at `/v2/app` when that folder exists. Wave 2 mounts the same router
in `api/main.py` instead (with `attach_services`), so nothing here is needed in production.

    python -m api.v2.app                    # the demo API on http://127.0.0.1:8010
    python -m api.v2.app --fake             # the same on the small fake-world demo (starts faster)
    python -m api.v2.app --openapi          # rewrite docs/v2/openapi.json and exit

The demo is a Gym shop run by the whole system (`autonomy.runtime.demo.run_demo`) for `--days`
simulated days, ending at today's 09:00 UTC sweep; `POST /v2/runtime/tick` keeps it running. The
win-back goal has run on autopilot all along; the repeat-purchase goal started ten days ago at the
APPROVE level, and its latest batch is waiting for the marketer.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.v2.deps import Services, attach_services, build_demo_services
from api.v2.engines.router import router as engines_router
from api.v2.routes import router

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "ui" / "v2"
OPENAPI_PATH = REPO_ROOT / "docs" / "v2" / "openapi.json"
LOCAL_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
"""Pages served from this machine on any port; the UI's own dev server is one of them."""
DEFAULT_PORT = 8010
FAKE_CUSTOMERS = 200
FAKE_DAYS = 7
DEMO_CUSTOMERS = 1200
DEMO_DAYS = 56
"""Eight weeks: long enough for six weeks of 14-day outcome windows to close and for every proof
group to pass its minimum size, so the proof page puts numbers (with ranges) on the agent's effect.
Simulating it takes about 35 seconds on a laptop; `--customers` and `--days` trade realism for speed."""
DEMO_APPROVE_GOAL_DAYS = 10
"""The APPROVE goal starts this many days before the end of the demo, as a second goal the marketer
added recently. It is shorter than the 14-day outcome window, so none of that goal's outcomes is in
the proof yet and the autopilot goal's proof stays its own (one shared holdout cannot tell two goals
apart, so a goal's own proof is withheld once another goal's outcomes count)."""
SCHEMA_NOW = datetime(2026, 1, 1, tzinfo=UTC)
"""The demo's "now" when it is built only to print the schema (the schema does not depend on it)."""


def create_app(
    services: Services, *, ui_dir: Path | None = None, cors_origin_regex: str = LOCAL_ORIGINS
) -> FastAPI:
    app = FastAPI(
        title="Autonomy API",
        version="2.0.0",
        description=(
            "The v2 marketer app's API: goals, plans, approvals, batches, decisions, proof, webhooks "
            "and the operator's runtime tick."
        ),
    )
    attach_services(app, services)
    app.add_exception_handler(RequestValidationError, _validation_error)
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
    app.include_router(engines_router)  # the six standalone engines, /v2/engines/... (docs/v2/ENGINES.md)
    return app


def _json_safe(value: Any) -> Any:
    """Inputs echoed back in a 422 may hold infinity (JSON's 1e999), which JSON cannot carry."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """FastAPI's own 422, made safe for inputs JSON cannot encode (otherwise it raises a 500)."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{k: _json_safe(v) for k, v in e.items() if k != "ctx"} for e in errors]
    return JSONResponse(status_code=422, content={"detail": detail})


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
    parser.add_argument("--fake", action="store_true", help="serve the small fake-world demo instead")
    parser.add_argument("--customers", type=int, help=f"simulated customers (default {DEMO_CUSTOMERS})")
    parser.add_argument("--days", type=int, help=f"simulated days of history (default {DEMO_DAYS})")
    args = parser.parse_args(argv)
    if args.openapi:
        export_openapi(Path(args.openapi))
        print(f"wrote {args.openapi}")
        return

    import uvicorn  # only needed to serve; the light CI environment does not install it

    # The edge of the system: the one place the demo reads the clock.
    now = datetime.now(UTC)
    # The fake-world demo keeps its own small defaults unless sizes are given.
    customers: int = args.customers or (FAKE_CUSTOMERS if args.fake else DEMO_CUSTOMERS)
    days: int = args.days or (FAKE_DAYS if args.fake else DEMO_DAYS)
    if not args.fake:
        print(f"simulating {customers:,} customers over {days} days; this takes a little while ...")
    services = build_demo_services(
        now,
        n_customers=customers,
        days=days,
        clock=lambda: datetime.now(UTC),
        simulation=not args.fake,
        approve_goal_days=DEMO_APPROVE_GOAL_DAYS,
        hold_batches=True,  # the last day's batch waits for the marketer
    )
    uvicorn.run(create_app(services), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
