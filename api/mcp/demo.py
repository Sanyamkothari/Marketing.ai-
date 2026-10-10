"""The demo-only guard: M133 serves the demo library's artefacts and nothing else (Plan K M133, DEC-1404 (d)).

Until M141 adds per-client scoping, the MCP server answers about **demo data only**:

* the workspace must be `demo` (`mcp.workspace`; a client may also name the workspace it means in the request's
  `_meta`, `marketing-ai/workspace`): any other is refused;
* every run a tool is asked about must be demo data: made for one of `mcp.demo_use_cases` (the public datasets
  of `library/`, e.g. Hillstrom) or from generated data (`run.json`'s `synthetic`), and never an onboarded
  client's run (`client_id` set);
* every campaign must rest on demo data only: a scored campaign on its runs, an audited one on the uploads it
  read (the assignment and the outcomes file, each made for a demo use case or marked synthetic).

Anything else is refused with **`MCP_DEMO_ONLY`** (403), and so is an id that names nothing: the answer is the
same for "not demo" and "not there", so a caller cannot use the guard to find out which ids exist, and the
message never repeats the id it was given. A stored file that does not read counts as not demo (fail closed).

The guard reads the parsed input, so it sees exactly the ids the tool will read. `GUARDED_FIELDS` names every
input field that points at an artefact; `tests/unit/mcp/test_demo_guard.py` walks every exposed tool's input
model so a new id field cannot reach a tool unguarded.

**Small campaigns.** A campaign with an arm of 1 to 9 customers is refused for the read tools with
`MCP_GROUP_TOO_SMALL`: its figures are a small group's (the Proof Pack prints them as text, which the
suppression of `api.mcp.suppress` cannot reach).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from engine.agentic.errors import AgenticError

if TYPE_CHECKING:
    from api.mcp.config import McpSettings
    from engine.agentic.registry import ToolContext
    from engine.storage import Storage

__all__ = [
    "GUARDED_FIELDS",
    "NOT_ARTEFACT_FIELDS",
    "WORKSPACE_META_KEY",
    "check_workspace",
    "guard",
    "is_demo_run",
    "is_demo_upload",
]

WORKSPACE_META_KEY: Final[str] = "marketing-ai/workspace"

NOT_ARTEFACT_FIELDS: Final[frozenset[str]] = frozenset({"evidence_ids"})
"""Id-shaped input fields that name no stored artefact: evidence ids are ledger entries, which the proposal
path checks itself (each must already be on the ledger)."""


def _refuse(what: str) -> AgenticError:
    return AgenticError(
        "MCP_DEMO_ONLY",
        f"This server answers about the demo library only (Hillstrom and generated data) until client data is "
        f"connected; the {what} asked about is not part of it.",
        status=403,
    )


def check_workspace(settings: McpSettings, meta: Mapping[str, Any] | None) -> None:
    """Refuse a deployment whose workspace is not `demo`, and a request that names another workspace."""
    from api.mcp.config import DEMO_WORKSPACE

    asked = None if meta is None else meta.get(WORKSPACE_META_KEY)
    if settings.workspace != DEMO_WORKSPACE or (asked is not None and asked != DEMO_WORKSPACE):
        raise _refuse("workspace")


def _record(storage: Storage, key: str) -> dict[str, Any] | None:
    from engine.agentic.artefacts import read_stored_json

    try:
        return read_stored_json(storage, key)
    except AgenticError:  # stored but unreadable: not demo, fail closed
        return None


def is_demo_run(storage: Storage, run_id: str, settings: McpSettings) -> bool:
    """A run made for a demo use case or from generated data, and not an onboarded client's."""
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key

    record = _record(storage, run_key(run_id, RUN_FILENAME))
    if record is None or record.get("client_id") is not None:
        return False
    return record.get("synthetic") is True or record.get("use_case_id") in settings.demo_use_cases


def is_demo_upload(storage: Storage, upload_id: str, settings: McpSettings) -> bool:
    """An upload made for a demo use case or marked synthetic."""
    from engine.storage import upload_key

    record = _record(storage, upload_key(upload_id, "upload.json"))
    if record is None:
        return False
    return record.get("synthetic") is True or record.get("use_case_id") in settings.demo_use_cases


def _safe_id(value: object) -> str | None:
    from engine.agentic.tools import ID_PATTERN

    return value if isinstance(value, str) and re.fullmatch(ID_PATTERN, value) else None


def _campaign_refs(storage: Storage, campaign: Any) -> tuple[list[str], list[str]] | None:
    """`(run ids, upload ids)` the campaign rests on; None when a reference is not an id (fail closed)."""
    from engine.measurement.audit import AUDIT_FILENAME
    from engine.measurement.campaign import CampaignKind, campaign_key

    runs = [_safe_id(run_id) for run_id in campaign.run_ids]
    uploads: list[str | None] = []
    if campaign.outcomes is not None:
        uploads.append(_safe_id(campaign.outcomes.upload_id))
    audit = _record(storage, campaign_key(campaign.campaign_id, AUDIT_FILENAME))
    if audit is not None:
        uploads.append(_safe_id(audit.get("assignment_upload_id")))
    elif campaign.kind is not CampaignKind.SCORED:
        return None  # an outside campaign without its audit record: nothing says what it read
    if any(ref is None for ref in (*runs, *uploads)):
        return None
    return [ref for ref in runs if ref], [ref for ref in uploads if ref]


def _small(count: int, minimum: int) -> bool:
    return 0 < count < minimum


def _check_campaign(ctx: ToolContext, campaign_id: str, settings: McpSettings, *, reads: bool) -> None:
    campaign = None if ctx.campaigns is None else ctx.campaigns.get(campaign_id)
    if campaign is None:
        raise _refuse("campaign")
    refs = _campaign_refs(ctx.storage, campaign)
    if refs is None:
        raise _refuse("campaign")
    runs, uploads = refs
    if not runs and not uploads:
        raise _refuse("campaign")
    if not all(is_demo_run(ctx.storage, run_id, settings) for run_id in runs):
        raise _refuse("campaign")
    if not all(is_demo_upload(ctx.storage, upload_id, settings) for upload_id in uploads):
        raise _refuse("campaign")
    counts = campaign.counts
    arms = (counts.treated, counts.holdout, counts.intended_treated, counts.intended_holdout)
    if reads and any(_small(count, settings.min_group_rows) for count in arms):
        raise AgenticError(
            "MCP_GROUP_TOO_SMALL",
            f"An arm of this campaign holds fewer than {settings.min_group_rows} customers, so its figures "
            "would describe a group that small; they are not served over MCP.",
            status=403,
        )


def _check_run(ctx: ToolContext, run_id: str, settings: McpSettings, *, reads: bool) -> None:
    del reads
    if not is_demo_run(ctx.storage, run_id, settings):
        raise _refuse("run")


GUARDED_FIELDS: Final[dict[str, Callable[..., None]]] = {
    "run_id": _check_run,
    "campaign_id": _check_campaign,
}
"""Every input field that names a stored artefact, and the check it gets."""


def guard(parsed: Any, ctx: ToolContext, settings: McpSettings, *, reads: bool) -> None:
    """Refuse unless every artefact the parsed input names is demo data (see the module docstring)."""
    for field, check in GUARDED_FIELDS.items():
        if field in type(parsed).model_fields:
            value = getattr(parsed, field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise _refuse(field.removesuffix("_id"))
            check(ctx, value, settings, reads=reads)
