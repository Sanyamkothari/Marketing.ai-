"""Plan K M141: `agentic_workspace_log`, one hash-chained decision ledger per client workspace.

Revision ID: 0010
Revises: 0009 (Plan K M132's `0009_agentic_ledger`)
Created: 2026-10-11

WHY a second table (DEC-1408 (c), design §13 q3): when one deployment serves several clients over MCP, each
client workspace keeps a chain of its own, so the receipts a client holds are checkpoints of a history that
holds only that client's entries. The deployment's own chain stays in `agentic_decision_log`, untouched: no row
of it moves and its hashes do not change. Here the key is `(workspace, seq)`: each workspace's `seq` counts from
1 from its own genesis, and every entry's hash covers its workspace id. The columns are otherwise the M132
ledger's, with the same indexes; the body is canonical JSON of ids, digests and codes, never a customer's data.

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. Reversible: `downgrade` drops the table,
which loses the workspaces' chains (export them first; `python -m engine.agentic.ledger verify --workspace`
checks a copy).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)
_TABLE = "agentic_workspace_log"


def upgrade() -> None:
    """Create `agentic_workspace_log` and its four lookup indexes."""
    op.create_table(
        _TABLE,
        sa.Column("workspace", _STR(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("prev_hash", _STR(), nullable=False),
        sa.Column("entry_hash", _STR(), nullable=False),
        sa.Column("kind", _STR(), nullable=False),
        sa.Column("actor", _STR(), nullable=False),
        sa.Column("body", _STR(), nullable=False),
        sa.Column("proposal_id", _STR(), nullable=True),
        sa.Column("evidence_id", _STR(), nullable=True),
        sa.Column("signature", _STR(), nullable=True),
        sa.Column("key_id", _STR(), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("workspace", "seq"),
    )
    op.create_index(op.f("ix_agentic_workspace_log_entry_hash"), _TABLE, ["entry_hash"], unique=True)
    op.create_index(op.f("ix_agentic_workspace_log_kind"), _TABLE, ["kind"], unique=False)
    op.create_index(op.f("ix_agentic_workspace_log_proposal_id"), _TABLE, ["proposal_id"], unique=False)
    op.create_index(op.f("ix_agentic_workspace_log_evidence_id"), _TABLE, ["evidence_id"], unique=False)


def downgrade() -> None:
    """Drop what `upgrade` added, and with it the workspaces' chains."""
    op.drop_index(op.f("ix_agentic_workspace_log_evidence_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_agentic_workspace_log_proposal_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_agentic_workspace_log_kind"), table_name=_TABLE)
    op.drop_index(op.f("ix_agentic_workspace_log_entry_hash"), table_name=_TABLE)
    op.drop_table(_TABLE)
