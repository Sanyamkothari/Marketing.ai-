"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Say WHY in this docstring, not what: the `op` calls below already say what. A migration is read
years later by someone deciding whether it is safe to run, and the answer is in the intent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # SQLModel columns are `sqlmodel.sql.sqltypes.AutoString`, not `sa.String`
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Undo this revision."""
    ${downgrades if downgrades else "pass"}
