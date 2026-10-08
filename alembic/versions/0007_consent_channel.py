"""Plan J M99: `consent_record.channel`, channel-aware consent.

Revision ID: 0007
Revises: 0006
Created: 2026-10-09

WHY a channel column (DEC-1309): consent is per-purpose and per-channel (a customer may opt out
of SMS while remaining opted in to email). Nullable: `channel is null` means all channels, so every
record stored before this revision continues to apply to every channel without migration or
re-importing (DEC-1309 (b)).

Reversible: `downgrade` drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString


def upgrade() -> None:
    """Add nullable `channel` to `consent_record`."""
    op.add_column("consent_record", sa.Column("channel", _STR(), nullable=True))


def downgrade() -> None:
    """Drop `channel` from `consent_record`."""
    op.drop_column("consent_record", "channel")
