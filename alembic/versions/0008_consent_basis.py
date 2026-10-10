"""Plan K M131: `consent_record.basis` and `consent_record.operator_registered`.

Revision ID: 0008
Revises: 0007
Created: 2026-10-10

WHY (DEC-1323): TRAI's Third Amendment treats an inquiry-based consent as valid for seven days only,
and counts a legacy consent only when it is registered on the telecom operators' digital platform.
The ledger therefore records how a consent was obtained (`basis`: explicit, inquiry or legacy) and
whether it is operator-registered. Both are nullable: null means not recorded, read as explicit and
never as registered, so every record stored before this revision behaves as it did (the rules apply
only where a region file asks for them).

Reversible: `downgrade` drops both columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString


def upgrade() -> None:
    """Add nullable `basis` and `operator_registered` to `consent_record`."""
    op.add_column("consent_record", sa.Column("basis", _STR(), nullable=True))
    op.add_column("consent_record", sa.Column("operator_registered", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop `operator_registered` and `basis` from `consent_record`."""
    op.drop_column("consent_record", "operator_registered")
    op.drop_column("consent_record", "basis")
