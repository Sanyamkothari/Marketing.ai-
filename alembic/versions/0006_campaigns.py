"""Plan J M94: `campaign`, the one record every campaign measurement attaches to.

Revision ID: 0006
Revises: 0005
Created: 2026-10-07

WHY a table (DEC-1304 (a)): a campaign is found by its id, by the scoring run it came from and by
its use case (the Results list, a run's campaigns), and its status changes as outcomes arrive and are
measured. A row per campaign in the platform database, beside the audit trail that records who
created it and which test plan was registered for it, is what those queries need. The record itself
is `record_json` - the `engine.measurement.campaign.Campaign` model - so later milestones that add
optional fields to it (M103's external campaigns, M104's programme readouts) need no new revision for
them; the columns are only what a query filters or sorts on.

WHY no column holds a data value: the record holds ids, counts, dates and column names, never a
customer id or an outcome. The assignment and the outcomes, which do hold one row per customer, are
artefacts under `campaigns/<id>/`, registered with the privacy jobs like every row-level artefact.

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. The model is
`engine/measurement/campaign.py`; this revision creates exactly what it declares. Reversible:
`downgrade` drops the table, which loses the campaign records (their artefacts stay in the store).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create `campaign` and its two lookup indexes."""
    op.create_table(
        "campaign",
        sa.Column("campaign_id", _STR(), nullable=False),
        sa.Column("kind", _STR(), nullable=False),
        sa.Column("use_case_id", _STR(), nullable=True),
        sa.Column("run_id", _STR(), nullable=True),
        sa.Column("status", _STR(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("record_json", _STR(), nullable=False),
        sa.PrimaryKeyConstraint("campaign_id"),
    )
    op.create_index(op.f("ix_campaign_use_case_id"), "campaign", ["use_case_id"], unique=False)
    op.create_index(op.f("ix_campaign_run_id"), "campaign", ["run_id"], unique=False)


def downgrade() -> None:
    """Drop what `upgrade` added and, with it, every campaign record."""
    op.drop_index(op.f("ix_campaign_run_id"), table_name="campaign")
    op.drop_index(op.f("ix_campaign_use_case_id"), table_name="campaign")
    op.drop_table("campaign")
