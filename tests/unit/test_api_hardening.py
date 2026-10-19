"""Security review 2, H-1 / M-1 / M-2 / L-2: what the *deployed* app (`create_app()` with no settings) does.

The served process is `uvicorn api.main:app`, built with no `Settings`, and each of these was true
only for an app that was handed settings: the redacting log formatter, the configured CORS origins
and the early refusal of a large body. So every test builds the app the way uvicorn does.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import uvicorn.config
from fastapi.testclient import TestClient

from api import hardening
from api.main import create_app
from engine.access.roles import Role
from engine.settings import Settings
from engine.utils import logging as engine_logging
from engine.utils.logging import WITHHELD, RedactingFormatter, RedactingJsonFormatter
from tests.integration.production.access_support import bearer, local_app, make_user

SECRET = "asha@example.in"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def clean_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[logging.Logger]:
    """A root logger with no handler, as a fresh process has; everything restored afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    saved = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate) for n in UVICORN_LOGGERS
    }
    monkeypatch.setattr(engine_logging, "_handler", None)
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for name, (hs, propagate) in saved.items():
            logging.getLogger(name).handlers = hs
            logging.getLogger(name).propagate = propagate


# --- H-1 -----------------------------------------------------------------------------------------
def _failed_conversion() -> BaseException:
    try:
        pd.Series([SECRET]).astype(float)
    except ValueError as exc:
        assert SECRET in str(exc)  # the library does quote the cell: that is the leak
        return exc
    raise AssertionError("the conversion should have failed")


def test_the_deployed_app_never_logs_the_value_of_a_failed_conversion(
    clean_logging: logging.Logger, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)  # uvicorn does this before importing the app
    create_app(data_dir=tmp_path)  # the deployed app: no settings
    exc = _failed_conversion()
    try:
        raise exc
    except ValueError:
        logging.getLogger("engine.jobs").exception("job %s failed", "job-1")
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=True)
    captured = capsys.readouterr()
    written = captured.out + captured.err
    assert "job job-1 failed" in written and "Exception in ASGI application" in written
    assert "ValueError" in written and WITHHELD in written
    assert SECRET not in written


def test_the_deployed_app_puts_the_redacting_formatter_on_the_root_handler(
    clean_logging: logging.Logger, tmp_path: Path
) -> None:
    create_app(data_dir=tmp_path)
    assert any(isinstance(h.formatter, RedactingFormatter) for h in clean_logging.handlers)


def test_uvicorns_own_handlers_are_replaced_by_the_root_one(
    clean_logging: logging.Logger, tmp_path: Path
) -> None:
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    assert logging.getLogger("uvicorn").handlers  # the precondition: uvicorn dressed them itself
    create_app(data_dir=tmp_path)
    for name in UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True


def test_the_log_format_comes_from_the_environment_for_the_deployed_app(
    clean_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MARKETING_AI_LOG_FORMAT", "json")
    create_app(data_dir=tmp_path)
    assert any(isinstance(h.formatter, RedactingJsonFormatter) for h in clean_logging.handlers)


# --- M-1 -----------------------------------------------------------------------------------------
def _preflight(client: TestClient, origin: str) -> Any:
    return client.options("/healthz", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})


def test_the_configured_cors_origins_are_honoured_by_the_deployed_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MARKETING_AI_CORS_ORIGINS", "https://app.client.example")
    client = TestClient(create_app(data_dir=tmp_path))
    allowed = _preflight(client, "https://app.client.example")
    assert allowed.headers["access-control-allow-origin"] == "https://app.client.example"
    refused = _preflight(client, "https://evil.example")
    assert "access-control-allow-origin" not in refused.headers
    simple = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_a_local_deployment_still_answers_any_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MARKETING_AI_CORS_ORIGINS", raising=False)
    client = TestClient(create_app(data_dir=tmp_path))
    assert client.get("/healthz", headers={"Origin": "null"}).headers["access-control-allow-origin"] == "*"


def test_cors_origins_given_to_create_app_are_honoured(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path, settings=Settings(cors_origins=("https://a.example",)))
    client = TestClient(app)
    assert (
        _preflight(client, "https://a.example").headers["access-control-allow-origin"] == "https://a.example"
    )
    assert "access-control-allow-origin" not in _preflight(client, "https://b.example").headers


# --- M-2 -----------------------------------------------------------------------------------------
class _Driver:
    """Call an ASGI app directly and count the request-body bytes it was handed."""

    def __init__(self, app: Any, path: str, headers: dict[str, str]) -> None:
        self.app, self.path, self.headers = app, path, headers
        self.consumed = 0
        self.status = 0
        self.body = b""

    def run(self, chunks: list[bytes], *, content_length: int | None) -> _Driver:
        headers = {k.lower(): v for k, v in self.headers.items()}
        if content_length is not None:
            headers["content-length"] = str(content_length)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("203.0.113.9", 5000),
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
        queue = list(chunks)

        async def receive() -> dict[str, Any]:
            if queue:
                chunk = queue.pop(0)
                self.consumed += len(chunk)
                return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                self.body += message.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        return self


