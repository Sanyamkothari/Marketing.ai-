"""A standalone app for the v2 API, for local development and the demo.

`create_app(services)` returns a FastAPI app with the `/v2` router, CORS open to localhost, and the
marketer app (ui/v2) served at `/v2/app` when that folder exists. Wave 2 mounts the same router
in `api/main.py` instead (with `attach_services`), so nothing here is needed in production.

    python -m api.v2.app                    # the demo API on http://127.0.0.1:8010
    python -m api.v2.app --fake             # the same on the small fake-world demo (starts faster)
    python -m api.v2.app --production       # the real runtime and connectors from AUTONOMY_* settings
    python -m api.v2.app --openapi          # rewrite docs/v2/openapi.json and exit

`create_production_app(environ)` is the production wiring: the runtime from `RuntimeSettings`
(`autonomy.runtime.settings.build_runtime`) over the deployment's database and, with
`AUTONOMY_CONNECTORS=true`, the same connector engine the worker runs (`build_connector_engine`), so a
connection made on the Connect screen is what the worker syncs and sends through. `api.main` mounts
it at `/v2` when `AUTONOMY_V2_API` is on (`mount_production`).

The demo is a Gym shop run by the whole system (`autonomy.runtime.demo.run_demo`) for `--days`
simulated days, ending at today's 09:00 UTC sweep; `POST /v2/runtime/tick` keeps it running. The
win-back goal has run on autopilot all along; the repeat-purchase goal started ten days ago at the
APPROVE level, and its latest batch is waiting for the marketer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.v2.connectors import (
    CONNECTORS_PREFIX,
    ConnectorServices,
    attach_connector_services,
    local_connector_services,
)
from api.v2.connectors import router as connectors_router
from api.v2.deps import Services, attach_services, build_demo_services
from api.v2.engines.router import router as engines_router
from api.v2.images import ImageServices, attach_image_services
from api.v2.images import router as images_router
from api.v2.inbox import InboxServices, attach_inbox_services, local_inbox_services
from api.v2.inbox import autopilot_router as inbox_autopilot_router
from api.v2.inbox import router as inbox_router
from api.v2.onboarding import router as onboarding_router
from api.v2.onboarding_brand import BrandServices, attach_brand_services
from api.v2.onboarding_brand import router as brand_router
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
    services: Services,
    *,
    ui_dir: Path | None = None,
    cors_origin_regex: str = LOCAL_ORIGINS,
    connectors: ConnectorServices | None = None,
    local_connectors: bool = True,
    brand: BrandServices | None = None,
    inbox: InboxServices | None = None,
    local_inbox: bool = True,
    images: ImageServices | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Autonomy API",
        version="2.0.0",
        description=(
            "The v2 marketer app's API: goals, plans, approvals, batches, decisions, proof, webhooks, "
            "the SaaS connectors (/v2/connectors) and the operator's runtime tick."
        ),
    )
    attach_services(app, services)
    # The SaaS connectors (/v2/connectors, docs/v2/CONNECTORS.md). Without wiring of its own, the demo gets an
    # in-memory engine over the same event store, so the Connect screen shows the platforms it can connect.
    if connectors is not None:
        attach_connector_services(app, connectors)
    elif local_connectors:
        attach_connector_services(
            app,
            local_connector_services(
                business_id=services.business_id, sink=services.events.append, environ=os.environ
            ),
        )
    # Onboarding, brand voice (/v2/onboarding/brand): the kit read from the shop's own tools. Without a
    # store of its own, the kit lives in memory and the product photos in an in-memory asset library.
    attach_brand_services(app, brand if brand is not None else _memory_brand(services.clock))
    # The exceptions-only inbox and one-click Autopilot (/v2/inbox, /v2/autopilot). Without wiring of its
    # own, the demo gets one in memory over its runtime, whose ticks keep it current.
    if inbox is not None:
        attach_inbox_services(app, inbox)
    elif local_inbox:
        attach_inbox_services(app, local_inbox_services(services))
    # Generated images (/v2/images, docs/v3/IMAGES.md): off by default, so without `images` the routes answer 503.
    if images is not None:
        attach_image_services(app, images)
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
    app.include_router(connectors_router)
    app.include_router(brand_router)
    app.include_router(router)
    app.include_router(onboarding_router)  # sized goals and learned limits (docs/v2/ZERO_TOUCH.md)
    app.include_router(inbox_router)  # what needs a person, and its one button (docs/v2/ZERO_TOUCH.md)
    app.include_router(inbox_autopilot_router)  # Shadow -> Autopilot in one click
    app.include_router(images_router)  # batch approval of generated images (docs/v3/IMAGES.md)
    app.include_router(engines_router)  # the six standalone engines, /v2/engines/... (docs/v2/ENGINES.md)
    return app


def _memory_brand(clock: Callable[[], datetime]) -> BrandServices:
    from autonomy.assets.library import InMemoryAssetLibrary
    from autonomy.brand import InMemoryBrandKitStore

    return BrandServices(store=InMemoryBrandKitStore(), clock=clock, assets=InMemoryAssetLibrary())


def _production_brand(connectors: ConnectorServices | None, clock: Callable[[], datetime]) -> BrandServices:
    """The brand kit and its product photos in the deployment's database (the connector engine's)."""
    if connectors is None:
        return _memory_brand(clock)
    from autonomy.brand import SqlAssetLibrary, SqlBrandKitStore

    db = connectors.engine.store.engine
    return BrandServices(
        store=SqlBrandKitStore(db), clock=clock, assets=SqlAssetLibrary(db, connectors.business_id)
    )


def build_production(
    environ: Mapping[str, str] | None = None, *, clock: Callable[[], datetime] | None = None
) -> tuple[Services, ConnectorServices | None]:
    """The v2 services over the production runtime, and the connector services over its engine.

    The API process never ticks, syncs or sends (`run_send_jobs=False`; the worker does): it reads
    the shared database, records approvals, and makes connections that the worker then runs."""
    services, connector_services, _, _ = _production_parts(environ, clock=clock)
    return services, connector_services


def _production_parts(
    environ: Mapping[str, str] | None, *, clock: Callable[[], datetime] | None
) -> tuple[Services, ConnectorServices | None, Any, Any]:
    from autonomy.runtime.settings import (
        RuntimeSettings,
        build_connector_engine,
        build_runtime,
        engine_for,
    )
    from autonomy.runtime.stores import SqlAlertStore, create_record_tables
    from autonomy.store import SqlEventStore, create_all

    env = os.environ if environ is None else environ
    settings = RuntimeSettings.from_env(env)
    now = clock or (lambda: datetime.now(UTC))
    db = engine_for(settings.database_url)
    wiring = None
    if settings.connectors:
        create_all(db)
        create_record_tables(db)
        wiring = build_connector_engine(
            settings, db=db, events=SqlEventStore(db), alerts=SqlAlertStore(db), environ=env, holder="api"
        )
    runtime = build_runtime(settings, environ=env, engine=db, run_send_jobs=False, connectors=wiring)
    public = (settings.public_url or env.get("AUTONOMY_PUBLIC_URL") or "").strip().rstrip("/") or None
    services, connector_services = _services_for(runtime, wiring, now, public, settings.business_id)
    # `services.follow_ups_run` stays False, v3 on or off: nothing sends a brief's follow-up steps. The v3 journeys
    # come from configs/v3/journeys.yaml (JourneyRunner), not from `Brief.follow_ups`.
    return services, connector_services, wiring, db


def _services_for(
    runtime: Any, wiring: Any, clock: Callable[[], datetime], public_url: str | None, fallback_business: str
) -> tuple[Services, ConnectorServices | None]:
    from api.v2.deps import build_services_from_runtime

    business = runtime.business_id or fallback_business
    services = build_services_from_runtime(runtime, clock=clock, business_id=business)
    connector_services = None
    if wiring is not None and runtime.queue is not None:
        connector_services = ConnectorServices(
            engine=wiring.engine,
            queue=runtime.queue,
            business_id=business,
            clock=clock,
            public_url=public_url,
        )
    return services, connector_services


IDENTITY_REFRESH_SECONDS = 60.0
"""How often the API re-reads the identity links the worker makes (the API process never ticks)."""


def create_production_app(
    environ: Mapping[str, str] | None = None, *, clock: Callable[[], datetime] | None = None
) -> FastAPI:
    """The v2 API over the production runtime (see the module docstring); no in-memory stand-ins.

    Identity links are made by the worker (its linker sees the records), so this process re-reads
    them at most once a minute, on a request; otherwise its pages and proof would keep reading a
    linked Klaviyo profile as a separate customer until a restart."""
    now = clock or (lambda: datetime.now(UTC))
    services, connector_services, wiring, db = _production_parts(environ, clock=now)
    return _production_app(services, connector_services, wiring, db, now, environ)


def app_for_runtime(
    runtime: Any,
    *,
    wiring: Any,
    db: Any,
    clock: Callable[[], datetime],
    public_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """`create_production_app` over a runtime and connector wiring built elsewhere (the end-to-end
    rehearsal builds them over simulated platforms); the same app, routes and stores."""
    services, connector_services = _services_for(runtime, wiring, clock, public_url, "default")
    return _production_app(services, connector_services, wiring, db, clock, environ)


def _production_app(
    services: Services,
    connector_services: ConnectorServices | None,
    wiring: Any,
    db: Any,
    now: Callable[[], datetime],
    environ: Mapping[str, str] | None,
) -> FastAPI:
    from api.v2.inbox import production_inbox_services
    from api.v2.onboarding import REFILL_UNREACHABLE, OnboardingDesk, attach_onboarding

    app = create_app(
        services,
        connectors=connector_services,
        local_connectors=False,
        brand=_production_brand(connector_services, now),
        inbox=production_inbox_services(services, engine=db, wiring=wiring),
    )
    # The production runtime puts no refill scores on customers, so a refill plan is never preselected.
    attach_onboarding(app, OnboardingDesk.from_env(environ, unreachable=REFILL_UNREACHABLE))
    identity = getattr(wiring, "identity", None)
    if identity is not None:
        last: list[datetime] = []

        @app.middleware("http")
        async def _fresh_links(request: Request, call_next: Any) -> Any:
            at = now()
            if not last or (at - last[0]).total_seconds() >= IDENTITY_REFRESH_SECONDS:
                identity.refresh(at)
                last[:] = [at]
            return await call_next(request)

    return app


V2_TOKEN_ENV = "AUTONOMY_V2_API_TOKEN"
"""The bearer token every non-public route of the mounted v2 API asks for (see `mount_production`)."""

_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/v2/connectors/webhooks/",  # Shopify / Klaviyo webhooks: each checks its own signature
    "/v2/webhooks/",  # channel providers' delivery webhooks: verified by the adapter
    "/v2/app/",  # the static UI files (its data calls still need the token)
)


def _is_public(method: str, path: str) -> bool:
    if not (path == "/v2" or path.startswith("/v2/")):
        path = "/v2" + path  # a server that strips the mount prefix from the path
    if path == "/v2/app" or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return True
    parts = path.strip("/").split("/")
    # OAuth: the platform redirects the person's browser here, with a signed state / HMAC.
    return (
        method in ("GET", "HEAD")
        and len(parts) == 4
        and parts[:2] == ["v2", "connectors"]
        and parts[3] in ("callback", "install")
    )


class _Guard:
    """ASGI: the mounted v2 API answers only public callbacks without the operator's bearer token.

    Mounted under another app, the v2 routes are outside that app's access control, and they sign
    mandates, approve batches, connect and disconnect platforms. Platform callbacks (OAuth
    redirects, webhooks) stay open because each checks its own signature; everything else needs
    `Authorization: Bearer <AUTONOMY_V2_API_TOKEN>`, and with no token configured it is refused."""

    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = (token or "").strip() or None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and not _is_public(
            str(scope.get("method", "GET")), str(scope.get("path", ""))
        ):
            if not self._allowed(scope):
                response = JSONResponse(
                    status_code=401 if self.token else 503,
                    content={
                        "detail": (
                            "Sign in with the v2 operator token."
                            if self.token
                            else f"{V2_TOKEN_ENV} is not set, so the v2 API answers only platform callbacks."
                        )
                    },
                    headers={"WWW-Authenticate": "Bearer"} if self.token else None,
                )
                await response(scope, receive, send)
                return
        elif scope.get("type") == "websocket" and not self._allowed(scope):
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)

    def _allowed(self, scope: Any) -> bool:
        import hmac

        if self.token is None:
            return False
        headers = {bytes(k).lower(): bytes(v) for k, v in scope.get("headers") or []}
        given = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, value = given.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), self.token.encode())


class _KeepPrefix:
    """ASGI: run a mounted app on the full path (`/v2/...`), as if it were the top-level app.

    `Mount("/v2", ...)` moves `/v2` into `root_path`; the v2 app's routes are declared with the
    prefix, so it is put back before the app routes the request."""

    def __init__(self, app: Any, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            scope = dict(scope)
            root = str(scope.get("root_path") or "")
            if root.endswith(self.prefix):
                scope["root_path"] = root[: -len(self.prefix)]
        await self.app(scope, receive, send)


def mount_production(app: FastAPI, environ: Mapping[str, str] | None = None) -> FastAPI:
    """Serve the production v2 API (and the Connect screen's routes) under `/v2` of another app.

    A mounted app is outside the host app's router, so the host's global dependencies (v1's access
    control) do not reach it: Shopify's and Klaviyo's OAuth redirects and webhooks must arrive with
    no session, and each connector route checks its own signature or state. Every other route asks
    for the operator's bearer token (`AUTONOMY_V2_API_TOKEN`) and is refused when none is set: they
    sign mandates, approve batches and disconnect platforms."""
    env = os.environ if environ is None else environ
    v2 = create_production_app(env)
    app.mount("/v2", _KeepPrefix(_Guard(v2, env.get(V2_TOKEN_ENV)), "/v2"), name="autonomy-v2")
    return v2


def _json_safe(value: Any) -> Any:
    """Inputs echoed back in a 422 may hold infinity (JSON's 1e999), which JSON cannot carry."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's own 422, made safe for inputs JSON cannot encode (otherwise it raises a 500).

    Under /v2/connectors the refused input is left out: a body there may carry a pasted API key or token."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    secret = request.url.path.startswith(CONNECTORS_PREFIX)
    drop = {"ctx", "input"} if secret else {"ctx"}
    detail = [{k: _json_safe(v) for k, v in e.items() if k not in drop} for e in errors]
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
    parser.add_argument(
        "--production",
        action="store_true",
        help="serve the real runtime and connectors (AUTONOMY_* settings)",
    )
    parser.add_argument("--customers", type=int, help=f"simulated customers (default {DEMO_CUSTOMERS})")
    parser.add_argument("--days", type=int, help=f"simulated days of history (default {DEMO_DAYS})")
    args = parser.parse_args(argv)
    if args.openapi:
        export_openapi(Path(args.openapi))
        print(f"wrote {args.openapi}")
        return

    import uvicorn  # only needed to serve; the light CI environment does not install it

    if args.production:
        # The same guard as when mounted: only platform callbacks answer without the operator token.
        guarded = _Guard(create_production_app(), os.environ.get(V2_TOKEN_ENV))
        uvicorn.run(guarded, host=args.host, port=args.port)
        return

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
