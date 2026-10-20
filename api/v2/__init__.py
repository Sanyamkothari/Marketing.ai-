"""The v2 HTTP API: what the marketer app (ui/v2) talks to.

- `api.v2.deps`: the `Services` container the routes read, the operations behind each endpoint, and
  `build_demo_services()`, which wires the in-memory fakes so every endpoint answers with no
  external services.
- `api.v2.routes`: the `/v2` router. The UI codes against exactly these endpoints.
- `api.v2.app`: `create_app()`, and `python -m api.v2.app` to run the demo on port 8010.

This package imports nothing heavy on purpose: `deps` needs only pydantic and starlette, so its
logic can be tested without FastAPI installed.
"""