MB = 1024 * 1024
JSON = {"content-type": "application/json"}
MULTIPART = {"content-type": "multipart/form-data; boundary=xyz"}


@pytest.fixture
def signed_in(tmp_path: Path) -> tuple[Any, dict[str, str]]:
    app = local_app(tmp_path)
    return app, bearer(app, make_user(app, "analyst", [Role.ANALYST]))


@pytest.mark.parametrize(("path", "headers"), [("/runs", JSON), ("/uploads", MULTIPART)])
def test_an_anonymous_large_body_is_refused_before_it_is_read(
    signed_in: tuple[Any, dict[str, str]], path: str, headers: dict[str, str]
) -> None:
    app, _ = signed_in
    driver = _Driver(app, path, headers).run([b"x" * MB] * 20, content_length=20 * MB)
    assert driver.status == 401 and b"AUTH_REQUIRED" in driver.body
    assert driver.consumed == 0


def test_a_garbage_token_is_no_better_than_none(signed_in: tuple[Any, dict[str, str]]) -> None:
    app, _ = signed_in
    headers = {**JSON, "Authorization": "Bearer not-a-session"}
    driver = _Driver(app, "/runs", headers).run([b"x" * MB] * 20, content_length=20 * MB)
    assert driver.status == 401 and driver.consumed == 0


def test_an_anonymous_chunked_body_is_cut_off_once_it_outgrows_the_small_allowance(
    signed_in: tuple[Any, dict[str, str]],
) -> None:
    app, _ = signed_in
    chunk = 256 * 1024
    driver = _Driver(app, "/runs", JSON).run([b"x" * chunk] * 400, content_length=None)  # 100 MB
    assert driver.status == 401
    assert driver.consumed <= hardening.SMALL_BODY_BYTES + chunk


def test_a_signed_in_body_over_the_ceiling_is_refused_unread(signed_in: tuple[Any, dict[str, str]]) -> None:
    app, auth = signed_in
    driver = _Driver(app, "/runs", {**JSON, **auth}).run([], content_length=hardening.MAX_JSON_BYTES + 1)
    assert driver.status == 413 and b"REQUEST_TOO_LARGE" in driver.body and driver.consumed == 0


def test_a_signed_in_chunked_body_over_the_ceiling_is_cut_off(
    signed_in: tuple[Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, auth = signed_in
    monkeypatch.setattr(hardening, "MAX_JSON_BYTES", 3 * MB)
    driver = _Driver(app, "/runs", {**JSON, **auth}).run([b"x" * MB] * 10, content_length=None)
    assert driver.status == 413
    assert driver.consumed <= 4 * MB


def test_a_signed_in_two_megabyte_body_still_reaches_the_route(signed_in: tuple[Any, dict[str, str]]) -> None:
    app, auth = signed_in
    body = b'{"pad": "' + b"a" * (2 * MB) + b'"}'
    driver = _Driver(app, "/runs", {**JSON, **auth}).run([body], content_length=len(body))
    assert driver.status not in {401, 413}
    assert driver.consumed == len(body)


def test_a_small_anonymous_body_is_unchanged(signed_in: tuple[Any, dict[str, str]]) -> None:
    app, _ = signed_in
    client = TestClient(app)
    response = client.post("/runs", content=b"{}", headers=JSON)
    assert response.status_code == 401 and response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_with_sign_in_off_on_a_laptop_a_large_body_is_not_refused(tmp_path: Path) -> None:
    app = local_app(tmp_path, auth_mode="off")
    body = b'{"pad": "' + b"a" * (2 * MB) + b'"}'
    driver = _Driver(app, "/runs", JSON).run([body], content_length=len(body))
    assert driver.status not in {401, 413}


# --- L-2 -----------------------------------------------------------------------------------------
def test_every_response_carries_the_basic_security_headers(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    for response in (
        client.get("/healthz"),
        client.get("/industries"),
        client.get("/nope"),
        client.post("/runs"),
    ):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert "strict-transport-security" not in response.headers  # not on a laptop


def test_a_refusal_before_the_body_is_read_has_the_headers_too(signed_in: tuple[Any, dict[str, str]]) -> None:
    app, _ = signed_in
    client = TestClient(app)
    response = client.post("/uploads", content=b"x" * (2 * MB), headers=MULTIPART)
    assert response.status_code == 401
    assert response.headers["x-frame-options"] == "DENY"


def test_the_ui_still_loads_and_hsts_is_sent_on_prod(tmp_path: Path) -> None:
    settings = Settings(
        env="prod", cors_origins=("https://pilot.example",), auth_mode="local", data_dir=tmp_path
    )
    app = create_app(data_dir=tmp_path)
    app.state.settings = settings  # as the access tests do: a prod app also demands a privacy salt
    client = TestClient(app)
    ui = client.get("/ui/")
    assert ui.status_code == 200 and "<title>" in ui.text
    # Only frame-ancestors: a script-src / style-src would blank the screens (inline <style>, Google Fonts).
    assert ui.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert client.get("/ui/app.js").status_code == 200
    assert ui.headers["strict-transport-security"] == "max-age=31536000"
