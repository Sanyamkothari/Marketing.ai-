"""Client workspaces: the verifier on client data, scoped per client (Plan K M141, DEC-1408 (a), (b); design §7.2).

On a `mcp.workspace: clients` server (`configs/agentic/mcp.yaml`) M133's demo-only guard is lifted and this guard
takes its place. Every signed-in person is bound to at most one workspace (`mcp.workspaces[].members`), so every
bearer token reaches exactly one client's data, and one deployment can serve several clients:

* **Sign-in.** A person bound to no workspace is refused before any tool runs (`MCP_WORKSPACE_DENIED`, 403).
  A request that names a workspace (`_meta` `marketing-ai/workspace`) must name the caller's own.
* **What belongs to a workspace.** A run belongs to the workspace of the client its `run.json` records
  (`client_id`, set on every run made from an onboarded client's dataset); a run with no client recorded (made
  from an upload) belongs to the workspace that claims the unattributed data (`unattributed: true`), if one does.
  A campaign belongs where its scoring runs belong - all of them, to one workspace (as `engine.measurement.cycle`
  matches a campaign to a client) - and a campaign with no scoring run (an audited outside campaign, the
  programme) to the workspace that claims the unattributed data: the product records no client on an upload.
* Anything else - another client's run or campaign, an id that names nothing, a stored record that does not
  read - is refused with **`MCP_WORKSPACE_DENIED`** (403). The answer is the same for "not yours" and "not
  there", so a caller cannot use the guard to learn which ids exist, and the message never repeats the id.
* **Small groups.** A campaign with a group of 1 to 9 customers is refused for the read tools with
  `MCP_GROUP_TOO_SMALL`, exactly as on the demo server (`api.mcp.demo.refuse_small_campaign`), and every grouped
  output is suppressed (`api.mcp.suppress`).

**Aggregates only.** No tool reads or returns a row: the registry's tools return counts, rates, intervals and
the product's own sentences (`engine.agentic.tools`), and the audited row-level downloads stay HTTP routes for a
person (`GET /runs/{id}/artefacts/{name}`, audited), never an MCP tool. A Viewer's token reads aggregates; only an
Analyst's drafts a proposal; no token approves.

The guard reads the parsed input, so it sees exactly the ids the tool will read; it checks the same fields as the
demo guard (`api.mcp.demo.GUARDED_FIELDS`, walked by `tests/unit/mcp/test_demo_guard.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from engine.agentic.errors import AgenticError

if TYPE_CHECKING:
    from api.mcp.config import ClientWorkspace, McpSettings
    from engine.agentic.registry import ToolContext
    from engine.storage import Storage

__all__ = [
    "WORKSPACE_GUARDED_FIELDS",
    "check_named",
    "denied",
    "guard",
    "run_belongs",
    "workspace_for",
]


def denied(what: str = "data") -> AgenticError:
    """`MCP_WORKSPACE_DENIED`: the same words for "not yours" and "not there"."""
    return AgenticError(
        "MCP_WORKSPACE_DENIED",
        f"This sign-in reaches one client workspace only, and the {what} asked about is not in it.",
        status=403,
    )


def workspace_for(settings: McpSettings, user_id: str) -> ClientWorkspace:
    """The workspace a signed-in person is bound to; `MCP_WORKSPACE_DENIED` when they are bound to none."""
    found = settings.workspace_of(user_id)
    if found is None:
        raise AgenticError(
            "MCP_WORKSPACE_DENIED",
            "This sign-in is bound to no client workspace on this server; an Admin adds people to a workspace "
            "in configs/agentic/mcp.yaml.",
            status=403,
        )
    return found


def check_named(workspace: ClientWorkspace, meta: Mapping[str, Any] | None) -> None:
    """Refuse a request that names a workspace other than the caller's."""
    from api.mcp.demo import WORKSPACE_META_KEY

    asked = None if meta is None else meta.get(WORKSPACE_META_KEY)
    if asked is not None and asked != workspace.id:
        raise denied("workspace")


def _record(storage: Storage, key: str) -> dict[str, Any] | None:
    from engine.agentic.artefacts import read_stored_json

    try:
        return read_stored_json(storage, key)
    except AgenticError:  # stored but unreadable: not anyone's, fail closed
        return None


def _holds(workspace: ClientWorkspace, client_id: Any) -> bool:
    """Whether data recorded under `client_id` (None: no client recorded) is this workspace's."""
    if client_id is None:
        return workspace.unattributed
    return isinstance(client_id, str) and client_id == workspace.client_id


def run_belongs(storage: Storage, run_id: str, workspace: ClientWorkspace) -> bool:
    """Whether the run is this workspace's: by the client its `run.json` records (see the module docstring)."""
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key

    record = _record(storage, run_key(run_id, RUN_FILENAME))
    return record is not None and _holds(workspace, record.get("client_id"))


def _check_run(
    ctx: ToolContext, run_id: str, workspace: ClientWorkspace, settings: McpSettings, *, reads: bool
) -> None:
    del settings, reads
    if not run_belongs(ctx.storage, run_id, workspace):
        raise denied("run")


def _check_campaign(
    ctx: ToolContext, campaign_id: str, workspace: ClientWorkspace, settings: McpSettings, *, reads: bool
) -> None:
    from api.mcp.demo import _safe_id, refuse_small_campaign

    campaign = None if ctx.campaigns is None else ctx.campaigns.get(campaign_id)
    if campaign is None:
        raise denied("campaign")
    runs = [_safe_id(run_id) for run_id in campaign.run_ids]
    if any(run is None for run in runs):
        raise denied("campaign")
    if runs:
        if not all(run_belongs(ctx.storage, str(run), workspace) for run in runs):
            raise denied("campaign")
    elif not workspace.unattributed:
        raise denied("campaign")
    if reads:
        refuse_small_campaign(ctx.storage, campaign, settings.min_group_rows)


WORKSPACE_GUARDED_FIELDS: Final[dict[str, Callable[..., None]]] = {
    "run_id": _check_run,
    "campaign_id": _check_campaign,
}
"""The same fields as the demo guard's (`api.mcp.demo.GUARDED_FIELDS`); a unit test holds the two equal."""


def guard(
    parsed: Any, ctx: ToolContext, workspace: ClientWorkspace, settings: McpSettings, *, reads: bool
) -> None:
    """Refuse unless every artefact the parsed input names is in the caller's workspace."""
    for field, check in WORKSPACE_GUARDED_FIELDS.items():
        if field in type(parsed).model_fields:
            value = getattr(parsed, field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise denied(field.removesuffix("_id"))
            check(ctx, value, workspace, settings, reads=reads)
