"""The audit event and the protocol every audit log implements (Phase 4b M47).

An audit event records *that* something happened - who, when, which action, on which object, and
hashes of the object before and after - and never *what the data said*. There is no free-text field
a caller could pour a customer value into: `object_id` is an identifier the API minted (a run id, a
model id, a schedule id, an erasure request id) and `details` is a mapping of short tokens whose
keys come from a closed allow-list (`DETAIL_KEYS`). A data principal's id is personal data and is
therefore never an `object_id`; erasure and access requests are recorded under the request's own id
and the principal appears only as `principal_hash` in `details` (DEC-705).

`append` is the only write. There is no update and no delete on the protocol, so "append-only" is a
property of the type rather than a promise of each implementation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DETAIL_KEYS",
    "AuditEvent",
    "AuditLog",
    "AuditQuery",
    "content_hash",
    "principal_hash",
]

DETAIL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "method",
        "route",
        "status_code",
        "outcome",
        "use_case_id",
        "client_id",
        "run_id",
        "model_id",
        "schedule_id",
        "request_kind",
        "principal_hash",
        "purpose",
        "role",
        "roles",
        "target_user_id",
        "setting",
        "count",
        "deleted",
        "tombstoned",
        "dry_run",
        "stores",
        "models_flagged",
        "reason_code",
        "export_key",
        "trigger",
        # M49 (DEC-769): a scheduled firing's own ids, so its one audit event can be joined to the
        # firing history and the dataset it built without a free-text field.
        "firing_id",
        "schedule_kind",
        "dataset_id",
        # DEC-724: the X-Request-ID an anonymous caller sent, kept beside the id the server minted.
        "client_request_id",
        # DEC-726: an audit export's window and the chain position it covered.
        "window_start",
        "window_end",
    }
)
"""The only keys `AuditEvent.details` accepts. A new key is a reviewed change to this set, never a
side effect of a caller passing a dictionary it built from a request (DEC-705)."""

_MAX_DETAIL_CHARS: Final[int] = 200
"""A detail value longer than this is refused: identifiers and counts are short, sentences are not."""


def content_hash(payload: bytes | str | Mapping[str, Any] | BaseModel | None) -> str | None:
    """SHA-256 of an object's canonical form, or None when there was no object.

    A model is hashed through its JSON dump with sorted keys, so two processes hashing the same
    record agree. The hash lets an auditor prove *that* an object changed, and match it against an
    exported copy, without the audit log ever holding the object.
    """
    if payload is None:
        return None
    if isinstance(payload, BaseModel):
        raw = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    elif isinstance(payload, Mapping):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    elif isinstance(payload, str):
        raw = payload.encode()
    else:
        raw = payload
    return hashlib.sha256(raw).hexdigest()


def principal_hash(principal_id: str, *, salt: str) -> str:
    """A data principal's id as the audit log may hold it: salted SHA-256, never the id itself.

    The salt is per deployment (the client id, or a configured value) so the same customer id at two
    clients does not produce a joinable value. Anyone holding the id can recompute the hash to find
    their request; nobody holding the log can recover the id (DEC-705).
    """
    return hashlib.sha256(f"{salt}\x00{principal_id}".encode()).hexdigest()


class AuditEvent(BaseModel):
    """One thing that happened. Frozen: an event is never edited after it is written."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(description="Unique id of this event.")
    occurred_at: datetime = Field(description="When it happened, UTC.")
    actor_id: str = Field(description="`Principal.user_id` of whoever did it.")
    actor_kind: str = Field(description="`user`, `local_operator` or `system`.")
    action: str = Field(description="Dotted action name, e.g. `models.approve`, `auth.login`.")
    object_type: str | None = Field(default=None, description="What kind of thing was acted on.")
    object_id: str | None = Field(default=None, description="Its API identifier - never a data value.")
    before_hash: str | None = Field(default=None, description="`content_hash` of the object before.")
    after_hash: str | None = Field(default=None, description="`content_hash` of the object after.")
    request_id: str | None = Field(default=None, description="Correlates the event with one API request.")
    outcome: str = Field(default="success", description="`success`, `denied` or `failed`.")
    details: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, description="Short tokens under `DETAIL_KEYS` only."
    )

    @field_validator("details")
    @classmethod
    def _closed_details(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        unknown = sorted(set(value) - DETAIL_KEYS)
        if unknown:
            raise ValueError(f"audit details may not carry {unknown}; see DETAIL_KEYS")
        for key, item in value.items():
            if isinstance(item, str) and len(item) > _MAX_DETAIL_CHARS:
                raise ValueError(f"audit detail {key!r} is longer than {_MAX_DETAIL_CHARS} characters")
        return value


class AuditQuery(BaseModel):
    """Filters the audit viewer applies. Every field is optional; all given ones must match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str | None = None
    action: str | None = Field(default=None, description="Exact action, or a prefix ending in `.`.")
    object_type: str | None = None
    object_id: str | None = None
    outcome: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=200, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)


@runtime_checkable
class AuditLog(Protocol):
    """Where audit events go. Append and read; there is deliberately no way to change or remove one."""

    def append(self, event: AuditEvent) -> None: ...

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]: ...

    def count(self, query: AuditQuery) -> int: ...
