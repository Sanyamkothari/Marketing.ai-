"""Plan K M132: `agentic_decision_log`, the hash-chained decision ledger.

Revision ID: 0009
Revises: 0007 (becomes 0008 when Plan K M131's `0008_consent_basis` is merged before it)
Created: 2026-10-10

WHY a table (DEC-1401): every agent proposal, every refusal, every approval and every evidence label is one
entry of an append-only chain (`engine.agentic.ledger`): `seq` orders it, `prev_hash` and `entry_hash` chain
it, `signature` and `key_id` sign the kinds that are signed. Nothing updates or deletes a row. The body is
canonical JSON of ids, digests, codes and the policy version: never a customer id, a cell value or free
text, so erasure never touches this table and never breaks the chain.

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. Reversible: `downgrade` drops the table,
which loses the ledger (export it first; `python -m engine.agentic.ledger verify` checks a copy).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0009"
# PLAN-K (M132): set to "0008" at integration, after M131's 0008_consent_basis (one linear history).
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)
_TABLE = "agentic_decision_log"


def upgrade() -> None:
    """Create `agentic_decision_log` and its two lookup indexes."""
    op.create_table(
        _TABLE,
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("prev_hash", _STR(), nullable=False),
        sa.Column("entry_hash", _STR(), nullable=False),
        sa.Column("kind", _STR(), nullable=False),
        sa.Column("actor", _STR(), nullable=False),
        sa.Column("body", _STR(), nullable=False),
        sa.Column("signature", _STR(), nullable=True),
        sa.Column("key_id", _STR(), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("seq"),
    )
    op.create_index(op.f("ix_agentic_decision_log_entry_hash"), _TABLE, ["entry_hash"], unique=True)
    op.create_index(op.f("ix_agentic_decision_log_kind"), _TABLE, ["kind"], unique=False)


def downgrade() -> None:
    """Drop what `upgrade` added, and with it the ledger."""
    op.drop_index(op.f("ix_agentic_decision_log_kind"), table_name=_TABLE)
    op.drop_index(op.f("ix_agentic_decision_log_entry_hash"), table_name=_TABLE)
    op.drop_table(_TABLE)
