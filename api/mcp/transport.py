"""How a client reaches the verifier: the `McpTransport` protocol and two in-process implementations (Plan K
M133, DEC-1404 (b)).

Every external call sits behind a protocol with a fake (Plan K's rule), and the fast suite needs no network:

* `InProcessTransport` - the SDK's own client connected to the verifier's `Server` in the same process (no
  HTTP, no JSON-RPC framing), as one fixed caller. For unit tests of the surface and the call pipeline.
* `AsgiTransport` - the SDK's streamable HTTP client against the **real app** (`api.main.create_app`, `/mcp`
  mounted, sign-in on) through `httpx2.ASGITransport`, with the app's lifespan running in the same event loop.
  Every byte goes through the endpoint, the sign-in, the session manager and the transport a deployed client
  meets, without a socket.

A real client connects over the network with its own MCP client (`docs/AGENTIC.md` §8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI

    from api.mcp.scopes import McpCaller
    from api.mcp.server import Verifier
    from mcp import Client

__all__ = ["AsgiTransport", "InProcessTransport", "McpTransport"]


class McpTransport(Protocol):
    """Connects one MCP client to the verifier."""

    def connect(self) -> AbstractAsyncContextManager[Client]:
        """A connected `mcp.Client`, closed when the context ends."""
        ...


@dataclass(frozen=True)
class InProcessTransport:
    """The SDK client against the verifier's low-level `Server`, in process, as `caller`."""

    verifier: Verifier
    caller: McpCaller

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Client]:
        from api.mcp.server import build_server
        from mcp import Client

        caller = self.caller
        server = build_server(self.verifier, lambda _ctx: caller)
        async with Client(server) as client:
            yield client

    def connect(self) -> AbstractAsyncContextManager[Client]:
        return self._connect()


@dataclass(frozen=True)
class AsgiTransport:
    """The SDK's streamable HTTP client against the app's `/mcp`, through an in-process ASGI transport.

    `token` is the bearer session sent with every request (None sends none). `client_name` is what the client
    declares about itself (`clientInfo.name`). The app's lifespan is entered for the connection, which starts
    the MCP session manager; one app serves one connection at a time.
    """

    app: FastAPI
    token: str | None
    client_name: str = "scripted-client"
    mode: str = "auto"
    """`auto` (2026-07-28 when the server offers it) or `legacy` (the 2025 initialize handshake)."""
    base_url: str = "http://testserver"

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Client]:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        from api.mcp.http import MCP_PATH
        from mcp import Client, Implementation

        headers = {} if self.token is None else {"Authorization": f"Bearer {self.token}"}
        lifespan: Any = self.app.router.lifespan_context(self.app)
        async with lifespan:
            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.app), base_url=self.base_url, headers=headers
            )
            async with http:
                transport = streamable_http_client(f"{self.base_url}{MCP_PATH}", http_client=http)
                info = Implementation(name=self.client_name, version="1")
                async with Client(transport, client_info=info, mode=self.mode) as client:
                    yield client

    def connect(self) -> AbstractAsyncContextManager[Client]:
        return self._connect()
