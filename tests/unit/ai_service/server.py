"""A local HTTP server that plays an AI service, for tests that must see real bytes on a real socket.

`FakeService(handler)` listens on 127.0.0.1 on a free port; `handler(request) -> Reply` decides the
answer, and every request is kept in `requests` (method, path, headers, parsed JSON body) so a test can
assert on exactly what was sent - the key in its header, and nowhere else.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = ["FakeService", "Reply", "Sent", "serve"]


@dataclass
class Sent:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    raw: bytes = b""


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None


Handler = Callable[[Sent], Reply]


class FakeService:
    def __init__(self, handler: Handler) -> None:
        self.requests: list[Sent] = []
        outer = self

        class _Handle(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw) if raw else None
                except ValueError:
                    parsed = None
                sent = Sent(
                    method=self.command,
                    path=self.path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=parsed,
                    raw=raw,
                )
                outer.requests.append(sent)
                reply = handler(sent)
                payload = reply.raw if reply.raw is not None else json.dumps(reply.body).encode()
                self.send_response(reply.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _serve  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base signature
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handle)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeService:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def serve(handler: Handler) -> Iterator[FakeService]:
    service = FakeService(handler).start()
    try:
        yield service
    finally:
        service.stop()


def chat_reply(text: str = "ready", *, prompt_tokens: int = 5, completion_tokens: int = 1) -> Reply:
    return Reply(
        body={
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }
    )


def message_reply(text: str = "ready") -> Reply:
    return Reply(
        body={
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 2},
        }
    )
