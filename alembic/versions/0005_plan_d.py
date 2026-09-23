"""Plan D M54: `model_decision` and `erasure_progress`.

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

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. The models are
`engine/approvals.py` and `engine/privacy/tables.py`; this revision creates exactly what they
declare. Reversible: `downgrade` drops both, which loses the decision reasons and the progress rows.
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
    """Create the two Plan D tables and their indexes."""
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


def downgrade() -> None:
    """Drop both tables and, with them, the decision reasons and the progress rows."""
    op.drop_table("erasure_progress")
    op.drop_index(op.f("ix_model_decision_model_id"), table_name="model_decision")
    op.drop_table("model_decision")
