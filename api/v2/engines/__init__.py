"""The six Autonomy engines' public HTTP APIs, all under `/v2/engines/...`.

Each module here is one engine's API and exposes an `APIRouter` named `router` with its own
sub-prefix (`/decisions`, `/doctor`, ...). `api.v2.engines.router` gathers them into one router
with the `/v2/engines` prefix, which the integrator mounts. The engines are packages in one app,
not separate services, but each endpoint takes its inputs in the request, so each engine can be
used (and sold) on its own. See `docs/v2/ENGINES.md`.

This package stays import-light on purpose: importing one engine's module must not pull in the
others, so nothing is imported here.
"""
