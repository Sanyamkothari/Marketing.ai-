"""The real UI served with a replayed API, for browser checks that need no seeded store (Plan M, M181).

`tests/fixtures/ui_demo/api.json` holds the answers the app gave on the validated Hillstrom demo
(`tests/fixtures/capture_ui_demo.py`). This module serves the files of `ui/` at `/ui/` and answers every
recorded `GET` from that file, on a free local port in a background thread. A request it has no answer for gets the
404 the real API gives for a thing that does not exist; nothing but `GET` is answered (a page draws itself from
reads). It is what the keyboard path check and the screenshot set (`scripts/ui_screenshots.py`) open in Chromium, so
both work on a plain checkout, offline.

`ui_layout="campaigns"` makes `GET /auth/me` carry `ui_layout: "campaigns"`, which is how the application switches to
the campaigns layout (`ui/layout.js`, Plan M M174); the default leaves the recorded answer as it is (the classic
layout). Against a checkout without `ui/layout.js` the field changes nothing.

    with ReplayServer() as server:
        page.goto(f"{server.base}/ui/#/")
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

__all__ = ["RECORDED", "UI_DIR", "ReplayServer", "has_campaigns_layout", "key_of"]

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
UI_DIR: Final[Path] = REPO_ROOT / "ui"
RECORDED: Final[Path] = Path(__file__).resolve().parent / "ui_demo" / "api.json"

HOST: Final[str] = "127.0.0.1"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


def has_campaigns_layout() -> bool:
    """Whether this checkout has the campaigns layout's shell (`ui/layout.js`, Plan M M174)."""
    return (UI_DIR / "layout.js").is_file()


def key_of(method: str, url: str) -> str:
    """`GET /path?a=1&b=2` with the query sorted, the key `capture_ui_demo.py` recorded the answer under."""
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return f"{method.upper()} {parsed.path}" + (f"?{query}" if query else "")


class ReplayServer:
    """Serve `ui/` and the recorded API on `HOST`, a free port, until the `with` block ends."""

    def __init__(self, *, ui_layout: str = "classic") -> None:
        if ui_layout not in {"classic", "campaigns"}:
            raise ValueError(f"ui_layout must be 'classic' or 'campaigns', not {ui_layout!r}")
        self.ui_layout = ui_layout
        self.recorded: dict[str, Any] = json.loads(RECORDED.read_text(encoding="utf-8"))
        self.requests: list[str] = []
        self.unrecorded: set[str] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base(self) -> str:
        assert self._server is not None, "the server is not running"
        return f"http://{HOST}:{self._server.server_address[1]}"

    def __enter__(self) -> ReplayServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_GET(self) -> None:
                owner.requests.append(self.path)
                path = urlparse(self.path).path
                if path == "/":
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/ui/")
                    self.end_headers()
                    return
                if path.startswith("/ui"):
                    self._file(path[len("/ui") :])
                    return
                key = key_of("GET", self.path)
                hit = owner.recorded.get(key)
                if hit is not None and path == "/auth/me" and owner.ui_layout == "campaigns":
                    body = {**hit["body"], "ui_layout": "campaigns"}
                    self._json(hit["status"], body)
                    return
                if hit is None:
                    owner.unrecorded.add(key)
                    self._json(404, {"detail": {"code": "NOT_RECORDED", "message": "Not found."}})
                    return
                payload = hit["body"]
                content_type = hit.get("content_type") or "application/json"
                if isinstance(payload, str):
                    self._send(hit["status"], payload.encode("utf-8"), content_type)
                else:
                    self._send(hit["status"], json.dumps(payload).encode("utf-8"), content_type)

            def _file(self, relative: str) -> None:
                name = unquote(relative).lstrip("/") or "index.html"
                target = (UI_DIR / name).resolve()
                if UI_DIR.resolve() not in target.parents and target != UI_DIR.resolve():
                    self._json(404, {"detail": {"code": "NOT_FOUND", "message": "Not found."}})
                    return
                if target.is_dir():
                    target = target / "index.html"
                if not target.is_file():
                    self._json(404, {"detail": {"code": "NOT_FOUND", "message": "Not found."}})
                    return
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), content_type)

            def do_POST(self) -> None:
                self._json(404, {"detail": {"code": "NOT_REPLAYED", "message": "Writes are not replayed."}})

            do_PUT = do_PATCH = do_DELETE = do_POST  # noqa: N815

        self._server = ThreadingHTTPServer((HOST, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
