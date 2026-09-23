"""Phase 4b M48: `consent_record`, `erasure_request` and `model_retrain_flag`.

Revision ID: 0003
Revises: 0002
Created: 2026-09-23

WHY these three tables and no others: the consent ledger, the register of erasure requests and the
flags that tell the scheduler which model versions to retrain are the only M48 state that is not an
artefact. Retention plans are recomputed from the store on every pass and access exports are
streamed, never stored (DEC-745), so neither needs a table.

WHY there is no column holding a data principal's id: every table is keyed by `principal_hash`, the
salted SHA-256 `engine.audit.events.principal_hash` computes, so neither the ledger nor the register
is a second copy of the customer list and erasing a customer never has to reach into the database
(DEC-733).

WHY `consent_record.seq` is an autoincrementing integer: the latest record as of a moment decides a
principal's consent, and two records stamped with the same instant need a tie-break that follows the
order they were written in (DEC-731).

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. The models are
`engine/privacy/tables.py`; this revision creates exactly what they declare. Reversible: `downgrade`
drops all three, which destroys the consent evidence - a decision, not a rollback step.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the three M48 tables and their indexes."""
    op.create_table(
        "consent_record",
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("client_id", _STR(), nullable=False),
        sa.Column("principal_hash", _STR(), nullable=False),
        sa.Column("purpose", _STR(), nullable=False),
        sa.Column("status", _STR(), nullable=False),
        sa.Column("source", _STR(), nullable=False),
        sa.Column("recorded_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("seq"),
    )
    # The one question the ledger is asked: this client, this purpose, these principals.
    op.create_index(
        "ix_consent_record_lookup", "consent_record", ["client_id", "purpose", "principal_hash"], unique=False
    )
    op.create_table(
        "erasure_request",
        sa.Column("request_id", _STR(), nullable=False),
        sa.Column("client_id", _STR(), nullable=True),
        sa.Column("principal_hash", _STR(), nullable=False),
        sa.Column("status", _STR(), nullable=False),
        sa.Column("mode", _STR(), nullable=False),
        sa.Column("store_counts_json", _STR(), nullable=False),
        sa.Column("models_flagged_json", _STR(), nullable=False),
        sa.Column("rows_deleted", sa.Integer(), nullable=False),
        sa.Column("rows_tombstoned", sa.Integer(), nullable=False),
        sa.Column("cells_masked", sa.Integer(), nullable=False),
        sa.Column("files_rewritten", sa.Integer(), nullable=False),
        sa.Column("files_deleted", sa.Integer(), nullable=False),
        sa.Column("requested_by", _STR(), nullable=False),
        sa.Column("requested_at", _TS, nullable=False),
        sa.Column("completed_at", _TS, nullable=True),
        sa.Column("error_code", _STR(), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        op.f("ix_erasure_request_principal_hash"), "erasure_request", ["principal_hash"], unique=False
    )
    op.create_table(
        "model_retrain_flag",
        sa.Column("flag_id", sa.Integer(), nullable=False),
        sa.Column("model_id", _STR(), nullable=False),
        sa.Column("request_id", _STR(), nullable=False),
        sa.Column("reason", _STR(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("cleared_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("flag_id"),
    )
    op.create_index(op.f("ix_model_retrain_flag_model_id"), "model_retrain_flag", ["model_id"], unique=False)
    op.create_index(
        op.f("ix_model_retrain_flag_request_id"), "model_retrain_flag", ["request_id"], unique=False
    )


def downgrade() -> None:
    """Drop the three tables. The consent ledger goes with them: see the module docstring."""
    op.drop_index(op.f("ix_model_retrain_flag_request_id"), table_name="model_retrain_flag")
    op.drop_index(op.f("ix_model_retrain_flag_model_id"), table_name="model_retrain_flag")
    op.drop_table("model_retrain_flag")
    op.drop_index(op.f("ix_erasure_request_principal_hash"), table_name="erasure_request")
    op.drop_table("erasure_request")
    op.drop_index("ix_consent_record_lookup", table_name="consent_record")
    op.drop_table("consent_record")
