"""Plan D M54: `model_decision`, `erasure_progress`, `platform_setting` and `erasure_request.history_all_clients`.

Revision ID: 0005
Revises: 0004
Created: 2026-09-23

WHY `model_decision` (DEC-862): an Approver approves or rejects a challenger with a reason, and a
promotion carries one too. The registry row has room for one name and one note, and Phase 1's
registry schema is not this plan's to widen; a decision is also an event in time (a version can be
rejected, retrained, approved), so it is a row per decision in the platform database, beside the
audit trail. `decided_by` is a user id, never a name typed into a form.

WHY `erasure_progress` (DEC-863): erasure runs as a background job, and the Admin's screen shows how
far it has got, store by store, with the attempts each store took and why one failed. One row per
(request, store); counts and codes only, never the principal - the request row already holds the
salted hash and nothing here needs more.

WHY `erasure_request.history_all_clients` (DEC-870): whether an erasure deletes the consent history
under every client or only the request's is decided when the request is made (the Admin named no
client) and cannot be inferred later from `client_id`, which then holds the deployment's id. A retry
must do what the request asked, so the choice is stored. `NOT NULL DEFAULT false`: a row written
before this revision behaves as it did.

WHY `platform_setting` (DEC-871): a key/value table of facts about the database itself; its first
key is the privacy salt's fingerprint, so a deployment that changes its salt is refused rather than
silently orphaning every stored hash. The fingerprint is a hash of the salt, never the salt.

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. The models are
`engine/approvals.py`, `engine/privacy/tables.py` and `engine/platform_db.py`; this revision creates
exactly what they declare. Reversible: `downgrade` drops what `upgrade` added, which loses the
decision reasons, the progress rows, the recorded history choice and the salt fingerprint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the Plan D tables and their indexes, and add the erasure request's history choice."""
    op.create_table(
        "model_decision",
        sa.Column("decision_id", _STR(), nullable=False),
        sa.Column("model_id", _STR(), nullable=False),
        sa.Column("use_case_id", _STR(), nullable=False),
        sa.Column("decision", _STR(), nullable=False),
        sa.Column("decided_by", _STR(), nullable=False),
        sa.Column("reason", _STR(), nullable=True),
        sa.Column("champion_id", _STR(), nullable=True),
        sa.Column("decided_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index(op.f("ix_model_decision_model_id"), "model_decision", ["model_id"], unique=False)
    op.create_table(
        "erasure_progress",
        sa.Column("request_id", _STR(), nullable=False),
        sa.Column("store", _STR(), nullable=False),
        sa.Column("status", _STR(), nullable=False),
        sa.Column("files_total", sa.Integer(), nullable=False),
        sa.Column("files_done", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", _STR(), nullable=True),
        sa.Column("updated_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("request_id", "store"),
    )
    op.create_table(
        "platform_setting",
        sa.Column("key", _STR(), nullable=False),
        sa.Column("value", _STR(), nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.add_column(
        "erasure_request",
        sa.Column("history_all_clients", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop what `upgrade` added and, with it, the decision reasons, progress rows and salt fingerprint."""
    with op.batch_alter_table("erasure_request") as batch:  # SQLite cannot drop a column in place
        batch.drop_column("history_all_clients")
    op.drop_table("platform_setting")
    op.drop_table("erasure_progress")
    op.drop_index(op.f("ix_model_decision_model_id"), table_name="model_decision")
    op.drop_table("model_decision")
