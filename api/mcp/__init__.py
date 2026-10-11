"""The verifier over MCP: other companies' agents may look and propose, on the demo library only (Plan K M133).

DEC-1404; `docs/AGENTIC.md` §8; design §7. An MCP server that exposes exactly the agent-safe core's `read` and
`propose` tools (`engine.agentic.tools`) - never one that approves, sends or writes - over streamable HTTP
(stateless), behind sign-in, with scopes mapped from the product's roles, every call audited, small groups
suppressed, every result schema-validated and gated for third-party text, and a per-session rate limit.

| Module | Does |
|---|---|
| `config` | the switch (`mcp.enabled` in `configs/agentic/agentic.yaml`, default off) and `configs/agentic/mcp.yaml`, the server's settings and limits. The only module `api.main` imports while the switch is off; it does not import the MCP SDK. |
| `scopes` | `marketing-ai:read` (Viewer) and `marketing-ai:propose` (Analyst), the caller, the scope a tool needs. |
| `demo` | The guard: only the demo library's runs and campaigns are served (`MCP_DEMO_ONLY`). |
| `suppress` | Small-group suppression on every grouped output (minimum 10, Plan I's `rate_by` rule). |
| `gate` | No third-party text in a result: arguments are never echoed, instruction-shaped text and personal data are withheld. |
| `limits` | Per-session calls per minute and calls at once (`MCP_RATE_LIMITED`). |
| `render` | The short text beside each structured result, written by our code from numbers only. |
| `server` | `Verifier`: the tool list and the call pipeline, and the SDK's low-level `Server` over it. |
| `http` | The `/mcp` endpoint: sign-in, the stateless streamable HTTP transport, the hook `api.main` calls. |
| `transport` | `McpTransport`: how a client reaches the verifier - in process over the SDK, or streamable HTTP over an in-process ASGI transport. The fast suite needs no network. |
"""
